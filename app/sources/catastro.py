"""Conector con el Catastro espanol (Sede Electronica + servicios INSPIRE).

Fuente publica, gratuita y sin credenciales. Aporta lo que ningun portal da:
la *geometria real* y la superficie oficial de la parcela, que es lo que
alimenta la simulacion 2D/3D de implantacion.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from app.sources.base import BaseSource, SourceError, SourceStatus

GML_NS = {
    "gml": "http://www.opengis.net/gml/3.2",
    "cp": "urn:x-inspire:specification:gmlas:CadastralParcels:3.0",
}


class CatastroSource(BaseSource):
    key = "catastro"
    name = "Catastro (INSPIRE / Sede Electronica)"
    kind = "cadastre"
    required = True
    docs_url = "https://www.catastro.hacienda.gob.es/webinspire/index.html"
    licence = "Datos abiertos, uso libre citando la fuente."

    # -- referencia catastral a partir de coordenadas ---------------------

    @property
    def _coords_url(self) -> str:
        return (f"{self.settings.catastro_ovc_url}/ovcservweb/"
                "OVCSWLocalizacionRC/OVCCoordenadas.asmx")

    def ref_from_coords(self, lat: float, lon: float) -> str | None:
        """Referencia catastral de la parcela que contiene exactamente el punto."""
        response = self.request(
            "GET", f"{self._coords_url}/Consulta_RCCOOR",
            params={"SRS": "EPSG:4326", "Coordenada_X": f"{lon}", "Coordenada_Y": f"{lat}"},
        )
        if response.status_code != 200:
            raise SourceError(f"Catastro Consulta_RCCOOR HTTP {response.status_code}")
        return self.parse_ref(response.text)

    @staticmethod
    def parse_ref(xml_text: str) -> str | None:
        """Extrae la referencia, o explica por que no hay ninguna.

        El servicio devuelve HTTP 200 tambien cuando falla, con el motivo
        dentro del cuerpo. Ignorarlo hacia que un error de parametros
        pareciera "aqui no hay parcela", que es un diagnostico muy distinto.
        """
        root = ET.fromstring(xml_text)
        error = _first_text(root, "des")
        pc1, pc2 = _first_text(root, "pc1"), _first_text(root, "pc2")
        if pc1 and pc2:
            return f"{pc1}{pc2}"
        if error:
            raise SourceError(f"Catastro: {error}")
        return None

    def refs_near(self, lat: float, lon: float) -> list[dict[str, Any]]:
        """Parcelas cercanas al punto, ordenadas por distancia.

        Hace falta porque un punto puede caer en una carretera, en marisma o
        en un hueco sin parcela, y entonces la consulta exacta no devuelve
        nada aunque haya suelo alrededor. Este servicio del Catastro existe
        justo para ese caso.
        """
        response = self.request(
            "GET", f"{self._coords_url}/Consulta_RCCOOR_Distancia",
            params={"SRS": "EPSG:4326", "Coordenada_X": f"{lon}", "Coordenada_Y": f"{lat}"},
        )
        if response.status_code != 200:
            raise SourceError(f"Catastro Consulta_RCCOOR_Distancia HTTP {response.status_code}")
        return self.parse_refs_near(response.text)

    @staticmethod
    def parse_refs_near(xml_text: str) -> list[dict[str, Any]]:
        root = ET.fromstring(xml_text)
        parcelas: list[dict[str, Any]] = []
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] != "pcd":
                continue
            pc1, pc2 = _first_text(element, "pc1"), _first_text(element, "pc2")
            if not (pc1 and pc2):
                continue
            distance = _first_text(element, "dis")
            parcelas.append({
                "cadastral_ref": f"{pc1}{pc2}",
                "distance_m": float(distance) if distance and distance.isdigit() else None,
                "address": _first_text(element, "ldt") or "",
            })
        parcelas.sort(key=lambda p: p["distance_m"] if p["distance_m"] is not None else 1e9)
        return parcelas

    # -- geometria de la parcela -----------------------------------------

    def coords_for_ref(self, cadastral_ref: str) -> tuple[float, float] | None:
        """Coordenadas de una parcela por su referencia catastral.

        Existe como respaldo del WFS de INSPIRE, que sirve la geometría pero no
        siempre responde a las parcelas rústicas: son las que traen las
        subastas del BOE, y sin coordenadas la candidata se descartaba entera
        aunque el anuncio trajera su referencia. Este servicio no da polígono,
        sólo el punto, que es suficiente para situar la parcela y analizarla.

        Consulta_CPMRC admite provincia y municipio vacíos: con la referencia
        completa el propio Catastro los deduce.
        """
        response = self.request(
            "GET", f"{self._coords_url}/Consulta_CPMRC",
            params={"Provincia": "", "Municipio": "", "SRS": "EPSG:4326",
                    "RC": cadastral_ref},
        )
        if response.status_code != 200:
            raise SourceError(f"Catastro Consulta_CPMRC HTTP {response.status_code}")
        return self.parse_coords(response.text)

    @staticmethod
    def parse_coords(xml_text: str) -> tuple[float, float] | None:
        """Punto (lat, lon) de una respuesta de Consulta_CPMRC.

        Como el resto de servicios OVC, contesta 200 también cuando falla y
        pone el motivo en el cuerpo: tomarlo por «no hay parcela» ocultaría un
        error de parámetros, que es un diagnóstico distinto.
        """
        root = ET.fromstring(xml_text)
        xcen, ycen = _first_text(root, "xcen"), _first_text(root, "ycen")
        if xcen and ycen:
            try:
                # El servicio devuelve xcen como longitud e ycen como latitud.
                return float(ycen), float(xcen)
            except ValueError:
                return None
        error = _first_text(root, "des")
        if error:
            raise SourceError(f"Catastro: {error}")
        return None

    def parcel_geometry(self, cadastral_ref: str) -> dict[str, Any] | None:
        """Descarga el poligono de una parcela por referencia catastral.

        Devuelve un Feature GeoJSON con el anillo exterior en [lon, lat].
        """
        url = f"{self.settings.catastro_inspire_url}/wfsCP.aspx"
        params = {
            "service": "wfs",
            "version": "2.0.0",
            "request": "getfeature",
            "STOREDQUERIE_ID": "GetParcel",
            "refcat": cadastral_ref,
            "srsname": "EPSG::4326",
        }
        response = self.request("GET", url, params=params)
        if response.status_code != 200:
            raise SourceError(f"Catastro WFS HTTP {response.status_code}")
        return self.parse_parcel_gml(response.text, cadastral_ref)

    @staticmethod
    def parse_parcel_gml(xml_text: str, cadastral_ref: str = "") -> dict[str, Any] | None:
        """Extrae el anillo exterior y el area oficial de una respuesta GML.

        El Catastro sirve EPSG:4326 en orden de eje lat,lon (el orden oficial
        de ese CRS), mientras GeoJSON exige lon,lat: por eso se invierte aqui.
        """
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise SourceError(f"GML de Catastro ilegible: {exc}") from None

        pos_list = _first_element(root, "posList")
        if pos_list is None or not (pos_list.text or "").strip():
            return None

        values = [float(v) for v in pos_list.text.split()]
        ring = [[values[i + 1], values[i]] for i in range(0, len(values) - 1, 2)]
        if len(ring) < 4:
            return None
        if ring[0] != ring[-1]:
            ring.append(ring[0])

        area_text = _first_text(root, "areaValue")
        properties: dict[str, Any] = {"cadastral_ref": cadastral_ref, "source": "catastro"}
        if area_text:
            try:
                properties["official_area_m2"] = float(area_text)
            except ValueError:
                pass

        label = _first_text(root, "label")
        if label:
            properties["label"] = label

        return {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": properties,
        }

    def parcel_at(self, lat: float, lon: float) -> dict[str, Any] | None:
        """Poligono de la parcela del punto, o de la mas cercana.

        Se prueba primero la consulta exacta y, si no devuelve nada, las
        parcelas del entorno: es lo que evita que un punto caido en un camino
        o en marisma deje la simulacion sin geometria real.
        """
        detail = self.parcel_at_detail(lat, lon)
        return detail.get("feature")

    def parcel_at_detail(self, lat: float, lon: float) -> dict[str, Any]:
        """Igual que parcel_at pero contando que se intento y con que resultado."""
        info: dict[str, Any] = {"lat": lat, "lon": lon, "steps": []}

        try:
            reference = self.ref_from_coords(lat, lon)
            info["steps"].append({"step": "exacta", "cadastral_ref": reference})
        except SourceError as exc:
            reference = None
            info["steps"].append({"step": "exacta", "error": str(exc)[:200]})

        if not reference:
            try:
                nearby = self.refs_near(lat, lon)
                info["steps"].append({
                    "step": "cercanas",
                    "found": len(nearby),
                    "closest": nearby[:5],
                })
                if nearby:
                    reference = nearby[0]["cadastral_ref"]
                    info["used_nearby"] = True
                    info["distance_m"] = nearby[0]["distance_m"]
            except SourceError as exc:
                info["steps"].append({"step": "cercanas", "error": str(exc)[:200]})

        if not reference:
            info["feature"] = None
            info["reason"] = (
                "El Catastro no encuentra ninguna parcela en el punto ni en su "
                "entorno inmediato. Suele pasar sobre agua, viales o dominio "
                "publico; prueba a mover el punto unos metros hacia suelo."
            )
            return info

        info["cadastral_ref"] = reference
        try:
            feature = self.parcel_geometry(reference)
        except SourceError as exc:
            info["feature"] = None
            info["reason"] = f"Referencia {reference} localizada pero sin geometria: {exc}"
            return info

        info["feature"] = feature
        if feature is None:
            info["reason"] = f"El WFS no devolvio geometria para {reference}."
        return info

    def check(self) -> SourceStatus:
        url = f"{self.settings.catastro_ovc_url}/ovcservweb/OVCSWLocalizacionRC/OVCCoordenadas.asmx/Consulta_RCCOOR"
        # Punto de prueba en Malaga capital: si responde XML, el servicio esta vivo.
        return self._timed_probe(
            url,
            params={"SRS": "EPSG:4326", "Coordenada_X": "-4.4214", "Coordenada_Y": "36.7213"},
        )


def _first_element(root: ET.Element, local_name: str) -> ET.Element | None:
    """Busca por nombre local, ignorando el namespace (el Catastro los varia)."""
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == local_name:
            return element
    return None


def _first_text(root: ET.Element, local_name: str) -> str | None:
    element = _first_element(root, local_name)
    if element is None or element.text is None:
        return None
    return element.text.strip() or None

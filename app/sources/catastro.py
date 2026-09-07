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

    def ref_from_coords(self, lat: float, lon: float) -> str | None:
        """Devuelve la referencia catastral de la parcela que contiene el punto."""
        url = f"{self.settings.catastro_ovc_url}/ovcservweb/OVCSWLocalizacionRC/OVCCoordenadas.asmx/Consulta_RCCOOR"
        params = {"SRS": "EPSG:4326", "Coordenada_X": f"{lon}", "Coordenada_Y": f"{lat}"}
        with self.client() as client:
            response = client.get(url, params=params)
        if response.status_code != 200:
            raise SourceError(f"Catastro Consulta_RCCOOR HTTP {response.status_code}")

        root = ET.fromstring(response.text)
        pc1 = _first_text(root, "pc1")
        pc2 = _first_text(root, "pc2")
        if pc1 and pc2:
            return f"{pc1}{pc2}"
        return None

    # -- geometria de la parcela -----------------------------------------

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
        with self.client() as client:
            response = client.get(url, params=params)
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
        """Atajo: de coordenadas a poligono de parcela en un paso."""
        ref = self.ref_from_coords(lat, lon)
        if not ref:
            return None
        return self.parcel_geometry(ref)

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

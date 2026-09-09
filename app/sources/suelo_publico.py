"""Organismos públicos que venden suelo, y por dónde se les lee.

Buscando de dónde más pueden salir solares baratos, los que de verdad mueven
volumen en España no son portales inmobiliarios sino cuatro organismos que
llevan años desprendiéndose de patrimonio:

  * **SEPES**, la entidad pública de suelo, que urbaniza y vende parcelas
    industriales y residenciales por toda España.
  * **ADIF**, que enajena el suelo ferroviario que deja de necesitar.
  * **INVIED**, que vende los acuartelamientos y solares que Defensa desafecta.
  * **Patrimonio del Estado**, que saca el resto de inmuebles de la
    Administración General.

Ninguno publica una API, y sus webs son páginas de noticias y pliegos en PDF
que cambian de maquetación cada temporada: raspar las cuatro sería garantizar
cuatro roturas. Pero los cuatro están obligados por ley a anunciar cada venta
en el BOE, que sí es un XML estable, documentado y sin clave. Así que aquí no
se raspa nada: se catalogan, se comprueba que el sitio responde -para poder
enlazar el pliego y las condiciones- y el suelo se descubre por el Boletín,
con `BoeAnunciosSource`.

Se registran igualmente en el panel, y no se ocultan, porque quien mira las
fuentes necesita saber que estos vendedores existen y por qué vía llegan sus
solares a la aplicación.
"""
from __future__ import annotations

from app.sources.base import BaseSource, SourceStatus


class _VendedorPublico(BaseSource):
    """Vendedor público de suelo que se descubre a través del BOE.

    `kind` es "catalog" y no "listings" a propósito: el panel usa "listings"
    para decidir si hay ofertas consultables, y marcar estas fuentes como tales
    haría que anunciara ofertas disponibles cuando lo único comprobado es que
    su web responde.
    """

    kind = "catalog"
    required = False
    # Cada uno pone lo suyo.
    home_url = ""
    boe_hint = ""

    @property
    def base_url(self) -> str:
        return self.home_url

    def check(self) -> SourceStatus:
        estado = self._timed_probe(self.home_url)
        # El detalle explica la vía real de lectura: sin esto, un "ok" aquí se
        # entiende como "de aquí salen anuncios", que no es el caso.
        estado.detail = f"{estado.detail} {self.boe_hint}".strip()
        estado.extra["discovered_via"] = "boe_anuncios"
        return estado


class SepesSource(_VendedorPublico):
    key = "sepes"
    name = "SEPES · Entidad Pública Empresarial de Suelo"
    home_url = "https://www.sepes.es"
    docs_url = "https://www.sepes.es"
    licence = (
        "Información del sector público. Reutilización conforme a la Ley "
        "37/2007, citando la fuente."
    )
    radius_note = (
        "No usa radio: sus parcelas se descubren por los anuncios del BOE, que "
        "es donde SEPES publica cada licitación."
    )
    boe_hint = (
        "Sus parcelas industriales y residenciales se leen desde los anuncios "
        "del BOE, no raspando la web."
    )


class AdifSuelosSource(_VendedorPublico):
    key = "adif_suelos"
    name = "ADIF · Venta de inmuebles y suelo ferroviario"
    home_url = "https://www.adif.es/servicios/inmuebles/venta"
    docs_url = "https://www.adif.es/servicios/inmuebles"
    licence = (
        "Información del sector público. Reutilización conforme a la Ley "
        "37/2007, citando la fuente."
    )
    radius_note = (
        "No usa radio: sus subastas de parcelas se descubren por los anuncios "
        "del BOE."
    )
    boe_hint = (
        "Sus subastas de parcelas sobrantes se leen desde los anuncios del BOE."
    )


class InviedSource(_VendedorPublico):
    key = "invied"
    name = "INVIED · Venta de inmuebles de Defensa"
    home_url = "https://www.defensa.gob.es/invied/02-ventas-inmuebles/"
    docs_url = "https://www.defensa.gob.es/invied/"
    licence = (
        "Información del sector público. Reutilización conforme a la Ley "
        "37/2007, citando la fuente."
    )
    radius_note = (
        "No usa radio: sus subastas en sobre cerrado se descubren por los "
        "anuncios del BOE."
    )
    boe_hint = (
        "Vende por subasta en sobre cerrado, sin identificador del Portal de "
        "Subastas: sus solares sólo aparecen leyendo el anuncio del BOE."
    )


class PatrimonioEstadoSource(_VendedorPublico):
    key = "patrimonio_estado"
    name = "Patrimonio del Estado · Enajenación de inmuebles"
    home_url = "https://www.hacienda.gob.es/es-ES/Areas%20Tematicas/Patrimonio%20del%20Estado/Gestion%20Patrimonial/Paginas/Enajenaciones.aspx"
    docs_url = "https://www.hacienda.gob.es/es-ES/Areas%20Tematicas/Patrimonio%20del%20Estado/Paginas/default.aspx"
    licence = (
        "Información del sector público. Reutilización conforme a la Ley "
        "37/2007, citando la fuente."
    )
    radius_note = (
        "No usa radio: sus enajenaciones se descubren por los anuncios del BOE."
    )
    boe_hint = (
        "Las enajenaciones de la Administración General se leen desde los "
        "anuncios del BOE."
    )


def iter_public_land_sources() -> list[BaseSource]:
    """Los vendedores públicos de suelo que conoce la aplicación."""
    return [
        SepesSource(),
        AdifSuelosSource(),
        InviedSource(),
        PatrimonioEstadoSource(),
    ]

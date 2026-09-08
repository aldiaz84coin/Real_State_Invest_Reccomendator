"""Puntos de partida del simulador.

Sin un punto concreto el simulador arranca vacio y hay que teclear
coordenadas, que es justo la barrera que impide probarlo. Estos presets son
zonas reales donde la combinacion que busca la aplicacion tiene sentido:
suelo asequible, costa o montana cerca y mercado de alquiler turistico.

Las coordenadas apuntan a suelo no urbano de cada zona; la parcela concreta
la resuelve el Catastro al simular, asi que lo que se fija es el punto, no una
referencia catastral que podria quedar obsoleta.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class LocationPreset:
    id: str
    name: str
    region: str
    lat: float
    lon: float
    zone_type: str          # costa | montana | rural
    ccaa: str               # para la fiscalidad de la compra
    typical_price_eur: float
    typical_area_m2: float
    note: str

    def as_dict(self) -> dict:
        return asdict(self)


PRESETS: list[LocationPreset] = [
    LocationPreset(
        id="cantabria-noja",
        name="Noja, marismas de Santoña",
        region="Cantabria",
        lat=43.4869,
        lon=-3.5290,
        zone_type="costa",
        ccaa="Cantabria",
        typical_price_eur=48000.0,
        typical_area_m2=1200.0,
        note=(
            "Costa cantábrica con temporada alta corta pero intensa. Suelo muy "
            "por debajo del Mediterráneo y parque natural al lado."
        ),
    ),
    LocationPreset(
        id="cantabria-san-vicente",
        name="San Vicente de la Barquera",
        region="Cantabria",
        lat=43.3880,
        lon=-4.3980,
        zone_type="costa",
        ccaa="Cantabria",
        typical_price_eur=52000.0,
        typical_area_m2=1000.0,
        note="Playa y Picos de Europa a media hora: sirve para verano e invierno.",
    ),
    LocationPreset(
        id="cantabria-potes",
        name="Potes, Liébana",
        region="Cantabria",
        lat=43.1540,
        lon=-4.6210,
        zone_type="montana",
        ccaa="Cantabria",
        typical_price_eur=35000.0,
        typical_area_m2=1500.0,
        note="Montaña con estacionalidad invertida: llena en invierno y otoño.",
    ),
    LocationPreset(
        id="malaga-nerja",
        name="Nerja, Axarquía",
        region="Málaga",
        lat=36.7476,
        lon=-3.8760,
        zone_type="costa",
        ccaa="Andalucia",
        typical_price_eur=75000.0,
        typical_area_m2=900.0,
        note="Mercado turístico maduro; tarifas altas y suelo caro.",
    ),
    LocationPreset(
        id="asturias-llanes",
        name="Llanes",
        region="Asturias",
        lat=43.4212,
        lon=-4.7554,
        zone_type="costa",
        ccaa="Asturias",
        typical_price_eur=55000.0,
        typical_area_m2=1100.0,
        note="Costa verde con demanda creciente y suelo aún contenido.",
    ),
    LocationPreset(
        id="huesca-jaca",
        name="Jaca, Pirineo",
        region="Huesca",
        lat=42.5700,
        lon=-0.5490,
        zone_type="montana",
        ccaa="Aragon",
        typical_price_eur=45000.0,
        typical_area_m2=1300.0,
        note="Estaciones de esquí cerca: temporada alta en invierno.",
    ),
]

PRESETS_BY_ID = {preset.id: preset for preset in PRESETS}

# El simulador arranca aqui: Cantabria, costa, suelo asequible.
DEFAULT_PRESET_ID = "cantabria-noja"


def get_preset(preset_id: str | None) -> LocationPreset:
    return PRESETS_BY_ID.get(preset_id or "", PRESETS_BY_ID[DEFAULT_PRESET_ID])


def list_presets() -> list[dict]:
    return [preset.as_dict() for preset in PRESETS]

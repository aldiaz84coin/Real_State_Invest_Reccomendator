"""Ficha visual de cada vivienda prefabricada, dibujada a escala.

No se usan fotografias del fabricante: no se pueden descargar en este entorno y
ademas son material ajeno. En su lugar se dibuja el modulo a partir de sus
dimensiones reales (largo, ancho y alto del catalogo), que para decidir entre
modelos es mas informativo que una foto de catalogo, porque deja ver la
proporcion real y el reparto interior. La ficha enlaza la referencia comercial
para quien quiera ver la foto.
"""
from __future__ import annotations

from app.simulation.catalog import PrefabModel

PALETTE = {
    "wall": "#efe7db",
    "wall_dark": "#d8cbb8",
    "outline": "#6b6055",
    "roof": "#4a4642",
    "glass": "#7fb4cc",
    "glass_dark": "#4d8ba6",
    "door": "#a9683c",
    "floor": "#f6f2ea",
    "room": "#e9e2d5",
    "wet": "#dbe6ea",
    "text": "#3a342c",
    "dim": "#8d8375",
    "ground": "#c8cdb4",
}


def render_model_card_svg(model: PrefabModel, width: int = 420, height: int = 250) -> str:
    """Alzado frontal y planta del modelo, a escala y acotados."""
    margin = 26
    gap = 18
    # Ambas vistas comparten escala para que se puedan comparar de un vistazo.
    usable_w = width - 2 * margin - gap
    elevation_w = usable_w * 0.56
    plan_w = usable_w - elevation_w

    scale_elevation = elevation_w / max(model.length_m, 0.1)
    scale_plan = plan_w / max(model.length_m, 0.1)
    scale = min(scale_elevation, scale_plan)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="100%" height="auto" role="img" '
        f'aria-label="Esquema a escala de {model.name}">',
        f'<rect width="{width}" height="{height}" fill="{PALETTE["floor"]}" rx="10"/>',
    ]

    parts.append(_elevation(model, margin, 42, scale))
    parts.append(_plan(model, margin + elevation_w + gap, 42, scale))
    parts.append(
        f'<text x="{margin}" y="24" font-family="system-ui,sans-serif" font-size="12" '
        f'font-weight="600" fill="{PALETTE["text"]}">Alzado</text>'
        f'<text x="{margin + elevation_w + gap:.0f}" y="24" font-family="system-ui,sans-serif" '
        f'font-size="12" font-weight="600" fill="{PALETTE["text"]}">Planta</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _elevation(model: PrefabModel, x: float, y: float, scale: float) -> str:
    """Fachada larga: la que da a la terraza y concentra el acristalamiento."""
    w = model.length_m * scale
    h = model.height_m * scale
    base = y + h

    overhang = 0.35 * scale
    roof_h = max(6.0, 0.28 * scale)

    parts = [
        # Terreno
        f'<line x1="{x - 10:.1f}" y1="{base:.1f}" x2="{x + w + 10:.1f}" y2="{base:.1f}" '
        f'stroke="{PALETTE["ground"]}" stroke-width="4" stroke-linecap="round"/>',
        # Muro
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
        f'fill="{PALETTE["wall"]}" stroke="{PALETTE["outline"]}" stroke-width="1.6" rx="1.5"/>',
        # Cubierta con vuelo
        f'<rect x="{x - overhang:.1f}" y="{y - roof_h:.1f}" width="{w + 2 * overhang:.1f}" '
        f'height="{roof_h:.1f}" fill="{PALETTE["roof"]}" rx="1.5"/>',
    ]

    # Ventanal continuo: ocupa la franja central de la fachada.
    band_y = y + h * 0.30
    band_h = h * 0.34
    band_x = x + w * 0.06
    band_w = w * 0.60
    parts.append(
        f'<rect x="{band_x:.1f}" y="{band_y:.1f}" width="{band_w:.1f}" height="{band_h:.1f}" '
        f'fill="{PALETTE["glass"]}" stroke="{PALETTE["glass_dark"]}" stroke-width="1.2"/>'
    )
    # Montantes: uno por dormitorio, que es lo que marca el ritmo de la fachada.
    for i in range(1, max(model.bedrooms, 1) + 1):
        mx = band_x + band_w * i / (max(model.bedrooms, 1) + 1)
        parts.append(
            f'<line x1="{mx:.1f}" y1="{band_y:.1f}" x2="{mx:.1f}" '
            f'y2="{band_y + band_h:.1f}" stroke="{PALETTE["glass_dark"]}" stroke-width="1"/>'
        )

    # Puerta
    door_w = min(0.95 * scale, w * 0.16)
    door_h = h * 0.62
    door_x = x + w * 0.76
    parts.append(
        f'<rect x="{door_x:.1f}" y="{base - door_h:.1f}" width="{door_w:.1f}" '
        f'height="{door_h:.1f}" fill="{PALETTE["door"]}" stroke="{PALETTE["outline"]}" '
        f'stroke-width="1.2" rx="1"/>'
    )

    parts.append(_dimension_h(x, base + 16, w, f"{model.length_m:g} m"))
    parts.append(_dimension_v(x - 14, y, h, f"{model.height_m:g} m"))
    return "".join(parts)


def _plan(model: PrefabModel, x: float, y: float, scale: float) -> str:
    """Distribucion orientativa segun dormitorios y banos del catalogo."""
    w = model.length_m * scale
    d = model.width_m * scale

    parts = [
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{d:.1f}" '
        f'fill="{PALETTE["room"]}" stroke="{PALETTE["outline"]}" stroke-width="1.6"/>'
    ]

    # La zona de dia ocupa algo menos de la mitad; el resto se reparte entre
    # dormitorios y banos, que es la distribucion tipica de estos modulos.
    day_w = w * 0.42
    parts.append(
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{day_w:.1f}" height="{d:.1f}" '
        f'fill="{PALETTE["wall"]}" stroke="{PALETTE["outline"]}" stroke-width="1"/>'
        f'<text x="{x + day_w / 2:.1f}" y="{y + d / 2:.1f}" text-anchor="middle" '
        f'dominant-baseline="middle" font-family="system-ui,sans-serif" font-size="8.5" '
        f'fill="{PALETTE["dim"]}">salón</text>'
    )

    rest_x = x + day_w
    rest_w = w - day_w
    rooms = max(model.bedrooms, 1)
    bath_d = d * (0.34 if model.bathrooms >= 2 else 0.28)

    # Banos en una franja, dormitorios en la otra.
    parts.append(
        f'<rect x="{rest_x:.1f}" y="{y:.1f}" width="{rest_w:.1f}" height="{bath_d:.1f}" '
        f'fill="{PALETTE["wet"]}" stroke="{PALETTE["outline"]}" stroke-width="1"/>'
        f'<text x="{rest_x + rest_w / 2:.1f}" y="{y + bath_d / 2:.1f}" text-anchor="middle" '
        f'dominant-baseline="middle" font-family="system-ui,sans-serif" font-size="8"'
        f' fill="{PALETTE["dim"]}">{model.bathrooms} baño{"s" if model.bathrooms > 1 else ""}</text>'
    )

    room_w = rest_w / rooms
    for i in range(rooms):
        rx = rest_x + i * room_w
        parts.append(
            f'<rect x="{rx:.1f}" y="{y + bath_d:.1f}" width="{room_w:.1f}" '
            f'height="{d - bath_d:.1f}" fill="{PALETTE["wall"]}" '
            f'stroke="{PALETTE["outline"]}" stroke-width="1"/>'
            f'<text x="{rx + room_w / 2:.1f}" y="{y + bath_d + (d - bath_d) / 2:.1f}" '
            f'text-anchor="middle" dominant-baseline="middle" '
            f'font-family="system-ui,sans-serif" font-size="8" fill="{PALETTE["dim"]}">dorm</text>'
        )

    parts.append(_dimension_h(x, y + d + 16, w, f"{model.length_m:g} m"))
    parts.append(_dimension_v(x + w + 14, y, d, f"{model.width_m:g} m"))
    return "".join(parts)


def _dimension_h(x: float, y: float, length: float, label: str) -> str:
    return (
        f'<g stroke="{PALETTE["dim"]}" stroke-width="1">'
        f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x + length:.1f}" y2="{y:.1f}"/>'
        f'<line x1="{x:.1f}" y1="{y - 3:.1f}" x2="{x:.1f}" y2="{y + 3:.1f}"/>'
        f'<line x1="{x + length:.1f}" y1="{y - 3:.1f}" x2="{x + length:.1f}" y2="{y + 3:.1f}"/>'
        f'</g><text x="{x + length / 2:.1f}" y="{y + 12:.1f}" text-anchor="middle" '
        f'font-family="system-ui,sans-serif" font-size="9" fill="{PALETTE["dim"]}">{label}</text>'
    )


def _dimension_v(x: float, y: float, length: float, label: str) -> str:
    return (
        f'<g stroke="{PALETTE["dim"]}" stroke-width="1">'
        f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x:.1f}" y2="{y + length:.1f}"/>'
        f'<line x1="{x - 3:.1f}" y1="{y:.1f}" x2="{x + 3:.1f}" y2="{y:.1f}"/>'
        f'<line x1="{x - 3:.1f}" y1="{y + length:.1f}" x2="{x + 3:.1f}" y2="{y + length:.1f}"/>'
        f'</g><text x="{x:.1f}" y="{y + length / 2:.1f}" text-anchor="middle" '
        f'transform="rotate(-90 {x:.1f} {y + length / 2:.1f})" dy="-4" '
        f'font-family="system-ui,sans-serif" font-size="9" fill="{PALETTE["dim"]}">{label}</text>'
    )

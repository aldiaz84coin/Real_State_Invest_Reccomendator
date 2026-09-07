"""Plano de implantacion en 2D como SVG.

Se genera en el servidor y no en el navegador para que el mismo dibujo sirva
en la pantalla, en el PDF del plan de negocio y en cualquier exportacion.
"""
from __future__ import annotations

import math
from typing import Any, Sequence

PALETTE = {
    "parcel_fill": "#eef4e6",
    "parcel_stroke": "#5b7a4a",
    "buildable_fill": "#ffffff",
    "buildable_stroke": "#9bb08c",
    "house_fill": "#c9793f",
    "house_stroke": "#8a4f24",
    "terrace_fill": "#e6cfa8",
    "terrace_stroke": "#b99a68",
    "parking_fill": "#d5d7da",
    "parking_stroke": "#9aa0a6",
    "pool_fill": "#7fc4e8",
    "pool_stroke": "#3f8db3",
    "driveway": "#b9a68a",
    "tree": "#6f9d5a",
    "text": "#243018",
    "dim": "#7a6a55",
}


def render_site_plan_svg(
    plan: dict[str, Any], *, width: int = 900, height: int = 680, margin: int = 60
) -> str:
    """Dibuja el plano en planta a partir de la escena calculada.

    En pantalla el eje Y crece hacia abajo y en el terreno crece hacia el
    norte, por eso la transformacion invierte Y: asi el norte queda arriba,
    como en cualquier plano.
    """
    parcel = (plan.get("parcel") or {}).get("meters") or []
    if len(parcel) < 3:
        return _empty_svg(width, height, "Sin geometria de parcela")

    xs = [p[0] for p in parcel]
    ys = [p[1] for p in parcel]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    span_x = max(max_x - min_x, 1e-6)
    span_y = max(max_y - min_y, 1e-6)
    scale = min((width - 2 * margin) / span_x, (height - 2 * margin) / span_y)

    offset_x = (width - span_x * scale) / 2
    offset_y = (height - span_y * scale) / 2

    def project(point: Sequence[float]) -> tuple[float, float]:
        return (
            offset_x + (point[0] - min_x) * scale,
            height - offset_y - (point[1] - min_y) * scale,
        )

    def path(ring: Sequence[Sequence[float]]) -> str:
        return " ".join(
            f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}"
            for i, (x, y) in enumerate(project(p) for p in ring)
        ) + " Z"

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="100%" height="auto" role="img" aria-label="Plano de implantacion">',
        _defs(),
        f'<rect width="{width}" height="{height}" fill="#fbfaf7"/>',
    ]

    # Parcela y area edificable.
    parts.append(
        f'<path d="{path(parcel)}" fill="{PALETTE["parcel_fill"]}" '
        f'stroke="{PALETTE["parcel_stroke"]}" stroke-width="2.5"/>'
    )
    buildable = (plan.get("buildable") or {}).get("meters") or []
    if len(buildable) >= 3:
        parts.append(
            f'<path d="{path(buildable)}" fill="{PALETTE["buildable_fill"]}" '
            f'fill-opacity="0.55" stroke="{PALETTE["buildable_stroke"]}" '
            f'stroke-width="1.4" stroke-dasharray="7 5"/>'
        )

    # Camino de acceso.
    driveway_m = plan.get("driveway_meters") or []
    if len(driveway_m) >= 2:
        points = " ".join(f"{x:.1f},{y:.1f}" for x, y in (project(p) for p in driveway_m))
        parts.append(
            f'<polyline points="{points}" fill="none" stroke="{PALETTE["driveway"]}" '
            f'stroke-width="{max(3.0, 3.0 * scale / 4):.1f}" stroke-linecap="round" '
            f'stroke-linejoin="round" stroke-opacity="0.75"/>'
        )

    # Arbolado, por debajo de lo construido.
    for tree in plan.get("trees", []):
        cx, cy = project((tree["x"], tree["y"]))
        radius = max(2.5, tree.get("radius_m", 1.5) * scale)
        parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" '
            f'fill="{PALETTE["tree"]}" fill-opacity="0.45"/>'
        )

    # Elementos construidos.
    for key, fill, stroke, label in (
        ("parking", PALETTE["parking_fill"], PALETTE["parking_stroke"], "Aparcamiento"),
        ("pool", PALETTE["pool_fill"], PALETTE["pool_stroke"], "Piscina"),
        ("terrace", PALETTE["terrace_fill"], PALETTE["terrace_stroke"], "Terraza"),
    ):
        element = plan.get(key)
        ring = (element or {}).get("meters")
        if not ring:
            continue
        parts.append(
            f'<path d="{path(ring)}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
        )
        parts.append(_label(project, ring, label, scale))

    house = plan.get("house") or {}
    house_ring = house.get("meters")
    if house_ring:
        parts.append(
            f'<path d="{path(house_ring)}" fill="{PALETTE["house_fill"]}" '
            f'stroke="{PALETTE["house_stroke"]}" stroke-width="2.2" filter="url(#drop)"/>'
        )
        parts.append(_label(project, house_ring, f'{house.get("area_m2", 0):.0f} m2', scale, bold=True))

    parts.append(_dimension_lines(project, parcel, scale))
    parts.append(_north_arrow(width, margin))
    parts.append(_scale_bar(scale, margin, height))
    parts.append(_legend(plan, width, margin))
    parts.append("</svg>")
    return "".join(parts)


def _defs() -> str:
    return (
        '<defs><filter id="drop" x="-20%" y="-20%" width="150%" height="150%">'
        '<feDropShadow dx="2" dy="3" stdDeviation="3" flood-opacity="0.28"/>'
        "</filter></defs>"
    )


def _label(project, ring, text: str, scale: float, bold: bool = False) -> str:
    cx = sum(p[0] for p in ring) / len(ring)
    cy = sum(p[1] for p in ring) / len(ring)
    x, y = project((cx, cy))
    weight = "600" if bold else "400"
    size = 13 if bold else 11
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle" dominant-baseline="middle" '
        f'font-family="system-ui,sans-serif" font-size="{size}" font-weight="{weight}" '
        f'fill="#ffffff" stroke="#00000055" stroke-width="2.5" paint-order="stroke">{text}</text>'
    )


def _dimension_lines(project, parcel, scale: float) -> str:
    """Acota los lados de la parcela: sin medidas un plano no sirve de nada."""
    parts: list[str] = []
    n = len(parcel)
    for i in range(n):
        ax, ay = parcel[i]
        bx, by = parcel[(i + 1) % n]
        length = math.hypot(bx - ax, by - ay)
        if length < 4.0:  # lados muy cortos: la cota no cabe
            continue
        mx, my = project(((ax + bx) / 2, (ay + by) / 2))
        angle = math.degrees(math.atan2(-(by - ay), bx - ax))
        if angle > 90 or angle < -90:
            angle += 180
        parts.append(
            f'<text x="{mx:.1f}" y="{my:.1f}" text-anchor="middle" '
            f'transform="rotate({angle:.1f} {mx:.1f} {my:.1f})" dy="-5" '
            f'font-family="system-ui,sans-serif" font-size="11" '
            f'fill="{PALETTE["dim"]}">{length:.1f} m</text>'
        )
    return "".join(parts)


def _north_arrow(width: int, margin: int) -> str:
    x = width - margin + 10
    y = margin - 15
    return (
        f'<g transform="translate({x},{y})">'
        f'<path d="M0,-18 L7,10 L0,4 L-7,10 Z" fill="{PALETTE["text"]}"/>'
        f'<text x="0" y="26" text-anchor="middle" font-family="system-ui,sans-serif" '
        f'font-size="12" font-weight="600" fill="{PALETTE["text"]}">N</text></g>'
    )


def _scale_bar(scale: float, margin: int, height: int) -> str:
    """Barra de escala con una longitud redonda en metros."""
    for candidate in (50, 20, 10, 5, 2):
        if candidate * scale <= 180:
            meters = candidate
            break
    else:
        meters = 1

    length = meters * scale
    x, y = margin, height - margin / 2
    return (
        f'<g><line x1="{x}" y1="{y}" x2="{x + length:.1f}" y2="{y}" '
        f'stroke="{PALETTE["text"]}" stroke-width="2.5"/>'
        f'<line x1="{x}" y1="{y - 5}" x2="{x}" y2="{y + 5}" stroke="{PALETTE["text"]}" stroke-width="2.5"/>'
        f'<line x1="{x + length:.1f}" y1="{y - 5}" x2="{x + length:.1f}" y2="{y + 5}" '
        f'stroke="{PALETTE["text"]}" stroke-width="2.5"/>'
        f'<text x="{x + length / 2:.1f}" y="{y - 10}" text-anchor="middle" '
        f'font-family="system-ui,sans-serif" font-size="12" fill="{PALETTE["text"]}">{meters} m</text></g>'
    )


def _legend(plan: dict[str, Any], width: int, margin: int) -> str:
    metrics = plan.get("metrics", {})
    rows = [
        ("Parcela", f'{metrics.get("parcel_area_m2", 0):,.0f} m2'),
        ("Edificable", f'{metrics.get("buildable_area_m2", 0):,.0f} m2'),
        ("Huella casa", f'{metrics.get("house_footprint_m2", 0):,.0f} m2'),
    ]
    if metrics.get("terrace_area_m2"):
        rows.append(("Terraza", f'{metrics["terrace_area_m2"]:,.0f} m2'))

    parts = [f'<g transform="translate({margin - 20},{margin - 35})">']
    parts.append(
        f'<rect x="0" y="0" width="168" height="{18 * len(rows) + 14}" rx="6" '
        f'fill="#ffffffd8" stroke="#d8d2c4"/>'
    )
    for index, (label, value) in enumerate(rows):
        y = 20 + index * 18
        parts.append(
            f'<text x="10" y="{y}" font-family="system-ui,sans-serif" font-size="11.5" '
            f'fill="{PALETTE["dim"]}">{label}</text>'
            f'<text x="158" y="{y}" text-anchor="end" font-family="system-ui,sans-serif" '
            f'font-size="11.5" font-weight="600" fill="{PALETTE["text"]}">{value}</text>'
        )
    parts.append("</g>")
    return "".join(parts)


def _empty_svg(width: int, height: int, message: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%">'
        f'<rect width="{width}" height="{height}" fill="#f6f5f2"/>'
        f'<text x="{width // 2}" y="{height // 2}" text-anchor="middle" '
        f'font-family="system-ui,sans-serif" font-size="15" fill="#8a8578">{message}</text></svg>'
    )

"""Descarga de la imagen de referencia de un anuncio.

La app desplegada sí tiene salida a internet, así que puede traerse la foto del
anuncio del fabricante, guardarla en el volumen y servirla ella misma. Eso
evita enlazar la imagen del vendedor, que además de ser mala práctica suele
estar bloqueada: Amazon y la mayoría de portales rechazan las peticiones cuyo
Referer no es el suyo, y el resultado serían imágenes rotas en producción.
"""
from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin, urlparse

import httpx

from app.sources.base import BaseSource, SourceError

# Metaetiquetas donde las tiendas publican su imagen principal, en el orden en
# que conviene probarlas: og:image es la que usan las redes sociales y suele
# ser la foto grande del producto.
META_PATTERNS = (
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
    r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\']([^"\']+)["\']',
)

ALLOWED_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

# Cabeceras de navegador: muchas tiendas responden 403 a clientes que no lo
# parecen, y sin Referer propio bloquean la descarga de sus imagenes.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9",
}


class ReferenceImageSource(BaseSource):
    """Extrae y descarga la imagen principal de una página de producto."""

    key = "reference_image"
    name = "Imagen de referencia del anuncio"
    kind = "geo"
    licence = "La imagen pertenece a su publicador; se descarga sólo como referencia."

    def client(self, **kwargs):  # type: ignore[override]
        import httpx

        return httpx.Client(
            timeout=self.settings.http_timeout,
            headers=BROWSER_HEADERS,
            follow_redirects=True,
            **kwargs,
        )

    def extract_image_url(self, html: str, base_url: str) -> str | None:
        """Localiza la imagen principal declarada por la página."""
        for pattern in META_PATTERNS:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                candidate = unescape(match.group(1)).strip()
                if candidate:
                    # Las rutas relativas se resuelven contra la propia página.
                    return urljoin(base_url, candidate)
        return None

    def fetch(self, page_url: str) -> tuple[bytes, str]:
        """Devuelve (contenido, extensión) de la imagen de una página.

        Acepta tanto la URL de la página del producto como la de una imagen
        directa, porque desde fuera no siempre se sabe cuál se tiene.
        """
        if not urlparse(page_url).scheme.startswith("http"):
            raise SourceError("La URL de referencia debe ser http o https.")

        response = self._get(page_url)
        if response.status_code != 200:
            raise SourceError(
                f"La página de referencia respondió HTTP {response.status_code}. "
                "Muchas tiendas bloquean las peticiones automatizadas; en ese "
                "caso sube la foto a mano."
            )

        content_type = (response.headers.get("content-type") or "").split(";")[0].lower()

        # Caso directo: la URL ya era una imagen.
        if content_type in ALLOWED_CONTENT_TYPES:
            return self._validate(response.content, content_type)

        # Es una imagen, pero de un tipo que no se acepta. Se rechaza aquí en
        # lugar de intentar leerla como HTML, que daría un error engañoso: un
        # SVG, por ejemplo, puede contener scripts y no debe servirse.
        if content_type.startswith("image/"):
            raise SourceError(f"Tipo de imagen no admitido: {content_type}.")

        image_url = self.extract_image_url(response.text, page_url)
        if not image_url:
            raise SourceError(
                "La página no declara imagen principal (og:image). Copia la URL "
                "directa de la foto, o súbela a mano."
            )

        image_response = self._get(image_url, referer=page_url)
        if image_response.status_code != 200:
            raise SourceError(
                f"La imagen respondió HTTP {image_response.status_code} "
                f"({image_url[:120]}). El vendedor probablemente bloquea la "
                "descarga externa; súbela a mano."
            )
        content_type = (
            image_response.headers.get("content-type") or ""
        ).split(";")[0].lower()
        return self._validate(image_response.content, content_type)

    def _get(self, url: str, referer: str | None = None) -> httpx.Response:
        """Petición cuyos fallos de red salen siempre como SourceError.

        Sin esto, un bloqueo del proxy o un DNS caído escapaban como
        httpx.ProxyError y el endpoint devolvía un 500 en vez de explicar qué
        había pasado.
        """
        try:
            return self.request("GET", url, headers={"Referer": referer or url})
        except httpx.ProxyError as exc:
            raise SourceError(
                f"La red de este entorno bloquea {urlparse(url).netloc}: {exc}. "
                "Desde el servidor desplegado sí debería funcionar."
            ) from None
        except httpx.HTTPError as exc:
            raise SourceError(
                f"No se pudo acceder a {urlparse(url).netloc}: "
                f"{type(exc).__name__}: {exc}"
            ) from None

    def _validate(self, content: bytes, content_type: str) -> tuple[bytes, str]:
        extension = ALLOWED_CONTENT_TYPES.get(content_type)
        if extension is None:
            raise SourceError(f"Tipo de imagen no admitido: {content_type or 'desconocido'}.")
        if not content:
            raise SourceError("La imagen descargada está vacía.")
        if len(content) > self.settings.max_image_bytes:
            raise SourceError(
                f"La imagen pesa {len(content) // 1024} KB y el máximo son "
                f"{self.settings.max_image_bytes // 1024} KB."
            )
        return content, extension

    def check(self):
        return self._status(
            "ok", "Descarga bajo demanda; no se comprueba de forma periódica."
        )

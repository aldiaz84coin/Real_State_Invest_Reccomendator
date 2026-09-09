"""Configuracion central de la aplicacion."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    # DEBUG añade la traza de cada petición saliente a las fuentes.
    log_level: str = "INFO"
    database_url: str = "sqlite:///./investment.db"

    # Idealista: unica via oficial para leer anuncios de portales.
    idealista_api_key: str = ""
    idealista_api_secret: str = ""
    idealista_base_url: str = "https://api.idealista.com"

    # Complemento opcional de datos de alquiler turistico (pago por uso).
    airroi_api_key: str = ""
    airroi_base_url: str = "https://api.airroi.com"

    # --- Respaldo NO oficial via RapidAPI -------------------------------
    # Revendedores que extraen datos de los portales. Quedan fuera del criterio
    # de "solo vias oficiales" y por eso van desactivados mientras no haya
    # clave. Host y ruta son configurables porque cada proveedor los cambia sin
    # aviso y no deben requerir tocar codigo.
    # RapidAPI entrega una clave por aplicacion, y lo habitual es crear una por
    # API suscrita. Por eso hay clave global y ademas clave por fuente.
    rapidapi_key: str = ""     # respaldo comun si no hay una especifica
    rapidapi_keys: str = ""    # "clave_fuente=API_KEY,clave_fuente=API_KEY"
    rapidapi_hosts: str = ""   # "clave_fuente=host"
    rapidapi_paths: str = ""   # "clave_fuente=/ruta"
    # Valores sueltos de parametros que cambian entre revendedores sin que
    # cambie nada mas: "clave_fuente.parametro=valor".
    rapidapi_params: str = ""

    # Fuentes publicas abiertas: no requieren credenciales.
    catastro_ovc_url: str = "https://ovc.catastro.meh.es"
    catastro_inspire_url: str = "https://ovc.catastro.meh.es/INSPIRE"
    ine_base_url: str = "https://servicios.ine.es/wstempus/js/ES"
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    insideairbnb_url: str = "https://insideairbnb.com/get-the-data"
    # API de datos abiertos del BOE: documentada, sin clave y sin cuota
    # publicada. Es la via limpia para las subastas, frente a raspar el
    # buscador del portal.
    boe_api_url: str = "https://boe.es/datosabiertos/api"
    boe_diario_url: str = "https://www.boe.es/diario_boe"

    # Nominatim y Overpass exigen identificar al cliente.
    contact_email: str = "contacto@ejemplo.com"
    user_agent: str = "RealStateInvestRecommendator/1.0"

    # --- Fotos reales de los modelos prefabricados ----------------------
    # Se guardan en el volumen, no en la imagen, para poder anadirlas sin
    # redesplegar. PREFAB_IMAGES permite ademas apuntar a una URL externa
    # cuando se tienen derechos sobre ella.
    model_images_dir: str = "data/model_images"
    prefab_images: str = ""   # "modelo=https://...,modelo=https://..."
    max_image_bytes: int = 6 * 1024 * 1024

    http_timeout: float = 30.0
    cache_ttl_seconds: int = 60 * 60 * 12

    @property
    def http_headers(self) -> dict[str, str]:
        return {"User-Agent": f"{self.user_agent} ({self.contact_email})"}

    @property
    def idealista_configured(self) -> bool:
        return bool(self.idealista_api_key and self.idealista_api_secret)

    @property
    def rapidapi_configured(self) -> bool:
        return bool(self.rapidapi_key or self.rapidapi_keys)

    def rapidapi_key_for(self, source_key: str) -> str:
        """Clave especifica de esa API, o la global si no hay una propia."""
        return _parse_overrides(self.rapidapi_keys).get(source_key) or self.rapidapi_key

    def prefab_image_url(self, model_id: str) -> str | None:
        return _parse_overrides(self.prefab_images).get(model_id)

    def rapidapi_host_for(self, source_key: str) -> str | None:
        return _parse_overrides(self.rapidapi_hosts).get(source_key)

    def rapidapi_path_for(self, source_key: str) -> str | None:
        return _parse_overrides(self.rapidapi_paths).get(source_key)

    def rapidapi_param_for(self, source_key: str, param: str) -> str | None:
        """Valor de un parámetro concreto de una fuente, si se ha fijado."""
        return _parse_overrides(self.rapidapi_params).get(f"{source_key}.{param}")


def _parse_overrides(raw: str) -> dict[str, str]:
    """Lee "clave=valor,clave=valor" y descarta lo que no encaje."""
    overrides: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        if "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        key, value = key.strip(), value.strip()
        if key and value:
            overrides[key] = value
    return overrides


@lru_cache
def get_settings() -> Settings:
    return Settings()

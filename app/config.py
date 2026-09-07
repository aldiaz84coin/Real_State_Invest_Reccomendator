"""Configuracion central de la aplicacion."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite:///./investment.db"

    # Idealista: unica via oficial para leer anuncios de portales.
    idealista_api_key: str = ""
    idealista_api_secret: str = ""
    idealista_base_url: str = "https://api.idealista.com"

    # Complemento opcional de datos de alquiler turistico (pago por uso).
    airroi_api_key: str = ""
    airroi_base_url: str = "https://api.airroi.com"

    # Fuentes publicas abiertas: no requieren credenciales.
    catastro_ovc_url: str = "https://ovc.catastro.meh.es"
    catastro_inspire_url: str = "https://ovc.catastro.meh.es/INSPIRE"
    ine_base_url: str = "https://servicios.ine.es/wstempus/js/ES"
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    insideairbnb_url: str = "https://insideairbnb.com/get-the-data"

    # Nominatim y Overpass exigen identificar al cliente.
    contact_email: str = "contacto@ejemplo.com"
    user_agent: str = "RealStateInvestRecommendator/1.0"

    http_timeout: float = 30.0
    cache_ttl_seconds: int = 60 * 60 * 12

    @property
    def http_headers(self) -> dict[str, str]:
        return {"User-Agent": f"{self.user_agent} ({self.contact_email})"}

    @property
    def idealista_configured(self) -> bool:
        return bool(self.idealista_api_key and self.idealista_api_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "Huai-Coder API"
    environment: str = "development"
    database_url: str = "postgresql+asyncpg://huai_coder:huai_coder@db:5432/huai_coder_dev"
    cors_origins: str = "http://localhost:5173,http://localhost"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    @property
    def cors_origin_list(self) -> list[str]:
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]

@lru_cache
def get_settings() -> Settings:
    return Settings()

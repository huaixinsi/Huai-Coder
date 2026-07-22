from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "Huai-Coder API"
    environment: str = "development"
    database_url: str = "postgresql+asyncpg://huai_coder:huai_coder@db:5432/huai_coder_dev"
    cors_origins: str = "http://localhost:5173,http://localhost"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    memory_enabled: bool = True
    memory_extraction_enabled: bool = True
    memory_max_retrieved: int = 8
    memory_default_importance: int = 5
    memory_retention_days: int = 90
    context_compaction_enabled: bool = True
    context_max_tokens: int = 32768
    context_compaction_threshold: float = 0.75
    react_max_turns: int = 128
    client_tool_timeout_seconds: int = 300
    tool_approval_enabled: bool = False
    context_recent_turns: int = 8
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    @property
    def cors_origin_list(self) -> list[str]:
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]

@lru_cache
def get_settings() -> Settings:
    return Settings()

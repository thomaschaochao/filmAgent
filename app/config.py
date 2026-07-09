from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FILMAGENT_",
        env_file=(".env", "config.local.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "FilmPilot"
    database_url: str = "sqlite:///./data/filmagent.db"
    deepseek_api_key: SecretStr | None = None
    deepseek_api_key_file: Path = Path("deepseekapi.txt")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_timeout_seconds: float = 120.0
    deepseek_max_tokens: int = Field(default=32768, ge=1024, le=384000)
    deepseek_thinking_enabled: bool = True
    deepseek_reasoning_effort: str = "high"
    validation_rules_version: str = "2026-07-p0"
    storyboard_reference_coverage_threshold: float = 0.8
    storyboard_reference_match_threshold: float = 0.7
    script_min_characters: int = 50
    asset_prompt_min_characters: int = 20
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_image_model: str = "gpt-image-2"
    ark_api_key: SecretStr | None = None
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    seedream_model: str = "doubao-seedream-5-0-260128"
    image_timeout_seconds: float = 300.0
    embedding_enabled: bool = True
    embedding_model: str = "BAAI/bge-m3"
    embedding_device: str = "auto"
    embedding_max_length: int = Field(default=2048, ge=256, le=8192)
    embedding_cpu_batch_size: int = Field(default=4, ge=1, le=128)
    embedding_gpu_batch_size: int = Field(default=16, ge=1, le=256)
    embedding_model_path: Path = Path("storage/models/bge-m3")
    qdrant_path: Path = Path("storage/vector/qdrant")
    vector_collection: str = "filmagent_knowledge_v1"
    ark_search_model: str = ""
    web_timeout_seconds: float = Field(default=30.0, ge=1, le=180)
    web_max_bytes: int = Field(default=2_000_000, ge=10_000, le=10_000_000)
    master_agent_ai_enabled: bool = True
    agent_framework: str = "crewai"
    crewai_enabled: bool = True

    @staticmethod
    def _secret_value(secret: SecretStr | None) -> str | None:
        if secret is None:
            return None
        value = secret.get_secret_value().strip()
        return value or None

    def get_deepseek_api_key(self) -> str:
        value = self._secret_value(self.deepseek_api_key)
        if value:
            return value

        key_path = self.deepseek_api_key_file
        if not key_path.is_absolute():
            key_path = Path.cwd() / key_path
        if not key_path.exists():
            raise RuntimeError(
                "DeepSeek API key is not configured. Set FILMAGENT_DEEPSEEK_API_KEY "
                "or FILMAGENT_DEEPSEEK_API_KEY_FILE."
            )
        value = key_path.read_text(encoding="utf-8").strip()
        if not value:
            raise RuntimeError("DeepSeek API key file is empty.")
        return value

    def get_openai_api_key(self) -> str:
        value = self._secret_value(self.openai_api_key)
        if not value:
            raise RuntimeError("OpenAI API key is not configured in config.local.env.")
        return value

    def get_ark_api_key(self) -> str:
        value = self._secret_value(self.ark_api_key)
        if not value:
            raise RuntimeError("Volcengine Ark API key is not configured in config.local.env.")
        return value

    def provider_configured(self, provider: str) -> bool:
        if provider == "openai":
            return self._secret_value(self.openai_api_key) is not None
        if provider == "seedream":
            return self._secret_value(self.ark_api_key) is not None
        return False


@lru_cache
def get_settings() -> Settings:
    return Settings()

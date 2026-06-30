from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FILMAGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "FilmAgent"
    database_url: str = "sqlite:///./data/filmagent.db"
    deepseek_api_key: SecretStr | None = None
    deepseek_api_key_file: Path = Path("deepseekapi.txt")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_timeout_seconds: float = 120.0

    def get_deepseek_api_key(self) -> str:
        if self.deepseek_api_key is not None:
            value = self.deepseek_api_key.get_secret_value().strip()
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


@lru_cache
def get_settings() -> Settings:
    return Settings()

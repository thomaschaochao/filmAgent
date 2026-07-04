import tomllib
from pathlib import Path

import pytest

from app.config import Settings
from app.version import __version__


def test_package_and_application_versions_match():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["version"] == __version__


def test_reads_api_key_from_file_without_exposing_it(tmp_path: Path):
    key_file = tmp_path / "secret.txt"
    key_file.write_text("secret-value\n", encoding="utf-8")
    settings = Settings(_env_file=None, deepseek_api_key_file=key_file)
    assert settings.get_deepseek_api_key() == "secret-value"
    assert "secret-value" not in repr(settings)


def test_missing_api_key_has_safe_error(tmp_path: Path):
    settings = Settings(_env_file=None, deepseek_api_key_file=tmp_path / "missing.txt")
    with pytest.raises(RuntimeError, match="not configured"):
        settings.get_deepseek_api_key()


def test_local_config_overrides_legacy_env_and_environment_wins(tmp_path: Path, monkeypatch):
    legacy = tmp_path / ".env"
    local = tmp_path / "config.local.env"
    legacy.write_text("FILMAGENT_OPENAI_API_KEY=legacy-key\n", encoding="utf-8")
    local.write_text("FILMAGENT_OPENAI_API_KEY=local-key\n", encoding="utf-8")
    settings = Settings(_env_file=(legacy, local))
    assert settings.get_openai_api_key() == "local-key"
    assert "local-key" not in repr(settings)

    monkeypatch.setenv("FILMAGENT_OPENAI_API_KEY", "environment-key")
    settings = Settings(_env_file=(legacy, local))
    assert settings.get_openai_api_key() == "environment-key"


def test_provider_configuration_does_not_expose_keys():
    settings = Settings(
        _env_file=None, openai_api_key="openai-secret", ark_api_key="ark-secret"
    )
    assert settings.provider_configured("openai") is True
    assert settings.provider_configured("seedream") is True
    assert "openai-secret" not in repr(settings)
    assert "ark-secret" not in repr(settings)

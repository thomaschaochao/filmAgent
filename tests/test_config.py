import tomllib
from pathlib import Path

import pytest
from pydantic_settings.sources import DotEnvSettingsSource, SecretsSettingsSource

from app.config import Settings
from app.version import __version__


@pytest.fixture(autouse=True)
def isolate_provider_environment(monkeypatch):
    for variable in (
        "FILMAGENT_DEEPSEEK_API_KEY",
        "FILMAGENT_DEEPSEEK_API_KEY_FILE",
        "FILMAGENT_OPENAI_API_KEY",
        "FILMAGENT_ARK_API_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)


def test_package_and_application_versions_match():
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["version"] == __version__


def test_reads_api_key_from_file_without_exposing_it(tmp_path: Path):
    key_file = tmp_path / "secret.txt"
    key_file.write_text("secret-value\n", encoding="utf-8")
    settings = Settings(
        settings_source_mode="local", _env_file=None, deepseek_api_key_file=key_file
    )
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
    settings = Settings(settings_source_mode="local", _env_file=(legacy, local))
    assert settings.get_openai_api_key() == "local-key"
    assert "local-key" not in repr(settings)

    monkeypatch.setenv("FILMAGENT_OPENAI_API_KEY", "environment-key")
    settings = Settings(settings_source_mode="local", _env_file=(legacy, local))
    assert settings.get_openai_api_key() == "environment-key"


def test_provider_configuration_does_not_expose_keys():
    settings = Settings(
        _env_file=None, openai_api_key="openai-secret", ark_api_key="ark-secret"
    )
    assert settings.provider_configured("openai") is True
    assert settings.provider_configured("seedream") is True
    assert "openai-secret" not in repr(settings)
    assert "ark-secret" not in repr(settings)


def test_local_settings_remain_the_default(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("FILMAGENT_SETTINGS_SOURCE_MODE", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.local.env").write_text("FILMAGENT_APP_NAME=Local fixture\n")
    monkeypatch.delenv("FILMAGENT_APP_NAME", raising=False)

    settings = Settings()

    assert settings.settings_source_mode == "local"
    assert settings.app_name == "Local fixture"


@pytest.mark.parametrize("select_with_environment", [False, True])
def test_environment_mode_never_reads_dotenv_or_secret_files(
    tmp_path: Path, monkeypatch, select_with_environment: bool
):
    dotenv = tmp_path / ".env"
    dotenv.write_text("FILMAGENT_APP_NAME=Should not be read\n")
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    (secret_dir / "FILMAGENT_APP_NAME").write_text("Should not be read\n")

    def unexpected_file_read(*args, **kwargs):
        pytest.fail("Environment settings mode attempted to read a settings file")

    monkeypatch.setattr(DotEnvSettingsSource, "_read_env_file", unexpected_file_read)
    monkeypatch.setattr(SecretsSettingsSource, "get_field_value", unexpected_file_read)
    monkeypatch.setenv("FILMAGENT_APP_NAME", "Environment fixture")
    kwargs = {"_env_file": dotenv, "_secrets_dir": secret_dir}
    if select_with_environment:
        monkeypatch.setenv("FILMAGENT_SETTINGS_SOURCE_MODE", "environment")
    else:
        kwargs["settings_source_mode"] = "environment"

    settings = Settings(**kwargs)

    assert settings.settings_source_mode == "environment"
    assert settings.app_name == "Environment fixture"


def test_environment_mode_disables_key_file_fallback(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("FILMAGENT_DEEPSEEK_API_KEY", raising=False)
    settings = Settings(
        settings_source_mode="environment", deepseek_api_key_file=tmp_path / "never-read.txt"
    )

    def unexpected_file_access(*args, **kwargs):
        pytest.fail("Environment settings mode attempted to access a key file")

    monkeypatch.setattr(Path, "exists", unexpected_file_access)
    monkeypatch.setattr(Path, "read_text", unexpected_file_access)

    with pytest.raises(RuntimeError, match="key-file loading is disabled"):
        settings.get_deepseek_api_key()


def test_environment_mode_accepts_environment_keys(monkeypatch):
    monkeypatch.setenv("FILMAGENT_DEEPSEEK_API_KEY", "synthetic-deepseek-value")
    settings = Settings(settings_source_mode="environment")

    assert settings.get_deepseek_api_key() == "synthetic-deepseek-value"
    assert "synthetic-deepseek-value" not in repr(settings)


def test_invalid_source_mode_fails_before_dotenv_loading(monkeypatch):
    def unexpected_file_read(*args, **kwargs):
        pytest.fail("Invalid settings mode attempted to read settings files")

    monkeypatch.setenv("FILMAGENT_SETTINGS_SOURCE_MODE", "environmnt")
    monkeypatch.setattr(DotEnvSettingsSource, "_read_env_files", unexpected_file_read)

    with pytest.raises(ValueError, match="Settings source mode must be"):
        Settings()

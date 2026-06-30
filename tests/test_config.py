from pathlib import Path

import pytest

from app.config import Settings


def test_reads_api_key_from_file_without_exposing_it(tmp_path: Path):
    key_file = tmp_path / "secret.txt"
    key_file.write_text("secret-value\n", encoding="utf-8")
    settings = Settings(deepseek_api_key_file=key_file)
    assert settings.get_deepseek_api_key() == "secret-value"
    assert "secret-value" not in repr(settings)


def test_missing_api_key_has_safe_error(tmp_path: Path):
    settings = Settings(deepseek_api_key_file=tmp_path / "missing.txt")
    with pytest.raises(RuntimeError, match="not configured"):
        settings.get_deepseek_api_key()

import socket

import pytest

from app.services.web_tools import WebToolError, validate_public_url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost:8000/private",
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data",
    ],
)
def test_web_fetch_rejects_local_and_unsafe_urls(url):
    with pytest.raises(WebToolError):
        validate_public_url(url)


def test_web_fetch_accepts_public_https(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    assert validate_public_url("https://example.com/research") == "https://example.com/research"

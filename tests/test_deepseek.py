import json

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.services.deepseek import DeepSeekClient, DeepSeekTruncatedError


class FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._data


class FakeHttpClient:
    def __init__(self, response: FakeResponse, requests: list[dict]) -> None:
        self.response = response
        self.requests = requests

    def __enter__(self) -> "FakeHttpClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, url: str, *, headers: dict, json: dict) -> FakeResponse:
        self.requests.append(json)
        return self.response


def make_settings() -> Settings:
    return Settings(
        deepseek_api_key=SecretStr("test-key"),
        deepseek_max_tokens=32768,
    )


def test_chat_json_uses_configured_output_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict] = []
    response = FakeResponse(
        {
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps({"shots": []})},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    )
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: FakeHttpClient(response, requests),
    )

    client = DeepSeekClient(make_settings())

    assert client.chat_json("system", "user") == {"shots": []}
    assert requests[0]["max_tokens"] == 32768
    assert client.last_call["finish_reason"] == "stop"


def test_chat_json_reports_truncated_output_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict] = []
    response = FakeResponse(
        {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": '{"shots": ['},
                }
            ],
            "usage": {"completion_tokens": 32768},
        }
    )
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: FakeHttpClient(response, requests),
    )

    client = DeepSeekClient(make_settings())

    with pytest.raises(DeepSeekTruncatedError, match="finish_reason=length"):
        client.chat_json("system", "user")

    assert len(requests) == 1
    assert client.last_call["output_tokens"] == 32768
    assert client.last_call["finish_reason"] == "length"

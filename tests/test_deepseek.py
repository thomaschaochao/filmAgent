import json

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.services.deepseek import DeepSeekClient, DeepSeekError, DeepSeekTruncatedError


class FakeResponse:
    def __init__(self, data: object) -> None:
        self._data = data
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._data


class FakeHttpClient:
    def __init__(self, response: FakeResponse | httpx.Response, requests: list[dict]) -> None:
        self.response = response
        self.requests = requests

    def __enter__(self) -> "FakeHttpClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, url: str, *, headers: dict, json: dict) -> FakeResponse | httpx.Response:
        self.requests.append(json)
        return self.response


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
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
    assert requests[0]["thinking"] == {"type": "enabled"}
    assert requests[0]["reasoning_effort"] == "high"
    assert client.last_call["finish_reason"] == "stop"
    assert client.last_call["thinking_enabled"] is True
    assert client.last_call["reasoning_effort"] == "high"


def test_chat_json_can_disable_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict] = []
    response = FakeResponse(
        {
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            "usage": {},
        }
    )
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: FakeHttpClient(response, requests))
    settings = Settings(
        _env_file=None,
        deepseek_api_key=SecretStr("test-key"),
        deepseek_thinking_enabled=False,
    )

    assert DeepSeekClient(settings).chat_json("system", "user") == {}
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in requests[0]


def test_chat_json_ignores_reasoning_content_for_business_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict] = []
    response = FakeResponse(
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "reasoning_content": "private reasoning must not be parsed",
                        "content": json.dumps({"ok": True}),
                    },
                }
            ],
            "usage": {},
        }
    )
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: FakeHttpClient(response, requests))

    client = DeepSeekClient(make_settings())

    assert client.chat_json("system", "user") == {"ok": True}
    assert "private reasoning" not in client.last_call["raw_response"]


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


@pytest.mark.parametrize("status_code", [401, 429, 503])
def test_chat_json_preserves_http_errors_without_retry_or_body_logging(
    monkeypatch: pytest.MonkeyPatch, status_code: int,
) -> None:
    requests: list[dict] = []
    response = httpx.Response(
        status_code,
        json={"error": "private-provider-error-body"},
        request=httpx.Request("POST", "https://deepseek.invalid/chat/completions"),
    )
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: FakeHttpClient(response, requests))
    client = DeepSeekClient(make_settings())

    with pytest.raises(DeepSeekError) as error:
        client.chat_json("system", "user")

    assert error.value.error_type == "http_error"
    assert error.value.validation_results == []
    assert len(requests) == client.attempt_count == client.last_call["attempt_count"] == 1
    assert client.last_call["raw_response"] == ""
    assert "private-provider-error-body" not in str(error.value)
    assert "private-provider-error-body" not in json.dumps(client.last_call)


@pytest.mark.parametrize("error_class", [httpx.ConnectError, httpx.ReadTimeout])
def test_chat_json_preserves_transport_errors_without_retry(
    monkeypatch: pytest.MonkeyPatch, error_class: type[httpx.RequestError],
) -> None:
    requests: list[dict] = []

    class FailingHttpClient(FakeHttpClient):
        def post(self, url: str, *, headers: dict, json: dict) -> FakeResponse:
            self.requests.append(json)
            raise error_class("simulated transport failure")

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: FailingHttpClient(FakeResponse({}), requests),
    )
    client = DeepSeekClient(make_settings())

    with pytest.raises(DeepSeekError) as error:
        client.chat_json("system", "user")

    assert error.value.error_type == "http_error"
    assert len(requests) == client.attempt_count == 1
    assert client.last_call["raw_response"] == ""


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "private-provider-error-body",
        {"error": "private-provider-error-body"},
        {"choices": None},
        {"choices": {}},
        {"choices": []},
        {"choices": [None]},
        {"choices": [{}], "usage": []},
        {"choices": [{}]},
        {"choices": [{"message": None}]},
        {"choices": [{"message": []}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": []}}]},
        {"choices": [{"message": {"content": 0}}]},
        {"choices": [{"message": {"content": False}}]},
    ],
)
def test_chat_json_rejects_malformed_provider_shapes_without_retry(
    monkeypatch: pytest.MonkeyPatch, data: object,
) -> None:
    requests: list[dict] = []
    response = FakeResponse(data)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: FakeHttpClient(response, requests))
    client = DeepSeekClient(make_settings())

    with pytest.raises(DeepSeekError) as error:
        client.chat_json("system", "user")

    assert error.value.error_type == "response_shape"
    assert error.value.validation_results == []
    assert len(requests) == client.attempt_count == 1
    assert client.last_call["raw_response"] == ""
    assert "private-provider-error-body" not in json.dumps(client.last_call)


def test_chat_json_rejects_non_json_provider_body_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict] = []
    response = httpx.Response(
        200,
        text="<html>private-provider-error-body</html>",
        request=httpx.Request("POST", "https://deepseek.invalid/chat/completions"),
    )
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: FakeHttpClient(response, requests))
    client = DeepSeekClient(make_settings())

    with pytest.raises(DeepSeekError) as error:
        client.chat_json("system", "user")

    assert error.value.error_type == "response_shape"
    assert len(requests) == client.attempt_count == 1
    assert client.last_call["raw_response"] == ""
    assert "private-provider-error-body" not in str(error.value)


@pytest.mark.parametrize("content", ['{"shots": [', "", " \n", None, "[]", "1", "null"])
def test_chat_json_retries_malformed_model_json_once(
    monkeypatch: pytest.MonkeyPatch, content: str | None,
) -> None:
    requests: list[dict] = []
    response = FakeResponse({"choices": [{"message": {"content": content}}]})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: FakeHttpClient(response, requests))
    client = DeepSeekClient(make_settings())

    with pytest.raises(DeepSeekError) as error:
        client.chat_json("system", "user")

    assert error.value.error_type == "json_parse"
    assert error.value.validation_results[0]["key"] == "json_parse"
    assert error.value.validation_results[0]["passed"] is False
    assert len(requests) == client.attempt_count == client.last_call["attempt_count"] == 2


def test_chat_json_can_recover_from_malformed_model_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict] = []
    responses = [
        FakeResponse({"choices": [{"message": {"content": "not JSON"}}]}),
        FakeResponse({"choices": [{"message": {"content": '{"ok": true}'}}]}),
    ]
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: FakeHttpClient(responses[len(requests)], requests),
    )
    client = DeepSeekClient(make_settings())

    assert client.chat_json("system", "user") == {"ok": True}
    assert len(requests) == client.last_call["attempt_count"] == 2
    assert client.last_call["raw_response"] == '{"ok": true}'


def test_chat_json_preserves_http_error_on_second_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict] = []
    responses = [
        FakeResponse({"choices": [{"message": {"content": "not JSON"}}]}),
        httpx.Response(
            429,
            text="private-provider-error-body",
            request=httpx.Request("POST", "https://deepseek.invalid/chat/completions"),
        ),
    ]
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: FakeHttpClient(responses[len(requests)], requests),
    )
    client = DeepSeekClient(make_settings())

    with pytest.raises(DeepSeekError) as error:
        client.chat_json("system", "user")

    assert error.value.error_type == "http_error"
    assert error.value.validation_results == []
    assert len(requests) == client.last_call["attempt_count"] == 2
    assert client.last_call["raw_response"] == ""


def test_chat_json_preserves_incomplete_output_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict] = []
    response = FakeResponse({
        "choices": [{"finish_reason": "content_filter", "message": {"content": "{}"}}],
    })
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: FakeHttpClient(response, requests))
    client = DeepSeekClient(make_settings())

    with pytest.raises(DeepSeekError) as error:
        client.chat_json("system", "user")

    assert error.value.error_type == "incomplete_output"
    assert len(requests) == client.attempt_count == 1

import json
import time
from typing import Any

import httpx

from app.config import Settings, get_settings


class DeepSeekError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_type: str = "deepseek_error",
        validation_results: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.validation_results = validation_results or []


class DeepSeekClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.last_call: dict[str, Any] = {}
        self.attempt_count = 0

    def chat_text(self, system_prompt: str, user_prompt: str) -> str:
        self.attempt_count = 1
        return self._request(system_prompt, user_prompt, json_output=False)

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, 3):
            self.attempt_count = attempt
            try:
                content = self._request(system_prompt, user_prompt, json_output=True)
                if not content.strip():
                    raise DeepSeekError(
                        "DeepSeek returned empty content.", error_type="empty_output"
                    )
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    raise DeepSeekError(
                        "DeepSeek JSON response must be an object.", error_type="json_shape"
                    )
                return parsed
            except (json.JSONDecodeError, DeepSeekError) as exc:
                last_error = exc
        raise DeepSeekError(
            "DeepSeek did not return valid structured JSON.",
            error_type="json_parse",
            validation_results=[
                {
                    "key": "json_parse",
                    "label": "JSON 解析校验",
                    "passed": False,
                    "value": 0,
                    "threshold": 1,
                    "detail": f"返回内容无法解析为 JSON 对象：{last_error}",
                }
            ],
        ) from last_error

    def _request(self, system_prompt: str, user_prompt: str, *, json_output: bool) -> str:
        payload: dict[str, Any] = {
            "model": self.settings.deepseek_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": self.settings.deepseek_max_tokens,
            "thinking": {"type": "disabled"},
        }
        if json_output:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.settings.get_deepseek_api_key()}",
            "Content-Type": "application/json",
        }
        url = f"{self.settings.deepseek_base_url.rstrip('/')}/chat/completions"
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=self.settings.deepseek_timeout_seconds) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            response_text = ""
            if isinstance(exc, httpx.HTTPStatusError):
                response_text = exc.response.text[:20000]
            self.last_call = {
                "provider": "deepseek",
                "model": self.settings.deepseek_model,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "attempt_count": self.attempt_count,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "raw_response": response_text,
            }
            raise DeepSeekError("DeepSeek API request failed.", error_type="http_error") from exc

        data = response.json()
        usage = data.get("usage") or {}
        self.last_call = {
            "provider": "deepseek",
            "model": data.get("model") or self.settings.deepseek_model,
            "request_id": data.get("id") or response.headers.get("x-request-id"),
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "attempt_count": self.attempt_count,
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "raw_response": json.dumps(data, ensure_ascii=False)[:20000],
        }
        try:
            content = data["choices"][0]["message"]["content"] or ""
            self.last_call["raw_response"] = content
            return content
        except (KeyError, IndexError, TypeError) as exc:
            raise DeepSeekError(
                "DeepSeek API returned an unexpected response shape.",
                error_type="response_shape",
            ) from exc

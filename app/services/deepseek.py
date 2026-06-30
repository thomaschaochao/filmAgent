import json
from typing import Any

import httpx

from app.config import Settings, get_settings


class DeepSeekError(RuntimeError):
    pass


class DeepSeekClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def chat_text(self, system_prompt: str, user_prompt: str) -> str:
        return self._request(system_prompt, user_prompt, json_output=False)

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for _ in range(2):
            try:
                content = self._request(system_prompt, user_prompt, json_output=True)
                if not content.strip():
                    raise DeepSeekError("DeepSeek returned empty content.")
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    raise DeepSeekError("DeepSeek JSON response must be an object.")
                return parsed
            except (json.JSONDecodeError, DeepSeekError) as exc:
                last_error = exc
        raise DeepSeekError("DeepSeek did not return valid structured JSON.") from last_error

    def _request(self, system_prompt: str, user_prompt: str, *, json_output: bool) -> str:
        payload: dict[str, Any] = {
            "model": self.settings.deepseek_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 8192,
            "thinking": {"type": "disabled"},
        }
        if json_output:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.settings.get_deepseek_api_key()}",
            "Content-Type": "application/json",
        }
        url = f"{self.settings.deepseek_base_url.rstrip('/')}/chat/completions"
        try:
            with httpx.Client(timeout=self.settings.deepseek_timeout_seconds) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DeepSeekError("DeepSeek API request failed.") from exc

        data = response.json()
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise DeepSeekError("DeepSeek API returned an unexpected response shape.") from exc

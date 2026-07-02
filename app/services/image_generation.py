import base64
from dataclasses import dataclass

import httpx

from app.config import Settings, get_settings


class ImageGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeneratedImage:
    content: bytes
    extension: str


def _decode_result(data: dict, client: httpx.Client) -> GeneratedImage:
    items = data.get("data")
    if not isinstance(items, list) or not items:
        raise ImageGenerationError("图片服务没有返回生成结果。")
    item = items[0]
    encoded = item.get("b64_json")
    if encoded:
        try:
            return GeneratedImage(base64.b64decode(encoded, validate=True), ".png")
        except (ValueError, TypeError) as exc:
            raise ImageGenerationError("图片服务返回了无效的图片数据。") from exc
    url = item.get("url")
    if not url:
        raise ImageGenerationError("图片服务返回结果中缺少图片数据。")
    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ImageGenerationError("无法下载图片服务生成的临时文件。") from exc
    content_type = response.headers.get("content-type", "").split(";", 1)[0]
    extension = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(content_type, ".png")
    return GeneratedImage(response.content, extension)


def _post_image(
    url: str, api_key: str, payload: dict, settings: Settings
) -> GeneratedImage:
    try:
        with httpx.Client(timeout=settings.image_timeout_seconds, follow_redirects=True) as client:
            response = client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            image = _decode_result(response.json(), client)
    except ImageGenerationError:
        raise
    except httpx.TimeoutException as exc:
        raise ImageGenerationError("图片生成超时，请稍后重试。") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in {401, 403}:
            message = "图片服务鉴权失败，请检查 config.local.env 中的 API Key。"
        elif status == 429:
            message = "图片服务请求过于频繁或额度不足，请稍后重试。"
        elif status == 400:
            message = "图片服务拒绝了当前提示词或生成参数。"
        else:
            message = f"图片服务暂时不可用（HTTP {status}）。"
        raise ImageGenerationError(message) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise ImageGenerationError("图片服务响应异常，请稍后重试。") from exc
    if not image.content or len(image.content) > 30 * 1024 * 1024:
        raise ImageGenerationError("生成图片为空或超过 30 MB 限制。")
    return image


def generate_image(
    provider: str,
    prompt: str,
    *,
    size: str = "1536x1024",
    quality: str = "high",
    settings: Settings | None = None,
) -> tuple[GeneratedImage, str]:
    settings = settings or get_settings()
    if provider == "openai":
        model = settings.openai_image_model
        payload = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "quality": quality,
            "output_format": "png",
            "n": 1,
        }
        result = _post_image(
            f"{settings.openai_base_url.rstrip('/')}/images/generations",
            settings.get_openai_api_key(),
            payload,
            settings,
        )
        return result, model
    if provider == "seedream":
        model = settings.seedream_model
        payload = {
            "model": model,
            "prompt": prompt,
            "size": "2K",
            "output_format": "png",
            "response_format": "b64_json",
            "watermark": False,
            "sequential_image_generation": "disabled",
        }
        result = _post_image(
            f"{settings.ark_base_url.rstrip('/')}/images/generations",
            settings.get_ark_api_key(),
            payload,
            settings,
        )
        return result, model
    raise ImageGenerationError("不支持的图片生成供应商。")

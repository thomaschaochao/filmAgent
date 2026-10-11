import base64
import json
import zlib
from dataclasses import dataclass

import httpx

from app.config import Settings, get_settings
from app.services.web_tools import WebToolError, validate_public_url

MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_BASE64_BYTES = ((MAX_IMAGE_BYTES + 2) // 3) * 4
# One maximum-size image plus a finite allowance for provider metadata.
MAX_RESPONSE_BYTES = MAX_BASE64_BYTES + 1024 * 1024
_ACCEPT_ENCODING = "gzip, deflate"


class ImageGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeneratedImage:
    content: bytes
    extension: str


def _read_limited_response(response: httpx.Response, limit: int) -> bytes:
    """Bound both wire bytes and decoded bytes, including compressed responses."""
    too_large = "图片服务响应超过大小限制。"
    length = response.headers.get("content-length")
    if length is not None:
        try:
            advertised = int(length)
        except ValueError:
            raise ImageGenerationError("图片服务响应异常，请稍后重试。") from None
        if advertised < 0:
            raise ImageGenerationError("图片服务响应异常，请稍后重试。")
        if advertised > limit:
            raise ImageGenerationError(too_large)

    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding in {"", "identity"}:
        decoder = None
    elif encoding in {"gzip", "deflate"}:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
    else:
        # Only advertised encodings are supported. HTTPX's normal iterator
        # decompresses without an output limit before splitting into chunks.
        raise ImageGenerationError("图片服务返回了不支持的压缩格式。")

    content = bytearray()
    wire_bytes = 0
    for chunk in response.iter_raw():
        wire_bytes += len(chunk)
        if wire_bytes > limit:
            raise ImageGenerationError(too_large)
        remaining = limit - len(content)
        if decoder is not None:
            chunk = decoder.decompress(chunk, remaining + 1)
        if len(chunk) > remaining:
            raise ImageGenerationError(too_large)
        content.extend(chunk)
        if decoder is not None and decoder.unused_data:
            # Reject extra members/trailing data instead of silently ignoring it.
            raise ImageGenerationError("图片服务返回了无效的压缩数据。")
    if decoder is not None and not decoder.eof:
        raise ImageGenerationError("图片服务返回了无效的压缩数据。")
    return bytes(content)


def _decode_result(data: object, settings: Settings) -> GeneratedImage:
    if not isinstance(data, dict):
        raise ImageGenerationError("图片服务返回了无效的生成结果。")
    items = data.get("data")
    if not isinstance(items, list) or not items:
        raise ImageGenerationError("图片服务没有返回生成结果。")
    item = items[0]
    if not isinstance(item, dict):
        raise ImageGenerationError("图片服务返回了无效的生成结果。")
    encoded = item.get("b64_json")
    if encoded is not None:
        if not isinstance(encoded, str) or not encoded:
            raise ImageGenerationError("图片服务返回了无效的图片数据。")
        if len(encoded) > MAX_BASE64_BYTES:
            raise ImageGenerationError("生成图片为空或超过 30 MB 限制。")
        try:
            return GeneratedImage(base64.b64decode(encoded, validate=True), ".png")
        except (ValueError, TypeError):
            raise ImageGenerationError("图片服务返回了无效的图片数据。") from None
    url = item.get("url")
    if not isinstance(url, str) or not url:
        raise ImageGenerationError("图片服务返回结果中缺少图片数据。")
    try:
        # Provider endpoints may be local/custom, but provider-supplied asset
        # URLs are untrusted. Private hosts and redirects are not supported.
        url = validate_public_url(url)
        # A fresh client prevents provider cookies/auth from reaching an asset
        # host, including one on the same origin. No redirect can replay a POST.
        with httpx.Client(timeout=settings.image_timeout_seconds, follow_redirects=False) as client:
            with client.stream(
                "GET", url, headers={"Accept-Encoding": _ACCEPT_ENCODING}, auth=None
            ) as response:
                response.raise_for_status()
                content = _read_limited_response(response, MAX_IMAGE_BYTES)
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    except httpx.TimeoutException:
        raise
    except (httpx.HTTPError, httpx.InvalidURL, WebToolError, ValueError):
        raise ImageGenerationError("无法下载图片服务生成的临时文件。") from None
    extension = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(content_type, ".png")
    return GeneratedImage(content, extension)


def _post_image(url: str, api_key: str, payload: dict, settings: Settings) -> GeneratedImage:
    try:
        with httpx.Client(timeout=settings.image_timeout_seconds, follow_redirects=False) as client:
            with client.stream(
                "POST",
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept-Encoding": _ACCEPT_ENCODING,
                },
                json=payload,
            ) as response:
                response.raise_for_status()
                body = _read_limited_response(response, MAX_RESPONSE_BYTES)
        image = _decode_result(json.loads(body), settings)
    except ImageGenerationError:
        raise
    except httpx.TimeoutException:
        raise ImageGenerationError("图片生成超时，请稍后重试。") from None
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
        raise ImageGenerationError(message) from None
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, RecursionError, zlib.error):
        raise ImageGenerationError("图片服务响应异常，请稍后重试。") from None
    if not image.content or len(image.content) > MAX_IMAGE_BYTES:
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

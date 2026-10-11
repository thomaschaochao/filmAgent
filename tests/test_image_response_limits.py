import asyncio
import base64
import gzip
import ipaddress
import json
import socket
import traceback
import zlib

import httpx
import pytest
from httpx import Client

from app.config import Settings
from app.services import image_generation
from app.services.image_generation import GeneratedImage, ImageGenerationError, _post_image

PROVIDER_URL = "https://provider.example/images/generations"
ASSET_URL = "https://assets.example/result.png?signature=private-token"
ORIGINAL_LIMITS = (
    image_generation.MAX_IMAGE_BYTES,
    image_generation.MAX_BASE64_BYTES,
    image_generation.MAX_RESPONSE_BYTES,
)


@pytest.fixture(autouse=True)
def isolate_requests(monkeypatch):
    def resolve(hostname, port):
        try:
            address = str(ipaddress.ip_address(hostname))
        except ValueError:
            address = "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    def unexpected_client(**kwargs):
        pytest.fail("Image requests must use an isolated mock transport")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(image_generation.httpx, "Client", unexpected_client)


@pytest.fixture
def settings(monkeypatch):
    # Exercise the same boundaries without allocating 30 MiB in every test.
    monkeypatch.setattr(image_generation, "MAX_IMAGE_BYTES", 3072)
    monkeypatch.setattr(image_generation, "MAX_BASE64_BYTES", 4096)
    monkeypatch.setattr(image_generation, "MAX_RESPONSE_BYTES", 4224)
    return Settings(settings_source_mode="environment", image_timeout_seconds=0.25)


class TrackingStream(httpx.SyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks = chunks
        self.error = error
        self.read_count = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk
        if self.error is not None:
            raise self.error

    def close(self):
        self.closed = True


def json_body(data):
    return json.dumps(data).encode()


def image_body(content=b"image"):
    return json_body({"data": [{"b64_json": base64.b64encode(content).decode()}]})


def install_client(monkeypatch, handler):
    clients = []
    requests = []

    def handle(request):
        requests.append(request)
        return handler(request)

    def create_client(**kwargs):
        client = Client(transport=httpx.MockTransport(handle), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(image_generation.httpx, "Client", create_client)
    return clients, requests


def install_response(monkeypatch, target, stream, *, headers=None, status=200):
    provider_stream = TrackingStream([json_body({"data": [{"url": ASSET_URL}]})])

    def handle(request):
        if target == "asset" and request.method == "POST":
            return httpx.Response(200, stream=provider_stream)
        return httpx.Response(status, headers=headers, stream=stream)

    return install_client(monkeypatch, handle)


def run(settings, url=PROVIDER_URL):
    return _post_image(url, "synthetic-key", {"prompt": "test"}, settings)


def assert_closed_once(clients, requests, target):
    assert len(requests) == (2 if target == "asset" else 1)
    assert sum(request.method == "POST" for request in requests) == 1
    assert all(client.is_closed for client in clients)


def test_production_limits_allow_base64_and_finite_metadata():
    binary, encoded, response = ORIGINAL_LIMITS
    assert binary == 30 * 1024 * 1024
    assert encoded == ((binary + 2) // 3) * 4
    assert response == encoded + 1024 * 1024


@pytest.mark.parametrize("target", ["provider", "asset"])
def test_rejects_advertised_oversize_before_reading(monkeypatch, settings, target):
    limit = 3072 if target == "asset" else 4224
    stream = TrackingStream([b"must not be read"])
    clients, requests = install_response(
        monkeypatch, target, stream, headers={"content-length": str(limit + 1)}
    )

    with pytest.raises(ImageGenerationError, match="超过大小限制"):
        run(settings)

    assert stream.read_count == 0
    assert stream.closed
    assert_closed_once(clients, requests, target)


@pytest.mark.parametrize("target", ["provider", "asset"])
@pytest.mark.parametrize("headers", [{}, {"content-length": "1"}])
def test_chunked_oversize_closes_early(monkeypatch, settings, target, headers):
    limit = 3072 if target == "asset" else 4224
    stream = TrackingStream([b"x" * limit, b"!", b"must not be read"])
    clients, requests = install_response(monkeypatch, target, stream, headers=headers)

    with pytest.raises(ImageGenerationError, match="超过大小限制"):
        run(settings)

    assert stream.read_count == 2
    assert stream.closed
    assert_closed_once(clients, requests, target)


@pytest.mark.parametrize("target", ["provider", "asset"])
@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
def test_accepts_exact_decoded_boundary(monkeypatch, settings, target, encoding):
    content = b"x" * 3072
    body = content if target == "asset" else image_body(content)
    if target == "provider":
        body += b" " * (4224 - len(body))
    if encoding == "gzip":
        body = gzip.compress(body)
    elif encoding == "deflate":
        body = zlib.compress(body)
    # Split compression headers and trailers across transport chunks.
    stream = TrackingStream([body[index : index + 1] for index in range(len(body))])
    clients, requests = install_response(
        monkeypatch,
        target,
        stream,
        headers={"content-encoding": encoding, "content-length": str(len(body))},
    )

    assert run(settings) == GeneratedImage(content, ".png")
    assert stream.closed
    assert_closed_once(clients, requests, target)


@pytest.mark.parametrize("target", ["provider", "asset"])
@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
def test_compression_expansion_is_capped_before_next_chunk(
    monkeypatch, settings, target, encoding, compress
):
    limit = 3072 if target == "asset" else 4224
    compressed = compress(b"x" * (limit * 128))
    stream = TrackingStream([compressed, b"must not be read"])
    clients, requests = install_response(
        monkeypatch,
        target,
        stream,
        headers={"content-encoding": encoding, "content-length": str(len(compressed))},
    )
    original_decoder = zlib.decompressobj
    output_limits = []

    class BoundedDecoder:
        def __init__(self, *args):
            self.decoder = original_decoder(*args)

        def decompress(self, data, max_length=0):
            output_limits.append(max_length)
            assert 0 < max_length <= limit + 1
            return self.decoder.decompress(data, max_length)

        def __getattr__(self, name):
            return getattr(self.decoder, name)

    monkeypatch.setattr(image_generation.zlib, "decompressobj", BoundedDecoder)

    with pytest.raises(ImageGenerationError, match="超过大小限制"):
        run(settings)

    assert output_limits == [limit + 1]
    assert stream.read_count == 1
    assert stream.closed
    assert_closed_once(clients, requests, target)


@pytest.mark.parametrize("target", ["provider", "asset"])
@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
@pytest.mark.parametrize("corruption", ["truncated", "garbage", "second-member", "invalid"])
def test_rejects_bad_compression(monkeypatch, settings, target, encoding, compress, corruption):
    body = compress(b"image" if target == "asset" else image_body())
    if corruption == "truncated":
        body = body[:-1]
    elif corruption == "garbage":
        body += b"private-diagnostic"
    elif corruption == "second-member":
        body += compress(b"extra")
    else:
        body = b"private-diagnostic"
    stream = TrackingStream([body[:1], body[1:]])
    clients, requests = install_response(
        monkeypatch, target, stream, headers={"content-encoding": encoding}
    )

    with pytest.raises(ImageGenerationError) as error:
        run(settings)

    assert "private-diagnostic" not in "".join(traceback.format_exception(error.value))
    assert stream.closed
    assert_closed_once(clients, requests, target)


@pytest.mark.parametrize("encoding", ["br", "zstd", "gzip, deflate"])
def test_rejects_unadvertised_encoding_without_reading(monkeypatch, settings, encoding):
    stream = TrackingStream([b"must not be read"])
    clients, requests = install_response(
        monkeypatch, "provider", stream, headers={"content-encoding": encoding}
    )
    with pytest.raises(ImageGenerationError, match="不支持的压缩格式"):
        run(settings)
    assert stream.read_count == 0
    assert stream.closed
    assert_closed_once(clients, requests, "provider")


@pytest.mark.parametrize("target", ["provider", "asset"])
def test_caps_compressed_wire_bytes_even_without_output(monkeypatch, settings, target):
    limit = 3072 if target == "asset" else 4224
    # A gzip filename field can grow without producing decoded output.
    header = b"\x1f\x8b\x08\x08\x00\x00\x00\x00\x00\xff"
    stream = TrackingStream([header + b"x" * (limit - len(header)), b"x", b"unread"])
    clients, requests = install_response(
        monkeypatch, target, stream, headers={"content-encoding": "gzip"}
    )
    with pytest.raises(ImageGenerationError, match="超过大小限制"):
        run(settings)
    assert stream.read_count == 2
    assert stream.closed
    assert_closed_once(clients, requests, target)


@pytest.mark.parametrize("target", ["provider", "asset"])
def test_trailing_compressed_garbage_in_later_chunk_is_rejected(monkeypatch, settings, target):
    body = gzip.compress(b"image" if target == "asset" else image_body())
    stream = TrackingStream([body, b"private-diagnostic", b"unread"])
    clients, requests = install_response(
        monkeypatch, target, stream, headers={"content-encoding": "gzip"}
    )
    with pytest.raises(ImageGenerationError, match="无效的压缩数据"):
        run(settings)
    assert stream.read_count == 2
    assert stream.closed
    assert_closed_once(clients, requests, target)


@pytest.mark.parametrize("length", ["bad-private-diagnostic", "-1", "1, 2"])
def test_rejects_bad_content_length_safely(monkeypatch, settings, length):
    stream = TrackingStream([b"must not be read"])
    clients, requests = install_response(
        monkeypatch, "provider", stream, headers={"content-length": length}
    )
    with pytest.raises(ImageGenerationError) as error:
        run(settings)
    assert "private-diagnostic" not in "".join(traceback.format_exception(error.value))
    assert stream.read_count == 0
    assert stream.closed
    assert_closed_once(clients, requests, "provider")


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "private-diagnostic",
        4,
        {},
        {"data": []},
        {"data": {}},
        {"data": None},
        {"data": [None]},
        {"data": [4]},
        {"data": [[]]},
        {"data": ["private-diagnostic"]},
        {"data": [{}]},
        {"data": [{"url": []}]},
        {"data": [{"url": 42}]},
    ],
)
def test_malformed_envelopes_fail_without_attribute_errors(monkeypatch, settings, data):
    stream = TrackingStream([json_body(data)])
    clients, requests = install_response(monkeypatch, "provider", stream)
    with pytest.raises(ImageGenerationError) as error:
        run(settings)
    assert "private-diagnostic" not in str(error.value)
    assert stream.closed
    assert_closed_once(clients, requests, "provider")


@pytest.mark.parametrize("body", [b"", b"private-diagnostic", b"\xff", b"[" * 1200])
def test_malformed_json_fails_generically(monkeypatch, settings, body):
    stream = TrackingStream([body])
    clients, requests = install_response(monkeypatch, "provider", stream)
    with pytest.raises(ImageGenerationError) as error:
        run(settings)
    assert "private-diagnostic" not in "".join(traceback.format_exception(error.value))
    assert stream.closed
    assert_closed_once(clients, requests, "provider")


@pytest.mark.parametrize("encoded", ["", " ", "!invalid!", "é", "a", 7, False, {}, []])
def test_malformed_base64_never_downloads_fallback_url(monkeypatch, settings, encoded):
    body = json_body({"data": [{"b64_json": encoded, "url": ASSET_URL}]})
    stream = TrackingStream([body])
    clients, requests = install_response(monkeypatch, "provider", stream)
    with pytest.raises(ImageGenerationError, match="无效的图片数据"):
        run(settings)
    assert stream.closed
    assert_closed_once(clients, requests, "provider")


def test_rejects_base64_size_before_decoding(monkeypatch, settings):
    stream = TrackingStream([image_body(b"x" * 3073)])
    clients, requests = install_response(monkeypatch, "provider", stream)
    monkeypatch.setattr(
        image_generation.base64, "b64decode", lambda *args, **kwargs: pytest.fail("Must not decode")
    )
    with pytest.raises(ImageGenerationError, match="30 MB"):
        run(settings)
    assert_closed_once(clients, requests, "provider")


def test_binary_size_is_checked_after_base64_rounding(monkeypatch, settings):
    monkeypatch.setattr(image_generation, "MAX_IMAGE_BYTES", 3071)
    stream = TrackingStream([image_body(b"x" * 3072)])
    clients, requests = install_response(monkeypatch, "provider", stream)
    with pytest.raises(ImageGenerationError, match="30 MB"):
        run(settings)
    assert_closed_once(clients, requests, "provider")


@pytest.mark.parametrize(
    "content_type,extension",
    [("image/jpeg; q=1", ".jpg"), ("image/webp", ".webp"), ("image/png", ".png")],
)
def test_asset_download_has_no_provider_auth_or_cookies(
    monkeypatch, settings, content_type, extension
):
    provider = TrackingStream([json_body({"data": [{"url": "https://provider.example/asset"}]})])
    asset = TrackingStream([b"image"])

    def handle(request):
        if request.method == "POST":
            assert request.headers["authorization"] == "Bearer synthetic-key"
            return httpx.Response(
                200, headers={"set-cookie": "provider-session=secret"}, stream=provider
            )
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, headers={"content-type": content_type}, stream=asset)

    clients, requests = install_client(monkeypatch, handle)
    assert run(settings) == GeneratedImage(b"image", extension)
    assert provider.closed and asset.closed
    assert len(clients) == 2
    assert all(request.headers["accept-encoding"] == "gzip, deflate" for request in requests)
    assert_closed_once(clients, requests, "asset")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/private",
        "http://127.0.0.1/private",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/private",
        "file:///etc/passwd",
        "https://u:secret@assets.example/picture",
        "https://assets.example:bad/private",
        "https://[invalid/private",
    ],
)
def test_rejects_unsafe_asset_urls_before_download(monkeypatch, settings, url):
    stream = TrackingStream([json_body({"data": [{"url": url}]})])
    clients, requests = install_response(monkeypatch, "provider", stream)
    with pytest.raises(ImageGenerationError, match="无法下载") as error:
        run(settings)
    assert url not in "".join(traceback.format_exception(error.value))
    assert_closed_once(clients, requests, "provider")


def test_dns_failure_is_generic_and_does_not_repeat_post(monkeypatch, settings):
    def failed_dns(*args):
        raise socket.gaierror("private-diagnostic")

    monkeypatch.setattr(socket, "getaddrinfo", failed_dns)
    stream = TrackingStream([json_body({"data": [{"url": ASSET_URL}]})])
    clients, requests = install_response(monkeypatch, "provider", stream)
    with pytest.raises(ImageGenerationError, match="无法下载") as error:
        run(settings)
    assert "private-diagnostic" not in "".join(traceback.format_exception(error.value))
    assert_closed_once(clients, requests, "provider")


@pytest.mark.parametrize("target", ["provider", "asset"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_never_follows_redirect_or_reads_its_body(monkeypatch, settings, target, status):
    stream = TrackingStream([b"must not be read"])
    clients, requests = install_response(
        monkeypatch, target, stream, status=status, headers={"location": "http://127.0.0.1/private"}
    )
    with pytest.raises(ImageGenerationError) as error:
        run(settings)
    assert "127.0.0.1" not in "".join(traceback.format_exception(error.value))
    assert stream.read_count == 0
    assert stream.closed
    assert_closed_once(clients, requests, target)


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_provider_error_never_reads_body_or_retries(monkeypatch, settings, status):
    stream = TrackingStream([b"private-diagnostic"])
    clients, requests = install_response(monkeypatch, "provider", stream, status=status)
    with pytest.raises(ImageGenerationError) as error:
        run(settings)
    assert "private-diagnostic" not in "".join(traceback.format_exception(error.value))
    assert stream.read_count == 0
    assert stream.closed
    assert_closed_once(clients, requests, "provider")


@pytest.mark.parametrize("target", ["provider", "asset"])
@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ReadError, asyncio.CancelledError])
def test_interrupted_stream_always_closes_without_retry(monkeypatch, settings, target, error_type):
    stream = TrackingStream([b"partial"], error=error_type("private-diagnostic"))
    clients, requests = install_response(monkeypatch, target, stream)
    expected = (
        asyncio.CancelledError if error_type is asyncio.CancelledError else ImageGenerationError
    )
    with pytest.raises(expected) as error:
        run(settings)
    if expected is ImageGenerationError:
        assert "private-diagnostic" not in "".join(traceback.format_exception(error.value))
    assert stream.closed
    assert_closed_once(clients, requests, target)


def test_empty_asset_is_rejected(monkeypatch, settings):
    stream = TrackingStream([])
    clients, requests = install_response(monkeypatch, "asset", stream)
    with pytest.raises(ImageGenerationError, match="为空"):
        run(settings)
    assert stream.closed
    assert_closed_once(clients, requests, "asset")


def test_custom_local_provider_still_supports_base64(monkeypatch, settings):
    stream = TrackingStream([image_body()])
    clients, requests = install_response(monkeypatch, "provider", stream)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: pytest.fail("Unexpected DNS lookup"))
    assert run(settings, "http://127.0.0.1:8000/custom/images/generations") == GeneratedImage(
        b"image", ".png"
    )
    assert_closed_once(clients, requests, "provider")

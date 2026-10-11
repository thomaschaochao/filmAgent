import asyncio
import gzip
import ipaddress
import socket

import httpx
import pytest
from httpx import AsyncClient

from app.config import Settings
from app.services import web_tools
from app.services.web_tools import WebToolError, fetch_page, validate_public_url


@pytest.fixture(autouse=True)
def isolate_web_fetch(monkeypatch):
    def resolve(hostname, port):
        try:
            address = str(ipaddress.ip_address(hostname))
        except ValueError:
            address = "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    async def no_crawler(url):
        return None

    def unexpected_client(**kwargs):
        pytest.fail("HTTP requests must use a mock transport")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(web_tools, "_crawl4ai_fetch", no_crawler)
    monkeypatch.setattr(web_tools.httpx, "AsyncClient", unexpected_client)


@pytest.fixture
def settings():
    return Settings(settings_source_mode="environment", web_max_bytes=10_000)


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, stall=False, error=None):
        self.chunks = chunks
        self.stall = stall
        self.error = error
        self.read_count = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk
        if self.stall:
            await asyncio.Event().wait()
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


def install_http_client(monkeypatch, handler):
    clients = []

    def create_client(**kwargs):
        client = AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(web_tools.httpx, "AsyncClient", create_client)
    return clients


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost:8000/private",
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/admin",
        "https://private.local/page",
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


@pytest.mark.parametrize(
    "url",
    [
        "https://reader:secret@example.com/page",
        "https://reader@example.com/page",
        "https://@example.com/page",
    ],
)
def test_web_fetch_rejects_userinfo_before_dns(monkeypatch, url):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: pytest.fail("Unexpected DNS lookup"))
    with pytest.raises(WebToolError, match="^URLs containing credentials are not allowed$"):
        validate_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com:secret/page",
        "https://example.com:65536/page",
        "https://example.com:-1/page",
        "https://[invalid/page",
    ],
)
def test_web_fetch_rejects_malformed_url_safely(monkeypatch, url):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: pytest.fail("Unexpected DNS lookup"))
    with pytest.raises(WebToolError, match="^The page URL is invalid$"):
        validate_public_url(url)


@pytest.mark.parametrize("scheme,port", [("http", 80), ("https", 443)])
def test_web_fetch_resolves_correct_default_port(monkeypatch, scheme, port):
    calls = []

    def resolve(hostname, requested_port):
        calls.append((hostname, requested_port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", requested_port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    validate_public_url(f"{scheme}://example.com/research")
    assert calls == [("example.com", port)]


def test_web_fetch_rejects_mixed_public_private_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ],
    )
    with pytest.raises(WebToolError, match="Local and private network"):
        validate_public_url("https://example.com/page")


def test_web_fetch_reports_dns_failure_safely(monkeypatch):
    def failed_dns(*args):
        raise socket.gaierror("private diagnostic")

    monkeypatch.setattr(socket, "getaddrinfo", failed_dns)
    with pytest.raises(WebToolError, match="^The web host could not be resolved$"):
        validate_public_url("https://example.com/page")


def test_web_fetch_extracts_streamed_html_and_closes_response(monkeypatch, settings):
    body = "<h1>Research</h1><script>ignored</script><p>Café &amp; cinema</p>".encode()
    split = body.index("é".encode()) + 1
    stream = TrackingStream([body[:split], body[split:]])
    clients = install_http_client(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, stream=stream
        ),
    )

    assert asyncio.run(fetch_page("https://example.com/research", settings)) == {
        "url": "https://example.com/research",
        "content": "Research\nCafé & cinema",
        "method": "httpx",
    }
    assert stream.closed
    assert all(client.is_closed for client in clients)


def test_web_fetch_accepts_body_at_exact_size_limit(monkeypatch, settings):
    stream = TrackingStream([b"x" * settings.web_max_bytes])
    install_http_client(monkeypatch, lambda request: httpx.Response(200, stream=stream))

    result = asyncio.run(fetch_page("https://example.com/page", settings))

    assert result["content"] == "x" * settings.web_max_bytes
    assert stream.closed


@pytest.mark.parametrize("headers", [{}, {"content-length": "1"}])
def test_web_fetch_stops_reading_oversized_body(monkeypatch, settings, headers):
    stream = TrackingStream([b"x" * settings.web_max_bytes, b"!", b"must not be read"])
    clients = install_http_client(
        monkeypatch, lambda request: httpx.Response(200, headers=headers, stream=stream)
    )

    with pytest.raises(WebToolError, match="^The page exceeds the maximum allowed size$"):
        asyncio.run(fetch_page("https://example.com/page", settings))

    assert stream.read_count == 2
    assert stream.closed
    assert all(client.is_closed for client in clients)


def test_web_fetch_limits_decoded_compressed_bytes(monkeypatch, settings):
    compressed = gzip.compress(b"x" * (settings.web_max_bytes + 1))
    stream = TrackingStream([compressed, b"must not be read"])
    install_http_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-length": str(len(compressed))},
            stream=stream,
        ),
    )

    with pytest.raises(WebToolError, match="maximum allowed size"):
        asyncio.run(fetch_page("https://example.com/page", settings))

    assert stream.read_count == 1
    assert stream.closed


def test_web_fetch_follows_revalidated_relative_redirect(monkeypatch, settings):
    redirects = TrackingStream([b"redirect body must not be read"])
    page = TrackingStream([b"<p>Found</p>"])
    requested = []
    validated = []
    original_validate = validate_public_url

    def record_validation(url):
        validated.append(url)
        return original_validate(url)

    def handler(request):
        requested.append(str(request.url))
        if len(requested) == 1:
            return httpx.Response(302, headers={"location": "../page"}, stream=redirects)
        return httpx.Response(200, stream=page)

    monkeypatch.setattr(web_tools, "validate_public_url", record_validation)
    install_http_client(monkeypatch, handler)

    result = asyncio.run(fetch_page("https://example.com/start/here", settings))

    assert result == {"url": "https://example.com/page", "content": "Found", "method": "httpx"}
    assert (
        requested
        == validated
        == [
            "https://example.com/start/here",
            "https://example.com/page",
        ]
    )
    assert redirects.read_count == 0
    assert redirects.closed and page.closed


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data",
        "https://reader:secret@example.com/private",
        "https://example.com:secret/private",
        "file:///etc/passwd",
    ],
)
def test_web_fetch_rejects_unsafe_redirect_before_following(monkeypatch, settings, location):
    stream = TrackingStream([b"redirect body must not be read"])
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": location}, stream=stream)

    install_http_client(monkeypatch, handler)
    with pytest.raises(WebToolError):
        asyncio.run(fetch_page("https://example.com/page", settings))

    assert len(requests) == 1
    assert stream.read_count == 0
    assert stream.closed


def test_web_fetch_rejects_redirect_without_location(monkeypatch, settings):
    stream = TrackingStream([])
    install_http_client(monkeypatch, lambda request: httpx.Response(302, stream=stream))

    with pytest.raises(WebToolError, match="^The page returned an invalid redirect$"):
        asyncio.run(fetch_page("https://example.com/page", settings))

    assert stream.closed


def test_web_fetch_caps_redirect_requests(monkeypatch, settings):
    streams = []

    def handler(request):
        stream = TrackingStream([b"redirect body must not be read"])
        streams.append(stream)
        return httpx.Response(302, headers={"location": "/again"}, stream=stream)

    install_http_client(monkeypatch, handler)
    with pytest.raises(WebToolError, match="^The page redirected too many times$"):
        asyncio.run(fetch_page("https://example.com/page", settings))

    assert len(streams) == 4
    assert all(stream.closed and stream.read_count == 0 for stream in streams)


def test_web_fetch_deadline_cancels_stalled_body_and_closes_response(monkeypatch, settings):
    stream = TrackingStream([b"<p>unfinished"], stall=True)
    clients = install_http_client(monkeypatch, lambda request: httpx.Response(200, stream=stream))
    settings = settings.model_copy(update={"web_timeout_seconds": 0.02})

    with pytest.raises(WebToolError, match="^The page request timed out$"):
        asyncio.run(fetch_page("https://example.com/page", settings))

    assert stream.read_count == 1
    assert stream.closed
    assert all(client.is_closed for client in clients)


def test_web_fetch_uses_one_deadline_across_redirects(monkeypatch, settings):
    streams = []

    async def handler(request):
        await asyncio.sleep(0.02)
        stream = TrackingStream([])
        streams.append(stream)
        return httpx.Response(302, headers={"location": "/again"}, stream=stream)

    install_http_client(monkeypatch, handler)
    settings = settings.model_copy(update={"web_timeout_seconds": 0.05})

    with pytest.raises(WebToolError, match="^The page request timed out$"):
        asyncio.run(fetch_page("https://example.com/page", settings))

    assert 1 <= len(streams) < 4
    assert all(stream.closed for stream in streams)


def test_web_fetch_preserves_http_status_errors(monkeypatch, settings):
    stream = TrackingStream([b"error body must not be read"])
    install_http_client(monkeypatch, lambda request: httpx.Response(503, stream=stream))

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(fetch_page("https://example.com/page", settings))

    assert stream.read_count == 0
    assert stream.closed


def test_web_fetch_preserves_transport_errors_and_closes_response(monkeypatch, settings):
    stream = TrackingStream([b"unfinished"], error=httpx.ReadError("connection lost"))
    install_http_client(monkeypatch, lambda request: httpx.Response(200, stream=stream))

    with pytest.raises(httpx.ReadError):
        asyncio.run(fetch_page("https://example.com/page", settings))

    assert stream.closed


def test_web_fetch_reports_invalid_response_safely(monkeypatch, settings):
    stream = TrackingStream([], error=httpx.RemoteProtocolError("private diagnostic"))
    install_http_client(monkeypatch, lambda request: httpx.Response(200, stream=stream))

    with pytest.raises(WebToolError, match="^The page returned an invalid URL or HTTP response$"):
        asyncio.run(fetch_page("https://example.com/page", settings))

    assert stream.closed


def test_web_fetch_keeps_optional_crawler_result(monkeypatch, settings):
    async def crawler(url):
        return "# Research\nCrawler text"

    monkeypatch.setattr(web_tools, "_crawl4ai_fetch", crawler)
    assert asyncio.run(fetch_page("https://example.com/page", settings)) == {
        "url": "https://example.com/page",
        "content": "# Research\nCrawler text",
        "method": "crawl4ai",
    }

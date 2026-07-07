from __future__ import annotations

import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from app.config import Settings, get_settings


class WebToolError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored += 1
        elif tag in {"p", "br", "li", "h1", "h2", "h3", "article", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._ignored:
            self._ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored and data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return "\n".join(line.strip() for line in " ".join(self.parts).splitlines() if line.strip())


def validate_public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WebToolError("Only public HTTP and HTTPS URLs are allowed")
    hostname = parsed.hostname.casefold()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        raise WebToolError("Local and private network URLs are not allowed")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or 443)}
    except socket.gaierror as exc:
        raise WebToolError("The web host could not be resolved") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise WebToolError("Local and private network URLs are not allowed")
    return url


async def _crawl4ai_fetch(url: str) -> str | None:
    try:
        from crawl4ai import AsyncWebCrawler
    except ImportError:
        return None
    async with AsyncWebCrawler() as crawler:
        result = await crawler.arun(url=url)
    if not getattr(result, "success", False):
        return None
    markdown = getattr(result, "markdown", None)
    if hasattr(markdown, "raw_markdown"):
        markdown = markdown.raw_markdown
    return str(markdown).strip() if markdown else None


async def fetch_page(url: str, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    current_url = validate_public_url(url)
    markdown = await _crawl4ai_fetch(current_url)
    if markdown:
        return {"url": current_url, "content": markdown, "method": "crawl4ai"}

    async with httpx.AsyncClient(timeout=settings.web_timeout_seconds) as client:
        for _ in range(4):
            response = await client.get(
                current_url,
                follow_redirects=False,
                headers={"User-Agent": "FilmPilot research assistant/1.0"},
            )
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise WebToolError("The page returned an invalid redirect")
                current_url = validate_public_url(urljoin(current_url, location))
                continue
            response.raise_for_status()
            content = response.content[: settings.web_max_bytes]
            extractor = _TextExtractor()
            extractor.feed(content.decode(response.encoding or "utf-8", errors="replace"))
            return {"url": current_url, "content": extractor.text(), "method": "httpx"}
    raise WebToolError("The page redirected too many times")


def _response_text(payload: dict) -> tuple[str, list[dict]]:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"], []
    texts: list[str] = []
    sources: list[dict] = []
    for output in payload.get("output", []):
        for content in output.get("content", []):
            if content.get("text"):
                texts.append(content["text"])
            for annotation in content.get("annotations", []):
                url = annotation.get("url") or annotation.get("url_citation", {}).get("url")
                if url:
                    sources.append({"url": url, "title": annotation.get("title", url)})
    return "\n".join(texts), sources


async def search_web(query: str, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    if not settings.ark_search_model:
        raise WebToolError("Ark Web Search is not configured")
    async with httpx.AsyncClient(timeout=settings.web_timeout_seconds) as client:
        response = await client.post(
            f"{settings.ark_base_url.rstrip('/')}/responses",
            headers={"Authorization": f"Bearer {settings.get_ark_api_key()}"},
            json={
                "model": settings.ark_search_model,
                "input": query,
                "tools": [{"type": "web_search"}],
            },
        )
    if response.status_code >= 400:
        raise WebToolError(f"Ark Web Search request failed ({response.status_code})")
    text, sources = _response_text(response.json())
    return {"query": query, "summary": text, "sources": sources, "provider": "volcengine"}

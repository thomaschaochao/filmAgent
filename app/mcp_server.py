"""Optional read-only MCP entrypoint: python -m app.mcp_server."""

import asyncio

from app.database import SessionLocal
from app.schemas import RetrievalQuery
from app.services.agent_retrieval import get_retrieval_status_snapshot
from app.services.agent_retrieval import retrieve_context as retrieve_agent_context
from app.services.web_tools import fetch_page, search_web


def build_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("Install FilmPilot with the 'tools' extra to run MCP") from exc

    server = FastMCP("FilmPilot Read-only Tools")

    @server.tool()
    def retrieval_status() -> dict:
        """Return local RAG availability without exposing paths or secrets."""
        with SessionLocal() as db:
            return get_retrieval_status_snapshot(db)

    @server.tool()
    def retrieve_context(query: str, project_id: str | None = None, limit: int = 8) -> list[dict]:
        """Retrieve authoritative script chunks for a FilmPilot project."""
        with SessionLocal() as db:
            return retrieve_agent_context(
                RetrievalQuery(query=query, project_id=project_id, limit=limit), db
            )

    @server.tool()
    def web_search(query: str) -> dict:
        """Search the public web through the configured Ark Web Search fallback."""
        return asyncio.run(search_web(query))

    @server.tool()
    def fetch_public_page(url: str) -> dict:
        """Fetch a public page with SSRF protection and Crawl4AI when installed."""
        return asyncio.run(fetch_page(url))

    return server


if __name__ == "__main__":
    build_server().run()

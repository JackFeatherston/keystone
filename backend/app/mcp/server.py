"""MCP server (streamable HTTP, mounted at /mcp). Used by Claude directly and by ChatGPT
(developer-mode connectors). ChatGPT can alternatively use the REST API's /openapi.json as Actions."""
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .. import service

mcp = FastMCP("keystone", stateless_http=True)
mcp.settings.streamable_http_path = "/"


@mcp.tool()
def get_project_state(project_id: str) -> dict:
    """Current project overview: active decisions and open tasks."""
    return service.get_project_state(project_id)


@mcp.tool()
def search_context(project_id: str, query: str, limit: int = 10) -> list[dict]:
    """Semantic search over project decisions and source text."""
    return service.search_context(project_id, query, limit)


@mcp.tool()
def get_decisions(project_id: str, include_history: bool = False) -> list[dict]:
    """Decisions; set include_history to also see superseded ones."""
    return service.get_decisions(project_id, include_history)


@mcp.tool()
def get_open_tasks(project_id: str) -> list[dict]:
    """Outstanding tasks."""
    return service.get_open_tasks(project_id)


@mcp.tool()
def record_decision(project_id: str, client: Literal["claude", "chatgpt"], title: str,
                    value: str, reason: str = "", subject: str = "") -> dict:
    """Submit a decision. It is stored as 'proposed' until a human approves it."""
    return service.add_event(project_id, client, "decision_proposed",
                             {"title": title, "value": value, "reason": reason, "subject": subject or title})

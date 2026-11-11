"""MCP server (streamable HTTP, mounted at /mcp). Used by Claude directly and by ChatGPT
(developer-mode connectors). ChatGPT can alternatively use the REST API's /openapi.json as Actions."""
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .. import service

# Host-header (DNS rebinding) check would reject tunnel hostnames; /mcp is already key-protected in main.py.
mcp = FastMCP("keystone", stateless_http=True,
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
mcp.settings.streamable_http_path = "/"

PROJECT_DOC = ("`project` is a project name or ID; leave it empty if the user has only one project. "
               "Never ask the user for an ID: call list_projects and pick the match from context.")


@mcp.tool()
def list_projects() -> list[dict]:
    """The user's projects (id, name, description). Use to pick the project the conversation is about."""
    return service.list_projects()


@mcp.tool(description=f"Current project overview: active decisions and open tasks. {PROJECT_DOC}")
def get_project_state(project: str = "") -> dict:
    return service.get_project_state(service.resolve_project(project))


@mcp.tool(description=f"Semantic search over project decisions and source text. {PROJECT_DOC}")
def search_context(query: str, limit: int = 10, project: str = "") -> list[dict]:
    return service.search_context(service.resolve_project(project), query, limit)


@mcp.tool(description=f"Decisions; set include_history to also see superseded ones. {PROJECT_DOC}")
def get_decisions(include_history: bool = False, project: str = "") -> list[dict]:
    return service.get_decisions(service.resolve_project(project), include_history)


@mcp.tool(description=f"Outstanding tasks. {PROJECT_DOC}")
def get_open_tasks(project: str = "") -> list[dict]:
    return service.get_open_tasks(service.resolve_project(project))


@mcp.tool(description=f"""Record a project decision so every AI tool shares it.

Call this, without being asked, whenever the user commits to a choice ("yeah we'll go with MongoDB",
"we're now using Postgres"), or when you suggest one worth tracking.

- decided_by="user": the user themselves stated or agreed to the decision. user_quote MUST be their
  exact words. Stored as approved. Do not use this for hedged statements ("maybe postgres?").
- decided_by="ai_suggestion": your own recommendation. Stored as proposed until the user confirms.

subject is the topic the decision is about (e.g. "database", "auth provider"). Call get_decisions first
and reuse an existing subject, so the new decision supersedes the old one instead of duplicating it.
value is the choice itself (e.g. "PostgreSQL"). {PROJECT_DOC}""")
def record_decision(client: Literal["claude", "chatgpt"], title: str, subject: str, value: str,
                    decided_by: Literal["user", "ai_suggestion"], user_quote: str = "",
                    reason: str = "", project: str = "") -> dict:
    if decided_by == "user" and not user_quote.strip():
        raise ValueError("decided_by='user' requires user_quote: the user's exact words")
    payload = {"title": title, "value": value, "reason": reason, "subject": subject}
    if decided_by == "user":
        payload["user_quote"] = user_quote
    return service.add_event(service.resolve_project(project), client,
                             "decision_confirmed" if decided_by == "user" else "decision_proposed", payload)

"""MCP server (streamable HTTP, mounted at /mcp). Used by Claude directly and by ChatGPT
(developer-mode connectors). ChatGPT can alternatively use the REST API's /openapi.json as Actions."""
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel

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


@mcp.tool(description=f"Current project overview: active decisions, constraints, open tasks and pending proposals. {PROJECT_DOC}")
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


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


@mcp.tool(description=f"""Share the relevant part of this conversation so every AI tool stays in sync.

Call this, without being asked, whenever the conversation contains something worth remembering about
the project: the user commits to a choice ("yeah we're now using Postgres"), agrees to or rejects one of
your suggestions, states a requirement or limitation, or commits to a task. Also call it when you
recommend something worth tracking. Keystone extracts decisions, constraints and tasks in the background.

messages: the relevant turns, in order. USER messages MUST be copied verbatim (their exact words are the
evidence that they decided something; paraphrased user text is only stored as a proposal). Include the
assistant turn the user is responding to when they say things like "yes, do that". Keep it to the
relevant excerpt, not the whole conversation.

Extraction runs in the background, so this call does not tell you what was recorded or with what status
(approved, proposed, task). Don't tell the user how it was recorded; if they ask, check get_decisions /
get_open_tasks / list_proposals. {PROJECT_DOC}""")
def log_context(client: Literal["claude", "chatgpt"], messages: list[Message], project: str = "") -> dict:
    ev = service.log_context(service.resolve_project(project), client, [m.model_dump() for m in messages])
    return {"queued": True, "event_id": ev["id"],
            "note": "Extraction is running in the background; the outcome is not known yet. Don't tell the "
                    "user whether it was recorded as approved or proposed. Check get_decisions, "
                    "get_open_tasks or list_proposals if they ask."}


@mcp.tool(description=f"""AI-inferred decisions, constraints and tasks awaiting the user's review.
Show them to the user and ask which to confirm or reject. {PROJECT_DOC}""")
def list_proposals(project: str = "") -> list[dict]:
    return service.list_proposals(service.resolve_project(project))


@mcp.tool(description=f"""Confirm a proposal (id from list_proposals) after the user agrees to it.
user_quote MUST be the user's exact words agreeing. {PROJECT_DOC}""")
def confirm_proposal(client: Literal["claude", "chatgpt"], entity_id: str, user_quote: str,
                     project: str = "") -> dict:
    if not user_quote.strip():
        raise ValueError("user_quote is required: the user's exact words")
    return service.review(service.resolve_project(project), client, "proposal_confirmed", entity_id,
                          user_quote=user_quote)


@mcp.tool(description=f"""Reject a proposal (id from list_proposals) the user turned down. {PROJECT_DOC}""")
def reject_proposal(client: Literal["claude", "chatgpt"], entity_id: str, user_quote: str = "",
                    project: str = "") -> dict:
    return service.review(service.resolve_project(project), client, "proposal_rejected", entity_id,
                          user_quote=user_quote)


@mcp.tool(description=f"""Mark an approved decision/constraint as implemented, or an open task as done.
evidence: what shows it is done (commit, PR, file, or the user's words). {PROJECT_DOC}""")
def mark_implemented(client: Literal["claude", "chatgpt"], entity_id: str, evidence: str,
                     project: str = "") -> dict:
    return service.review(service.resolve_project(project), client, "entity_implemented", entity_id,
                          evidence=evidence)

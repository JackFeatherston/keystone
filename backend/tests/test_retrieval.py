"""Retrieval integration tests against the real Supabase DB, with a deterministic bag-of-words embedding.
Events are inserted without jobs and processed directly, so a running dev server can't race these tests."""
import re
import uuid

import pytest

from app import context_engine, extraction, retrieval, service
from app.config import settings
from app.db import supabase as sb


def bag_of_words(text, task=None):
    v = [0.01] * 768
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        v[hash(w) % 768] += 1
    return v


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    monkeypatch.setattr(context_engine, "embed", bag_of_words)
    monkeypatch.setattr(retrieval, "embed_query", bag_of_words)
    monkeypatch.setattr(extraction, "find_conflicts", lambda new, existing: [])


@pytest.fixture
def project():
    p = sb.table("projects").insert({"owner_id": settings.KEYSTONE_OWNER_ID, "name": f"test-{uuid.uuid4().hex[:6]}"}).execute().data[0]
    yield p["id"]
    sb.table("entities").delete().eq("project_id", p["id"]).execute()
    sb.table("projects").delete().eq("id", p["id"]).execute()


def _process(pid, etype, payload, source="claude"):
    ev = sb.table("events").insert({"project_id": pid, "source": source, "event_type": etype,
                                    "payload": payload}).execute().data[0]
    for derived in context_engine.process_event(ev, queue=False) or []:
        context_engine.process_event(derived)
    return ev


def excerpt(pid, monkeypatch, user_text, items):
    """A logged excerpt whose extraction returns `items` (user_quote defaults to the whole user text)."""
    items = [{"stated_by": "user", "user_quote": user_text, "reason": "", **i} for i in items]
    monkeypatch.setattr(extraction, "extract", lambda m, known: items)
    messages = [{"role": "user", "content": user_text}]
    return _process(pid, "context_logged", {"messages": messages, "text": extraction.render(messages)})


def decision(pid, monkeypatch, text, subject, value, kind="decision"):
    return excerpt(pid, monkeypatch, text, [{"kind": kind, "subject": subject, "title": text, "value": value}])


def test_superseded_excluded_by_default_and_labeled_with_history(project, monkeypatch):
    decision(project, monkeypatch, "we use mongodb for the database", "database", "MongoDB")
    decision(project, monkeypatch, "replacing mongodb with postgresql for the database", "database", "PostgreSQL")
    current = service.search_context(project, "which database")
    assert [r["value"] for r in current if r["kind"] == "decision"] == ["PostgreSQL"]
    assert all(r["kind"] != "source" for r in current)  # both excerpts folded into their decisions
    history = {r["value"]: r["status"] for r in service.search_context(project, "which database", include_history=True)}
    assert history == {"PostgreSQL": "approved", "MongoDB": "superseded"}


def test_rejected_proposal_excluded(project, monkeypatch):
    ev = excerpt(project, monkeypatch, "what about a redis cache?",
                 [{"kind": "decision", "subject": "cache", "title": "Redis cache", "value": "Redis", "stated_by": "assistant", "user_quote": ""}])
    (ent,) = sb.table("entities").select("id,status").eq("project_id", project).execute().data
    assert ent["status"] == "proposed"
    assert [r["status"] for r in service.search_context(project, "redis cache")] == ["proposed"]
    _process(project, "proposal_rejected", {"entity_id": ent["id"], "user_quote": "no"})
    assert service.search_context(project, "redis cache") == []
    assert ev["id"]  # the excerpt itself is kept as history, just not returned as current


def test_each_entity_returned_once_and_orphan_excerpt_is_a_source(project, monkeypatch):
    decision(project, monkeypatch, "payments go through stripe checkout", "payments", "Stripe")
    note = excerpt(project, monkeypatch, "trail data comes from the park service api, capped at 1000 requests per day", [])
    res = service.search_context(project, "stripe payments and trail data api")
    assert sorted(r["kind"] for r in res) == ["decision", "source"]
    (src,) = [r for r in res if r["kind"] == "source"]
    assert src["event_id"] == note["id"] and src["status"] == "source"


def test_full_text_finds_exact_terms(project, monkeypatch):
    monkeypatch.setattr(retrieval, "embed_query", lambda q: [1.0] * 768)  # vector side ranks everything alike
    monkeypatch.setattr(context_engine, "embed", lambda text, task=None: [1.0] * 768)
    for text, subject in [("booking flow must meet WCAG 2.1 AA", "accessibility"), ("p95 under 300ms", "latency"),
                          ("personal data stays in the EU", "residency"), ("hosting under $50 a month", "budget")]:
        decision(project, monkeypatch, text, subject, text, kind="constraint")
    assert service.search_context(project, "WCAG")[0]["subject"] == "accessibility"


def test_get_context_package(project, monkeypatch):
    decision(project, monkeypatch, "we use mongodb for the database", "database", "MongoDB")
    pg = decision(project, monkeypatch, "replacing mongodb with postgresql for the database", "database", "PostgreSQL")
    excerpt(project, monkeypatch, "you could index the database bookings table",
            [{"kind": "task", "subject": "indexes", "title": "Index the database bookings table", "value": "",
              "stated_by": "assistant", "user_quote": ""}])
    pkg = service.get_context(project, "add a database index")
    (d,) = pkg["decisions"]
    assert d["value"] == "PostgreSQL" and d["status"] == "approved"
    assert d["source"] == {"client": "claude", "user_quote": "replacing mongodb with postgresql for the database",
                           "excerpt_id": pg["id"]}
    assert pkg["changes"] == [{"subject": "database", "was": "MongoDB", "now": "PostgreSQL", "changed": d["since"]}]
    assert [u["title"] for u in pkg["unverified"]] == ["Index the database bookings table"]
    assert "MongoDB" not in str(pkg["decisions"])
    assert pkg["truncated"] is False


def test_get_context_lists_rejected_suggestions(project, monkeypatch):
    excerpt(project, monkeypatch, "what about a redis cache?",
            [{"kind": "decision", "subject": "cache", "title": "Redis cache", "value": "Redis", "stated_by": "assistant", "user_quote": ""}])
    (ent,) = sb.table("entities").select("id").eq("project_id", project).execute().data
    _process(project, "proposal_rejected", {"entity_id": ent["id"], "user_quote": "no"})
    pkg = service.get_context(project, "add a redis cache")
    assert [r["title"] for r in pkg["rejected"]] == ["Redis cache"]
    assert "decisions" not in pkg and "unverified" not in pkg


def test_get_context_respects_token_budget(project, monkeypatch):
    for i in range(6):
        decision(project, monkeypatch, f"database choice number {i} with a fairly long explanation", f"s{i}", f"v{i}")
    full, small = service.get_context(project, "database choice"), service.get_context(project, "database choice", max_tokens=300)
    assert len(full["decisions"]) == 6 and not full["truncated"]
    assert 0 < len(small["decisions"]) < 6 and small["truncated"]

"""Integration tests against the real Supabase DB (uses backend/.env). Each test gets a throwaway project.
Run with the dev server stopped: claim_job() is global and the server's worker would steal jobs."""
import uuid

import pytest

from app import context_engine, service
from app.config import settings
from app.db import supabase as sb


@pytest.fixture(autouse=True)
def stub_embed(monkeypatch):
    monkeypatch.setattr(context_engine, "embed", lambda text: [0.0] * 768)


@pytest.fixture
def project():
    p = sb.table("projects").insert({"owner_id": settings.KEYSTONE_OWNER_ID, "name": f"test-{uuid.uuid4().hex[:6]}"}).execute().data[0]
    yield p["id"]
    # entity_versions.event_id has no ON DELETE CASCADE, so remove entities (-> versions) before the project
    sb.table("entities").delete().eq("project_id", p["id"]).execute()
    sb.table("projects").delete().eq("id", p["id"]).execute()


def decide(pid, source, etype, value, subject="database", **extra):
    ev = service.add_event(pid, source, etype, {"title": f"Use {value}", "subject": subject, "value": value, "reason": "r", **extra})
    context_engine.process_event(ev)
    sb.table("jobs").delete().eq("event_id", ev["id"]).execute()  # processed directly; don't leave a pending job
    return ev


def entities(pid, **eq):
    q = sb.table("entities").select("*").eq("project_id", pid)
    for k, v in eq.items():
        q = q.eq(k, v)
    return q.execute().data


def test_new_decision_supersedes_old_and_keeps_history(project):
    decide(project, "manual", "decision_approved", "MongoDB")
    decide(project, "manual", "decision_approved", "PostgreSQL")
    by_value = {e["current_state"]["value"]: e for e in entities(project, type="decision")}
    assert by_value["MongoDB"]["status"] == "superseded"
    assert by_value["PostgreSQL"]["status"] == "approved"
    assert [d["current_state"]["value"] for d in service.get_decisions(project)] == ["PostgreSQL"]
    assert len(service.get_decisions(project, include_history=True)) == 2
    rel = sb.table("relationships").select("*").eq("project_id", project).execute().data
    assert len(rel) == 1 and rel[0]["relationship_type"] == "supersedes"
    assert rel[0]["source_entity_id"] == by_value["PostgreSQL"]["id"]
    assert rel[0]["target_entity_id"] == by_value["MongoDB"]["id"]


def test_ai_source_cannot_approve(project):
    decide(project, "claude", "decision_approved", "PostgreSQL")
    assert entities(project, type="decision")[0]["status"] == "proposed"


def test_manual_source_can_approve(project):
    decide(project, "manual", "decision_approved", "PostgreSQL")
    assert entities(project, type="decision")[0]["status"] == "approved"


def test_approving_same_value_versions_existing_entity(project):
    decide(project, "claude", "decision_proposed", "PostgreSQL")
    decide(project, "manual", "decision_approved", "PostgreSQL")
    ents = entities(project, type="decision")
    assert len(ents) == 1 and ents[0]["status"] == "approved"
    versions = sb.table("entity_versions").select("version,status").eq("entity_id", ents[0]["id"]).order("version").execute().data
    assert [(v["version"], v["status"]) for v in versions] == [(1, "proposed"), (2, "approved")]


def test_user_confirmed_decision_from_ai_client_is_approved_with_quote(project):
    decide(project, "chatgpt", "decision_confirmed", "PostgreSQL", user_quote="yeah we're now using postgres")
    (ent,) = entities(project, type="decision")
    assert ent["status"] == "approved"
    assert ent["current_state"]["user_quote"] == "yeah we're now using postgres"
    assert ent["current_state"]["source"] == "chatgpt"


def test_confirmed_decision_without_quote_stays_proposed(project):
    decide(project, "claude", "decision_confirmed", "PostgreSQL")
    decide(project, "claude", "decision_confirmed", "MySQL", subject="other", user_quote="   ")
    assert {e["status"] for e in entities(project, type="decision")} == {"proposed"}


def test_user_confirmed_decision_supersedes_previous(project):
    decide(project, "claude", "decision_confirmed", "MongoDB", user_quote="yeah we'll go with mongodb")
    decide(project, "chatgpt", "decision_confirmed", "PostgreSQL", user_quote="we're now using postgres")
    by_value = {e["current_state"]["value"]: e["status"] for e in entities(project, type="decision")}
    assert by_value == {"MongoDB": "superseded", "PostgreSQL": "approved"}


def test_user_confirming_ai_suggestion_versions_existing_entity(project):
    decide(project, "claude", "decision_proposed", "PostgreSQL")
    decide(project, "claude", "decision_confirmed", "PostgreSQL", user_quote="yes, do that")
    (ent,) = entities(project, type="decision")
    assert ent["status"] == "approved"


# ---- project resolution / MCP tool ----
def test_resolve_project_by_id_and_name(project):
    name = sb.table("projects").select("name").eq("id", project).single().execute().data["name"]
    assert service.resolve_project(project) == project
    assert service.resolve_project(name.upper()) == project
    with pytest.raises(ValueError):
        service.resolve_project("no-such-project-xyz")


def test_mcp_log_context_requires_user_message(project):
    from app.mcp import server
    with pytest.raises(ValueError):
        server.log_context("claude", [server.Message(role="assistant", content="use postgres")], project=project)
    out = server.log_context("claude", [server.Message(role="user", content="we're now using postgres")], project=project)
    sb.table("jobs").delete().eq("event_id", out["event_id"]).execute()
    ev = sb.table("events").select("*").eq("id", out["event_id"]).single().execute().data
    assert ev["event_type"] == "context_logged" and ev["payload"]["messages"][0]["role"] == "user"


# ---- resolution: AI proposals never displace or downgrade approved state ----
def test_proposal_does_not_supersede_approved_decision(project):
    decide(project, "manual", "decision_approved", "PostgreSQL")
    decide(project, "claude", "decision_proposed", "MySQL")
    by_value = {e["current_state"]["value"]: e["status"] for e in entities(project, type="decision")}
    assert by_value == {"PostgreSQL": "approved", "MySQL": "proposed"}


def test_restating_approved_decision_as_proposal_does_not_downgrade(project):
    decide(project, "claude", "decision_confirmed", "PostgreSQL", user_quote="we use postgres")
    decide(project, "claude", "decision_proposed", "PostgreSQL")
    (ent,) = entities(project, type="decision")
    assert ent["status"] == "approved" and ent["current_state"]["user_quote"] == "we use postgres"


def test_new_proposal_replaces_older_proposal(project):
    decide(project, "claude", "decision_proposed", "MySQL")
    decide(project, "claude", "decision_proposed", "PostgreSQL")
    by_value = {e["current_state"]["value"]: e["status"] for e in entities(project, type="decision")}
    assert by_value == {"MySQL": "superseded", "PostgreSQL": "proposed"}


# ---- extraction (LLM stubbed) ----
def drain(pid):
    """Process pending jobs for this project's events directly, in order (stands in for the worker)."""
    while True:
        ids = [e["id"] for e in sb.table("events").select("id").eq("project_id", pid).execute().data]
        jobs = sb.table("jobs").select("*").in_("event_id", ids).order("created_at").execute().data
        if not jobs:
            return
        for j in jobs:
            context_engine.process_event(sb.table("events").select("*").eq("id", j["event_id"]).single().execute().data)
            sb.table("jobs").delete().eq("id", j["id"]).execute()


def item(kind, value, stated_by, quote="", subject="database"):
    return {"kind": kind, "subject": subject, "title": f"Use {value}", "value": value, "reason": "",
            "stated_by": stated_by, "user_quote": quote}


def test_log_context_extracts_entities_with_provenance(project, monkeypatch):
    monkeypatch.setattr(context_engine.extraction, "extract", lambda msgs, known: [
        item("decision", "PostgreSQL", "user", "we're now using postgres"),
        item("constraint", "offline only", "user", "must run offline", subject="hosting"),
        item("task", "write migration", "assistant", subject="migration"),
    ])
    ctx = service.log_context(project, "claude", [
        {"role": "user", "content": "ok, we're now using postgres. it must run offline"},
        {"role": "assistant", "content": "Great. Next step: write a migration."}])
    drain(project)
    by_type = {e["type"]: e for e in entities(project)}
    assert by_type["decision"]["status"] == "approved"
    assert by_type["constraint"]["status"] == "approved"
    assert by_type["task"]["status"] == "proposed"
    derived = sb.table("events").select("*").eq("source_ref", ctx["id"]).execute().data
    assert len(derived) == 3 and {e["source"] for e in derived} == {"claude"}
    assert service.get_project_state(project)["constraints"][0]["subject"] == "hosting"


def test_paraphrased_quote_becomes_proposal(project, monkeypatch):
    monkeypatch.setattr(context_engine.extraction, "extract", lambda msgs, known: [
        item("decision", "PostgreSQL", "user", "we have decided to use PostgreSQL")])
    service.log_context(project, "chatgpt", [{"role": "user", "content": "postgres I guess"}])
    drain(project)
    (ent,) = entities(project, type="decision")
    assert ent["status"] == "proposed" and "user_quote" not in ent["current_state"]


def test_extraction_is_idempotent_on_retry(project, monkeypatch):
    monkeypatch.setattr(context_engine.extraction, "extract", lambda msgs, known: [
        item("decision", "PostgreSQL", "user", "use postgres")])
    ctx = service.log_context(project, "claude", [{"role": "user", "content": "use postgres"}])
    sb.table("jobs").delete().eq("event_id", ctx["id"]).execute()
    context_engine.process_event(ctx)
    context_engine.process_event(ctx)  # retry
    assert len(sb.table("events").select("id").eq("source_ref", ctx["id"]).execute().data) == 1
    drain(project)


# ---- review flow: proposed -> approved -> implemented ----
def review(pid, etype, entity_id, **payload):
    ev = service.review(pid, "claude", etype, entity_id, **payload)
    drain(pid)
    return ev


def test_confirm_then_implement_proposal(project):
    decide(project, "manual", "decision_approved", "MongoDB")
    decide(project, "claude", "decision_proposed", "PostgreSQL")
    (prop,) = service.list_proposals(project)
    review(project, "proposal_confirmed", prop["id"], user_quote="yes switch to postgres")
    by_value = {e["current_state"]["value"]: e for e in entities(project, type="decision")}
    assert by_value["PostgreSQL"]["status"] == "approved"
    assert by_value["PostgreSQL"]["current_state"]["user_quote"] == "yes switch to postgres"
    assert by_value["MongoDB"]["status"] == "superseded"  # confirming supersedes the old approved one
    assert service.list_proposals(project) == []

    review(project, "entity_implemented", prop["id"], evidence="PR #12 merged")
    ent = sb.table("entities").select("*").eq("id", prop["id"]).single().execute().data
    assert ent["status"] == "implemented" and ent["current_state"]["evidence"] == "PR #12 merged"
    versions = sb.table("entity_versions").select("status").eq("entity_id", prop["id"]).order("version").execute().data
    assert [v["status"] for v in versions] == ["proposed", "approved", "implemented"]


def test_reject_proposal(project):
    decide(project, "claude", "decision_proposed", "MySQL")
    (prop,) = service.list_proposals(project)
    review(project, "proposal_rejected", prop["id"])
    assert entities(project, type="decision")[0]["status"] == "rejected"
    assert service.get_decisions(project) == []


def test_review_validates_state(project):
    decide(project, "claude", "decision_proposed", "MySQL")
    (prop,) = service.list_proposals(project)
    with pytest.raises(ValueError):  # can't implement an unapproved proposal
        service.review(project, "claude", "entity_implemented", prop["id"], evidence="x")
    with pytest.raises(ValueError):
        service.review(project, "claude", "proposal_confirmed", str(uuid.uuid4()), user_quote="yes")


def test_task_review_open_then_done(project):
    service.add_event(project, "claude", "task_proposed", {"title": "Write docs"})
    drain(project)
    (task,) = service.list_proposals(project)
    review(project, "proposal_confirmed", task["id"], user_quote="yes I'll do that")
    assert [t["id"] for t in service.get_open_tasks(project)] == [task["id"]]
    review(project, "entity_implemented", task["id"], evidence="docs/README written")
    assert sb.table("entities").select("status").eq("id", task["id"]).single().execute().data["status"] == "done"


def test_tasks_are_open_and_listed(project):
    ev = service.add_event(project, "manual", "task_created", {"title": "Write docs"})
    context_engine.process_event(ev)
    sb.table("jobs").delete().eq("event_id", ev["id"]).execute()
    assert [t["title"] for t in service.get_open_tasks(project)] == ["Write docs"]


def test_events_are_append_only_source_of_truth(project):
    decide(project, "manual", "decision_approved", "MongoDB")
    decide(project, "manual", "decision_approved", "PostgreSQL")
    assert len(sb.table("events").select("id").eq("project_id", project).execute().data) == 2


# ---- job claiming ----
def make_jobs(pid, n):
    ids = []
    for i in range(n):
        ev = service.add_event(pid, "manual", "note", {"title": f"n{i}"})
        ids.append(sb.table("jobs").select("id").eq("event_id", ev["id"]).execute().data[0]["id"])
    return ids


def test_claim_job_is_exclusive_and_counts_attempts(project):
    mine = set(make_jobs(project, 2))
    a, b = sb.rpc("claim_job").execute().data[0], sb.rpc("claim_job").execute().data[0]
    assert a["id"] != b["id"] and {a["id"], b["id"]} == mine
    assert a["status"] == "processing" and a["attempts"] == 1
    assert sb.rpc("claim_job").execute().data == []  # nothing left


def test_stale_processing_job_is_recovered(project):
    (jid,) = make_jobs(project, 1)
    assert sb.rpc("claim_job").execute().data[0]["id"] == jid
    assert sb.rpc("claim_job").execute().data == []  # fresh lock: not claimable
    sb.table("jobs").update({"locked_at": "2000-01-01T00:00:00Z"}).eq("id", jid).execute()
    again = sb.rpc("claim_job").execute().data[0]
    assert again["id"] == jid and again["attempts"] == 2


def test_job_not_claimed_after_three_attempts(project):
    (jid,) = make_jobs(project, 1)
    sb.table("jobs").update({"attempts": 3}).eq("id", jid).execute()
    assert sb.rpc("claim_job").execute().data == []

"""Rebuild a project's entities from its events, re-running extraction with the current prompt/model.

    cd backend && python -m app.rebuild <project name or id> [--yes]

Entities, versions, relationships and embeddings are a projection, so they are wiped and rebuilt. Derived
events (the extractor's output for each context_logged excerpt) are deleted and regenerated; the excerpts
themselves and all other events are kept and replayed in order.

Review events (confirm/reject/implemented/dismiss) reference entity ids. To keep them valid, a rebuilt
entity reuses the id of the old entity with the same (type, subject, value). Review events whose target is
gone (e.g. the extractor now picks a different subject) or in the wrong state are skipped and reported.

Stop the dev server first: its worker would otherwise pick up jobs for this project mid-rebuild.
"""
import argparse
from collections import Counter, defaultdict

from . import service
from .context_engine import process_event
from .db import supabase as sb

REVIEW = {"proposal_confirmed", "proposal_rejected", "entity_implemented"}


def _summary(project_id):
    rows = sb.table("entities").select("type,status").eq("project_id", project_id).execute().data
    return Counter(f"{r['type']}/{r['status']}" for r in rows)


def _chunks(xs, n=100):
    return [xs[i:i + n] for i in range(0, len(xs), n)]


def rebuild(project_id: str) -> dict:
    events = (sb.table("events").select("*").eq("project_id", project_id)
              .order("occurred_at").order("created_at").execute().data)
    excerpts = {e["id"] for e in events if e["event_type"] == "context_logged"}
    derived = [e["id"] for e in events if e["source_ref"] in excerpts]
    originals = [e for e in events if e["source_ref"] not in excerpts]

    old = (sb.table("entities").select("id,type,subject,current_state").eq("project_id", project_id)
           .order("created_at").execute().data)
    pool = defaultdict(list)
    for e in old:
        pool[(e["type"], e["subject"], e["current_state"].get("value"))].append(e["id"])

    def reuse_id(kind, subject, value):
        ids = pool.get((kind, subject, value))
        return ids.pop(0) if ids else None

    before = _summary(project_id)

    # Wipe the projection. Entities first: entity_versions.event_id has no ON DELETE CASCADE.
    for ids in _chunks([e["id"] for e in events]):
        sb.table("jobs").delete().in_("event_id", ids).neq("status", "completed").execute()
    sb.table("entities").delete().eq("project_id", project_id).execute()  # -> versions, relationships
    sb.table("embeddings").delete().eq("project_id", project_id).execute()
    for ids in _chunks(derived):
        sb.table("events").delete().in_("id", ids).execute()

    skipped = []
    for ev in originals:
        p = ev["payload"]
        if ev["event_type"] in REVIEW:
            rows = sb.table("entities").select("status").eq("id", p["entity_id"]).execute().data
            if not rows or rows[0]["status"] not in service.REVIEW_FROM[ev["event_type"]]:
                skipped.append((ev["event_type"], ev["id"], rows[0]["status"] if rows else "missing"))
                continue
        for d in process_event(ev, queue=False, reuse_id=reuse_id) or []:
            process_event(d, queue=False, reuse_id=reuse_id)

    return {"before": before, "after": _summary(project_id), "excerpts": len(excerpts),
            "derived_before": len(derived), "skipped": skipped}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args()
    pid = service.resolve_project(args.project)
    if not args.yes and input(f"Rebuild project {args.project} ({pid}) from its events? [y/N] ").lower() != "y":
        return
    r = rebuild(pid)
    print(f"re-extracted {r['excerpts']} excerpts (previously {r['derived_before']} derived events)")
    for k in sorted(set(r["before"]) | set(r["after"])):
        print(f"  {k:<24}{r['before'][k]:>4} -> {r['after'][k]}")
    for etype, ev_id, status in r["skipped"]:
        print(f"  skipped {etype} {ev_id}: target is {status}")


if __name__ == "__main__":
    main()

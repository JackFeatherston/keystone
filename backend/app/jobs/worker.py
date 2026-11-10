import asyncio
import logging

from ..context_engine import process_event
from ..db import supabase as sb

log = logging.getLogger("keystone.worker")


def _run_one() -> bool:
    jobs = sb.rpc("claim_job").execute().data
    if not jobs:
        return False
    job = jobs[0]
    try:
        event = sb.table("events").select("*").eq("id", job["event_id"]).single().execute().data
        process_event(event)
        sb.table("jobs").update({"status": "completed", "completed_at": "now()"}).eq("id", job["id"]).execute()
    except Exception as e:  # retried until attempts >= 3
        log.exception("job %s failed", job["id"])
        sb.table("jobs").update({"status": "pending" if job["attempts"] < 3 else "failed",
                                 "error": str(e)}).eq("id", job["id"]).execute()
    return True


async def worker_loop():
    while True:
        did_work = await asyncio.to_thread(_run_one)
        if not did_work:
            await asyncio.sleep(2)

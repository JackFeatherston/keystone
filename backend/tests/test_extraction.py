"""Verbatim-quote guard: pure unit tests, no DB or LLM."""
from app.extraction import quote_is_verbatim, to_events

MSGS = [
    {"role": "assistant", "content": "I'd recommend PostgreSQL."},
    {"role": "user", "content": "Yeah,   we’re now using\nPostgres."},
]


def item(stated_by, quote, kind="decision", subject="database"):
    return {"kind": kind, "subject": subject, "title": "Use PostgreSQL", "value": "PostgreSQL",
            "reason": "", "stated_by": stated_by, "user_quote": quote}


def test_verbatim_quote_tolerates_case_whitespace_and_smart_quotes():
    assert quote_is_verbatim("we're now using postgres", MSGS)


def test_paraphrased_quote_is_rejected():
    assert not quote_is_verbatim("we decided on PostgreSQL", MSGS)


def test_quote_from_assistant_message_is_rejected():
    assert not quote_is_verbatim("I'd recommend PostgreSQL", MSGS)


def test_empty_quote_is_rejected():
    assert not quote_is_verbatim("  ", MSGS)


def test_to_events_maps_guarded_items():
    events = to_events([
        item("user", "we’re now using postgres"),
        item("user", "we picked postgres", subject="db2"),  # paraphrase -> proposal
        item("assistant", "", subject="db3"),
        item("user", "now using", kind="task"),
        item("assistant", "", kind="constraint"),
    ], MSGS)
    assert [e for e, _ in events] == ["decision_confirmed", "decision_proposed", "decision_proposed",
                                      "task_created", "constraint_proposed"]
    assert "user_quote" in events[0][1] and "user_quote" not in events[1][1]


def test_to_events_keeps_one_item_per_subject():
    # "Drop OpenAI, we'll use Ollama" came out as two decisions on one subject; the second superseded the first.
    first, second = item("user", "we’re now using"), {**item("user", "now using"), "value": "not MongoDB"}
    events = to_events([first, second, item("user", "now using", kind="task")], MSGS)
    assert [(e, p["value"]) for e, p in events] == [("decision_confirmed", "PostgreSQL"), ("task_created", "PostgreSQL")]

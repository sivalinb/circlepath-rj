import json
from datetime import timedelta

import pytest
from icalendar import Calendar
from prometheus_client import generate_latest

from circlepath.calendar import calendar_bytes, export_summary
from circlepath.models import DomainError
from circlepath.store import stamp, utcnow


def test_calendar_stable_ids_and_cancellation_on_role_release(demo):
    service, _, _, _, cases, _ = demo
    start = utcnow() + timedelta(days=5)
    event_id = service.schedule("avery", cases[0], "Case review", stamp(start),
                                stamp(start + timedelta(hours=1)), "Demo office", True)
    parsed = Calendar.from_ical(calendar_bytes(service, "sam"))
    event = next(e for e in parsed.walk("VEVENT") if str(e["UID"]).startswith(event_id))
    assert "Cedar" not in str(event["SUMMARY"])
    assert "Youth" not in str(event["DESCRIPTION"])
    service.schedule("avery", cases[0], "Case review", stamp(start + timedelta(days=1)),
                     stamp(start + timedelta(days=1, hours=1)), "Demo office", True, event_id)
    events = Calendar.from_ical(calendar_bytes(service, "sam")).walk("VEVENT")
    updated = next(e for e in events if str(e["UID"]).startswith(event_id))
    assert updated["SEQUENCE"] == 1
    role = service.store.one("SELECT id FROM roles WHERE case_id=? AND username='sam'", (cases[0],))
    service.release_role("sam", role["id"])
    events = Calendar.from_ical(calendar_bytes(service, "sam")).walk("VEVENT")
    cancelled = next(e for e in events if str(e["UID"]).startswith(event_id))
    assert str(cancelled["STATUS"]) == "CANCELLED"
    with pytest.raises(DomainError):
        service.case("sam", cases[0])


def test_telemetry_has_no_narratives_keys_or_private_exception_messages(demo):
    service, agent, _, settings, cases, _ = demo
    agent.run("manager", cases[0], "Preparation plan")
    with pytest.raises(ValueError), service.telemetry.operation("test.failure", {
        "input.value": "SECRET_MARKER", "case.id": "UNTRUSTED_CASE_MARKER"
    }):
        raise ValueError("PRIVATE_EXCEPTION_MARKER")
    traces = (settings.data_dir / "traces.jsonl").read_text()
    assert "SECRET_MARKER" not in traces
    assert "UNTRUSTED_CASE_MARKER" not in traces
    assert "PRIVATE_EXCEPTION_MARKER" not in traces
    assert "garden gate" not in traces
    assert "manager" not in traces
    rows = [json.loads(line) for line in traces.splitlines()]
    assert any(r["attributes"].get("openinference.span.kind") == "LLM" for r in rows)
    metrics = generate_latest(service.telemetry.registry).decode()
    assert "circlepath_ai_requests_total" in metrics
    assert "circlepath_ai_latency_seconds" in metrics
    assert "circlepath_cases" in metrics


def test_unreviewed_summary_cannot_be_exported(demo):
    service, agent, _, settings, cases, _ = demo
    draft_id = agent.run("manager", cases[0], "Case summary")
    with pytest.raises(DomainError, match="human-approved"):
        export_summary(service, settings, "manager", draft_id)
    row = service.drafts("manager", cases[0])[0]
    service.review_draft("manager", draft_id, True, row["body"], "Reviewed for internal use.")
    path = export_summary(service, settings, "manager", draft_id)
    assert path.endswith(".txt")


def test_shared_availability_and_conflict_detection(demo):
    service, _, _, _, cases, _ = demo
    slots = service.suggest_slots("avery", cases[0])
    assert slots
    event = service.schedule("avery", cases[0], "Case review", slots[0]["start"], slots[0]["end"], "Demo office", True)
    with pytest.raises(DomainError, match="already has"):
        service.schedule("avery", cases[0], "Case review", slots[0]["start"], slots[0]["end"], "Demo office", True)
    assert event

from datetime import timedelta

import pytest

from circlepath.models import AIDraft, DomainError, Stage
from circlepath.store import stamp, utcnow


def test_pending_volunteer_cannot_read_case_or_run_ai(demo):
    service, agent, _, _, cases, credentials = demo
    assert service.store.authenticate("manager", credentials["manager"])
    assert not service.store.authenticate("manager", "wrong-password")
    assert not service.store.authenticate("nobody", "wrong-password")
    with pytest.raises(DomainError, match="approved"):
        service.case("riley", cases[0])
    with pytest.raises(DomainError):
        agent.run("riley", cases[0], "Preparation plan")
    assert "briefing" not in service.opportunities("riley")[0]


def test_role_capacity_and_training(demo):
    service, _, _, _, cases, _ = demo
    with pytest.raises(DomainError, match="training"):
        service.claim_role("riley", cases[1], "Facilitator")
    request = service.store.one("SELECT id FROM roles WHERE username='riley' AND case_id=?", (cases[0],))
    with pytest.raises(DomainError, match="manager"):
        service.review_role("avery", request["id"], True)
    service.review_role("manager", request["id"], True)
    assert service.case("riley", cases[0])["report"] == ""
    service.add_user("manager", "extra", "Demo extra", "long-test-password", ["Community member"], True)
    request2 = service.claim_role("extra", cases[0], "Community member")
    with pytest.raises(DomainError, match="already filled"):
        service.review_role("manager", request2, True)


def test_consent_withdrawal_pauses_work_and_cancels_events(demo):
    service, agent, worker, _, cases, _ = demo
    cid = cases[0]
    start = stamp(utcnow() + timedelta(days=3))
    event = service.schedule("avery", cid, "Case review", start,
                             stamp(utcnow() + timedelta(days=3, hours=1)), "Demo office", True)
    service.record_consent("avery", cid, "Youth", "Withdrawn")
    assert service.case("manager", cid)["stage"] == Stage.PAUSED
    assert service.store.one("SELECT status FROM events WHERE id=?", (event,))["status"] == "Cancelled"
    with pytest.raises(DomainError, match="paused"):
        agent.run("avery", cid, "Preparation plan")
    with pytest.raises(DomainError):
        service.resume("manager", cid)
    before = service.store.one("SELECT COUNT(*) n FROM notifications WHERE case_id=?", (cid,))["n"]
    worker.tick()
    assert service.store.one("SELECT COUNT(*) n FROM notifications WHERE case_id=?", (cid,))["n"] == before
    service.record_consent("manager", cid, "Youth", "Active")
    service.resume("manager", cid)
    assert service.case("manager", cid)["readiness"] == 0


def test_ai_requires_review_and_cannot_apply_twice(demo):
    service, agent, _, _, cases, _ = demo
    cid = cases[0]
    count = len(service.tasks("manager", cid))
    did = agent.run("jordan", cid, "Preparation plan")
    assert len(service.tasks("manager", cid)) == count
    row = service.drafts("manager", cid)[0]
    with pytest.raises(DomainError):
        service.review_draft("casey", did, True, row["body"], "")
    result = service.review_draft("avery", did, True, row["body"], "Reviewed for program fit.")
    assert len(result["created_task_ids"]) == 1
    with pytest.raises(DomainError, match="already"):
        service.review_draft("manager", did, True, row["body"], "")
    assert len(service.tasks("manager", cid)) == count + 1


def test_invalid_sources_owners_and_stale_drafts_are_blocked(demo):
    service, agent, _, _, cases, _ = demo
    cid = cases[0]
    did = agent.run("manager", cid, "Preparation plan")
    row = service.drafts("manager", cid)[0]
    body = AIDraft.model_validate_json(row["body"])
    body.actions[0].owner = "riley"
    with pytest.raises(DomainError, match="authorized owner"):
        service.review_draft("manager", did, True, body.model_dump_json(), "")
    body = AIDraft.model_validate_json(row["body"])
    body.facts[0].source_ids = ["invented-reference"]
    with pytest.raises(DomainError, match="unknown source"):
        service.review_draft("manager", did, True, body.model_dump_json(), "")
    service.advance("avery", cid)
    with pytest.raises(DomainError, match="stage has changed"):
        service.review_draft("manager", did, True, row["body"], "")


def test_raw_reports_and_private_notes_never_enter_model_context(demo):
    service, _, _, _, cases, _ = demo
    service.debrief("sam", cases[0], "PRIVATE_MARKER", "", "", private=True)
    context = service.ai_snapshot("manager", cases[0], "Preparation plan")
    text = str(context)
    assert "PRIVATE_MARKER" not in text
    assert "FICTIONAL REPORT FOR DEMONSTRATION" not in text
    assert any(note["happened"] == "PRIVATE_MARKER" for note in service.notes("manager", cases[0]))
    assert not any(note["happened"] == "PRIVATE_MARKER" for note in service.notes("casey", cases[0]))


def test_repair_requires_agreement_evidence_and_another_reviewer(demo):
    service, _, _, _, cases, _ = demo
    cid = cases[2]
    with pytest.raises(DomainError, match="Confirm participant"):
        service.add_repair("avery", cid, "Demo repair", "sam", stamp(), "Review confirmation", False)
    repair = service.repairs("manager", cid)[0]
    with pytest.raises(DomainError):
        service.update_repair("casey", repair["id"], "Submitted", "done")
    with pytest.raises(DomainError):
        service.update_repair("sam", repair["id"], "Submitted", "")
    service.update_repair("sam", repair["id"], "Submitted", "Workshop organizer confirmation received.")
    service.update_repair("avery", repair["id"], "Verified", "Reviewed the organizer's confirmation.")
    assert service.repairs("manager", cid)[0]["verified_by"] == "avery"


def test_complete_case_lifecycle(demo):
    service, agent, worker, _, cases, _ = demo
    cid = cases[0]
    assert service.advance("avery", cid) == Stage.PRECONFERENCE
    with pytest.raises(DomainError, match="preparation meetings"):
        service.advance("avery", cid)
    start = utcnow() + timedelta(days=4)
    preparation = service.schedule("avery", cid, "Pre-conference", stamp(start),
                                    stamp(start + timedelta(hours=1)), "Demo office", True)
    service.finish_event("avery", preparation)
    assert service.advance("avery", cid) == Stage.CONFERENCE
    conference = service.schedule("avery", cid, "Conference", stamp(start + timedelta(days=1)),
                                   stamp(start + timedelta(days=1, hours=1)), "Demo office", True)
    service.finish_event("avery", conference)
    service.debrief("jordan", cid, "The prepared conference took place.", "A workshop was agreed.", "")
    repair = service.add_repair("avery", cid, "Participant-agreed workshop", "sam",
                                stamp(start + timedelta(days=10)), "Organizer confirms attendance", True)
    assert service.advance("avery", cid) == Stage.REPAIR
    with pytest.raises(DomainError, match="outstanding"):
        service.advance("avery", cid)
    service.update_repair("sam", repair, "Submitted", "Organizer supplied confirmation.")
    service.update_repair("avery", repair, "Verified", "Facilitator reviewed the confirmation.")
    assert service.advance("avery", cid) == Stage.REINTEGRATION
    circle = service.schedule("avery", cid, "Reintegration", stamp(start + timedelta(days=12)),
                               stamp(start + timedelta(days=12, hours=1)), "Demo office", True)
    service.finish_event("avery", circle)
    assert service.advance("avery", cid) == Stage.CLOSED
    did = agent.run("manager", cid, "Case summary")
    row = service.drafts("manager", cid)[0]
    assert service.review_draft("manager", did, True, row["body"], "Reviewed.")["created_task_ids"] == []
    count = len(service.notifications("sam"))
    worker.tick()
    worker.tick()
    assert len(service.notifications("sam")) >= count
    assert len(service.audit("manager", cid)) > 15


def test_reminders_are_idempotent_and_do_not_send_email(demo):
    service, _, worker, _, _, _ = demo
    first = worker.tick()
    assert first > 0
    assert worker.tick() == 0
    assert worker.deliver_email() == 0

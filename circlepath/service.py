"""Authorization and restorative workflow rules live here, not in the UI or model."""

import json
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from .models import AIDraft, DomainError, MINIMUM_TEAM, ROLE_SLOTS, Stage
from .store import Store, hash_password, stamp, uid, utcnow
from .telemetry import Telemetry


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("America/Denver"))
        return parsed
    except (ValueError, AttributeError) as exc:
        raise DomainError("Use an ISO date/time, such as 2026-10-20T16:00-06:00.") from exc


def traced(fn):
    @wraps(fn)
    def run(self, *args, **kwargs):
        with self.telemetry.operation("workflow." + fn.__name__):
            return fn(self, *args, **kwargs)
    return run


class Service:
    def __init__(self, store: Store, telemetry: Telemetry):
        self.store = store
        self.telemetry = telemetry
        telemetry.gauges(store)

    def actor(self, username: str) -> dict:
        user = self.store.user(username)
        if not user:
            raise DomainError("Sign in with an active CirclePath account.")
        return user

    def manager(self, username: str) -> dict:
        user = self.actor(username)
        if not user["is_manager"]:
            raise DomainError("A program manager must perform this action.")
        return user

    def case(self, actor: str, case_id: str, *, lead: bool = False) -> dict:
        user = self.actor(actor)
        case = self.store.one("SELECT * FROM cases WHERE id=?", (case_id,))
        if not case:
            raise DomainError("Select an available case.")
        if user["is_manager"]:
            return case
        role = self.store.one(
            "SELECT * FROM roles WHERE case_id=? AND username=? AND status='Approved'",
            (case_id, actor),
        )
        if not role or not user["confidentiality"] or role["role"] not in user["trained_roles"]:
            raise DomainError("Case details are available to the approved, qualified team only.")
        if lead and role["role"] not in {"Facilitator", "Co-facilitator"}:
            raise DomainError("The facilitator, co-facilitator, or manager must review this action.")
        if case["report_policy"] == "office-only":
            case["report"] = ""
        return case

    def active(self, actor: str, case_id: str, *, lead: bool = False) -> dict:
        case = self.case(actor, case_id, lead=lead)
        if case["stage"] in {Stage.CLOSED, Stage.PAUSED}:
            raise DomainError("This case is closed or paused. Ask the manager to review next steps.")
        return case

    def owners(self, case_id: str) -> set[str]:
        team = self.store.rows(
            "SELECT r.username,r.role,u.trained_roles FROM roles r JOIN users u ON u.username=r.username "
            "WHERE r.case_id=? AND r.status='Approved' AND u.confidentiality=1", (case_id,)
        )
        managers = self.store.rows("SELECT username FROM users WHERE is_manager=1")
        return {row["username"] for row in managers} | {
            row["username"] for row in team if row["role"] in json.loads(row["trained_roles"])
        }

    def _owner(self, case_id: str, owner: str):
        if owner not in self.owners(case_id):
            raise DomainError("Choose an approved team member or program manager as owner.")

    def opportunities(self, actor: str) -> list[dict]:
        self.actor(actor)
        # Opportunity metadata contains neither narrative nor participant information.
        cases = self.store.rows("SELECT id,alias,stage FROM cases ORDER BY created_at DESC")
        for case in cases:
            roles = self.store.rows(
                "SELECT role,COUNT(*) n FROM roles WHERE case_id=? AND status='Approved' "
                "GROUP BY role", (case["id"],)
            )
            counts = {r["role"]: r["n"] for r in roles}
            case["open_roles"] = ", ".join(
                f"{role} ({capacity - counts.get(role, 0)})"
                for role, capacity in ROLE_SLOTS.items() if counts.get(role, 0) < capacity
            ) if case["stage"] not in {Stage.CLOSED, Stage.PAUSED} else "—"
        return cases

    def team(self, actor: str, case_id: str) -> list[dict]:
        user = self.actor(actor)
        if not user["is_manager"]:
            self.case(actor, case_id)
        rows = self.store.rows(
            "SELECT r.*,u.display_name FROM roles r JOIN users u ON u.username=r.username "
            "WHERE case_id=? ORDER BY r.role", (case_id,)
        )
        if not user["is_manager"]:
            for row in rows:
                row.pop("conflict_note", None)
        return rows

    @traced
    def create_case(self, actor: str, alias: str, briefing: str, report: str = "",
                    report_policy: str = "office-only", ai_allowed: bool = False,
                    is_synthetic: bool = True, guardian_required: bool = False,
                    support_needs: str = "") -> str:
        self.manager(actor)
        if not alias.strip() or len(alias) > 60 or not briefing.strip() or len(briefing) > 20000:
            raise DomainError("Use an operational case alias and a briefing of 1–20,000 characters.")
        if report_policy not in {"office-only", "secure-team"} or len(report) > 100000:
            raise DomainError("Choose a report policy and keep the report under 100,000 characters.")
        case_id = uid()
        now = stamp()
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO cases (id,alias,briefing,report,report_policy,stage,ai_allowed,"
                "is_synthetic,guardian_required,support_needs,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (case_id, alias.strip(), briefing.strip(), report, report_policy, Stage.REVIEW,
                 int(ai_allowed), int(is_synthetic), int(guardian_required), support_needs, now, now),
            )
            for party in ["Youth", "Harmed party", "Guardian"]:
                db.execute("INSERT INTO consents VALUES (?,?,?,?,?)",
                           (case_id, party, "Pending", actor, now))
            Store.audit(db, case_id, actor, "case.created", case_id)
        return case_id

    @traced
    def claim_role(self, actor: str, case_id: str, role: str, conflict_note: str = "") -> str:
        user = self.actor(actor)
        case = self.store.one("SELECT stage FROM cases WHERE id=?", (case_id,))
        if not case or case["stage"] in {Stage.CLOSED, Stage.PAUSED}:
            raise DomainError("Choose an active case opportunity.")
        if role not in ROLE_SLOTS or role not in user["trained_roles"] or not user["confidentiality"]:
            raise DomainError("This role requires recorded training and a confidentiality agreement.")
        role_id = uid()
        with self.store.transaction() as db:
            existing = db.execute(
                "SELECT id FROM roles WHERE case_id=? AND username=? AND status!='Released'",
                (case_id, actor),
            ).fetchone()
            if existing:
                raise DomainError("You already have a role request or assignment on this case.")
            db.execute("DELETE FROM roles WHERE case_id=? AND username=? AND status='Released'",
                       (case_id, actor))
            db.execute("INSERT INTO roles (id,case_id,username,role,status,conflict_note) "
                       "VALUES (?,?,?,?,?,?)", (role_id, case_id, actor, role, "Pending", conflict_note))
            Store.audit(db, case_id, actor, "role.requested", role_id)
            for manager in db.execute("SELECT username FROM users WHERE is_manager=1"):
                Store.notify(db, case_id, manager[0], "Role request",
                             "A volunteer role request needs review.", f"role:{role_id}")
        return role_id

    @traced
    def review_role(self, actor: str, role_id: str, approve: bool):
        self.manager(actor)
        with self.store.transaction() as db:
            role = db.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone()
            if not role or role["status"] != "Pending":
                raise DomainError("Choose a pending role request.")
            case = db.execute("SELECT stage FROM cases WHERE id=?", (role["case_id"],)).fetchone()
            if case[0] in {Stage.CLOSED, Stage.PAUSED}:
                raise DomainError("Role approvals require an active case.")
            if approve:
                user = db.execute("SELECT * FROM users WHERE username=?", (role["username"],)).fetchone()
                if not user["confidentiality"] or role["role"] not in json.loads(user["trained_roles"]):
                    raise DomainError("Training and confidentiality must be current.")
                count = db.execute(
                    "SELECT COUNT(*) FROM roles WHERE case_id=? AND role=? AND status='Approved'",
                    (role["case_id"], role["role"]),
                ).fetchone()[0]
                if count >= ROLE_SLOTS[role["role"]]:
                    raise DomainError("This role is already filled; review the team before assigning.")
            db.execute("UPDATE roles SET status=? WHERE id=?",
                       ("Approved" if approve else "Released", role_id))
            if approve:
                db.execute("UPDATE cases SET readiness=0,updated_at=? WHERE id=?",
                           (stamp(), role["case_id"]))
                db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                           (uid(), role["case_id"], "Complete the case review for your role",
                            role["username"], stamp(utcnow() + timedelta(days=2)), "Open",
                            "Case review", stamp()))
            Store.audit(db, role["case_id"], actor, "role.approved" if approve else "role.rejected", role_id)
            Store.notify(db, role["case_id"], role["username"], "Role decision",
                         "Your role request has been reviewed. Open CirclePath for the result.",
                         f"role-decision:{role_id}")

    @traced
    def release_role(self, actor: str, role_id: str):
        user = self.actor(actor)
        with self.store.transaction() as db:
            role = db.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone()
            if not role or (role["username"] != actor and not user["is_manager"]):
                raise DomainError("Only the assigned volunteer or manager can release this role.")
            db.execute("UPDATE roles SET status='Released' WHERE id=?", (role_id,))
            db.execute(
                "UPDATE event_members SET status='Cancelled' WHERE username=? AND event_id IN "
                "(SELECT id FROM events WHERE case_id=? AND status='Scheduled')",
                (role["username"], role["case_id"]),
            )
            db.execute("UPDATE events SET sequence=sequence+1,updated_at=? WHERE case_id=? "
                       "AND status='Scheduled'", (stamp(), role["case_id"]))
            db.execute("UPDATE cases SET readiness=0,updated_at=? WHERE id=?",
                       (stamp(), role["case_id"]))
            Store.audit(db, role["case_id"], actor, "role.released", role_id)

    @traced
    def mark_reviewed(self, actor: str, case_id: str):
        self.case(actor, case_id)
        with self.store.transaction() as db:
            count = db.execute("UPDATE roles SET reviewed=1 WHERE case_id=? AND username=? "
                               "AND status='Approved'", (case_id, actor)).rowcount
            if not count:
                raise DomainError("Only an assigned volunteer can attest to their own case review.")
            Store.audit(db, case_id, actor, "case.reviewed")
            db.execute("UPDATE tasks SET status='Completed' WHERE case_id=? AND owner=? "
                       "AND origin='Case review'", (case_id, actor))

    def consents(self, actor: str, case_id: str) -> list[dict]:
        self.case(actor, case_id)
        return self.store.rows("SELECT party,status,updated_at FROM consents WHERE case_id=?", (case_id,))

    @staticmethod
    def _consented(db, case_id: str):
        case = db.execute("SELECT guardian_required FROM cases WHERE id=?", (case_id,)).fetchone()
        required = {"Youth", "Harmed party"}
        if case[0]:
            required.add("Guardian")
        consents = {r[0]: r[1] for r in db.execute(
            "SELECT party,status FROM consents WHERE case_id=?", (case_id,)
        )}
        if any(consents.get(party) != "Active" for party in required):
            raise DomainError("Record the required participants' current choice to continue first.")

    @traced
    def record_consent(self, actor: str, case_id: str, party: str, status: str):
        self.active(actor, case_id, lead=True) if status == "Pending" else self.case(actor, case_id, lead=True)
        if party not in {"Youth", "Harmed party", "Guardian"} or status not in {
            "Active", "Pending", "Declined", "Withdrawn"
        }:
            raise DomainError("Choose a participant role and consent status.")
        with self.store.transaction() as db:
            stage = db.execute("SELECT stage FROM cases WHERE id=?", (case_id,)).fetchone()[0]
            if stage == Stage.CLOSED:
                raise DomainError("Consent records on a closed case cannot be changed.")
            db.execute("UPDATE consents SET status=?,recorded_by=?,updated_at=? "
                       "WHERE case_id=? AND party=?", (status, actor, stamp(), case_id, party))
            guardian_required = db.execute("SELECT guardian_required FROM cases WHERE id=?", (case_id,)).fetchone()[0]
            if status in {"Declined", "Withdrawn", "Pending"} and (party != "Guardian" or guardian_required):
                if stage != Stage.PAUSED:
                    db.execute("UPDATE cases SET prior_stage=stage,stage=?,readiness=0,updated_at=? "
                               "WHERE id=?", (Stage.PAUSED, stamp(), case_id))
                db.execute("UPDATE events SET status='Cancelled',sequence=sequence+1,updated_at=? "
                           "WHERE case_id=? AND status='Scheduled'", (stamp(), case_id))
            Store.audit(db, case_id, actor, "consent.recorded")

    @traced
    def set_readiness(self, actor: str, case_id: str, ready: bool):
        self.active(actor, case_id, lead=True)
        with self.store.transaction() as db:
            if ready:
                self._consented(db, case_id)
            db.execute("UPDATE cases SET readiness=?,updated_at=? WHERE id=?",
                       (int(ready), stamp(), case_id))
            Store.audit(db, case_id, actor, "readiness.recorded")

    @staticmethod
    def _staffed(db, case_id: str):
        counts = {}
        for row in db.execute(
            "SELECT r.role,u.trained_roles,u.confidentiality,r.reviewed FROM roles r JOIN users u ON u.username=r.username "
            "WHERE r.case_id=? AND r.status='Approved'", (case_id,)
        ):
            if not row[2] or row[0] not in json.loads(row[1]) or not row[3]:
                raise DomainError("Have every approved team member review the case and keep training and confidentiality current.")
            counts[row[0]] = counts.get(row[0], 0) + 1
        if any(counts.get(role, 0) < minimum for role, minimum in MINIMUM_TEAM.items()):
            raise DomainError("Fill the required team and have each member complete their case review.")

    @traced
    def advance(self, actor: str, case_id: str):
        self.active(actor, case_id, lead=True)
        stages = [Stage.REVIEW, Stage.PRECONFERENCE, Stage.CONFERENCE, Stage.REPAIR,
                  Stage.REINTEGRATION, Stage.CLOSED]
        with self.store.transaction() as db:
            case = dict(db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())
            self._consented(db, case_id)
            self._staffed(db, case_id)
            current = case["stage"]
            if current == Stage.PRECONFERENCE:
                if not case["readiness"]:
                    raise DomainError("The facilitator must confirm participant readiness.")
                if not db.execute("SELECT id FROM events WHERE case_id=? AND kind='Pre-conference' "
                                  "AND status='Completed'", (case_id,)).fetchone():
                    raise DomainError("Complete the preparation meetings before the conference stage.")
            if current == Stage.CONFERENCE and not db.execute(
                "SELECT id FROM events WHERE case_id=? AND kind='Conference' AND status='Completed'",
                (case_id,),
            ).fetchone():
                raise DomainError("Record that the conference took place before moving to repair.")
            if current == Stage.REPAIR:
                if db.execute("SELECT id FROM repairs WHERE case_id=? AND status!='Verified'",
                              (case_id,)).fetchone():
                    raise DomainError("Review outstanding repair commitments before reintegration.")
            if current == Stage.REINTEGRATION and not db.execute(
                "SELECT id FROM events WHERE case_id=? AND kind='Reintegration' AND status='Completed'",
                (case_id,),
            ).fetchone():
                raise DomainError("Complete the chosen reintegration process before closing.")
            next_stage = stages[stages.index(current) + 1]
            db.execute("UPDATE cases SET stage=?,updated_at=? WHERE id=?", (next_stage, stamp(), case_id))
            Store.audit(db, case_id, actor, "stage.advanced")
            for owner in self.owners(case_id):
                Store.notify(db, case_id, owner, "Stage changed",
                             "The case has moved to its next stage. Review your next steps.",
                             f"stage:{case_id}:{next_stage}")
        return next_stage

    @traced
    def pause(self, actor: str, case_id: str):
        self.active(actor, case_id, lead=True)
        with self.store.transaction() as db:
            db.execute("UPDATE cases SET prior_stage=stage,stage=?,readiness=0,updated_at=? WHERE id=?",
                       (Stage.PAUSED, stamp(), case_id))
            db.execute("UPDATE events SET status='Cancelled',sequence=sequence+1,updated_at=? "
                       "WHERE case_id=? AND status='Scheduled'", (stamp(), case_id))
            Store.audit(db, case_id, actor, "case.paused")

    @traced
    def resume(self, actor: str, case_id: str):
        self.manager(actor)
        with self.store.transaction() as db:
            case = db.execute("SELECT stage,prior_stage FROM cases WHERE id=?", (case_id,)).fetchone()
            if not case or case[0] != Stage.PAUSED or not case[1]:
                raise DomainError("Choose a paused case with a previous stage.")
            self._consented(db, case_id)
            db.execute("UPDATE cases SET stage=prior_stage,prior_stage=NULL,updated_at=? WHERE id=?",
                       (stamp(), case_id))
            Store.audit(db, case_id, actor, "case.resumed")

    @traced
    def close_alternative(self, actor: str, case_id: str, reason: str):
        self.manager(actor)
        self.case(actor, case_id)
        if len(reason.strip()) < 10:
            raise DomainError("Document the alternative closure outcome and remaining support needs.")
        with self.store.transaction() as db:
            db.execute("UPDATE cases SET stage=?,closure_reason=?,updated_at=? WHERE id=?",
                       (Stage.CLOSED, reason.strip(), stamp(), case_id))
            db.execute("UPDATE events SET status='Cancelled',sequence=sequence+1,updated_at=? "
                       "WHERE case_id=? AND status='Scheduled'", (stamp(), case_id))
            db.execute("UPDATE tasks SET status='Cancelled' WHERE case_id=? AND status='Open'", (case_id,))
            Store.audit(db, case_id, actor, "case.alternative_closure")

    @traced
    def add_task(self, actor: str, case_id: str, title: str, owner: str, due: str,
                 origin: str = "Human") -> str:
        self.active(actor, case_id, lead=True)
        self._owner(case_id, owner)
        if not title.strip() or len(title) > 240:
            raise DomainError("Use a task title of 1–240 characters.")
        task_id = uid()
        with self.store.transaction() as db:
            db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                       (task_id, case_id, title.strip(), owner, stamp(parse_time(due)), "Open", origin, stamp()))
            Store.audit(db, case_id, actor, "task.created", task_id)
            Store.notify(db, case_id, owner, "New task", "You have a new task. Open CirclePath for details.",
                         f"task:{task_id}")
        return task_id

    def tasks(self, actor: str, case_id: str | None = None) -> list[dict]:
        user = self.actor(actor)
        if case_id:
            self.case(actor, case_id)
            return self.store.rows("SELECT * FROM tasks WHERE case_id=? ORDER BY due_at", (case_id,))
        if user["is_manager"]:
            return self.store.rows("SELECT * FROM tasks ORDER BY due_at")
        return self.store.rows(
            "SELECT t.* FROM tasks t JOIN roles r ON r.case_id=t.case_id AND r.username=t.owner "
            "WHERE t.owner=? AND r.status='Approved' ORDER BY t.due_at", (actor,)
        )

    @traced
    def complete_task(self, actor: str, task_id: str):
        user = self.actor(actor)
        task = self.store.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task or (task["owner"] != actor and not user["is_manager"]):
            raise DomainError("Only the task owner or manager can complete this task.")
        self.case(actor, task["case_id"])
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='Completed' WHERE id=? AND status='Open'", (task_id,))
            Store.audit(db, task["case_id"], actor, "task.completed", task_id)

    @traced
    def debrief(self, actor: str, case_id: str, happened: str, agreed: str, barriers: str,
                private: bool = False) -> str:
        case = self.case(actor, case_id)
        if not happened.strip() or max(map(len, [happened, agreed, barriers])) > 5000:
            raise DomainError("Describe what happened; each debrief field allows up to 5,000 characters.")
        note_id = uid()
        with self.store.transaction() as db:
            db.execute("INSERT INTO notes VALUES (?,?,?,?,?,?,?,?,?)",
                       (note_id, case_id, actor, case["stage"], happened, agreed, barriers, int(private), stamp()))
            Store.audit(db, case_id, actor, "debrief.submitted", note_id)
        return note_id

    def notes(self, actor: str, case_id: str) -> list[dict]:
        user = self.actor(actor)
        self.case(actor, case_id)
        return self.store.rows(
            "SELECT * FROM notes WHERE case_id=? AND (private=0 OR author=? OR ?=1) ORDER BY created_at",
            (case_id, actor, user["is_manager"]),
        )

    @traced
    def add_repair(self, actor: str, case_id: str, commitment: str, owner: str, due: str,
                   verification: str, confirmed: bool) -> str:
        case = self.active(actor, case_id, lead=True)
        if case["stage"] not in {Stage.CONFERENCE, Stage.REPAIR}:
            raise DomainError("Record repair commitments after participants have agreed during the conference.")
        self._owner(case_id, owner)
        if not confirmed or not commitment.strip() or not verification.strip():
            raise DomainError("Confirm participant agreement and record how completion will be verified.")
        repair_id = uid()
        with self.store.transaction() as db:
            db.execute("INSERT INTO repairs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (repair_id, case_id, commitment, owner, stamp(parse_time(due)), verification,
                        "Agreed", "", 1, None, stamp()))
            Store.audit(db, case_id, actor, "repair.agreed", repair_id)
        return repair_id

    def repairs(self, actor: str, case_id: str) -> list[dict]:
        self.case(actor, case_id)
        return self.store.rows("SELECT * FROM repairs WHERE case_id=? ORDER BY due_at", (case_id,))

    @traced
    def update_repair(self, actor: str, repair_id: str, status: str, evidence: str):
        user = self.actor(actor)
        repair = self.store.one("SELECT * FROM repairs WHERE id=?", (repair_id,))
        if not repair:
            raise DomainError("Choose a repair commitment.")
        self.case(actor, repair["case_id"], lead=status == "Verified")
        if status not in {"In progress", "Blocked", "Submitted", "Verified"}:
            raise DomainError("Choose a supported repair status.")
        if status != "Verified" and repair["owner"] != actor and not user["is_manager"]:
            raise DomainError("The support owner or manager must update the repair commitment.")
        if status in {"Submitted", "Verified"} and not evidence.strip():
            raise DomainError("Record the completion evidence or review before submitting or verifying.")
        if status == "Verified" and repair["owner"] == actor:
            raise DomainError("Ask a different facilitator or manager to verify this commitment.")
        with self.store.transaction() as db:
            db.execute("UPDATE repairs SET status=?,evidence=?,verified_by=?,updated_at=? WHERE id=?",
                       (status, evidence, actor if status == "Verified" else None, stamp(), repair_id))
            Store.audit(db, repair["case_id"], actor, "repair.updated", repair_id)

    @traced
    def add_availability(self, actor: str, start: str, end: str):
        self.actor(actor)
        a, b = parse_time(start), parse_time(end)
        if b <= a or b - a > timedelta(days=1):
            raise DomainError("An availability window must last between one minute and one day.")
        with self.store.transaction() as db:
            db.execute("INSERT OR REPLACE INTO availability VALUES (?,?,?)", (actor, stamp(a), stamp(b)))
            Store.audit(db, None, actor, "availability.added")

    def suggest_slots(self, actor: str, case_id: str, duration_minutes: int = 60) -> list[dict]:
        self.case(actor, case_id, lead=True)
        team = [r["username"] for r in self.team(actor, case_id) if r["status"] == "Approved"]
        if not team:
            return []
        intervals = []
        for owner in team:
            rows = self.store.rows("SELECT start_at,end_at FROM availability WHERE username=? "
                                   "AND end_at>? ORDER BY start_at", (owner, stamp()))
            intervals.append([(parse_time(r["start_at"]), parse_time(r["end_at"])) for r in rows])
        overlaps = intervals[0]
        for windows in intervals[1:]:
            overlaps = [(max(a, c), min(b, d)) for a, b in overlaps for c, d in windows
                        if min(b, d) - max(a, c) >= timedelta(minutes=duration_minutes)]
        results = []
        for start, end in overlaps:
            start = max(start, utcnow())
            while start + timedelta(minutes=duration_minutes) <= end and len(results) < 12:
                finish = start + timedelta(minutes=duration_minutes)
                busy = self.store.rows(
                    "SELECT e.id FROM events e JOIN event_members m ON m.event_id=e.id "
                    "WHERE m.username IN (SELECT username FROM roles WHERE case_id=? AND status='Approved') "
                    "AND m.status='Invited' AND e.status='Scheduled' AND e.start_at<? AND e.end_at>?",
                    (case_id, stamp(finish), stamp(start)),
                )
                if not busy:
                    results.append({"start": stamp(start), "end": stamp(finish)})
                start = finish
        return results

    @traced
    def schedule(self, actor: str, case_id: str, kind: str, start: str, end: str,
                 location: str, confirmed: bool, event_id: str = "") -> str:
        case = self.active(actor, case_id, lead=True)
        kinds = {"Case review", "Pre-conference", "Conference", "Follow-up", "Reintegration"}
        if kind not in kinds or not confirmed:
            raise DomainError("Confirm the meeting arrangement with the people involved before scheduling.")
        a, b = parse_time(start), parse_time(end)
        if b <= a or b - a > timedelta(hours=12):
            raise DomainError("Choose an end time after the start, within twelve hours.")
        team = [r["username"] for r in self.team(actor, case_id) if r["status"] == "Approved"]
        if not team:
            raise DomainError("Assign a team before scheduling.")
        result_id = event_id or uid()
        with self.store.transaction() as db:
            case = dict(db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())
            if case["stage"] in {Stage.CLOSED, Stage.PAUSED}:
                raise DomainError("Scheduling is paused or closed for this case.")
            if kind != "Case review":
                self._consented(db, case_id)
            if kind in {"Conference", "Reintegration"}:
                self._staffed(db, case_id)
            if kind == "Conference" and (case["stage"] != Stage.CONFERENCE or not case["readiness"]):
                raise DomainError("Conference scheduling requires the conference stage and confirmed readiness.")
            if kind == "Reintegration" and case["stage"] != Stage.REINTEGRATION:
                raise DomainError("Move to reintegration before scheduling its circle.")
            busy = db.execute(
                "SELECT e.id FROM events e JOIN event_members m ON m.event_id=e.id "
                "WHERE m.username IN (SELECT username FROM roles WHERE case_id=? AND status='Approved') "
                "AND m.status='Invited' AND e.status='Scheduled' AND e.id!=? "
                "AND e.start_at<? AND e.end_at>?",
                (case_id, result_id, stamp(b), stamp(a)),
            ).fetchone()
            if busy:
                raise DomainError("An assigned volunteer already has a CirclePath event at that time.")
            if event_id:
                previous = db.execute("SELECT id FROM events WHERE id=? AND case_id=? AND status='Scheduled'",
                                      (event_id, case_id)).fetchone()
                if not previous:
                    raise DomainError("Choose a scheduled event from this case to reschedule.")
                db.execute("UPDATE events SET kind=?,start_at=?,end_at=?,location=?,sequence=sequence+1,"
                           "updated_at=? WHERE id=?", (kind, stamp(a), stamp(b), location, stamp(), event_id))
            else:
                db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (result_id, case_id, kind, stamp(a), stamp(b), location, "Scheduled", 0, actor, stamp()))
            for owner in team:
                db.execute("INSERT INTO event_members VALUES (?,?,'Invited') "
                           "ON CONFLICT(event_id,username) DO UPDATE SET status='Invited'", (result_id, owner))
                Store.notify(db, case_id, owner, "Calendar update",
                             "A meeting has been scheduled or updated. Refresh your CirclePath calendar.",
                             f"event:{result_id}:{stamp()}")
            Store.audit(db, case_id, actor, "event.scheduled", result_id)
        return result_id

    @traced
    def finish_event(self, actor: str, event_id: str, cancelled: bool = False):
        event = self.store.one("SELECT * FROM events WHERE id=?", (event_id,))
        if not event:
            raise DomainError("Choose an event.")
        self.active(actor, event["case_id"], lead=True)
        with self.store.transaction() as db:
            changed = db.execute("UPDATE events SET status=?,sequence=sequence+1,updated_at=? "
                                 "WHERE id=? AND status='Scheduled'",
                                 ("Cancelled" if cancelled else "Completed", stamp(), event_id)).rowcount
            if not changed:
                raise DomainError("Only a scheduled meeting can be completed or cancelled.")
            Store.audit(db, event["case_id"], actor, "event.cancelled" if cancelled else "event.completed", event_id)
            if not cancelled:
                for member in db.execute("SELECT username FROM event_members WHERE event_id=? AND status='Invited'",
                                         (event_id,)):
                    Store.notify(db, event["case_id"], member[0], "Debrief",
                                 "Please record your short meeting debrief in CirclePath.", f"debrief:{event_id}:{member[0]}")

    def events(self, actor: str, case_id: str) -> list[dict]:
        self.case(actor, case_id)
        return self.store.rows("SELECT * FROM events WHERE case_id=? ORDER BY start_at", (case_id,))

    def calendar_events(self, actor: str) -> list[dict]:
        self.actor(actor)
        return self.store.rows(
            "SELECT e.*,m.status member_status FROM events e JOIN event_members m ON m.event_id=e.id "
            "WHERE m.username=? ORDER BY e.start_at", (actor,)
        )

    def notifications(self, actor: str) -> list[dict]:
        self.actor(actor)
        return self.store.rows("SELECT * FROM notifications WHERE recipient=? ORDER BY created_at DESC LIMIT 100", (actor,))

    @traced
    def read_notification(self, actor: str, notification_id: str):
        self.actor(actor)
        with self.store.transaction() as db:
            count = db.execute("UPDATE notifications SET read_at=? WHERE id=? AND recipient=?",
                               (stamp(), notification_id, actor)).rowcount
            if not count:
                raise DomainError("Choose a notification from your own inbox.")

    def ai_snapshot(self, actor: str, case_id: str, task: str) -> dict:
        case = self.case(actor, case_id)
        if not case["ai_allowed"]:
            raise DomainError("The manager has not authorized AI processing of this case briefing.")
        if case["stage"] == Stage.PAUSED:
            raise DomainError("AI coordination is paused with this case.")
        if case["stage"] == Stage.CLOSED and task not in {"Case summary", "Learning reflection"}:
            raise DomainError("Only a summary or learning reflection can be drafted after closure.")
        sources = {f"S{i}": line.strip() for i, line in enumerate(case["briefing"].splitlines(), 1) if line.strip()}
        sources["WORKFLOW"] = f"Recorded workflow stage: {case['stage']}. This is a staff record, not an assessment of healing."
        if case["support_needs"]:
            sources["SUPPORT"] = "Recorded practical support needs to confirm: " + case["support_needs"]
        if case["closure_reason"]:
            sources["CLOSURE"] = "Manager-recorded alternative closure outcome: " + case["closure_reason"]
        for repair in self.store.rows("SELECT * FROM repairs WHERE case_id=? ORDER BY due_at", (case_id,)):
            sources[f"R-{repair['id'][:8]}"] = (
                f"Recorded participant-agreed commitment: {repair['commitment']}; "
                f"support owner={repair['owner']}; status={repair['status']}; agreed due={repair['due_at']}; "
                f"verification plan={repair['verification_plan']}; recorded evidence={repair['evidence']}"
            )
        for event in self.store.rows("SELECT id,kind,status,start_at FROM events WHERE case_id=? ORDER BY start_at", (case_id,)):
            sources[f"M-{event['id'][:8]}"] = (
                f"Recorded meeting: {event['kind']}; status={event['status']}; scheduled time={event['start_at']}"
            )
        # Raw reports and private debriefs never enter AI context.
        for note in self.store.rows("SELECT * FROM notes WHERE case_id=? AND private=0 ORDER BY created_at", (case_id,)):
            sources[f"N-{note['id'][:8]}"] = (
                f"Attributed volunteer debrief at {note['stage']}: happened={note['happened']}; "
                f"explicitly agreed={note['agreed']}; unresolved/barriers={note['barriers']}"
            )
        return {
            "case_id": case_id, "stage": case["stage"], "task": task,
            "sources": sources,
            "team": [{"owner": r["username"], "role": r["role"]}
                     for r in self.team(actor, case_id) if r["status"] == "Approved"],
            "allowed_owners": sorted(self.owners(case_id)),
            "support_needs": case["support_needs"],
            "closure_reason": case["closure_reason"],
        }

    @staticmethod
    def validate_draft(draft: AIDraft, snapshot: dict):
        available = set(snapshot["sources"])
        owners = set(snapshot["allowed_owners"])
        for fact in draft.facts:
            if not set(fact.source_ids).issubset(available):
                raise DomainError("The AI draft has an unknown source reference. Regenerate or correct it.")
        for action in draft.actions:
            if action.owner not in owners or not set(action.source_ids).issubset(available):
                raise DomainError("Every proposed action needs an authorized owner and valid source references.")
        if snapshot["stage"] == Stage.CLOSED and draft.actions:
            raise DomainError("A closed-case draft cannot create new tasks.")

    @traced
    def save_draft(self, actor: str, case_id: str, draft: AIDraft, snapshot: dict,
                   provider: str, model: str, trace_id: str) -> str:
        self.case(actor, case_id)
        self.validate_draft(draft, snapshot)
        draft_id = uid()
        with self.store.transaction() as db:
            db.execute("INSERT INTO drafts (id,case_id,task,body,sources,provider,model,created_by,"
                       "trace_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (draft_id, case_id, snapshot["task"], draft.model_dump_json(), json.dumps(snapshot),
                        provider, model, actor, trace_id, stamp()))
            Store.audit(db, case_id, actor, "draft.created", draft_id)
            for manager in db.execute("SELECT username FROM users WHERE is_manager=1"):
                Store.notify(db, case_id, manager[0], "AI review", "An AI draft is ready for human review.",
                             f"draft:{draft_id}")
        return draft_id

    def drafts(self, actor: str, case_id: str) -> list[dict]:
        self.case(actor, case_id)
        return self.store.rows("SELECT * FROM drafts WHERE case_id=? ORDER BY created_at DESC", (case_id,))

    @traced
    def review_draft(self, actor: str, draft_id: str, approve: bool, edited_json: str,
                     review_note: str) -> dict:
        draft = self.store.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
        if not draft:
            raise DomainError("Choose a draft.")
        case = self.case(actor, draft["case_id"], lead=True)
        snapshot = json.loads(draft["sources"])
        try:
            body = AIDraft.model_validate_json(edited_json or draft["body"])
        except ValueError as exc:
            raise DomainError("The edited draft must match the displayed JSON structure.") from exc
        if approve:
            if case["stage"] == Stage.PAUSED or case["stage"] != snapshot["stage"]:
                raise DomainError("The case stage has changed. Generate a new draft before approval.")
            snapshot["allowed_owners"] = sorted(self.owners(draft["case_id"]))
            self.validate_draft(body, snapshot)
        with self.telemetry.operation("human.review", {"human.decision": "approved" if approve else "rejected"}):
            with self.store.transaction() as db:
                current_stage = db.execute("SELECT stage FROM cases WHERE id=?", (draft["case_id"],)).fetchone()[0]
                if approve and (current_stage == Stage.PAUSED or current_stage != snapshot["stage"]):
                    raise DomainError("The case stage has changed. Generate a new draft before approval.")
                updated = db.execute(
                    "UPDATE drafts SET status=?,body=?,reviewed_by=?,review_note=? WHERE id=? AND status='Pending'",
                    ("Approved" if approve else "Rejected", body.model_dump_json(), actor, review_note, draft_id),
                ).rowcount
                if not updated:
                    raise DomainError("This draft has already been reviewed. It cannot be applied twice.")
                created = []
                if approve:
                    for action in body.actions:
                        task_id = uid()
                        due = stamp(utcnow() + timedelta(days=action.due_days))
                        db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                                   (task_id, draft["case_id"], action.title, action.owner, due,
                                    "Open", f"Reviewed AI draft {draft_id}", stamp()))
                        created.append(task_id)
                        Store.notify(db, draft["case_id"], action.owner, "New task",
                                     "A reviewed plan assigned you a task. Open CirclePath for details.", f"task:{task_id}")
                Store.audit(db, draft["case_id"], actor, "draft.approved" if approve else "draft.rejected", draft_id)
            self.telemetry.reviews.add(1, {"decision": "approved" if approve else "rejected", "task": draft["task"]})
        return {"status": "Approved" if approve else "Rejected", "created_task_ids": created}

    @traced
    def publish_learning(self, actor: str, case_id: str, lesson: str, reviewed: bool):
        self.manager(actor)
        self.case(actor, case_id)
        if not reviewed or not lesson.strip() or len(lesson) > 2000:
            raise DomainError("Review the lesson for identifying details and use 1–2,000 characters.")
        with self.store.transaction() as db:
            learning_id = uid()
            db.execute("INSERT INTO learnings VALUES (?,?,?,?,?)", (learning_id, case_id, lesson, actor, stamp()))
            Store.audit(db, case_id, actor, "learning.published", learning_id)

    def learning_library(self, actor: str) -> list[dict]:
        self.actor(actor)
        return self.store.rows("SELECT lesson,created_at FROM learnings ORDER BY created_at DESC")

    def audit(self, actor: str, case_id: str) -> list[dict]:
        self.manager(actor)
        return self.store.rows("SELECT * FROM audit WHERE case_id=? ORDER BY created_at DESC", (case_id,))

    def add_user(self, actor: str, username: str, display_name: str, password: str,
                 trained_roles: list[str], confidentiality: bool, email: str = ""):
        self.manager(actor)
        if not username.isidentifier() or len(username) > 60 or len(password) < 12:
            raise DomainError("Use a simple username and a password of at least twelve characters.")
        if not set(trained_roles).issubset(ROLE_SLOTS):
            raise DomainError("Choose supported volunteer roles.")
        with self.store.transaction() as db:
            db.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?)",
                       (username, display_name, hash_password(password), 0, json.dumps(trained_roles),
                        int(confidentiality), email))
            Store.audit(db, None, actor, "volunteer.created", username)

"""An idempotent, bounded reminder loop. No model-generated actions execute here."""

import smtplib
from datetime import timedelta
from email.message import EmailMessage

from .config import Settings
from .models import MINIMUM_TEAM, Stage
from .store import Store, stamp, utcnow
from .telemetry import Telemetry


class ReminderWorker:
    def __init__(self, store: Store, telemetry: Telemetry, settings: Settings):
        self.store, self.telemetry, self.settings = store, telemetry, settings

    def tick(self) -> int:
        now = utcnow()
        today = now.date().isoformat()
        count = 0
        with self.telemetry.operation("coordinator.reminders") as span:
            with self.store.transaction() as db:
                for case in db.execute("SELECT id FROM cases WHERE stage NOT IN (?,?)",
                                       (Stage.CLOSED, Stage.PAUSED)).fetchall():
                    cid = case[0]
                    for task in db.execute("SELECT * FROM tasks WHERE case_id=? AND status='Open' AND due_at<?",
                                           (cid, stamp(now + timedelta(days=1)))).fetchall():
                        authorized = db.execute(
                            "SELECT username FROM users WHERE username=? AND (is_manager=1 OR username IN "
                            "(SELECT username FROM roles WHERE case_id=? AND status='Approved'))",
                            (task["owner"], cid),
                        ).fetchone()
                        if authorized:
                            count += Store.notify(db, cid, task["owner"], "Task reminder",
                                                  "A task is due soon or needs a progress update. Open CirclePath.",
                                                  f"task-reminder:{task['id']}:{today}")
                    recipients = {r[0] for r in db.execute("SELECT username FROM users WHERE is_manager=1")}
                    recipients.update(r[0] for r in db.execute(
                        "SELECT username FROM roles WHERE case_id=? AND role='Support liaison' AND status='Approved'", (cid,)
                    ))
                    for repair in db.execute("SELECT * FROM repairs WHERE case_id=? AND status!='Verified' "
                                             "AND (status='Blocked' OR due_at<?)",
                                             (cid, stamp(now + timedelta(days=1)))).fetchall():
                        for recipient in recipients:
                            count += Store.notify(db, cid, recipient, "Repair support",
                                                  "A repair commitment may need support. Review the recorded barrier.",
                                                  f"repair-support:{repair['id']}:{recipient}:{today}")
                    roles = {r[0]: r[1] for r in db.execute(
                        "SELECT role,COUNT(*) FROM roles WHERE case_id=? AND status='Approved' GROUP BY role", (cid,)
                    )}
                    if any(roles.get(role, 0) < minimum for role, minimum in MINIMUM_TEAM.items()):
                        for manager in db.execute("SELECT username FROM users WHERE is_manager=1"):
                            count += Store.notify(db, cid, manager[0], "Team gap",
                                                  "An active case needs a team assignment. Review available volunteers.",
                                                  f"team-gap:{cid}:{manager[0]}:{today}")
                    for event in db.execute("SELECT * FROM events WHERE case_id=? AND status='Scheduled' "
                                            "AND start_at>=? AND start_at<=?",
                                            (cid, stamp(now), stamp(now + timedelta(days=1)))).fetchall():
                        for member in db.execute("SELECT username FROM event_members WHERE event_id=? AND status='Invited'",
                                                 (event["id"],)):
                            count += Store.notify(db, cid, member[0], "Meeting reminder",
                                                  "A CirclePath meeting is coming up. Review your preparation and calendar.",
                                                  f"meeting:{event['id']}:{member[0]}:{today}")
            span.set_attribute("notification.count", count)
            self.telemetry.reminders.add(count)
        return count

    def deliver_email(self) -> int:
        """Explicit SMTP opt-in; demo accounts use .example addresses and are never mailed."""
        if not self.settings.smtp_enabled or not self.settings.smtp_host:
            return 0
        pending = self.store.rows(
            "SELECT n.*,u.email FROM notifications n JOIN users u ON u.username=n.recipient "
            "WHERE n.email_status='Disabled' AND n.read_at IS NULL AND u.email!=''"
        )
        delivered = 0
        for row in pending:
            if row["email"].endswith(".example") or "@" not in row["email"]:
                continue
            message = EmailMessage()
            message["From"] = self.settings.smtp_from
            message["To"] = row["email"]
            message["Subject"] = "CirclePath: a coordination update"
            message.set_content("An update is waiting in your secure CirclePath account. "
                                "Please sign in to review your next steps.")
            try:
                with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=10) as client:
                    client.starttls()
                    if self.settings.smtp_username:
                        client.login(self.settings.smtp_username, self.settings.smtp_password.get_secret_value())
                    client.send_message(message)
                status = "Sent"
                delivered += 1
            except Exception:
                status = "Failed"
            with self.store.transaction() as db:
                db.execute("UPDATE notifications SET email_status=? WHERE id=?", (status, row["id"]))
        return delivered

"""Entirely fictional fixtures. No participant names, real reports, or real contact details."""

import json
import secrets
from datetime import timedelta

from .config import Settings
from .service import Service
from .store import hash_password, stamp, uid, utcnow

ACCOUNTS = [
    ("manager", "Demo program manager", True, []),
    ("avery", "Avery · facilitator", False, ["Facilitator", "Co-facilitator"]),
    ("jordan", "Jordan · co-facilitator", False, ["Co-facilitator", "Community member"]),
    ("casey", "Casey · community member", False, ["Community member"]),
    ("quinn", "Quinn · community member", False, ["Community member"]),
    ("sam", "Sam · support liaison", False, ["Support liaison", "Community member"]),
    ("riley", "Riley · community volunteer", False, ["Community member"]),
]


def seed(service: Service, settings: Settings) -> dict:
    if service.store.one("SELECT username FROM users LIMIT 1"):
        return {"seeded": False, "message": "Existing data preserved."}
    passwords = {}
    with service.store.transaction() as db:
        for username, name, manager, roles in ACCOUNTS:
            password = secrets.token_urlsafe(18)
            passwords[username] = password
            db.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?)",
                       (username, name, hash_password(password), int(manager), json.dumps(roles),
                        1, f"{username}@circlepath.example"))
    cases = []
    cases.append(service.create_case(
        "manager", "Demo · Cedar",
        "FICTIONAL CASE. The referral describes damage to a shared community garden gate.\n"
        "Youth A has expressed interest in exploring a restorative conversation; no agreement has been reached.\n"
        "The harmed party wants to understand the impact and explore practical repair.\n"
        "The team needs to discuss accessible transport and preparation arrangements with the people involved.",
        report="FICTIONAL REPORT FOR DEMONSTRATION ONLY.\nA referral reports damage to a garden gate. "
               "This report is an attributed account, not a finding of fact. Review under the program's policy.",
        ai_allowed=True, guardian_required=True, support_needs="Discuss transport and guardian availability.",
    ))
    cases.append(service.create_case(
        "manager", "Demo · Aspen",
        "FICTIONAL CASE. A school referral describes damage to shared art materials.\n"
        "Participants have not yet decided whether to take part.\n"
        "The program manager is forming a volunteer team for preparation.",
        ai_allowed=True,
    ))
    cases.append(service.create_case(
        "manager", "Demo · Willow",
        "FICTIONAL CASE. A facilitated conference addressed reported damage to a shared outdoor bench.\n"
        "Participants agreed to explore restoration of the bench with a supervised community workshop.\n"
        "The workshop time needs transport support; the liaison is helping coordinate the arrangement.",
        ai_allowed=True, support_needs="Transport support for the agreed workshop.",
    ))
    assignments = [("avery", "Facilitator"), ("jordan", "Co-facilitator"),
                   ("casey", "Community member"), ("quinn", "Community member"), ("sam", "Support liaison")]
    for cid in [cases[0], cases[2]]:
        for username, role in assignments:
            request = service.claim_role(username, cid, role)
            service.review_role("manager", request, True)
            service.mark_reviewed(username, cid)
        for party in ["Youth", "Harmed party", "Guardian"]:
            service.record_consent("manager", cid, party, "Active")
        service.set_readiness("avery", cid, True)
    # A pending request exercises manager approval and the optional third community role.
    service.claim_role("riley", cases[0], "Community member", "No known relationship with participants.")
    service.add_task("manager", cases[0], "Discuss transport options before confirming preparation meetings",
                     "sam", stamp(utcnow() + timedelta(hours=12)))
    start = (utcnow() + timedelta(days=2)).replace(hour=22, minute=0, second=0, microsecond=0)
    for username, *_ in ACCOUNTS:
        service.add_availability(username, stamp(start), stamp(start + timedelta(hours=3)))
    # Willow is a demonstration already in the repair stage. Its prior meetings are recorded explicitly.
    with service.store.transaction() as db:
        db.execute("UPDATE cases SET stage='Repair & follow-up' WHERE id=?", (cases[2],))
        for kind, days in [("Pre-conference", 8), ("Conference", 5)]:
            event_id = uid()
            past = utcnow() - timedelta(days=days)
            db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (event_id, cases[2], kind, stamp(past), stamp(past + timedelta(hours=1)),
                        "Demo program office", "Completed", 0, "manager", stamp()))
            for username, _ in assignments:
                db.execute("INSERT INTO event_members VALUES (?,?,'Invited')", (event_id, username))
    repair = service.add_repair("avery", cases[2], "Coordinate the participant-agreed supervised bench workshop",
                                "sam", stamp(utcnow() + timedelta(days=7)),
                                "Facilitator reviews the workshop organizer's completion confirmation.", True)
    service.update_repair("sam", repair, "Blocked", "Workshop transport has not yet been arranged.")
    service.debrief("jordan", cases[2], "The conference took place with the prepared participants.",
                     "A supervised workshop was agreed; timing remains to be confirmed.",
                     "Transport support is needed before the workshop.")
    credentials = settings.data_dir / "demo-credentials.json"
    credentials.write_text(json.dumps(passwords, indent=2))
    credentials.chmod(0o600)
    instructions = settings.data_dir / "demo-login.txt"
    instructions.write_text("CirclePath local demo accounts — generated for this installation only\n\n" +
                            "\n".join(f"{name}: {password}" for name, password in passwords.items()) +
                            "\n\nAll cases and volunteer names are fictional. Keep this file local.\n")
    instructions.chmod(0o600)
    return {"seeded": True, "case_ids": cases, "credentials_file": str(instructions.resolve())}

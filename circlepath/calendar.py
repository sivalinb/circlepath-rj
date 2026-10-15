import json
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from icalendar import Calendar, Event

from .config import Settings
from .models import DomainError
from .service import Service, parse_time
from .store import utcnow, uid

SCOPES = ["https://www.googleapis.com/auth/calendar.events.owned"]


def calendar_bytes(service: Service, actor: str) -> bytes:
    calendar = Calendar()
    calendar.add("prodid", "-//CirclePath//Restorative coordination//EN")
    calendar.add("version", "2.0")
    calendar.add("x-wr-calname", "CirclePath meetings")
    for row in service.calendar_events(actor):
        event = Event()
        event.add("uid", row["id"] + "@circlepath.local")
        event.add("summary", "CirclePath · " + row["kind"])
        event.add("dtstart", parse_time(row["start_at"]))
        event.add("dtend", parse_time(row["end_at"]))
        event.add("dtstamp", utcnow())
        event.add("last-modified", parse_time(row["updated_at"]))
        event.add("sequence", row["sequence"])
        cancelled = row["status"] == "Cancelled" or row["member_status"] == "Cancelled"
        event.add("status", "CANCELLED" if cancelled else "CONFIRMED")
        event.add("location", "" if cancelled else row["location"])
        event.add("description", "Sign in to CirclePath for preparation and next steps.")
        calendar.add_component(event)
    return calendar.to_ical()


def export_calendar(service: Service, settings: Settings, actor: str) -> str:
    service.actor(actor)
    target = settings.data_dir / "exports"
    target.mkdir(exist_ok=True, mode=0o700)
    path = target / f"calendar-{uid()}.ics"
    path.write_bytes(calendar_bytes(service, actor))
    path.chmod(0o600)
    return str(path.resolve())


def token_path(settings: Settings, actor: str) -> Path:
    if not actor.isidentifier():
        raise DomainError("Choose a valid CirclePath username.")
    return settings.data_dir / "calendar" / f"{actor}.json"


def connect_google(settings: Settings, actor: str):
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not settings.google_oauth_client_file:
        raise DomainError("Set GOOGLE_OAUTH_CLIENT_FILE to your desktop OAuth client JSON file.")
    flow = InstalledAppFlow.from_client_secrets_file(settings.google_oauth_client_file, SCOPES)
    credentials = flow.run_local_server(port=0)
    path = token_path(settings, actor)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(credentials.to_json())
    path.chmod(0o600)


def sync_google(service: Service, settings: Settings, actor: str) -> int:
    service.actor(actor)
    path = token_path(settings, actor)
    if not path.is_file():
        raise DomainError("Connect your Google Calendar using the local calendar-connect command first. "
                          "Calendar downloads work without a Google connection.")
    credentials = Credentials.from_authorized_user_file(str(path), SCOPES)
    client = AuthorizedSession(credentials)
    count = 0
    with service.telemetry.operation("calendar.google_sync"):
        try:
            for row in service.calendar_events(actor):
                # Stable lowercase hexadecimal IDs meet Google's base32hex constraint.
                event_id = row["id"]
                url = f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{event_id}"
                existing = client.get(url, timeout=15)
                cancelled = row["status"] == "Cancelled" or row["member_status"] == "Cancelled"
                if cancelled:
                    if existing.status_code == 200:
                        response = client.delete(url, timeout=15, params={"sendUpdates": "none"})
                        response.raise_for_status()
                        count += 1
                    continue
                payload = {
                    "summary": "CirclePath · " + row["kind"],
                    "description": "Sign in to CirclePath for preparation and next steps.",
                    "location": row["location"],
                    "start": {"dateTime": row["start_at"]}, "end": {"dateTime": row["end_at"]},
                    "extendedProperties": {"private": {"circlepath": "true"}},
                }
                if existing.status_code == 404:
                    payload["id"] = event_id
                    response = client.post(url.rsplit("/", 1)[0], json=payload, timeout=15,
                                           params={"sendUpdates": "none"})
                else:
                    existing.raise_for_status()
                    response = client.patch(url, json=payload, timeout=15, params={"sendUpdates": "none"})
                response.raise_for_status()
                count += 1
            path.write_text(credentials.to_json())
            path.chmod(0o600)
        except Exception as exc:
            raise DomainError("Calendar sync could not finish. Check your Google connection and retry.") from exc
        finally:
            client.close()
    return count


def export_summary(service: Service, settings: Settings, actor: str, draft_id: str) -> str:
    draft = service.store.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
    if not draft:
        raise DomainError("Choose a reviewed summary draft.")
    service.case(actor, draft["case_id"], lead=True)
    if draft["status"] != "Approved" or draft["task"] != "Case summary":
        raise DomainError("Only a human-approved Case summary can be exported.")
    body = json.loads(draft["body"])
    folder = settings.data_dir / "exports"
    folder.mkdir(exist_ok=True, mode=0o700)
    path = folder / f"summary-{uid()}.txt"
    path.write_text(f"CirclePath — reviewed case summary\n\n{body['summary']}\n\n"
                    "Prepared for internal review. Sharing requires the program's own authorization.\n")
    path.chmod(0o600)
    return str(path.resolve())

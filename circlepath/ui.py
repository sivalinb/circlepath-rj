import html
import json
from datetime import timedelta

import gradio as gr

from .agents import Coordinator
from .calendar import export_calendar, export_summary, sync_google
from .config import Settings
from .models import AIDraft, AI_TASKS, DomainError, ProposedAction, ROLE_SLOTS
from .service import Service
from .store import stamp, utcnow
from .worker import ReminderWorker

CSS = """
.gradio-container {max-width:1280px!important; margin:auto!important;}
#brand {padding:24px 8px 20px; border-bottom:1px solid #dce5df; margin-bottom:10px;}
#brand .eyebrow {font-size:12px; letter-spacing:2px; color:#277165; font-weight:600;}
#brand h1 {font-size:38px; letter-spacing:-1.1px; color:#20453d; margin:5px 0;}
#brand p {color:#567368; max-width:740px; margin:6px 0;}
#brand .mark {display:inline-flex; width:35px; height:35px; border:5px solid #307b6c;
 border-radius:50%; vertical-align:middle; margin-right:10px;}
.demo-banner {background:#eef4e7; border-radius:8px; padding:10px 14px; color:#456244; font-size:13px;}
.stage-strip {display:flex; flex-wrap:wrap; gap:8px; padding:12px 0;}
.stage-step {padding:6px 10px; background:#f0f3ef; border-radius:6px; color:#5b6f62; font-size:13px;}
.stage-step.current {background:#226d5f; color:white;}
.case-heading {margin:8px 0 0; color:#244e43;}
.status-line {color:#426858; padding:8px 0;}
.dark #brand h1, .dark .case-heading {color:#b5d8cb;}
.dark #brand p,.dark .status-line {color:#a8c0b4;}
.dark .demo-banner {background:#253629; color:#d1dfc7;}
.dark .stage-step {background:#25312c; color:#b6c9bf;}
.dark .stage-step.current {background:#286b5d; color:#fff;}
footer {display:none!important;}
"""


def table(rows: list[dict], columns: list[str]) -> list[list]:
    return [[row.get(column, "") for column in columns] for row in rows]


def choices(rows: list[dict], label: str) -> list[tuple[str, str]]:
    return [(str(row[label])[:100] + " · " + row["id"][:8], row["id"]) for row in rows]


def build_ui(service: Service, coordinator: Coordinator, worker: ReminderWorker, settings: Settings):
    def actor(request: gr.Request) -> str:
        username = request.username
        service.actor(username)
        return username

    def safely(fn):
        def wrapped(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except DomainError as exc:
                raise gr.Error(str(exc)) from None
            except Exception:
                raise gr.Error("This action could not finish. Refresh the case and review the workflow.") from None
        return wrapped

    def case_options(username):
        return [(f"{c['alias']} · {c['stage']}", c["id"]) for c in service.opportunities(username)]

    def overview(cid: str, request: gr.Request):
        username = actor(request)
        opportunities = service.opportunities(username)
        overview_rows = table(opportunities, ["alias", "stage", "open_roles"])
        try:
            case = service.case(username, cid)
        except DomainError:
            own = service.store.rows("SELECT id,role,status FROM roles WHERE case_id=? AND username=?", (cid, username))
            return (overview_rows, "Select a role opportunity or an assigned case.", "",
                    "Case details are available after role approval. Your request: " +
                    (own[0]["status"] if own else "none"), "", [],
                    gr.Dropdown(choices=choices(own, "role"), value=own[0]["id"] if own else None),
                    [], gr.Dropdown(choices=[], value=None), [], [],
                    gr.Dropdown(choices=[], value=None), [], gr.Dropdown(choices=[], value=None),
                    [], gr.Dropdown(choices=[], value=None), gr.Dropdown(choices=[], value=None),
                    [], [], "", False)
        stages = ["Case review", "Pre-conference", "Conference", "Repair & follow-up", "Reintegration", "Closed"]
        stage_html = '<div class="stage-strip">' + "".join(
            f'<span class="stage-step {"current" if stage == case["stage"] else ""}">{stage}</span>'
            for stage in stages
        ) + "</div>"
        if case["stage"] == "Paused":
            stage_html += '<p class="status-line">Paused — review participants’ choices before resuming.</p>'
        team = service.team(username, cid)
        tasks = service.tasks(username, cid)
        drafts = service.drafts(username, cid)
        repairs = service.repairs(username, cid)
        events = service.events(username, cid)
        owners = sorted(service.owners(cid))
        user = service.actor(username)
        heading = f'<h2 class="case-heading">{html.escape(case["alias"])}</h2>'
        role_choices = [(f"{r['display_name']} · {r['role']} · {r['status']}", r["id"]) for r in team]
        personal = [t for t in tasks if t["status"] == "Open" and (t["owner"] == username or user["is_manager"])]
        report = case["report"] or ("The original report is reviewed at the program office under this case's policy.")
        return (
            overview_rows, heading, stage_html, case["briefing"], report,
            table(team, ["username", "role", "status", "reviewed"]),
            gr.Dropdown(choices=role_choices, value=team[0]["id"] if team else None),
            table(personal, ["title", "owner", "due_at", "status"]),
            gr.Dropdown(choices=choices(personal, "title"), value=personal[0]["id"] if personal else None),
            table(service.consents(username, cid), ["party", "status"]),
            table(events, ["kind", "start_at", "end_at", "status"]),
            gr.Dropdown(choices=choices(events, "kind"), value=events[0]["id"] if events else None),
            table(repairs, ["commitment", "owner", "due_at", "status", "evidence"]),
            gr.Dropdown(choices=choices(repairs, "commitment"), value=repairs[0]["id"] if repairs else None),
            table(drafts, ["task", "status", "provider", "model"]),
            gr.Dropdown(choices=choices(drafts, "task"), value=drafts[0]["id"] if drafts else None),
            gr.Dropdown(choices=owners, value=owners[0] if owners else None),
            table(service.notes(username, cid), ["author", "stage", "happened", "agreed", "barriers", "private"]),
            table(service.audit(username, cid), ["actor", "action", "created_at"]) if user["is_manager"] else [],
            case["support_needs"], bool(case["readiness"]),
        )

    def load(request: gr.Request):
        username = actor(request)
        user = service.actor(username)
        available = case_options(username)
        approved = service.store.one("SELECT case_id FROM roles WHERE username=? AND status='Approved' LIMIT 1", (username,))
        value = approved["case_id"] if approved else (available[0][1] if available else None)
        return (f"Signed in as **{user['display_name']}** · " + ("Program manager" if user["is_manager"] else "Volunteer"),
                gr.Dropdown(choices=available, value=value),
                gr.Group(visible=bool(user["is_manager"])),
                gr.Dropdown(choices=user["trained_roles"], value=user["trained_roles"][0] if user["trained_roles"] else None))

    def mutation(fn, success: str):
        import inspect
        parameters = list(inspect.signature(fn).parameters.values())[1:]
        def factory(arity=None):
            selected = parameters[:arity] if arity is not None else [
                p for p in parameters if p.default is inspect.Parameter.empty
            ]
            def call(*args):
                request = args[-1]
                fn(actor(request), *args[:-1])
                return success
            call.__signature__ = inspect.Signature([
                p.replace(default=inspect.Parameter.empty) for p in selected
            ] + [inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=gr.Request)])
            call.__annotations__ = {"request": gr.Request}
            wrapped = safely(call)
            import functools
            return functools.wraps(call)(wrapped)
        return factory()

    def optional_mutation(fn, success, arity):
        import inspect
        import functools
        parameters = list(inspect.signature(fn).parameters.values())[1:arity + 1]
        def call(*args):
            fn(actor(args[-1]), *args[:-1])
            return success
        call.__signature__ = inspect.Signature([
            p.replace(default=inspect.Parameter.empty) for p in parameters
        ] + [inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=gr.Request)])
        call.__annotations__ = {"request": gr.Request}
        return functools.wraps(call)(safely(call))

    def new_case(alias, brief, original, policy, ai_allowed, synthetic, guardian, support, request: gr.Request):
        cid = service.create_case(actor(request), alias, brief, original, policy, ai_allowed,
                                  synthetic, guardian, support)
        return gr.Dropdown(choices=case_options(actor(request)), value=cid), "Case created. Assign the team next."

    def generate(cid, task, request: gr.Request):
        draft_id = coordinator.run(actor(request), cid, task)
        rows = service.drafts(actor(request), cid)
        return gr.Dropdown(choices=choices(rows, "task"), value=draft_id), "Draft ready for review. No actions have been applied."

    def draft_view(draft_id, request: gr.Request):
        if not draft_id:
            return "", "", [], "", "", ""
        row = service.store.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
        if not row:
            raise DomainError("Choose a draft.")
        service.case(actor(request), row["case_id"])
        body = AIDraft.model_validate_json(row["body"])
        facts = "\n\n".join(f"{f.statement}\nSources: {', '.join(f.source_ids)}" for f in body.facts)
        tips = "\n\n".join(f"Suggested: {t.suggested}\nReconsider: {t.reconsider}\nReason: {t.reason}" for t in body.language_tips)
        provenance = json.loads(row["sources"])
        sources = "\n\n".join(f"{sid}: {text}" for sid, text in provenance["sources"].items())
        actions = [[a.kind, a.title, a.owner, a.due_days, ",".join(a.source_ids)] for a in body.actions]
        return (body.summary, "\n".join(body.questions), actions, facts + "\n\n" + tips,
                sources, f"{row['status']} · {row['provider']} / {row['model']} · trace {row['trace_id']}\n\n"
                + "\n".join(body.learning_points))

    def review(draft_id, summary, questions, actions, decision, checked, note, request: gr.Request):
        row = service.store.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
        if not row:
            raise DomainError("Choose a draft to review.")
        if decision == "Approve" and not checked:
            raise DomainError("Review the draft and source references before approving.")
        body = AIDraft.model_validate_json(row["body"])
        body.summary = summary
        body.questions = [q.strip() for q in questions.splitlines() if q.strip()]
        body.actions = [ProposedAction(kind=r[0], title=r[1], owner=r[2], due_days=int(r[3]),
                                      source_ids=[s.strip() for s in str(r[4]).split(",") if s.strip()])
                        for r in (actions or []) if len(r) >= 5 and r[1]]
        result = service.review_draft(actor(request), draft_id, decision == "Approve", body.model_dump_json(), note)
        return f"{result['status']}. {len(result['created_task_ids'])} reviewed tasks created. Refresh the case to see updates."

    def inbox(request: gr.Request):
        rows = service.notifications(actor(request))
        return table(rows, ["kind", "message", "created_at", "read_at"]), gr.Dropdown(choices=choices(rows, "kind"), value=rows[0]["id"] if rows else None)

    def personal_calendar(request: gr.Request):
        return export_calendar(service, settings, actor(request))

    def google_sync(request: gr.Request):
        count = sync_google(service, settings, actor(request))
        return f"Updated {count} events in your connected Google Calendar."

    def slots(cid, request: gr.Request):
        return table(service.suggest_slots(actor(request), cid), ["start", "end"])

    def reminder_tick(request: gr.Request):
        service.manager(actor(request))
        return f"Created {worker.tick()} new reminder(s). Duplicate reminders are suppressed."

    def observations(request: gr.Request):
        service.manager(actor(request))
        service.telemetry.flush()
        state = service.telemetry.status()
        return json.dumps(state, indent=2), gr.Dataframe(value=table(service.learning_library(actor(request)), ["lesson", "created_at"]))

    def save_debrief(cid, happened, agreed, barriers, private, prepare_followup, request: gr.Request):
        username = actor(request)
        service.debrief(username, cid, happened, agreed, barriers, private)
        if private or not prepare_followup:
            return "Debrief saved. Private notes remain outside AI context.", gr.skip()
        try:
            did = coordinator.run(username, cid, "Follow-up plan")
            rows = service.drafts(username, cid)
            return ("Debrief saved and a follow-up draft prepared for review. No actions were applied.",
                    gr.Dropdown(choices=choices(rows, "task"), value=did))
        except DomainError:
            return "Debrief saved. A follow-up draft was unavailable; you can continue with the manual workflow.", gr.skip()

    def role_review_action(rid, dec, request: gr.Request):
        service.review_role(actor(request), rid, dec == "Approve")
        return "Role decision recorded. Refresh the case."

    def summary_export_action(did, request: gr.Request):
        return export_summary(service, settings, actor(request), did)

    def cancel_event_action(eid, request: gr.Request):
        service.finish_event(actor(request), eid, True)
        return "Meeting cancelled. Refresh or sync your personal calendar."

    default_start = stamp(utcnow() + timedelta(days=2))
    default_end = stamp(utcnow() + timedelta(days=2, hours=1))
    with gr.Blocks(title="CirclePath · Restorative coordination") as demo:
        gr.HTML('<div id="brand"><div class="eyebrow">RESTORATIVE CASE COORDINATION</div>'
                '<h1><span class="mark"></span>CirclePath</h1>'
                '<p>A prepared team. A clear next step. Follow-through with care.</p></div>'
                '<div class="demo-banner">Local demonstration · fictional cases · human decisions · private telemetry</div>')
        who = gr.Markdown()
        with gr.Row():
            case_id = gr.Dropdown(label="Case workspace", choices=[], scale=5, allow_custom_value=True)
            refresh = gr.Button("Refresh case", scale=1)
        case_title = gr.HTML()
        stage_strip = gr.HTML()
        status = gr.Textbox(label="Coordination update", interactive=False, lines=2)
        with gr.Tab("My next steps"):
            gr.Markdown("### Tasks and check-ins")
            personal_tasks = gr.Dataframe(headers=["Task", "Owner", "Due (UTC)", "Status"], interactive=False, wrap=True)
            with gr.Row():
                task_id = gr.Dropdown(label="Task to update", choices=[], scale=4, allow_custom_value=True)
                complete_task = gr.Button("Mark task completed", scale=1)
            gr.Markdown("### Your reminder inbox")
            inbox_table = gr.Dataframe(headers=["Update", "Message", "Created (UTC)", "Read"], interactive=False, wrap=True)
            with gr.Row():
                notification_id = gr.Dropdown(label="Reminder", choices=[], allow_custom_value=True)
                inbox_refresh = gr.Button("Refresh inbox")
                mark_read = gr.Button("Mark read")
            with gr.Row():
                calendar_download = gr.Button("Download my calendar")
                sync_calendar = gr.Button("Sync connected Google Calendar")
            calendar_file = gr.File(label="Personal calendar (.ics)", interactive=False)
        with gr.Tab("Case & team"):
            opportunities = gr.Dataframe(headers=["Case alias", "Stage", "Open roles"], interactive=False, wrap=True)
            with gr.Row():
                with gr.Column(scale=3):
                    briefing = gr.Textbox(label="Approved case briefing", interactive=False, lines=8)
                    support_needs = gr.Textbox(label="Support needs to discuss", interactive=False)
                    report = gr.Textbox(label="Report review — subject to case access policy", interactive=False, lines=5)
                    reviewed = gr.Button("I completed my case review")
                with gr.Column(scale=2):
                    team = gr.Dataframe(headers=["Volunteer", "Role", "Status", "Reviewed"], interactive=False, wrap=True)
                    claim = gr.Dropdown(label="Role I am trained for", choices=[])
                    conflict = gr.Textbox(label="Private conflict or relationship disclosure", lines=2)
                    claim_button = gr.Button("Request this role", variant="primary")
                    role_id = gr.Dropdown(label="Role request / assignment", choices=[], allow_custom_value=True)
                    role_decision = gr.Radio(["Approve", "Decline"], value="Approve", label="Manager role decision")
                    role_review = gr.Button("Review role request")
                    role_release = gr.Button("Release my role / manager release")
            consent_table = gr.Dataframe(headers=["Participant role", "Choice to continue"], interactive=False)
            with gr.Row():
                party = gr.Dropdown(["Youth", "Harmed party", "Guardian"], value="Youth", label="Participant role")
                consent_status = gr.Dropdown(["Active", "Pending", "Declined", "Withdrawn"], value="Active", label="Recorded consent")
                save_consent = gr.Button("Record participant’s choice")
            with gr.Row():
                readiness = gr.Checkbox(label="Facilitator confirms current participant readiness")
                save_readiness = gr.Button("Record readiness")
                advance = gr.Button("Move to next stage", variant="primary")
                pause = gr.Button("Pause coordination")
                resume = gr.Button("Manager resume")
            with gr.Accordion("Alternative closure", open=False):
                closure_reason = gr.Textbox(label="Outcome and remaining support needs", lines=3)
                close_alternative = gr.Button("Manager record alternative closure")
        with gr.Tab("Prepare with AI"):
            gr.Markdown("### Preparation, follow-up, and reflection")
            gr.Markdown("Use the approved briefing and shared debriefs. Every result stays a draft until reviewed.")
            with gr.Row():
                ai_task = gr.Dropdown(AI_TASKS, value=AI_TASKS[0], label="What would help now?", scale=4)
                generate_ai = gr.Button("Prepare a draft", variant="primary", scale=1)
            draft_table = gr.Dataframe(headers=["Draft", "Review status", "Provider", "Model"], interactive=False)
            draft_id = gr.Dropdown(label="Draft to review", choices=[], allow_custom_value=True)
            draft_status = gr.Textbox(label="Draft provenance and learning suggestions", lines=3, interactive=False)
            draft_summary = gr.Textbox(label="Review and edit the summary", lines=7)
            draft_questions = gr.Textbox(label="Suggested questions — one per line", lines=4)
            draft_guidance = gr.Textbox(label="Attributed facts and language guidance", lines=7, interactive=False)
            draft_actions = gr.Dataframe(headers=["Type", "Suggested task", "Owner", "Days from approval", "Source references"],
                                         datatype=["str", "str", "str", "number", "str"], type="array",
                                         interactive=True, wrap=True)
            with gr.Accordion("Review source references", open=False):
                draft_sources = gr.Textbox(label="Briefing and shared debrief sources", lines=10, interactive=False)
            with gr.Row():
                decision = gr.Radio(["Approve", "Reject"], value="Approve", label="Review decision")
                draft_checked = gr.Checkbox(label="I reviewed the draft and its source references")
            review_note = gr.Textbox(label="Review notes / correction feedback", lines=2)
            review_ai = gr.Button("Record human review", variant="primary")
            with gr.Row():
                summary_export = gr.Button("Export approved case summary")
                summary_file = gr.File(label="Reviewed summary", interactive=False)
        with gr.Tab("Meetings & calendar"):
            event_table = gr.Dataframe(headers=["Meeting", "Start (UTC)", "End (UTC)", "Status"], interactive=False, wrap=True)
            with gr.Row():
                available_start = gr.Textbox(label="My availability starts", value=default_start)
                available_end = gr.Textbox(label="My availability ends", value=default_end)
                availability_save = gr.Button("Save my availability")
            find_slots = gr.Button("Find shared team availability")
            slot_table = gr.Dataframe(headers=["Possible start (UTC)", "Possible end (UTC)"], interactive=False)
            with gr.Row():
                event_kind = gr.Dropdown(["Case review", "Pre-conference", "Conference", "Follow-up", "Reintegration"],
                                         value="Case review", label="Meeting type")
                event_start = gr.Textbox(label="Start (ISO time; unzoned times use Colorado)", value=default_start)
                event_end = gr.Textbox(label="End", value=default_end)
            location = gr.Textbox(label="Meeting location", value="Demo program office")
            meeting_confirmed = gr.Checkbox(label="The meeting arrangement is confirmed with the people involved")
            schedule = gr.Button("Schedule meeting", variant="primary")
            with gr.Row():
                event_id = gr.Dropdown(label="Existing meeting", choices=[], allow_custom_value=True)
                reschedule = gr.Button("Reschedule selected meeting")
                finish_event = gr.Button("Record meeting completed")
                cancel_event = gr.Button("Cancel meeting")
        with gr.Tab("Debriefs & repair"):
            gr.Markdown("### Short debrief after each step")
            happened = gr.Textbox(label="What happened? Preserve attribution where accounts differ.", lines=3)
            agreed = gr.Textbox(label="What was explicitly agreed?", lines=3)
            barriers = gr.Textbox(label="What remains unresolved or needs support?", lines=3)
            private_note = gr.Checkbox(label="Private to me and the manager — excluded from AI")
            auto_followup = gr.Checkbox(value=True, label="Prepare an AI follow-up draft from this shared debrief")
            debrief_save = gr.Button("Save my debrief", variant="primary")
            notes_table = gr.Dataframe(headers=["Author", "Stage", "What happened", "Agreed", "Barriers", "Private"],
                                       interactive=False, wrap=True)
            gr.Markdown("### Participant-agreed repair commitments")
            repair_table = gr.Dataframe(headers=["Commitment", "Support owner", "Due (UTC)", "Status", "Evidence"],
                                        interactive=False, wrap=True)
            owner = gr.Dropdown(label="Approved team support owner", choices=[], allow_custom_value=True)
            commitment = gr.Textbox(label="Commitment participants agreed to", lines=2)
            repair_due = gr.Textbox(label="Agreed due date/time", value=stamp(utcnow() + timedelta(days=14)))
            verification = gr.Textbox(label="How completion will be reviewed", lines=2)
            repair_confirmed = gr.Checkbox(label="Participants explicitly agreed to this commitment")
            repair_save = gr.Button("Record agreed repair")
            with gr.Row():
                repair_id = gr.Dropdown(label="Repair to update", choices=[], allow_custom_value=True)
                repair_status = gr.Dropdown(["In progress", "Blocked", "Submitted", "Verified"], value="In progress", label="Progress")
            evidence = gr.Textbox(label="Progress, barrier, or verification evidence", lines=3)
            repair_update = gr.Button("Record progress / human verification")
            with gr.Accordion("Add a volunteer coordination task", open=False):
                task_title = gr.Textbox(label="Next action")
                task_due = gr.Textbox(label="Due", value=stamp(utcnow() + timedelta(days=2)))
                task_add = gr.Button("Assign task to selected support owner")
        with gr.Tab("Program & observability"):
            with gr.Group(visible=False) as manager_group:
                with gr.Accordion("Open a new case", open=False):
                    new_alias = gr.Textbox(label="Operational case alias — no participant names")
                    new_brief = gr.Textbox(label="Approved briefing — one source statement per line", lines=5)
                    new_report = gr.Textbox(label="Original report, if digitization is permitted", lines=4)
                    new_policy = gr.Radio(["office-only", "secure-team"], value="office-only", label="Report access policy")
                    new_ai = gr.Checkbox(label="Program authorizes AI processing of this briefing")
                    new_synthetic = gr.Checkbox(value=True, label="This is a fictional demonstration case")
                    new_guardian = gr.Checkbox(label="Program requires guardian consent for this case")
                    new_support = gr.Textbox(label="Practical support needs to discuss")
                    create_case = gr.Button("Open case", variant="primary")
                with gr.Accordion("Add a volunteer account", open=False):
                    volunteer_username = gr.Textbox(label="Username")
                    volunteer_name = gr.Textbox(label="Volunteer display name")
                    volunteer_password = gr.Textbox(label="Initial password — at least 12 characters", type="password")
                    volunteer_roles = gr.CheckboxGroup(list(ROLE_SLOTS), label="Verified training for these roles")
                    volunteer_confidentiality = gr.Checkbox(label="Confidentiality agreement is recorded")
                    volunteer_email = gr.Textbox(label="Email (optional; SMTP delivery is disabled by default)")
                    volunteer_add = gr.Button("Create volunteer account")
                gr.Markdown("### Operational observability")
                gr.Markdown(f"[Open Grafana dashboard]({settings.grafana_url}) · OpenTelemetry traces include operational metadata only.")
                telemetry_status = gr.Textbox(label="Telemetry delivery status", interactive=False, lines=7)
                telemetry_refresh = gr.Button("Refresh observability")
                reminders_run = gr.Button("Run reminder coordinator now")
                audit_table = gr.Dataframe(headers=["Actor", "Action", "Time (UTC)"], interactive=False, wrap=True)
                lesson = gr.Textbox(label="Lesson for the program library", lines=3)
                lesson_reviewed = gr.Checkbox(label="I reviewed this text for identifying or sensitive details")
                lesson_publish = gr.Button("Publish reviewed learning")
            learning_table = gr.Dataframe(headers=["Reviewed lesson", "Published (UTC)"], interactive=False, wrap=True)

        overview_outputs = [opportunities, case_title, stage_strip, briefing, report, team, role_id,
                            personal_tasks, task_id, consent_table, event_table, event_id, repair_table,
                            repair_id, draft_table, draft_id, owner, notes_table, audit_table, support_needs, readiness]
        draft_outputs = [draft_summary, draft_questions, draft_actions, draft_guidance, draft_sources, draft_status]

        # Annotated request injection remains on original functions; wrappers keep signatures below.
        import functools
        def bind(fn):
            wrapped = safely(fn)
            return functools.wraps(fn)(wrapped)

        initial = demo.load(bind(load), outputs=[who, case_id, manager_group, claim], api_name="session")
        initial.then(bind(overview), inputs=case_id, outputs=overview_outputs)
        demo.load(bind(inbox), outputs=[inbox_table, notification_id])
        case_id.change(bind(overview), inputs=case_id, outputs=overview_outputs, api_name="case_view")
        refresh.click(bind(overview), inputs=case_id, outputs=overview_outputs)
        draft_id.change(bind(draft_view), inputs=draft_id, outputs=draft_outputs, api_name="draft_view")
        inbox_refresh.click(bind(inbox), outputs=[inbox_table, notification_id])
        complete_task.click(mutation(service.complete_task, "Task completed. Refresh the case for the updated list."), task_id, status)
        mark_read.click(mutation(service.read_notification, "Reminder marked read."), notification_id, status)
        calendar_download.click(bind(personal_calendar), outputs=calendar_file, api_name="calendar_export")
        sync_calendar.click(bind(google_sync), outputs=status)
        claim_button.click(optional_mutation(service.claim_role, "Role request submitted for manager review.", 3), [case_id, claim, conflict], status)
        reviewed.click(mutation(service.mark_reviewed, "Case review recorded."), case_id, status)
        role_review.click(bind(role_review_action), [role_id, role_decision], status)
        role_release.click(mutation(service.release_role, "Role released. Access and calendar membership were updated."), role_id, status)
        save_consent.click(mutation(service.record_consent, "Participant choice recorded. Refresh the case to see its current stage."),
                           [case_id, party, consent_status], status)
        save_readiness.click(mutation(service.set_readiness, "Human readiness decision recorded."), [case_id, readiness], status)
        advance.click(mutation(service.advance, "Case moved to the next stage. Refresh the case."), case_id, status)
        pause.click(mutation(service.pause, "Coordination paused and scheduled meetings cancelled."), case_id, status)
        resume.click(mutation(service.resume, "Case resumed. Confirm readiness and new meeting arrangements as needed."), case_id, status)
        close_alternative.click(mutation(service.close_alternative, "Alternative closure recorded."), [case_id, closure_reason], status)
        generate_ai.click(bind(generate), [case_id, ai_task], [draft_id, status], api_name="generate_draft")
        review_ai.click(bind(review), [draft_id, draft_summary, draft_questions, draft_actions, decision, draft_checked, review_note],
                        status, api_name="review_draft")
        summary_export.click(bind(summary_export_action), draft_id, summary_file)
        availability_save.click(mutation(service.add_availability, "Your availability was saved."), [available_start, available_end], status)
        find_slots.click(bind(slots), case_id, slot_table)
        schedule.click(mutation(service.schedule, "Meeting scheduled. Export or sync your personal calendar."),
                       [case_id, event_kind, event_start, event_end, location, meeting_confirmed], status)
        reschedule.click(optional_mutation(service.schedule, "Meeting rescheduled with the same calendar ID.", 7),
                         [case_id, event_kind, event_start, event_end, location, meeting_confirmed, event_id], status)
        finish_event.click(mutation(service.finish_event, "Meeting completion recorded; team debrief check-ins created."), event_id, status)
        cancel_event.click(bind(cancel_event_action), event_id, status)
        debrief_save.click(bind(save_debrief),
                           [case_id, happened, agreed, barriers, private_note, auto_followup], [status, draft_id],
                           api_name="save_debrief")
        repair_save.click(mutation(service.add_repair, "Participant-agreed repair recorded."),
                          [case_id, commitment, owner, repair_due, verification, repair_confirmed], status)
        repair_update.click(mutation(service.update_repair, "Repair progress recorded."), [repair_id, repair_status, evidence], status)
        task_add.click(mutation(service.add_task, "Volunteer task assigned."), [case_id, task_title, owner, task_due], status)
        create_case.click(bind(new_case), [new_alias, new_brief, new_report, new_policy, new_ai, new_synthetic, new_guardian, new_support],
                          [case_id, status])
        volunteer_add.click(optional_mutation(service.add_user, "Volunteer account created.", 6),
                            [volunteer_username, volunteer_name, volunteer_password, volunteer_roles, volunteer_confidentiality, volunteer_email], status)
        telemetry_refresh.click(bind(observations), outputs=[telemetry_status, learning_table])
        reminders_run.click(bind(reminder_tick), outputs=status)
        lesson_publish.click(mutation(service.publish_learning, "Reviewed learning added to the program library."),
                             [case_id, lesson, lesson_reviewed], status)

    demo.queue(default_concurrency_limit=4, max_size=32)
    return demo, CSS

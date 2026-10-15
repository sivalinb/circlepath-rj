import argparse
import json
from contextlib import asynccontextmanager
from pathlib import Path

import gradio as gr
import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from prometheus_client import make_asgi_app

from . import __version__
from .agents import Coordinator
from .calendar import connect_google
from .config import Settings
from .models import DomainError
from .seed import seed
from .service import Service
from .store import Store
from .telemetry import Telemetry
from .ui import build_ui
from .worker import ReminderWorker


def runtime(settings: Settings):
    settings.prepare()
    store = Store(settings.db_path)
    telemetry = Telemetry(settings)
    service = Service(store, telemetry)
    coordinator = Coordinator(service, settings)
    worker = ReminderWorker(store, telemetry, settings)
    return service, coordinator, worker


def app_for(settings: Settings, *, seed_demo: bool = True):
    service, coordinator, worker = runtime(settings)
    if seed_demo:
        result = seed(service, settings)
        if result["seeded"]:
            print("Generated fictional demo data. Login instructions:", result["credentials_file"])
    scheduler = BackgroundScheduler(timezone="UTC")

    @asynccontextmanager
    async def lifespan(app):
        scheduler.add_job(worker.tick, "interval", seconds=60, max_instances=1, coalesce=True)
        # The mail adapter remains inactive unless explicitly configured.
        if settings.smtp_enabled:
            scheduler.add_job(worker.deliver_email, "interval", seconds=120, max_instances=1)
        scheduler.start()
        worker.tick()
        yield
        scheduler.shutdown(wait=False)
        service.telemetry.shutdown()

    app = FastAPI(title="CirclePath", version=__version__, lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "circlepath", "version": __version__,
                "ai_provider": settings.ai_provider}

    app.mount("/metrics", make_asgi_app(registry=service.telemetry.registry))
    demo, css = build_ui(service, coordinator, worker, settings)
    exports = settings.data_dir / "exports"
    exports.mkdir(exist_ok=True, mode=0o700)
    blocked = [settings.db_path, settings.data_dir / "traces.jsonl",
               settings.data_dir / "demo-credentials.json", settings.data_dir / "demo-login.txt",
               settings.data_dir / "calendar"]
    for filename in [settings.gemini_api_key_file, settings.arize_api_key_file,
                     settings.google_oauth_client_file]:
        if filename:
            blocked.append(Path(filename).expanduser())
    app = gr.mount_gradio_app(
        app, demo, path="/", auth=service.store.authenticate,
        auth_message="CirclePath local demo. Use a generated account from data/demo-login.txt.",
        server_name=settings.host, server_port=settings.port,
        allowed_paths=[str(exports.resolve())], blocked_paths=[str(p.resolve()) for p in blocked],
        show_error=False, max_file_size="5mb", footer_links=[],
        theme=gr.themes.Soft(primary_hue="emerald", secondary_hue="stone", neutral_hue="stone"), css=css,
    )
    app.state.circlepath = service
    return app


def main():
    parser = argparse.ArgumentParser(description="CirclePath local restorative coordination demo")
    parser.add_argument("command", choices=["serve", "seed", "calendar-connect"], nargs="?", default="serve")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--provider", choices=["gemini", "offline"])
    parser.add_argument("--port", type=int)
    parser.add_argument("--username", default="")
    args = parser.parse_args()
    settings = Settings()
    if args.data_dir:
        settings.data_dir = args.data_dir
    if args.provider:
        settings.ai_provider = args.provider
    if args.port:
        settings.port = args.port
    try:
        if args.command == "seed":
            service, _, _ = runtime(settings)
            print(json.dumps(seed(service, settings), indent=2))
            service.telemetry.shutdown()
        elif args.command == "calendar-connect":
            store = Store(settings.db_path)
            if not store.user(args.username):
                raise DomainError("Choose an existing CirclePath username with --username.")
            connect_google(settings, args.username)
            print("Google Calendar connected for the selected local account.")
        else:
            uvicorn.run(app_for(settings), host=settings.host, port=settings.port, log_level="warning")
    except DomainError as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()

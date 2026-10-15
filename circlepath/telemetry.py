"""Manually instrument only operational metadata. Narrative text never enters a span."""

import json
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Status, StatusCode
from prometheus_client import CollectorRegistry

from .config import Settings

SAFE_ATTRIBUTES = {
    "case.id", "workflow.stage", "ai.task", "ai.provider", "ai.model",
    "ai.input_tokens", "ai.output_tokens", "ai.source_count", "ai.action_count",
    "ai.references_valid", "error.type", "human.decision", "notification.count",
    "openinference.span.kind", "llm.model_name", "llm.provider",
    "llm.token_count.prompt", "llm.token_count.completion", "llm.token_count.total",
    "evaluation.reference_integrity", "evaluation.allowed_owners",
}


class JSONExporter(SpanExporter):
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)

    def export(self, spans):
        with self.lock, self.path.open("a") as out:
            for span in spans:
                out.write(json.dumps({
                    "name": span.name,
                    "trace_id": f"{span.context.trace_id:032x}",
                    "span_id": f"{span.context.span_id:016x}",
                    "parent_span_id": f"{span.parent.span_id:016x}" if span.parent else None,
                    "start_time": span.start_time,
                    "duration_ms": (span.end_time - span.start_time) / 1e6,
                    "status": span.status.status_code.name,
                    "attributes": {
                        k: v for k, v in span.attributes.items() if k in SAFE_ATTRIBUTES
                    },
                }) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


class TrackedExporter(SpanExporter):
    def __init__(self, exporter: SpanExporter):
        self.exporter = exporter
        self.successful_batches = 0
        self.failed_batches = 0

    def export(self, spans):
        try:
            result = self.exporter.export(spans)
        except Exception:
            result = SpanExportResult.FAILURE
        if result == SpanExportResult.SUCCESS:
            self.successful_batches += 1
        else:
            self.failed_batches += 1
        return result

    def shutdown(self):
        self.exporter.shutdown()


class Telemetry:
    def __init__(self, settings: Settings):
        settings.prepare()
        self.provider = TracerProvider(resource=Resource.create({
            "service.name": "circlepath",
            "service.version": "0.1.0",
            "deployment.environment.name": "local-demo",
            "openinference.project.name": settings.arize_project,
        }))
        self.provider.add_span_processor(SimpleSpanProcessor(
            JSONExporter(settings.data_dir / "traces.jsonl")
        ))
        self.arize = None
        if settings.arize_space_id and settings.arize_key():
            self.arize = TrackedExporter(OTLPSpanExporter(
                endpoint=settings.arize_endpoint,
                headers={"arize-space-id": settings.arize_space_id,
                         "arize-api-key": settings.arize_key()},
                timeout=10,
            ))
            self.provider.add_span_processor(BatchSpanProcessor(
                self.arize, schedule_delay_millis=2000
            ))
        if settings.otlp_endpoint:
            self.provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
                endpoint=settings.otlp_endpoint, timeout=5
            )))
        self.tracer = self.provider.get_tracer("circlepath")
        self.registry = CollectorRegistry()
        reader = PrometheusMetricReader(registry=self.registry)
        self.meter_provider = MeterProvider(metric_readers=[reader])
        meter = self.meter_provider.get_meter("circlepath")
        self.ai_requests = meter.create_counter("circlepath_ai_requests")
        self.ai_tokens = meter.create_counter("circlepath_ai_tokens")
        self.ai_latency = meter.create_histogram("circlepath_ai_latency", unit="s")
        self.reviews = meter.create_counter("circlepath_draft_reviews")
        self.reminders = meter.create_counter("circlepath_reminders")
        self.operations = meter.create_counter("circlepath_operations")
        self.last_ai_trace = ""

    def gauges(self, store):
        from opentelemetry.metrics import Observation
        meter = self.meter_provider.get_meter("circlepath")

        def cases(_):
            for row in store.rows("SELECT stage,COUNT(*) n FROM cases GROUP BY stage"):
                yield Observation(row["n"], {"stage": row["stage"]})

        def overdue(_):
            from .store import stamp
            row = store.one(
                "SELECT COUNT(*) n FROM tasks t JOIN cases c ON c.id=t.case_id "
                "WHERE t.status='Open' AND t.due_at<? AND c.stage NOT IN ('Closed','Paused')",
                (stamp(),),
            )
            yield Observation(row["n"])

        meter.create_observable_gauge("circlepath_cases", callbacks=[cases])
        meter.create_observable_gauge("circlepath_overdue_tasks", callbacks=[overdue])

    @contextmanager
    def operation(self, name: str, attributes: dict | None = None):
        safe = {k: v for k, v in (attributes or {}).items() if k in SAFE_ATTRIBUTES}
        if "case.id" in safe and not re.fullmatch(r"[0-9a-f]{32}", str(safe["case.id"])):
            safe.pop("case.id")
        started = time.monotonic()
        with self.tracer.start_as_current_span(
            name, attributes=safe, record_exception=False, set_status_on_exception=False
        ) as span:
            try:
                yield span
                span.set_status(Status(StatusCode.OK))
                self.operations.add(1, {"operation": name, "outcome": "success"})
            except Exception as exc:
                span.set_attribute("error.type", type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR, "Operation failed"))
                self.operations.add(1, {"operation": name, "outcome": "error"})
                raise
            finally:
                if name == "agent.generate":
                    self.ai_latency.record(time.monotonic() - started,
                                           {"task": safe.get("ai.task", "unknown")})

    def status(self) -> dict:
        return {
            "arize_configured": self.arize is not None,
            "arize_successful_batches": self.arize.successful_batches if self.arize else 0,
            "arize_failed_batches": self.arize.failed_batches if self.arize else 0,
            "last_ai_trace": self.last_ai_trace,
            "narrative_capture": False,
        }

    def flush(self):
        self.provider.force_flush(timeout_millis=15000)

    def shutdown(self):
        self.flush()
        self.provider.shutdown()
        self.meter_provider.shutdown()

import json

import pytest

from circlepath.__main__ import runtime
from circlepath.config import Settings
from circlepath.seed import seed


@pytest.fixture
def demo(tmp_path):
    settings = Settings(data_dir=tmp_path, ai_provider="offline", arize_space_id="",
                        gemini_api_key_file="", arize_api_key_file="", otlp_endpoint="")
    service, coordinator, worker = runtime(settings)
    result = seed(service, settings)
    credentials = json.loads((tmp_path / "demo-credentials.json").read_text())
    yield service, coordinator, worker, settings, result["case_ids"], credentials
    service.telemetry.shutdown()

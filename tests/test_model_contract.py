import json

import pytest

from circlepath.agents import provider_schema
from circlepath.models import AIDraft


def test_gemini_wire_schema_and_local_strict_validation():
    schema = provider_schema()
    assert "additionalProperties" not in json.dumps(schema)
    assert "$defs" in schema
    with pytest.raises(ValueError):
        AIDraft.model_validate({"title": "A draft", "summary": "A summary", "execute_tool": "send"})

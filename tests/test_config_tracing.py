"""Configuration from the environment, the schemas and the trace log. No network."""

import io
import json

import pytest

from app.config import LIGHTNING_LANE, ULTRA_LANE, ConfigError, Settings
from app.schemas import TRACE_FIELDS, RunRequest, TraceEvent
from app.tracing import Tracer


def test_defaults_pin_nemotron_lanes():
    s = Settings.from_env({})
    assert s.lane_for("Analyst") == LIGHTNING_LANE
    assert s.lane_for("Evaluator") == ULTRA_LANE
    assert s.zetaris_timeout_s == 45 and s.meterless_timeout_s == 60
    assert s.sample_mode is False and s.port == 8000


def test_routing_mode_does_not_unpin_the_lane():
    assert Settings.from_env({"METERLESS_MODEL": "meterless/auto"}).default_lane == LIGHTNING_LANE
    pinned = "nvidia/nemotron-3.5-bolt-e4b"
    assert Settings.from_env({"METERLESS_MODEL": pinned}).default_lane == pinned


def test_evaluator_lane_must_be_nemotron():
    with pytest.raises(ConfigError):
        Settings.from_env({"METERLESS_EVALUATOR_MODEL": "meterless/deep"})


@pytest.mark.parametrize("key,value", [("ZETARIS_TIMEOUT_S", "5"), ("METERLESS_TIMEOUT_S", "120"), ("PORT", "x")])
def test_out_of_range_settings_raise(key, value):
    with pytest.raises(ConfigError):
        Settings.from_env({key: value})


def test_secrets_never_print():
    s = Settings.from_env({"ZETARIS_API_KEY": "zz-not-a-real-key", "SAMPLE_MODE": "true"})
    assert "zz-not-a-real-key" not in repr(s) and "zz-not-a-real-key" not in str(s.model_dump())
    assert s.zetaris_api_key.get_secret_value() == "zz-not-a-real-key"
    assert s.sample_mode is True


def test_run_request_shape():
    req = RunRequest.model_validate({"run_id": "r1", "input": {"query": "Check AAPL revenue"}, "options": {"sample_mode": True}})
    assert req.options.sample_mode is True and req.input.companies == []
    with pytest.raises(ValueError):
        RunRequest.model_validate({"run_id": "../etc", "input": {"query": "x"}})


def test_tracer_writes_file_and_stdout_with_twelve_fields(tmp_path):
    out = io.StringIO()
    tracer = Tracer("run-1", log_dir=tmp_path, stream=out)
    tracer.emit("Retriever", "query_zetaris", input_summary="SELECT 1", output_summary="1 row",
                target_agent="Analyst", details={"row_count": 1})
    tracer.emit("Analyst", "explain", model="nvidia/nemotron-x", receipt={"routed_model": "nvidia/nemotron-x"},
                status="needs_validation", confidence=0.7, retry_count=1)
    lines = (tmp_path / "trace-run-1.jsonl").read_text().splitlines()
    assert lines == out.getvalue().splitlines()
    first, second = (json.loads(line) for line in lines)
    assert list(first)[:12] == list(TRACE_FIELDS)
    assert "receipt" not in first and first["details"] == {"row_count": 1}
    assert first["trace_id"] == second["trace_id"] == tracer.trace_id
    assert second["receipt"]["routed_model"] == second["model"]
    assert second["status"] == "needs_validation" and second["retry_count"] == 1


def test_tracer_rejects_path_like_run_ids(tmp_path):
    for bad in ("../x", "a/b", "", ".."):
        with pytest.raises(ValueError):
            Tracer(bad, log_dir=tmp_path, stream=None)


def test_trace_event_rejects_unknown_status():
    with pytest.raises(ValueError):
        TraceEvent(trace_id="t", run_id="r", agent_name="a", action="b", status="done")

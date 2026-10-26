"""Sample mode: the three example runs replayed from data/sample/ with an empty
environment and no network. The agents and the engine run unchanged; only the clients
are the replay ones. A changed prompt or a request the sample does not hold fails, naming
the key, instead of answering with a stale or invented result."""

import json
import socket
from pathlib import Path

import pytest

from app.adapters.meterless import ChatResult, MeterlessError
from app.adapters.sample_data import (SAMPLE_DIR, SampleChat, SampleMiss, SampleStore, SampleZetaris, prompt_key,
                                      statement_key)
from app.adapters.zetaris import ZetarisError
from app.config import Settings
from app.orchestration import graph
from app.orchestration.graph import run_request
from app.schemas import RunRequest, RunResponse
from app.tracing import NullTracer

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / "input_examples").glob("example_*.json"))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("sample mode opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def replay(tmp_path, path, env=None, **options):
    """One example through run_request, as the server runs it, with only these settings."""
    settings = Settings.from_env({"LOG_DIR": str(tmp_path), **(env if env is not None else {"SAMPLE_MODE": "true"})})
    body = json.loads(path.read_text())
    body.setdefault("options", {}).update(options)
    response = run_request(RunRequest.model_validate(body), settings, stream=None)
    trace = [json.loads(line) for line in (tmp_path / f"trace-{body['run_id']}.jsonl").read_text().splitlines()]
    return response, trace


def test_three_examples_are_declared_and_captured():
    manifest = SampleStore().manifest
    assert [r["run_id"] for r in manifest["runs"]] == ["example_1", "example_2", "example_3"]
    assert all(r["status"] == "success" for r in manifest["runs"])
    assert len(EXAMPLES) == 3 and "subset" in (ROOT / "data" / "README.md").read_text()


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_each_example_replays_with_an_empty_environment(tmp_path, path):
    response, trace = replay(tmp_path, path)
    assert response.status == "success", response.error
    RunResponse.model_validate(response.model_dump(mode="json"))
    assert response.agents_used == ["Planner", "Retriever", "Analyst", "Evaluator", "Reporter"]
    assert all(s.kind == "sample" for f in response.result.findings for s in f.sources)
    assert any(line.startswith("Sample mode:") for line in response.result.limitations)
    calls = [e for e in trace if e["action"] in ("query_zetaris", "model_call")]
    assert calls and all(e["details"]["source"] == "sample" for e in calls)
    assert not any(e["status"] == "failed" for e in trace)


def test_the_three_outputs_differ_and_the_traces_keep_the_collaboration(tmp_path):
    runs = {p.stem: replay(tmp_path, p) for p in EXAMPLES}
    summaries = {name: r.result.summary for name, (r, _) in runs.items()}
    assert len(set(summaries.values())) == 3
    assert summaries["example_1"].startswith("Apple: the Revenues series stops at 2018-09-29 and continues under")
    assert summaries["example_2"].startswith("Oracle: no problem found")
    assert summaries["example_3"].startswith("Target:") and "Ford:" in summaries["example_3"]
    assert runs["example_2"][0].result.findings == []
    for name in ("example_1", "example_3"):
        response, trace = runs[name]
        acts = {(e["agent_name"], e["action"]) for e in trace}
        handoffs = {(e["agent_name"], e["target_agent"]) for e in trace if e["target_agent"]}
        assert {("Planner", "Retriever"), ("Retriever", "Analyst"), ("Analyst", "Evaluator"),
                ("Evaluator", "Analyst"), ("Evaluator", "Reporter")} <= handoffs, name
        assert ("Evaluator", "critique") in acts, name
        assert ("Evaluator", "request_retry") in acts and ("Orchestrator", "enqueue_retry") in acts, name
        assert ("Evaluator", "send_back") in acts, name
        assert response.collaboration.feedback_loop_completed and response.collaboration.revision_count >= 1
    assert ("Evaluator", "retry_fallback") in {(e["agent_name"], e["action"]) for e in runs["example_3"][1]}, \
        "Ford has no second concept registered"


def test_the_request_option_turns_on_sample_mode_too(tmp_path):
    response, _ = replay(tmp_path, EXAMPLES[1], env={}, sample_mode=True)
    assert response.status == "success"


def test_a_changed_prompt_fails_loudly_instead_of_replaying_a_stale_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(graph, "EXPLAIN_SYSTEM", graph.EXPLAIN_SYSTEM + " Be brief.")
    response, trace = replay(tmp_path, EXAMPLES[1])
    assert response.status == "failed" and response.result is None
    assert response.error.type == "sample_miss" and not response.error.recoverable
    assert "prompt " in response.error.message and "Analyst" in response.error.message
    assert "capture_sample.py" in response.error.message
    assert trace[-1]["action"] == "sample_miss"


def test_a_request_the_sample_does_not_hold_fails_naming_the_statement(tmp_path):
    settings = Settings.from_env({"LOG_DIR": str(tmp_path), "SAMPLE_MODE": "true"})
    request = RunRequest(run_id="ibm", input={"query": "Can I trust IBM's revenue series?", "companies": ["IBM"]})
    response = run_request(request, settings, stream=None)
    assert response.status == "failed" and response.error.type == "sample_miss"
    assert "statement " in response.error.message and "ibm_revenue_table" in response.error.message


def test_stored_answers_and_failures_replay_as_captured(tmp_path):
    store = SampleStore(tmp_path)
    store.put_result("SELECT 1 FROM t", ["x"], [[1]], total=1, run_id="r")
    store.put_result("SELECT 2 FROM t", None, None, error="ZetarisError: HTTP 503 after 3 attempts", run_id="r")
    messages = [{"role": "user", "content": "hello"}]
    live = ChatResult("hi", "nvidia/nemotron-3.5-lightning-30b-a3b", "nvidia/nemotron-3.5-lightning-30b-a3b",
                      {"routed_model": "nvidia/nemotron-3.5-lightning-30b-a3b"}, finish_reason="stop")
    store.put_completion("Analyst", messages, live, purpose="p", run_id="r")
    store.put_completion("Evaluator", messages, error="MeterlessError: 502 empty_response", run_id="r")
    zetaris, chat = SampleZetaris(store, NullTracer()), SampleChat(store, NullTracer())
    assert zetaris.query("SELECT  1\nFROM t;").rows == [[1]], "whitespace and a trailing ; do not change the key"
    with pytest.raises(ZetarisError, match="503"):
        zetaris.query("SELECT 2 FROM t")
    assert chat.chat("Analyst", messages).text == "hi"
    with pytest.raises(MeterlessError, match="502"):
        chat.chat("Evaluator", messages)
    with pytest.raises(SampleMiss):
        chat.chat("Analyst", [{"role": "user", "content": "hello!"}])
    assert zetaris.misses == [] and len(chat.misses) == 1


def test_the_sample_files_are_named_by_their_keys():
    for path in (SAMPLE_DIR / "zetaris").glob("*.json"):
        assert json.loads(path.read_text())["key"] == statement_key(json.loads(path.read_text())["statement"])
    assert prompt_key("Analyst", [{"role": "user", "content": "a"}]) != prompt_key("Evaluator", [{"role": "user", "content": "a"}])

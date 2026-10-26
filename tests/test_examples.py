"""The three example outputs in output_examples/ are evidence, not inputs: saved from the
live runs whose traces are in logs/, and never read by the code that answers a request.
Sample mode reproduces them from data/sample/ instead."""

import json
import os
import socket
import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.orchestration.graph import run_request
from app.schemas import RunRequest, RunResponse

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "output_examples"
NAMES = ["example_1", "example_2", "example_3"]
RUNTIME = [ROOT / "run.py", *sorted((ROOT / "app").rglob("*.py"))]

_opened: list[str] | None = None  # set while a test watches the process's file opens


def _watch(event, args):
    if _opened is not None and event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        _opened.append(os.fsdecode(os.fspath(args[0])))


sys.addaudithook(_watch)  # an audit hook cannot be removed; it records only while `_opened` is a list


def output(name):
    return json.loads((OUTPUTS / f"{name}_output.json").read_text())


def test_runtime_code_never_names_output_examples():
    for path in RUNTIME:
        assert "output_examples" not in path.read_text(), path.relative_to(ROOT)
    assert "!output_examples" not in (ROOT / ".dockerignore").read_text(), "the image does not carry them"


def test_a_sample_run_opens_nothing_under_output_examples(tmp_path, monkeypatch):
    global _opened
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network"))
    settings = Settings.from_env({"LOG_DIR": str(tmp_path), "SAMPLE_MODE": "true"})
    _opened = []
    try:
        for name in NAMES:
            body = json.loads((ROOT / "input_examples" / f"{name}.json").read_text())
            assert run_request(RunRequest.model_validate(body), settings, stream=None).status == "success"
        opened = list(_opened)
    finally:
        _opened = None
    assert any("data/sample" in p for p in opened), "the watch sees the sample being read"
    assert not [p for p in opened if "output_examples" in p]


@pytest.mark.parametrize("name", NAMES)
def test_each_output_is_the_live_run_whose_trace_is_kept(name):
    saved = output(name)
    RunResponse.model_validate(saved)
    assert saved["run_id"] == name and saved["status"] == "success"
    trace = [json.loads(line) for line in (ROOT / saved["log_path"]).read_text().splitlines()]
    assert {e["trace_id"] for e in trace} == {saved["trace_id"]}, "one run per trace file"
    assert trace[0]["action"] == "start_run" and trace[-1]["action"] == "finish_run"
    assert all(e.get("details", {}).get("source") != "sample" for e in trace), "the kept trace is the live one"
    assert all(s["kind"] == "zetaris" for f in saved["result"]["findings"] for s in f["sources"])
    assert (ROOT / "logs" / f"report-{name}.md").is_file() and (ROOT / "logs" / f"markovian-{name}.jsonl").is_file()


def test_the_three_outputs_differ():
    summaries = [output(n)["result"]["summary"] for n in NAMES]
    findings = [len(output(n)["result"]["findings"]) for n in NAMES]
    assert len(set(summaries)) == 3 and findings[1] == 0 and findings[0] > 0 and findings[2] > findings[0]


@pytest.mark.parametrize("name", NAMES)
def test_sample_mode_reproduces_the_saved_output(tmp_path, name):
    settings = Settings.from_env({"LOG_DIR": str(tmp_path), "SAMPLE_MODE": "true"})
    body = json.loads((ROOT / "input_examples" / f"{name}.json").read_text())
    replayed = run_request(RunRequest.model_validate(body), settings, stream=None)
    saved = output(name)
    assert replayed.result.summary == saved["result"]["summary"]
    assert [f.id for f in replayed.result.findings] == [f["id"] for f in saved["result"]["findings"]]
    assert replayed.collaboration.model_dump() == saved["collaboration"]

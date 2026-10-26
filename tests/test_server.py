"""`POST /run` over a real socket on 127.0.0.1, with the run itself faked: the routes, the
error shape, the HTTP status mapping, the wall-clock timeout and the in-flight guard."""

import json
import threading
import time
from http.client import HTTPConnection

import pytest

from app.config import Settings
from app.schemas import Collaboration, RunError, RunResponse, RunResult
from app.server import make_server


def fake_runner(delay=0.0, status="success", error=None):
    calls = []

    def runner(request, settings, stream=None, trace_id=None):
        calls.append((request, trace_id))
        time.sleep(delay)
        return RunResponse(
            run_id=request.run_id, trace_id=trace_id or "t", status=status,
            agents_used=["Planner", "Retriever", "Analyst", "Evaluator", "Reporter"],
            result=None if error else RunResult(summary="Apple: ok.", confidence=0.9),
            collaboration=Collaboration(feedback_loop_completed=True, revision_count=1),
            log_path=f"logs/trace-{request.run_id}.jsonl", execution_time_seconds=delay,
            error=RunError(type=error, message="m", recoverable=True) if error else None)

    runner.calls = calls
    return runner


@pytest.fixture
def serve(tmp_path):
    servers = []

    def start(runner, timeout_s=None):
        server = make_server(Settings(log_dir=tmp_path), host="127.0.0.1", port=0, runner=runner,
                             timeout_s=timeout_s, stream=None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def call(port, method, path, body=None, raw=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
    response = conn.getresponse()
    payload = json.loads(response.read())
    conn.close()
    return response.status, payload


REQUEST = {"run_id": "srv-1", "input": {"query": "Check Apple's revenue series", "companies": ["AAPL"]},
           "options": {"sample_mode": False}}


def test_post_run_returns_the_contract_response(serve):
    runner = fake_runner()
    port = serve(runner)
    status, body = call(port, "POST", "/run", REQUEST)
    assert status == 200
    assert body["run_id"] == "srv-1" and body["status"] == "success"
    assert set(body) >= {"run_id", "trace_id", "status", "agents_used", "result", "collaboration", "log_path",
                         "execution_time_seconds"}
    assert body["trace_id"] == runner.calls[0][1]  # the trace is named before the run starts


def test_health_and_unknown_routes(serve):
    port = serve(fake_runner())
    assert call(port, "GET", "/health") == (200, {"status": "ok"})
    status, body = call(port, "GET", "/run")
    assert status == 405 and body["error"]["type"] == "method_not_allowed"
    status, body = call(port, "POST", "/nope", REQUEST)
    assert status == 404 and body["status"] == "failed" and set(body["error"]) == {"type", "message", "recoverable"}


@pytest.mark.parametrize("raw", [b"{not json", b'{"input": {}}', b'{"run_id": "../x", "input": {"query": "q"}}', b""])
def test_bad_requests_get_the_error_shape(serve, raw):
    runner = fake_runner()
    port = serve(runner)
    status, body = call(port, "POST", "/run", raw=raw)
    assert status == 400
    assert body["status"] == "failed" and body["error"]["type"] == "invalid_input" and body["error"]["recoverable"]
    assert not runner.calls


def test_failed_runs_are_200_except_invalid_input(serve):
    port = serve(fake_runner(status="failed", error="source_unavailable"))
    status, body = call(port, "POST", "/run", REQUEST)
    assert status == 200 and body["error"]["type"] == "source_unavailable"
    port = serve(fake_runner(status="failed", error="invalid_input"))
    status, body = call(port, "POST", "/run", dict(REQUEST, run_id="srv-2"))
    assert status == 400 and body["error"]["type"] == "invalid_input"


def test_a_run_past_the_wall_clock_returns_a_timeout_error(serve, tmp_path):
    port = serve(fake_runner(delay=2.0), timeout_s=0.2)
    started = time.monotonic()
    status, body = call(port, "POST", "/run", REQUEST)
    assert time.monotonic() - started < 1.5
    assert status == 200 and body["status"] == "failed"
    assert body["error"] == {"type": "timeout", "message": body["error"]["message"], "recoverable": True}
    trace = (tmp_path / "trace-srv-1.jsonl").read_text().splitlines()
    last = json.loads(trace[-1])
    assert last["action"] == "finish_run" and last["status"] == "failed" and last["trace_id"] == body["trace_id"]


def test_the_same_run_id_cannot_run_twice_at_once(serve):
    port = serve(fake_runner(delay=0.6))
    results = {}

    def first():
        results["first"] = call(port, "POST", "/run", REQUEST)

    t = threading.Thread(target=first)
    t.start()
    time.sleep(0.2)
    status, body = call(port, "POST", "/run", REQUEST)
    t.join()
    assert status == 409 and body["error"]["type"] == "conflict"
    assert results["first"][0] == 200
    assert call(port, "POST", "/run", REQUEST)[0] == 200  # released once finished

"""The HTTP entry point: `POST /run` on `0.0.0.0:$PORT`, stdlib only.

    POST /run      body: {run_id?, input: {query, ...}, options: {sample_mode?}}
                   reply: the response of the submission contract (§4), always JSON
    GET  /health   {"status": "ok"}

HTTP status codes describe the exchange, the body's `status` describes the run:

- 200 for every run that produced a response, including `failed` runs and the capped
  ones (a run that hits its own deadline returns `partial_success` or a controlled
  `failed`; a run that overruns the server's wall clock returns `failed` with error type
  `timeout`). The body always carries `status` and, on failure, `error`.
- 400 when the request itself is wrong: not JSON, not the request shape, or a query the
  Planner cannot resolve to a company (error type `invalid_input`).
- 404 / 405 / 409 / 413 for an unknown path, a wrong method, a `run_id` already running,
  or a body over 1 MB, each with the same error shape.

Each run executes in its own worker thread and the handler waits at most the run budget
(`RUN_TIMEOUT_S`) plus a grace period. The orchestrator stops itself at `RUN_TIMEOUT_S`
(every step checks the deadline), so the wall clock here is the backstop for a call that
hangs past its own timeout; the thread cannot be killed, so it finishes in the
background and its trace keeps its last lines.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pydantic import ValidationError

from app.config import Settings
from app.orchestration.graph import run_request
from app.schemas import RunError, RunRequest, RunResponse, new_id
from app.tracing import Tracer

MAX_BODY = 1_000_000
GRACE_S = 30.0
Runner = Callable[..., RunResponse]


def error_body(kind: str, message: str, recoverable: bool, run_id: str | None = None) -> dict[str, Any]:
    """The contract's error shape for a request that never became a run."""
    return {"run_id": run_id, "status": "failed",
            "error": {"type": kind, "message": message, "recoverable": recoverable}}


def http_status(response: RunResponse) -> int:
    if response.status == "failed" and response.error and response.error.type == "invalid_input":
        return HTTPStatus.BAD_REQUEST
    return HTTPStatus.OK


class RunService:
    """Runs requests with a wall-clock cap and refuses a `run_id` that is already running
    (two runs would interleave one trace file)."""

    def __init__(self, settings: Settings, runner: Runner = run_request, timeout_s: float | None = None,
                 stream: Any = sys.stdout) -> None:
        self.settings = settings
        self.runner = runner
        self.timeout_s = timeout_s if timeout_s is not None else settings.run_timeout_s + GRACE_S
        self.stream = stream
        self._running: set[str] = set()
        self._lock = threading.Lock()

    def claim(self, run_id: str) -> bool:
        with self._lock:
            if run_id in self._running:
                return False
            self._running.add(run_id)
            return True

    def release(self, run_id: str) -> None:
        with self._lock:
            self._running.discard(run_id)

    def run(self, request: RunRequest) -> RunResponse:
        trace_id = new_id()
        started = time.monotonic()
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["response"] = self.runner(request, self.settings, stream=self.stream, trace_id=trace_id)
            except Exception as e:  # noqa: BLE001 - run_request never raises; a test runner might
                box["error"] = e
            finally:
                self.release(request.run_id)

        worker = threading.Thread(target=work, name=f"run-{request.run_id}", daemon=True)
        worker.start()
        worker.join(self.timeout_s)
        elapsed = round(time.monotonic() - started, 3)
        if "response" in box:
            return box["response"]
        tracer = Tracer(request.run_id, self.settings.log_dir, trace_id=trace_id, stream=self.stream)
        if worker.is_alive():
            kind, message = "timeout", (f"The run did not finish within {self.timeout_s:.0f} s; it was abandoned and "
                                        f"its trace holds what it did.")
        else:
            error = box.get("error")
            kind, message = "internal_error", f"{type(error).__name__}: {error}"
        tracer.emit("Server", "finish_run", input_summary=request.input.query, output_summary=f"failed: {kind}",
                    status="failed", details={"elapsed_seconds": elapsed})
        return RunResponse(run_id=request.run_id, trace_id=trace_id, status="failed", log_path=str(tracer.path),
                           execution_time_seconds=elapsed,
                           error=RunError(type=kind, message=message, recoverable=True))


class Handler(BaseHTTPRequestHandler):
    server_version = "revenue-health-agent"
    service: RunService  # set on the subclass by make_server

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        data = (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the stdlib's signature
        sys.stderr.write(f"{self.address_string()} {format % args}\n")

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's naming
        if self.path == "/health":
            self._reply(HTTPStatus.OK, {"status": "ok"})
        elif self.path == "/run":
            self._reply(HTTPStatus.METHOD_NOT_ALLOWED, error_body("method_not_allowed", "Use POST /run.", True))
        else:
            self._reply(HTTPStatus.NOT_FOUND, error_body("not_found", f"No route {self.path}; use POST /run.", True))

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/run":
            self._reply(HTTPStatus.NOT_FOUND, error_body("not_found", f"No route {self.path}; use POST /run.", True))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        error_body("invalid_input", f"The body must be JSON under {MAX_BODY} bytes.", True))
            return
        raw = self.rfile.read(length) if length else b""
        payload: Any = None
        try:
            payload = json.loads(raw.decode("utf-8") or "null")
            request = RunRequest.model_validate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as e:
            run_id = payload.get("run_id") if isinstance(payload, dict) else None
            message = (f"The request is not valid: {e.errors(include_url=False)}" if isinstance(e, ValidationError)
                       else f"The body is not JSON: {e}")
            self._reply(HTTPStatus.BAD_REQUEST, error_body("invalid_input", message, True,
                                                           run_id if isinstance(run_id, str) else None))
            return
        if not self.service.claim(request.run_id):
            self._reply(HTTPStatus.CONFLICT, error_body("conflict", f"Run {request.run_id} is already running.", True,
                                                        request.run_id))
            return
        response = self.service.run(request)
        self._reply(http_status(response), response.model_dump(mode="json"))

    def do_PUT(self) -> None:  # noqa: N802
        self._reply(HTTPStatus.METHOD_NOT_ALLOWED, error_body("method_not_allowed", "Use POST /run.", True))

    do_DELETE = do_PATCH = do_PUT


def make_server(settings: Settings, host: str = "0.0.0.0", port: int | None = None, runner: Runner = run_request,
                timeout_s: float | None = None, stream: Any = sys.stdout) -> ThreadingHTTPServer:
    service = RunService(settings, runner=runner, timeout_s=timeout_s, stream=stream)
    handler = type("RunHandler", (Handler,), {"service": service})
    server = ThreadingHTTPServer((host, settings.port if port is None else port), handler)
    server.daemon_threads = True
    return server


def serve(settings: Settings, host: str = "0.0.0.0", port: int | None = None) -> None:
    server = make_server(settings, host, port)
    bound_host, bound_port = server.server_address[:2]
    sys.stderr.write(f"revenue-health-agent: POST /run on {bound_host}:{bound_port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

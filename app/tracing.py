"""The trace log: one JSONL file per run, `logs/trace-<run_id>.jsonl`, mirrored to stdout.

Every line carries the twelve fields of the judges' format (`app.schemas.TRACE_FIELDS`).
Lines are written by code as things happen, never by hand: each adapter call, engine step
and agent decision emits one. A model call adds the Nightshift receipt (`receipt`) and
puts its `routed_model` in `model`.
"""

from __future__ import annotations

import re
import sys
import threading
from pathlib import Path
from typing import Any, TextIO

from app.schemas import TraceEvent, TraceStatus, new_id

_RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class Tracer:
    def __init__(
        self,
        run_id: str,
        log_dir: str | Path = "logs",
        trace_id: str | None = None,
        stream: TextIO | None = sys.stdout,
    ) -> None:
        if not _RUN_ID.match(run_id) or run_id in {".", ".."}:
            raise ValueError(f"run_id must match {_RUN_ID.pattern}, got {run_id!r}")
        self.run_id = run_id
        self.trace_id = trace_id or new_id()
        self.path = Path(log_dir) / f"trace-{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = stream
        self._lock = threading.Lock()
        self.events: list[TraceEvent] = []

    def emit(
        self,
        agent_name: str,
        action: str,
        *,
        input_summary: str = "",
        output_summary: str = "",
        target_agent: str | None = None,
        confidence: float | None = None,
        retry_count: int = 0,
        status: TraceStatus = "success",
        model: str | None = None,
        receipt: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> TraceEvent:
        event = TraceEvent(
            trace_id=self.trace_id,
            run_id=self.run_id,
            agent_name=agent_name,
            action=action,
            input_summary=input_summary,
            output_summary=output_summary,
            target_agent=target_agent,
            confidence=confidence,
            retry_count=retry_count,
            status=status,
            model=model,
            receipt=receipt,
            details=details,
        )
        line = event.to_json_line()
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if self._stream is not None:
                self._stream.write(line + "\n")
                self._stream.flush()
            self.events.append(event)
        return event


class NullTracer(Tracer):
    """A tracer that records events in memory only; for adapters used without a run."""

    def __init__(self) -> None:  # noqa: D401 - no file, no stream
        self.run_id = "none"
        self.trace_id = "none"
        self.path = Path("/dev/null")
        self._stream = None
        self._lock = threading.Lock()
        self.events = []

    def emit(self, agent_name: str, action: str, **kwargs: Any) -> TraceEvent:
        event = TraceEvent(trace_id=self.trace_id, run_id=self.run_id, agent_name=agent_name, action=action, **kwargs)
        with self._lock:
            self.events.append(event)
        return event

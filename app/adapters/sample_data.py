"""Sample mode's data: the Zetaris results and model completions of three live runs.

    data/sample/manifest.json          what the subset holds and where it came from
    data/sample/zetaris/<key>.json     one statement's answer, keyed by the statement's hash
    data/sample/completions/<key>.json one model call's answer, keyed by the prompt's hash

The keys are hashes of what was sent: the statement text (whitespace folded), and for a
model call the agent and the exact messages. A changed statement or prompt has another
key, so replay fails loudly instead of answering with a stale result. A call that failed
live is stored as its failure and fails the same way on replay.

`RecordingZetaris` and `RecordingChat` wrap the live clients and write what passes
through them (`scripts/capture_sample.py`); nothing at runtime writes here.
`SampleZetaris` and `SampleChat` stand in for the live clients in sample mode: same
methods, same trace events (marked `"source": "sample"`), no network. A miss is raised
to the caller, whose usual fallback runs, and kept in `misses`; `run_request` turns any
miss into a failed response naming the keys, so a stale sample never passes quietly.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from app.adapters.meterless import ChatResult, MeterlessError, compact_receipt
from app.adapters.zetaris import QueryResult, StatementRefused, ZetarisError, check_statement

SAMPLE_DIR = Path(__file__).resolve().parents[2] / "data" / "sample"
KEY_CHARS = 16


class SampleError(Exception):
    """The sample holds no answer for this statement or prompt."""


class SampleMiss(ZetarisError, MeterlessError, SampleError):
    """Raised by a replay client: no stored answer. A ZetarisError and a MeterlessError
    too, so the agents take the fallback they take for a failed call."""


def statement_key(statement: str) -> str:
    return hashlib.sha256(" ".join(statement.split()).encode("utf-8")).hexdigest()[:KEY_CHARS]


def prompt_key(agent_name: str, messages: list[dict[str, Any]]) -> str:
    body = {"agent": agent_name.strip(),
            "messages": [{"role": str(m.get("role")), "content": str(m.get("content"))} for m in messages]}
    text = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:KEY_CHARS]


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class SampleStore:
    """Reads and (for the capture script) writes the sample directory."""

    def __init__(self, root: str | Path = SAMPLE_DIR) -> None:
        self.root = Path(root)

    # --- reading --------------------------------------------------------------------------

    @property
    def manifest(self) -> dict[str, Any]:
        path = self.root / "manifest.json"
        if not path.is_file():
            raise SampleError(f"no sample data at {self.root} (manifest.json is missing)")
        return json.loads(path.read_text(encoding="utf-8"))

    @property
    def as_of(self) -> date:
        """The date the sample was captured: the window's 'latest' is computed from it, so
        replayed prompts match the recorded ones."""
        return date.fromisoformat(self.manifest["as_of"])

    def _read(self, kind: str, key: str) -> dict[str, Any] | None:
        path = self.root / kind / f"{key}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def result(self, statement: str) -> dict[str, Any] | None:
        return self._read("zetaris", statement_key(statement))

    def completion(self, agent_name: str, messages: list[dict[str, Any]]) -> dict[str, Any] | None:
        return self._read("completions", prompt_key(agent_name, messages))

    # --- writing (capture only) -----------------------------------------------------------------

    def _write(self, kind: str, key: str, record: dict[str, Any]) -> None:
        path = self.root / kind / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    def put_result(self, statement: str, headers: list[str] | None, rows: list[list[Any]] | None,
                   total: int | None = None, error: str | None = None, run_id: str | None = None) -> str:
        key = statement_key(statement)
        record: dict[str, Any] = {"key": key, "statement": statement, "captured_in": [run_id] if run_id else []}
        if error is None:
            record.update(headers=headers or [], rows=rows or [], total=total)
        else:
            record["error"] = error
        old = self._read("zetaris", key)
        if old:
            record["captured_in"] = sorted(set(old.get("captured_in", [])) | set(record["captured_in"]))
        self._write("zetaris", key, record)
        return key

    def put_completion(self, agent_name: str, messages: list[dict[str, Any]], result: Any = None,
                       error: str | None = None, purpose: str = "", run_id: str | None = None) -> str:
        key = prompt_key(agent_name, messages)
        last = next((m for m in reversed(messages) if m.get("role") == "user"), messages[-1] if messages else {})
        record: dict[str, Any] = {"key": key, "agent": agent_name, "purpose": purpose,
                                  "prompt_preview": _short(last.get("content", "")),
                                  "captured_in": [run_id] if run_id else []}
        if error is None:
            record.update(text=result.text, requested_model=result.requested_model, routed_model=result.routed_model,
                          receipt=compact_receipt(result.receipt), finish_reason=result.finish_reason,
                          usage=result.usage)
        else:
            record["error"] = error
        old = self._read("completions", key)
        if old:
            record["captured_in"] = sorted(set(old.get("captured_in", [])) | set(record["captured_in"]))
        self._write("completions", key, record)
        return key


# --- recording (scripts/capture_sample.py) -----------------------------------------------------------


class RecordingZetaris:
    """A live Zetaris client that also stores every answer, and every failure, it returns."""

    def __init__(self, inner: Any, store: SampleStore, run_id: str) -> None:
        self.inner, self.store, self.run_id = inner, store, run_id
        self.keys: list[str] = []

    def query(self, sql: str, limit: int = 5000, purpose: str = "") -> Any:
        try:
            result = self.inner.query(sql, limit=limit, purpose=purpose)
        except Exception as e:
            self.keys.append(self.store.put_result(sql, None, None, error=f"{type(e).__name__}: {e}", run_id=self.run_id))
            raise
        self.keys.append(self.store.put_result(result.statement, result.headers, result.rows, result.total,
                                               run_id=self.run_id))
        return result

    def close(self) -> None:
        self.inner.close()


class RecordingChat:
    """A live model client that also stores every completion, and every failure, by prompt."""

    def __init__(self, inner: Any, store: SampleStore, run_id: str) -> None:
        self.inner, self.store, self.run_id = inner, store, run_id
        self.keys: list[str] = []

    def chat(self, agent_name: str, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        purpose = str(kwargs.get("purpose") or "")
        try:
            result = self.inner.chat(agent_name, messages, **kwargs)
        except Exception as e:
            self.keys.append(self.store.put_completion(agent_name, messages, error=f"{type(e).__name__}: {e}",
                                                       purpose=purpose, run_id=self.run_id))
            raise
        self.keys.append(self.store.put_completion(agent_name, messages, result, purpose=purpose, run_id=self.run_id))
        return result

    def close(self) -> None:
        self.inner.close()


# --- replay (sample mode) -----------------------------------------------------------------------


class SampleZetaris:
    """The Zetaris client in sample mode: each statement's stored answer, by its hash."""

    agent_name = "Retriever"

    def __init__(self, store: SampleStore, tracer: Any) -> None:
        self.store, self.tracer = store, tracer
        self.misses: list[str] = []

    def query(self, sql: str, limit: int = 5000, purpose: str = "") -> QueryResult:
        try:
            statement = check_statement(sql)
        except StatementRefused as e:
            self.tracer.emit(self.agent_name, "refuse_statement", input_summary=_short(sql, 300),
                             output_summary=f"refused: {e}", status="needs_revision",
                             details={"statement": sql, "reason": str(e), "source": "sample"})
            raise
        record = self.store.result(statement) or self.store.result(sql)
        key = statement_key(statement)
        if record is None:
            table = re.search(r"\bFROM\s+([\w.]+)", statement, re.IGNORECASE)
            miss = f"statement {key} ({'reads ' + table.group(1) if table else _short(statement, 120)})"
            self.misses.append(miss)
            self.tracer.emit(self.agent_name, "query_zetaris", input_summary=_short(statement, 300),
                             output_summary=f"failed: not in the sample: {miss}", status="failed",
                             details={"statement": statement, "sample_key": key, "source": "sample"})
            raise SampleMiss(f"not in the sample: {miss}")
        if "error" in record:
            self.tracer.emit(self.agent_name, "query_zetaris", input_summary=_short(statement, 300),
                             output_summary=f"failed: {record['error']} (as captured)", status="failed",
                             details={"statement": statement, "error": record["error"], "sample_key": key,
                                      "source": "sample"})
            raise ZetarisError(record["error"].split(": ", 1)[-1])
        result = QueryResult(statement, list(record["headers"]), [list(r) for r in record["rows"]], record.get("total"))
        self.tracer.emit(
            self.agent_name, "query_zetaris", input_summary=_short(statement, 300),
            output_summary=f"{len(result)} rows; columns {', '.join(result.headers[:12])}"
            + (f"; {purpose}" if purpose else "") + "; from the sample",
            status="success",
            details={"statement": statement, "row_count": len(result), "headers": result.headers,
                     "sample_key": key, "source": "sample"},
        )
        return result

    def close(self) -> None:
        pass


class SampleChat:
    """The model client in sample mode: each prompt's stored completion, by its hash."""

    def __init__(self, store: SampleStore, tracer: Any) -> None:
        self.store, self.tracer = store, tracer
        self.misses: list[str] = []

    def chat(self, agent_name: str, messages: list[dict[str, Any]], *, purpose: str = "",
             target_agent: str | None = None, **_: Any) -> ChatResult:
        key = prompt_key(agent_name, messages)
        record = self.store.completion(agent_name, messages)
        last = next((m for m in reversed(messages) if m.get("role") == "user"), messages[-1] if messages else {})
        prompt = _short(last.get("content", ""), 300)
        details: dict[str, Any] = {"sample_key": key, "source": "sample", **({"purpose": purpose} if purpose else {})}
        if record is None:
            miss = f"prompt {key} ({agent_name}{', ' + purpose if purpose else ''})"
            self.misses.append(miss)
            self.tracer.emit(agent_name, "model_call", input_summary=prompt,
                             output_summary=f"failed: not in the sample: {miss}", target_agent=target_agent,
                             status="failed", details=details)
            raise SampleMiss(f"not in the sample: {miss}")
        if "error" in record:
            self.tracer.emit(agent_name, "model_call", input_summary=prompt,
                             output_summary=f"failed: {record['error']} (as captured)", target_agent=target_agent,
                             status="failed", details={**details, "error": record["error"]})
            raise MeterlessError(record["error"].split(": ", 1)[-1])
        result = ChatResult(text=record["text"], requested_model=record["requested_model"],
                            routed_model=record.get("routed_model"), receipt=record.get("receipt"),
                            finish_reason=record.get("finish_reason"), usage=record.get("usage") or {})
        self.tracer.emit(
            agent_name, "model_call", input_summary=prompt, output_summary=_short(result.text, 300),
            target_agent=target_agent, status="success" if result.on_nemotron else "needs_validation",
            model=result.routed_model, receipt=result.receipt,
            details={**details, "requested_model": result.requested_model, "finish_reason": result.finish_reason,
                     "usage": result.usage},
        )
        return result

    def close(self) -> None:
        pass

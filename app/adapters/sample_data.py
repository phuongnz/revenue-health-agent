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
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from app.adapters.meterless import compact_receipt

SAMPLE_DIR = Path(__file__).resolve().parents[2] / "data" / "sample"
KEY_CHARS = 16


class SampleError(Exception):
    """The sample holds no answer for this statement or prompt."""


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

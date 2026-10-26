"""Read-only Zetaris client over the REST API: the only code that talks to Zetaris.

- **Allowlist.** The API key is an admin key, so the client refuses anything that is not
  one read-only statement: the first keyword must be `SELECT`, `WITH`, `SHOW`,
  `DESCRIBE`, `LIST DQ`, `RUN DQ` or `SHOW DQ`, no second statement may follow a `;`, and
  no write keyword may appear outside quotes and comments.
- **Typed rows.** Zetaris returns every cell as a string (`"215639000000"`, sometimes
  `"1.2E7"`) and a separate `headers` list. The client keeps the headers and coerces the
  cells: integers, decimals and scientific notation to numbers, `true`/`false` to booleans,
  identifier columns (CIK, SIC, accession number, `*_id`, `*_code`) left as text, and any
  value with a leading zero left as text.
- **Bounded time.** 30-60 s per request, three attempts with exponential back-off of
  1-8 s on a timeout, a connection error, an HTTP 5xx or an HTTP 429; a 429 waits for
  its `Retry-After` (capped) instead. A 4xx other than 429 is the statement's fault and is
  not retried.
- **Traced.** Every statement, retry, refusal and failure is one trace event.

The request shape (`queryId`, `sql`, `source`, `limit`; bearer key, `X-Org-ID`, a fresh
`X-Request-ID`) is adapted from `scripts/zetaris_sql.py` in Zetaris's zetaris-hackathon
repository (Apache License 2.0); see THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings
from app.tracing import NullTracer, Tracer

ALLOWED_PREFIXES = ("SELECT", "WITH", "SHOW DQ", "SHOW", "DESCRIBE", "LIST DQ", "RUN DQ")
WRITE_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "MERGE", "DROP", "CREATE", "ALTER", "TRUNCATE", "GRANT",
    "REVOKE", "COMPILE", "ACTIVATE", "REFRESH", "CACHE", "UNCACHE", "LOAD", "MSCK",
)
_WRITE = re.compile(r"\b(" + "|".join(WRITE_KEYWORDS) + r")\b", re.IGNORECASE)
TEXT_COLUMNS = frozenset({"cik", "sic", "sic_code", "accession_number", "accn", "zip", "zip_code"})
MAX_RETRY_AFTER_S = 30.0
RUN_PATH = "/sql-editor/sqls/run"

_INT = re.compile(r"^[+-]?(0|[1-9]\d*)$")
_DECIMAL = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


class ZetarisError(Exception):
    """A statement failed: refused, rejected by Zetaris, or out of attempts."""


class StatementRefused(ZetarisError):
    """The statement is not one read-only statement; it was never sent."""


class _Transient(ZetarisError):
    """Worth another attempt: timeout, connection error, HTTP 5xx or 429."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# --- statement checks --------------------------------------------------------


def _scan(sql: str) -> tuple[str, int]:
    """Return the statement with comments removed and quoted text blanked, and the
    number of `;` outside quotes and comments."""
    out: list[str] = []
    semicolons = 0
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
            continue
        if c == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
            continue
        if c in "'\"`":
            j = i + 1
            while j < n:
                if sql[j] == "\\" and c != "`":
                    j += 2
                    continue
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:  # doubled quote inside the literal
                        j += 2
                        continue
                    break
                j += 1
            out.append(c + c)
            i = j + 1
            continue
        if c == ";":
            semicolons += 1
        out.append(c)
        i += 1
    return "".join(out), semicolons


def check_statement(sql: str) -> str:
    """Return the statement ready to send (one trailing `;` dropped), or raise
    StatementRefused saying why."""
    code, semicolons = _scan(sql)
    stripped = code.strip()
    if stripped.endswith(";"):
        semicolons -= 1
        stripped = stripped[:-1].rstrip()
    if not stripped:
        raise StatementRefused("empty statement")
    if semicolons:
        raise StatementRefused("more than one statement")
    head = " ".join(stripped.split()[:2]).upper()
    if not any(head == p or head.startswith(p + " ") or head.startswith(p + "(") for p in ALLOWED_PREFIXES):
        raise StatementRefused(f"not a read-only statement (starts with {head.split(' ')[0]!r})")
    hit = _WRITE.search(stripped)
    if hit:
        raise StatementRefused(f"write keyword {hit.group(1).upper()!r} outside quotes")
    text = sql.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    return text


# --- typed rows -------------------------------------------------------------


def _is_text_column(name: str) -> bool:
    low = name.lower()
    return low in TEXT_COLUMNS or low.endswith("_id") or low.endswith("_code") or low.endswith("_cik")


def coerce_cell(value: Any) -> Any:
    """A Zetaris cell, typed: numbers and booleans from their text, everything else as is."""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if _INT.match(s):
        return int(s)
    if _DECIMAL.match(s) and not re.match(r"^[+-]?0\d", s):
        number = float(s)
        if ("e" in s or "E" in s) and number.is_integer() and abs(number) < 2**63:
            return int(number)
        return number
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    return value


@dataclass
class QueryResult:
    """One statement's answer: the headers as Zetaris sent them, the rows typed."""

    statement: str
    headers: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    total: int | None = None
    time_used_ms: int | None = None
    attempts: int = 1

    @classmethod
    def from_response(cls, statement: str, out: Any, attempts: int = 1) -> "QueryResult":
        if not isinstance(out, dict):
            raise ZetarisError(f"unexpected reply shape: {type(out).__name__}")
        headers = [str(h) for h in (out.get("headers") or [])]
        text = [_is_text_column(h) for h in headers]
        rows = []
        for raw in out.get("data") or []:
            cells = list(raw) if isinstance(raw, (list, tuple)) else [raw]
            rows.append([c if (i < len(text) and text[i]) else coerce_cell(c) for i, c in enumerate(cells)])
        return cls(statement, headers, rows, out.get("total"), out.get("timeUsed"), attempts)

    @property
    def records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.headers, row)) for row in self.rows]

    def __len__(self) -> int:
        return len(self.rows)


# --- client -----------------------------------------------------------------


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After", "").strip()
    try:
        return min(max(float(raw), 0.0), MAX_RETRY_AFTER_S) if raw else None
    except ValueError:
        return None


class ZetarisClient:
    def __init__(
        self,
        settings: Settings,
        tracer: Tracer | None = None,
        agent_name: str = "Retriever",
        transport: httpx.BaseTransport | None = None,
        attempts: int = 3,
        sleep: Any = time.sleep,
    ) -> None:
        if settings.zetaris_api_key is None:
            raise ZetarisError("ZETARIS_API_KEY is not set")
        self.settings = settings
        self.tracer = tracer or NullTracer()
        self.agent_name = agent_name
        self.attempts = attempts
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=settings.zetaris_rest_url,
            timeout=httpx.Timeout(settings.zetaris_timeout_s, connect=10.0),
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ZetarisClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _post(self, sql: str, limit: int) -> Any:
        key = self.settings.zetaris_api_key.get_secret_value()  # type: ignore[union-attr]
        headers = {
            "Authorization": f"Bearer {key}",
            "X-Org-ID": self.settings.zetaris_org_id,
            "X-Request-ID": str(uuid.uuid4()),
        }
        body = {"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": limit}
        try:
            response = self._http.post(RUN_PATH, json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise _Transient(f"timed out after {self.settings.zetaris_timeout_s:g} s") from e
        except httpx.TransportError as e:
            raise _Transient(f"cannot reach Zetaris: {type(e).__name__}") from e
        if response.status_code == 429:
            raise _Transient("HTTP 429 rate limited", _retry_after(response))
        if response.status_code >= 500:
            raise _Transient(f"HTTP {response.status_code}: {_short(response.text, 200)}")
        try:
            out = response.json()
        except ValueError as e:
            raise ZetarisError(f"HTTP {response.status_code}, not JSON: {_short(response.text, 160)}") from e
        if response.status_code >= 400:
            message = out.get("msg") if isinstance(out, dict) else out
            raise ZetarisError(f"HTTP {response.status_code}: {_short(message or '', 400)}")
        return out

    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, _Transient) and exc.retry_after is not None:
            return exc.retry_after
        return wait_exponential(multiplier=1, min=1, max=8)(state)

    def query(self, sql: str, limit: int = 5000, purpose: str = "") -> QueryResult:
        """Run one read-only statement and return its typed rows."""
        try:
            statement = check_statement(sql)
        except StatementRefused as e:
            self.tracer.emit(
                self.agent_name, "refuse_statement", input_summary=_short(sql),
                output_summary=f"refused: {e}", status="needs_revision",
                details={"statement": sql, "reason": str(e)},
            )
            raise

        def before_sleep(state: RetryCallState) -> None:
            exc = state.outcome.exception() if state.outcome else None
            self.tracer.emit(
                self.agent_name, "query_zetaris", input_summary=_short(statement),
                output_summary=f"attempt {state.attempt_number} failed: {exc}; retrying",
                retry_count=state.attempt_number, status="retrying",
                details={"statement": statement, "error": str(exc)},
            )

        started = time.monotonic()
        attempt = 0
        try:
            for attempt_state in Retrying(
                stop=stop_after_attempt(self.attempts),
                wait=self._wait,
                retry=retry_if_exception(lambda e: isinstance(e, _Transient)),
                before_sleep=before_sleep,
                sleep=self._sleep,
                reraise=True,
            ):
                with attempt_state:
                    attempt = attempt_state.retry_state.attempt_number
                    out = self._post(statement, limit)
        except ZetarisError as e:
            self.tracer.emit(
                self.agent_name, "query_zetaris", input_summary=_short(statement),
                output_summary=f"failed: {e}", retry_count=attempt - 1, status="failed",
                details={"statement": statement, "error": str(e), "attempts": attempt,
                         "duration_ms": round((time.monotonic() - started) * 1000)},
            )
            raise ZetarisError(str(e)) from e

        result = QueryResult.from_response(statement, out, attempts=attempt)
        self.tracer.emit(
            self.agent_name, "query_zetaris",
            input_summary=_short(statement),
            output_summary=f"{len(result)} rows; columns {', '.join(result.headers[:12])}"
            + (f"; {purpose}" if purpose else ""),
            retry_count=attempt - 1,
            status="success",
            details={
                "statement": statement,
                "row_count": len(result),
                "headers": result.headers,
                "time_used_ms": result.time_used_ms,
                "attempts": attempt,
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return result

#!/usr/bin/env python3
"""Register data sources in Zetaris from SQL files, one statement per REST call.

Usage:
    python3 scripts/register_sources.py FILE.sql [FILE.sql ...] [--dry-run] [--skip-exists]
                                        [--limit N] [--pace SECONDS]
    python3 scripts/register_sources.py -e "SELECT 1"

Environment:
    ZETARIS_API_KEY     required, the bearer key (never in a file of this repository)
    ZETARIS_REST_URL    default http://localhost:8888/api/v1.0
    ZETARIS_ORG_ID      default 1
    ZETARIS_USER_AGENT  "app_name user@domain.tld"; required when a file carries the
                        placeholder, since SEC EDGAR and company_dns throttle or block
                        callers that do not identify themselves

The registration scripts call rate-limited public APIs. Run them once, deliberately,
never in a loop or a test. The script stops at the first error; with --skip-exists an
"already exists" error is reported and skipped instead.

Adapted from scripts/zetaris_sql.py and scripts/run_sql.py in Zetaris's
zetaris-hackathon repository (Apache License 2.0); see THIRD_PARTY_NOTICES.md. Changes:
configuration from the environment only, result headers kept, one wait-and-retry on
HTTP 429, pacing between statements, and no JDBC channel.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_REST_URL = "http://localhost:8888/api/v1.0"
PLACEHOLDER_UA = "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
USER_AGENT_FORMAT = re.compile(r"^\S.*\s[^\s@]+@[^\s@]+\.[^\s@]+$")
USER_AGENT_HELP = (
    "ZETARIS_USER_AGENT must be an app name followed by a contact e-mail address, "
    "like 'my-app me@example.com' (app_name user@domain.tld)."
)
RETRY_AFTER_429_SECONDS = 60


class ZetarisError(Exception):
    """A statement Zetaris rejected, or an endpoint that could not be reached."""


# --- SQL splitting ---------------------------------------------------------


def split_statements(text: str) -> list[str]:
    """Split SQL on semicolons outside quotes, dropping comments.

    Handles '..', "..", `..` quoting and both `-- line` and `/* block */` comments.
    A CREATE TABLE that follows COMPILE USL ... DDL is merged back into that statement,
    because a multi-table USL must be sent whole.
    """
    statements: list[str] = []
    buf: list[str] = []
    i, n, quote = 0, len(text), None
    while i < n:
        c = text[i]
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
            buf.append(c)
        elif text.startswith("--", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        elif c == ";":
            _push(statements, "".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    _push(statements, "".join(buf))
    return statements


def _push(statements: list[str], raw: str) -> None:
    stmt = raw.strip()
    if not stmt:
        return
    if (
        stmt.upper().startswith("CREATE TABLE")
        and statements
        and statements[-1].upper().startswith("COMPILE USL")
    ):
        statements[-1] += ";\n" + stmt
    else:
        statements.append(stmt)


# --- user agent ------------------------------------------------------------


def valid_user_agent(ua: str) -> bool:
    """True for `app_name user@domain.tld`: an app name, then a contact e-mail."""
    return bool(USER_AGENT_FORMAT.match(ua.strip()))


def apply_user_agent(statements: list[str], user_agent: str | None) -> list[str]:
    """Replace the placeholder with the caller's user agent.

    Refuses to send the placeholder itself, a value with quotes (it goes inside a quoted
    SQL string) and a value that does not identify the caller.
    """
    if not any(PLACEHOLDER_UA in s for s in statements):
        return statements
    ua = (user_agent or "").strip()
    if not ua:
        raise ZetarisError(
            f"The SQL contains the user-agent placeholder '{PLACEHOLDER_UA}'. "
            "Set ZETARIS_USER_AGENT. " + USER_AGENT_HELP
        )
    if '"' in ua or "'" in ua:
        raise ZetarisError("ZETARIS_USER_AGENT must not contain quotes.")
    if not valid_user_agent(ua):
        raise ZetarisError(f"ZETARIS_USER_AGENT does not identify you. {USER_AGENT_HELP}")
    return [s.replace(PLACEHOLDER_UA, ua) for s in statements]


# --- REST channel ----------------------------------------------------------


@dataclass
class Result:
    headers: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    total: int | None = None
    time_used_ms: int | None = None

    @classmethod
    def from_response(cls, out: Any) -> "Result":
        if not isinstance(out, dict):
            return cls()
        return cls(
            headers=list(out.get("headers") or []),
            rows=list(out.get("data") or []),
            total=out.get("total"),
            time_used_ms=out.get("timeUsed"),
        )


def _short(text: str, limit: int = 400) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


class RestChannel:
    """POST /sql-editor/sqls/run with a bearer key, the org id and a fresh request id."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        org_id: str | None = None,
        timeout: int = 600,
    ) -> None:
        key = api_key or os.environ.get("ZETARIS_API_KEY", "")
        if not key:
            raise ZetarisError("ZETARIS_API_KEY is not set in the environment.")
        self._key = key
        self.base = (base_url or os.environ.get("ZETARIS_REST_URL", DEFAULT_REST_URL)).rstrip("/")
        self.org = org_id or os.environ.get("ZETARIS_ORG_ID", "1")
        self.timeout = timeout

    def _call(self, path: str, body: dict | None = None) -> Any:
        req = Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            method="POST" if body is not None else "GET",
            headers={
                "Authorization": f"Bearer {self._key}",
                "X-Org-ID": self.org,
                "X-Request-ID": str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "null")
        except HTTPError as e:
            detail = _short(e.read().decode(errors="replace"))
            raise ZetarisError(f"HTTP {e.code}: {detail}") from e
        except (URLError, OSError) as e:
            raise ZetarisError(f"Cannot reach {self.base}: {getattr(e, 'reason', e)}") from e

    def run(self, sql: str, limit: int = 100) -> Result:
        """Run one statement. On HTTP 429 wait once and retry that statement once."""
        body = {"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": limit}
        try:
            return Result.from_response(self._call("/sql-editor/sqls/run", body))
        except ZetarisError as e:
            if not str(e).startswith("HTTP 429"):
                raise
            print(f"  rate limited; waiting {RETRY_AFTER_429_SECONDS}s and retrying once", file=sys.stderr)
            time.sleep(RETRY_AFTER_429_SECONDS)
            body["queryId"] = str(uuid.uuid4())
            return Result.from_response(self._call("/sql-editor/sqls/run", body))


# --- CLI -------------------------------------------------------------------


def load_statements(paths: Iterable[Path], user_agent: str | None) -> list[tuple[str, str]]:
    """(source label, statement) for every statement in the files, placeholder applied."""
    out: list[tuple[str, str]] = []
    for path in paths:
        stmts = apply_user_agent(split_statements(path.read_text()), user_agent)
        out.extend((path.name, s) for s in stmts)
    return out


def _label(stmt: str, width: int = 90) -> str:
    one = " ".join(stmt.split())
    return one if len(one) <= width else one[: width - 1] + "…"


def _is_exists_error(err: ZetarisError) -> bool:
    return "already exist" in str(err).lower()


def _print_rows(result: Result, limit: int) -> None:
    if result.headers:
        print("  " + " | ".join(result.headers))
    for row in result.rows[:limit]:
        print("  " + " | ".join("" if v is None else str(v) for v in row))
    if result.total is not None:
        print(f"  ({result.total} rows, {result.time_used_ms} ms)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path, help="SQL files, run in the order given")
    ap.add_argument("-e", "--execute", metavar="SQL", help="run this one statement instead of files")
    ap.add_argument("--dry-run", action="store_true", help="print the statements, send nothing")
    ap.add_argument("--skip-exists", action="store_true", help="skip statements whose object already exists")
    ap.add_argument("--limit", type=int, default=20, help="rows to fetch and print per statement")
    ap.add_argument("--pace", type=float, default=1.0, help="seconds to wait between statements")
    args = ap.parse_args(argv)

    if not args.files and not args.execute:
        ap.error("give SQL files or -e SQL")

    user_agent = os.environ.get("ZETARIS_USER_AGENT")
    try:
        if args.execute:
            work = [("-e", s) for s in apply_user_agent(split_statements(args.execute), user_agent)]
        else:
            work = load_statements(args.files, user_agent)
    except ZetarisError as e:
        print(f"register_sources: {e}", file=sys.stderr)
        return 2

    if args.dry_run:
        for i, (src, stmt) in enumerate(work, 1):
            print(f"[{i}/{len(work)}] {src}: {_label(stmt)}")
        print(f"dry run: {len(work)} statements, nothing sent")
        return 0

    try:
        channel = RestChannel()
    except ZetarisError as e:
        print(f"register_sources: {e}", file=sys.stderr)
        return 2

    for i, (src, stmt) in enumerate(work, 1):
        print(f"[{i}/{len(work)}] {src}: {_label(stmt)}")
        started = time.monotonic()
        try:
            result = channel.run(stmt, limit=args.limit)
        except ZetarisError as e:
            if args.skip_exists and _is_exists_error(e):
                print(f"  skipped, already exists: {_short(str(e), 160)}")
                continue
            print(f"register_sources: statement {i} failed: {e}", file=sys.stderr)
            return 1
        print(f"  ok in {time.monotonic() - started:.1f}s")
        if result.rows or result.headers:
            _print_rows(result, args.limit)
        if args.pace > 0 and i < len(work):
            time.sleep(args.pace)
    return 0


if __name__ == "__main__":
    sys.exit(main())

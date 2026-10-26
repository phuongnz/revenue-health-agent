"""The Retriever: the only part that talks to Zetaris.

It runs read-only SQL over REST through `ZetarisClient` (whose allowlist refuses anything
but `SELECT, WITH, SHOW, DESCRIBE` and the DQ reads, and which traces every statement
with its row count), and hands typed rows to the Analyst. Table names come from the
Planner's registry and window bounds are formatted by code, never pasted from a request.

Sample mode swaps the client for one that answers from cached results; this module does
not change.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.tracing import NullTracer, Tracer

AGENT = "Retriever"
REVENUE_COLUMNS = (
    "cik", "entity_name", "concept", "period_start", "period_end", "value_usd", "accession_number",
    "fiscal_year", "fiscal_period", "period_frame", "form_type", "filed_date",
)
DQ_COMPANY_TABLE = "lightning.metastore.usl_demo.sic_edgar_usl.company"
_TABLE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
_YEAR = re.compile(r"^\d{4}")


class RetrievalError(Exception):
    """A statement failed after the client's bounded retries, or was refused."""

    def __init__(self, message: str, statement: str | None = None) -> None:
        super().__init__(message)
        self.statement = statement


class QueryClient(Protocol):
    def query(self, sql: str, limit: int = 5000, purpose: str = "") -> Any: ...


@dataclass
class Rows:
    """One statement's rows as records, with the statement that produced them."""

    table: str
    statement: str
    records: list[dict[str, Any]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.records)


def _year(value: str | int | None) -> int | None:
    text = "" if value is None else str(value).strip()
    return int(text[:4]) if _YEAR.match(text) else None


def revenue_statement(table: str, period_start: str | None = None, period_end: str | None = None) -> str:
    """The SELECT for one company's revenue series, optionally cut to the requested years."""
    if not _TABLE.match(table):
        raise ValueError(f"not a registered table name: {table!r}")
    where = []
    start, end = _year(period_start), _year(period_end)
    if start:
        where.append(f"period_end >= '{start:04d}-01-01'")
    if end:
        where.append(f"period_start <= '{end:04d}-12-31'")
    return (
        f"SELECT {', '.join(REVENUE_COLUMNS)} FROM {table}"
        + (f" WHERE {' AND '.join(where)}" if where else "")
        + " ORDER BY period_end, period_start, filed_date, accession_number"
    )


class Retriever:
    name = AGENT

    def __init__(self, client: QueryClient, tracer: Tracer | None = None) -> None:
        self.client = client
        self.tracer = tracer or NullTracer()

    def _run(self, sql: str, purpose: str, limit: int = 5000) -> Any:
        try:
            return self.client.query(sql, limit=limit, purpose=purpose)
        except Exception as e:  # noqa: BLE001 - the client has traced the failure; callers take the fallback
            raise RetrievalError(f"{type(e).__name__}: {e}", statement=sql) from e

    def list_tables(self, container: str = "edgar") -> set[str]:
        """Fully qualified, lower-case names of the tables and views in a container."""
        result = self._run(f"SHOW TABLES IN {container}", "which tables exist")
        names = set()
        for record in result.records:
            name = record.get("table_identifier") or record.get("tableName") or record.get("name")
            if name:
                names.add(f"{container}.{str(name).lower()}")
        return names

    def revenue_rows(
        self, table: str, period_start: str | None = None, period_end: str | None = None,
        purpose: str = "", handoff: str | None = "Analyst",
    ) -> Rows:
        sql = revenue_statement(table, period_start, period_end)
        result = self._run(sql, purpose or f"revenue facts from {table}")
        rows = Rows(table, result.statement, result.records)
        if handoff:
            self.tracer.emit(
                self.name, "rows_ready",
                input_summary=f"{table}" + (f" ({purpose})" if purpose else ""),
                output_summary=f"{len(rows)} rows handed to the {handoff}",
                target_agent=handoff,
                details={"table": table, "statement": rows.statement, "row_count": len(rows)},
            )
        return rows

    def profile(self, company_key: str) -> Rows:
        table = f"edgar.{company_key}_profile_table"
        if not _TABLE.match(table):
            raise ValueError(f"not a registered company: {company_key!r}")
        result = self._run(f"SELECT * FROM {table}", "company profile: name and SIC", limit=10)
        return Rows(table, result.statement, result.records)

    def dq_invalid_companies(self) -> Rows:
        """The rows of the semantic layer's `company` table that fail a data-quality rule
        (the foreign key from a company's SIC code to the SIC reference)."""
        sql = f"SHOW DQ ALL INVALID TABLE {DQ_COMPANY_TABLE}"
        result = self._run(sql, "data-quality rule failures in sic_edgar_usl.company", limit=100)
        records = []
        for record in result.records:
            raw = record.get("records", record) if isinstance(record, dict) else record
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = {"raw": raw}
            if isinstance(raw, dict):
                records.append(raw)
        return Rows(DQ_COMPANY_TABLE, result.statement, records)

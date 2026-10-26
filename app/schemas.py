"""Pydantic schemas: the run request and response of the submission contract, a finding
with its sources, and the trace event in the judges' format.

The request and response shapes follow §4 of the build contract; these models are the
contract the agents are held to.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

TraceStatus = Literal[
    "success",
    "needs_revision",
    "retrying",
    "partial_success",
    "needs_validation",
    "failed",
]
"""The judges' five status values, plus `failed` for a terminal error, logged honestly."""

TRACE_FIELDS = (
    "timestamp",
    "trace_id",
    "run_id",
    "agent_name",
    "action",
    "input_summary",
    "output_summary",
    "target_agent",
    "confidence",
    "retry_count",
    "status",
    "model",
)
"""The twelve fields every trace line carries, in this order."""

RunStatus = Literal["success", "partial_success", "failed"]


def new_id() -> str:
    return uuid.uuid4().hex


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- request ----------------------------------------------------------------


class RunInput(BaseModel):
    """What to check. `query` is the free-text request; the rest narrows it."""

    model_config = ConfigDict(extra="allow")

    query: str = Field(min_length=1)
    companies: list[str] = Field(default_factory=list, description="tickers, CIKs or names")
    period_start: str | None = Field(default=None, description="fiscal year or ISO date")
    period_end: str | None = None
    focus: str | None = None


class RunOptions(BaseModel):
    model_config = ConfigDict(extra="allow")

    sample_mode: bool | None = None


class RunRequest(BaseModel):
    run_id: str = Field(default_factory=new_id, pattern=r"^[A-Za-z0-9._-]{1,128}$")
    input: RunInput
    options: RunOptions = Field(default_factory=RunOptions)


# --- findings and response ----------------------------------------------------


class Source(BaseModel):
    """Where a finding's evidence came from: a Zetaris table and the rows it rests on."""

    kind: Literal["zetaris", "sample"] = "zetaris"
    table: str
    statement: str | None = None
    row_ids: list[str] = Field(default_factory=list)


FindingKind = Literal["gap", "duplicate", "restatement", "early_stop", "late_start", "tag_change", "other"]


class Finding(BaseModel):
    id: str = Field(default_factory=new_id)
    company: str
    cik: str | None = None
    concept: str | None = None
    period: str | None = None
    kind: FindingKind
    description: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal["proposed", "accepted", "rejected", "unsupported"] = "proposed"
    sources: list[Source] = Field(default_factory=list)
    recommendation: str | None = None
    evaluator_reason: str | None = Field(default=None, description="why the Evaluator decided as it did")
    revision_count: int = Field(default=0, ge=0, description="times the Evaluator sent it back to the Analyst")


class RunResult(BaseModel):
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    limitations: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)


class Collaboration(BaseModel):
    feedback_loop_completed: bool = False
    revision_count: int = Field(default=0, ge=0)
    evaluator_findings: list[str] = Field(default_factory=list)


class RunError(BaseModel):
    type: str
    message: str
    recoverable: bool


class RunResponse(BaseModel):
    run_id: str
    trace_id: str
    status: RunStatus
    agents_used: list[str] = Field(default_factory=list)
    result: RunResult | None = None
    collaboration: Collaboration = Field(default_factory=Collaboration)
    log_path: str
    execution_time_seconds: float = Field(ge=0.0)
    error: RunError | None = None


# --- trace ------------------------------------------------------------------


class TraceEvent(BaseModel):
    """One trace line. The twelve fields are always present (null when not applicable);
    `receipt` (the Nightshift compute receipt) and `details` (statement text, row counts,
    timings) are added only when set."""

    model_config = ConfigDict(extra="forbid")

    timestamp: str = Field(default_factory=utc_now)
    trace_id: str
    run_id: str
    agent_name: str = Field(min_length=1)
    action: str = Field(min_length=1)
    input_summary: str = ""
    output_summary: str = ""
    target_agent: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    retry_count: int = Field(default=0, ge=0)
    status: TraceStatus = "success"
    model: str | None = None
    receipt: dict[str, Any] | None = None
    details: dict[str, Any] | None = None

    @field_validator("input_summary", "output_summary")
    @classmethod
    def _one_line(cls, value: str) -> str:
        value = " ".join(str(value).split())
        return value if len(value) <= 500 else value[:499] + "…"

    def to_json_line(self) -> str:
        data = self.model_dump(mode="json")
        for optional in ("receipt", "details"):
            if data.get(optional) is None:
                data.pop(optional, None)
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)

"""The run's state: the step queue, what each step found, and the carry-over block the
engine hands from one step to the next.

The carry-over is written by code from the state, not by the model: it names what was
done, what remains, what was decided, and the facts a later step needs (where each
series starts and ends), in a few lines per series. The engine bounds it to
`carryoverTokens`.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.agents.analyst import SeriesContext, facts_from_rows, short_concept
from app.agents.evaluator import Outcome
from app.agents.planner import Plan, PlanStep
from app.agents.retriever import Rows
from app.schemas import Finding, RunRequest

AGENT_ORDER = ("Planner", "Retriever", "Analyst", "Evaluator", "Reporter")


@dataclass
class StepRecord:
    number: int
    step: PlanStep
    rows: Rows | None = None
    ctx: SeriesContext | None = None
    error: str | None = None
    outcome: Outcome | None = None
    explanation: str = ""
    explanation_model: str | None = None
    step_text: str = ""

    @property
    def span(self) -> str:
        facts = facts_from_rows(self.rows.records) if self.rows else []
        if not facts:
            return "no periods"
        first = min((f.start or f.end) for f in facts)
        return f"{first}..{max(f.end for f in facts)}"

    def line(self) -> str:
        label = f"{self.step.company.name} {short_concept(self.step.concept.name)}"
        if self.error:
            return f"{label}: read failed"
        kinds = sorted({f.kind for f in (self.outcome.accepted if self.outcome else [])})
        return f"{label} ({len(self.rows or [])} rows, {self.span}: {', '.join(kinds) or 'clean'})"


@dataclass
class RunState:
    request: RunRequest
    plan: Plan | None = None
    queue: deque[PlanStep] = field(default_factory=deque)
    records: list[StepRecord] = field(default_factory=list)
    series: dict[tuple[str, str], tuple[Rows, SeriesContext]] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    explanations: dict[str, list[str]] = field(default_factory=dict)
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)  # cik without leading zeros -> profile row
    dq_records: list[dict[str, Any]] = field(default_factory=list)
    dq_statement: str | None = None
    agents: list[str] = field(default_factory=list)
    revision_count: int = 0
    fallbacks: int = 0
    reflection: str | None = None

    def use(self, agent: str) -> None:
        if agent not in self.agents:
            self.agents.append(agent)

    @property
    def agents_used(self) -> list[str]:
        return [a for a in AGENT_ORDER if a in self.agents] + [a for a in self.agents if a not in AGENT_ORDER]

    @property
    def steps_with_rows(self) -> int:
        return sum(1 for r in self.records if r.rows is not None and not r.error)

    def carryover_block(self, as_of: str) -> str:
        done = "; ".join(r.line() for r in self.records) or "nothing yet"
        remaining = "; ".join(f"{s.label} ({s.role})" for s in self.queue) or "none"
        decisions = "; ".join(self.decisions[-8:]) or "none"
        return (f"COMPLETED: {done}\nREMAINING: {remaining}\nDECISIONS: {decisions}\n"
                f"CONTEXT: as of {as_of}; findings so far {sum(len(r.outcome.accepted) for r in self.records if r.outcome)} "
                f"accepted, {self.revision_count} sent back")

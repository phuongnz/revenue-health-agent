"""The Planner: reads the request and emits the work plan, one bounded step per company
and revenue concept.

Companies are resolved by code from `input.companies` (tickers, CIKs or names) and the
free-text query; only when code finds none does the Planner ask the model to pick from
the seven registered companies. Which tables exist comes from the Retriever
(`SHOW TABLES IN edgar`), so a company whose alternate concept was never registered
(IBM, Ford) gets no alternate step and the Evaluator's retry branch takes its fallback.

Delegation by input: when the request is about tag or concept changes, both concepts are
planned from the start; otherwise only the primary concept is, and the alternate is held
back for the Evaluator's truncated-series retry.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.schemas import RunInput
from app.tracing import NullTracer, Tracer

AGENT = "Planner"


@dataclass(frozen=True)
class Company:
    key: str
    name: str
    ticker: str
    cik: str
    aliases: tuple[str, ...] = ()


COMPANIES: dict[str, Company] = {c.key: c for c in (
    Company("apple", "Apple", "AAPL", "320193", ("apple inc",)),
    Company("ibm", "IBM", "IBM", "51143", ("international business machines",)),
    Company("oracle", "Oracle", "ORCL", "1341439", ("oracle corp", "oracle corporation")),
    Company("walmart", "Walmart", "WMT", "104169", ("wal-mart", "walmart inc", "wal mart")),
    Company("target", "Target", "TGT", "27419", ("target corp", "target corporation")),
    Company("ford", "Ford", "F", "37996", ("ford motor", "ford motor co")),
    Company("tesla", "Tesla", "TSLA", "1318605", ("tesla inc", "tesla motors")),
)}


@dataclass(frozen=True)
class Concept:
    key: str
    name: str
    table_suffix: str

    def table(self, company_key: str) -> str:
        return f"edgar.{company_key}_{self.table_suffix}"


CONCEPTS: dict[str, Concept] = {
    "revenues": Concept("revenues", "us-gaap/Revenues", "revenue_table"),
    "rfcc": Concept("rfcc", "us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax", "rfcc_table"),
}
CONCEPT_ORDER = ("revenues", "rfcc")
ALL_COMPANIES = re.compile(r"\b(all|every|each)\b.{0,20}\b(compan|seven|registered)", re.IGNORECASE)
TAG_FOCUS = re.compile(r"\b(tag|concept|taxonomy|rfcc|RevenueFromContract)", re.IGNORECASE)


class PlanError(ValueError):
    """The request names no company this agent can check."""


@dataclass
class PlanStep:
    company: Company
    concept: Concept
    role: str  # "primary", "alternate" (planned from the start) or "retry" (the Evaluator's branch)
    reason: str = ""

    @property
    def table(self) -> str:
        return self.concept.table(self.company.key)

    @property
    def label(self) -> str:
        return f"{self.company.name} {self.concept.name}"


@dataclass
class Plan:
    steps: list[PlanStep]
    companies: list[Company]
    tables: set[str]
    period_start: str | None = None
    period_end: str | None = None
    unknown: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def alternate_for(self, company: Company, concept: Concept) -> Concept | None:
        """The other revenue concept for this company, when its table exists."""
        for key in CONCEPT_ORDER:
            other = CONCEPTS[key]
            if other.key != concept.key and other.table(company.key) in self.tables:
                return other
        return None

    def summary(self) -> str:
        return "; ".join(f"{s.label} ({s.role})" for s in self.steps)


def _match_token(token: str) -> Company | None:
    t = token.strip().lower()
    if not t:
        return None
    digits = t.lstrip("0")
    for c in COMPANIES.values():
        if t in (c.key, c.ticker.lower(), c.name.lower()) or t in c.aliases or (digits and digits == c.cik):
            return c
    return None


def companies_in_text(text: str) -> list[Company]:
    """Companies named in free text, in the order they appear. 'Target' must be written
    with a capital or as TGT, and Ford's one-letter ticker is not matched in prose."""
    found: list[tuple[int, Company]] = []
    for c in COMPANIES.values():
        patterns = [re.escape(a) for a in c.aliases] + [re.escape(c.key)]
        flags = re.IGNORECASE
        if c.key == "target":
            patterns, flags = [r"Target", r"TARGET"] + [re.escape(a.title()) for a in c.aliases], 0
        hits = [m.start() for p in patterns for m in re.finditer(rf"\b{p}\b", text, flags)]
        if len(c.ticker) > 1:
            hits += [m.start() for m in re.finditer(rf"\b{c.ticker}\b", text)]
        hits += [m.start() for m in re.finditer(rf"\b0*{c.cik}\b", text)]
        if hits:
            found.append((min(hits), c))
    return [c for _, c in sorted(found, key=lambda x: x[0])]



class Planner:
    name = AGENT

    def __init__(self, tracer: Tracer | None = None, chat: Any = None) -> None:
        self.tracer = tracer or NullTracer()
        self.chat = chat  # an object with MeterlessClient.chat's signature, or None

    def resolve(self, run_input: RunInput) -> tuple[list[Company], list[str]]:
        companies: list[Company] = []
        unknown: list[str] = []
        for token in run_input.companies:
            c = _match_token(token)
            if c is None:
                unknown.append(token)
            elif c not in companies:
                companies.append(c)
        if not run_input.companies:
            if ALL_COMPANIES.search(run_input.query):
                companies = list(COMPANIES.values())
            else:
                companies = companies_in_text(run_input.query)
        if not companies and self.chat is not None:
            companies = self._ask_model(run_input.query)
        return companies, unknown

    def _ask_model(self, query: str) -> list[Company]:
        keys = ", ".join(f"{c.key} ({c.name}, {c.ticker})" for c in COMPANIES.values())
        messages = [
            {"role": "system", "content": "You map an analyst's request to company keys. Answer with a JSON array "
                                          "of keys only, [] when none applies."},
            {"role": "user", "content": f"Keys: {keys}.\nRequest: {query}\nWhich keys does the request name?"},
        ]
        try:
            result = self.chat.chat(self.name, messages, max_tokens=1024, purpose="resolve companies",
                                    target_agent="Retriever")
            match = re.search(r"\[[^\[\]]*\]", result.text)
            keys_out = json.loads(match.group(0)) if match else []
        except Exception as e:  # noqa: BLE001 - the model is a fallback here; no answer means no company
            self.tracer.emit(self.name, "resolve_companies", input_summary=query,
                             output_summary=f"model fallback failed: {e}", status="failed")
            return []
        return [COMPANIES[k] for k in keys_out if isinstance(k, str) and k in COMPANIES]

    def plan(self, run_input: RunInput, tables: set[str] | None) -> Plan:
        companies, unknown = self.resolve(run_input)
        limitations: list[str] = []
        if tables is None:  # the table check failed: plan on the registered set and say so
            tables = {CONCEPTS[k].table(c.key) for c in COMPANIES.values() for k in CONCEPT_ORDER}
            tables -= {"edgar.ibm_rfcc_table", "edgar.ford_rfcc_table"}
            limitations.append("The Zetaris table list could not be read; the plan assumes the registered tables.")
        if unknown:
            limitations.append(
                f"Not registered in Zetaris, so not checked: {', '.join(unknown)}. Registered: "
                f"{', '.join(c.name for c in COMPANIES.values())}."
            )
        if not companies:
            self.tracer.emit(self.name, "plan", input_summary=run_input.query,
                             output_summary="no registered company in the request", status="failed")
            raise PlanError(
                "The request names no company this agent can check. Registered: "
                + ", ".join(f"{c.name} ({c.ticker})" for c in COMPANIES.values())
            )
        both = bool(TAG_FOCUS.search(" ".join(filter(None, [run_input.query, run_input.focus]))))
        steps: list[PlanStep] = []
        for c in companies:
            available = [CONCEPTS[k] for k in CONCEPT_ORDER if CONCEPTS[k].table(c.key) in tables]
            if not available:
                limitations.append(f"{c.name}: no revenue table is registered in Zetaris.")
                continue
            steps.append(PlanStep(c, available[0], "primary", "the company's primary revenue concept"))
            if both and len(available) > 1:
                steps.append(PlanStep(c, available[1], "alternate", "the request asks about tag changes"))
        plan = Plan(steps, companies, set(tables), run_input.period_start, run_input.period_end, unknown, limitations)
        self.tracer.emit(
            self.name, "plan",
            input_summary=run_input.query,
            output_summary=f"{len(steps)} steps: {plan.summary()}",
            target_agent="Retriever",
            details={"steps": [{"company": s.company.name, "concept": s.concept.name, "table": s.table,
                                "role": s.role} for s in steps],
                     "period_start": run_input.period_start, "period_end": run_input.period_end,
                     "unknown": unknown, "alternates_from_start": both},
        )
        return plan

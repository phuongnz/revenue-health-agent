"""The Evaluator: the critic. Every finding is accepted, rejected with a reason, or sent
back; a truncated series makes it request a retry with the other revenue concept.

Two checks, in this order:

1. **Support, by code.** The rows a finding cites must exist in what the Retriever
   returned, and must show what the finding claims (same period and value across a 10-K
   and a 10-Q for a duplicate, a later different value for a restatement, no later row
   for an early stop, ...). This is arithmetic, so code decides it.
2. **Critique, by the model.** The Ultra lane reads each finding with its evidence rows
   and accepts it or rejects it with a reason, and gives a confidence. A model failure is
   not a run failure: the code verdicts stand and the outcome says so.

A finding that fails either check goes back to the Analyst once, which re-derives it
from the rows. If the rows do not yield it, or it fails the support check again, it is
recorded as unsupported; if the model rejects it again, it is rejected with the model's
reason. Branches: a truncated series (early stop or late start) on a step that may retry
asks the Retriever for the alternate concept; with no alternate registered it takes the
fallback and records the truncation as a limitation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.agents.analyst import GAP_TOLERANCE_DAYS, Analyst, Fact, SeriesContext, facts_from_rows, short_concept
from app.schemas import Finding
from app.tracing import NullTracer, Tracer

AGENT = "Evaluator"
CRITIQUE_MAX_TOKENS = 8192  # Ultra reasons before it answers; its completion tokens include the reasoning
MODEL_REVIEW_LIMIT = 15  # findings per critique call; the rest stand on the support check
TRUNCATION = ("early_stop", "late_start")
KIND_PRIORITY = {"tag_change": 0, "early_stop": 1, "late_start": 2, "gap": 3, "restatement": 4, "duplicate": 5}

SYSTEM = (
    "You are the Evaluator in a data-quality audit of SEC XBRL revenue series. Code has already computed every "
    "finding from the rows; you check that each finding's description is supported by its evidence rows, is "
    "worded without overstating, and matters to an equity analyst deciding whether to trust the series. Reject "
    "only with a concrete reason tied to the rows. Answer with JSON only."
)


@dataclass
class Evidence:
    """The rows a step's findings rest on: one series, and the other concept's series
    when the step compares the two."""

    rows: list[dict[str, Any]]
    ctx: SeriesContext
    alternate_rows: list[dict[str, Any]] | None = None
    alternate_ctx: SeriesContext | None = None

    def tables(self) -> dict[str, tuple[list[Fact], SeriesContext]]:
        out = {self.ctx.table: (facts_from_rows(self.rows), self.ctx)}
        if self.alternate_ctx is not None and self.alternate_rows is not None:
            out[self.alternate_ctx.table] = (facts_from_rows(self.alternate_rows), self.alternate_ctx)
        return out


@dataclass
class RetryRequest:
    company: str
    concept: str  # the alternate concept's name
    table: str
    reason: str
    finding_ids: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    accepted: list[Finding] = field(default_factory=list)
    rejected: list[Finding] = field(default_factory=list)
    unsupported: list[Finding] = field(default_factory=list)
    retry: RetryRequest | None = None
    revision_count: int = 0
    notes: list[str] = field(default_factory=list)  # the Evaluator's findings about the findings
    limitations: list[str] = field(default_factory=list)
    model_reviewed: bool = False

    @property
    def all(self) -> list[Finding]:
        return self.accepted + self.rejected + self.unsupported


# --- the support check ---------------------------------------------------------------------


def check_support(finding: Finding, evidence: Evidence) -> str | None:
    """None when the cited rows support the finding; otherwise the reason they do not."""
    tables = evidence.tables()
    cited: list[tuple[str, Fact]] = []
    missing: list[str] = []
    for source in finding.sources:
        facts = {f.row_id: f for f in tables.get(source.table, ([], None))[0]}
        for rid in source.row_ids:
            if rid in facts:
                cited.append((source.table, facts[rid]))
            else:
                missing.append(rid)
    if not cited and not missing:
        return "the finding cites no rows"
    if missing:
        return f"{len(missing)} cited row(s) are not in the retrieved data, e.g. {missing[0]}"
    facts = [f for _, f in cited]
    periods = {f.period for f in facts}
    kind = finding.kind
    if kind == "duplicate":
        if len(periods) != 1 or len({f.value for f in facts}) != 1:
            return "the cited rows are not one period with one value"
        if not {"10-K", "10-Q"} <= {f.base_form for f in facts}:
            return "the cited rows are not in both a 10-K and a 10-Q form"
        return None
    if kind == "restatement":
        if len(periods) != 1:
            return "the cited rows are not one period"
        if len({f.value for f in facts}) < 2:
            return "the cited rows all carry the same value: nothing was restated"
        first = facts[0]
        if any(f.sort_filed() < first.sort_filed() for f in facts[1:]):
            return "the first cited row is not the original filing"
        return None
    if kind in TRUNCATION:
        table = cited[0][0]
        all_facts, ctx = tables[table]
        if kind == "early_stop":
            last = max(f.end for f in all_facts)
            if max(f.end for f in facts) != last:
                return f"a later period exists in {table} (ends {last})"
            if last >= ctx.expected_through:
                return f"the series reaches {last}, inside the requested window"
        else:
            first_start = min((f.start for f in all_facts if f.start), default=None)
            cited_start = min((f.start for f in facts if f.start), default=None)
            if cited_start is None or cited_start != first_start:
                return f"an earlier period exists in {table} (starts {first_start})"
            if first_start <= ctx.late_after:
                return f"the series starts {first_start}, inside the requested window"
        return None
    if kind == "gap":
        if len(facts) < 2:
            return "a gap needs the two periods around it"
        ordered = sorted(facts, key=lambda f: f.end)
        a, b = ordered[0], ordered[-1]
        if b.start is None or (b.start - a.end).days - 1 <= GAP_TOLERANCE_DAYS:
            return "the cited periods leave no gap between them"
        same_concept = len({t for t, _ in cited}) == 1
        for all_facts, _ in (tables[t] for t in {t for t, _ in cited}):
            for f in all_facts:
                inside = f.start is not None and f.start > a.end and f.end < b.start
                if inside and (f.is_annual or not same_concept):
                    return f"a period inside the gap is reported ({f.row_id})"
        return None
    if kind == "tag_change":
        if len({t for t, _ in cited}) < 2:
            return "a tag change needs rows from both concepts"
        for table, f in cited:
            all_facts = tables[table][0]
            last, first = max(x.end for x in all_facts), min((x.start or x.end) for x in all_facts)
            if f.end != last and (f.start or f.end) != first:
                return f"{f.row_id} is neither the last nor the first period of {table}"
        return None
    return None


# --- the critique ------------------------------------------------------------------------------


def _evidence_lines(finding: Finding, limit: int = 4) -> list[str]:
    lines = []
    for row in finding.evidence[:limit]:
        lines.append(
            f"    row {row.get('row_id')}: {row.get('period_start')}..{row.get('period_end')} "
            f"value {row.get('value_usd')} form {row.get('form_type')} filed {row.get('filed_date')} "
            f"[{row.get('table')}]"
        )
    return lines


def critique_prompt(items: list[tuple[str, Finding, str | None]], ctx: SeriesContext) -> str:
    lines = [
        f"Company: {ctx.company}. Window: fiscal years from {ctx.from_year}, data expected through "
        f"{ctx.expected_through}. Each finding below was computed by code from the cited rows.",
        "",
    ]
    for key, f, note in items:
        lines.append(f"{key} [{f.kind}] period {f.period} concept {short_concept(f.concept or '')}")
        lines.append(f"  claim: {f.description}")
        if note:
            lines.append(f"  analyst note: {note}")
        lines.extend(_evidence_lines(f))
    lines += [
        "",
        'Reply with JSON only: {"reviews": [{"id": "F1", "verdict": "accept" or "reject", "reason": "one '
        'sentence", "confidence": 0.0 to 1.0}, ...], "critique": "two sentences on the set as a whole"}. '
        "One review per finding id.",
    ]
    return "\n".join(lines)


def parse_reviews(text: str) -> tuple[dict[str, dict[str, Any]], str]:
    """The model's reviews keyed by finding key, and its overall critique. Raises
    ValueError when the reply holds no usable JSON."""
    candidates = [text] + re.findall(r"\{.*\}", text, re.DOTALL)
    for candidate in candidates:
        try:
            data = json.loads(candidate.strip().strip("`").removeprefix("json").strip())
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("reviews"), list):
            out = {}
            for r in data["reviews"]:
                if isinstance(r, Mapping) and isinstance(r.get("id"), str):
                    verdict = str(r.get("verdict", "")).lower().strip()
                    if verdict in ("accept", "reject"):
                        conf = r.get("confidence")
                        conf = float(conf) if isinstance(conf, (int, float)) and 0 <= conf <= 1 else None
                        out[r["id"].strip()] = {"verdict": verdict, "reason": str(r.get("reason") or "").strip(),
                                                "confidence": conf}
            return out, str(data.get("critique") or "").strip()
    raise ValueError("no JSON object with a 'reviews' list in the model's reply")


# --- the agent ---------------------------------------------------------------------------------------


class Evaluator:
    name = AGENT

    def __init__(self, tracer: Tracer | None = None, chat: Any = None, max_tokens: int = CRITIQUE_MAX_TOKENS) -> None:
        self.tracer = tracer or NullTracer()
        self.chat = chat  # MeterlessClient-like: .chat(agent_name, messages, ...) -> ChatResult; None = code only
        self.max_tokens = max_tokens

    def _critique(
        self, findings: list[Finding], ctx: SeriesContext, notes: dict[str, str], round_no: int, out: Outcome,
    ) -> dict[str, dict[str, Any]] | None:
        """One model call over up to MODEL_REVIEW_LIMIT findings; None when the model is
        unavailable or its reply unusable (the caller keeps the code verdicts)."""
        if self.chat is None or not findings:
            return None
        ranked = sorted(findings, key=lambda f: KIND_PRIORITY.get(f.kind, 9))[:MODEL_REVIEW_LIMIT]
        keyed = [(f"F{i + 1}", f, notes.get(f.id)) for i, f in enumerate(ranked)]
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": critique_prompt(keyed, ctx)}]
        try:
            result = self.chat.chat(self.name, messages, max_tokens=self.max_tokens, temperature=0.1,
                                    purpose=f"critique round {round_no}", target_agent="Analyst")
            reviews, critique = parse_reviews(result.text)
        except Exception as e:  # noqa: BLE001 - the fallback is the code verdicts
            note = f"Model critique unavailable in round {round_no} ({type(e).__name__}); verdicts rest on the support check."
            out.limitations.append(note)
            self.tracer.emit(self.name, "critique", input_summary=f"{len(keyed)} findings for {ctx.company}",
                             output_summary=f"fallback to code verdicts: {e}", status="partial_success",
                             retry_count=round_no - 1)
            return None
        by_id = {f.id: reviews[k] for k, f, _ in keyed if k in reviews}
        rejected = sum(1 for r in by_id.values() if r["verdict"] == "reject")
        confs = [r["confidence"] for r in by_id.values() if r["confidence"] is not None]
        out.model_reviewed = True
        if critique:
            out.notes.append(f"{ctx.company} {short_concept(ctx.concept)}, round {round_no}: {critique}")
        self.tracer.emit(
            self.name, "critique",
            input_summary=f"{len(keyed)} findings for {ctx.company} {short_concept(ctx.concept)} (round {round_no})",
            output_summary=f"{len(by_id) - rejected} accepted, {rejected} rejected"
                           + (f"; {critique}" if critique else ""),
            target_agent="Analyst" if rejected else None,
            confidence=round(sum(confs) / len(confs), 3) if confs else None,
            retry_count=round_no - 1,
            status="needs_revision" if rejected else "success",
            model=getattr(result, "routed_model", None),
            details={"reviews": {fid: r for fid, r in by_id.items()},
                     "unreviewed": [f.id for f in findings if f.id not in by_id]},
        )
        return by_id

    def evaluate(
        self, findings: list[Finding], evidence: Evidence, analyst: Analyst, *,
        alternate: tuple[str, str] | None = None, may_retry: bool = False,
    ) -> Outcome:
        """`alternate` is (concept name, table) of the other registered concept, or None;
        `may_retry` is False for steps that are themselves retries or alternates."""
        out = Outcome()
        ctx = evidence.ctx
        if not findings:
            self.tracer.emit(self.name, "evaluate", input_summary=f"{ctx.company} {short_concept(ctx.concept)}",
                             output_summary="no findings to review: the series is clean in the requested window")
            return out

        support = {f.id: check_support(f, evidence) for f in findings}
        reviews = self._critique(findings, ctx, {}, 1, out) or {}

        send_back: list[tuple[Finding, str, str]] = []  # (finding, reason, by)
        for f in findings:
            review = reviews.get(f.id)
            if support[f.id] is not None:
                send_back.append((f, support[f.id], "support check"))
            elif review and review["verdict"] == "reject":
                send_back.append((f, review["reason"] or "rejected by the critique", "critique"))
            else:
                out.accepted.append(self._accept(f, review))

        revised_for_model: list[Finding] = []
        notes: dict[str, str] = {}
        for f, reason, by in send_back:
            out.revision_count += 1
            self.tracer.emit(self.name, "send_back", input_summary=f"{f.kind} {f.period} ({f.id})",
                             output_summary=f"back to the Analyst ({by}): {reason}", target_agent="Analyst",
                             retry_count=1, status="needs_revision", details={"finding_id": f.id, "by": by})
            revised = analyst.revise(f, reason, evidence.rows, evidence.ctx,
                                     evidence.alternate_rows, evidence.alternate_ctx)
            again = check_support(revised, evidence) if revised is not None else "the Analyst could not re-derive it"
            if again is not None:
                out.unsupported.append(self._mark(f, "unsupported", f"{reason}; after revision: {again}"))
                self.tracer.emit(self.name, "record_unsupported", input_summary=f"{f.kind} {f.period} ({f.id})",
                                 output_summary=f"unsupported after one revision: {again}", retry_count=1,
                                 status="needs_validation", details={"finding_id": f.id})
            elif by == "critique":
                revised_for_model.append(revised)
                notes[revised.id] = (f"re-derived from {len(evidence.rows)} rows after the critique said: {reason}. "
                                     "The arithmetic holds.")
            else:
                out.accepted.append(self._accept(revised, None, f"accepted after revision ({reason})"))

        if revised_for_model:
            second = self._critique(revised_for_model, ctx, notes, 2, out)
            for f in revised_for_model:
                review = (second or {}).get(f.id)
                if review and review["verdict"] == "reject":
                    out.rejected.append(self._mark(f, "rejected", review["reason"] or "rejected twice by the critique"))
                    out.notes.append(f"Rejected {f.kind} {f.period} for {f.company}: {review['reason']}")
                else:
                    out.accepted.append(self._accept(f, review, "accepted after revision"))

        self._branch(out, evidence, alternate, may_retry)
        self.tracer.emit(
            self.name, "evaluate",
            input_summary=f"{len(findings)} findings for {ctx.company} {short_concept(ctx.concept)}",
            output_summary=f"{len(out.accepted)} accepted, {len(out.rejected)} rejected, "
                           f"{len(out.unsupported)} unsupported; {out.revision_count} sent back"
                           + (f"; retry with {short_concept(out.retry.concept)}" if out.retry else ""),
            target_agent="Retriever" if out.retry else "Reporter",
            confidence=round(sum(f.confidence for f in out.accepted) / len(out.accepted), 3) if out.accepted else None,
            retry_count=out.revision_count,
            status="retrying" if out.retry else "success",
            details={"accepted": [f.id for f in out.accepted], "rejected": [f.id for f in out.rejected],
                     "unsupported": [f.id for f in out.unsupported], "model_reviewed": out.model_reviewed},
        )
        return out

    def _branch(self, out: Outcome, evidence: Evidence, alternate: tuple[str, str] | None, may_retry: bool) -> None:
        truncated = [f for f in out.accepted if f.kind in TRUNCATION]
        if not truncated or not may_retry:
            return
        ctx = evidence.ctx
        what = "; ".join(f"{f.kind.replace('_', ' ')} at {f.period.strip('/')}" for f in truncated)
        if alternate is None:
            note = (f"{ctx.company}: the {short_concept(ctx.concept)} series is truncated ({what}) and no other revenue "
                    f"concept is registered in Zetaris for it, so the missing periods could not be looked up.")
            out.limitations.append(note)
            self.tracer.emit(self.name, "retry_fallback", input_summary=what, output_summary=note,
                             status="partial_success", details={"finding_ids": [f.id for f in truncated]})
            return
        concept, table = alternate
        out.retry = RetryRequest(ctx.company, concept, table,
                                 f"{short_concept(ctx.concept)} series truncated ({what}); re-query {short_concept(concept)}",
                                 [f.id for f in truncated])
        self.tracer.emit(self.name, "request_retry", input_summary=what,
                         output_summary=f"re-query {table} for the alternate concept {concept}",
                         target_agent="Retriever", retry_count=1, status="retrying",
                         details={"table": table, "concept": concept, "finding_ids": out.retry.finding_ids})

    @staticmethod
    def _accept(f: Finding, review: dict[str, Any] | None, reason: str | None = None) -> Finding:
        confidence = f.confidence
        why = reason or "the cited rows support it"
        if review:
            if review.get("confidence") is not None:
                confidence = round((f.confidence + review["confidence"]) / 2, 3)
            if review.get("reason"):
                why = f"{why}; critique: {review['reason']}" if reason else review["reason"]
        return f.model_copy(update={"status": "accepted", "confidence": confidence, "evaluator_reason": why})

    @staticmethod
    def _mark(f: Finding, status: str, reason: str) -> Finding:
        return f.model_copy(update={"status": status, "evaluator_reason": reason,
                                    "revision_count": max(f.revision_count, 1)})

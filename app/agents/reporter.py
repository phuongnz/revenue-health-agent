"""The Reporter: writes the report from accepted findings only.

The JSON is the `result` of the submission contract's response (§4 of the build
contract): `summary`, `findings[]` with their `sources`, `confidence`, `limitations`,
`recommended_actions`. The Markdown says the same for a reader: per company, the
periods and what is wrong with them, the confidence, the cited rows, what to do, and the
explanation the model wrote in the engine step. Rejected and unsupported findings never
appear as findings; the Evaluator's notes about them do, under the review.

The code findings are the truth. Model-written prose (a step's explanation, the
critique's summary, a review reason) is printed only where `check_explanation` finds it
agrees with the company's accepted findings; otherwise the findings speak alone. The
engine's reflection stays in the trace and never enters the report.

Code only, so a report can always be written, whatever failed before it.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.analyst import short_concept, starts_with_the_tag
from app.agents.evaluator import check_explanation
from app.schemas import Finding, RunResult
from app.tracing import NullTracer, Tracer

AGENT = "Reporter"
ORDER = {"tag_change": 0, "early_stop": 1, "late_start": 2, "gap": 3, "restatement": 4, "duplicate": 5, "other": 6}
LABEL = {
    "tag_change": "tag change", "early_stop": "stops early", "late_start": "starts late", "gap": "missing periods",
    "restatement": "restated", "duplicate": "duplicated across forms", "other": "reference data",
}
CLEAN_CONFIDENCE = 0.85
STANDING_LIMITATIONS = (
    "Only the XBRL concepts us-gaap/Revenues and us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax are "
    "read; revenue a company reported under another tag is not seen.",
    "Facts are as reported to SEC EDGAR's XBRL API and read through Zetaris; the filings' text is not read.",
    "The report judges the data's reliability, not the companies.",
)


@dataclass
class Explanation:
    """One step's explanation. `by` is "model" or "code"; a model's carries what it was
    shown (`given`, where its dates and figures must come from) and the code-written text
    that replaces it if it disagrees with the findings."""

    company: str
    step: str
    text: str
    by: str = "model"
    given: str = ""
    fallback: str = ""


@dataclass
class ReportInput:
    """What the run gathered for the Reporter, besides the findings."""

    run_id: str
    companies: list[str]
    explanations: list[Explanation] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    evaluator_notes: list[str] = field(default_factory=list)  # code-written
    critiques: list[tuple[str, str]] = field(default_factory=list)  # (company, model-written critique)
    steps_planned: int = 0
    steps_with_rows: int = 0
    rejected: int = 0
    unsupported: int = 0
    revision_count: int = 0
    explanations_sent_back: int = 0
    agents_used: list[str] = field(default_factory=list)
    trace_path: str | None = None


def _unique(items: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def ordered(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (f.company, ORDER.get(f.kind, 9), f.concept or "", f.period or ""))


def explained_by_tag_change(findings: Iterable[Finding]) -> dict[str, str]:
    """Finding id -> note, for the old concept's stop and the new concept's late start
    that a tag change accounts for: continuous, or the two ends of a hole that the gap
    finding reports."""
    findings = list(findings)
    out: dict[str, str] = {}
    for tag in (f for f in findings if f.kind == "tag_change"):
        old, _, new = (tag.concept or "").partition(" -> ")
        old_last, _, new_first = (tag.period or "").partition("/")
        if any(g.kind == "gap" and g.concept == tag.concept and g.company == tag.company for g in findings):
            note = (f"One end of the hole between {short_concept(old)} and {short_concept(new)} ({old_last} / "
                    f"{new_first}); see the missing periods.")
        else:
            note = f"Explained by the tag change from {short_concept(old)} to {short_concept(new)} ({old_last} / {new_first})."
        for f in findings:
            if f.company != tag.company:
                continue
            if f.kind == "early_stop" and f.concept == old and (f.period or "").endswith(old_last):
                out[f.id] = note
            elif f.kind == "late_start" and f.concept == new:  # the older concept covers the earlier years
                out[f.id] = note
    return out


def company_headline(company: str, findings: list[Finding]) -> str:
    if not findings:
        return f"{company}: no problem found in the requested window."
    explained = explained_by_tag_change(findings)
    by_kind: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        if f.id not in explained:
            by_kind[f.kind].append(f)
    parts = []
    for tag in by_kind.get("tag_change", []):
        old, _, new = (tag.concept or "").partition(" -> ")
        old_last, _, new_first = (tag.period or "").partition("/")
        hole = any(" -> " in (g.concept or "") for g in by_kind.get("gap", []))
        parts.append(f"the {short_concept(old)} series stops at {old_last} and continues under {short_concept(new)} "
                     f"from {new_first}" + (", with periods missing in between" if hole
                                            else ", a tag change, not missing revenue"))
    for stop in by_kind.get("early_stop", []):
        parts.append(f"the {short_concept(stop.concept or '')} series stops at {(stop.period or '').strip('/')}")
    for start in by_kind.get("late_start", []):
        concept, first = short_concept(start.concept or ""), (start.period or "").split("/")[0]
        handed_over = any(t.kind == "tag_change" and (t.concept or "").endswith(f" -> {start.concept}") for t in findings)
        if starts_with_the_tag(start) and not handed_over:
            parts.append(f"the {concept} series starts at {first}, where the company began using the tag; earlier "
                         f"revenue sits under a concept this run does not read")
        else:
            parts.append(f"the {concept} series starts late, at {first}")
    for kind, one, many in (
        ("gap", "gap", "gaps"),
        ("restatement", "restated period", "restated periods"),
        ("duplicate", "period duplicated across 10-K and 10-Q", "periods duplicated across 10-K and 10-Q"),
        ("other", "reference-data problem", "reference-data problems"),
    ):
        n = len([f for f in by_kind.get(kind, []) if not (kind == "gap" and " -> " in (f.concept or ""))])
        if n:
            parts.append(f"{n} {one if n == 1 else many}")
    return f"{company}: " + "; ".join(parts) + "."


def recommended_actions(findings: list[Finding]) -> list[str]:
    grouped: dict[tuple[str, str, str], list[Finding]] = defaultdict(list)
    explained = explained_by_tag_change(findings)
    for f in ordered(findings):
        if f.id in explained:
            continue
        grouped[(f.company, f.kind, f.concept or "")].append(f)
    actions = []
    for (company, kind, concept), group in grouped.items():
        if len(group) == 1:
            actions.append(f"{company}: {group[0].recommendation or group[0].description}")
        elif kind == "duplicate":
            actions.append(f"{company} {short_concept(concept)}: {len(group)} periods are reported with the same value in "
                           f"both 10-K and 10-Q forms; deduplicate on (period_start, period_end) before summing.")
        elif kind == "restatement":
            actions.append(f"{company} {short_concept(concept)}: {len(group)} periods were restated in later filings; "
                           f"use the latest filed value for each and keep the original for comparisons.")
        else:
            actions.extend(f"{company}: {f.recommendation or f.description}" for f in group)
    return _unique(actions)


CODE_REASON = "the cited rows support it; the critique's wording contradicted a code finding and is not kept"


def vet_reasons(accepted: list[Finding]) -> list[Finding]:
    """An accepted finding's review reason is the model's words: kept only where they agree
    with the company's accepted findings."""
    by_company: dict[str, list[Finding]] = defaultdict(list)
    for f in accepted:
        by_company[f.company].append(f)
    out = []
    for f in accepted:
        if f.evaluator_reason and check_explanation(f.evaluator_reason, by_company[f.company], company=f.company):
            f = f.model_copy(update={"evaluator_reason": CODE_REASON})
        out.append(f)
    return out


def vetted_critiques(findings: list[Finding], critiques: Iterable[tuple[str, str]]) -> list[str]:
    """The critique summaries that agree with their company's accepted findings; the others
    are replaced by a line saying they were left out, and why."""
    accepted = [f for f in findings if f.status == "accepted"]
    out = []
    for company, text in critiques:
        reason = check_explanation(text, [f for f in accepted if f.company == company], company=company)
        head = text.split(":", 1)[0]
        out.append(text if reason is None else f"{head}: the critique's summary is left out: {reason}.")
    return out


def build_result(findings: list[Finding], report: ReportInput) -> RunResult:
    accepted = vet_reasons(ordered(f for f in findings if f.status == "accepted"))
    by_company: dict[str, list[Finding]] = defaultdict(list)
    for f in accepted:
        by_company[f.company].append(f)
    headlines = [company_headline(c, by_company.get(c, [])) for c in report.companies]
    coverage = report.steps_with_rows / report.steps_planned if report.steps_planned else 0.0
    base = sum(f.confidence for f in accepted) / len(accepted) if accepted else CLEAN_CONFIDENCE
    confidence = round(max(0.0, min(1.0, base * coverage)), 3)
    review = (f" The Evaluator reviewed every finding: {len(accepted)} accepted, {report.rejected} rejected, "
              f"{report.unsupported} unsupported, {report.revision_count} sent back to the Analyst.")
    if report.explanations_sent_back:
        n = report.explanations_sent_back
        review += f" {n} explanation{'s' if n > 1 else ''} contradicting the findings went back to the Analyst."
    if not report.steps_with_rows:
        summary = "No revenue rows could be read, so nothing could be checked." + review
    else:
        summary = " ".join(headlines) + review
    return RunResult(
        summary=summary,
        findings=accepted,
        confidence=confidence,
        limitations=_unique([*report.limitations, *STANDING_LIMITATIONS]),
        recommended_actions=recommended_actions(accepted),
    )


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _rows_cited(f: Finding, limit: int = 3) -> str:
    ids = [f"`{rid}`" for s in f.sources for rid in s.row_ids]
    more = f" +{len(ids) - limit}" if len(ids) > limit else ""
    return ", ".join(ids[:limit]) + more


def explanation_text(e: Explanation, findings: list[Finding]) -> tuple[str, str | None]:
    """What the report prints for one step's explanation, and why the model's words were
    replaced (None when they are printed): code-written text is printed as is; a model's
    only where it agrees with the company's accepted findings."""
    if e.by != "model":
        return e.text, None
    reason = check_explanation(e.text, findings, e.given, e.company)
    if reason is None:
        return e.text, None
    return e.fallback or "", reason


def render_markdown(result: RunResult, report: ReportInput) -> str:
    lines = [f"# Revenue series health report: {', '.join(report.companies) or 'no company'}", "",
             f"Run `{report.run_id}`. Overall confidence {result.confidence:.2f}.", "", "## Summary", "",
             result.summary, ""]
    by_company: dict[str, list[Finding]] = defaultdict(list)
    for f in result.findings:
        by_company[f.company].append(f)
    replaced = []
    for company in report.companies:
        found = by_company.get(company, [])
        lines += [f"## {company}", ""]
        for e in (e for e in report.explanations if e.company == company):
            text, why = explanation_text(e, found)
            if why:
                replaced.append(f"The model's explanation of {e.step} is replaced by the findings' own summary: {why}.")
            if text.strip():
                lines += [text.strip(), ""]
        explained = explained_by_tag_change(found)
        main = [f for f in found if f.kind != "duplicate"]
        dups = [f for f in found if f.kind == "duplicate"]
        if not found:
            lines += ["No problem found in the requested window.", ""]
        if main:
            lines += ["| Period | Status | Concept | Confidence | Evidence rows | What it means | What to do |",
                      "|---|---|---|---|---|---|---|"]
            for f in main:
                lines.append(f"| {_cell(f.period or '')} | {LABEL.get(f.kind, f.kind)} | {_cell(short_concept(f.concept or ''))} "
                             f"| {f.confidence:.2f} | {_rows_cited(f)} | {_cell(f.description + (' ' + explained[f.id] if f.id in explained else ''))} | "
                             f"{_cell(f.recommendation or '')} |")
            lines.append("")
        if dups:
            periods = ", ".join(f.period or "" for f in dups[:8]) + (f" and {len(dups) - 8} more" if len(dups) > 8 else "")
            lines += [f"**Duplicated across 10-K and 10-Q forms** ({len(dups)} period{'s' if len(dups) > 1 else ''}, confidence "
                      f"{min(f.confidence for f in dups):.2f}-{max(f.confidence for f in dups):.2f}): {periods}. "
                      f"Example: {dups[0].description} Rows: {_rows_cited(dups[0], 4)}.", ""]
    lines += ["## Recommended actions", ""] + [f"- {a}" for a in result.recommended_actions] + [""]
    lines += ["## Limitations", ""] + [f"- {item}" for item in result.limitations] + [""]
    lines += ["## Review", "",
              f"Agents: {', '.join(report.agents_used)}. Findings sent back to the Analyst: {report.revision_count}; "
              f"rejected: {report.rejected}; unsupported: {report.unsupported}; explanations sent back: "
              f"{report.explanations_sent_back}.", ""]
    notes = [*vetted_critiques(result.findings, report.critiques), *report.evaluator_notes, *replaced]
    lines += [f"- {note}" for note in notes]
    if report.trace_path:
        lines += ["", f"Trace: `{report.trace_path}`"]
    return "\n".join(lines).rstrip() + "\n"


class Reporter:
    name = AGENT

    def __init__(self, tracer: Tracer | None = None) -> None:
        self.tracer = tracer or NullTracer()

    def report(self, findings: list[Finding], report: ReportInput, out_dir: str | Path | None = None,
               ) -> tuple[RunResult, str, dict[str, str]]:
        """The result, its Markdown, and the paths written (when `out_dir` is set)."""
        result = build_result(findings, report)
        markdown = render_markdown(result, report)
        paths: dict[str, str] = {}
        if out_dir is not None:
            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
            md_path, json_path = out / f"report-{report.run_id}.md", out / f"report-{report.run_id}.json"
            md_path.write_text(markdown, encoding="utf-8")
            json_path.write_text(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
            paths = {"markdown": str(md_path), "json": str(json_path)}
        self.tracer.emit(
            self.name, "write_report",
            input_summary=f"{len(findings)} findings, {sum(1 for f in findings if f.status == 'accepted')} accepted",
            output_summary=f"{len(result.findings)} findings reported; confidence {result.confidence:.2f}; "
                           f"{len(result.recommended_actions)} actions",
            confidence=result.confidence,
            details=paths or None,
        )
        return result, markdown, paths


def finding_payload(f: Finding) -> dict[str, Any]:
    """A compact view of a finding for the engine step and the critique."""
    return {"id": f.id, "kind": f.kind, "period": f.period, "concept": f.concept, "confidence": f.confidence,
            "description": f.description, "rows": [rid for s in f.sources for rid in s.row_ids]}

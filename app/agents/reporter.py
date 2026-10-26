"""The Reporter: writes the report from accepted findings only.

The JSON is the `result` of the submission contract's response (§4 of the build
contract): `summary`, `findings[]` with their `sources`, `confidence`, `limitations`,
`recommended_actions`. The Markdown says the same for a reader: per company, the
periods and what is wrong with them, the confidence, the cited rows, what to do, and the
explanation the model wrote in the engine step. Rejected and unsupported findings never
appear as findings; the Evaluator's notes about them do, under the review.

Code only, so a report can always be written, whatever failed before it.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.analyst import short_concept
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
class ReportInput:
    """What the run gathered for the Reporter, besides the findings."""

    run_id: str
    companies: list[str]
    explanations: dict[str, list[str]] = field(default_factory=dict)  # company -> model-written paragraphs
    limitations: list[str] = field(default_factory=list)
    evaluator_notes: list[str] = field(default_factory=list)
    steps_planned: int = 0
    steps_with_rows: int = 0
    rejected: int = 0
    unsupported: int = 0
    revision_count: int = 0
    agents_used: list[str] = field(default_factory=list)
    trace_path: str | None = None
    reflection: str | None = None


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
    that a continuous tag change accounts for."""
    findings = list(findings)
    out: dict[str, str] = {}
    for tag in (f for f in findings if f.kind == "tag_change"):
        old, _, new = (tag.concept or "").partition(" -> ")
        old_last, _, new_first = (tag.period or "").partition("/")
        if any(g.kind == "gap" and g.concept == tag.concept and g.company == tag.company for g in findings):
            continue
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
        parts.append(f"the {short_concept(start.concept or '')} series starts late, at {(start.period or '').split('/')[0]}")
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


def build_result(findings: list[Finding], report: ReportInput) -> RunResult:
    accepted = ordered(f for f in findings if f.status == "accepted")
    by_company: dict[str, list[Finding]] = defaultdict(list)
    for f in accepted:
        by_company[f.company].append(f)
    headlines = [company_headline(c, by_company.get(c, [])) for c in report.companies]
    coverage = report.steps_with_rows / report.steps_planned if report.steps_planned else 0.0
    base = sum(f.confidence for f in accepted) / len(accepted) if accepted else CLEAN_CONFIDENCE
    confidence = round(max(0.0, min(1.0, base * coverage)), 3)
    review = (f" The Evaluator reviewed every finding: {len(accepted)} accepted, {report.rejected} rejected, "
              f"{report.unsupported} unsupported, {report.revision_count} sent back to the Analyst.")
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


def render_markdown(result: RunResult, report: ReportInput) -> str:
    lines = [f"# Revenue series health report: {', '.join(report.companies) or 'no company'}", "",
             f"Run `{report.run_id}`. Overall confidence {result.confidence:.2f}.", "", "## Summary", "",
             result.summary, ""]
    by_company: dict[str, list[Finding]] = defaultdict(list)
    for f in result.findings:
        by_company[f.company].append(f)
    for company in report.companies:
        found = by_company.get(company, [])
        lines += [f"## {company}", ""]
        for paragraph in report.explanations.get(company, []):
            lines += [paragraph.strip(), ""]
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
              f"rejected: {report.rejected}; unsupported: {report.unsupported}.", ""]
    lines += [f"- {note}" for note in report.evaluator_notes]
    if report.reflection:
        lines += ["", f"Engine reflection: {' '.join(report.reflection.split())[:1200]}"]
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

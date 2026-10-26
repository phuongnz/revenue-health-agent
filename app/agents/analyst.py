"""The Analyst: Zetaris rows in, findings out. All arithmetic here is code.

Each row of `edgar.<co>_revenue_table` / `edgar.<co>_rfcc_table` is one XBRL fact: a
period (start, end), a value in USD, and the filing it came from (accession, form, filed
date). The same period appears in many filings (every 10-K repeats earlier years; a 10-K
can carry a quarterly table), which is where duplicates and restatements come from.

Findings, each citing the rows it rests on (`row_id` = `accession:start:end`):

- **gap**: two consecutive annual periods with uncovered days between them.
- **duplicate**: one period with the same value in both a 10-K and a 10-Q form.
- **restatement**: one period whose value changes in a later filing.
- **early_stop**: the series' last period ends before the requested window does.
- **late_start**: the series' first period starts after the requested window begins.
- **tag_change**: across two revenue concepts, one series ends where the other begins
  (`compare_concepts`, after the Evaluator's alternate-concept retry).

The model never computes any of this; it only writes the explanation of the findings
(the orchestrator's engine step). Finding ids are deterministic in (company, concept,
kind, period), so a revision of a finding keeps its id.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app.schemas import Finding, Source
from app.tracing import NullTracer, Tracer

AGENT = "Analyst"
ANNUAL_DAYS = (350, 380)
QUARTER_DAYS = (80, 100)
GAP_TOLERANCE_DAYS = 7
DEFAULT_FROM_YEAR = 2009  # the first year of mandatory XBRL financial data for the largest filers
STALE_AFTER_DAYS = 365
MAX_EVIDENCE = 6

CONFIDENCE: dict[str, float] = {
    "duplicate": 0.95,
    "restatement": 0.9,
    "tag_change": 0.9,
    "gap": 0.85,
    "early_stop": 0.8,
    "late_start": 0.75,
}


# --- rows -------------------------------------------------------------------------


def parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value).strip()
        return int(text) if text.lstrip("+-").isdigit() else float(text)
    except ValueError:
        return None


def row_id(row: Mapping[str, Any]) -> str:
    """A row's identity in its table: the filing and the period it reports."""
    return f"{row.get('accession_number') or '?'}:{row.get('period_start') or ''}:{row.get('period_end') or ''}"


def base_form(form: str) -> str:
    """`10-K/A` and `10-KT` count as 10-K, `10-Q/A` as 10-Q."""
    form = (form or "").upper().strip()
    if form.startswith("10-K"):
        return "10-K"
    if form.startswith("10-Q"):
        return "10-Q"
    return form


@dataclass(frozen=True)
class Fact:
    row_id: str
    concept: str
    start: date | None
    end: date
    value: float
    accession: str
    form: str
    filed: date | None
    frame: str | None
    row: dict[str, Any] = field(compare=False, hash=False, repr=False)

    @property
    def days(self) -> int | None:
        return None if self.start is None else (self.end - self.start).days

    @property
    def is_annual(self) -> bool:
        return self.days is not None and ANNUAL_DAYS[0] <= self.days <= ANNUAL_DAYS[1]

    @property
    def is_quarter(self) -> bool:
        return self.days is not None and QUARTER_DAYS[0] <= self.days <= QUARTER_DAYS[1]

    @property
    def base_form(self) -> str:
        return base_form(self.form)

    @property
    def period(self) -> tuple[date | None, date]:
        return (self.start, self.end)

    @property
    def length(self) -> str:
        if self.is_annual:
            return "annual"
        if self.is_quarter:
            return "quarterly"
        return f"{self.days}-day" if self.days is not None else "instant"

    def cite(self) -> str:
        return f"{self.form} {self.accession} filed {self.filed or '?'}"

    def sort_filed(self) -> tuple[date, str]:
        return (self.filed or date.min, self.accession)


def facts_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[Fact]:
    """Typed facts from table rows; rows without an end date or a value are skipped. A
    repeated row id (the same filing reporting one period twice) gets a `#n` suffix."""
    facts: list[Fact] = []
    seen: dict[str, int] = defaultdict(int)
    for raw in rows:
        end = parse_date(raw.get("period_end"))
        value = _number(raw.get("value_usd"))
        if end is None or value is None:
            continue
        rid = row_id(raw)
        seen[rid] += 1
        if seen[rid] > 1:
            rid = f"{rid}#{seen[rid]}"
        facts.append(Fact(
            row_id=rid,
            concept=str(raw.get("concept") or ""),
            start=parse_date(raw.get("period_start")),
            end=end,
            value=value,
            accession=str(raw.get("accession_number") or "?"),
            form=str(raw.get("form_type") or ""),
            filed=parse_date(raw.get("filed_date")),
            frame=raw.get("period_frame") or None,
            row={**dict(raw), "row_id": rid},
        ))
    return facts


# --- the window the request asks about ------------------------------------------------


def _year(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    head = text[:4]
    return int(head) if head.isdigit() else None


@dataclass(frozen=True)
class SeriesContext:
    """What the Analyst needs besides the rows: whose series, where it came from, and
    the window the request asks about."""

    company: str
    concept: str  # e.g. "us-gaap/Revenues"
    table: str  # e.g. "edgar.apple_revenue_table"
    statement: str | None = None
    cik: str | None = None
    period_start: str | None = None  # fiscal year or ISO date, as the request gave it
    period_end: str | None = None
    as_of: date = field(default_factory=date.today)

    @property
    def from_year(self) -> int:
        return _year(self.period_start) or DEFAULT_FROM_YEAR

    @property
    def late_after(self) -> date:
        """A series whose first period starts after this date started late."""
        return date(self.from_year + 1, 12, 31)

    @property
    def expected_through(self) -> date:
        """A series whose last period ends before this date stopped early: the start of
        the requested end year, and never later than a year before `as_of`."""
        latest = self.as_of - timedelta(days=STALE_AFTER_DAYS)
        end_year = _year(self.period_end)
        return min(date(end_year, 1, 1), latest) if end_year else latest

    def with_concept(self, concept: str, table: str, statement: str | None = None) -> "SeriesContext":
        return SeriesContext(self.company, concept, table, statement, self.cik, self.period_start,
                             self.period_end, self.as_of)


# --- findings ---------------------------------------------------------------------------


def money(value: float) -> str:
    return f"${value:,.0f}" if float(value).is_integer() else f"${value:,.2f}"


def finding_id(company: str, concept: str, kind: str, period: str) -> str:
    digest = hashlib.sha1(f"{company}|{concept}|{kind}|{period}".encode()).hexdigest()[:10]
    return f"{kind}-{digest}"


def short_concept(concept: str) -> str:
    """`us-gaap/Revenues` -> `Revenues`; a tag-change pair `a -> b` is shortened on both sides."""
    return " -> ".join(part.split("/", 1)[-1] for part in concept.split(" -> "))


def _make(
    ctx: SeriesContext, kind: str, period: str, description: str, cited: list[tuple[Fact, SeriesContext]],
    recommendation: str, confidence: float | None = None, concept: str | None = None,
) -> Finding:
    concept = concept or ctx.concept
    by_table: dict[str, tuple[SeriesContext, list[str]]] = {}
    evidence = []
    for fact, source_ctx in cited[:MAX_EVIDENCE]:
        evidence.append({**fact.row, "table": source_ctx.table})
        by_table.setdefault(source_ctx.table, (source_ctx, []))[1].append(fact.row_id)
    return Finding(
        id=finding_id(ctx.company, concept, kind, period),
        company=ctx.company,
        cik=ctx.cik,
        concept=concept,
        period=period,
        kind=kind,  # type: ignore[arg-type]
        description=description,
        evidence=evidence,
        confidence=CONFIDENCE.get(kind, 0.7) if confidence is None else confidence,
        sources=[Source(table=t, statement=c.statement, row_ids=ids) for t, (c, ids) in by_table.items()],
        recommendation=recommendation,
    )


def _period(start: date | None, end: date) -> str:
    return f"{start.isoformat() if start else ''}/{end.isoformat()}"


def latest_per_period(facts: Iterable[Fact]) -> dict[tuple[date | None, date], Fact]:
    out: dict[tuple[date | None, date], Fact] = {}
    for f in facts:
        if f.period not in out or f.sort_filed() > out[f.period].sort_filed():
            out[f.period] = f
    return out


def find_gaps(facts: list[Fact], ctx: SeriesContext) -> list[Finding]:
    annual = sorted(latest_per_period(f for f in facts if f.is_annual).values(), key=lambda f: (f.end, f.start))
    quarters = [f for f in facts if f.is_quarter]
    out = []
    for a, b in zip(annual, annual[1:]):
        if b.start is None or b.start <= a.end:
            continue
        first_missing, last_missing = a.end + timedelta(days=1), b.start - timedelta(days=1)
        uncovered = (last_missing - first_missing).days + 1
        if uncovered <= GAP_TOLERANCE_DAYS:
            continue
        years = max(1, round(uncovered / 365.25))
        inside = {q.period for q in quarters if q.start and q.start >= first_missing and q.end <= last_missing}
        note = (f" Quarterly values exist for {len(inside)} quarter(s) inside it, so the annual figure may be"
                f" derivable." if inside else " No quarterly value covers it either.")
        out.append(_make(
            ctx, "gap", _period(first_missing, last_missing),
            f"No annual {short_concept(ctx.concept)} value for {first_missing} to {last_missing}, about {years} "
            f"fiscal year(s): the annual period ending {a.end} ({a.cite()}) is followed by the one starting "
            f"{b.start} ({b.cite()}).{note}",
            [(a, ctx), (b, ctx)],
            f"Treat {first_missing.year}-{last_missing.year} as missing under this concept; look for the annual "
            f"figure in the filings themselves or under another revenue concept before using the series.",
            confidence=0.7 if inside else None,
        ))
    return out


def _groups(facts: Iterable[Fact]) -> dict[tuple[date | None, date], list[Fact]]:
    groups: dict[tuple[date | None, date], list[Fact]] = defaultdict(list)
    for f in facts:
        groups[f.period].append(f)
    return dict(sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0] or date.min)))


def find_duplicates(facts: list[Fact], ctx: SeriesContext) -> list[Finding]:
    out = []
    for (start, end), group in _groups(facts).items():
        by_value: dict[float, list[Fact]] = defaultdict(list)
        for f in group:
            by_value[f.value].append(f)
        for value, same in by_value.items():
            forms = {f.base_form for f in same}
            if not {"10-K", "10-Q"} <= forms:
                continue
            same = sorted(same, key=Fact.sort_filed)
            first_q = next(f for f in same if f.base_form == "10-Q")
            first_k = next(f for f in same if f.base_form == "10-K")
            cited = [first_q, first_k] + [f for f in same if f not in (first_q, first_k)]
            out.append(_make(
                ctx, "duplicate", _period(start, end),
                f"The {same[0].length} period {start} to {end} carries the same value, {money(value)}, in "
                f"{len(same)} filings across forms {', '.join(sorted({f.form for f in same}))} (first in "
                f"{first_q.cite()} and {first_k.cite()}). Summing or counting rows double-counts it.",
                [(f, ctx) for f in cited],
                "Deduplicate on (period_start, period_end) and keep one row per period, preferring the latest "
                "filing.",
            ))
            break  # one duplicate finding per period
    return out


def find_restatements(facts: list[Fact], ctx: SeriesContext) -> list[Finding]:
    out = []
    for (start, end), group in _groups(facts).items():
        ordered = sorted(group, key=Fact.sort_filed)
        changes: list[Fact] = []
        previous = ordered[0].value
        for f in ordered[1:]:
            if f.value != previous:
                changes.append(f)
                previous = f.value
        if not changes:
            continue
        first, last = ordered[0], changes[-1]
        delta = last.value - first.value
        pct = f" ({delta / first.value * 100:+.2f}%)" if first.value else ""
        steps = "; ".join(f"{money(c.value)} in {c.cite()}" for c in changes)
        out.append(_make(
            ctx, "restatement", _period(start, end),
            f"The {first.length} period {start} to {end} was first reported as {money(first.value)} in "
            f"{first.cite()}, then as {steps}: a change of {money(delta) if delta >= 0 else '-' + money(-delta)}"
            f"{pct} from the original.",
            [(first, ctx)] + [(c, ctx) for c in changes],
            f"Use the latest value, {money(last.value)} (filed {last.filed}), and note the restatement when "
            f"comparing with figures taken from the original filing.",
        ))
    return out


def find_early_stop(facts: list[Fact], ctx: SeriesContext) -> list[Finding]:
    if not facts:
        return []
    last_end = max(f.end for f in facts)
    through = ctx.expected_through
    if last_end >= through:
        return []
    last = sorted((f for f in facts if f.end == last_end), key=lambda f: (not f.is_annual, f.sort_filed()))
    head = last[0]
    return [_make(
        ctx, "early_stop", _period(None, last_end),
        f"The {short_concept(ctx.concept)} series stops at the period ending {last_end}: its last value, "
        f"{money(head.value)} for the {head.length} period {head.start} to {head.end}, comes from {head.cite()}. "
        f"No later period is reported under this concept, though the request expects one ending on or after "
        f"{through}.",
        [(f, ctx) for f in last],
        f"Check whether the company moved revenue to another XBRL concept after {last_end}; until then treat "
        f"later periods as missing under this tag, not as zero.",
    )]


def find_late_start(facts: list[Fact], ctx: SeriesContext) -> list[Finding]:
    starts = [f for f in facts if f.start is not None]
    if not starts:
        return []
    first_start = min(f.start for f in starts)  # type: ignore[type-var]
    if first_start <= ctx.late_after:
        return []
    first = sorted((f for f in starts if f.start == first_start), key=lambda f: (not f.is_annual, f.sort_filed()))
    head = first[0]
    return [_make(
        ctx, "late_start", _period(first_start, head.end),
        f"The {short_concept(ctx.concept)} series starts late: its earliest period begins {first_start} "
        f"({head.length}, {money(head.value)}, from {head.cite()}), while the request covers fiscal years from "
        f"{ctx.from_year}.",
        [(f, ctx) for f in first],
        f"Look for periods before {first_start} under another revenue concept before using this series for "
        f"history earlier than {first_start.year}.",
    )]


def analyze_series(rows: Iterable[Mapping[str, Any]], ctx: SeriesContext) -> list[Finding]:
    """Every single-concept finding for one company's series, in a stable order."""
    facts = facts_from_rows(rows)
    return (find_early_stop(facts, ctx) + find_late_start(facts, ctx) + find_gaps(facts, ctx)
            + find_restatements(facts, ctx) + find_duplicates(facts, ctx))


def compare_concepts(
    primary_rows: Iterable[Mapping[str, Any]], primary: SeriesContext,
    alternate_rows: Iterable[Mapping[str, Any]], alternate: SeriesContext,
) -> list[Finding]:
    """The tag change between two revenue concepts: where the older series ends and the
    newer begins, and the gap between them when there is one."""
    p, a = facts_from_rows(primary_rows), facts_from_rows(alternate_rows)
    if not p or not a:
        return []
    series = [(p, primary), (a, alternate)]

    def first_start(fs: list[Fact]) -> date:
        return min((f.start or f.end) for f in fs)

    def last_end(fs: list[Fact]) -> date:
        return max(f.end for f in fs)

    (old, old_ctx), (new, new_ctx) = sorted(series, key=lambda s: (first_start(s[0]), last_end(s[0])))
    if last_end(old) >= last_end(new):
        return []  # the newer concept never outlasts the older one: no hand-over to report
    old_last = last_end(old)
    new_first = first_start(new)
    # The hand-over: where the newer concept takes over. When the newer concept also
    # reports earlier periods (comparatives restated under the new tag), that is the first
    # of its periods starting after the older series ends, not its first period.
    later = [f.start for f in new if f.start and f.start > old_last]
    handover = min(later) if new_first <= old_last and later else new_first

    def rank(f: Fact) -> tuple[bool, tuple]:
        return (not f.is_annual, f.sort_filed())

    old_cited = sorted((f for f in old if f.end == old_last), key=rank)[:2]
    new_cited = sorted((f for f in new if (f.start or f.end) == handover), key=rank)[:2]
    if handover != new_first:
        new_cited += sorted((f for f in new if (f.start or f.end) == new_first), key=rank)[:1]
    cited = [(f, old_ctx) for f in old_cited] + [(f, new_ctx) for f in new_cited]
    pair = f"{old_ctx.concept} -> {new_ctx.concept}"
    old_name, new_name = short_concept(old_ctx.concept), short_concept(new_ctx.concept)
    uncovered = (handover - old_last).days - 1
    if uncovered <= GAP_TOLERANCE_DAYS:
        overlap = (f" {new_name} also reports {new_first} to {old_last} ({new_cited[-1].cite()}), the comparatives "
                   f"of later filings, so both concepts cover those periods." if new_first <= old_last else "")
        return [_make(
            primary, "tag_change", f"{old_last}/{handover}",
            f"Revenue moves from {old_name} to {new_name}: the last {old_name} period ends {old_last} "
            f"({old_cited[0].cite()}) and {new_name} continues from {handover} ({new_cited[0].cite()}). "
            f"The series is continuous across the tag change, so the stop under {old_name} is not missing "
            f"revenue.{overlap}",
            cited,
            f"Join the two concepts into one series: {old_name} through {old_last}, {new_name} from {handover}.",
            concept=pair,
        )]
    first_missing, last_missing = old_last + timedelta(days=1), new_first - timedelta(days=1)
    years = max(1, round((uncovered + 1) / 365.25))
    return [
        _make(
            primary, "tag_change", f"{old_last}/{new_first}",
            f"Revenue moves from {old_name} to {new_name}, with a hole between them: the last {old_name} period "
            f"ends {old_last} ({old_cited[0].cite()}) and the first {new_name} period starts {new_first} "
            f"({new_cited[0].cite()}).",
            cited,
            f"Join the two concepts into one series, {old_name} through {old_last} and {new_name} from "
            f"{new_first}, and fill {first_missing} to {last_missing} from the filings themselves.",
            concept=pair,
        ),
        _make(
            primary, "gap", _period(first_missing, last_missing),
            f"No revenue under either {old_name} or {new_name} for {first_missing} to {last_missing}, about "
            f"{years} fiscal year(s): {old_name} ends {old_last} and {new_name} starts {new_first}.",
            cited,
            f"Treat {first_missing.year}-{last_missing.year} as missing in both concepts; take those years from "
            f"the filings themselves or another concept before using the series.",
            concept=pair,
        ),
    ]


def find_reference_mismatch(
    company: str, cik: str | None, dq_records: Iterable[Mapping[str, Any]], dq_table: str, dq_statement: str | None,
) -> list[Finding]:
    """A company whose SIC code fails the semantic layer's foreign-key rule to the SIC
    reference (the Zetaris data-quality run reports the failing rows)."""
    want = (cik or "").lstrip("0")
    out = []
    for record in dq_records:
        if not want or str(record.get("cik") or "").lstrip("0") != want:
            continue
        sic = str(record.get("sic") or record.get("sic_code") or "?")
        rid = f"cik:{record.get('cik')}"
        out.append(Finding(
            id=finding_id(company, "SIC reference", "other", sic),
            company=company, cik=cik, concept="SIC reference", period=None, kind="other",
            description=(f"SEC files {company} under SIC {sic} ({record.get('sic_description_edgar') or 'no description'}), "
                         f"a code absent from the 1,005-code SIC reference: Zetaris's foreign-key rule from "
                         f"sic_edgar_usl.company.sic to sic_code fails for this company, so an industry join drops it "
                         f"or leaves its industry columns empty."),
            evidence=[{**dict(record), "row_id": rid, "table": dq_table}],
            confidence=0.9,
            sources=[Source(table=dq_table, statement=dq_statement, row_ids=[rid])],
            recommendation=(f"Map SIC {sic} to its industry group ({sic[:3]}) by hand before comparing {company} with "
                            f"its industry peers."),
        ))
    return out


# --- the agent ------------------------------------------------------------------------------


def summarize_kinds(findings: Iterable[Finding]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for f in findings:
        counts[f.kind] += 1
    return ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "none"


class Analyst:
    """Turns one company's rows into findings and re-derives a finding the Evaluator sent
    back. Code only; the explanation is written by the model in the engine step."""

    name = AGENT

    def __init__(self, tracer: Tracer | None = None) -> None:
        self.tracer = tracer or NullTracer()

    def analyze(self, rows: list[Mapping[str, Any]], ctx: SeriesContext) -> list[Finding]:
        findings = analyze_series(rows, ctx)
        self.tracer.emit(
            self.name, "analyze_series",
            input_summary=f"{ctx.company} {ctx.concept}: {len(rows)} rows from {ctx.table}",
            output_summary=f"{len(findings)} findings ({summarize_kinds(findings)})",
            target_agent="Evaluator",
            details={"company": ctx.company, "concept": ctx.concept, "table": ctx.table,
                     "finding_ids": [f.id for f in findings]},
        )
        return findings

    def compare(
        self, primary_rows: list[Mapping[str, Any]], primary: SeriesContext,
        alternate_rows: list[Mapping[str, Any]], alternate: SeriesContext,
    ) -> list[Finding]:
        findings = compare_concepts(primary_rows, primary, alternate_rows, alternate)
        self.tracer.emit(
            self.name, "compare_concepts",
            input_summary=f"{primary.company}: {len(primary_rows)} {short_concept(primary.concept)} rows, "
                          f"{len(alternate_rows)} {short_concept(alternate.concept)} rows",
            output_summary=f"{len(findings)} findings ({summarize_kinds(findings)})",
            target_agent="Evaluator",
            details={"company": primary.company, "finding_ids": [f.id for f in findings]},
        )
        return findings

    def revise(
        self, finding: Finding, reason: str, rows: list[Mapping[str, Any]], ctx: SeriesContext,
        alternate_rows: list[Mapping[str, Any]] | None = None, alternate: SeriesContext | None = None,
    ) -> Finding | None:
        """Re-derive a finding from the rows after the Evaluator sent it back. Returns the
        recomputed finding (same id, fresh evidence) or None when the rows do not yield it."""
        if finding.kind == "other":  # reference data: the evidence is the data-quality record itself
            fresh = [finding] if finding.evidence else []
        elif " -> " in (finding.concept or ""):
            fresh = compare_concepts(rows, ctx, alternate_rows or [], alternate) if alternate else []
        else:
            fresh = analyze_series(rows, ctx)
        match = next((f for f in fresh if f.id == finding.id), None)
        if match is not None:
            match = match.model_copy(update={"revision_count": finding.revision_count + 1})
        self.tracer.emit(
            self.name, "revise_finding",
            input_summary=f"{finding.kind} {finding.period} sent back: {reason}",
            output_summary=(f"re-derived from {len(rows)} rows with {len(match.evidence)} evidence rows"
                            if match else "the rows do not yield this finding"),
            target_agent="Evaluator",
            retry_count=finding.revision_count + 1,
            status="success" if match else "needs_validation",
            details={"finding_id": finding.id},
        )
        return match

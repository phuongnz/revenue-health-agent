"""The Analyst's arithmetic on fixture rows: Apple's 2018 stop under us-gaap/Revenues,
the 10-K/10-Q duplicate in Apple's FY2019 rows, the tag change between the two, and a
synthetic gap and restatement. Every finding must cite rows that exist."""

from datetime import date

import pytest

from app.agents.analyst import (
    Analyst,
    SeriesContext,
    analyze_series,
    compare_concepts,
    facts_from_rows,
    row_id,
)
from tests.edgar_rows import APPLE_RFCC_FY2019, APPLE_REVENUES, SYNTHETIC_GAP_AND_RESTATEMENT

AS_OF = date(2026, 10, 26)
OLD = SeriesContext("Apple", "us-gaap/Revenues", "edgar.apple_revenue_table", cik="320193", as_of=AS_OF)
NEW = OLD.with_concept("us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax", "edgar.apple_rfcc_table")


def kinds(findings):
    return sorted(f.kind for f in findings)


def cited_ids(finding):
    return [rid for s in finding.sources for rid in s.row_ids]


def assert_cites_real_rows(finding, *row_sets):
    known = {row_id(r) for rows in row_sets for r in rows}
    ids = cited_ids(finding)
    assert ids, f"{finding.kind} cites no rows"
    assert set(ids) <= known
    assert [e["row_id"] for e in finding.evidence] == ids


def test_apple_2018_stop_is_found_with_the_fy2018_rows():
    findings = analyze_series(APPLE_REVENUES, OLD)
    stop = [f for f in findings if f.kind == "early_stop"]
    assert len(stop) == 1
    stop = stop[0]
    assert_cites_real_rows(stop, APPLE_REVENUES)
    # the annual FY2018 row first, then the Q4 row ending the same day
    assert cited_ids(stop) == ["0000320193-18-000145:2017-10-01:2018-09-29", "0000320193-18-000145:2018-07-01:2018-09-29"]
    assert "2018-09-29" in stop.description and "0000320193-18-000145" in stop.description
    assert stop.period == "/2018-09-29"
    assert stop.sources[0].table == "edgar.apple_revenue_table"
    assert stop.recommendation


def test_apple_revenues_also_starts_late_and_has_no_gap_duplicate_or_restatement():
    findings = analyze_series(APPLE_REVENUES, OLD)
    assert kinds(findings) == ["early_stop", "late_start"]
    late = next(f for f in findings if f.kind == "late_start")
    assert cited_ids(late) == ["0000320193-18-000145:2015-09-27:2016-09-24"]
    assert "2015-09-27" in late.description


def test_ten_k_ten_q_duplicate_in_apple_fy2019():
    findings = analyze_series(APPLE_RFCC_FY2019, NEW)
    dup = [f for f in findings if f.kind == "duplicate"]
    assert len(dup) == 1, "only the quarter is in both forms; the annual rows are all 10-K"
    dup = dup[0]
    assert dup.period == "2018-09-30/2018-12-29"
    assert_cites_real_rows(dup, APPLE_RFCC_FY2019)
    ids = cited_ids(dup)
    assert ids[:2] == ["0000320193-19-000010:2018-09-30:2018-12-29", "0000320193-19-000119:2018-09-30:2018-12-29"]
    assert len(ids) == 4
    assert {e["form_type"] for e in dup.evidence} == {"10-Q", "10-K"}
    assert "$84,310,000,000" in dup.description
    assert not [f for f in findings if f.kind == "restatement"], "same value in every filing"


def test_tag_change_from_revenues_to_rfcc_is_continuous():
    findings = compare_concepts(APPLE_REVENUES, OLD, APPLE_RFCC_FY2019, NEW)
    assert kinds(findings) == ["tag_change"]
    tag = findings[0]
    assert tag.concept == "us-gaap/Revenues -> us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax"
    assert tag.period == "2018-09-29/2018-09-30"
    assert {s.table for s in tag.sources} == {"edgar.apple_revenue_table", "edgar.apple_rfcc_table"}
    assert_cites_real_rows(tag, APPLE_REVENUES, APPLE_RFCC_FY2019)
    assert "continuous" in tag.description
    # the comparison does not depend on argument order
    assert [f.id for f in compare_concepts(APPLE_RFCC_FY2019, NEW, APPLE_REVENUES, OLD)] != []


def test_tag_change_hands_over_after_the_old_series_when_the_new_concept_has_comparatives():
    # The FY2019 10-K also reports FY2017 under the new tag (live: edgar.apple_rfcc_table)
    fy2017 = dict(APPLE_RFCC_FY2019[4], period_start="2016-09-25", period_end="2017-09-30", value_usd=229234000000)
    rfcc = [fy2017, *APPLE_RFCC_FY2019]
    [tag] = compare_concepts(APPLE_REVENUES, OLD, rfcc, NEW)
    assert tag.period == "2018-09-29/2018-09-30", "the hand-over, not the first comparative"
    assert "continues from 2018-09-30" in tag.description and "also reports 2016-09-25 to 2018-09-29" in tag.description
    assert "0000320193-19-000119:2016-09-25:2017-09-30" in cited_ids(tag)
    assert tag.recommendation.endswith("from 2018-09-30.")
    from app.agents.evaluator import Evidence, check_support
    assert check_support(tag, Evidence(APPLE_REVENUES, OLD, rfcc, NEW)) is None


def test_gap_and_restatement_on_synthetic_rows():
    ctx = SeriesContext("Synthetic", "us-gaap/Revenues", "edgar.synthetic_revenue_table", as_of=date(2015, 6, 1),
                        period_start="2010")
    findings = analyze_series(SYNTHETIC_GAP_AND_RESTATEMENT, ctx)
    assert kinds(findings) == ["gap", "restatement"]
    gap = next(f for f in findings if f.kind == "gap")
    assert gap.period == "2013-01-01/2013-12-31"
    assert cited_ids(gap) == ["S-12:2012-01-01:2012-12-31", "S-14:2014-01-01:2014-12-31"]
    restated = next(f for f in findings if f.kind == "restatement")
    assert cited_ids(restated) == ["S-11:2011-01-01:2011-12-31", "S-12:2011-01-01:2011-12-31"]
    assert "$1,100" in restated.description and "$1,150" in restated.description and "+4.55%" in restated.description


def test_window_from_the_request_moves_the_early_stop_and_late_start_lines():
    within = SeriesContext("Apple", "us-gaap/Revenues", "t", period_start="2016", period_end="2018", as_of=AS_OF)
    assert analyze_series(APPLE_REVENUES, within) == []
    assert within.expected_through == date(2018, 1, 1)
    later = SeriesContext("Apple", "us-gaap/Revenues", "t", period_end="2020", as_of=AS_OF)
    assert "early_stop" in kinds(analyze_series(APPLE_REVENUES, later))
    future = SeriesContext("Apple", "us-gaap/Revenues", "t", period_end="2030", as_of=AS_OF)
    assert future.expected_through == date(2025, 10, 26)


def test_ids_are_stable_and_revise_rederives_a_finding_sent_back():
    first = analyze_series(APPLE_REVENUES, OLD)
    assert [f.id for f in first] == [f.id for f in analyze_series(APPLE_REVENUES, OLD)]
    stop = next(f for f in first if f.kind == "early_stop")
    tampered = stop.model_copy(update={"evidence": [], "sources": []})
    revised = Analyst().revise(tampered, "evidence missing", APPLE_REVENUES, OLD)
    assert revised is not None and revised.id == stop.id and revised.revision_count == 1
    assert cited_ids(revised) == cited_ids(stop)
    invented = stop.model_copy(update={"id": "restatement-0000000000", "kind": "restatement"})
    assert Analyst().revise(invented, "no such change", APPLE_REVENUES, OLD) is None


@pytest.mark.parametrize("bad", [{"period_end": None}, {"value_usd": None}, {"period_end": "not a date"}])
def test_rows_without_a_period_or_value_are_skipped(bad):
    row = {**APPLE_REVENUES[0], **bad}
    assert facts_from_rows([row]) == []


def test_analyze_traces_a_handoff_to_the_evaluator():
    from app.tracing import NullTracer

    tracer = NullTracer()
    Analyst(tracer).analyze(APPLE_REVENUES, OLD)
    event = tracer.events[-1]
    assert (event.agent_name, event.action, event.target_agent) == ("Analyst", "analyze_series", "Evaluator")
    assert "early_stop" in event.output_summary

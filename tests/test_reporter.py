"""The Reporter: accepted findings only, the §4 result shape, Markdown with cited rows."""

import json
from datetime import date

from app.agents.analyst import SeriesContext, analyze_series, compare_concepts
from app.agents.reporter import Explanation, ReportInput, Reporter, build_result
from app.schemas import RunResult
from tests.edgar_rows import APPLE_RFCC_FY2019, APPLE_REVENUES

AS_OF = date(2026, 10, 26)
OLD = SeriesContext("Apple", "us-gaap/Revenues", "edgar.apple_revenue_table", as_of=AS_OF)
NEW = OLD.with_concept("us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax", "edgar.apple_rfcc_table")


def apple_findings():
    found = analyze_series(APPLE_REVENUES, OLD) + compare_concepts(APPLE_REVENUES, OLD, APPLE_RFCC_FY2019, NEW)
    found += [f for f in analyze_series(APPLE_RFCC_FY2019, NEW) if f.kind == "duplicate"]
    accepted = [f.model_copy(update={"status": "accepted"}) for f in found]
    rejected = accepted[1].model_copy(update={"status": "rejected", "id": "late_start-rejected"})
    return accepted, rejected


def test_only_accepted_findings_are_reported_in_the_contract_shape(tmp_path):
    accepted, rejected = apple_findings()
    info = ReportInput("r1", ["Apple"], explanations=[Explanation("Apple", "Apple Revenues", "The model's paragraph.")],
                       limitations=["one"], evaluator_notes=["Rejected late start: wording"], steps_planned=2, steps_with_rows=2,
                       rejected=1, agents_used=["Planner", "Retriever", "Analyst", "Evaluator", "Reporter"])
    result, markdown, paths = Reporter().report(accepted + [rejected], info, tmp_path)
    assert {f.status for f in result.findings} == {"accepted"}
    assert "late_start-rejected" not in {f.id for f in result.findings}
    assert [f.kind for f in result.findings][:2] == ["tag_change", "early_stop"]
    assert result.summary.startswith("Apple: the Revenues series stops at 2018-09-29 and continues under "
                                     "RevenueFromContractWithCustomerExcludingAssessedTax from 2018-09-30, a tag change")
    assert "Revenues series stops at 2018-09-29;" not in result.summary, "the stop is told once, as the tag change"
    assert not any("after 2018-09-29" in a for a in result.recommended_actions)
    assert "1 rejected" in result.summary
    assert 0 < result.confidence <= 1
    assert result.limitations[0] == "one" and len(result.limitations) == 4
    assert any("deduplicate" in a.lower() for a in result.recommended_actions)
    # JSON on disk is the result, and the Markdown cites rows and carries the model's words
    on_disk = RunResult.model_validate(json.loads((tmp_path / "report-r1.json").read_text()))
    assert on_disk == result and paths["markdown"].endswith("report-r1.md")
    assert "`0000320193-18-000145:2017-10-01:2018-09-29`" in markdown
    assert "The model's paragraph." in markdown and "Rejected late start" in markdown
    assert "Duplicated across 10-K and 10-Q forms" in markdown
    assert all(f.sources and f.sources[0].row_ids for f in result.findings)


def test_a_clean_company_and_partial_coverage():
    info = ReportInput("r2", ["IBM", "Apple"], steps_planned=2, steps_with_rows=1)
    result = build_result([], info)
    assert result.findings == []
    assert result.summary.startswith("IBM: no problem found")
    assert result.confidence == round(0.85 * 0.5, 3)


def test_nothing_read_says_so():
    result = build_result([], ReportInput("r3", ["Apple"], steps_planned=1, steps_with_rows=0))
    assert result.summary.startswith("No revenue rows could be read") and result.confidence == 0


# The live Apple run (phase 3) printed these next to a code finding that the series is
# continuous across the tag change: the RFCC step's explanation and the engine's reflection.
CONTRADICTING = (
    "The RevenueFromContractWithCustomerExcludingAssessedTax series begins in fiscal 2016, so it cannot represent the "
    "full 2009-2025 request, creating a coverage gap that earlier accepted early_stop and late_start flags for the "
    "primary concept do not resolve.")
REFLECTION = ("Audit complete. The RevenueFromContractWithCustomerExcludingAssessedTax concept starts later (FY2016) and "
              "does not bridge the gap. **Verdict**: Refine.")


def test_prose_that_contradicts_a_code_finding_never_reaches_the_report(tmp_path):
    accepted, _ = apple_findings()
    agreeing = ("Apple's revenue continues under RevenueFromContractWithCustomerExcludingAssessedTax from 2018-09-30, "
                "so the series is continuous across the tag change.")
    info = ReportInput(
        "r4", ["Apple"], steps_planned=2, steps_with_rows=2,
        explanations=[
            Explanation("Apple", "Apple us-gaap/Revenues", agreeing, given="handover 2018-09-30", fallback="code one"),
            Explanation("Apple", "Apple RFCC", CONTRADICTING, given="", fallback="Apple RFCC: the findings' summary."),
        ],
        critiques=[("Apple", "Apple Revenues, round 1: " + REFLECTION), ("Apple", "Apple RFCC, round 1: Supported.")],
    )
    result, markdown, _ = Reporter().report(accepted, info, tmp_path)
    assert agreeing in markdown, "prose that agrees with the findings is printed"
    assert "do not resolve" not in markdown and "coverage gap" not in markdown
    assert "Apple RFCC: the findings' summary." in markdown, "the findings speak alone in its place"
    assert "does not bridge" not in markdown and "Verdict" not in markdown and "Refine" not in markdown
    assert "Apple RFCC, round 1: Supported." in markdown
    assert "replaced by the findings' own summary: it denies that the new concept carries" in markdown
    on_disk = (tmp_path / "report-r4.json").read_text()
    assert "do not resolve" not in on_disk and "bridge" not in on_disk


def test_a_review_reason_that_contradicts_the_findings_is_not_kept():
    accepted, _ = apple_findings()
    stop = next(f for f in accepted if f.kind == "early_stop")
    accepted = [f if f is not stop else stop.model_copy(update={"evaluator_reason": "A gap follows the last period."})
                for f in accepted]
    result = build_result(accepted, ReportInput("r5", ["Apple"], steps_planned=2, steps_with_rows=2))
    reason = next(f.evaluator_reason for f in result.findings if f.kind == "early_stop")
    assert "gap" not in reason and reason.startswith("the cited rows support it")


def test_apples_old_concept_starts_where_apple_began_using_the_tag():
    """Apple's us-gaap/Revenues rows begin with FY2016, a comparative in the FY2018 10-K, the
    first filing that used the tag: where the tag starts, not a hole in Apple's revenue."""
    found = [f.model_copy(update={"status": "accepted"}) for f in analyze_series(APPLE_REVENUES, OLD)]
    late = next(f for f in found if f.kind == "late_start")
    assert "began using this concept late" in late.description and "not revenue missing" in late.description
    result = build_result(found, ReportInput("r6", ["Apple"], steps_planned=1, steps_with_rows=1))
    assert "the Revenues series starts at 2015-09-27, where the company began using the tag" in result.summary
    assert "starts late" not in result.summary

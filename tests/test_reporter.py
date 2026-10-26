"""The Reporter: accepted findings only, the §4 result shape, Markdown with cited rows."""

import json
from datetime import date

from app.agents.analyst import SeriesContext, analyze_series, compare_concepts
from app.agents.reporter import ReportInput, Reporter, build_result
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
    info = ReportInput("r1", ["Apple"], explanations={"Apple": ["The model's paragraph."]}, limitations=["one"],
                       evaluator_notes=["Rejected late start: wording"], steps_planned=2, steps_with_rows=2,
                       rejected=1, agents_used=["Planner", "Retriever", "Analyst", "Evaluator", "Reporter"])
    result, markdown, paths = Reporter().report(accepted + [rejected], info, tmp_path)
    assert {f.status for f in result.findings} == {"accepted"}
    assert "late_start-rejected" not in {f.id for f in result.findings}
    assert [f.kind for f in result.findings][:2] == ["tag_change", "early_stop"]
    assert result.summary.startswith("Apple: revenue moves from Revenues to RevenueFromContractWithCustomerExcludingAssessedTax")
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

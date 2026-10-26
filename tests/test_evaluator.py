"""The Evaluator's branches: the truncated-series retry (and its fallback when no other
concept is registered), the unsupported finding sent back to the Analyst once, and the
model critique with its code-only fallback. The model is a scripted fake."""

import json
from dataclasses import dataclass
from datetime import date

import pytest

from app.agents.analyst import Analyst, SeriesContext, analyze_series, compare_concepts
from app.agents.evaluator import Evaluator, Evidence, check_explanation, check_support, parse_reviews
from app.schemas import Source
from app.tracing import NullTracer
from tests.edgar_rows import APPLE_RFCC_FY2019, APPLE_REVENUES

AS_OF = date(2026, 10, 26)
OLD = SeriesContext("Apple", "us-gaap/Revenues", "edgar.apple_revenue_table", as_of=AS_OF)
NEW = OLD.with_concept("us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax", "edgar.apple_rfcc_table")
RFCC = ("us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax", "edgar.apple_rfcc_table")


@dataclass
class Reply:
    text: str
    routed_model: str = "nvidia/nemotron-3-ultra-550b-a55b"


class ScriptedModel:
    """Answers each critique call with the next scripted verdict map {kind: verdict}."""

    def __init__(self, *rounds, fail=False):
        self.rounds = list(rounds)
        self.fail = fail
        self.calls = []

    def chat(self, agent, messages, **kw):
        self.calls.append((agent, kw.get("max_tokens"), messages[-1]["content"]))
        if self.fail:
            raise RuntimeError("502 empty_response after 3 attempts")
        verdicts = self.rounds.pop(0)
        reviews = []
        for line in messages[-1]["content"].splitlines():
            if line.startswith("F") and " [" in line:
                key, kind = line.split(" [", 1)
                kind = kind.split("]", 1)[0]
                verdict = verdicts.get(kind, "accept")
                reviews.append({"id": key, "verdict": verdict, "reason": f"{verdict} {kind}", "confidence": 0.9})
        return Reply("<think>checking rows</think>" + json.dumps({"reviews": reviews, "critique": "Looks sound."}))


def evaluate(findings, evidence, model=None, **kw):
    tracer = NullTracer()
    out = Evaluator(tracer, chat=model).evaluate(findings, evidence, Analyst(tracer), **kw)
    return out, tracer.events


def actions(events):
    return [(e.agent_name, e.action, e.status) for e in events]


# --- the truncated-series branch -------------------------------------------------------------


def test_truncated_series_requests_a_retry_with_the_alternate_concept():
    findings = analyze_series(APPLE_REVENUES, OLD)
    out, events = evaluate(findings, Evidence(APPLE_REVENUES, OLD), alternate=RFCC, may_retry=True)
    assert sorted(f.kind for f in out.accepted) == ["early_stop", "late_start"]
    assert out.retry is not None and out.retry.table == "edgar.apple_rfcc_table"
    assert set(out.retry.finding_ids) == {f.id for f in findings}
    retry = next(e for e in events if e.action == "request_retry")
    assert (retry.target_agent, retry.status) == ("Retriever", "retrying")
    assert events[-1].action == "evaluate" and events[-1].status == "retrying"


def test_truncated_series_without_an_alternate_takes_the_fallback():
    findings = analyze_series(APPLE_REVENUES, OLD)
    out, events = evaluate(findings, Evidence(APPLE_REVENUES, OLD), alternate=None, may_retry=True)
    assert out.retry is None
    assert out.limitations and "no other revenue concept" in out.limitations[0]
    assert ("Evaluator", "retry_fallback", "partial_success") in actions(events)


def test_a_retry_step_does_not_retry_again():
    findings = analyze_series(APPLE_REVENUES, OLD)
    out, events = evaluate(findings, Evidence(APPLE_REVENUES, OLD), alternate=RFCC, may_retry=False)
    assert out.retry is None and not any(e.action in ("request_retry", "retry_fallback") for e in events)


def test_the_tag_change_after_the_retry_is_supported_by_both_tables():
    findings = compare_concepts(APPLE_REVENUES, OLD, APPLE_RFCC_FY2019, NEW)
    evidence = Evidence(APPLE_REVENUES, OLD, APPLE_RFCC_FY2019, NEW)
    assert check_support(findings[0], evidence) is None
    out, _ = evaluate(findings, evidence)
    assert [f.kind for f in out.accepted] == ["tag_change"]


# --- the unsupported branch ---------------------------------------------------------------------


def test_a_finding_citing_missing_rows_goes_back_once_and_is_accepted_when_rederived():
    stop = next(f for f in analyze_series(APPLE_REVENUES, OLD) if f.kind == "early_stop")
    broken = stop.model_copy(update={"sources": [Source(table=OLD.table, row_ids=["0000320193-18-000145:x:y"])]})
    assert "not in the retrieved data" in check_support(broken, Evidence(APPLE_REVENUES, OLD))
    out, events = evaluate([broken], Evidence(APPLE_REVENUES, OLD))
    assert out.revision_count == 1
    assert [f.id for f in out.accepted] == [stop.id]
    assert out.accepted[0].revision_count == 1 and out.accepted[0].sources == stop.sources
    send = next(e for e in events if e.action == "send_back")
    assert (send.target_agent, send.status, send.retry_count) == ("Analyst", "needs_revision", 1)
    assert ("Analyst", "revise_finding", "success") in actions(events)


def test_a_finding_the_rows_do_not_support_is_recorded_unsupported_after_one_revision():
    dup = next(f for f in analyze_series(APPLE_RFCC_FY2019, NEW) if f.kind == "duplicate")
    # claim a restatement on rows that all carry one value
    invented = dup.model_copy(update={"id": "restatement-invented00", "kind": "restatement"})
    out, events = evaluate([invented], Evidence(APPLE_RFCC_FY2019, NEW))
    assert out.accepted == [] and [f.status for f in out.unsupported] == ["unsupported"]
    assert "same value" in out.unsupported[0].evaluator_reason
    assert out.revision_count == 1
    sent = [e for e in events if e.action == "send_back"]
    assert len(sent) == 1, "sent back once, not again"
    assert ("Evaluator", "record_unsupported", "needs_validation") in actions(events)


# --- the critique ---------------------------------------------------------------------------------


def test_the_critique_runs_on_the_evaluator_lane_with_room_to_reason_and_blends_confidence():
    model = ScriptedModel({})
    findings = analyze_series(APPLE_RFCC_FY2019, NEW)
    out, events = evaluate(findings, Evidence(APPLE_RFCC_FY2019, NEW), model)
    assert model.calls[0][0] == "Evaluator" and model.calls[0][1] >= 8192
    assert out.model_reviewed and out.critiques == [("Apple", "Apple RevenueFromContractWithCustomerExcludingAssessedTax, round 1: Looks sound.")]
    dup = next(f for f in out.accepted if f.kind == "duplicate")
    assert dup.confidence == pytest.approx((0.95 + 0.9) / 2)
    critique = next(e for e in events if e.action == "critique")
    assert critique.model == "nvidia/nemotron-3-ultra-550b-a55b" and critique.status == "success"


def test_a_critique_rejection_goes_back_to_the_analyst_and_a_second_rejection_stands():
    model = ScriptedModel({"late_start": "reject"}, {"late_start": "reject"})
    findings = analyze_series(APPLE_REVENUES, OLD)
    out, events = evaluate(findings, Evidence(APPLE_REVENUES, OLD), model)
    assert [f.kind for f in out.accepted] == ["early_stop"]
    assert [(f.kind, f.status, f.evaluator_reason) for f in out.rejected] == [("late_start", "rejected", "reject late_start")]
    assert len(model.calls) == 2 and "analyst note: re-derived" in model.calls[1][2]
    statuses = [e.status for e in events if e.action == "critique"]
    assert statuses == ["needs_revision", "needs_revision"]


def test_a_critique_rejection_withdrawn_after_revision_is_accepted():
    model = ScriptedModel({"late_start": "reject"}, {})
    out, _ = evaluate(analyze_series(APPLE_REVENUES, OLD), Evidence(APPLE_REVENUES, OLD), model)
    assert sorted(f.kind for f in out.accepted) == ["early_stop", "late_start"]
    late = next(f for f in out.accepted if f.kind == "late_start")
    assert late.revision_count == 1 and out.revision_count == 1


def test_a_failed_model_call_falls_back_to_the_code_verdicts():
    model = ScriptedModel(fail=True)
    out, events = evaluate(analyze_series(APPLE_REVENUES, OLD), Evidence(APPLE_REVENUES, OLD), model)
    assert len(out.accepted) == 2 and not out.model_reviewed
    assert any("unavailable" in line for line in out.limitations)
    assert ("Evaluator", "critique", "partial_success") in actions(events)


def test_parse_reviews_reads_fenced_json_and_refuses_prose():
    reviews, critique = parse_reviews('```json\n{"reviews":[{"id":"F1","verdict":"Reject","reason":"r","confidence":2}],"critique":"c"}\n```')
    assert reviews == {"F1": {"verdict": "reject", "reason": "r", "confidence": None}} and critique == "c"
    with pytest.raises(ValueError):
        parse_reviews("I accept them all.")


def test_no_findings_is_a_clean_series():
    out, events = evaluate([], Evidence([], OLD))
    assert out.all == [] and events[-1].output_summary.startswith("no findings")


def _apple_accepted():
    found = analyze_series(APPLE_REVENUES, OLD) + compare_concepts(APPLE_REVENUES, OLD, APPLE_RFCC_FY2019, NEW)
    return [f.model_copy(update={"status": "accepted"}) for f in found]


@pytest.mark.parametrize("text", [
    # the live Apple run's RFCC explanation and engine reflection, next to a continuous tag change
    "The RFCC series begins in fiscal 2016, creating a coverage gap that the early_stop and late_start flags do not resolve.",
    "The RevenueFromContractWithCustomerExcludingAssessedTax concept starts later and does not bridge the gap.",
    "FY2019 to FY2025 are unaccounted for.",
    "Apple restated its 2018 revenue.",
    "Quarterly values are duplicated across forms.",
    "The series stops at 2019-06-29.",
    "Revenue was $270 billion in the last year.",
])
def test_prose_contradicting_a_finding_is_named(text):
    assert check_explanation(text, _apple_accepted(), given="", company="Apple") is not None


@pytest.mark.parametrize("text", [
    "Apple's revenue continues under the new concept from 2018-09-30: a tag change, not missing revenue.",
    "There is no gap and nothing was restated; the Revenues series starts late and stops early under this tag.",
    "Its last annual value, $265.6 billion, closes the Revenues series at 2018-09-29.",
    "",
])
def test_prose_that_agrees_with_the_findings_passes(text):
    assert check_explanation(text, _apple_accepted(), given="", company="Apple") is None


def test_a_clean_series_must_be_described_as_clean():
    assert check_explanation("The series has gaps.", [], company="IBM") is not None
    assert check_explanation("The series is clean in the window; no gap, duplicate or restatement.", [],
                             company="IBM") is None
    assert check_explanation("It runs 2009-01-01 to 2025-12-31.", [], given="rows covering 2009-01-01..2025-12-31",
                             company="IBM") is None

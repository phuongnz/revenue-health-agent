"""The whole run offline: the Planner's step, the Evaluator's retry with the second
concept as the next engine chunk, the critique, the carry-over, the response shape, and
the fallbacks when the model or a table fails. Zetaris and the model are fakes; the rows
are Apple's real ones from tests/edgar_rows.py."""

import json
from dataclasses import dataclass, field
from datetime import date

from app.adapters.zetaris import QueryResult, ZetarisError, check_statement
from app.config import Settings
from app.orchestration.graph import Orchestrator, run_request
from app.schemas import RunRequest, RunResponse
from app.tracing import Tracer
from tests.edgar_rows import APPLE_RFCC_FY2019, APPLE_REVENUES, COLUMNS

AS_OF = date(2026, 10, 26)
TABLES = ["APPLE_REVENUE_TABLE", "APPLE_RFCC_TABLE", "IBM_REVENUE_TABLE", "ALL_COMPANIES_PROFILE_TABLE"]
IBM_ROWS = [dict(r, cik="51143", entity_name="IBM", period_start=r["period_start"].replace("2015", "2009"))
            for r in APPLE_REVENUES[:1]]


class FakeZetaris:
    def __init__(self, fail=()):
        self.fail = fail
        self.statements = []

    def query(self, sql, limit=5000, purpose=""):
        check_statement(sql)
        self.statements.append(sql)
        if any(f in sql for f in self.fail):
            raise ZetarisError("HTTP 503 after 3 attempts")
        if sql.startswith("SHOW TABLES"):
            return QueryResult(sql, ["table_identifier"], [[t] for t in TABLES])
        if sql.startswith("SHOW DQ"):
            return QueryResult(sql, ["records"], [['{"cik":"0000051143","sic":"3570","sic_description_edgar":"Computer & office Equipment"}']])
        if "profile" in sql:
            return QueryResult(sql, ["cik", "sic_code", "sic_description_edgar"], [["0000320193", "3571", "Electronic Computers"]])
        table = {"apple_revenue_table": APPLE_REVENUES, "apple_rfcc_table": APPLE_RFCC_FY2019,
                 "ibm_revenue_table": IBM_ROWS}
        for name, rows in table.items():
            if name in sql:
                return QueryResult(sql, COLUMNS, [[r[c] for c in COLUMNS] for r in rows])
        return QueryResult(sql, [], [])


@dataclass
class Reply:
    text: str
    routed_model: str
    usage: dict = field(default_factory=dict)

    def metadata(self):
        return {"routed_model": self.routed_model, "receipt": {"routed_model": self.routed_model}, "usage": {}}


class FakeModel:
    def __init__(self, fail_agents=()):
        self.calls = []
        self.fail_agents = fail_agents

    def chat(self, agent, messages, **kw):
        self.calls.append((agent, kw.get("purpose"), messages[-1]["content"]))
        if agent in self.fail_agents:
            raise RuntimeError("502 empty_response")
        if agent == "Evaluator":
            ids = [line.split(" [", 1)[0] for line in messages[-1]["content"].splitlines() if line.startswith("F") and " [" in line]
            body = {"reviews": [{"id": i, "verdict": "accept", "reason": "rows agree", "confidence": 0.8} for i in ids],
                    "critique": "Supported."}
            return Reply(json.dumps(body), "nvidia/nemotron-3-ultra-550b-a55b")
        if kw.get("purpose") == "engine reflection":
            return Reply("The new concept does not bridge the gap. **Verdict**: Refine.", "nvidia/nemotron-3.5-lightning-30b-a3b")
        return Reply("Apple's old revenue tag ends in 2018 and the new one carries on.\n[STATE_CHECKPOINT]\nCOMPLETED: x",
                     "nvidia/nemotron-3.5-lightning-30b-a3b")


def run(tmp_path, zetaris=None, model=None, **inp):
    settings = Settings.from_env({"LOG_DIR": str(tmp_path)})
    request = RunRequest(run_id="t1", input={"query": "Can I trust Apple's revenue series?", **inp})
    tracer = Tracer("t1", tmp_path, stream=None)
    response = Orchestrator(settings, tracer, zetaris or FakeZetaris(), model, as_of=AS_OF).run(request)
    return response, tracer.events


def test_apple_run_retries_with_the_second_concept_and_reports_the_2018_stop(tmp_path):
    model = FakeModel()
    response, events = run(tmp_path, model=model)
    assert response.status == "success", response.result.limitations
    assert response.agents_used == ["Planner", "Retriever", "Analyst", "Evaluator", "Reporter"]
    kinds = [f.kind for f in response.result.findings]
    assert "early_stop" in kinds and "tag_change" in kinds
    stop = next(f for f in response.result.findings if f.kind == "early_stop" and f.concept == "us-gaap/Revenues")
    assert "0000320193-18-000145:2017-10-01:2018-09-29" in stop.sources[0].row_ids
    assert stop.sources[0].statement.startswith("SELECT cik, entity_name")
    actions = [(e.agent_name, e.action) for e in events]
    # the retry branch, the critique, and two bounded engine chunks
    assert ("Evaluator", "request_retry") in actions and ("Orchestrator", "enqueue_retry") in actions
    assert ("Evaluator", "critique") in actions
    chunks = [e for e in events if e.action == "engine_chunk"]
    assert len(chunks) == 2 and events[-1].action == "finish_run"
    dispatched = [e.input_summary for e in events if e.action == "dispatch_step"]
    assert dispatched == ["step 1: Apple us-gaap/Revenues (primary)",
                          "step 2: Apple us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax (retry)"]
    # the second explanation was written with the first step's carry-over, built by code
    explain = [c for c in model.calls if (c[1] or "").startswith("explain step")]
    assert len(explain) == 2 and "[CARRYOVER STATE]" in explain[1][2] and "COMPLETED: Apple Revenues" in explain[1][2]
    assert response.collaboration.feedback_loop_completed
    assert response.result.summary.startswith("Apple: the Revenues series stops at 2018-09-29 and continues under")
    assert (tmp_path / "report-t1.md").exists() and (tmp_path / "trace-t1.jsonl").exists()
    md = (tmp_path / "report-t1.md").read_text()
    assert "Apple's old revenue tag ends in 2018" in md and "COMPLETED: x" not in md
    assert "Verdict" not in md and "bridge" not in md, "the engine's reflection stays in the trace"
    RunResponse.model_validate(response.model_dump(mode="json"))


def test_ibm_takes_the_retry_fallback_and_reports_the_sic_reference_problem(tmp_path):
    response, events = run(tmp_path, model=FakeModel(), companies=["IBM"])
    kinds = {f.kind for f in response.result.findings}
    assert "other" in kinds, "IBM's SIC code fails the reference rule"
    assert ("Evaluator", "retry_fallback") in [(e.agent_name, e.action) for e in events]
    assert any("no other revenue concept" in line for line in response.result.limitations)


def test_without_a_model_the_run_still_reports_on_code_verdicts(tmp_path):
    response, events = run(tmp_path, model=None)
    assert response.status == "partial_success"
    assert any(f.kind == "early_stop" for f in response.result.findings)
    assert any("No model was reachable" in line for line in response.result.limitations)
    assert not any(e.action == "engine_run" and e.status == "failed" for e in events)


def test_a_failing_model_falls_back_per_call(tmp_path):
    response, events = run(tmp_path, model=FakeModel(fail_agents=("Evaluator", "Analyst")))
    assert response.status == "partial_success"
    assert ("Analyst", "explain_fallback") in [(e.agent_name, e.action) for e in events]
    assert any(e.action == "critique" and e.status == "partial_success" for e in events)


def test_a_failed_table_read_is_recorded_and_the_run_goes_on(tmp_path):
    response, _ = run(tmp_path, zetaris=FakeZetaris(fail=("apple_rfcc_table",)), model=FakeModel())
    assert response.status == "partial_success"
    assert any("apple_rfcc_table could not be read" in line for line in response.result.limitations)
    assert any(f.kind == "early_stop" for f in response.result.findings)


def test_nothing_readable_is_a_controlled_failure(tmp_path):
    response, _ = run(tmp_path, zetaris=FakeZetaris(fail=("SELECT", "SHOW")), model=FakeModel())
    assert response.status == "failed" and response.error.type == "source_unavailable" and response.error.recoverable


def test_an_unknown_company_is_an_invalid_input_error(tmp_path):
    response, _ = run(tmp_path, model=None, query="how is the weather")
    assert response.status == "failed" and response.error.type == "invalid_input" and response.result is None


def test_run_request_without_credentials_returns_the_error_shape(tmp_path):
    settings = Settings.from_env({"LOG_DIR": str(tmp_path)})
    response = run_request(RunRequest(run_id="t2", input={"query": "Apple"}), settings, stream=None)
    assert response.status == "failed" and response.error.type == "configuration"
    # sample mode needs no credentials; a request other than the captured examples is not in the sample
    sample = Settings.from_env({"LOG_DIR": str(tmp_path), "SAMPLE_MODE": "true"})
    assert run_request(RunRequest(run_id="t3", input={"query": "Apple"}), sample, stream=None).error.type == "sample_miss"


CONTRADICTING = ("The RevenueFromContractWithCustomerExcludingAssessedTax series cannot represent the full request, "
                 "creating a coverage gap that the early_stop and late_start flags do not resolve.")


class ContradictingModel(FakeModel):
    """The Analyst model writes the live run's contradicting sentence for the RFCC step;
    `rewrite` is what it answers when the Evaluator sends that back."""

    def __init__(self, rewrite):
        super().__init__()
        self.rewrite = rewrite

    def chat(self, agent, messages, **kw):
        purpose = kw.get("purpose") or ""
        if purpose.startswith("explain step 2"):
            self.calls.append((agent, purpose, messages[-1]["content"]))
            return Reply(CONTRADICTING, "nvidia/nemotron-3.5-lightning-30b-a3b")
        if purpose.startswith("revise explanation"):
            self.calls.append((agent, purpose, messages[-1]["content"]))
            return Reply(self.rewrite, "nvidia/nemotron-3.5-lightning-30b-a3b")
        return super().chat(agent, messages, **kw)


def test_a_contradicting_explanation_goes_back_once_then_the_findings_stand_alone(tmp_path):
    model = ContradictingModel(rewrite=CONTRADICTING)
    response, events = run(tmp_path, model=model)
    md = (tmp_path / "report-t1.md").read_text()
    assert "do not resolve" not in md and "coverage gap" not in md, "the contradiction never reaches the report"
    assert "cannot represent" not in (tmp_path / "report-t1.json").read_text()
    reviews = [e for e in events if e.action == "review_explanation"]
    assert [e.status for e in reviews] == ["success", "needs_revision", "needs_revision"]
    actions = [(e.agent_name, e.action) for e in events]
    assert ("Evaluator", "send_back") in actions and ("Evaluator", "explain_fallback") in actions
    revise = next(c for c in model.calls if c[1].startswith("revise explanation"))
    assert "continuous across the tag change at 2018-09-29 / 2018-09-30" in revise[2], "the reason goes back with it"
    assert "Apple us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax: 7 rows" in md, "the findings speak alone"
    assert response.collaboration.revision_count == 1
    assert any("contradicted the findings twice" in n for n in response.collaboration.evaluator_findings)
    assert response.status == "success", "a dropped explanation is the loop working, not a failure"


def test_a_rewrite_that_agrees_with_the_findings_is_printed(tmp_path):
    rewrite = "Apple's revenue continues under the new concept: a tag change, not missing revenue."
    response, events = run(tmp_path, model=ContradictingModel(rewrite=rewrite))
    md = (tmp_path / "report-t1.md").read_text()
    assert rewrite in md and "do not resolve" not in md
    assert ("Evaluator", "explain_fallback") not in [(e.agent_name, e.action) for e in events]
    assert "1 explanation contradicting the findings went back to the Analyst" in response.result.summary

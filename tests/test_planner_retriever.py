"""The Planner's steps from a request, and the Retriever's statements against a fake
Zetaris client (the real client and its allowlist are tested in test_adapters)."""

import pytest

from app.adapters.zetaris import QueryResult, ZetarisError, check_statement
from app.agents.planner import COMPANIES, PlanError, Planner, companies_in_text
from app.agents.retriever import RetrievalError, Retriever, revenue_statement
from app.schemas import RunInput
from app.tracing import NullTracer
from tests.edgar_rows import APPLE_REVENUES, COLUMNS

REGISTERED = {f"edgar.{c}_revenue_table" for c in COMPANIES} | {
    f"edgar.{c}_rfcc_table" for c in ("apple", "oracle", "walmart", "target", "tesla")}


class FakeZetaris:
    def __init__(self, answers=None, fail=()):
        self.answers = answers or {}
        self.fail = fail
        self.statements = []

    def query(self, sql, limit=5000, purpose=""):
        check_statement(sql)
        self.statements.append(sql)
        if any(f in sql for f in self.fail):
            raise ZetarisError("HTTP 500 after 3 attempts")
        for needle, (headers, rows) in self.answers.items():
            if needle in sql:
                return QueryResult(sql, headers, rows)
        return QueryResult(sql, [], [])


def plan(query="", **kw):
    return Planner().plan(RunInput(query=query or "check", **kw), REGISTERED)


def test_one_primary_step_per_company_and_the_alternate_held_back():
    p = plan("Can I trust Apple's revenue series?")
    assert [(s.company.key, s.concept.key, s.role) for s in p.steps] == [("apple", "revenues", "primary")]
    assert p.alternate_for(p.steps[0].company, p.steps[0].concept).key == "rfcc"
    assert p.steps[0].table == "edgar.apple_revenue_table"


def test_a_tag_focus_plans_both_concepts_from_the_start():
    p = plan("Did Apple change its revenue tag?")
    assert [(s.concept.key, s.role) for s in p.steps] == [("revenues", "primary"), ("rfcc", "alternate")]
    p = plan("Apple revenue", focus="concept changes")
    assert len(p.steps) == 2


def test_companies_from_the_list_by_ticker_cik_or_name_and_unknown_ones_noted():
    p = plan(companies=["AAPL", "0000051143", "Walmart", "Initech"])
    assert [c.key for c in p.companies] == ["apple", "ibm", "walmart"]
    assert p.unknown == ["Initech"]
    assert any("Initech" in line for line in p.limitations)
    ibm = next(s for s in p.steps if s.company.key == "ibm")
    assert p.alternate_for(ibm.company, ibm.concept) is None, "IBM never reported the second concept"


def test_free_text_resolution():
    assert [c.key for c in companies_in_text("Compare Tesla and Ford, then TGT")] == ["tesla", "ford", "target"]
    assert companies_in_text("target the restatements first") == []
    assert [c.key for c in plan("check all seven companies").companies] == list(COMPANIES)


def test_no_company_is_a_plan_error_and_the_model_fallback_is_used_first():
    with pytest.raises(PlanError):
        plan("how is the weather")

    class Chat:
        def chat(self, agent, messages, **kw):
            assert agent == "Planner"
            return type("R", (), {"text": 'The request names ["oracle"].'})()

    p = Planner(chat=Chat()).plan(RunInput(query="the database company from Austin"), REGISTERED)
    assert [c.key for c in p.companies] == ["oracle"]


def test_plan_without_a_table_list_assumes_the_registered_set():
    p = Planner().plan(RunInput(query="IBM"), None)
    assert [s.table for s in p.steps] == ["edgar.ibm_revenue_table"]
    assert p.alternate_for(p.steps[0].company, p.steps[0].concept) is None
    assert any("table list" in line for line in p.limitations)


def test_plan_is_traced_as_a_handoff_to_the_retriever():
    tracer = NullTracer()
    Planner(tracer).plan(RunInput(query="Apple"), REGISTERED)
    event = tracer.events[-1]
    assert (event.agent_name, event.action, event.target_agent) == ("Planner", "plan", "Retriever")


def test_revenue_statement_is_read_only_and_cut_to_the_window():
    sql = revenue_statement("edgar.apple_revenue_table", "2016", "2018-12-31")
    assert check_statement(sql) == sql
    assert "period_end >= '2016-01-01'" in sql and "period_start <= '2018-12-31'" in sql
    assert sql.endswith("ORDER BY period_end, period_start, filed_date, accession_number")
    assert "WHERE" not in revenue_statement("edgar.apple_revenue_table", None, "soon")
    with pytest.raises(ValueError):
        revenue_statement("edgar.apple; DROP TABLE x")


def test_retriever_returns_records_and_hands_off_to_the_analyst():
    rows = [[r[c] for c in COLUMNS] for r in APPLE_REVENUES]
    fake = FakeZetaris({"apple_revenue_table": (COLUMNS, rows)})
    tracer = NullTracer()
    out = Retriever(fake, tracer).revenue_rows("edgar.apple_revenue_table")
    assert len(out) == 11 and out.records[0]["accession_number"] == "0000320193-18-000145"
    assert out.statement == fake.statements[0]
    event = tracer.events[-1]
    assert (event.action, event.target_agent, event.details["row_count"]) == ("rows_ready", "Analyst", 11)


def test_retriever_lists_tables_and_reads_dq_failures():
    fake = FakeZetaris({
        "SHOW TABLES": (["table_identifier", "catalog_name"], [["APPLE_REVENUE_TABLE", "edgar"], ["IBM_PROFILE_TABLE", "edgar"]]),
        "SHOW DQ": (["records"], [['{"cik":"0000051143","sic":"3570"}']]),
    })
    r = Retriever(fake)
    assert r.list_tables() == {"edgar.apple_revenue_table", "edgar.ibm_profile_table"}
    assert r.dq_invalid_companies().records == [{"cik": "0000051143", "sic": "3570"}]


def test_a_failed_statement_becomes_a_retrieval_error_with_the_statement():
    r = Retriever(FakeZetaris(fail=("rfcc",)))
    with pytest.raises(RetrievalError) as e:
        r.revenue_rows("edgar.apple_rfcc_table")
    assert "apple_rfcc_table" in e.value.statement

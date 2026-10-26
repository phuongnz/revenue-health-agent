"""The Zetaris and Nightshift adapters against mock transports, plus live smoke tests
that are skipped unless LIVE_SMOKE=1 (see conftest.py)."""

import json
import time

import httpx
import pytest

from app.adapters.zetaris import (
    QueryResult,
    StatementRefused,
    ZetarisClient,
    ZetarisError,
    check_statement,
    coerce_cell,
)
from app.adapters.meterless import MeterlessClient, MeterlessError, split_reasoning
from app.config import LIGHTNING_LANE, ULTRA_LANE, Settings
from app.tracing import NullTracer

FAKE_KEY = "test-key-not-real"


def settings(**env):
    base = {"ZETARIS_API_KEY": FAKE_KEY, "ZETARIS_REST_URL": "http://zetaris.test/api/v1.0",
            "METERLESS_RUNTIME_TOKEN": FAKE_KEY, "METERLESS_BASE_URL": "http://gateway.test/v1"}
    base.update(env)
    return Settings.from_env(base)


# --- Zetaris: allowlist -------------------------------------------------------


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "select * from edgar.apple_revenue_table;",
    "  -- a comment\n  WITH a AS (SELECT 1) SELECT * FROM a",
    "SHOW TABLES IN edgar",
    "DESCRIBE edgar.apple_revenue_table",
    "SHOW DQ PIPELINES",
    "LIST DQ RULES",
    "RUN DQ revenue_checks",
    "SELECT 'drop table x; delete' AS s",
    "SELECT `update`, created_at, date_add(d, 1) FROM t /* insert */",
])
def test_allowlist_accepts_read_only(sql):
    assert check_statement(sql)


@pytest.mark.parametrize("sql,reason", [
    ("", "empty"),
    ("-- only a comment", "empty"),
    ("DROP TABLE edgar.x", "not a read-only"),
    ("CREATE LIGHTNING DATABASE x", "not a read-only"),
    ("INSERT INTO t VALUES (1)", "not a read-only"),
    ("SELECT 1; DROP TABLE t", "more than one"),
    ("WITH a AS (SELECT 1) INSERT INTO t SELECT * FROM a", "write keyword"),
    ("SELECT * FROM t WHERE x = 'it''s'; DELETE FROM t", "more than one"),
    ("SHOWTABLES", "not a read-only"),
    ("SELECT 1 /* unterminated", None),
])
def test_allowlist_refuses_the_rest(sql, reason):
    if reason is None:  # an unterminated comment swallows the rest; still one SELECT
        assert check_statement(sql)
        return
    with pytest.raises(StatementRefused, match=reason):
        check_statement(sql)


def test_trailing_semicolon_dropped():
    assert check_statement("SELECT 1 ;  ") == "SELECT 1"


# --- Zetaris: typed rows ------------------------------------------------------


@pytest.mark.parametrize("raw,typed", [
    ("215639000000", 215639000000), ("2018", 2018), ("-5", -5), ("1.2E7", 12000000),
    ("3.5", 3.5), ("1.5e-3", 0.0015), ("true", True), ("FALSE", False),
    ("0000320193-18-000145", "0000320193-18-000145"), ("007", "007"), ("2016-09-24", "2016-09-24"),
    ("FY", "FY"), ("", ""), (None, None), (7, 7), ("NaN", "NaN"),
])
def test_coerce_cell(raw, typed):
    assert coerce_cell(raw) == typed and type(coerce_cell(raw)) is type(typed)


def test_result_keeps_headers_and_leaves_identifiers_as_text():
    reply = {"headers": ["cik", "fiscal_year", "value_usd", "sic_code", "form_type"],
             "data": [["320193", "2018", "1.2E7", "3571", "10-K"]], "total": 1, "timeUsed": 9}
    result = QueryResult.from_response("SELECT …", reply)
    assert result.headers == reply["headers"]
    assert result.records == [{"cik": "320193", "fiscal_year": 2018, "value_usd": 12000000,
                               "sic_code": "3571", "form_type": "10-K"}]
    assert result.total == 1 and result.time_used_ms == 9 and len(result) == 1


# --- Zetaris: the client over a mock transport --------------------------------


def client_with(handler, **kwargs):
    sleeps = []
    tracer = NullTracer()
    client = ZetarisClient(settings(), tracer=tracer, transport=httpx.MockTransport(handler),
                           sleep=sleeps.append, **kwargs)
    return client, tracer, sleeps


def test_query_sends_contract_and_traces_statement():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"headers": ["one"], "data": [["1"]], "total": 1, "timeUsed": 5})

    client, tracer, _ = client_with(handler)
    result = client.query("SELECT 1 AS one;", purpose="probe")
    assert result.records == [{"one": 1}]
    request = seen[0]
    assert request.url.path == "/api/v1.0/sql-editor/sqls/run"
    assert request.headers["Authorization"] == f"Bearer {FAKE_KEY}" and request.headers["X-Org-ID"] == "1"
    body = json.loads(request.content)
    assert body["sql"] == "SELECT 1 AS one" and body["source"] == "SqlEditor" and body["queryId"]
    (event,) = tracer.events
    assert event.action == "query_zetaris" and event.status == "success" and event.agent_name == "Retriever"
    assert event.details["statement"] == "SELECT 1 AS one" and event.details["row_count"] == 1
    assert FAKE_KEY not in event.to_json_line()


def test_refused_statement_is_never_sent():
    client, tracer, _ = client_with(lambda r: pytest.fail("sent"))
    with pytest.raises(StatementRefused):
        client.query("DROP TABLE edgar.apple_revenue_table")
    assert tracer.events[0].action == "refuse_statement" and tracer.events[0].status == "needs_revision"


def test_429_waits_for_retry_after_then_succeeds():
    replies = iter([httpx.Response(429, headers={"Retry-After": "4"}, json={"msg": "slow down"}),
                    httpx.Response(200, json={"headers": ["n"], "data": [["3"]]})])
    client, tracer, sleeps = client_with(lambda r: next(replies))
    assert client.query("SELECT 3 AS n").records == [{"n": 3}]
    assert sleeps == [4.0]
    assert [e.status for e in tracer.events] == ["retrying", "success"]
    assert tracer.events[-1].retry_count == 1


def test_transient_errors_stop_after_three_attempts():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectTimeout("slow", request=request)

    client, tracer, sleeps = client_with(handler)
    with pytest.raises(ZetarisError, match="timed out"):
        client.query("SELECT 1")
    assert len(calls) == 3 and len(sleeps) == 2 and all(1 <= s <= 8 for s in sleeps)
    assert [e.status for e in tracer.events] == ["retrying", "retrying", "failed"]


def test_5xx_retried_but_sql_error_is_not():
    replies = iter([httpx.Response(503, text="busy"), httpx.Response(400, json={"msg": "[UNRESOLVED_COLUMN] val"})])
    client, tracer, sleeps = client_with(lambda r: next(replies))
    with pytest.raises(ZetarisError, match="HTTP 400: \\[UNRESOLVED_COLUMN\\] val"):
        client.query("SELECT val FROM t")
    assert len(sleeps) == 1 and tracer.events[-1].status == "failed"


def test_non_json_reply_is_an_error():
    client, _, _ = client_with(lambda r: httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(ZetarisError, match="not JSON"):
        client.query("SELECT 1")


def test_missing_key_is_refused():
    with pytest.raises(ZetarisError, match="ZETARIS_API_KEY"):
        ZetarisClient(Settings.from_env({}))


# --- Nightshift ----------------------------------------------------------------


def completion(content="OK", routed=LIGHTNING_LANE, receipt=True):
    body = {"id": "c1", "object": "chat.completion", "model": routed,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}}
    if receipt:
        body["meterless"] = {"schema": "meterless.compute_receipt.v1", "receipt_id": "rcpt_1",
                             "requested_model": routed, "routed_model": routed,
                             "winning_route": {"provider": "nvidia"}, "phases": [{"ms": 420}],
                             "receipt_envelope": {"sig_b64": "x" * 40}, "receipt": {"nested": True}}
    return body


def meterless_with(handler, **kwargs):
    sleeps = []
    tracer = NullTracer()
    client = MeterlessClient(settings(), tracer=tracer, transport=httpx.MockTransport(handler),
                             sleep=sleeps.append, **kwargs)
    return client, tracer, sleeps


@pytest.mark.parametrize("agent,lane", [("Analyst", LIGHTNING_LANE), ("Planner", LIGHTNING_LANE), ("Evaluator", ULTRA_LANE)])
def test_chat_pins_lane_per_agent_and_keeps_receipt(agent, lane):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=completion(routed=lane))

    client, tracer, _ = meterless_with(handler)
    result = client.chat(agent, [{"role": "user", "content": "Say OK"}], target_agent="Reporter")
    request = seen[0]
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["meterless-app"] == "revenue-health-agent" and request.headers["meterless-scope"] == "user"
    assert request.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert json.loads(request.content)["model"] == lane
    assert result.text == "OK" and result.routed_model == lane and result.on_nemotron
    (event,) = tracer.events
    assert event.action == "model_call" and event.status == "success" and event.agent_name == agent
    assert event.model == lane and event.receipt["routed_model"] == lane and event.target_agent == "Reporter"
    assert event.receipt["receipt_id"] == "rcpt_1" and "receipt_envelope" not in event.receipt
    assert "receipt_envelope" in result.receipt  # the whole receipt stays on the result
    assert FAKE_KEY not in event.to_json_line()


def test_routing_mode_in_env_never_reaches_the_request():
    seen = []
    client = MeterlessClient(settings(METERLESS_MODEL="meterless/auto"), tracer=NullTracer(),
                             transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json=completion())))
    client.chat("Analyst", [{"role": "user", "content": "x"}])
    assert json.loads(seen[0].content)["model"] == LIGHTNING_LANE


def test_reply_routed_off_nemotron_needs_validation():
    client, tracer, _ = meterless_with(lambda r: httpx.Response(200, json=completion(routed="local-runtime/phi-4-mini-instruct")))
    result = client.chat("Analyst", [{"role": "user", "content": "x"}])
    assert not result.on_nemotron and tracer.events[-1].status == "needs_validation"


def test_missing_receipt_is_kept_honest():
    client, tracer, _ = meterless_with(lambda r: httpx.Response(200, json=completion(receipt=False)))
    result = client.chat("Analyst", [{"role": "user", "content": "x"}])
    assert result.receipt is None and result.routed_model is None
    assert tracer.events[-1].status == "needs_validation" and tracer.events[-1].model == LIGHTNING_LANE


def test_reasoning_is_split_from_the_answer():
    assert split_reasoning("<think>check rows</think>\nACCEPT") == ("ACCEPT", "check rows")
    assert split_reasoning("<think>cut off before the end") == ("", "cut off before the end")
    client, _, _ = meterless_with(lambda r: httpx.Response(200, json=completion("<think>hmm</think>REJECT")))
    result = client.chat("Evaluator", [{"role": "user", "content": "x"}])
    assert result.text == "REJECT" and result.reasoning == "hmm"


def test_chat_retries_transient_then_fails_cleanly():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, text="upstream busy")

    client, tracer, sleeps = meterless_with(handler)
    with pytest.raises(MeterlessError, match="HTTP 503"):
        client.chat("Analyst", [{"role": "user", "content": "x"}])
    assert len(calls) == 3 and len(sleeps) == 2
    assert [e.status for e in tracer.events] == ["retrying", "retrying", "failed"]


def test_thinking_is_off_except_on_the_evaluator_lane():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    client, _, _ = meterless_with(handler)
    client.chat("Analyst", [{"role": "user", "content": "x"}])
    client.chat("Evaluator", [{"role": "user", "content": "x"}])
    client.chat("Planner", [{"role": "user", "content": "x"}], thinking=True,
                extra={"chat_template_kwargs": {"other": 1}})
    client.chat("Analyst", [{"role": "user", "content": "x"}], extra={"chat_template_kwargs": {"other": 1}})
    assert bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "chat_template_kwargs" not in bodies[1], "Ultra reasons before it critiques"
    assert bodies[2]["chat_template_kwargs"] == {"other": 1}
    assert bodies[3]["chat_template_kwargs"] == {"enable_thinking": False, "other": 1}


def test_a_spent_deadline_stops_the_attempts():
    calls = []

    def handler(request):
        calls.append(request.extensions.get("timeout"))
        return httpx.Response(503, text="busy")

    client, tracer, _ = meterless_with(handler)
    with pytest.raises(MeterlessError, match="no time left"):
        client.chat("Analyst", [{"role": "user", "content": "x"}], deadline=time.monotonic() + 1)
    assert calls == [] and tracer.events[-1].status == "failed"
    # a per-call timeout is capped by the deadline (and at the contract's 90 s)
    with pytest.raises(MeterlessError):
        client.chat("Evaluator", [{"role": "user", "content": "x"}], timeout_s=500, deadline=time.monotonic() + 40)
    assert len(calls) == 3 and all(t["read"] <= 40 for t in calls)


def test_unauthorised_is_not_retried():
    calls = []
    client, _, _ = meterless_with(lambda r: calls.append(1) or httpx.Response(401, json={"error": "runtime_token_missing"}))
    with pytest.raises(MeterlessError, match="HTTP 401"):
        client.chat("Analyst", [{"role": "user", "content": "x"}])
    assert len(calls) == 1


def test_generator_matches_engine_contract():
    seen = []
    client, _, _ = meterless_with(lambda r: seen.append(json.loads(r.content)) or httpx.Response(200, json=completion("chunk")))
    streamed = []
    out = client.as_generator("Analyst")("Do step 1", ["rows: 3"], "You are the Analyst", streamed.append, None)
    assert out["text"] == "chunk" and streamed == ["chunk"]
    assert out["metadata"]["routed_model"] == LIGHTNING_LANE and out["metadata"]["usage"]["total_tokens"] == 15
    assert seen[0]["messages"][0] == {"role": "system", "content": "You are the Analyst"}
    assert seen[0]["messages"][1]["content"] == "Do step 1\n\nrows: 3"


def test_missing_token_is_refused():
    with pytest.raises(MeterlessError, match="METERLESS_RUNTIME_TOKEN"):
        MeterlessClient(Settings.from_env({}))


# --- live smoke tests -----------------------------------------------------------


@pytest.mark.live
def test_live_zetaris_smoke(live_settings, live_tracer):
    with ZetarisClient(live_settings, tracer=live_tracer) as client:
        result = client.query(
            "SELECT cik, fiscal_year, fiscal_period, form_type, value_usd "
            "FROM edgar.apple_revenue_table ORDER BY period_end LIMIT 5",
            purpose="live smoke test",
        )
    assert result.headers == ["cik", "fiscal_year", "fiscal_period", "form_type", "value_usd"]
    assert len(result) == 5
    first = result.records[0]
    assert first["cik"] == "320193" and isinstance(first["fiscal_year"], int) and isinstance(first["value_usd"], int)
    assert live_tracer.events[-1].action == "query_zetaris" and live_tracer.events[-1].status == "success"


@pytest.mark.live
def test_live_nightshift_smoke(live_settings, live_tracer):
    with MeterlessClient(live_settings, tracer=live_tracer) as client:
        result = client.chat(
            "Analyst",
            [{"role": "user", "content": "Reply with the single word OK."}],
            max_tokens=256,
            purpose="live smoke test",
        )
    print("\nrouted_model:", result.routed_model, "| receipt keys:", sorted(result.receipt or {}))
    assert result.routed_model and result.routed_model.startswith("nvidia/nemotron")
    assert result.text
    event = live_tracer.events[-1]
    assert event.action == "model_call" and event.model.startswith("nvidia/nemotron") and event.receipt

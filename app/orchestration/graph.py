"""The orchestrator: one run, from request to response, through the Markovian engine.

    Planner -> (per step, inside one engine chunk)
        Retriever -> Analyst -> Evaluator --(truncated series)--> Retriever, alternate concept
                         ^-------(unsupported or rejected, once)----'
        -> Analyst model: the step's explanation, written with the carry-over
    -> Reporter

The engine owns the loop: one bounded chunk per company and concept. Before each chunk
its `step_input` hook pops the next planned step and runs the agents on it (code and
the Evaluator's critique); the chunk itself is the Analyst model writing the step's
explanation from the engine's prompt, which holds only the carry-over of earlier steps
and this step's own findings. The carry-over block is written by code from the run's
state. The Evaluator's retry puts the alternate-concept step at the front of the queue,
so it is the next chunk, and the engine stops when the queue is empty.

Every sponsor call has a fallback: a failed table list plans on the registered set, a
failed read records the step and goes on, a failed or skipped model call leaves the code
verdicts and a code-written explanation. Time is capped per step (model calls past the
step's budget are skipped) and per run (the engine starts no chunk past the deadline).
"""

from __future__ import annotations

import re
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from typing import Any

from app.agents.analyst import Analyst, SeriesContext, find_reference_mismatch, short_concept
from app.agents.evaluator import Evaluator, Evidence
from app.agents.planner import CONCEPTS, PlanError, Planner, PlanStep
from app.agents.reporter import Explanation, ReportInput, Reporter, vetted_critiques
from app.agents.retriever import DQ_COMPANY_TABLE, RetrievalError, Retriever
from app.adapters.meterless import MeterlessClient, MeterlessError
from app.adapters.zetaris import ZetarisClient, ZetarisError
from app.config import Settings
from app.engines.markovian import REFLECTION_SYSTEM, Markovian, clean_markers
from app.orchestration.state import RunState, StepRecord
from app.schemas import Collaboration, RunError, RunRequest, RunResponse
from app.tracing import Tracer

AGENT = "Orchestrator"
ENGINE_AGENT = "MarkovianEngine"
CARRYOVER_TOKENS = 512
EXPLAIN_MAX_TOKENS = 2048
PROFILE_TABLE = "edgar.all_companies_profile_table"
MAX_FINDINGS_IN_STEP = 8

EXPLAIN_SYSTEM = (
    "You are the Analyst in a data-quality audit of SEC XBRL revenue series, writing for an equity analyst. Code "
    "has computed and the Evaluator has checked the findings in the step input. Explain in three to five plain "
    "sentences what they mean for trusting this company's revenue series, and connect them to what earlier steps "
    "found (the carry-over state) when they bear on it, for example a series that stopped under one concept and "
    "continues under another. Quote only figures and dates given to you; never compute new ones. Call each problem "
    "by its finding's kind: say gap, restated, duplicated, stops early or starts late only when a finding of that "
    "kind is listed, and call a series with no findings clean. When a tag_change finding says the series is "
    "continuous, say the revenue continues under the new concept; never call that change a gap or missing revenue. "
    "No lists, no headings."
)
REVISE_EXPLANATION = ("The Evaluator sent this explanation back: {reason}. Rewrite it so that it agrees with the "
                      "accepted findings above, naming each problem by its finding's kind and quoting only dates and "
                      "figures given above. Three to five plain sentences, no lists.")
FRAMING = ("You are explaining a revenue data-quality audit one company and revenue concept at a time; each step "
           "gives you that step's checked findings.")


class StepBudget:
    """Wraps the model client: a call past the step's or the run's deadline is refused,
    so the agent takes its fallback instead of overrunning the loop."""

    def __init__(self, inner: Any, run_deadline: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.inner = inner
        self.run_deadline = run_deadline
        self.step_deadline = run_deadline
        self.clock = clock

    def start_step(self, seconds: float) -> None:
        self.step_deadline = min(self.run_deadline, self.clock() + seconds)

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        left = self.step_deadline - self.clock()
        if left <= 0:
            raise TimeoutError("the time budget for this step is spent; model call skipped")
        kwargs.setdefault("deadline", time.monotonic() + left)  # every attempt fits in what is left
        return self.inner.chat(*args, **kwargs)


def _cik(value: Any) -> str:
    return str(value or "").lstrip("0")


class Orchestrator:
    def __init__(
        self, settings: Settings, tracer: Tracer, zetaris: Any, chat: Any = None, *,
        as_of: date | None = None, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.tracer = tracer
        self.zetaris = zetaris
        self.chat = chat
        self.as_of = as_of or date.today()
        self.clock = clock

    def run(self, request: RunRequest) -> RunResponse:
        return _Run(self, request).execute()


class _Run:
    """One request's worth of agents and state."""

    def __init__(self, orch: Orchestrator, request: RunRequest) -> None:
        self.o = orch
        self.request = request
        self.tracer = orch.tracer
        self.started = orch.clock()
        self.deadline = self.started + orch.settings.run_timeout_s
        self.budget = StepBudget(orch.chat, self.deadline, orch.clock) if orch.chat is not None else None
        self.state = RunState(request)
        self.planner = Planner(self.tracer, chat=self.budget)
        self.retriever = Retriever(orch.zetaris, self.tracer)
        self.analyst = Analyst(self.tracer)
        self.evaluator = Evaluator(self.tracer, chat=self.budget)
        self.reporter = Reporter(self.tracer)

    # --- the run ---------------------------------------------------------------------------

    def execute(self) -> RunResponse:
        state, inp = self.state, self.request.input
        self.tracer.emit(AGENT, "start_run", input_summary=inp.query, target_agent="Planner",
                         output_summary=f"run {self.request.run_id}; companies {inp.companies or 'from the query'}; "
                                        f"window {inp.period_start or 'default'} to {inp.period_end or 'latest'}",
                         details={"model_available": self.budget is not None})
        if self.budget is None:
            state.limitations.append("No model was reachable: verdicts rest on the code checks and the explanations "
                                     "are written by code.")
            state.fallbacks += 1
        state.use("Retriever")
        try:
            tables: set[str] | None = self.retriever.list_tables()
        except RetrievalError:
            tables = None
            state.fallbacks += 1
        state.use("Planner")
        try:
            plan = self.planner.plan(inp, tables)
        except PlanError as e:
            return self.respond(error=RunError(type="invalid_input", message=str(e), recoverable=True))
        state.plan = plan
        state.queue.extend(plan.steps)
        state.limitations.extend(plan.limitations)
        self.load_reference()

        primaries = sum(1 for s in plan.steps if s.role == "primary")
        max_chunks = max(1, min(32, len(plan.steps) + primaries))
        engine = Markovian(
            {"chunkSize": 8000, "maxChunks": max_chunks, "carryoverTokens": CARRYOVER_TOKENS, "overlapTokens": 0},
            tracer=self.tracer, log_dir=self.o.settings.log_dir,
        )
        goal = (f"Audit the reliability of the revenue series of {', '.join(c.name for c in plan.companies)} "
                f"(fiscal years {inp.period_start or 'from 2009'} to {inp.period_end or 'latest'}), one company and "
                f"revenue concept per step: {plan.summary()}. Request: {inp.query}")
        remaining_ms = max(1.0, (self.deadline - self.o.clock()) * 1000)
        run = engine.run(
            goal, generator=self.generate, mode="ARCHITECT", system_prompt=EXPLAIN_SYSTEM, framing=FRAMING,
            step_input=self.step_input, stop_when=lambda step, carry: not state.queue,
            reflect=self.budget is not None, run_id=self.request.run_id, agent_id=ENGINE_AGENT,
            max_duration_ms=remaining_ms,
        )
        state.reflection = run.reflection
        if state.queue:
            left = "; ".join(s.label for s in state.queue)
            why = run.error or f"the engine stopped with status {run.status}"
            state.limitations.append(f"Not checked, the run was cut short ({why}): {left}.")
            self.tracer.emit(AGENT, "loop_capped", input_summary=f"{len(state.queue)} steps left",
                             output_summary=f"returned a partial result: {why}", status="partial_success")
        elif run.status not in ("completed",):
            state.limitations.append(f"The engine ended with status {run.status}: {run.error}.")
        return self.respond(engine_status=run.status)

    # --- reference data -----------------------------------------------------------------------

    def load_reference(self) -> None:
        state = self.state
        try:
            profiles = self.retriever.profile_rows(PROFILE_TABLE)
            state.profiles = {_cik(r.get("cik")): r for r in profiles.records}
        except RetrievalError as e:
            state.limitations.append(f"Company profiles could not be read ({e}); industry context is missing.")
            state.fallbacks += 1
        try:
            dq = self.retriever.dq_invalid_companies()
            state.dq_records, state.dq_statement = dq.records, dq.statement
        except RetrievalError as e:
            state.limitations.append(f"The SIC data-quality rule could not be read ({e}); reference-data problems "
                                     f"are not reported.")
            state.fallbacks += 1

    # --- one engine step -------------------------------------------------------------------------

    def step_input(self, number: int, carryover: str) -> str | None:
        state = self.state
        if not state.queue:
            return None
        step = state.queue.popleft()
        if self.budget is not None:
            self.budget.start_step(self.o.settings.step_timeout_s)
        record = StepRecord(number, step)
        state.records.append(record)
        self.tracer.emit(AGENT, "dispatch_step", input_summary=f"step {number}: {step.label} ({step.role})",
                         output_summary=step.reason or "planned", target_agent="Retriever",
                         details={"table": step.table, "role": step.role})
        try:
            self.work(record)
        except Exception as e:  # noqa: BLE001 - one step's failure is recorded, the run goes on
            record.error = record.error or f"{type(e).__name__}: {e}"
            state.limitations.append(f"{step.label}: the step failed ({record.error}).")
            self.tracer.emit(AGENT, "step_failed", input_summary=step.label, output_summary=record.error,
                             status="failed")
        record.step_text = self.describe(record)
        return record.step_text

    def work(self, record: StepRecord) -> None:
        state, step = self.state, record.step
        inp = self.request.input
        ctx = SeriesContext(step.company.name, step.concept.name, step.table, cik=step.company.cik,
                            period_start=inp.period_start, period_end=inp.period_end, as_of=self.o.as_of)
        try:
            rows = self.retriever.revenue_rows(step.table, inp.period_start, inp.period_end,
                                               purpose=f"{step.label}, {step.role}")
        except RetrievalError as e:
            record.error = str(e)
            state.fallbacks += 1
            state.limitations.append(f"{step.label}: {step.table} could not be read ({e}); this series is not checked.")
            self.tracer.emit(AGENT, "step_failed", input_summary=step.label, output_summary=f"read failed: {e}",
                             status="partial_success", details={"statement": e.statement})
            return
        record.rows = rows
        record.ctx = ctx = replace(ctx, statement=rows.statement)
        state.series[(step.company.key, step.concept.key)] = (rows, ctx)
        state.use("Analyst")
        findings = self.analyst.analyze(rows.records, ctx)
        evidence = Evidence(rows.records, ctx)
        other_key = next(k for k in CONCEPTS if k != step.concept.key)
        other = state.series.get((step.company.key, other_key))
        if other is not None:
            other_rows, other_ctx = other
            findings += self.analyst.compare(other_rows.records, other_ctx, rows.records, ctx)
            evidence = Evidence(rows.records, ctx, other_rows.records, other_ctx)
        if not any(r.step.company.key == step.company.key for r in state.records[:-1]):
            findings += find_reference_mismatch(step.company.name, step.company.cik, state.dq_records,
                                                DQ_COMPANY_TABLE, state.dq_statement)
        alternate = state.plan.alternate_for(step.company, step.concept) if state.plan else None
        planned = {(s.company.key, s.concept.key) for s in state.queue} | set(state.series)
        may_retry = step.role == "primary" and (alternate is None or (step.company.key, alternate.key) not in planned)
        state.use("Evaluator")
        outcome = self.evaluator.evaluate(
            findings, evidence, self.analyst,
            alternate=(alternate.name, alternate.table(step.company.key)) if alternate else None, may_retry=may_retry,
        )
        record.outcome = outcome
        state.findings.extend(outcome.all)
        state.limitations.extend(outcome.limitations)
        state.notes.extend(outcome.notes)
        state.critiques.extend(outcome.critiques)
        state.notes.extend(f"{f.status.capitalize()} {f.kind} {f.period or ''} for {f.company}: {f.evaluator_reason}"
                           for f in outcome.rejected + outcome.unsupported)
        state.revision_count += outcome.revision_count
        if any("unavailable" in line for line in outcome.limitations):
            state.fallbacks += 1
        kinds = ", ".join(sorted({f.kind for f in outcome.accepted})) or "none"
        state.decisions.append(f"{step.company.name} {short_concept(step.concept.name)}: accepted {kinds}")
        if outcome.retry is not None and alternate is not None:
            retry = PlanStep(step.company, alternate, "retry", outcome.retry.reason)
            state.queue.appendleft(retry)
            state.decisions.append(f"{step.company.name}: retry with {short_concept(alternate.name)}")
            self.tracer.emit(AGENT, "enqueue_retry", input_summary=outcome.retry.reason,
                             output_summary=f"next step: {retry.label}", target_agent="Retriever",
                             retry_count=1, status="retrying", details={"table": retry.table})

    def describe(self, record: StepRecord) -> str:
        step = record.step
        profile = self.state.profiles.get(_cik(step.company.cik)) or {}
        lines = [f"Company: {step.company.name} (CIK {step.company.cik}"
                 + (f", SEC SIC {profile.get('sic_code')} {profile.get('sic_description_edgar')}" if profile else "")
                 + f"). Concept: {step.concept.name}. Step role: {step.role}."]
        if record.error:
            lines.append(f"The table {step.table} could not be read: {record.error}. Say the series was not checked.")
            return "\n".join(lines)
        lines.append(f"Table {step.table}: {len(record.rows or [])} rows covering {record.span}.")
        outcome = record.outcome
        accepted = outcome.accepted if outcome else []
        if not accepted:
            lines.append("Accepted findings: none; the series is clean in the requested window.")
        else:
            by_kind = Counter(f.kind for f in accepted)
            lines.append("Accepted findings (computed by code, checked by the Evaluator), by kind: "
                         + ", ".join(f"{n} {k}" for k, n in sorted(by_kind.items())) + ". Quote these counts.")
            for f in accepted[:MAX_FINDINGS_IN_STEP]:
                lines.append(f"- [{f.kind}] {f.period or ''} confidence {f.confidence:.2f}: {f.description}")
            if len(accepted) > MAX_FINDINGS_IN_STEP:
                rest = accepted[MAX_FINDINGS_IN_STEP:]
                kinds = ", ".join(sorted({f.kind for f in rest}))
                lines.append(f"- and {len(rest)} more ({kinds})")
        if outcome and (outcome.rejected or outcome.unsupported):
            lines.append(f"Dropped by the Evaluator: {len(outcome.rejected)} rejected, {len(outcome.unsupported)} unsupported.")
        if outcome and outcome.retry:
            lines.append(f"The Evaluator asked for a retry with {outcome.retry.concept}: {outcome.retry.reason}.")
        lines.append(f"Write the explanation of this step for {step.company.name}.")
        return "\n".join(lines)

    # --- the injected generator ------------------------------------------------------------------

    def generate(self, prompt: str, attachments: list[Any] | None = None, system_prompt: str | None = None,
                 on_stream: Any = None, abort: Any = None) -> dict[str, Any]:
        messages = [{"role": "system", "content": system_prompt or EXPLAIN_SYSTEM}, {"role": "user", "content": prompt}]
        if system_prompt == REFLECTION_SYSTEM:  # the engine's reflection pass; its failure never fails the run
            if self.budget is None:
                raise RuntimeError("no model for the reflection")
            result = self.budget.chat("Analyst", messages, max_tokens=1024, purpose="engine reflection")
            return {"text": result.text, "metadata": result.metadata()}
        record = self.state.records[-1]
        company, label = record.step.company.name, record.step.label
        text, metadata = "", {"routed_model": None}
        if self.budget is not None and not record.error:
            try:
                text, metadata = self.explain(record, messages, prompt)
            except Exception as e:  # noqa: BLE001 - the fallback is a code-written explanation
                text = ""
                self.state.fallbacks += 1
                self.tracer.emit("Analyst", "explain_fallback", input_summary=label,
                                 output_summary=f"model call failed or skipped: {e}", status="partial_success")
        code_text = self.fallback_explanation(record)
        if text:
            explanation = Explanation(company, label, text, by="model", given=prompt, fallback=code_text)
        else:
            text = code_text
            explanation = Explanation(company, label, text, by="code")
        record.explanation = text
        self.state.explanations.append(explanation)
        marker = ("[TASK_COMPLETE]" if not self.state.queue
                  else "[STATE_CHECKPOINT]\n" + self.state.carryover_block(self.o.as_of.isoformat()))
        return {"text": f"{text}\n\n{marker}", "metadata": metadata}

    @staticmethod
    def _clean(text: str) -> str:
        text = clean_markers(text)
        return re.split(r"\n\s*(COMPLETED|REMAINING|DECISIONS|CONTEXT):", text)[0].strip()

    def explain(self, record: StepRecord, messages: list[dict[str, str]], prompt: str) -> tuple[str, dict[str, Any]]:
        """The Analyst model's explanation of one step, held to the findings by the Evaluator:
        a contradicting explanation goes back once with the reason, and a second one is
        dropped, so the step is told by the findings alone ("" is returned)."""
        company, label = record.step.company.name, record.step.label
        result = self.budget.chat("Analyst", messages, max_tokens=EXPLAIN_MAX_TOKENS,
                                  purpose=f"explain step {record.number}", target_agent="Evaluator")
        text, metadata = self._clean(result.text), result.metadata()
        record.explanation_model = result.routed_model
        if not text:
            return "", metadata
        findings = self.state.accepted_for(company)
        reason = self.evaluator.review_explanation(text, findings, prompt, company, label)
        if reason is None:
            return text, metadata
        self.state.explanations_sent_back += 1
        self.tracer.emit("Evaluator", "send_back", input_summary=f"explanation of {label}",
                         output_summary=f"back to the Analyst (prose check): {reason}", target_agent="Analyst",
                         retry_count=1, status="needs_revision", details={"explanation_of": label})
        revise = [*messages, {"role": "assistant", "content": text},
                  {"role": "user", "content": REVISE_EXPLANATION.format(reason=reason)}]
        result = self.budget.chat("Analyst", revise, max_tokens=EXPLAIN_MAX_TOKENS,
                                  purpose=f"revise explanation of step {record.number}", target_agent="Evaluator")
        text, metadata = self._clean(result.text), result.metadata()
        again = self.evaluator.review_explanation(text, findings, prompt, company, label, attempt=2) if text else "empty"
        if again is None:
            self.state.notes.append(f"The explanation of {label} was sent back once ({reason}); the rewrite agrees "
                                    f"with the findings.")
            return text, metadata
        self.state.notes.append(f"The explanation of {label} contradicted the findings twice ({again}); the step is "
                                f"told by the findings alone.")
        self.tracer.emit("Evaluator", "explain_fallback", input_summary=f"explanation of {label} (attempt 2)",
                         output_summary=f"dropped, the findings stand alone: {again}", target_agent="Reporter",
                         retry_count=1, status="partial_success", details={"explanation_of": label})
        return "", metadata

    @staticmethod
    def fallback_explanation(record: StepRecord) -> str:
        step = record.step
        if record.error:
            return f"{step.label} could not be read, so this series was not checked."
        accepted = record.outcome.accepted if record.outcome else []
        if not accepted:
            return f"{step.label}: {len(record.rows or [])} rows covering {record.span}, with no problem found."
        lead = sorted(accepted, key=lambda f: ("tag_change", "early_stop", "late_start", "gap", "restatement",
                                               "duplicate", "other").index(f.kind))[0]
        return (f"{step.label}: {len(record.rows or [])} rows covering {record.span}; {len(accepted)} accepted "
                f"finding(s). The most important: {lead.description}")

    # --- the response ------------------------------------------------------------------------------

    def respond(self, error: RunError | None = None, engine_status: str | None = None) -> RunResponse:
        state = self.state
        state.use("Reporter")
        plan = state.plan
        companies = [c.name for c in plan.companies] if plan else []
        info = ReportInput(
            run_id=self.request.run_id, companies=companies, explanations=state.explanations,
            limitations=state.limitations, evaluator_notes=state.notes, critiques=state.critiques,
            steps_planned=len(state.records) + len(state.queue), steps_with_rows=state.steps_with_rows,
            rejected=sum(1 for f in state.findings if f.status == "rejected"),
            unsupported=sum(1 for f in state.findings if f.status == "unsupported"),
            revision_count=state.revision_count, explanations_sent_back=state.explanations_sent_back,
            agents_used=state.agents_used, trace_path=str(self.tracer.path),
        )
        result = None
        if error is None:
            result, _, paths = self.reporter.report(state.findings, info, self.o.settings.log_dir)
            if state.steps_with_rows == 0:
                error = RunError(type="source_unavailable", recoverable=True,
                                 message="No revenue table could be read from Zetaris; see limitations.")
        reviewed = [r for r in state.records if r.outcome is not None]
        status = "failed" if error else (
            "success" if not state.queue and not state.fallbacks and engine_status == "completed"
            and all(not r.error for r in state.records) else "partial_success")
        collaboration = Collaboration(
            feedback_loop_completed=bool(reviewed) and all(r.outcome is not None for r in state.records if not r.error),
            revision_count=state.revision_count + state.explanations_sent_back,
            evaluator_findings=[*vetted_critiques(state.findings, state.critiques), *state.notes],
        )
        elapsed = round(self.o.clock() - self.started, 3)
        response = RunResponse(
            run_id=self.request.run_id, trace_id=self.tracer.trace_id, status=status,
            agents_used=state.agents_used, result=result, collaboration=collaboration,
            log_path=str(self.tracer.path), execution_time_seconds=max(0.0, elapsed), error=error,
        )
        self.tracer.emit(
            AGENT, "finish_run", input_summary=f"{len(state.records)} steps run, {len(state.queue)} left",
            output_summary=f"{status}; {len(result.findings) if result else 0} findings reported; "
                           f"{state.revision_count} sent back; {elapsed:.1f} s"
                           + (f"; error {error.type}: {error.message}" if error else ""),
            confidence=result.confidence if result else None,
            status=status,
        )
        return response


def run_request(request: RunRequest, settings: Settings | None = None, stream: Any = sys.stdout,
                as_of: date | None = None, trace_id: str | None = None) -> RunResponse:
    """Build the clients for one request and run it. Never raises: a configuration or
    internal failure comes back as the contract's error shape. `trace_id` lets a caller
    that may give up on the run (the server's timeout) name the trace in advance."""
    settings = settings or Settings.from_env()
    tracer = Tracer(request.run_id, settings.log_dir, trace_id=trace_id, stream=stream)
    started = time.monotonic()

    def fail(kind: str, message: str, recoverable: bool) -> RunResponse:
        tracer.emit(AGENT, "finish_run", input_summary=request.input.query, output_summary=f"failed: {kind}: {message}",
                    status="failed")
        return RunResponse(run_id=request.run_id, trace_id=tracer.trace_id, status="failed", log_path=str(tracer.path),
                           execution_time_seconds=round(time.monotonic() - started, 3),
                           error=RunError(type=kind, message=message, recoverable=recoverable))

    if settings.sample_mode or request.options.sample_mode:
        return fail("not_available", "Sample mode is not built yet; run live with the Zetaris and Nightshift "
                                     "settings in the environment.", False)
    try:
        zetaris = ZetarisClient(settings, tracer)
    except ZetarisError as e:
        return fail("configuration", f"Zetaris is not configured: {e}", False)
    try:
        chat: Any = MeterlessClient(settings, tracer)
    except MeterlessError:
        chat = None  # the agents take their code-only fallbacks; the response says so
    try:
        return Orchestrator(settings, tracer, zetaris, chat, as_of=as_of).run(request)
    except Exception as e:  # noqa: BLE001 - the contract wants an error shape, not a stack trace
        return fail("internal_error", f"{type(e).__name__}: {e}", True)
    finally:
        zetaris.close()
        if chat is not None:
            chat.close()

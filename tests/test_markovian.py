"""The Markovian engine's behaviour beyond the eight conformance checks: prompts, the
carry-over cascade, stopping, failure, the JSONL record and the trace. No model calls."""

import json
import threading

import pytest

from app.engines.markovian import (
    ConfigValidationError,
    Markovian,
    build_prompt,
    clean_markers,
    next_carryover,
    parse_markers,
    resolve_config,
)
from app.tracing import NullTracer

CFG = {"chunkSize": 4000, "carryoverTokens": 640, "overlapTokens": 0}


def scripted(*replies, metadata=None):
    prompts = []

    def generate(prompt, attachments, system_prompt, on_stream, abort):
        prompts.append({"prompt": prompt, "attachments": attachments, "system": system_prompt})
        reply = replies[min(len(prompts), len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return {"text": reply, "metadata": metadata or {}}

    generate.prompts = prompts
    return generate


STATE = "[STATE_CHECKPOINT]\nCOMPLETED: fetched Apple rows\nREMAINING: IBM\nDECISIONS: none\nCONTEXT: audit"


def test_defaults_and_snake_case_keys():
    cfg = resolve_config()
    assert (cfg.chunk_size, cfg.max_chunks, cfg.carryover_tokens, cfg.overlap_tokens) == (8000, 12, 256, 128)
    assert resolve_config({"carryover_tokens": 512}).carryover_tokens == 512


@pytest.mark.parametrize("bad", [
    {"chunkSize": 200000}, {"maxChunks": 0}, {"maxChunks": 33}, {"carryoverTokens": 64},
    {"overlapTokens": -1}, {"chunkSize": 4000.5}, {"maxChunks": True}, {"chunkSize": "8000"},
    {"chunkSize": 2000, "carryoverTokens": 640}, {"chunkTokens": 1},
])
def test_bad_config_raises_never_clamps(bad):
    with pytest.raises(ConfigValidationError):
        Markovian(bad)


def test_first_chunk_gets_memory_and_attachments_later_chunks_only_goal_and_carryover():
    goal = "Audit revenue series " + "x" * 400
    gen = scripted("step one " + STATE, "step two [TASK_COMPLETE]")
    run = Markovian(CFG).run(goal, gen, memory_context="MEMORY", attachments=[{"name": "rows", "content": "R1"}],
                             reflect=False)
    first, second = gen.prompts[0]["prompt"], gen.prompts[1]["prompt"]
    assert first.startswith("MEMORY") and "[ATTACHMENT rows]\nR1" in first and goal in first
    assert gen.prompts[0]["attachments"] and not gen.prompts[1]["attachments"]
    assert "MEMORY" not in second and "R1" not in second and goal not in second and goal[:200] in second
    assert "[CARRYOVER STATE]\nCOMPLETED: fetched Apple rows" in second
    assert run.chunks[1].carryover.startswith("COMPLETED: fetched Apple rows")
    assert run.status == "completed" and run.completed and run.output == "step one\n\nstep two"


def test_step_input_reaches_each_chunk():
    gen = scripted("a " + STATE, "b " + STATE, "c [TASK_COMPLETE]")
    Markovian(CFG).run("goal", gen, step_input=lambda step, carry: f"rows for step {step}", reflect=False)
    assert all(f"[STEP INPUT {i}]\nrows for step {i}" in p["prompt"] for i, p in enumerate(gen.prompts, 1))


def test_overlap_block_carries_previous_tail():
    gen = scripted("alpha beta gamma " + STATE, "done [TASK_COMPLETE]")
    Markovian({**CFG, "overlapTokens": 64}).run("goal", gen, reflect=False)
    assert "[OVERLAP]\nalpha beta gamma" in gen.prompts[1]["prompt"]
    assert "[OVERLAP]" not in gen.prompts[0]["prompt"]


def test_cascade_order():
    assert next_carryover("COMPLETED: a long enough explicit state", "", "t", 640) == (
        "COMPLETED: a long enough explicit state", "marker")
    compressor = lambda prompt, system: "COMPLETED: compressed by the model\nREMAINING: none"
    assert next_carryover("short", "", "text", 640, compressor)[1] == "llm"
    broken = lambda prompt, system: (_ for _ in ()).throw(RuntimeError("down"))
    text = "We looked at rows. Therefore the series has a gap in 2019. Status: two companies left."
    carry, level = next_carryover(None, "", text, 640, broken)
    assert level == "heuristic" and carry.startswith("Therefore the series has a gap in 2019")
    carry, level = next_carryover(None, "", "word " * 2000, 128)
    assert level == "truncation" and len(carry) <= 128 * 4 and carry.endswith("word")


def test_marker_cleaning_variants():
    state, done, cleaned = parse_markers("Body text\n[STATE_CHECKPOINT]\nCOMPLETED: x")
    assert (state, done, cleaned) == ("COMPLETED: x", False, "Body text")
    assert parse_markers("All done. [TASK_COMPLETE]")[1:] == (True, "All done.")
    assert parse_markers("<CARRYOVER>kept state</CARRYOVER> body <DONE/>") == ("kept state", True, "body")
    assert clean_markers("keep [STATE] in prose") == "keep [STATE] in prose"


def test_max_chunks_caps_the_run():
    run = Markovian({**CFG, "maxChunks": 3}).run("goal", scripted("more " + STATE), reflect=False)
    assert run.status == "max-chunks" and not run.completed and len(run.chunks) == 3


def test_generator_failure_keeps_partial_output():
    run = Markovian(CFG).run("goal", scripted("one " + STATE, RuntimeError("gateway down")), reflect=False)
    assert run.status == "errored" and "gateway down" in run.error and len(run.chunks) == 1
    assert run.output == "one"


def test_abort_checked_before_each_chunk():
    stop = threading.Event()

    def generate(prompt, attachments, system_prompt, on_stream, abort):
        stop.set()
        return {"text": "one " + STATE}

    run = Markovian(CFG).run("goal", generate, abort=stop, reflect=False)
    assert run.status == "aborted" and len(run.chunks) == 1


def test_deadline_stops_the_run():
    ticks = iter(range(0, 10_000_000, 1000))
    run = Markovian(CFG, clock=lambda: next(ticks)).run("goal", scripted("x " + STATE), reflect=False,
                                                        max_duration_ms=2500)
    assert run.status == "aborted" and "deadline" in run.error and 1 <= len(run.chunks) < 12


def test_stop_when_ends_the_run():
    run = Markovian(CFG).run("goal", scripted("x " + STATE), reflect=False, stop_when=lambda step, carry: step == 2)
    assert run.status == "completed" and len(run.chunks) == 2


def test_reflection_runs_after_completion():
    gen = scripted("only chunk [TASK_COMPLETE]", "Ratified: goal met.")
    run = Markovian(CFG).run("goal", gen)
    assert run.reflection == "Ratified: goal met." and gen.prompts[1]["system"].startswith("You are the reflection")


def test_jsonl_record_and_trace(tmp_path):
    tracer = NullTracer()
    meta = {"routed_model": "nvidia/nemotron-3.5-lightning-30b-a3b", "usage": {"completion_tokens": 9}}
    engine = Markovian(CFG, log_dir=tmp_path, tracer=tracer)
    run = engine.run("goal", scripted("a " + STATE, "b " + STATE, "c [TASK_COMPLETE]", metadata=meta),
                     reflect=False, run_id="r-7", agent_id="Analyst")
    lines = [json.loads(line) for line in (tmp_path / "markovian-r-7.jsonl").read_text().splitlines()]
    assert [line["type"] for line in lines] == ["chunk", "chunk", "chunk", "run"]
    assert lines[0]["carryoverOut"].startswith("COMPLETED: fetched Apple rows") and lines[0]["cascadeLevel"] == "marker"
    record = lines[-1]
    assert record["chunkConfig"] == {"chunkSize": 4000, "maxChunks": 12, "carryoverTokens": 640, "overlapTokens": 0}
    assert record["runId"] == "r-7" and record["agentId"] == "Analyst" and record["tokenLabel"] == "estimated"
    assert record["savings"]["actual"]["tokensSaved"] == record["totalTokensSaved"] == run.total_tokens_saved
    assert "projected" in record["savings"] and record["completed"] is True
    assert run.chunks[0].routed_model == meta["routed_model"] and run.chunks[0].measured_usage == meta["usage"]
    actions = [e.action for e in tracer.events]
    assert actions == ["engine_chunk"] * 3 + ["engine_run"]
    assert tracer.events[0].model == meta["routed_model"] and tracer.events[0].agent_name == "Analyst"


def test_context_per_chunk_stays_bounded_as_steps_grow():
    """The point of the engine: the prompt for chunk 30 is no bigger than for chunk 2."""
    body = "Findings for this step, with enough text to matter. " * 40
    run = Markovian({"chunkSize": 8000, "maxChunks": 30, "carryoverTokens": 256, "overlapTokens": 0}).run(
        "goal", scripted(body + STATE), reflect=False)
    sizes = [c.prompt_tokens for c in run.chunks[1:]]
    assert len(run.chunks) == 30 and max(sizes) - min(sizes) <= 2
    assert all(c.carryover_out_tokens <= 256 for c in run.chunks)
    assert run.efficiency_percent > 80

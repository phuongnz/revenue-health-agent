"""The Markovian conformance checks, ported to pytest under their upstream IDs.

Ported from `engines/markovian/conformance/runner.ts` in github.com/Meterless/Meterless
(Apache License 2.0), fetched at commit 85c5d41493c6ae2ceb06ad76144932309cfda3dd
(2026-07-13). Same IDs, same parameters, same assertions; TypeScript calls become Python
calls (`modelTotals` -> `model_totals`, `run.chunkConfig` -> `run.chunk_config`). This is
a port, not the official suite: say "ported conformance checks pass", never "official
suite green".
"""

import re

import pytest

from app.engines.markovian import ConfigValidationError, Markovian, model_totals, run_efficiency

T0 = 1_750_000_000_000
UPSTREAM_IDS = ("eff-001", "snap-001", "snap-002", "ovl-001", "lab-001", "ref-001", "mrk-001", "cfg-001")


def clock():
    return T0


def step_gen(total):
    """upstream stepGen(total): a body of ~730 chars, then the checkpoint marker with a
    state block, or the final marker on call `total`."""
    calls = {"n": 0}

    def generate(prompt, *_):
        calls["n"] += 1
        n = calls["n"]
        done = n >= total
        body = f"Output for step {n} with enough text to register in the accounting math. " * 10
        marker = ("[TASK_COMPLETE]" if done else
                  f"[STATE_CHECKPOINT]\nCOMPLETED: steps 1-{n}\nREMAINING: {total - n}\nDECISIONS: none\nCONTEXT: conformance")
        return {"text": body + "\n" + marker}

    generate.calls = lambda: calls["n"]
    return generate


def test_eff_001_efficiency_model_reproduces_worked_example():
    """eff-001: efficiency model reproduces the documented worked example: N=20 -> 86% (within 0.5%)"""
    totals = model_totals(20)
    assert totals["naiveTotal"] == 246_000
    assert totals["markovianTotal"] == 34_000
    assert abs(totals["efficiency"] * 100 - 86.18) < 0.5


def test_snap_001_run_carries_chunk_config_snapshot():
    """snap-001: run records carry a mandatory chunkConfig snapshot"""
    m = Markovian({"chunkSize": 4000, "carryoverTokens": 640}, clock=clock)
    run = m.run("snapshot check", step_gen(3), reflect=False)
    assert run.chunk_config is not None and run.chunk_config.carryover_tokens == 640
    assert run.dump()["chunkConfig"]["carryoverTokens"] == 640


def test_snap_002_historical_efficiency_uses_the_runs_own_snapshot():
    """snap-002: historical efficiency is computed against the run's own snapshot, not current config"""
    chunks = [{"id": i, "tokens": 1000, "carryover": "", "content": ""} for i in (1, 2, 3, 4)]
    a = run_efficiency(chunks, {"chunkSize": 8000, "maxChunks": 24, "carryoverTokens": 512, "overlapTokens": 0})
    b = run_efficiency(chunks, {"chunkSize": 8000, "maxChunks": 24, "carryoverTokens": 2048, "overlapTokens": 0})
    assert a["standardCost"] == 10_000, "prefix-sum formula"
    assert a["markovianCost"] == 6_048, "4*512+4000"
    assert a["tokensSaved"] != b["tokensSaved"]


def test_ovl_001_overlap_implemented_or_rejected():
    """ovl-001: overlap contract: implement [OVERLAP] or reject non-zero; accept-and-ignore fails"""
    try:
        constructed = Markovian({"chunkSize": 4000, "carryoverTokens": 640, "overlapTokens": 64}, clock=clock)
    except Exception as err:  # rejecting non-zero values is a conforming choice
        assert re.search(r"overlap", str(err), re.IGNORECASE), f"rejection must name overlapTokens; got: {err}"
        return
    prompts = []

    def generate(prompt, *_):
        prompts.append(prompt)
        done = len(prompts) >= 3
        return {"text": f"chunk {len(prompts)} content " * 20
                + ("[TASK_COMPLETE]" if done else "[STATE_CHECKPOINT]\nCOMPLETED: chunk recorded with carried state")}

    constructed.run("overlap conformance", generate, reflect=False)
    assert any("[OVERLAP]" in p for p in prompts[1:]), (
        "config accepted overlapTokens=64 but no [OVERLAP] block appeared: accept-and-ignore is a contract violation")


def test_lab_001_token_numbers_are_labelled():
    """lab-001: token numbers are labeled: runs carry estimated or measured"""
    m = Markovian({"chunkSize": 4000, "carryoverTokens": 640}, clock=clock)
    run = m.run("label check", step_gen(2), reflect=False)
    assert run.token_label in ("estimated", "measured")


def test_ref_001_reflection_failure_does_not_fail_the_run():
    """ref-001: reflection failure does not fail the run"""
    calls = {"n": 0}

    def generate(prompt, *_):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("provider exploded in reflection")
        return {"text": "done in one chunk [TASK_COMPLETE]"}

    m = Markovian({"chunkSize": 4000, "carryoverTokens": 640}, clock=clock)
    run = m.run("reflection failure", generate)  # reflection on, as upstream
    assert run.status == "completed", "reflection failures must not fail the run"
    assert calls["n"] == 2, "the reflection pass must actually have been attempted"


def test_mrk_001_markers_cleaned_and_drive_completion():
    """mrk-001: markers are cleaned from display content and drive completion"""
    m = Markovian({"chunkSize": 4000, "carryoverTokens": 640}, clock=clock)
    run = m.run("marker cleaning", step_gen(3), reflect=False)
    assert run.status == "completed", "expected completed via [TASK_COMPLETE]"
    assert not re.search(r"STATE_CHECKPOINT|TASK_COMPLETE", run.output), "markers leaked into display output"
    assert len(run.chunks) == 3


def test_cfg_001_out_of_range_config_rejected_with_typed_error():
    """cfg-001: config bounds reject out-of-range values with a typed error, never silent truncation"""
    with pytest.raises(ConfigValidationError):
        Markovian({"chunkSize": 128, "carryoverTokens": 640}, clock=clock)


def test_all_upstream_ids_are_ported():
    names = {n for n in globals() if n.startswith("test_")}
    for upstream in UPSTREAM_IDS:
        assert any(n.startswith("test_" + upstream.replace("-", "_")) for n in names), upstream

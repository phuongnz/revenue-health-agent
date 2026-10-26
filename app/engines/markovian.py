"""The Markovian engine: a long task run as bounded chunks, each seeing only a compressed
carry-over of the chunks before it, so context per step stays flat as steps grow.

The engine never calls a model. The generator is injected:

    generate(prompt, attachments, system_prompt, on_stream, abort) -> {"text", "metadata"}

with the Nightshift receipt in `metadata` (`MeterlessClient.as_generator` provides one;
sample mode and the tests use scripted ones).

One run, in order, while chunks remain and the task is not done: check abort and the
deadline; build the prompt (first chunk: memory context, framing, goal, attachments;
later chunks: goal cut to ~200 chars, the carry-over, the `[OVERLAP]` tail of the previous
chunk when `overlapTokens > 0`, the rules); call the generator; strip the markers;
account tokens; persist the chunk; compress into the next carry-over through the cascade
(explicit state after `[STATE_CHECKPOINT]`, then `compressor_fn`, then key phrases such
as "Therefore" / "Key insight" / "Status", then tail truncation to the budget); stop on
`[TASK_COMPLETE]`. Then reflection, whose failure never fails the run.

Token numbers are estimates, `ceil(chars / 4)`, and the run says so (`tokenLabel`
"estimated"); provider-reported usage from the receipts is kept per chunk alongside, as
measured, but does not enter the efficiency math: Nightshift's completion tokens include
reasoning and its prompt tokens include context the gateway injects.

Written for this project in Python from the Markovian engine specification, docs and
conformance suite in Meterless's repository (github.com/Meterless/Meterless,
`engines/markovian`, Apache License 2.0); the cascade, marker and accounting rules follow
its reference implementation. See THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# --- configuration ------------------------------------------------------------

BOUNDS: dict[str, tuple[int, int]] = {
    "chunkSize": (1000, 128000),
    "maxChunks": (1, 32),
    "carryoverTokens": (128, 32768),
    "overlapTokens": (0, 4096),
}
DEFAULTS: dict[str, int] = {"chunkSize": 8000, "maxChunks": 12, "carryoverTokens": 256, "overlapTokens": 128}
FRAMING_TOKENS = 400
OUTPUT_BUDGET = 1200
MIN_STATE_CHARS = 24


class ConfigValidationError(ValueError):
    """A chunk config value is out of range or not an integer. Never clamped."""

    def __init__(self, message: str) -> None:
        super().__init__(f"Markovian config: {message}")


class _Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class ChunkConfig(_Camel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)

    chunk_size: int
    max_chunks: int
    carryover_tokens: int
    overlap_tokens: int


_SNAKE = {"chunk_size": "chunkSize", "max_chunks": "maxChunks", "carryover_tokens": "carryoverTokens",
          "overlap_tokens": "overlapTokens"}


def resolve_config(partial: Mapping[str, Any] | ChunkConfig | None = None) -> ChunkConfig:
    """Defaults overlaid with `partial` (camelCase or snake_case keys), validated.
    Out-of-range or non-integer values raise ConfigValidationError; nothing is clamped.
    The bounds' step sizes (1000, 128) are slider granularity upstream, not validation,
    so only min and max are enforced, as the reference does."""
    if isinstance(partial, ChunkConfig):
        partial = partial.dump()
    given = {_SNAKE.get(k, k): v for k, v in dict(partial or {}).items()}
    unknown = set(given) - set(BOUNDS)
    if unknown:
        raise ConfigValidationError(f"unknown keys {sorted(unknown)}")
    cfg = {**DEFAULTS, **given}
    for key, (low, high) in BOUNDS.items():
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigValidationError(f"{key} must be an integer, got {value!r}")
        if not low <= value <= high:
            raise ConfigValidationError(f"{key}={value} outside [{low}, {high}]")
    floor = FRAMING_TOKENS + cfg["carryoverTokens"] + OUTPUT_BUDGET
    if cfg["chunkSize"] < floor:
        raise ConfigValidationError(
            f"chunkSize={cfg['chunkSize']} < framing({FRAMING_TOKENS}) + carryover({cfg['carryoverTokens']})"
            f" + outputBudget({OUTPUT_BUDGET})"
        )
    return ChunkConfig.model_validate(cfg)


# --- records ------------------------------------------------------------------

RunStatus = Literal["completed", "max-chunks", "errored", "aborted"]
Mode = Literal["ARCHITECT", "RESEARCH"]
CascadeLevel = Literal["marker", "llm", "heuristic", "truncation"]


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


class ChunkInfo(_Camel):
    id: int  # 1-based
    tokens: int  # ceil(chars / 4) of the cleaned content
    carryover: str  # the state passed INTO this chunk
    content: str  # marker-cleaned output
    cascade_level: CascadeLevel | None = None  # how the carry-over OUT of this chunk was made
    carryover_out_tokens: int | None = None
    prompt_tokens: int | None = None  # estimate of the prompt sent for this chunk
    projected_savings: int | None = None
    routed_model: str | None = None
    measured_usage: dict[str, Any] | None = None


class MarkovianRun(_Camel):
    id: str
    timestamp: int  # ms since the epoch, from the clock
    prompt: str  # the goal, cut to 500 chars
    mode: Mode
    chunk_config: ChunkConfig  # mandatory snapshot: historical efficiency uses it, not current config
    chunks: list[ChunkInfo] = Field(default_factory=list)
    total_tokens_used: int = 0
    total_tokens_saved: int = 0
    efficiency_percent: float = 0.0
    duration_ms: int = 0
    status: RunStatus = "completed"
    completed: bool = False
    token_label: Literal["estimated", "measured"] = "estimated"
    output: str = ""
    reflection: str | None = None
    error: str | None = None
    run_id: str | None = None
    agent_id: str | None = None
    log_path: str | None = None


# --- efficiency math ------------------------------------------------------------

DOC_DEFAULTS = {"F": 400, "G": 200, "S": 300, "O": 1200, "C": 800}


def model_totals(n: int, k: Mapping[str, int] = DOC_DEFAULTS) -> dict[str, float]:
    """The symbolic cost model of upstream docs/efficiency-model.md, input tokens over N steps:
    naive = N(F+G+S) + O·N(N-1)/2, markovian = N(F+G+C+S). Efficiency is a ratio, 0..1."""
    naive = n * (k["F"] + k["G"] + k["S"]) + k["O"] * n * (n - 1) // 2
    markovian = n * (k["F"] + k["G"] + k["C"] + k["S"])
    savings = naive - markovian
    return {"naiveTotal": naive, "markovianTotal": markovian, "savings": savings,
            "efficiency": savings / naive if naive else 0.0}


def run_efficiency(chunks: list[ChunkInfo] | list[Mapping[str, Any]], config: ChunkConfig | Mapping[str, Any]) -> dict[str, float]:
    """A run's efficiency against the config given, which must be the run's own snapshot.
    Standard cost re-sends every earlier chunk (prefix sums); Markovian cost is one
    carry-over budget per chunk plus the chunks themselves."""
    cfg = config if isinstance(config, ChunkConfig) else resolve_config(config)
    tokens = [c.tokens if isinstance(c, ChunkInfo) else int(c["tokens"]) for c in chunks]
    used = sum(tokens)
    standard = prefix = 0
    for t in tokens:
        standard += prefix + t
        prefix += t
    markovian = len(tokens) * cfg.carryover_tokens + used
    saved = max(0, standard - markovian)
    return {"totalTokensUsed": used, "standardCost": standard, "markovianCost": markovian,
            "tokensSaved": saved, "efficiencyPercent": saved / standard * 100 if standard else 0.0}


def projection_curve(config: ChunkConfig, steps: int = 10) -> list[dict[str, int]]:
    """Projected per-step context, chunks modelled at the full chunkSize."""
    return [{"step": s, "standard": (s - 1) * config.chunk_size + config.chunk_size,
             "markovian": config.carryover_tokens + config.chunk_size} for s in range(1, steps + 1)]


# --- markers ----------------------------------------------------------------------

_STATE_BLOCK = re.compile(r"<CARRYOVER>(.*?)</CARRYOVER>", re.DOTALL)
_STATE_TAILS = (
    re.compile(r"\[STATE_CHECKPOINT\]\s*(.*)$", re.DOTALL),
    re.compile(r"@@@STATE@@@\s*(.*)$", re.DOTALL),
    re.compile(r"(?<!_)\[STATE\]\s*(.*)$", re.DOTALL),
    re.compile(r"---STATE---\s*(.*)$", re.DOTALL),
)
_FINAL = (re.compile(r"\[TASK_COMPLETE\]"), re.compile(r"@@@FINAL@@@"), re.compile(r"(?<!_)\[FINAL\]"),
          re.compile(r"---FINAL---"), re.compile(r"<DONE\s*/?>"))


def parse_markers(raw: str) -> tuple[str | None, bool, str]:
    """Return (state after the checkpoint marker or None, final marker present, cleaned text)."""
    blocks = _STATE_BLOCK.findall(raw)
    state: str | None = blocks[-1].strip() if blocks else None
    if state is None:
        for pattern in _STATE_TAILS:
            m = pattern.search(raw)
            if m:
                state = m.group(1).strip()
                break
    done = any(p.search(raw) for p in _FINAL)
    return state, done, clean_markers(raw)


def clean_markers(raw: str) -> str:
    out = _STATE_BLOCK.sub("", raw)
    out = re.sub(r"<PROGRESS>.*?</PROGRESS>", "", out, flags=re.DOTALL)
    for pattern in (r"\[STATE_CHECKPOINT\].*$", r"@@@STATE@@@.*$", r"---STATE---.*$", r"\n\[STATE\].*$",
                    r"\n\[FINAL\].*$"):
        out = re.sub(pattern, "", out, flags=re.DOTALL)
    for pattern in (r"\[TASK_COMPLETE\]", r"@@@FINAL@@@", r"---FINAL---", r"<DONE\s*/?>"):
        out = re.sub(pattern, "", out)
    out = re.sub(r"[ \t]+$", "", out, flags=re.MULTILINE)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


# --- carry-over cascade -------------------------------------------------------------

_KEY_PHRASE = re.compile(
    r"(?:^|\n|\.\s+)((?:Therefore|Key insight|Status|Decision|Decided|Next|Remaining|Completed)[^.\n]*)",
    re.IGNORECASE,
)
COMPRESSOR_SYSTEM = "You compress reasoning state. Be terse and lossless on decisions."


def next_carryover(
    state: str | None,
    previous: str,
    chunk_text: str,
    budget: int,
    compressor_fn: Callable[[str, str], str] | None = None,
) -> tuple[str, CascadeLevel]:
    """The carry-over out of a chunk, and the cascade level that produced it."""
    if state is not None:
        sanitized = re.sub(r"[<>\[\]@-]{3,}", "", state).strip()
        if len(sanitized) >= MIN_STATE_CHARS and estimate_tokens(sanitized) <= budget:
            return sanitized, "marker"
    if compressor_fn is not None:
        try:
            prompt = (
                f"Previous state:\n{previous or '(none)'}\n\nLatest chunk (tail):\n{chunk_text[-2000:]}\n\n"
                "Compress into 3-5 critical points as a labeled block:\n"
                "COMPLETED: ...\nREMAINING: ...\nDECISIONS: ...\nCONTEXT: ..."
            )
            compressed = (compressor_fn(prompt, COMPRESSOR_SYSTEM) or "").strip()
            if len(compressed) >= MIN_STATE_CHARS and estimate_tokens(compressed) <= budget:
                return compressed, "llm"
        except Exception:  # noqa: BLE001 - any compressor failure falls through to heuristics
            pass
    phrases = [m.group(1).strip() for m in _KEY_PHRASE.finditer(chunk_text)][:5]
    if phrases:
        summary = "; ".join(phrases)
        if estimate_tokens(summary) <= budget:
            return summary, "heuristic"
    words = (chunk_text or previous).split()
    tail: list[str] = []
    for word in reversed(words):
        if estimate_tokens(" ".join([word, *tail])) > budget:
            break
        tail.insert(0, word)
    return " ".join(tail), "truncation"


# --- prompts --------------------------------------------------------------------------

MARKER_RULES = (
    "End every non-final chunk with [STATE_CHECKPOINT] followed by a concise state block:\n"
    "COMPLETED: ...\nREMAINING: ...\nDECISIONS: ...\nCONTEXT: ...\n"
    "End the final chunk with [TASK_COMPLETE]. Exactly one marker per chunk. Never narrate the markers."
)
FRAMING = {
    "ARCHITECT": "You are executing a long task in bounded chunks. Analyze the goal, produce a plan, and start.",
    "RESEARCH": "You are executing recursive research in bounded chunks. Log every action as '>> action' "
                "and keep a research-tree state snapshot.",
}


def build_prompt(
    *, mode: Mode, goal: str, step: int, carryover: str, overlap: str, memory_context: str | None,
    attachments: list[Any], step_input: str | None, framing: str | None = None,
) -> str:
    parts: list[str] = []
    if step == 1:
        if memory_context:
            parts += [memory_context.strip(), ""]
        parts += [framing or FRAMING[mode], "", f"User Request: {goal}"]
        for a in attachments:
            name, content = (a.get("name", "attachment"), a.get("content", "")) if isinstance(a, Mapping) else ("attachment", str(a))
            parts += ["", f"[ATTACHMENT {name}]\n{content}"]
    else:
        parts += [f"Continuing a chunked {mode.lower()} run. Goal (truncated): {goal[:200]}", "",
                  f"[CARRYOVER STATE]\n{carryover}"]
        if overlap:
            parts += ["", f"[OVERLAP]\n{overlap}"]
        parts += ["", "Continue exactly where the state leaves off. Do not repeat completed work."]
    if step_input:
        parts += ["", f"[STEP INPUT {step}]\n{step_input}"]
    parts += ["", MARKER_RULES]
    return "\n".join(parts)


def overlap_slice(previous_cleaned: str, config: ChunkConfig) -> str:
    """The last overlapTokens*4 characters of the previous chunk's cleaned output."""
    if config.overlap_tokens <= 0 or not previous_cleaned:
        return ""
    return previous_cleaned[-config.overlap_tokens * 4:]


# --- the engine -------------------------------------------------------------------------

Generator = Callable[..., Mapping[str, Any]]
REFLECTION_SYSTEM = "You are the reflection pass. Be brief and concrete."


def _aborted(abort: Any) -> bool:
    if abort is None:
        return False
    if callable(getattr(abort, "is_set", None)):
        return bool(abort.is_set())
    return bool(abort() if callable(abort) else abort)


class Markovian:
    """Configure once, run many. Config is validated here: out of range raises."""

    def __init__(
        self,
        chunk_config: Mapping[str, Any] | ChunkConfig | None = None,
        generator: Generator | None = None,
        compressor_fn: Callable[[str, str], str] | None = None,
        clock: Callable[[], float] | None = None,
        log_dir: str | Path | None = None,
        tracer: Any = None,
    ) -> None:
        self.config = resolve_config(chunk_config)
        self.generator = generator
        self.compressor_fn = compressor_fn
        self.clock = clock or (lambda: time.time() * 1000)
        self.log_dir = Path(log_dir) if log_dir is not None else None
        self.tracer = tracer

    # JSONL record: one line per chunk as it lands, then the run record with the projected
    # and actual savings. Written only when a log_dir is set.
    def _write(self, path: Path | None, record: dict[str, Any]) -> None:
        if path is None:
            return
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _trace(self, agent: str, action: str, **kwargs: Any) -> None:
        if self.tracer is not None:
            self.tracer.emit(agent, action, **kwargs)

    def run(
        self,
        goal: str,
        generator: Generator | None = None,
        *,
        mode: Mode = "ARCHITECT",
        memory_context: str | None = None,
        attachments: list[Any] | None = None,
        system_prompt: str | None = None,
        framing: str | None = None,
        step_input: Callable[[int, str], str | None] | None = None,
        stop_when: Callable[[int, str], bool] | None = None,
        abort: Any = None,
        on_stream: Callable[[str], None] | None = None,
        reflect: bool = True,
        run_id: str | None = None,
        agent_id: str | None = None,
        max_duration_ms: float | None = None,
    ) -> MarkovianRun:
        if not goal:
            raise ValueError("Markovian: run needs a goal")
        generate = generator or self.generator
        if generate is None:
            raise ValueError("Markovian: provide a generator")
        attachments = list(attachments or [])
        start = self.clock()
        run = MarkovianRun(
            id=f"mk-{uuid.uuid4().hex[:12]}", timestamp=int(start), prompt=goal[:500], mode=mode,
            chunk_config=self.config, run_id=run_id, agent_id=agent_id,
        )
        log_path: Path | None = None
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.log_dir / f"markovian-{run_id or run.id}.jsonl"
            run.log_path = str(log_path)
        agent = agent_id or "MarkovianEngine"
        cfg = self.config
        carryover = ""
        previous_cleaned = ""
        projected = 0
        done = False

        try:
            while len(run.chunks) < cfg.max_chunks and not done:
                if _aborted(abort):
                    run.status, run.error = "aborted", "aborted before chunk"
                    break
                if max_duration_ms is not None and self.clock() - start > max_duration_ms:
                    run.status, run.error = "aborted", f"run deadline of {max_duration_ms:g} ms reached"
                    break
                step = len(run.chunks) + 1
                extra = step_input(step, carryover) if step_input else None
                prompt = build_prompt(
                    mode=mode, goal=goal, step=step, carryover=carryover,
                    overlap=overlap_slice(previous_cleaned, cfg), memory_context=memory_context,
                    attachments=attachments if step == 1 else [], step_input=extra, framing=framing,
                )
                reply = generate(prompt, attachments if step == 1 else [], system_prompt, on_stream, abort)
                text = str((reply or {}).get("text") or "")
                metadata = (reply or {}).get("metadata") or {}
                state, final, content = parse_markers(text)
                tokens = estimate_tokens(content)
                standard_chunk = (step - 1) * cfg.chunk_size + tokens
                markovian_chunk = cfg.carryover_tokens + tokens
                projected += max(0, standard_chunk - markovian_chunk)
                chunk = ChunkInfo(
                    id=step, tokens=tokens, carryover=carryover, content=content,
                    prompt_tokens=estimate_tokens(prompt), projected_savings=projected,
                    routed_model=metadata.get("routed_model") if isinstance(metadata, Mapping) else None,
                    measured_usage=metadata.get("usage") if isinstance(metadata, Mapping) else None,
                )
                run.chunks.append(chunk)  # persisted before compression: its cost is already paid
                previous_cleaned = content
                if _aborted(abort):
                    run.status, run.error = "aborted", f"aborted after chunk {step}"
                    self._write(log_path, {"type": "chunk", **chunk.dump()})
                    break
                carryover, level = next_carryover(state, carryover, text, cfg.carryover_tokens, self.compressor_fn)
                chunk.cascade_level = level
                chunk.carryover_out_tokens = estimate_tokens(carryover)
                self._write(log_path, {"type": "chunk", "runId": run_id, "engineRunId": run.id,
                                       "carryoverOut": carryover, **chunk.dump()})
                self._trace(
                    agent, "engine_chunk",
                    input_summary=f"chunk {step}/{cfg.max_chunks}; prompt ~{chunk.prompt_tokens} tokens; "
                                  f"carry-over in ~{estimate_tokens(chunk.carryover)} tokens",
                    output_summary=f"~{tokens} tokens out; carry-over by {level}, ~{chunk.carryover_out_tokens}"
                                   f" tokens{'; final' if final else ''}",
                    model=chunk.routed_model,
                    details={"engine_run_id": run.id, "step": step, "cascade_level": level,
                             "projected_savings": projected},
                )
                if final:
                    done = True
                elif stop_when is not None and stop_when(step, carryover):
                    done = True
            if not done and run.status == "completed" and len(run.chunks) >= cfg.max_chunks:
                run.status = "max-chunks"
        except Exception as e:  # noqa: BLE001 - a failed chunk ends the run with its partial output
            run.status, run.error = "errored", f"{type(e).__name__}: {e}"

        run.output = "\n\n".join(c.content for c in run.chunks)
        eff = run_efficiency(run.chunks, run.chunk_config)
        run.total_tokens_used = int(eff["totalTokensUsed"])
        run.total_tokens_saved = int(eff["tokensSaved"])
        run.efficiency_percent = round(float(eff["efficiencyPercent"]), 4)

        if reflect and run.status == "completed" and run.chunks:
            try:
                prompt = (
                    f"Reflect on this completed run.\nGoal: {goal[:200]}\nFinal state: {carryover}\n"
                    f"Output (truncated): {run.output[:4000]}\n"
                    "Verdict: ratify if the goal is met, refine with concrete gaps otherwise."
                )
                reply = generate(prompt, [], REFLECTION_SYSTEM, None, abort)
                run.reflection = clean_markers(str((reply or {}).get("text") or ""))
            except Exception as e:  # noqa: BLE001 - reflection failure never fails the run
                run.reflection = None
                run.error = f"reflection failed (run preserved): {type(e).__name__}: {e}"

        run.completed = run.status == "completed"
        run.duration_ms = int(self.clock() - start)
        self._write(log_path, {
            "type": "run", **run.dump(),
            "savings": {"label": run.token_label, "projected": projected, "actual": eff,
                        "projectionCurve": projection_curve(cfg, max(len(run.chunks), 1))},
        })
        self._trace(
            agent, "engine_run",
            input_summary=_one_line(goal, 200),
            output_summary=f"{run.status}; {len(run.chunks)} chunks; ~{run.total_tokens_used} tokens; "
                           f"{run.efficiency_percent:.1f}% saved vs re-sending history ({run.token_label})",
            status="success" if run.status == "completed" else ("partial_success" if run.chunks else "failed"),
            details={"engine_run_id": run.id, "status": run.status, "log_path": run.log_path,
                     "error": run.error},
        )
        return run


def _one_line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"

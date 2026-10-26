"""Nightshift client: OpenAI-compatible chat completions through the local Meterless gateway.

- **Lane pinned per agent.** The request's `model` is a Nemotron lane id, never a routing
  mode: Ultra for the Evaluator, Lightning for every other agent (`Settings.lane_for`).
- **Receipt kept.** Every completion carries a `meterless` block, the compute receipt
  (`requested_model`, `routed_model`, `winning_route.provider`, per-phase timings). The
  client returns it whole and puts its main fields in the trace event, with
  `routed_model` in the event's `model`.
  A reply whose `routed_model` is not Nemotron is returned but traced `needs_validation`.
- **Bounded time.** 30-90 s per request (`timeout_s` per call, capped at 90 s); three
  attempts with exponential back-off of 1-8 s on a timeout, a connection error, an HTTP
  5xx or 429. Other 4xx are not retried. A `deadline` caps every attempt to the time left
  and stops the retries when it has passed.
- **Thinking only where it is wanted.** Nemotron reasons before it answers unless the
  chat template is told not to, and its completion tokens include the reasoning: a
  Lightning call asked for a paragraph spent its whole `max_tokens` thinking and came
  back empty (HTTP 502 `empty_response`). So a call thinks only on the Evaluator's lane
  (Ultra critiques the findings); every other call sends
  `chat_template_kwargs: {enable_thinking: false}`. `thinking=` overrides per call.
- **Reasoning split off.** Nemotron Ultra may put its reasoning in the content inside
  `<think>…</think>`; `text` is the answer without it, `reasoning` keeps it.

`as_generator` adapts the client to the Markovian engine's injected generator,
`generate(prompt, attachments, system_prompt, on_stream, abort) -> {"text", "metadata"}`.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import NEMOTRON_PREFIX, Settings
from app.tracing import NullTracer, Tracer

CHAT_PATH = "/chat/completions"
RECEIPT_TRACE_KEYS = (
    "schema", "receipt_id", "decision_id", "requested_model", "provider_model", "routed_model",
    "winning_route", "requires_review", "phases", "usage", "cost", "savings", "cache", "mode",
    "scope", "app", "from_cache",
)
"""The receipt fields copied into a trace line. The full receipt (with its nested copy, the
signed envelope and the host telemetry, about 10 KB) stays on `ChatResult.receipt`."""
MAX_TIMEOUT_S = 90.0  # the build contract's upper bound for one model call
MIN_ATTEMPT_S = 5.0
_THINK = re.compile(r"<think>(.*?)(</think>|$)", re.DOTALL | re.IGNORECASE)


class MeterlessError(Exception):
    """A model request failed: rejected by the gateway, or out of attempts."""


class _Transient(MeterlessError):
    pass


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def compact_receipt(receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    if receipt is None:
        return None
    return {k: receipt[k] for k in RECEIPT_TRACE_KEYS if k in receipt}


def split_reasoning(content: str) -> tuple[str, str]:
    """Return (answer, reasoning): `<think>` blocks removed from the answer."""
    reasoning = "\n".join(m.group(1).strip() for m in _THINK.finditer(content))
    answer = _THINK.sub("", content).strip()
    return answer, reasoning


@dataclass
class ChatResult:
    text: str
    requested_model: str
    routed_model: str | None
    receipt: dict[str, Any] | None
    reasoning: str = ""
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    attempts: int = 1
    duration_ms: int = 0

    @property
    def on_nemotron(self) -> bool:
        return bool(self.routed_model and self.routed_model.startswith(NEMOTRON_PREFIX))

    def metadata(self) -> dict[str, Any]:
        """What the engine stores per chunk: the receipt and the measured usage."""
        return {
            "requested_model": self.requested_model,
            "routed_model": self.routed_model,
            "receipt": self.receipt,
            "usage": self.usage,
            "finish_reason": self.finish_reason,
        }


class MeterlessClient:
    def __init__(
        self,
        settings: Settings,
        tracer: Tracer | None = None,
        transport: httpx.BaseTransport | None = None,
        attempts: int = 3,
        sleep: Any = time.sleep,
    ) -> None:
        if settings.meterless_runtime_token is None:
            raise MeterlessError("METERLESS_RUNTIME_TOKEN is not set")
        self.settings = settings
        self.tracer = tracer or NullTracer()
        self.attempts = attempts
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=settings.meterless_base_url,
            timeout=httpx.Timeout(settings.meterless_timeout_s, connect=10.0),
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MeterlessClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _post(self, body: dict[str, Any], timeout_s: float | None = None) -> dict[str, Any]:
        token = self.settings.meterless_runtime_token.get_secret_value()  # type: ignore[union-attr]
        headers = {
            "Authorization": f"Bearer {token}",
            "meterless-app": self.settings.meterless_app,
            "meterless-scope": "user",
        }
        timeout_s = timeout_s or self.settings.meterless_timeout_s
        try:
            response = self._http.post(CHAT_PATH, json=body, headers=headers,
                                       timeout=httpx.Timeout(timeout_s, connect=min(10.0, timeout_s)))
        except httpx.TimeoutException as e:
            raise _Transient(f"timed out after {timeout_s:.0f} s") from e
        except httpx.TransportError as e:
            raise _Transient(f"cannot reach Nightshift: {type(e).__name__} (is the app running?)") from e
        if response.status_code == 429 or response.status_code >= 500:
            raise _Transient(f"HTTP {response.status_code}: {_short(response.text, 200)}")
        if response.status_code >= 400:
            raise MeterlessError(f"HTTP {response.status_code}: {_short(response.text, 300)}")
        try:
            out = response.json()
        except ValueError as e:
            raise MeterlessError(f"not a JSON reply: {_short(response.text, 160)}") from e
        if not isinstance(out, dict) or not out.get("choices"):
            raise MeterlessError(f"reply without choices: {_short(str(out), 200)}")
        return out

    def chat(
        self,
        agent_name: str,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        purpose: str = "",
        target_agent: str | None = None,
        extra: dict[str, Any] | None = None,
        thinking: bool | None = None,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> ChatResult:
        """One chat completion on the agent's pinned lane, traced with its receipt.
        `thinking` defaults to on for the Evaluator's lane only; `timeout_s` bounds each
        attempt (at most 90 s); `deadline` (a `time.monotonic()` value) bounds them all."""
        lane = self.settings.lane_for(agent_name)
        body: dict[str, Any] = {
            "model": lane,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if thinking is None:
            thinking = lane == self.settings.evaluator_lane
        if not thinking:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        if extra:
            for k, v in extra.items():
                if k in ("model", "messages", "stream"):
                    continue
                if k == "chat_template_kwargs" and isinstance(v, dict):
                    body[k] = {**body.get(k, {}), **v}
                else:
                    body[k] = v
        per_attempt = min(MAX_TIMEOUT_S, timeout_s or self.settings.meterless_timeout_s)

        def attempt_timeout() -> float:
            if deadline is None:
                return per_attempt
            left = deadline - time.monotonic()
            if left < MIN_ATTEMPT_S:
                raise MeterlessError(f"no time left for a model call ({max(left, 0):.0f} s of the budget remain)")
            return min(per_attempt, left)
        prompt = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")

        def before_sleep(state: RetryCallState) -> None:
            exc = state.outcome.exception() if state.outcome else None
            self.tracer.emit(
                agent_name, "model_call", input_summary=_short(prompt),
                output_summary=f"attempt {state.attempt_number} failed: {exc}; retrying",
                target_agent=target_agent, retry_count=state.attempt_number, status="retrying",
                details={"requested_model": lane, "error": str(exc)},
            )

        started = time.monotonic()
        attempt = 0
        try:
            for attempt_state in Retrying(
                stop=stop_after_attempt(self.attempts),
                wait=wait_exponential(multiplier=1, min=1, max=8),
                retry=retry_if_exception(lambda e: isinstance(e, _Transient)),
                before_sleep=before_sleep,
                sleep=self._sleep,
                reraise=True,
            ):
                with attempt_state:
                    attempt = attempt_state.retry_state.attempt_number
                    out = self._post(body, attempt_timeout())
        except MeterlessError as e:
            self.tracer.emit(
                agent_name, "model_call", input_summary=_short(prompt), output_summary=f"failed: {e}",
                target_agent=target_agent, retry_count=max(attempt - 1, 0), status="failed",
                details={"requested_model": lane, "error": str(e), "attempts": attempt},
            )
            raise MeterlessError(str(e)) from e

        choice = out["choices"][0] or {}
        message = choice.get("message") or {}
        content = message.get("content") or ""
        answer, reasoning = split_reasoning(content)
        extra_reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        if isinstance(extra_reasoning, str) and extra_reasoning:
            reasoning = (extra_reasoning + "\n" + reasoning).strip()
        receipt = out.get("meterless") if isinstance(out.get("meterless"), dict) else None
        routed = (receipt or {}).get("routed_model") or None
        result = ChatResult(
            text=answer,
            requested_model=lane,
            routed_model=routed,
            receipt=receipt,
            reasoning=reasoning,
            finish_reason=choice.get("finish_reason"),
            usage=out.get("usage") or {},
            attempts=attempt,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        ok = result.on_nemotron
        self.tracer.emit(
            agent_name,
            "model_call",
            input_summary=_short(prompt),
            output_summary=_short(answer) if ok else f"routed off Nemotron ({routed}): {_short(answer, 200)}",
            target_agent=target_agent,
            retry_count=attempt - 1,
            status="success" if ok else "needs_validation",
            model=routed or out.get("model"),
            receipt=compact_receipt(receipt),
            details={
                "requested_model": lane,
                "finish_reason": result.finish_reason,
                "usage": result.usage,
                "attempts": attempt,
                "duration_ms": result.duration_ms,
                **({"purpose": purpose} if purpose else {}),
            },
        )
        return result

    def as_generator(self, agent_name: str, max_tokens: int = 2048) -> Callable[..., dict[str, Any]]:
        """The engine's injected generator, on this agent's lane."""

        def generate(
            prompt: str,
            attachments: list[Any] | None = None,
            system_prompt: str | None = None,
            on_stream: Callable[[str], None] | None = None,
            abort: Any = None,
        ) -> dict[str, Any]:
            if abort is not None and getattr(abort, "is_set", lambda: False)():
                raise MeterlessError("aborted before the model call")
            messages: list[dict[str, str]] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            parts = [prompt] + [a if isinstance(a, str) else str(a) for a in (attachments or [])]
            messages.append({"role": "user", "content": "\n\n".join(parts)})
            result = self.chat(agent_name, messages, max_tokens=max_tokens, purpose="markovian chunk")
            if on_stream is not None:
                on_stream(result.text)
            return {"text": result.text, "metadata": result.metadata()}

        return generate

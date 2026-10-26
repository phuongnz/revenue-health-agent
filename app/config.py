"""Configuration, read from the environment and nowhere else.

Credentials reach the process through the environment (the maintainer's `./run.sh`
injects them from OpenBao); they are never read from a file in this repository, and they
are held as `SecretStr` so a repr or a log line cannot print them.

Model lanes. Every model request is pinned to a Nemotron lane through Nightshift:
Lightning for the agents by default, Ultra for the Evaluator. `METERLESS_MODEL` names the
default lane when it is a Nemotron model; a routing mode such as `meterless/auto` is
ignored there, because a routing mode does not pin the model the receipt must show.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr

LIGHTNING_LANE = "nvidia/nemotron-3.5-lightning-30b-a3b"
ULTRA_LANE = "nvidia/nemotron-3-ultra-550b-a55b"
NEMOTRON_PREFIX = "nvidia/nemotron"

DEFAULT_ZETARIS_REST_URL = "http://localhost:8888/api/v1.0"
DEFAULT_METERLESS_BASE_URL = "http://127.0.0.1:8088/v1"

_TRUE = {"1", "true", "yes", "on"}


class ConfigError(ValueError):
    """A setting is present but unusable."""


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE


def _number(env: Mapping[str, str], key: str, default: float, low: float, high: float) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as e:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from e
    if not low <= value <= high:
        raise ConfigError(f"{key} must be between {low:g} and {high:g}, got {value:g}")
    return value


def _secret(env: Mapping[str, str], key: str) -> SecretStr | None:
    value = (env.get(key) or "").strip()
    return SecretStr(value) if value else None


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    zetaris_rest_url: str = DEFAULT_ZETARIS_REST_URL
    zetaris_org_id: str = "1"
    zetaris_api_key: SecretStr | None = None
    zetaris_timeout_s: float = Field(default=45.0, ge=30.0, le=60.0)

    meterless_base_url: str = DEFAULT_METERLESS_BASE_URL
    meterless_runtime_token: SecretStr | None = None
    meterless_app: str = "revenue-health-agent"
    default_lane: str = LIGHTNING_LANE
    evaluator_lane: str = ULTRA_LANE
    meterless_timeout_s: float = Field(default=60.0, ge=30.0, le=90.0)

    sample_mode: bool = False
    log_level: str = "INFO"
    log_dir: Path = Path("logs")
    port: int = 8000

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        model = (env.get("METERLESS_MODEL") or "").strip()
        evaluator = (env.get("METERLESS_EVALUATOR_MODEL") or "").strip()
        if evaluator and not evaluator.startswith(NEMOTRON_PREFIX):
            raise ConfigError(
                f"METERLESS_EVALUATOR_MODEL must name a Nemotron model ({NEMOTRON_PREFIX}…), got {evaluator!r}"
            )
        port_raw = (env.get("PORT") or "8000").strip()
        if not port_raw.isdigit() or not 0 < int(port_raw) < 65536:
            raise ConfigError(f"PORT must be a TCP port number, got {port_raw!r}")
        return cls(
            zetaris_rest_url=(env.get("ZETARIS_REST_URL") or DEFAULT_ZETARIS_REST_URL).strip().rstrip("/"),
            zetaris_org_id=(env.get("ZETARIS_ORG_ID") or "1").strip(),
            zetaris_api_key=_secret(env, "ZETARIS_API_KEY"),
            zetaris_timeout_s=_number(env, "ZETARIS_TIMEOUT_S", 45.0, 30.0, 60.0),
            meterless_base_url=(env.get("METERLESS_BASE_URL") or DEFAULT_METERLESS_BASE_URL).strip().rstrip("/"),
            meterless_runtime_token=_secret(env, "METERLESS_RUNTIME_TOKEN"),
            default_lane=model if model.startswith(NEMOTRON_PREFIX) else LIGHTNING_LANE,
            evaluator_lane=evaluator or ULTRA_LANE,
            meterless_timeout_s=_number(env, "METERLESS_TIMEOUT_S", 60.0, 30.0, 90.0),
            sample_mode=_flag(env.get("SAMPLE_MODE")),
            log_level=(env.get("LOG_LEVEL") or "INFO").strip().upper(),
            log_dir=Path((env.get("LOG_DIR") or "logs").strip()),
            port=int(port_raw),
        )

    def lane_for(self, agent_name: str) -> str:
        """The pinned Nemotron lane for an agent: Ultra for the Evaluator, else Lightning."""
        return self.evaluator_lane if agent_name.strip().lower() == "evaluator" else self.default_lane

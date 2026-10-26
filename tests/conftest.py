"""Shared fixtures. Live smoke tests run only with LIVE_SMOKE=1 and the credentials in the
environment, which only the maintainer's `./run.sh` provides:

    ./run.sh env LIVE_SMOKE=1 uv run pytest -m live -s

They write their trace to `logs/trace-smoke-<date>.jsonl`. `./test.sh` never sets
LIVE_SMOKE, so the checkpoint stays offline."""

import os
from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.tracing import Tracer


@pytest.fixture(scope="session")
def live_settings():
    if os.environ.get("LIVE_SMOKE") != "1":
        pytest.skip("live smoke test: set LIVE_SMOKE=1 and run through ./run.sh")
    return Settings.from_env()


@pytest.fixture(scope="session")
def live_tracer(live_settings):
    run_id = "smoke-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Tracer(run_id, log_dir=live_settings.log_dir)

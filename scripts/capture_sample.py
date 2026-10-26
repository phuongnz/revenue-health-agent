#!/usr/bin/env python3
"""Capture sample mode's data: run the three examples live and keep what Zetaris and the
models answered.

    ./run.sh uv run python scripts/capture_sample.py [--fresh] [input_examples/example_N.json ...]

Each example runs the real agents against Zetaris and Nightshift, through clients that
also store every statement's answer (`data/sample/zetaris/`, keyed by the statement's
hash) and every completion (`data/sample/completions/`, keyed by the prompt's hash).
`data/sample/manifest.json` records the capture date, which sample mode reuses as the
run's "today" so its prompts match, and what each run read and asked. The live
responses go to `output_examples/example_N_output.json`, the traces and reports to
`logs/`. With `--fresh` the stored answers are cleared first.

Run by hand, through the credentials wrapper, when a prompt or a statement changes (a
changed one no longer matches, and sample mode says so). Never from a test or a loop:
it calls Zetaris, which calls SEC EDGAR, and the models.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.adapters.meterless import MeterlessClient  # noqa: E402
from app.adapters.sample_data import SAMPLE_DIR, RecordingChat, RecordingZetaris, SampleStore  # noqa: E402
from app.adapters.zetaris import ZetarisClient  # noqa: E402
from app.config import Settings  # noqa: E402
from app.orchestration.graph import Orchestrator  # noqa: E402
from app.schemas import RunRequest, utc_now  # noqa: E402
from app.tracing import Tracer  # noqa: E402

EXAMPLES = sorted((ROOT / "input_examples").glob("example_*.json"))
NOTICE = ("A small extract of SEC EDGAR XBRL company facts (public domain; SEC asks that it not be bulk-"
          "redistributed) and of the EDGAR submissions header, as read through Zetaris, plus the Nemotron "
          "completions of the same runs. Only what the three example requests read is kept.")


def capture(path: Path, store: SampleStore, settings: Settings, as_of: date) -> dict:
    request = RunRequest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    # the trace and the engine log append; an example's files hold its latest capture only
    for name in (f"trace-{request.run_id}.jsonl", f"markovian-{request.run_id}.jsonl"):
        (Path(settings.log_dir) / name).unlink(missing_ok=True)
    tracer = Tracer(request.run_id, settings.log_dir)
    zetaris = RecordingZetaris(ZetarisClient(settings, tracer), store, request.run_id)
    chat = RecordingChat(MeterlessClient(settings, tracer), store, request.run_id)
    try:
        response = Orchestrator(settings, tracer, zetaris, chat, as_of=as_of).run(request)
    finally:
        zetaris.close()
        chat.close()
    out = ROOT / "output_examples" / f"{path.stem}_output.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(response.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    print(f"{request.run_id}: {response.status} in {response.execution_time_seconds:.0f} s; "
          f"{len(set(zetaris.keys))} statements, {len(set(chat.keys))} completions -> {out.relative_to(ROOT)}",
          file=sys.stderr)
    return {"run_id": request.run_id, "input": str(path.relative_to(ROOT)), "status": response.status,
            "trace": str(tracer.path), "statements": sorted(set(zetaris.keys)), "completions": sorted(set(chat.keys))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("inputs", nargs="*", type=Path, help="example requests (default: all in input_examples/)")
    parser.add_argument("--fresh", action="store_true", help="clear the stored answers first")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    store = SampleStore(SAMPLE_DIR)
    manifest_path = SAMPLE_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() and not args.fresh else {}
    if args.fresh:
        for kind in ("zetaris", "completions"):
            shutil.rmtree(SAMPLE_DIR / kind, ignore_errors=True)
    as_of = date.fromisoformat(manifest["as_of"]) if manifest.get("as_of") else date.today()
    runs = {r["run_id"]: r for r in manifest.get("runs", [])}
    for path in [p.resolve() for p in args.inputs] or EXAMPLES:
        record = capture(path, store, settings, as_of)
        runs[record["run_id"]] = record
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {"as_of": as_of.isoformat(), "captured_at": utc_now(), "notice": NOTICE,
                "runs": [runs[k] for k in sorted(runs)]}
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

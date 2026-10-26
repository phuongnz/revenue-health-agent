"""Entry point.

    python run.py                                                 serve POST /run on 0.0.0.0:$PORT (default 8000)
    python run.py --input request.json [--output response.json]   one run, the response JSON on stdout last

The trace is mirrored to stdout as JSON lines while the run goes, as the submission
contract asks, and written to `logs/trace-<run_id>.jsonl`; the response follows as one
pretty-printed JSON document, and `--output` also writes it to a file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from app.config import ConfigError, Settings
from app.orchestration.graph import run_request
from app.schemas import RunRequest
from app.server import serve


def load_request(path: str) -> RunRequest:
    return RunRequest.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Revenue series health checker")
    parser.add_argument("--input", help="a request JSON file: {run_id, input: {query, ...}, options}")
    parser.add_argument("--output", help="also write the response JSON here")
    parser.add_argument("--host", default="0.0.0.0", help="server mode: the address to bind (default 0.0.0.0)")
    parser.add_argument("--port", type=int, help="server mode: the port (default $PORT, else 8000)")
    args = parser.parse_args(argv)
    if not args.input:
        try:
            settings = Settings.from_env()
        except ConfigError as e:
            print(json.dumps({"status": "failed", "error": {"type": "configuration", "message": str(e),
                                                            "recoverable": False}}, indent=2))
            return 2
        serve(settings, args.host, args.port)
        return 0
    try:
        settings = Settings.from_env()
        request = load_request(args.input)
    except (OSError, ValueError, ValidationError, ConfigError) as e:
        error = {"status": "failed", "error": {"type": "invalid_input", "message": str(e), "recoverable": True}}
        print(json.dumps(error, indent=2), file=sys.stdout)
        return 2
    response = run_request(request, settings)
    text = json.dumps(response.model_dump(mode="json"), indent=2, ensure_ascii=False)
    print(text)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    return 0 if response.status != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())

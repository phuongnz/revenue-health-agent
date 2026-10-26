#!/usr/bin/env bash
# The checkpoint: what CI runs, in one command. Every phase ends with this green, and CI
# calls this script rather than repeating its steps, so there is one definition of green.
#
# 1. No token-looking string in any tracked file. This repository is public and the
#    sponsor keys live only in the environment; a hit here is a blocker, not a warning.
# 2. The test suite. Nothing in it touches the network, Zetaris or Nightshift.
set -eu
cd "$(dirname "$0")"

if git ls-files -z | xargs -0 grep -nIE \
    -e 'Bearer [A-Za-z0-9._-]{20,}' \
    -e 'nvapi-[A-Za-z0-9_-]{10,}' \
    -e 'ghp_[A-Za-z0-9]{20,}' \
    -e 'AKIA[0-9A-Z]{16}' \
    -e 'BEGIN [A-Z ]*PRIVATE KEY' \
    -e 'sk-[A-Za-z0-9]{20,}' \
    -e 'eyJ[A-Za-z0-9_-]{30,}\.' \
    -- 2>/dev/null; then
  echo "test.sh: a tracked file holds a token-looking string (see above)" >&2
  exit 1
fi

if command -v uv >/dev/null 2>&1; then
  uv run --quiet pytest -q
else
  python3 -m pytest -q
fi

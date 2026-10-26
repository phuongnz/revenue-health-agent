#!/usr/bin/env bash
# The checkpoint: what CI runs, in one command. Every phase ends with this green, and CI
# calls this script rather than repeating its steps, so there is one definition of green.
#
# 1. No token-looking string in any tracked file, and none of the real credentials this
#    project uses, when they are in the environment (the maintainer's wrapper puts them
#    there; their values are never printed). This repository is public: a hit is a
#    blocker, not a warning.
# 2. The test suite. Nothing in it touches the network, Zetaris or Nightshift.
set -eu
cd "$(dirname "$0")"

# git grep runs in one process, so its exit status is the whole answer: 0 found, 1 none.
if git grep -nIE \
    -e 'Bearer [A-Za-z0-9._-]{20,}' \
    -e 'nvapi-[A-Za-z0-9_-]{10,}' \
    -e 'ghp_[A-Za-z0-9]{20,}' \
    -e 'AKIA[0-9A-Z]{16}' \
    -e 'BEGIN [A-Z ]*PRIVATE KEY' \
    -e 'sk-[A-Za-z0-9]{20,}' \
    -e 'eyJ[A-Za-z0-9_-]{30,}\.' \
    -- . ; then
  echo "test.sh: a tracked file holds a token-looking string (see above)" >&2
  exit 1
fi

for var in ZETARIS_API_KEY METERLESS_RUNTIME_TOKEN; do
  value="${!var:-}"
  if [ "${#value}" -ge 8 ] && git grep -qF -- "$value" -- . ; then
    echo "test.sh: the value of $var appears in a tracked file" >&2
    exit 1
  fi
done

if command -v uv >/dev/null 2>&1; then
  uv run --quiet pytest -q
else
  python3 -m pytest -q
fi

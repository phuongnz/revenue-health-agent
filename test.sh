#!/usr/bin/env bash
# The checkpoint: what CI runs, in one command. Every phase ends with this green, and CI
# calls this script rather than repeating its steps, so there is one definition of green.
# Wire the project's own checks here (lint, types, tests); until then it fails on purpose.
set -eu
cd "$(dirname "$0")"

echo "test.sh: nothing wired yet; add the project's checks here" >&2
exit 1

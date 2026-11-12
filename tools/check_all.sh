#!/bin/sh
# The full gate for the package refactor: ./check (parse, --help, a hook call, tests/test_orch.py) plus the tests the split adds
# (section 7, Step 0). `./check` stays the release gate install runs; this is the wider net a developer runs before committing.
set -e
cd "$(dirname "$0")/.."
./check
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -q tests.test_templates tests.test_state

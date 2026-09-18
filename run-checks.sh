#!/usr/bin/env bash
# Every check this repo has, in the order that fails fastest.
#
# Run it before pushing and in CI -- the same script both places, so a
# green terminal and a green pipeline mean the same thing. It needs no
# dependency beyond what the agent already ships on (Python 3.10+ and the
# three packages in requirements.txt); there is no linter config to drift.
set -uo pipefail

cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
failed=0

step() {
    local name="$1"; shift
    printf '\n=== %s ===\n' "$name"
    if "$@"; then
        printf '%s: OK\n' "$name"
    else
        printf '%s: FAILED\n' "$name"
        failed=1
    fi
}

# 1. Everything must at least import and compile. A syntax error found
#    here costs a second; found by a user it costs the session.
step "compile" "$PY" -m compileall -q fullagent tests

# 2. Each module's own self-test, run as a subprocess so one module's
#    global state cannot mask another's failure.
step "module self-tests" "$PY" run_selftests.py

# 3. The cross-module suites.
step "test suite" "$PY" -m unittest discover -s tests -p 'test_*.py' -b

# 4. The contract layer's own invariants, stated loudly because a broken
#    schema is invisible until a model sends the wrong argument.
step "tool contracts" "$PY" -m fullagent.toolcontract
step "dispatch core" "$PY" -m fullagent.dispatch
step "orchestrator" "$PY" -m fullagent.orchestrator

printf '\n'
if [ "$failed" -eq 0 ]; then
    printf 'ALL CHECKS PASSED\n'
else
    printf 'CHECKS FAILED\n'
fi
exit "$failed"

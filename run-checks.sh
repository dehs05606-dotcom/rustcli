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

# 4. Contract drift: the registry, the lock file, the docs and the tests
#    must still agree. This is the check that catches a tool added in one
#    place and forgotten in three.
step "contract drift" "$PY" -m fullagent.contractmanifest --check

# 5. The tool reference is generated from the registry. If the two
#    disagree, the docs are wrong, and confidently wrong docs are worse
#    than none.
step "generated docs" "$PY" -m fullagent.introspect --check-docs

# 6. The proof layer: every module's stated pre/postconditions and
#    state-machine closures, checked rather than asserted in prose. This
#    is the check that found three real defects on its first run.
step "invariants" "$PY" -m fullagent.invariants --check

# 7. Contract evolution: a breaking change to a tool contract needs a
#    version bump and a migration, or it does not merge.
step "contract governance" "$PY" -m fullagent.governance --gate

# 8. The regression gate: eleven governed surfaces are fingerprinted —
#    the prompt, clauses, policy pipeline and its metamodel, recovery
#    playbooks, envelopes, verification floors, the contract lock, the
#    failure catalogue and the threat model — and the two-armed
#    adherence benchmark has to still hold AND still catch. A rule change
#    with no benchmark behind it fails here.
step "regression gate" "$PY" -m fullagent.regressiongate --check

# 9. Behavioural envelopes: every tool declares what it may do, and
#    every declaration agrees with its contract's idempotency.
step "behavioural envelopes" "$PY" -m fullagent.envelopes --check

# 10. The chaos runbooks: every failure class injected deterministically,
#     proving each recovery playbook still does what it says. A failing
#     runbook is a defect, never a flake — there is no retry here.
step "recovery runbooks" "$PY" -m fullagent.runbook --check

# 11. The policy metamodel: the permission pipeline modelled as data and
#     its meta-properties proved by enumeration — deny-dominance, no
#     silent widen, reorder safety. A pipeline change ships with this.
step "policy metamodel" "$PY" -m fullagent.policymeta --check

# 12. The counterfactual failure catalogue: every typed refusal the
#     platform can emit is provoked on purpose. A class with no scenario
#     is a defect, not a warning.
step "failure catalogue" "$PY" -m fullagent.faultcatalogue --check

# 13. The assurance case: every compliance claim resolved to sealed
#     evidence or to an assumption stated in the open. A claim with
#     nothing under it fails here.
step "assurance case" "$PY" -m fullagent.assurance --check

# 14. The threat model: the documented open risks, measured and compared
#     with the committed posture. A change in either direction needs a
#     named person to re-record it.
step "threat model" "$PY" -m fullagent.threatpins --check

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

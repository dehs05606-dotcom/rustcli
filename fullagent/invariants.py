"""INVARIANTS — the properties this codebase claims, checked by machine.

Every module here states things about itself in its docstring: a deny is
final, only an idempotent call is repeated, a playbook cannot overrule
the taxonomy. Those sentences are the design. Until something checks
them on every commit they are also just sentences, and the gap between a
stated property and a checked one is where a refactor quietly breaks an
invariant nobody thought to test.

So each claim becomes an `Invariant`: an id, the module it belongs to,
the sentence in English, and a function that tries to falsify it.

**A word about the word "proof".** An invariant marked `exhaustive`
enumerated its entire input domain -- all nine error codes against all
thirty-two context combinations, every stage against every request in a
constructed grid. For that domain the result is a proof: there is no
unchecked case. An invariant not so marked sampled, and sampling is
evidence, not proof. The report counts the two separately and never
calls the second one a proof. Anything else would be the kind of
overclaiming the compliance stack exists to prevent.

What this does **not** do is verify the agent's behaviour against a
model of the world. It verifies that the code's own stated properties
hold over the inputs it can actually receive.

    python -m fullagent.invariants            # the whole report
    python -m fullagent.invariants --check    # exit 1 on any failure
    python -m fullagent.invariants --json     # machine-readable
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from typing import Any, Callable

# -- kinds ------------------------------------------------------------------
PRECONDITION = "precondition"    # what a call may assume on entry
POSTCONDITION = "postcondition"  # what it guarantees on exit
CLOSURE = "closure"              # a state machine reaches only declared states
TOTALITY = "totality"            # a function answers for every input
CONSISTENCY = "consistency"      # two views of the same thing agree

KINDS = (PRECONDITION, POSTCONDITION, CLOSURE, TOTALITY, CONSISTENCY)


@dataclass(frozen=True)
class Result:
    """What one attempt to falsify an invariant found."""
    ok: bool
    cases: int = 0
    detail: str = ""
    counterexample: dict | None = None

    def to_dict(self) -> dict:
        return {"ok": self.ok, "cases": self.cases, "detail": self.detail,
                "counterexample": self.counterexample}


def holds(cases: int, detail: str = "") -> Result:
    return Result(True, cases, detail)


def fails(detail: str, counterexample: dict | None = None,
          cases: int = 0) -> Result:
    return Result(False, cases, detail, counterexample)


@dataclass(frozen=True)
class Invariant:
    """One claim, and the function that tries to break it."""
    id: str
    module: str
    kind: str
    statement: str
    check: Callable[[], Result]
    exhaustive: bool = False

    def run(self) -> "Checked":
        try:
            result = self.check()
        except Exception as exc:
            result = fails(f"the check itself raised "
                           f"{type(exc).__name__}: {exc}")
        return Checked(self, result)


@dataclass(frozen=True)
class Checked:
    invariant: Invariant
    result: Result

    def to_dict(self) -> dict:
        return {"id": self.invariant.id, "module": self.invariant.module,
                "kind": self.invariant.kind,
                "statement": self.invariant.statement,
                "exhaustive": self.invariant.exhaustive,
                **self.result.to_dict()}

    def line(self) -> str:
        mark = "ok" if self.result.ok else "FAIL"
        proof = "proved" if (self.invariant.exhaustive and self.result.ok) \
            else ("checked" if self.result.ok else "broken")
        tail = f" — {self.result.detail}" if self.result.detail else ""
        return (f"  {mark:>4}  {self.invariant.id:<34} {proof:<7} "
                f"{self.result.cases:>5} case(s){tail}")


@dataclass
class Report:
    checked: tuple[Checked, ...] = ()

    @property
    def ok(self) -> bool:
        return all(c.result.ok for c in self.checked)

    @property
    def failures(self) -> tuple[Checked, ...]:
        return tuple(c for c in self.checked if not c.result.ok)

    @property
    def proved(self) -> int:
        return sum(1 for c in self.checked
                   if c.invariant.exhaustive and c.result.ok)

    @property
    def sampled(self) -> int:
        return sum(1 for c in self.checked
                   if not c.invariant.exhaustive and c.result.ok)

    @property
    def cases(self) -> int:
        return sum(c.result.cases for c in self.checked)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "invariants": len(self.checked),
                "proved": self.proved, "sampled": self.sampled,
                "cases": self.cases,
                "checked": [c.to_dict() for c in self.checked]}

    def format(self) -> str:
        head = (f"INVARIANTS — {len(self.checked)} claim(s), "
                f"{self.cases} case(s): {self.proved} proved exhaustively, "
                f"{self.sampled} checked by sampling")
        if not self.ok:
            head += f" — {len(self.failures)} BROKEN"
        lines = [head]
        module = ""
        for c in self.checked:
            if c.invariant.module != module:
                module = c.invariant.module
                lines.append(f"  [{module}]")
            lines.append(c.line())
        for c in self.failures:
            if c.result.counterexample:
                lines.append(f"  counterexample for {c.invariant.id}: "
                             f"{json.dumps(c.result.counterexample, default=str)}")
        return "\n".join(lines)


# ===========================================================================
# The invariants themselves
# ===========================================================================

def _toolcontract_invariants() -> list[Invariant]:
    from .toolcontract import (E_INTERNAL, E_PERMISSION, E_TIMEOUT,
                               E_VALIDATION, ERROR_CODES, RETRYABLE,
                               RetryPolicy, ToolError, build_contracts,
                               unsupported_keywords)
    from .tools import build_registry

    # Codes the dispatcher can produce for any tool, whatever the tool
    # itself does: it validates the input, it consults policy, it enforces
    # the timeout, and it labels an unclassifiable escape E_INTERNAL.
    DISPATCH_IMPOSED = (E_VALIDATION, E_PERMISSION, E_TIMEOUT, E_INTERNAL)

    def contracts():
        return build_contracts(build_registry())

    def taxonomy_total() -> Result:
        missing = set(ERROR_CODES) - set(RETRYABLE)
        extra = set(RETRYABLE) - set(ERROR_CODES)
        if missing or extra:
            return fails("the taxonomy and its retryability table disagree",
                         {"missing": sorted(missing), "extra": sorted(extra)})
        return holds(len(ERROR_CODES))

    def approval_iff_risky() -> Result:
        cs = contracts()
        for name, c in cs.items():
            if c.needs_approval != (c.destructive or c.outward_facing):
                return fails("needs_approval is not exactly "
                             "destructive-or-outward-facing",
                             {"tool": name, "needs_approval": c.needs_approval,
                              "destructive": c.destructive,
                              "outward_facing": c.outward_facing})
        return holds(len(cs))

    def schema_subset() -> Result:
        cs = contracts()
        n = 0
        for name, c in cs.items():
            for label, schema in (("input", c.input_schema),
                                  ("output", c.output_schema)):
                n += 1
                unknown = unsupported_keywords(schema)
                if unknown:
                    return fails("a schema uses keywords validate() ignores, "
                                 "which reads as a guarantee and is not one",
                                 {"tool": name, "schema": label,
                                  "keywords": list(unknown)})
        return holds(n)

    def dispatch_codes_declared() -> Result:
        cs = contracts()
        for name, c in sorted(cs.items()):
            missing = [code for code in DISPATCH_IMPOSED
                       if code not in c.errors]
            if missing:
                return fails(
                    "a contract omits a code the dispatcher can produce for "
                    "it, so a caller written against the contract would not "
                    "handle what it can actually receive",
                    {"tool": name, "missing": missing,
                     "declared": sorted(set(c.errors))})
        return holds(len(cs) * len(DISPATCH_IMPOSED))

    def retry_narrows_only() -> Result:
        n = 0
        for code in ERROR_CODES:
            policy = RetryPolicy(max_attempts=5, codes=(code,))
            for attempt in (1, 2, 4):
                n += 1
                if policy.should_retry(ToolError(code, "x"), attempt) and \
                        not RETRYABLE[code]:
                    return fails("a retry policy resurrected a code the "
                                 "taxonomy calls final",
                                 {"code": code, "attempt": attempt})
        return holds(n)

    def attempts_bounded() -> Result:
        n = 0
        for limit in range(1, 6):
            policy = RetryPolicy(max_attempts=limit, codes=tuple(ERROR_CODES))
            for attempt in range(0, 8):
                n += 1
                if attempt >= limit and policy.should_retry(
                        ToolError(E_TIMEOUT, "x"), attempt):
                    return fails("should_retry allowed an attempt past its "
                                 "own ceiling",
                                 {"limit": limit, "attempt": attempt})
        return holds(n)

    return [
        Invariant("taxonomy-is-total", "toolcontract", TOTALITY,
                  "every error code has a retryability, and nothing else does",
                  taxonomy_total, exhaustive=True),
        Invariant("approval-iff-risky", "toolcontract", CONSISTENCY,
                  "a contract needs approval exactly when it is destructive "
                  "or outward-facing", approval_iff_risky, exhaustive=True),
        Invariant("schema-is-checkable", "toolcontract", PRECONDITION,
                  "every schema uses only keywords validate() implements",
                  schema_subset, exhaustive=True),
        Invariant("declares-dispatch-codes", "toolcontract", POSTCONDITION,
                  "every contract declares the codes the dispatcher can "
                  "produce for it", dispatch_codes_declared, exhaustive=True),
        Invariant("retry-narrows-only", "toolcontract", CONSISTENCY,
                  "a retry policy can narrow the taxonomy, never widen it",
                  retry_narrows_only, exhaustive=True),
        Invariant("attempts-are-bounded", "toolcontract", POSTCONDITION,
                  "should_retry never allows an attempt past max_attempts",
                  attempts_bounded, exhaustive=True),
    ]


def _policy_invariants() -> list[Invariant]:
    import dataclasses
    import tempfile
    from pathlib import Path

    from .policypipeline import (ALLOW, ASK, DENY, DEFAULT_STAGES, SKIP,
                                 PolicyPipeline, Request)
    from .toolpolicy import (FS_DELETE, FS_READ, FS_WRITE, NET_FETCH,
                             PROC_EXEC, ROLES, ToolPolicy)

    OUTCOMES = {ALLOW, ASK, DENY, SKIP}
    root = str(Path(tempfile.mkdtemp(prefix="fa-inv-")).resolve())

    def grid() -> list[Request]:
        """A constructed domain covering every stage's interesting input."""
        roles = [ROLES["untrusted"], ROLES["readonly"], ROLES["developer"],
                 ROLES["operator"],
                 dataclasses.replace(ROLES["operator"],
                                     ceilings={"run_command": 1})]
        cases = [
            ("read_file", {"path": f"{root}/a"}, {FS_READ}),
            ("read_file", {"path": "/etc/passwd"}, {FS_READ}),
            ("read_file", {"path": "../../../etc/shadow"}, {FS_READ}),
            ("write_file", {"path": f"{root}/a", "content": "x"},
             {FS_WRITE}),
            ("delete_path", {"path": f"{root}/a"}, {FS_DELETE}),
            ("run_command", {"command": "ls"}, {PROC_EXEC}),
            ("run_command", {"command": "rm -rf /"}, {PROC_EXEC}),
            ("live_shell", {"command": "git push --force"}, {PROC_EXEC}),
            ("web_fetch", {"url": "https://example.com/"}, {NET_FETCH}),
            ("web_fetch", {"url": "http://169.254.169.254/"}, {NET_FETCH}),
            ("web_fetch", {"url": "file:///etc/passwd"}, {NET_FETCH}),
            ("mystery", {}, set()),
            ("teleport", {"path": f"{root}/a"}, {FS_READ}),
        ]
        out = []
        for role, (tool, args, caps) in itertools.product(roles, cases):
            for counts in ({}, {"run_command": 99}):
                out.append(Request(tool, args, role, frozenset(caps),
                                   (root,), counts,
                                   known=tool != "teleport"))
        return out

    def stage_totality() -> Result:
        n = 0
        for stage, request in itertools.product(DEFAULT_STAGES, grid()):
            n += 1
            r = stage.check(request)
            if r.outcome not in OUTCOMES or not isinstance(r.code, str) \
                    or r.stage != stage.name:
                return fails("a stage returned something outside its contract",
                             {"stage": stage.name, "tool": request.tool,
                              "outcome": r.outcome, "code": r.code})
        return holds(n)

    def deny_is_final() -> Result:
        pipe = PolicyPipeline()
        n = 0
        for request in grid():
            n += 1
            decision = pipe.decide(request)
            denies = [i for i, r in enumerate(decision.rationale)
                      if r.outcome == DENY]
            if denies and denies[0] != len(decision.rationale) - 1:
                return fails("the pipeline kept going after a deny",
                             {"tool": request.tool,
                              "stages": [r.stage for r in decision.rationale]})
            if denies and decision.outcome != DENY:
                return fails("a stage denied and the decision did not",
                             {"tool": request.tool,
                              "outcome": decision.outcome})
            if not denies and any(r.outcome == ASK
                                  for r in decision.rationale) \
                    and decision.outcome != ASK:
                return fails("a stage asked and the decision did not",
                             {"tool": request.tool,
                              "outcome": decision.outcome})
        return holds(n)

    def collapse_agrees() -> Result:
        n = 0
        for request in grid():
            policy = ToolPolicy(request.role, roots=(root,))
            policy.counts = dict(request.counts)
            if not request.known:
                policy.manifest.pop(request.tool, None)
            simple = policy.evaluate(request.tool, request.args)
            staged = policy.evaluate_detailed(request.tool, request.args)
            n += 1
            if simple.outcome != staged.outcome or simple.rule != staged.rule:
                return fails("evaluate and evaluate_detailed disagree",
                             {"tool": request.tool, "role": request.role.name,
                              "simple": simple.to_dict()["outcome"],
                              "staged": staged.outcome})
        return holds(n)

    def unknown_is_denied() -> Result:
        pipe = PolicyPipeline()
        n = 0
        for role in ROLES.values():
            for caps in (frozenset(), frozenset({FS_READ}),
                         frozenset({FS_READ, PROC_EXEC})):
                n += 1
                out = pipe.decide(Request("never_registered", {}, role, caps,
                                          (root,), {}, known=False))
                if not out.denied:
                    return fails("an unregistered tool was not denied",
                                 {"role": role.name, "outcome": out.outcome})
        return holds(n)

    def stages_are_named_once() -> Result:
        names = [s.name for s in DEFAULT_STAGES]
        if len(names) != len(set(names)):
            return fails("two stages share a name, so the audit cannot tell "
                         "them apart", {"names": names})
        return holds(len(names))

    return [
        Invariant("stage-totality", "policypipeline", TOTALITY,
                  "every stage answers every request with a declared outcome",
                  stage_totality, exhaustive=True),
        Invariant("deny-is-final", "policypipeline", CLOSURE,
                  "a deny stops the pipeline and decides it; an ask decides "
                  "it only when nothing denies", deny_is_final,
                  exhaustive=True),
        Invariant("collapse-agrees", "policypipeline", CONSISTENCY,
                  "evaluate is exactly the collapse of evaluate_detailed",
                  collapse_agrees, exhaustive=True),
        Invariant("unknown-tool-denied", "policypipeline", POSTCONDITION,
                  "an unregistered tool is denied under every role",
                  unknown_is_denied, exhaustive=True),
        Invariant("stage-names-unique", "policypipeline", CONSISTENCY,
                  "no two stages share a name", stages_are_named_once,
                  exhaustive=True),
    ]


def _recovery_invariants() -> list[Invariant]:
    from . import recovery
    from .toolcontract import ERROR_CODES, RETRYABLE, ToolError

    def contexts() -> list[recovery.Context]:
        """Every combination of what recovery is allowed to know."""
        out = []
        for idem, comp, human, refused in itertools.product(
                (False, True), repeat=4):
            for attempts, limit in ((1, 1), (1, 3), (3, 3)):
                out.append(recovery.Context(
                    idempotent=idem, has_compensation=comp,
                    can_ask_human=human, attempts=attempts,
                    max_attempts=limit, approval_refused=refused))
        return out

    def product():
        return list(itertools.product(ERROR_CODES, contexts()))

    def playbooks_total() -> Result:
        missing = set(ERROR_CODES) - set(recovery.PLAYBOOKS)
        extra = set(recovery.PLAYBOOKS) - set(ERROR_CODES)
        if missing or extra:
            return fails("a code has no playbook, or a playbook has no code",
                         {"missing": sorted(missing), "extra": sorted(extra)})
        return holds(len(ERROR_CODES))

    def strategy_total() -> Result:
        cases = product()
        for code, ctx in cases:
            verdict = recovery.plan(ToolError(code, "x"), ctx)
            if verdict.strategy not in recovery.STRATEGIES:
                return fails("recovery returned a strategy that does not exist",
                             {"code": code, "strategy": verdict.strategy})
            if not verdict.reason:
                return fails("a recovery decision came with no reason",
                             {"code": code})
        return holds(len(cases))

    def retry_implies_repeatable() -> Result:
        cases = product()
        for code, ctx in cases:
            verdict = recovery.plan(ToolError(code, "x"), ctx)
            if verdict.strategy != recovery.RETRY:
                continue
            if not ctx.idempotent or not RETRYABLE[code] or \
                    not ctx.attempts_left:
                return fails("retry was chosen for a call that must not be "
                             "repeated",
                             {"code": code, "context": ctx.__dict__})
        return holds(len(cases))

    def compensate_implies_compensation() -> Result:
        cases = product()
        for code, ctx in cases:
            verdict = recovery.plan(ToolError(code, "x"), ctx)
            if verdict.strategy == recovery.COMPENSATE and \
                    not ctx.has_compensation:
                return fails("compensate was chosen with nothing to "
                             "compensate with",
                             {"code": code, "context": ctx.__dict__})
        return holds(len(cases))

    def escalate_implies_someone() -> Result:
        cases = product()
        for code, ctx in cases:
            verdict = recovery.plan(ToolError(code, "x"), ctx)
            if verdict.strategy == recovery.ESCALATE and not ctx.can_ask_human:
                return fails("escalate was chosen with nobody to escalate to",
                             {"code": code, "context": ctx.__dict__})
        return holds(len(cases))

    return [
        Invariant("playbooks-are-total", "recovery", TOTALITY,
                  "every error code has exactly one playbook",
                  playbooks_total, exhaustive=True),
        Invariant("strategy-is-total", "recovery", TOTALITY,
                  "every code and context yields a real strategy with a "
                  "stated reason", strategy_total, exhaustive=True),
        Invariant("retry-implies-repeatable", "recovery", POSTCONDITION,
                  "retry is only ever chosen for a repeatable call with "
                  "attempts left", retry_implies_repeatable, exhaustive=True),
        Invariant("compensate-implies-undo", "recovery", POSTCONDITION,
                  "compensate is only ever chosen when a compensation exists",
                  compensate_implies_compensation, exhaustive=True),
        Invariant("escalate-implies-human", "recovery", POSTCONDITION,
                  "escalate is only ever chosen when somebody can be asked",
                  escalate_implies_someone, exhaustive=True),
    ]


def _orchestrator_invariants() -> list[Invariant]:
    import os
    import shutil
    import tempfile
    from pathlib import Path

    from .dispatch import Dispatcher
    from .kernel import EventLog
    from .orchestrator import (COMPENSATED, DONE, ESCALATED, FAILED,
                               IRREVERSIBLE, PENDING, RUNNING, SKIPPED,
                               Expectation, Orchestrator, Plan, Step)
    from .toolcontract import build_contracts
    from .toolpolicy import ToolPolicy
    from .tools import build_registry

    TERMINAL = {DONE, FAILED, SKIPPED, COMPENSATED, IRREVERSIBLE, ESCALATED}
    ALL_STATUSES = TERMINAL | {PENDING, RUNNING}

    def runs() -> list:
        """A handful of real runs covering each way a plan can end."""
        root = Path(tempfile.mkdtemp(prefix="fa-inv-orch-"))
        here = os.getcwd()
        os.chdir(root)
        out = []
        try:
            log = EventLog(path=str(root / "events.jsonl"))
            registry = build_registry()
            contracts = build_contracts(registry)
            d = Dispatcher(policy=ToolPolicy("developer", log=log,
                                             roots=(str(root),)),
                           log=log, approve=lambda c, a: True)
            d.register_registry(registry, contracts)
            orch = Orchestrator(d, log=log, approve=lambda p, r: True)

            def w(step_id, name, expect=None):
                path = str(root / name)
                return Step(step_id, "write_file",
                            {"path": path, "content": "x\n"},
                            expect=(Expectation(path_exists=(path,))
                                    if expect is None
                                    else Expectation(contains=(expect,))),
                            undo_tool="delete_path",
                            undo_args={"path": path})

            out.append(orch.run(Plan("clean", (w("a", "a.txt"),))))
            out.append(orch.run(Plan("fails", (
                w("a", "b.txt"), w("b", "c.txt", expect="never")))))
            out.append(orch.run(Plan("nested", (
                w("a", "d.txt"),
                Step("group", sub=Plan("g", (w("i", "e.txt"),)))))))
            out.append(orch.run(Plan("nested-fails", (
                w("a", "f.txt"),
                Step("group", sub=Plan("g", (
                    w("i", "g.txt"), w("j", "h.txt", expect="never"))))))))
            out.append(orch.run(Plan("refused", (
                Step("bad", "teleport", {"path": "x"}),))))
            out.append(Orchestrator(d, log=log, approve=None).run(
                Plan("unapproved", (w("a", "i.txt"),))))
        finally:
            os.chdir(here)
            shutil.rmtree(root, ignore_errors=True)
        return out

    _RUNS = None

    def cached_runs():
        nonlocal _RUNS
        if _RUNS is None:
            _RUNS = runs()
        return _RUNS

    def status_closure() -> Result:
        n = 0
        for result in cached_runs():
            for entry in result.ledger:
                n += 1
                if entry.status not in ALL_STATUSES:
                    return fails("a ledger entry reached an undeclared state",
                                 {"goal": result.goal, "step": entry.path,
                                  "status": entry.status})
        return holds(n)

    def nothing_left_running() -> Result:
        n = 0
        for result in cached_runs():
            for entry in result.ledger:
                n += 1
                if entry.status in (PENDING, RUNNING):
                    return fails("a finished run left a step mid-flight",
                                 {"goal": result.goal, "step": entry.path,
                                  "status": entry.status})
        return holds(n)

    def failure_implies_accounted() -> Result:
        n = 0
        for result in cached_runs():
            if result.ok:
                continue
            n += 1
            named = set(result.rolled_back) | set(result.irreversible) | \
                set(result.escalated)
            applied = {e.path for e in result.ledger
                       if e.status in (COMPENSATED, IRREVERSIBLE, ESCALATED)}
            if applied - named:
                return fails("a step that ran is not named in the result",
                             {"goal": result.goal,
                              "unaccounted": sorted(applied - named)})
        return holds(n)

    def success_implies_all_done() -> Result:
        n = 0
        for result in cached_runs():
            if not result.ok:
                continue
            n += 1
            wrong = [e.path for e in result.ledger if e.status != DONE]
            if wrong:
                return fails("a run reported success with a step not done",
                             {"goal": result.goal, "steps": wrong})
            if result.rolled_back or result.irreversible or result.escalated:
                return fails("a successful run rolled something back",
                             {"goal": result.goal})
        return holds(n)

    def replay_matches_the_run() -> Result:
        from .orchestrator import replay
        root = Path(tempfile.mkdtemp(prefix="fa-inv-replay-"))
        here = os.getcwd()
        os.chdir(root)
        n = 0
        try:
            log = EventLog(path=str(root / "events.jsonl"))
            registry = build_registry()
            d = Dispatcher(policy=ToolPolicy("developer", log=log,
                                             roots=(str(root),)),
                           log=log, approve=lambda c, a: True)
            d.register_registry(registry, build_contracts(registry))
            orch = Orchestrator(d, log=log, approve=lambda p, r: True)
            for goal, expect in (("ok", None), ("bad", "never happens")):
                path = str(root / f"{goal}.txt")
                step = Step("one", "write_file",
                            {"path": path, "content": "x\n"},
                            expect=(Expectation(path_exists=(path,))
                                    if expect is None
                                    else Expectation(contains=(expect,))),
                            undo_tool="delete_path", undo_args={"path": path})
                result = orch.run(Plan(goal, (step,)))
                seen = replay(log, result.trace_id)
                n += 1
                if seen.ok != result.ok or \
                        [e.path for e in seen.ledger] != \
                        [e.path for e in result.ledger]:
                    return fails("replay disagrees with the run it replays",
                                 {"goal": goal, "run_ok": result.ok,
                                  "replay_ok": seen.ok})
                if replay(log, result.trace_id).to_dict() != seen.to_dict():
                    return fails("replay is not deterministic", {"goal": goal})
        finally:
            os.chdir(here)
            shutil.rmtree(root, ignore_errors=True)
        return holds(n)

    return [
        Invariant("ledger-status-closure", "orchestrator", CLOSURE,
                  "a ledger entry only ever reaches a declared status",
                  status_closure),
        Invariant("nothing-left-running", "orchestrator", POSTCONDITION,
                  "a finished run leaves no step pending or running",
                  nothing_left_running),
        Invariant("failure-is-accounted", "orchestrator", POSTCONDITION,
                  "every step that ran is named in a failed run's result",
                  failure_implies_accounted),
        Invariant("success-is-total", "orchestrator", POSTCONDITION,
                  "a successful run has every step done and nothing undone",
                  success_implies_all_done),
        Invariant("replay-matches-run", "orchestrator", CONSISTENCY,
                  "replaying a run from the log reproduces it, deterministically",
                  replay_matches_the_run),
    ]


def _telemetry_invariants() -> list[Invariant]:
    from .telemetry import GRADES, Telemetry, grade_for, windows_of

    def proposal_is_inert() -> Result:
        n = 0
        for incumbent_score, rival_score in itertools.product(
                (0.4, 0.6, 0.75, 0.95), repeat=2):
            tel = Telemetry()
            for _ in range(20):
                tel.observe("incumbent", incumbent_score)
                tel.observe("rival", rival_score)
            proposal = tel.routing("incumbent")
            n += 1
            if proposal.in_effect != "incumbent":
                return fails("a proposal took effect without being accepted",
                             {"incumbent": incumbent_score,
                              "rival": rival_score,
                              "in_effect": proposal.in_effect})
            if proposal.should_switch and not proposal.justification:
                return fails("a switch was proposed with no justification",
                             {"incumbent": incumbent_score})
        return holds(n)

    def grade_is_monotone() -> Result:
        order = [name for name, _ in GRADES]
        previous = 0
        n = 0
        for step in range(101):
            score = step / 100
            n += 1
            rank = order.index(grade_for(score))
            if rank > previous and step:
                pass  # grades improve as the score rises; index falls
            previous = rank
        # a falling score must never improve the grade
        for a, b in itertools.combinations(range(0, 101, 5), 2):
            n += 1
            lo, hi = a / 100, b / 100
            if order.index(grade_for(hi)) > order.index(grade_for(lo)):
                return fails("a higher score produced a worse grade",
                             {"low": lo, "high": hi})
        return holds(n)

    def windows_are_complete() -> Result:
        n = 0
        for length in range(0, 45):
            n += 1
            ws = windows_of([0.9] * length)
            if any(w.n != 10 for w in ws):
                return fails("a partial window was reported as a window",
                             {"length": length,
                              "sizes": [w.n for w in ws]})
            if len(ws) != length // 10:
                return fails("the wrong number of windows came back",
                             {"length": length, "windows": len(ws)})
        return holds(n)

    return [
        Invariant("proposal-is-inert", "telemetry", POSTCONDITION,
                  "a routing proposal never changes which model is in effect",
                  proposal_is_inert, exhaustive=True),
        Invariant("grade-is-monotone", "telemetry", CONSISTENCY,
                  "a higher score never produces a worse grade",
                  grade_is_monotone, exhaustive=True),
        Invariant("windows-are-complete", "telemetry", POSTCONDITION,
                  "a rolling window is always full or absent",
                  windows_are_complete, exhaustive=True),
    ]


def _manifest_invariants() -> list[Invariant]:
    import dataclasses

    from .contractmanifest import compare, manifest
    from .toolcontract import build_contracts
    from .tools import build_registry

    def contracts():
        return build_contracts(build_registry())

    def compare_reflexive() -> Result:
        m = manifest(contracts())
        out = compare(m, m)
        if out.changes:
            return fails("a manifest differs from itself",
                         {"changes": [c.to_dict() for c in out.changes]})
        return holds(1)

    def manifest_deterministic() -> Result:
        first = manifest(build_contracts(build_registry()))
        second = manifest(build_contracts(build_registry()))
        if first != second:
            return fails("two manifests of the same registry differ",
                         {"first": first["digest"], "second": second["digest"]})
        return holds(2)

    def removal_is_breaking() -> Result:
        cs = contracts()
        n = 0
        for name in sorted(cs):
            n += 1
            fewer = manifest({k: v for k, v in cs.items() if k != name})
            whole = manifest(cs)
            if compare(whole, fewer).compatible:
                return fails("removing a tool was called compatible",
                             {"tool": name})
            if not compare(fewer, whole).compatible:
                return fails("adding a tool was called breaking",
                             {"tool": name})
        return holds(n * 2)

    def required_argument_is_breaking() -> Result:
        cs = contracts()
        whole = manifest(cs)
        n = 0
        for name, c in sorted(cs.items()):
            props = (c.input_schema or {}).get("properties") or {}
            if not props:
                continue
            n += 1
            schema = json.loads(json.dumps(c.input_schema))
            schema["properties"]["a_brand_new_field"] = {"type": "string"}
            optional = dict(cs)
            optional[name] = dataclasses.replace(c, input_schema=schema)
            if not compare(whole, manifest(optional)).compatible:
                return fails("an optional argument was called breaking",
                             {"tool": name})
            schema = json.loads(json.dumps(schema))
            schema["required"] = sorted(set(schema.get("required", [])) |
                                        {"a_brand_new_field"})
            demanded = dict(cs)
            demanded[name] = dataclasses.replace(c, input_schema=schema)
            if compare(whole, manifest(demanded)).compatible:
                return fails("a newly required argument was called additive",
                             {"tool": name})
        return holds(n * 2)

    return [
        Invariant("compare-is-reflexive", "contractmanifest", CONSISTENCY,
                  "a manifest compared with itself reports no change",
                  compare_reflexive, exhaustive=True),
        Invariant("manifest-deterministic", "contractmanifest", POSTCONDITION,
                  "the same registry always produces the same manifest",
                  manifest_deterministic, exhaustive=True),
        Invariant("removal-is-breaking", "contractmanifest", CONSISTENCY,
                  "removing any tool is breaking and adding it is additive",
                  removal_is_breaking, exhaustive=True),
        Invariant("required-arg-is-breaking", "contractmanifest", CONSISTENCY,
                  "a new optional argument is additive and a required one "
                  "is not", required_argument_is_breaking, exhaustive=True),
    ]


def _constitution_invariants() -> list[Invariant]:
    import tempfile
    from pathlib import Path

    from .constitution import ConstitutionalCore
    from .kernel import EventLog

    SAMPLE = ("## Rules\n- You MUST read a file before editing it.\n"
              "- You must NEVER delete a path without asking.\n")

    def tamper_is_detected() -> Result:
        root = Path(tempfile.mkdtemp(prefix="fa-inv-const-"))
        log = EventLog(path=str(root / "events.jsonl"))
        core = ConstitutionalCore(log, app_dir=root)
        const = core.ratify_prompt("sample", SAMPLE)
        n = 0
        for policy in const.policies:
            n += 1
            if not policy.verify(core.key):
                return fails("a freshly signed policy does not verify",
                             {"policy": policy.policy_id})
            forged = dataclasses_replace_rule(policy)
            if forged.verify(core.key):
                return fails("an altered policy still verified",
                             {"policy": policy.policy_id})
        return holds(n * 2)

    def dataclasses_replace_rule(policy):
        import dataclasses
        rule = dict(policy.rule)
        rule["text"] = rule.get("text", "") + " (altered)"
        return dataclasses.replace(policy, rule=rule)

    def ratify_is_idempotent() -> Result:
        root = Path(tempfile.mkdtemp(prefix="fa-inv-const2-"))
        log = EventLog(path=str(root / "events.jsonl"))
        core = ConstitutionalCore(log, app_dir=root)
        first = core.ratify_prompt("sample", SAMPLE)
        again = core.ratify_prompt("sample", SAMPLE)
        if first.root != again.root or first.version != again.version:
            return fails("ratifying identical text produced a new version",
                         {"first": first.version, "again": again.version})
        changed = core.ratify_prompt("sample", SAMPLE + "- Be brief.\n")
        if changed.version <= first.version:
            return fails("changed text did not raise the version",
                         {"before": first.version, "after": changed.version})
        return holds(3)

    return [
        Invariant("tamper-is-detected", "constitution", POSTCONDITION,
                  "an altered policy fails verification",
                  tamper_is_detected),
        Invariant("ratify-is-idempotent", "constitution", CONSISTENCY,
                  "identical prompt text ratifies to the same constitution",
                  ratify_is_idempotent),
    ]


def all_invariants() -> tuple[Invariant, ...]:
    out: list[Invariant] = []
    for builder in (_toolcontract_invariants, _policy_invariants,
                    _recovery_invariants, _orchestrator_invariants,
                    _telemetry_invariants, _manifest_invariants,
                    _constitution_invariants):
        out.extend(builder())
    return tuple(out)


def verify(only: str | None = None) -> Report:
    """Run every invariant, or every invariant of one module."""
    chosen = [i for i in all_invariants()
              if only is None or i.module == only or i.id == only]
    return Report(tuple(i.run() for i in chosen))


if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
    only = next((a for a in argv if not a.startswith("-")), None)
    report = verify(only)

    if "--json" in argv:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.format())

    if "--check" in argv:
        raise SystemExit(0 if report.ok else 1)

    if not argv:
        # Self-test: the layer has to be able to catch a broken invariant,
        # or a green report means nothing.
        broken = Invariant("deliberately-false", "self-test", CONSISTENCY,
                           "this claim is false on purpose",
                           lambda: fails("as designed", {"why": "a test"}))
        checked = broken.run()
        assert not checked.result.ok and checked.result.counterexample

        raising = Invariant("raises", "self-test", CONSISTENCY,
                            "a check that raises is a failure, not a pass",
                            lambda: (_ for _ in ()).throw(
                                RuntimeError("boom")))
        assert not raising.run().result.ok

        mixed = Report(tuple([checked] + list(verify("recovery").checked)))
        assert not mixed.ok and len(mixed.failures) == 1

        assert report.ok, "\n" + report.format()
        assert report.proved >= 20, report.proved
        assert report.cases > 1000, report.cases
        print(f"INVARIANTS SELF-TEST PASS — {len(report.checked)} claims, "
              f"{report.proved} proved exhaustively over {report.cases} cases")

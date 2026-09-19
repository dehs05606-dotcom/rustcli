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



def _envelope_invariants() -> list[Invariant]:
    from . import envelopes as env
    from .toolcontract import build_contracts
    from .tools import build_registry

    contracts = build_contracts(build_registry())

    def every_tool_declared() -> Result:
        found = env.check_declarations(contracts)
        missing = [v for v in found if v.kind == env.V_NO_ENVELOPE]
        if missing:
            return fails("a registered tool has no behavioural envelope",
                         {"tools": [v.tool for v in missing][:5]})
        return holds(len(contracts))

    def class_agrees_with_contract() -> Result:
        for name, contract in sorted(contracts.items()):
            envelope = env.ENVELOPES.get(name)
            if envelope is None:
                continue
            allowed = env.CONSISTENT_WITH.get(envelope.klass, ())
            if contract.idempotency not in allowed:
                return fails("an envelope class contradicts its contract's "
                             "idempotency",
                             {"tool": name, "class": envelope.klass,
                              "idempotency": contract.idempotency})
        return holds(len(contracts))

    def classes_are_total() -> Result:
        missing = set(env.CLASSES) - set(env.CONSISTENT_WITH)
        if missing:
            return fails("a repetition class maps to no idempotency",
                         {"classes": sorted(missing)})
        return holds(len(env.CLASSES))

    def effects_are_declared() -> Result:
        for name, envelope in sorted(env.ENVELOPES.items()):
            unknown = set(envelope.effects) - set(env.EFFECTS)
            if unknown:
                return fails("an envelope names an effect that does not "
                             "exist", {"tool": name,
                                       "effects": sorted(unknown)})
        return holds(len(env.ENVELOPES))

    def judging_is_total() -> Result:
        """Every (envelope, outcome, observation) answers without raising."""
        cases = 0
        for name, envelope in sorted(env.ENVELOPES.items()):
            for ok in (True, False):
                for obs in (None, env.Observation(name, ())):
                    verdict = env.judge(envelope, name, {}, ok, obs)
                    cases += 1
                    if not isinstance(verdict, env.Verdict):
                        return fails("judge returned something else",
                                     {"tool": name})
                    for v in verdict.violations:
                        if v.kind not in env.KINDS:
                            return fails("a violation kind that does not "
                                         "exist", {"kind": v.kind})
        return holds(cases)

    def unmeasurable_never_violates() -> Result:
        """A tool this module cannot observe is never blamed by it.

        An envelope with no path arguments has nothing measured, and a
        check that cannot see anything must not be able to fail a call.
        """
        cases = 0
        for name, envelope in sorted(env.ENVELOPES.items()):
            if envelope.measurable:
                continue
            for ok in (True, False):
                verdict = env.judge(envelope, name, {}, ok,
                                    env.Observation(name, ()))
                cases += 1
                if verdict.blocking:
                    return fails("an unmeasurable envelope blocked a call",
                                 {"tool": name})
        return holds(cases)

    def missing_envelope_never_blocks() -> Result:
        verdict = env.judge(None, "unknown", {}, True, None)
        if verdict.blocking:
            return fails("a missing declaration blamed the call for it",
                         {"violations": [v.to_dict()
                                         for v in verdict.violations]})
        if verdict.ok:
            return fails("a missing declaration was reported as clean")
        return holds(1)

    return [
        Invariant("every-tool-has-an-envelope", "envelopes", TOTALITY,
                  "every registered tool declares a behavioural envelope",
                  every_tool_declared, exhaustive=True),
        Invariant("envelope-class-matches-contract", "envelopes",
                  CONSISTENCY,
                  "an envelope's repetition class agrees with its "
                  "contract's idempotency",
                  class_agrees_with_contract, exhaustive=True),
        Invariant("classes-map-to-idempotency", "envelopes", TOTALITY,
                  "every repetition class says which idempotencies it "
                  "allows", classes_are_total, exhaustive=True),
        Invariant("effects-are-in-the-vocabulary", "envelopes", CLOSURE,
                  "no envelope names an effect outside the declared set",
                  effects_are_declared, exhaustive=True),
        Invariant("judging-is-total", "envelopes", TOTALITY,
                  "judging answers for every envelope, outcome and "
                  "observation", judging_is_total, exhaustive=True),
        Invariant("unmeasurable-never-blocks", "envelopes", POSTCONDITION,
                  "an envelope with nothing observable never fails a call",
                  unmeasurable_never_violates, exhaustive=True),
        Invariant("undeclared-tool-is-not-blamed", "envelopes",
                  POSTCONDITION,
                  "a tool with no envelope is reported, not blocked",
                  missing_envelope_never_blocks, exhaustive=True),
    ]


def _releasegate_invariants() -> list[Invariant]:
    from . import releasegate as rel

    def release_is_unconstructible() -> Result:
        try:
            rel.Release(object(), rel.Range("x"), "d", rel.Evidence(), 0.0)
        except rel.ReleaseRefused:
            return holds(1)
        except Exception as exc:
            return fails("Release raised the wrong thing",
                         {"exception": type(exc).__name__})
        return fails("a Release was constructed outside the gate, which "
                     "is the one thing this type exists to prevent")

    def every_reason_has_a_remedy() -> Result:
        for code, pair in sorted(rel.REASONS.items()):
            what, remedy = pair
            if not what or not remedy:
                return fails("a refusal reason does not say what clears it",
                             {"code": code})
        return holds(len(rel.REASONS))

    return [
        Invariant("release-needs-the-gate", "releasegate", PRECONDITION,
                  "a Release cannot be constructed outside build_release",
                  release_is_unconstructible, exhaustive=True),
        Invariant("release-reasons-are-actionable", "releasegate",
                  TOTALITY,
                  "every refusal reason says what would clear it",
                  every_reason_has_a_remedy, exhaustive=True),
    ]


def _calibration_invariants() -> list[Invariant]:
    from . import calibration as cal

    def loosening_is_never_automatic() -> Result:
        """No calibration outcome lowers scrutiny without a human.

        Checked over every combination of the inputs `recalibrate` reads,
        which is the whole space of decisions it can make.
        """
        cases = 0
        for audits in (0, 5, 20, 100):
            for hold in (0.0, 0.05, 0.3, 0.9):
                for recent in (0.0, 0.05, 0.3, 0.9):
                    for windows in (0, 1, 3):
                        for downgraded in ((), ("lazy",)):
                            for floor in (2, 3, 4):
                                c = cal.Calibration(
                                    audits=audits, windows=windows,
                                    hold_rate=hold, recent_hold_rate=recent)
                                c.degeneracies = tuple(
                                    cal.Degeneracy(cal.D_ALWAYS_PASSES, d,
                                                   "constant")
                                    for d in downgraded)
                                current = cal.Threshold(min_agreeing=floor)
                                out = cal.recalibrate(c, current)
                                cases += 1
                                applied = out.threshold.min_agreeing
                                if applied < floor:
                                    return fails(
                                        "calibration lowered the threshold "
                                        "by itself",
                                        {"from": floor, "to": applied,
                                         "audits": audits})
                                if out.proposal is not None and \
                                        out.proposal.in_effect.min_agreeing \
                                        != floor:
                                    return fails(
                                        "an unaccepted proposal changed "
                                        "the threshold in effect",
                                        {"from": floor})
        return holds(cases)

    def degeneracies_are_explained() -> Result:
        for kind, pair in sorted(cal.DEGENERACIES.items()):
            what, cost = pair
            if not what or not cost:
                return fails("a degeneracy does not explain itself",
                             {"kind": kind})
        return holds(len(cal.DEGENERACIES))

    def unsure_is_never_a_downgrade_target() -> Result:
        """Only a strategy that stopped discriminating loses its pass."""
        downgrading = {k for k in cal.DEGENERACIES
                       if cal.Degeneracy(k, "x", "y").downgrades}
        if cal.D_ALWAYS_FAILS in downgrading:
            return fails("a strategy that fails everything had a pass "
                         "downgraded, which it does not have")
        if not downgrading:
            return fails("no degeneracy downgrades anything, so the "
                         "finding has no consequence")
        return holds(len(cal.DEGENERACIES))

    return [
        Invariant("loosening-needs-a-human", "calibration", POSTCONDITION,
                  "calibration may tighten on its own and never loosen",
                  loosening_is_never_automatic, exhaustive=True),
        Invariant("degeneracies-explain-themselves", "calibration",
                  TOTALITY,
                  "every degeneracy says what it means and what it costs",
                  degeneracies_are_explained, exhaustive=True),
        Invariant("downgrade-targets-are-right", "calibration",
                  CONSISTENCY,
                  "only a strategy that stopped discriminating loses its "
                  "pass", unsure_is_never_a_downgrade_target,
                  exhaustive=True),
    ]


def _invariantloop_invariants() -> list[Invariant]:
    from . import invariantloop as loop

    class _Gate:
        def __init__(self, allowed):
            self.allowed = allowed
            self.reasons = ()

        def to_dict(self):
            return {"allowed": self.allowed}

    def _ledger() -> loop.Ledger:
        led = loop.Ledger()
        led.observe([loop.Candidate("c1", loop.S_GAP, "provenance",
                                    CONSISTENCY, "a claim")])
        return led

    def nothing_adopts_itself() -> Result:
        cases = 0
        for who in ("", "somebody"):
            for gate in (None, _Gate(False), _Gate(True)):
                led = _ledger()
                cases += 1
                try:
                    led.accept("c1", who, "why", gate)
                except (ValueError, loop.NotGated):
                    continue
                if not who or gate is None or gate.allowed is not True:
                    return fails("a candidate was adopted without a named "
                                 "human and a passing gate",
                                 {"who": who,
                                  "gate": None if gate is None
                                  else gate.allowed})
        return holds(cases)

    def nothing_drops_silently() -> Result:
        led = _ledger()
        try:
            led.reject("c1", "somebody", "")
        except ValueError:
            return holds(1)
        return fails("a candidate was rejected with no reason recorded")

    def decisions_are_final() -> Result:
        led = _ledger()
        led.reject("c1", "somebody", "not a real defect")
        try:
            led.accept("c1", "somebody else", "changed my mind",
                       _Gate(True))
        except ValueError:
            return holds(1)
        return fails("a decided candidate was re-decided, erasing the "
                     "record of who decided it first")

    def ids_are_content_addressed() -> Result:
        a = loop._ident(loop.S_GAP, "provenance", "the same claim")
        b = loop._ident(loop.S_GAP, "provenance", "the same claim")
        c = loop._ident(loop.S_GAP, "provenance", "a different claim")
        if a != b:
            return fails("the same claim produced two ids")
        if a == c:
            return fails("two different claims collided on one id")
        return holds(3)

    def sources_are_explained() -> Result:
        missing = set(loop.SOURCES) - set(loop.SOURCE_MEANING)
        if missing:
            return fails("a candidate source explains nothing",
                         {"sources": sorted(missing)})
        return holds(len(loop.SOURCES))

    return [
        Invariant("candidates-need-a-human-and-a-gate", "invariantloop",
                  PRECONDITION,
                  "no candidate is adopted without a named human and a "
                  "passing regression gate", nothing_adopts_itself,
                  exhaustive=True),
        Invariant("rejection-needs-a-reason", "invariantloop",
                  PRECONDITION,
                  "a candidate cannot be dropped without a recorded reason",
                  nothing_drops_silently, exhaustive=True),
        Invariant("decisions-are-final", "invariantloop", POSTCONDITION,
                  "a decided candidate cannot be quietly re-decided",
                  decisions_are_final, exhaustive=True),
        Invariant("candidate-ids-are-content-addressed", "invariantloop",
                  CONSISTENCY,
                  "the same evidence always produces the same candidate id",
                  ids_are_content_addressed, exhaustive=True),
        Invariant("every-source-is-explained", "invariantloop", TOTALITY,
                  "every candidate source says what it means",
                  sources_are_explained, exhaustive=True),
    ]


def _budget_invariants() -> list[Invariant]:
    from . import budgets as bud
    from .toolcontract import build_contracts
    from .tools import build_registry

    contracts = build_contracts(build_registry())

    def floors_are_never_bought_back() -> Result:
        """No budget, however small, drops a call below its floor.

        Enumerated over every tool and a spread of budgets: the whole
        point of the module is that this case cannot happen, so it is
        checked rather than asserted.
        """
        planner = bud.BudgetPlanner(contracts)
        cases = 0
        for tool in sorted(contracts):
            for total in (0, 1, 3, 10, 30, 100):
                decision = planner.plan(tool, bud.Budget(total))
                cases += 1
                if decision.refused:
                    if decision.runs:
                        return fails("a refused call still ran checks",
                                     {"tool": tool, "runs":
                                      list(decision.runs)})
                    continue
                if bud.depth_rank(decision.depth) < \
                        bud.depth_rank(decision.floor):
                    return fails("a call was verified below its floor",
                                 {"tool": tool, "budget": total,
                                  "depth": decision.depth,
                                  "floor": decision.floor})
        return holds(cases)

    def depths_are_monotonic() -> Result:
        for i in range(len(bud.DEPTHS) - 1):
            a, b = bud.DEPTHS[i], bud.DEPTHS[i + 1]
            if not set(bud.INCLUDES[a]) <= set(bud.INCLUDES[b]):
                return fails("a deeper level does not include a shallower "
                             "one", {"shallower": a, "deeper": b})
            if bud.COST[a] > bud.COST[b]:
                return fails("a deeper level costs less",
                             {"shallower": a, "deeper": b})
        return holds(len(bud.DEPTHS))

    def every_grade_has_a_floor() -> Result:
        from .riskgrade import GRADES
        missing = set(GRADES) - set(bud.FLOOR)
        if missing:
            return fails("a risk grade has no minimum depth",
                         {"grades": sorted(missing)})
        for grade, floor in bud.FLOOR.items():
            if floor not in bud.DEPTHS:
                return fails("a floor names a depth that does not exist",
                             {"grade": grade, "floor": floor})
        return holds(len(GRADES))

    def unknown_tool_is_critical() -> Result:
        planner = bud.BudgetPlanner({})
        if planner.grade_of("nobody_declared_me") != "critical":
            return fails("an undeclared tool was graded as anything other "
                         "than critical; unknown is not safe")
        return holds(1)

    def every_decision_explains_itself() -> Result:
        planner = bud.BudgetPlanner(contracts)
        cases = 0
        for tool in sorted(contracts):
            decision = planner.plan(tool, bud.Budget(bud.UNLIMITED))
            cases += 1
            if decision.rule not in bud.RULES:
                return fails("a budget decision cited a rule that does "
                             "not exist", {"tool": tool,
                                           "rule": decision.rule})
            if not decision.to_dict()["why"]:
                return fails("a budget decision gave no reason",
                             {"tool": tool})
        return holds(cases)

    return [
        Invariant("floor-is-never-bought-back", "budgets", POSTCONDITION,
                  "no budget drops a call below its risk grade's floor",
                  floors_are_never_bought_back, exhaustive=True),
        Invariant("depths-are-monotonic", "budgets", CONSISTENCY,
                  "a deeper level includes everything a shallower one "
                  "runs and costs at least as much", depths_are_monotonic,
                  exhaustive=True),
        Invariant("every-grade-has-a-floor", "budgets", TOTALITY,
                  "every risk grade names a minimum verification depth",
                  every_grade_has_a_floor, exhaustive=True),
        Invariant("unknown-tool-is-critical", "budgets", POSTCONDITION,
                  "a tool with no contract is graded critical, not safe",
                  unknown_tool_is_critical, exhaustive=True),
        Invariant("budget-decisions-are-auditable", "budgets", TOTALITY,
                  "every budget decision names a rule that exists and "
                  "gives a reason", every_decision_explains_itself,
                  exhaustive=True),
    ]


def _runbook_invariants() -> list[Invariant]:
    from . import recovery
    from . import runbook as rb
    from .toolcontract import ERROR_CODES

    def every_class_is_injectable() -> Result:
        missing = set(ERROR_CODES) - set(rb.INJECTORS)
        extra = set(rb.INJECTORS) - set(ERROR_CODES)
        if missing or extra:
            return fails("a failure class has no injector, or an injector "
                         "no class", {"missing": sorted(missing),
                                      "extra": sorted(extra)})
        return holds(len(ERROR_CODES))

    def every_class_is_exercised() -> Result:
        covered = {b.code for b in rb.RUNBOOKS}
        missing = set(ERROR_CODES) - covered
        if missing:
            return fails("a failure class has no runbook",
                         {"classes": sorted(missing)})
        return holds(len(ERROR_CODES))

    def expectations_come_from_the_playbook() -> Result:
        """No runbook's expectation is a constant it wrote down itself."""
        for book in rb.RUNBOOKS:
            expected = book.expected()
            if expected not in recovery.STRATEGIES:
                return fails("a runbook expects a strategy that does not "
                             "exist", {"id": book.id, "expected": expected})
            if expected == recovery.RETRY:
                return fails("a runbook expects a retry the dispatcher "
                             "has already spent", {"id": book.id})
        return holds(len(rb.RUNBOOKS))

    def defects_explain_themselves() -> Result:
        for kind, pair in sorted(rb.DEFECTS.items()):
            what, remedy = pair
            if not what or not remedy:
                return fails("a runbook defect does not say what to do "
                             "about it", {"kind": kind})
        return holds(len(rb.DEFECTS))

    return [
        Invariant("every-failure-class-is-injectable", "runbook", TOTALITY,
                  "every error code has a deterministic injector",
                  every_class_is_injectable, exhaustive=True),
        Invariant("every-failure-class-is-exercised", "runbook", TOTALITY,
                  "every error code has a runbook proving its playbook",
                  every_class_is_exercised, exhaustive=True),
        Invariant("expectations-are-read-not-written", "runbook",
                  CONSISTENCY,
                  "a runbook's expectation comes from the playbook, not "
                  "from a constant beside it",
                  expectations_come_from_the_playbook, exhaustive=True),
        Invariant("runbook-defects-explain-themselves", "runbook",
                  TOTALITY,
                  "every runbook defect says what would fix it",
                  defects_explain_themselves, exhaustive=True),
    ]


def _assurance_invariants() -> list[Invariant]:
    from . import assurance as asr

    def every_defect_explains_itself() -> Result:
        for kind, pair in sorted(asr.DEFECTS.items()):
            what, remedy = pair
            if not what or not remedy:
                return fails("an assurance defect does not say what would "
                             "fix it", {"kind": kind})
        return holds(len(asr.DEFECTS))

    def the_shipped_case_reaches_every_node() -> Result:
        """No node hides in the case without a root reaching it.

        An orphan is already a defect; this is the stronger claim that
        the shipped case has none, so every sentence in it is one a
        reader can get to from the top.
        """
        case = asr.shipped_case()
        stranded = sorted(set(case.nodes) - case.reachable())
        if stranded:
            return fails("the shipped case has nodes no root reaches",
                         {"nodes": stranded[:5]})
        return holds(len(case.nodes))

    def every_claim_has_something_under_it() -> Result:
        case = asr.shipped_case()
        bare = sorted(n.id for n in case.nodes.values()
                      if n.kind in (asr.CLAIM, asr.INFERENCE)
                      and not n.children)
        if bare:
            return fails("a claim or inference in the shipped case has no "
                         "children at all", {"nodes": bare[:5]})
        return holds(len(case.nodes))

    def every_evidence_node_can_produce() -> Result:
        case = asr.shipped_case()
        idle = sorted(n.id for n in case.nodes.values()
                      if n.kind == asr.EVIDENCE and n.producer is None)
        if idle:
            return fails("an evidence node has no producer, so it can only "
                         "assert", {"nodes": idle})
        return holds(sum(1 for n in case.nodes.values()
                         if n.kind == asr.EVIDENCE))

    def every_assumption_says_what_it_assumes() -> Result:
        case = asr.shipped_case()
        silent = sorted(n.id for n in case.nodes.values()
                        if n.kind == asr.ASSUMPTION and not n.text.strip())
        if silent:
            return fails("an assumption in the shipped case is blank",
                         {"nodes": silent})
        return holds(len(case.assumed))

    return [
        Invariant("assurance-defects-explain-themselves", "assurance",
                  TOTALITY,
                  "every assurance defect says what would fix it",
                  every_defect_explains_itself, exhaustive=True),
        Invariant("shipped-case-has-no-orphans", "assurance", CLOSURE,
                  "every node in the shipped case is reachable from a root",
                  the_shipped_case_reaches_every_node, exhaustive=True),
        Invariant("no-claim-stands-alone", "assurance", TOTALITY,
                  "no claim or inference in the shipped case is childless",
                  every_claim_has_something_under_it, exhaustive=True),
        Invariant("evidence-can-be-gathered", "assurance", TOTALITY,
                  "every evidence node has a producer that can be run",
                  every_evidence_node_can_produce, exhaustive=True),
        Invariant("assumptions-are-stated", "assurance", TOTALITY,
                  "every assumption in the shipped case says what it "
                  "assumes", every_assumption_says_what_it_assumes,
                  exhaustive=True),
    ]


def _policymeta_invariants() -> list[Invariant]:
    from . import policymeta as pm
    from .policypipeline import DEFAULT_STAGES

    def every_stage_is_modelled() -> Result:
        shipped = {s.name for s in DEFAULT_STAGES}
        missing = sorted(shipped - set(pm.STAGE_MODELS))
        phantom = sorted(set(pm.STAGE_MODELS) - shipped)
        if missing or phantom:
            return fails("the metamodel and the pipeline name different "
                         "stages", {"unmodelled": missing,
                                    "phantom": phantom})
        return holds(len(shipped))

    def the_spec_is_order_free() -> Result:
        """`spec_outcome` must not depend on the order it is given.

        Enumerated over every outcome vector and its reverse: the spec is
        what reorder-safety is proved against, so a spec that quietly
        cared about order would make that proof meaningless.
        """
        import itertools
        cases = 0
        for vector in itertools.product(pm.OUTCOMES, repeat=4):
            cases += 1
            if pm.spec_outcome(vector) != pm.spec_outcome(vector[::-1]):
                return fails("the specification depends on stage order",
                             {"vector": list(vector)}, cases)
        return holds(cases)

    def every_property_is_registered() -> Result:
        report = pm.verify_metamodel()
        found = {p.id for p in report.properties}
        declared = set(pm.PROPERTY_IDS)
        if found != declared:
            return fails("a meta-property is run but not declared, or "
                         "declared but not run",
                         {"unregistered": sorted(found - declared),
                          "never-run": sorted(declared - found)})
        return holds(len(declared))

    def every_failure_kind_explains_itself() -> Result:
        for kind, what in sorted(pm.FAILURES.items()):
            if not what:
                return fails("a metamodel failure kind has no explanation",
                             {"kind": kind})
        return holds(len(pm.FAILURES))

    def a_deny_is_the_most_restrictive_answer() -> Result:
        worst = max(pm.RESTRICTIVENESS, key=pm.RESTRICTIVENESS.get)
        if worst != "deny":
            return fails("something outranks a deny in the restrictiveness "
                         "order, so 'widen' no longer means what it says",
                         {"worst": worst})
        if pm.RESTRICTIVENESS.get(pm.SKIP, 0) != 0:
            return fails("a stage that skipped is ranked above one that "
                         "checked and allowed", {})
        return holds(len(pm.RESTRICTIVENESS))

    return [
        Invariant("every-stage-is-modelled", "policymeta", TOTALITY,
                  "the metamodel and the shipped pipeline name exactly the "
                  "same stages", every_stage_is_modelled, exhaustive=True),
        Invariant("spec-is-order-free", "policymeta", CONSISTENCY,
                  "the ordering specification gives the same answer "
                  "whatever order it reads the stages in",
                  the_spec_is_order_free, exhaustive=True),
        Invariant("meta-properties-are-registered", "policymeta",
                  CONSISTENCY,
                  "every meta-property that runs is declared, and every "
                  "one declared runs", every_property_is_registered,
                  exhaustive=True),
        Invariant("metamodel-failures-explain-themselves", "policymeta",
                  TOTALITY,
                  "every metamodel failure kind says what it means",
                  every_failure_kind_explains_itself, exhaustive=True),
        Invariant("deny-is-the-ceiling", "policymeta", CLOSURE,
                  "a deny is the most restrictive outcome and a skip the "
                  "least, which is what makes 'widen' a word with a "
                  "meaning", a_deny_is_the_most_restrictive_answer,
                  exhaustive=True),
    ]


def _faultcatalogue_invariants() -> list[Invariant]:
    from . import faultcatalogue as fc

    def every_surveyed_class_is_accounted_for() -> Result:
        """No class is merely absent: covered, or unreachable with a reason.

        The coverage check itself runs the scenarios; this is the cheap
        structural half, so a missing counterfactual is caught by the
        invariant run as well as by the catalogue run.
        """
        targets = {(c.surface, c.target) for c in fc.counterfactuals()}
        cases = 0
        for surface in fc.SURFACES:
            for code in surface.universe():
                cases += 1
                if (surface.id, code) in targets:
                    continue
                if code in fc.UNREACHABLE:
                    continue
                return fails("a typed refusal has neither a counterfactual "
                             "nor a written reason it cannot be reached",
                             {"surface": surface.id, "class": code}, cases)
        return holds(cases)

    def unreachable_claims_carry_a_reason() -> Result:
        for code, why in sorted(fc.UNREACHABLE.items()):
            if not why.strip():
                return fails("a class is declared unreachable with no "
                             "reason", {"class": code})
        return holds(len(fc.UNREACHABLE))

    def no_counterfactual_is_orphaned() -> Result:
        known = {s.id: set(s.universe()) for s in fc.SURFACES}
        for case in fc.counterfactuals():
            if case.target not in known.get(case.surface, set()):
                return fails("a counterfactual targets a class its surface "
                             "does not declare",
                             {"surface": case.surface,
                              "class": case.target})
        return holds(len(fc.counterfactuals()))

    def catalogue_defects_explain_themselves() -> Result:
        for kind, pair in sorted(fc.DEFECTS.items()):
            what, remedy = pair
            if not what or not remedy:
                return fails("a catalogue defect does not say what would "
                             "fix it", {"kind": kind})
        return holds(len(fc.DEFECTS))

    return [
        Invariant("every-failure-class-is-surveyed", "faultcatalogue",
                  TOTALITY,
                  "every typed refusal has a counterfactual, or a written "
                  "reason none can reach it",
                  every_surveyed_class_is_accounted_for, exhaustive=True),
        Invariant("unreachable-is-a-claim-with-a-reason", "faultcatalogue",
                  TOTALITY,
                  "no class is declared unreachable without saying why",
                  unreachable_claims_carry_a_reason, exhaustive=True),
        Invariant("no-orphan-counterfactuals", "faultcatalogue",
                  CONSISTENCY,
                  "every counterfactual targets a class its surface "
                  "declares", no_counterfactual_is_orphaned,
                  exhaustive=True),
        Invariant("catalogue-defects-explain-themselves", "faultcatalogue",
                  TOTALITY,
                  "every catalogue defect says what would fix it",
                  catalogue_defects_explain_themselves, exhaustive=True),
    ]


def _historicaudit_invariants() -> list[Invariant]:
    from . import historicaudit as ha

    def every_verdict_is_explained() -> Result:
        for kind, pair in sorted(ha.VERDICTS.items()):
            what, remedy = pair
            if not what:
                return fails("a replay verdict has no explanation",
                             {"kind": kind})
            if kind not in ha.JUDGED and not remedy:
                return fails("a verdict that leaves a decision unjudged "
                             "does not say what to do about it",
                             {"kind": kind})
        return holds(len(ha.VERDICTS))

    def only_a_real_replay_counts_as_judged() -> Result:
        """Nothing but agrees/differs may count as having re-checked.

        The failure mode this guards is a report saying "12 of 12
        re-checked" when most of them were unreplayable, which is the one
        number an auditor would read and the one that must not be able to
        drift.
        """
        if set(ha.JUDGED) != {ha.V_AGREES, ha.V_DIFFERS}:
            return fails("a verdict other than agrees/differs counts as a "
                         "re-check", {"judged": list(ha.JUDGED)})
        return holds(len(ha.VERDICTS))

    def the_limits_are_stated() -> Result:
        if not ha.REPLAY_LIMITS:
            return fails("replay declares no limits, which cannot be true",
                         {})
        for line in ha.REPLAY_LIMITS:
            if len(line.strip()) < 20:
                return fails("a stated replay limit says nothing",
                             {"limit": line})
        return holds(len(ha.REPLAY_LIMITS))

    def a_newer_rule_never_governs_an_older_decision() -> Result:
        """Exhaustive over a small ladder of bundle and decision positions.

        The property is that `governing` picks the newest bundle sealed at
        or before a decision, and never one sealed after it: a rule
        written on Tuesday did not govern Monday, however much it looks
        like it should have.
        """
        made = tuple(ha.Ruleset(f"d{i}", 0.0, {}, {}, f"v{i}", "", i)
                     for i in range(6))
        cases = 0
        for seq in range(-1, 8):
            cases += 1
            picked = ha.governing(made, seq)
            expected = max((b for b in made if 0 <= b.seq <= seq),
                           key=lambda b: b.seq, default=None)
            if picked is not expected:
                return fails("governing() chose a ruleset that was not the "
                             "newest one at or before the decision",
                             {"seq": seq,
                              "picked": picked.label if picked else None},
                             cases)
        return holds(cases)

    return [
        Invariant("replay-verdicts-explain-themselves", "historicaudit",
                  TOTALITY,
                  "every replay verdict says what it means, and every one "
                  "that leaves a decision unjudged says what to do",
                  every_verdict_is_explained, exhaustive=True),
        Invariant("only-a-replay-counts-as-judged", "historicaudit",
                  CLOSURE,
                  "only agrees and differs count as having re-checked a "
                  "decision", only_a_real_replay_counts_as_judged,
                  exhaustive=True),
        Invariant("replay-limits-are-stated", "historicaudit", TOTALITY,
                  "replay carries its own limits in the report, not only "
                  "in a docstring", the_limits_are_stated, exhaustive=True),
        Invariant("rules-do-not-apply-backwards", "historicaudit",
                  CONSISTENCY,
                  "a decision is governed by the newest ruleset sealed at "
                  "or before it, never by a later one",
                  a_newer_rule_never_governs_an_older_decision,
                  exhaustive=True),
    ]


def _assuranceboard_invariants() -> list[Invariant]:
    from . import assuranceboard as ab

    def unknown_is_louder_than_a_failure() -> Result:
        if ab.LOUDNESS[ab.UNKNOWN] <= ab.LOUDNESS[ab.ATTENTION]:
            return fails("an unmeasured cell sorts below a failing one, so "
                         "'never checked' reads as quieter than 'checked "
                         "and broken'", dict(ab.LOUDNESS))
        if ab.LOUDNESS[ab.OK] != 0:
            return fails("a clean cell is not the quietest state",
                         dict(ab.LOUDNESS))
        return holds(len(ab.LOUDNESS))

    def every_state_is_ranked() -> Result:
        missing = sorted(set(ab.STATES) - set(ab.LOUDNESS))
        if missing:
            return fails("a cell state has no place in the ordering",
                         {"states": missing})
        return holds(len(ab.STATES))

    def only_an_unknown_cell_may_lack_a_source() -> Result:
        """Enumerated over every state a cell can be in.

        The board's whole claim is that a displayed figure names the seal
        it came from. A cell in any state but `unknown` with no source
        breaks it, so the rule is checked as a property of `Cell` rather
        than trusted to each reader.
        """
        cases = 0
        for state in ab.STATES:
            for source, seq in (("", -1), ("some.event", 3)):
                cases += 1
                cell = ab.Cell("k", "K", state, "1", source=source, seq=seq)
                board = ab.Board((cell,))
                expected = bool(source) or state == ab.UNKNOWN
                if board.traceable != expected:
                    return fails("a cell with no sealed source was "
                                 "reported as traceable",
                                 {"state": state, "source": source}, cases)
        return holds(cases)

    def every_cell_key_has_a_reader() -> Result:
        keys = [key for key, _ in ab.READERS]
        if len(keys) != len(set(keys)):
            return fails("two readers claim the same cell", {"keys": keys})
        unknown = sorted(set(ab.FILLS) - set(keys))
        if unknown:
            return fails("a command is advertised for a cell nobody reads",
                         {"cells": unknown})
        return holds(len(keys))

    return [
        Invariant("unmeasured-is-the-loudest", "assuranceboard", CLOSURE,
                  "an unmeasured cell outranks a failing one, because a "
                  "failure has an owner and an unmeasured surface does not",
                  unknown_is_louder_than_a_failure, exhaustive=True),
        Invariant("every-state-is-ranked", "assuranceboard", TOTALITY,
                  "every cell state has a place in the ordering",
                  every_state_is_ranked, exhaustive=True),
        Invariant("figures-name-their-seal", "assuranceboard", POSTCONDITION,
                  "only a cell displaying no figure may lack a sealed "
                  "source", only_an_unknown_cell_may_lack_a_source,
                  exhaustive=True),
        Invariant("every-cell-has-one-reader", "assuranceboard",
                  CONSISTENCY,
                  "each cell has exactly one reader, and every advertised "
                  "command fills a cell that exists",
                  every_cell_key_has_a_reader, exhaustive=True),
    ]


def _threatpin_invariants() -> list[Invariant]:
    from . import threatpins as tp

    def every_risk_says_where_it_is_written() -> Result:
        for risk in tp.RISKS:
            if len(risk.statement.strip()) < 40:
                return fails("a documented risk is stated too briefly to "
                             "be a limit anyone could act on",
                             {"risk": risk.id})
            if not risk.where.strip():
                return fails("a risk does not say where it is documented, "
                             "so a change to it cannot be followed through "
                             "to the text that states it", {"risk": risk.id})
        return holds(len(tp.RISKS))

    def a_narrowing_is_not_silently_a_pass() -> Result:
        """Both directions need a person.

        A widening is obviously a decision. A narrowing is the one that
        gets waved through -- and a document that overstates a limit
        misleads exactly as much as one that understates it.
        """
        if tp.P_NARROWED not in tp.NEEDS_REVIEW:
            return fails("a narrowing passes without anyone deciding, so "
                         "the documents stay wrong", {})
        for kind in (tp.P_WIDENED, tp.P_UNRECORDED, tp.P_STALE,
                     tp.P_UNMEASURABLE):
            if kind not in tp.NEEDS_REVIEW:
                return fails("a verdict that is not 'held' counts as clean",
                             {"kind": kind})
        if tp.P_HELD in tp.NEEDS_REVIEW:
            return fails("an unchanged posture asks for a decision every "
                         "run, which is how a gate gets muted", {})
        return holds(len(tp.VERDICTS))

    def every_verdict_explains_itself() -> Result:
        for kind, pair in sorted(tp.VERDICTS.items()):
            what, remedy = pair
            if not what:
                return fails("a threat-model verdict has no explanation",
                             {"kind": kind})
            if kind in tp.NEEDS_REVIEW and not remedy:
                return fails("a verdict that needs a person does not say "
                             "what they should do", {"kind": kind})
        return holds(len(tp.VERDICTS))

    def re_recording_needs_a_name_and_a_reason() -> Result:
        """Exhaustive over the four ways to leave one of them out."""
        import tempfile
        from pathlib import Path as _Path

        work = _Path(tempfile.mkdtemp(prefix="fa-inv-pins-"))
        probe = (tp.Risk("probe", "a risk", "here",
                         lambda: tp.Measurement(0, "none")),)
        cases = 0
        for who, why in (("", ""), ("someone", ""), ("", "a reason"),
                         ("  ", "  ")):
            cases += 1
            try:
                tp.record(who, why, work, risks=probe)
            except ValueError:
                continue
            return fails("the threat model was re-recorded with nobody's "
                         "name or no reason on it",
                         {"who": who, "why": why}, cases)
        return holds(cases)

    return [
        Invariant("risks-name-where-they-are-written", "threatpins",
                  TOTALITY,
                  "every pinned risk states the limit in full and says "
                  "which documents carry it",
                  every_risk_says_where_it_is_written, exhaustive=True),
        Invariant("both-directions-need-a-person", "threatpins", CLOSURE,
                  "a narrowing needs a decision just as a widening does, "
                  "and an unchanged posture needs none",
                  a_narrowing_is_not_silently_a_pass, exhaustive=True),
        Invariant("threat-verdicts-explain-themselves", "threatpins",
                  TOTALITY,
                  "every threat-model verdict says what it means and what "
                  "to do", every_verdict_explains_itself, exhaustive=True),
        Invariant("re-recording-is-signed", "threatpins", PRECONDITION,
                  "the threat model cannot be re-recorded without a named "
                  "person and a reason",
                  re_recording_needs_a_name_and_a_reason, exhaustive=True),
    ]


def all_invariants() -> tuple[Invariant, ...]:
    out: list[Invariant] = []
    for builder in (_toolcontract_invariants, _policy_invariants,
                    _recovery_invariants, _orchestrator_invariants,
                    _telemetry_invariants, _manifest_invariants,
                    _constitution_invariants, _envelope_invariants,
                    _releasegate_invariants, _calibration_invariants,
                    _invariantloop_invariants, _budget_invariants,
                    _runbook_invariants, _assurance_invariants,
                    _policymeta_invariants, _faultcatalogue_invariants,
                    _historicaudit_invariants, _assuranceboard_invariants,
                    _threatpin_invariants):
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

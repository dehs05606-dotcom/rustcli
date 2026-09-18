"""ORCHESTRATOR — plan, execute, verify, and undo a sequence of tool calls.

This is not `workflows.py` and does not replace it. A workflow is a saved
recipe whose steps are *tasks given to a model* ("implement the parser",
"review the diff"); it orchestrates subagents. An orchestration here is a
sequence of *tool calls* with concrete arguments, run through the
`Dispatcher`, each one verified by a deterministic check. No model is
consulted anywhere in this file. Where a workflow asks "who does this
next", an orchestration asks "did the call actually do what the plan said
it would, and if not, what has to be put back".

Four properties are the point of the module:

  PLAN-TIME REFUSAL   A plan is validated against the dispatcher before
                      its first step runs: every tool must exist, every
                      argument must satisfy that tool's input schema, and
                      every capability must be one the current role
                      holds. A plan that cannot run is refused whole,
                      with every reason listed, rather than half-run and
                      then abandoned in a state nobody planned for.

  A LEDGER            Every attempt is appended to a ledger and sealed in
                      the event log with its trace id -- before the call
                      for the intent, after it for the outcome. A crash
                      between the two leaves the intent recorded, which
                      is the half that matters when you are trying to
                      work out what touched a file.

  HONEST ROLLBACK     A failed step rolls back the completed ones in
                      reverse order using the compensation each declared.
                      A step that declared none is reported as
                      `irreversible` in the result -- never skipped
                      silently, and never described as rolled back. The
                      orchestrator refuses at plan time to run a step
                      that is both destructive and uncompensated unless
                      the plan says `accept_irreversible=True`.

  DEFAULT DENY        Approval is asked once, for the whole plan, before
                      anything runs: a human approving twelve prompts in
                      a row is not approving, they are clicking. With no
                      approval hook, a plan containing an approval-needing
                      step does not run at all.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .dispatch import Dispatcher, ToolResult, TraceContext
from .toolcontract import UNSAFE, ToolContract

# -- step outcomes ----------------------------------------------------------
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"          # never ran: an earlier step failed
COMPENSATED = "compensated"  # ran, then undone
IRREVERSIBLE = "irreversible"  # ran, failed to undo, and said so


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Expectation:
    """A deterministic check on what a step produced.

    Deliberately small. A check that needed a model to evaluate would
    make verification exactly as unreliable as the thing it verifies.
    """
    contains: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    path_exists: tuple[str, ...] = ()
    path_absent: tuple[str, ...] = ()
    predicate: Callable[[ToolResult], bool] | None = None
    description: str = ""

    def check(self, result: ToolResult) -> tuple[bool, str]:
        import os

        if not result.ok:
            return False, result.error.render() if result.error else "failed"
        text = result.render()
        for needle in self.contains:
            if needle not in text:
                return False, f"output does not contain {needle!r}"
        for needle in self.absent:
            if needle in text:
                return False, f"output contains {needle!r}, which it must not"
        for path in self.path_exists:
            if not os.path.exists(path):
                return False, f"{path} does not exist"
        for path in self.path_absent:
            if os.path.exists(path):
                return False, f"{path} still exists"
        if self.predicate is not None:
            try:
                if not self.predicate(result):
                    return False, self.description or "predicate returned False"
            except Exception as exc:
                return False, f"predicate raised {type(exc).__name__}: {exc}"
        return True, ""


NO_EXPECTATION = Expectation()


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    """One tool call, what it is for, and how to undo it."""
    id: str
    tool: str
    args: dict
    why: str = ""
    expect: Expectation = NO_EXPECTATION
    undo_tool: str = ""
    undo_args: dict = field(default_factory=dict)

    @property
    def reversible(self) -> bool:
        return bool(self.undo_tool)

    def to_dict(self) -> dict:
        return {"id": self.id, "tool": self.tool, "args": self.args,
                "why": self.why, "reversible": self.reversible}


@dataclass(frozen=True)
class Plan:
    """An ordered sequence of steps, plus what it admits about itself."""
    goal: str
    steps: tuple[Step, ...]
    accept_irreversible: bool = False

    def to_dict(self) -> dict:
        return {"goal": self.goal, "steps": [s.to_dict() for s in self.steps],
                "accept_irreversible": self.accept_irreversible}


@dataclass(frozen=True)
class PlanReview:
    """Why a plan may or may not run, decided before anything happens."""
    ok: bool
    problems: tuple[str, ...] = ()
    needs_approval: tuple[str, ...] = ()
    irreversible: tuple[str, ...] = ()

    def format(self) -> str:
        lines = ["PLAN REVIEW — " + ("ready" if self.ok else "refused")]
        for p in self.problems:
            lines.append(f"  refused: {p}")
        for s in self.irreversible:
            lines.append(f"  irreversible: {s}")
        for s in self.needs_approval:
            lines.append(f"  needs approval: {s}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

@dataclass
class LedgerEntry:
    step: str
    tool: str
    status: str = PENDING
    attempts: int = 0
    duration: float = 0.0
    trace_id: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return {"step": self.step, "tool": self.tool, "status": self.status,
                "attempts": self.attempts, "duration": round(self.duration, 4),
                "trace_id": self.trace_id, "detail": self.detail}


@dataclass
class RunResult:
    goal: str
    ok: bool
    ledger: list[LedgerEntry] = field(default_factory=list)
    trace_id: str = ""
    review: PlanReview | None = None
    rolled_back: tuple[str, ...] = ()
    irreversible: tuple[str, ...] = ()

    def entry(self, step_id: str) -> LedgerEntry | None:
        for e in self.ledger:
            if e.step == step_id:
                return e
        return None

    def to_dict(self) -> dict:
        return {"goal": self.goal, "ok": self.ok, "trace_id": self.trace_id,
                "ledger": [e.to_dict() for e in self.ledger],
                "rolled_back": list(self.rolled_back),
                "irreversible": list(self.irreversible)}

    def format(self) -> str:
        head = f"{'RUN OK' if self.ok else 'RUN FAILED'} — {self.goal}"
        lines = [head, f"  trace {self.trace_id}"]
        for e in self.ledger:
            mark = {DONE: "ok", FAILED: "FAIL", SKIPPED: "--",
                    COMPENSATED: "undone",
                    IRREVERSIBLE: "NOT UNDONE"}.get(e.status, e.status)
            detail = f" — {e.detail}" if e.detail else ""
            lines.append(f"  {mark:>10}  {e.step:<18} {e.tool}{detail}")
        if self.irreversible:
            lines.append("  left in place: " + ", ".join(self.irreversible))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------

class Orchestrator:
    """Planner, executor and verifier over one dispatcher."""

    def __init__(self, dispatcher: Dispatcher, log=None,
                 approve: Callable[[Plan, PlanReview], bool] | None = None):
        self.dispatcher = dispatcher
        self.log = log if log is not None else getattr(dispatcher, "log", None)
        self.approve = approve

    # -- planning ----------------------------------------------------------

    def review(self, plan: Plan) -> PlanReview:
        """Decide whether a plan may run, without running any of it."""
        problems: list[str] = []
        approvals: list[str] = []
        irreversible: list[str] = []
        seen: set[str] = set()
        available = set(self.dispatcher.negotiate().available)

        for step in plan.steps:
            if step.id in seen:
                problems.append(f"{step.id}: duplicate step id")
            seen.add(step.id)

            contract = self.dispatcher.contract(step.tool)
            if contract is None:
                problems.append(f"{step.id}: no tool named '{step.tool}'")
                continue
            if step.tool not in available:
                problems.append(
                    f"{step.id}: '{step.tool}' is not available to this role")
            bad = contract.validate_input(step.args)
            if bad is not None:
                problems.append(f"{step.id}: {bad.message}")
            if contract.needs_approval:
                approvals.append(f"{step.id} ({step.tool})")

            if step.undo_tool:
                undo = self.dispatcher.contract(step.undo_tool)
                if undo is None:
                    problems.append(
                        f"{step.id}: undo names no tool '{step.undo_tool}'")
                else:
                    bad_undo = undo.validate_input(step.undo_args)
                    if bad_undo is not None:
                        problems.append(
                            f"{step.id}: undo arguments are invalid: "
                            f"{bad_undo.message}")
            elif self._leaves_a_mark(contract):
                irreversible.append(f"{step.id} ({step.tool})")

        if irreversible and not plan.accept_irreversible:
            problems.append(
                "the plan contains steps that cannot be undone and does not "
                "say it accepts that: " + ", ".join(irreversible))

        return PlanReview(not problems, tuple(problems), tuple(approvals),
                          tuple(irreversible))

    @staticmethod
    def _leaves_a_mark(contract: ToolContract) -> bool:
        """A step worth insisting on a compensation for."""
        return contract.destructive or contract.idempotency == UNSAFE

    # -- running -----------------------------------------------------------

    def _seal(self, event: str, data: dict) -> None:
        if self.log is None:
            return
        try:
            self.log.append(event, data, actor="orchestrator")
        except Exception:
            pass

    def run(self, plan: Plan, trace: TraceContext | None = None,
            approve_tool: Callable[[ToolContract, dict], bool] | None = None
            ) -> RunResult:
        """Execute a plan. Returns a result; never raises."""
        ctx = trace.child() if trace is not None else TraceContext()
        ledger = [LedgerEntry(s.id, s.tool) for s in plan.steps]
        result = RunResult(plan.goal, False, ledger, ctx.trace_id)

        review = self.review(plan)
        result.review = review
        self._seal("orchestrator.plan", {
            "goal": plan.goal, "trace_id": ctx.trace_id,
            "steps": [s.to_dict() for s in plan.steps],
            "ok": review.ok, "problems": list(review.problems)})
        if not review.ok:
            for entry in ledger:
                entry.status = SKIPPED
                entry.detail = "plan refused"
            return result

        if review.needs_approval:
            granted = False
            if self.approve is not None:
                try:
                    granted = bool(self.approve(plan, review))
                except Exception:
                    granted = False   # a broken hook is not consent
            if not granted:
                for entry in ledger:
                    entry.status = SKIPPED
                    entry.detail = "plan not approved"
                self._seal("orchestrator.refused",
                           {"goal": plan.goal, "trace_id": ctx.trace_id,
                            "reason": "approval"})
                return result

        completed: list[tuple[Step, LedgerEntry]] = []
        failure: LedgerEntry | None = None

        for step, entry in zip(plan.steps, ledger):
            entry.status = RUNNING
            self._seal("orchestrator.step", {
                "step": step.id, "tool": step.tool, "why": step.why,
                "trace_id": ctx.trace_id})
            started = time.time()
            call = self.dispatcher.call(step.tool, step.args, trace=ctx,
                                        approve=approve_tool or (
                                            lambda c, a: True))
            entry.attempts = call.attempts
            entry.duration = time.time() - started
            entry.trace_id = call.trace_id

            passed, why = step.expect.check(call)
            entry.status = DONE if passed else FAILED
            if not passed:
                entry.detail = why
            self._seal("orchestrator.step.done", {
                "step": step.id, "status": entry.status,
                "trace_id": call.trace_id, "detail": entry.detail})

            if not passed:
                failure = entry
                # The step that failed may still have changed something --
                # a write that landed and then failed its check is the
                # ordinary case. Its compensation belongs in the rollback
                # too, or the plan leaves behind exactly the file it was
                # careful to be able to remove.
                if call.ok or step.reversible:
                    completed.append((step, entry))
                break
            completed.append((step, entry))

        if failure is None:
            result.ok = True
            self._seal("orchestrator.done",
                       {"goal": plan.goal, "trace_id": ctx.trace_id,
                        "ok": True})
            return result

        for entry in ledger:
            if entry.status == PENDING:
                entry.status = SKIPPED
                entry.detail = f"stopped after {failure.step} failed"

        undone, stuck = self._rollback(completed, ctx, approve_tool)
        result.rolled_back = undone
        result.irreversible = stuck
        self._seal("orchestrator.done", {
            "goal": plan.goal, "trace_id": ctx.trace_id, "ok": False,
            "failed_step": failure.step, "rolled_back": list(undone),
            "irreversible": list(stuck)})
        return result

    def _rollback(self, completed, ctx, approve_tool
                  ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Undo completed steps in reverse. Reports what it could not."""
        undone: list[str] = []
        stuck: list[str] = []
        for step, entry in reversed(completed):
            failed = entry.status == FAILED
            if not step.reversible:
                if not failed:
                    entry.status = IRREVERSIBLE
                    entry.detail = "no compensation was declared"
                else:
                    entry.detail += "; no compensation was declared"
                stuck.append(step.id)
                continue
            call = self.dispatcher.call(step.undo_tool, step.undo_args,
                                        trace=ctx,
                                        approve=approve_tool or (
                                            lambda c, a: True))
            if call.ok:
                # A step that failed keeps saying so: "failed, then undone"
                # is the truth, and overwriting it with "undone" would hide
                # which step stopped the plan.
                if failed:
                    entry.detail += f"; undone with {step.undo_tool}"
                else:
                    entry.status = COMPENSATED
                    entry.detail = f"undone with {step.undo_tool}"
                undone.append(step.id)
            else:
                reason = call.error.message if call.error else "?"
                if failed:
                    entry.detail += f"; NOT undone: {reason}"
                else:
                    entry.status = IRREVERSIBLE
                    entry.detail = f"undo failed: {reason}"
                stuck.append(step.id)
            self._seal("orchestrator.rollback", {
                "step": step.id, "tool": step.undo_tool, "ok": call.ok,
                "trace_id": ctx.trace_id})
        return tuple(undone), tuple(stuck)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import shutil
    import tempfile

    from .kernel import EventLog
    from .toolcontract import build_contracts
    from .toolpolicy import ToolPolicy
    from .tools import build_registry

    workdir = tempfile.mkdtemp(prefix="fa-orch-")
    here = os.getcwd()
    os.chdir(workdir)
    try:
        log = EventLog(path=os.path.join(workdir, "events.jsonl"))
        registry = build_registry()
        contracts = build_contracts(registry)

        def dispatcher(role="developer"):
            policy = ToolPolicy(role, log=log, roots=(workdir,))
            d = Dispatcher(policy=policy, log=log,
                           approve=lambda c, a: True)
            d.register_registry(registry, contracts)
            return d

        orch = Orchestrator(dispatcher(), log=log, approve=lambda p, r: True)

        a = os.path.join(workdir, "a.txt")
        b = os.path.join(workdir, "b.txt")

        # --- a plan that runs, verifies and reports -----------------------
        plan = Plan("write two files", (
            Step("write-a", "write_file", {"path": a, "content": "alpha\n"},
                 why="the first file",
                 expect=Expectation(path_exists=(a,)),
                 undo_tool="delete_path", undo_args={"path": a}),
            Step("check-a", "read_file", {"path": a},
                 expect=Expectation(contains=("alpha",))),
            Step("write-b", "write_file", {"path": b, "content": "beta\n"},
                 expect=Expectation(path_exists=(b,)),
                 undo_tool="delete_path", undo_args={"path": b}),
        ))
        out = orch.run(plan)
        assert out.ok, out.format()
        assert os.path.exists(a) and os.path.exists(b)
        assert all(e.status == DONE for e in out.ledger), out.format()
        assert out.trace_id and all(e.trace_id == out.trace_id
                                    for e in out.ledger if e.attempts)

        # --- a failing verification rolls the earlier steps back ----------
        c = os.path.join(workdir, "c.txt")
        d_path = os.path.join(workdir, "d.txt")
        failing = Plan("write then fail", (
            Step("write-c", "write_file", {"path": c, "content": "gamma\n"},
                 expect=Expectation(path_exists=(c,)),
                 undo_tool="delete_path", undo_args={"path": c}),
            Step("write-d", "write_file", {"path": d_path, "content": "x\n"},
                 # the check is wrong on purpose: the step succeeds, the
                 # expectation does not hold, and that must count as failure.
                 expect=Expectation(contains=("this text is never written",)),
                 undo_tool="delete_path", undo_args={"path": d_path}),
            Step("never", "read_file", {"path": c}),
        ))
        bad = orch.run(failing)
        assert not bad.ok, bad.format()
        assert bad.entry("write-d").status == FAILED
        assert bad.entry("never").status == SKIPPED
        assert bad.entry("write-c").status == COMPENSATED, bad.format()
        assert not os.path.exists(c), "rollback did not remove the file"
        assert "write-c" in bad.rolled_back
        # the step that failed its check had also written a file: it is
        # rolled back too, while still reading as the step that failed
        assert not os.path.exists(d_path), \
            "the failing step left its own file behind"
        assert "write-d" in bad.rolled_back
        assert bad.entry("write-d").status == FAILED
        assert "undone with delete_path" in bad.entry("write-d").detail

        # --- an uncompensated destructive step is refused at plan time ----
        victim = os.path.join(workdir, "victim.txt")
        open(victim, "w").write("bye\n")
        reckless = Plan("delete without an undo", (
            Step("nuke", "delete_path", {"path": victim}),
        ))
        refused = orch.run(reckless)
        assert not refused.ok
        assert refused.review is not None and refused.review.irreversible
        assert os.path.exists(victim), "it ran anyway"
        assert all(e.status == SKIPPED for e in refused.ledger)

        # ...unless the plan says so out loud
        owned = Plan("delete on purpose", (
            Step("nuke", "delete_path", {"path": victim},
                 expect=Expectation(path_absent=(victim,))),
        ), accept_irreversible=True)
        allowed = orch.run(owned)
        assert allowed.ok, allowed.format()
        assert not os.path.exists(victim)

        # --- bad arguments are caught before the first step runs ----------
        marker = os.path.join(workdir, "marker.txt")
        broken = Plan("typo in step two", (
            Step("ok", "write_file", {"path": marker, "content": "x"},
                 undo_tool="delete_path", undo_args={"path": marker}),
            Step("typo", "read_file", {"pth": marker}),
        ))
        stopped = orch.run(broken)
        assert not stopped.ok
        assert not os.path.exists(marker), \
            "a plan with an invalid step must not half-run"
        assert any("typo" in p for p in stopped.review.problems), \
            stopped.review.format()

        # --- a tool the role does not hold is refused at plan time --------
        readonly = Orchestrator(dispatcher("readonly"), log=log,
                                approve=lambda p, r: True)
        denied = readonly.review(Plan("write as readonly", (
            Step("w", "write_file", {"path": a, "content": "x"},
                 undo_tool="delete_path", undo_args={"path": a}),)))
        assert not denied.ok and any("not available" in p
                                     for p in denied.problems), denied.format()

        # --- no approval hook means the plan does not run -----------------
        silent = Orchestrator(dispatcher(), log=log, approve=None)
        untouched = os.path.join(workdir, "unapproved.txt")
        held = silent.run(Plan("write unattended", (
            Step("w", "write_file", {"path": untouched, "content": "x"},
                 undo_tool="delete_path", undo_args={"path": untouched}),)))
        assert not held.ok and not os.path.exists(untouched)

        # --- an approval hook that raises is not consent ------------------
        def explodes(plan, review):
            raise RuntimeError("hook is broken")

        hostile = Orchestrator(dispatcher(), log=log, approve=explodes)
        hostile_path = os.path.join(workdir, "hostile.txt")
        blew = hostile.run(Plan("write despite a broken hook", (
            Step("w", "write_file", {"path": hostile_path, "content": "x"},
                 undo_tool="delete_path", undo_args={"path": hostile_path}),)))
        assert not blew.ok and not os.path.exists(hostile_path)

        # --- the ledger reached the event log -----------------------------
        kinds = [e.type for e in log.events()]
        for wanted in ("orchestrator.plan", "orchestrator.step",
                       "orchestrator.step.done", "orchestrator.rollback",
                       "orchestrator.done"):
            assert wanted in kinds, f"{wanted} was never sealed"

        print(out.format())
        print(bad.format())
        print("ORCHESTRATOR SELF-TEST PASS")
    finally:
        os.chdir(here)
        shutil.rmtree(workdir, ignore_errors=True)

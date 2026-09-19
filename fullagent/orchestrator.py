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

from . import recovery
from .dispatch import Dispatcher, ToolResult, TraceContext
from .toolcontract import IDEMPOTENT, UNSAFE, ToolContract

# -- step outcomes ----------------------------------------------------------
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"          # never ran: an earlier step failed
COMPENSATED = "compensated"  # ran, then undone
IRREVERSIBLE = "irreversible"  # ran, failed to undo, and said so
ESCALATED = "escalated"      # ran, outcome unknown, deliberately not undone


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
    """One tool call -- or one nested saga -- and how to undo it."""
    id: str
    tool: str = ""
    args: dict = field(default_factory=dict)
    why: str = ""
    expect: Expectation = NO_EXPECTATION
    undo_tool: str = ""
    undo_args: dict = field(default_factory=dict)
    # A step may hold a whole plan instead of a call. Its compensation is
    # then its children's, run in reverse -- which is what makes this a
    # saga rather than a list: the unit of undo nests with the unit of
    # work, so a sub-plan that fails cleans up after itself before its
    # parent ever sees the failure.
    sub: "Plan | None" = None

    @property
    def nested(self) -> bool:
        return self.sub is not None

    @property
    def reversible(self) -> bool:
        if self.nested:
            return any(child.reversible for child in self.sub.steps)
        return bool(self.undo_tool)

    def to_dict(self) -> dict:
        payload = {"id": self.id, "tool": self.tool, "args": self.args,
                   "why": self.why, "reversible": self.reversible}
        if self.nested:
            payload["sub"] = self.sub.to_dict()
        return payload


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
    path: str = ""          # "outer/inner" for a step inside a sub-saga
    depth: int = 0
    error_code: str = ""
    recovery: str = ""      # the strategy the playbook chose

    def to_dict(self) -> dict:
        return {"step": self.step, "tool": self.tool, "status": self.status,
                "attempts": self.attempts, "duration": round(self.duration, 4),
                "trace_id": self.trace_id, "detail": self.detail,
                "path": self.path or self.step, "depth": self.depth,
                "error_code": self.error_code, "recovery": self.recovery}


@dataclass
class RunResult:
    goal: str
    ok: bool
    ledger: list[LedgerEntry] = field(default_factory=list)
    trace_id: str = ""
    review: PlanReview | None = None
    rolled_back: tuple[str, ...] = ()
    irreversible: tuple[str, ...] = ()
    escalated: tuple[str, ...] = ()

    def entry(self, step_id: str) -> LedgerEntry | None:
        """Find a ledger entry by step id, or by its full `outer/inner` path."""
        for e in self.ledger:
            if e.step == step_id or e.path == step_id:
                return e
        return None

    def to_dict(self) -> dict:
        return {"goal": self.goal, "ok": self.ok, "trace_id": self.trace_id,
                "ledger": [e.to_dict() for e in self.ledger],
                "rolled_back": list(self.rolled_back),
                "irreversible": list(self.irreversible),
                "escalated": list(self.escalated)}

    def format(self) -> str:
        head = f"{'RUN OK' if self.ok else 'RUN FAILED'} — {self.goal}"
        lines = [head, f"  trace {self.trace_id}"]
        for e in self.ledger:
            mark = {DONE: "ok", FAILED: "FAIL", SKIPPED: "--",
                    COMPENSATED: "undone", ESCALATED: "ESCALATED",
                    IRREVERSIBLE: "NOT UNDONE"}.get(e.status, e.status)
            detail = f" — {e.detail}" if e.detail else ""
            name = ("  " * e.depth) + e.step
            label = e.tool or "(saga)"
            lines.append(f"  {mark:>10}  {name:<20} {label}{detail}")
        if self.irreversible:
            lines.append("  left in place: " + ", ".join(self.irreversible))
        if self.escalated:
            lines.append("  needs a human: " + ", ".join(self.escalated))
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
        """Decide whether a plan may run, without running any of it.

        Recurses into sub-sagas, because a plan whose third sub-step names
        a tool that does not exist is exactly as unrunnable as one whose
        first step does, and finding that out after two writes have landed
        is the failure this method exists to prevent.
        """
        problems: list[str] = []
        approvals: list[str] = []
        irreversible: list[str] = []
        available = set(self.dispatcher.negotiate().available)
        self._review_into(plan, "", available, problems, approvals,
                          irreversible, set())

        if irreversible and not plan.accept_irreversible:
            problems.append(
                "the plan contains steps that cannot be undone and does not "
                "say it accepts that: " + ", ".join(irreversible))

        return PlanReview(not problems, tuple(problems), tuple(approvals),
                          tuple(irreversible))

    def _review_into(self, plan: Plan, prefix: str, available: set,
                     problems: list, approvals: list, irreversible: list,
                     seen: set) -> None:
        for step in plan.steps:
            path = f"{prefix}{step.id}"
            if path in seen:
                problems.append(f"{path}: duplicate step id")
            seen.add(path)

            if step.nested:
                if step.tool:
                    problems.append(
                        f"{path}: a step is either a call or a sub-plan, "
                        f"not both")
                if not step.sub.steps:
                    problems.append(f"{path}: the sub-plan has no steps")
                self._review_into(step.sub, f"{path}/", available, problems,
                                  approvals, irreversible, seen)
                continue

            contract = self.dispatcher.contract(step.tool)
            if contract is None:
                problems.append(f"{path}: no tool named '{step.tool}'")
                continue
            if step.tool not in available:
                problems.append(
                    f"{path}: '{step.tool}' is not available to this role")
            bad = contract.validate_input(step.args)
            if bad is not None:
                problems.append(f"{path}: {bad.message}")
            if contract.needs_approval:
                approvals.append(f"{path} ({step.tool})")

            if step.undo_tool:
                undo = self.dispatcher.contract(step.undo_tool)
                if undo is None:
                    problems.append(
                        f"{path}: undo names no tool '{step.undo_tool}'")
                else:
                    bad_undo = undo.validate_input(step.undo_args)
                    if bad_undo is not None:
                        problems.append(
                            f"{path}: undo arguments are invalid: "
                            f"{bad_undo.message}")
            elif self._leaves_a_mark(contract):
                irreversible.append(f"{path} ({step.tool})")

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
        ledger: list[LedgerEntry] = []
        _build_ledger(plan, "", 0, ledger)
        result = RunResult(plan.goal, False, ledger, ctx.trace_id)
        index = {e.path: e for e in ledger}

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

        self._nested_undone: list[str] = []
        self._nested_stuck: list[str] = []
        failure, completed = self._execute(plan, "", 0, ctx, approve_tool,
                                           index)

        if failure is None:
            result.ok = True
            self._seal("orchestrator.done",
                       {"goal": plan.goal, "trace_id": ctx.trace_id,
                        "ok": True})
            return result

        for entry in ledger:
            if entry.status == PENDING:
                entry.status = SKIPPED
                entry.detail = f"stopped after {failure.path} failed"

        undone, stuck = self._rollback(completed, ctx, approve_tool)
        undone = tuple(self._nested_undone) + undone
        stuck = tuple(self._nested_stuck) + stuck
        result.rolled_back = undone
        result.irreversible = stuck
        result.escalated = tuple(e.path for e in ledger
                                 if e.status == ESCALATED)
        self._seal("orchestrator.done", {
            "goal": plan.goal, "trace_id": ctx.trace_id, "ok": False,
            "failed_step": failure.path, "rolled_back": list(undone),
            "irreversible": list(stuck),
            "escalated": list(result.escalated)})
        return result

    def _execute(self, plan: Plan, prefix: str, depth: int,
                 ctx: TraceContext, approve_tool, index: dict
                 ) -> tuple[LedgerEntry | None, list["_Undoable"]]:
        """Run one plan's steps. Returns the failure and what can be undone.

        A sub-saga that fails rolls its own children back before returning,
        so by the time the parent sees the failure the child is already in
        a known state.
        """
        completed: list[_Undoable] = []
        for step in plan.steps:
            path = f"{prefix}{step.id}"
            entry = index[path]
            entry.status = RUNNING
            self._seal("orchestrator.step", {
                "step": step.id, "path": path, "depth": depth,
                "tool": step.tool, "why": step.why, "nested": step.nested,
                "trace_id": ctx.trace_id})

            if step.nested:
                inner_failure, inner_done = self._execute(
                    step.sub, f"{path}/", depth + 1, ctx, approve_tool, index)
                if inner_failure is None:
                    entry.status = DONE
                    entry.detail = f"{len(step.sub.steps)} step(s)"
                    self._seal("orchestrator.step.done",
                               {"step": step.id, "path": path,
                                "status": entry.status,
                                "trace_id": ctx.trace_id,
                                "detail": entry.detail})
                    completed.append(_Undoable(step, entry, inner_done))
                    continue
                # The child cleaned up after itself; the parent only needs
                # to know that this step did not happen. What the child
                # undid still belongs in the run's result -- a rollback
                # nobody is told about is indistinguishable from none.
                child_undone, child_stuck = self._rollback(
                    inner_done, ctx, approve_tool)
                self._nested_undone.extend(child_undone)
                self._nested_stuck.extend(child_stuck)
                entry.status = FAILED
                entry.detail = f"sub-plan failed at {inner_failure.path}"
                self._seal("orchestrator.step.done",
                           {"step": step.id, "path": path, "status": FAILED,
                            "trace_id": ctx.trace_id, "detail": entry.detail})
                return entry, completed

            started = time.time()
            call = self.dispatcher.call(step.tool, step.args, trace=ctx,
                                        approve=approve_tool or (
                                            lambda c, a: True))
            entry.attempts = call.attempts
            entry.duration = time.time() - started
            entry.trace_id = call.trace_id
            entry.error_code = call.error.code if call.error else ""

            passed, why = step.expect.check(call)
            entry.status = DONE if passed else FAILED
            if not passed:
                entry.detail = why
                entry.recovery = self._disposition(step, call, entry)
                # The step that failed may still have changed something --
                # a write that landed and then failed its check is the
                # ordinary case -- so its compensation belongs in the
                # rollback, unless the playbook says we cannot know.
                if entry.recovery == recovery.COMPENSATE and (
                        call.ok or step.reversible):
                    completed.append(_Undoable(step, entry))
                elif entry.recovery == recovery.ESCALATE:
                    entry.status = ESCALATED
            # Sealed after the disposition, so the log carries the status
            # the run actually ended on rather than an intermediate one.
            self._seal("orchestrator.step.done", {
                "step": step.id, "path": path, "status": entry.status,
                "trace_id": call.trace_id, "detail": entry.detail,
                "error_code": entry.error_code,
                "recovery": entry.recovery})
            if not passed:
                return entry, completed

            completed.append(_Undoable(step, entry))
        return None, completed

    def _disposition(self, step: Step, call: ToolResult,
                     entry: LedgerEntry) -> str:
        """What the taxonomy says to do about this particular failure.

        A verification failure is not an error code: the call ran and we
        watched it, so we know what landed and can undo it. An error code
        is where the playbooks earn their keep -- a timeout on a call that
        cannot be repeated is the case where undoing and retrying are both
        wrong, and only a human can find out what actually happened.
        """
        if call.error is None:
            return recovery.COMPENSATE
        contract = self.dispatcher.contract(step.tool)
        context = recovery.Context(
            idempotent=(contract is not None
                        and contract.idempotency == IDEMPOTENT),
            has_compensation=step.reversible,
            can_ask_human=self.approve is not None,
            attempts=call.attempts,
            max_attempts=(contract.retry.max_attempts if contract else 1),
            approval_refused=call.approved is False)
        verdict = recovery.plan(call.error, context)
        entry.detail = f"{entry.detail}; {verdict.reason}".strip("; ")
        # RETRY here means the dispatcher already spent its attempts: the
        # plan level has nothing further to try, so it becomes a question
        # for a person rather than another identical call.
        return (recovery.ESCALATE if verdict.strategy == recovery.RETRY
                else verdict.strategy)

    def _rollback(self, completed: list["_Undoable"], ctx, approve_tool
                  ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Undo completed work in reverse. Reports what it could not."""
        undone: list[str] = []
        stuck: list[str] = []
        for item in reversed(completed):
            step, entry = item.step, item.entry
            failed = entry.status == FAILED

            if step.nested:
                # A saga's compensation is its children's, in reverse.
                inner_undone, inner_stuck = self._rollback(
                    item.children, ctx, approve_tool)
                undone.extend(inner_undone)
                stuck.extend(inner_stuck)
                entry.status = COMPENSATED if not inner_stuck else IRREVERSIBLE
                entry.detail = (f"{len(inner_undone)} child step(s) undone"
                                if not inner_stuck else
                                f"{len(inner_stuck)} child step(s) left")
                continue

            if not step.reversible:
                if not failed:
                    entry.status = IRREVERSIBLE
                    entry.detail = "no compensation was declared"
                else:
                    entry.detail += "; no compensation was declared"
                stuck.append(entry.path)
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
                undone.append(entry.path)
            else:
                reason = call.error.message if call.error else "?"
                if failed:
                    entry.detail += f"; NOT undone: {reason}"
                else:
                    entry.status = IRREVERSIBLE
                    entry.detail = f"undo failed: {reason}"
                stuck.append(entry.path)
            self._seal("orchestrator.rollback", {
                "step": step.id, "path": entry.path, "tool": step.undo_tool,
                "ok": call.ok, "trace_id": ctx.trace_id})
        return tuple(undone), tuple(stuck)


@dataclass
class _Undoable:
    """A piece of completed work and how to take it back."""
    step: Step
    entry: LedgerEntry
    children: list["_Undoable"] = field(default_factory=list)


def _build_ledger(plan: Plan, prefix: str, depth: int,
                  out: list[LedgerEntry]) -> None:
    """One entry per step, sub-sagas included, in execution order."""
    for step in plan.steps:
        path = f"{prefix}{step.id}"
        out.append(LedgerEntry(step.id, step.tool, path=path, depth=depth))
        if step.nested:
            _build_ledger(step.sub, f"{path}/", depth + 1, out)


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

@dataclass
class Replay:
    """A run reconstructed from the event log, for a post-mortem.

    Deterministic by construction: it reads only sealed events, in seq
    order, and computes nothing. Two replays of the same log are the same
    replay, which is the property that makes it usable as evidence.
    """
    trace_id: str
    goal: str = ""
    found: bool = False
    ok: bool | None = None
    ledger: tuple[LedgerEntry, ...] = ()
    rolled_back: tuple[str, ...] = ()
    irreversible: tuple[str, ...] = ()
    escalated: tuple[str, ...] = ()
    failed_step: str = ""
    events: int = 0

    def entry(self, path: str) -> LedgerEntry | None:
        for e in self.ledger:
            if e.path == path or e.step == path:
                return e
        return None

    def to_dict(self) -> dict:
        return {"trace_id": self.trace_id, "goal": self.goal,
                "found": self.found, "ok": self.ok, "events": self.events,
                "failed_step": self.failed_step,
                "ledger": [e.to_dict() for e in self.ledger],
                "rolled_back": list(self.rolled_back),
                "irreversible": list(self.irreversible),
                "escalated": list(self.escalated)}

    def format(self) -> str:
        if not self.found:
            return f"REPLAY — nothing sealed under trace {self.trace_id}"
        verdict = "ok" if self.ok else ("failed" if self.ok is False
                                        else "incomplete")
        lines = [f"REPLAY {verdict} — {self.goal}",
                 f"  trace {self.trace_id} · {self.events} event(s)"]
        for e in self.ledger:
            detail = f" — {e.detail}" if e.detail else ""
            name = ("  " * e.depth) + e.path.rsplit("/", 1)[-1]
            lines.append(f"  {e.status:>11}  {name:<20} "
                         f"{e.tool or '(saga)'}{detail}")
        if self.failed_step:
            lines.append(f"  stopped at {self.failed_step}")
        if self.escalated:
            lines.append("  needs a human: " + ", ".join(self.escalated))
        return "\n".join(lines)


ORCHESTRATOR_EVENTS = ("orchestrator.plan", "orchestrator.step",
                       "orchestrator.step.done", "orchestrator.rollback",
                       "orchestrator.refused", "orchestrator.done")


def replay(log, trace_id: str) -> Replay:
    """Rebuild one run from the event log, by its trace id."""
    out = Replay(trace_id)
    entries: dict[str, LedgerEntry] = {}
    order: list[str] = []
    rolled: list[str] = []

    for ev in log.events():
        if ev.type not in ORCHESTRATOR_EVENTS:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("trace_id") != trace_id:
            continue
        out.events += 1

        if ev.type == "orchestrator.plan":
            out.found = True
            out.goal = str(data.get("goal", ""))
            if not data.get("ok", True):
                out.ok = False
        elif ev.type == "orchestrator.step":
            path = str(data.get("path") or data.get("step") or "")
            if path not in entries:
                entries[path] = LedgerEntry(
                    str(data.get("step") or path), str(data.get("tool") or ""),
                    status=RUNNING, path=path, depth=int(data.get("depth", 0)))
                order.append(path)
        elif ev.type == "orchestrator.step.done":
            path = str(data.get("path") or data.get("step") or "")
            entry = entries.get(path)
            if entry is None:
                entry = LedgerEntry(str(data.get("step") or path), "",
                                    path=path)
                entries[path] = entry
                order.append(path)
            entry.status = str(data.get("status") or entry.status)
            entry.detail = str(data.get("detail") or "")
            entry.error_code = str(data.get("error_code") or "")
            entry.recovery = str(data.get("recovery") or "")
        elif ev.type == "orchestrator.rollback":
            path = str(data.get("path") or data.get("step") or "")
            entry = entries.get(path)
            if entry is not None and data.get("ok"):
                entry.status = (entry.status if entry.status == FAILED
                                else COMPENSATED)
                rolled.append(path)
        elif ev.type == "orchestrator.refused":
            out.found = True
            out.ok = False
        elif ev.type == "orchestrator.done":
            out.ok = bool(data.get("ok"))
            out.failed_step = str(data.get("failed_step") or "")
            out.rolled_back = tuple(data.get("rolled_back") or rolled)
            out.irreversible = tuple(data.get("irreversible") or ())
            out.escalated = tuple(data.get("escalated") or ())

    out.ledger = tuple(entries[p] for p in order)
    if not out.rolled_back:
        out.rolled_back = tuple(rolled)
    return out


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

        # --- a nested saga cleans up after itself -------------------------
        inner_a = os.path.join(workdir, "inner_a.txt")
        inner_b = os.path.join(workdir, "inner_b.txt")
        outer_a = os.path.join(workdir, "outer_a.txt")

        def writer(step_id, path, expect_text=None):
            return Step(step_id, "write_file",
                        {"path": path, "content": "x\n"},
                        expect=Expectation(path_exists=(path,))
                        if expect_text is None
                        else Expectation(contains=(expect_text,)),
                        undo_tool="delete_path", undo_args={"path": path})

        nested_ok = Plan("outer", (
            writer("outer-a", outer_a),
            Step("inner", sub=Plan("inner", (
                writer("inner-a", inner_a),
                writer("inner-b", inner_b),
            ))),
        ))
        deep = orch.run(nested_ok)
        assert deep.ok, deep.format()
        assert all(os.path.exists(p) for p in (outer_a, inner_a, inner_b))
        assert deep.entry("inner/inner-b").depth == 1, deep.format()
        assert deep.entry("inner").tool == "", "a saga makes no call of its own"

        for path in (outer_a, inner_a, inner_b):
            os.remove(path)

        # a sub-saga that fails rolls back its own children, and the
        # parent then rolls back everything it had completed
        nested_bad = Plan("outer", (
            writer("outer-a", outer_a),
            Step("inner", sub=Plan("inner", (
                writer("inner-a", inner_a),
                writer("inner-b", inner_b, expect_text="never written"),
            ))),
            writer("never", os.path.join(workdir, "never.txt")),
        ))
        broke = orch.run(nested_bad)
        assert not broke.ok, broke.format()
        assert broke.entry("inner").status == FAILED
        assert broke.entry("inner/inner-a").status == COMPENSATED, broke.format()
        assert broke.entry("outer-a").status == COMPENSATED, broke.format()
        assert broke.entry("never").status == SKIPPED
        for path in (outer_a, inner_a, inner_b):
            assert not os.path.exists(path), f"{path} survived the rollback"

        # a sub-plan naming a tool that does not exist is refused whole
        bogus = orch.run(Plan("outer", (
            writer("outer-a", outer_a),
            Step("inner", sub=Plan("inner", (
                Step("nope", "teleport", {"path": "x"}),))),
        )))
        assert not bogus.ok
        assert any("inner/nope" in p for p in bogus.review.problems), \
            bogus.review.format()
        assert not os.path.exists(outer_a), "the plan half-ran"

        # --- an unrepeatable failure is escalated, not guessed at ---------
        from .toolcontract import NON_IDEMPOTENT, TEXT_OUT, ToolContract
        one_arg = {"type": "object",
                   "properties": {"path": {"type": "string"}},
                   "required": ["path"]}

        def unreachable(**kw):
            raise ConnectionError("the far end went away")

        escalating = Dispatcher(policy=ToolPolicy("developer", log=log,
                                                  roots=(workdir,)),
                                log=log, approve=lambda c, a: True)
        escalating.register_registry(registry, contracts)
        escalating.register(
            ToolContract("one_shot", "cannot be repeated", one_arg,
                         TEXT_OUT, frozenset({"fs.read"}),
                         idempotency=NON_IDEMPOTENT), unreachable)
        risky = Orchestrator(escalating, log=log, approve=lambda p, r: True)

        kept = os.path.join(workdir, "kept.txt")
        unknown = risky.run(Plan("a call we cannot repeat or undo", (
            writer("first", kept),
            Step("one-shot", "one_shot", {"path": "x"}),
        ), accept_irreversible=True))
        assert not unknown.ok, unknown.format()
        shot = unknown.entry("one-shot")
        assert shot.status == ESCALATED, unknown.format()
        assert shot.error_code == "E_UPSTREAM", shot.to_dict()
        assert shot.recovery == "escalate", shot.to_dict()
        assert "one-shot" in unknown.escalated
        assert unknown.entry("first").status == COMPENSATED, unknown.format()
        assert not os.path.exists(kept)

        # --- replay reconstructs a run from the log alone -----------------
        seen = replay(log, deep.trace_id)
        assert seen.found and seen.ok, seen.format()
        assert seen.goal == "outer"
        assert [e.path for e in seen.ledger] == \
            [e.path for e in deep.ledger], seen.format()
        assert replay(log, deep.trace_id).to_dict() == seen.to_dict(), \
            "replay is not deterministic"

        failed_replay = replay(log, broke.trace_id)
        assert failed_replay.ok is False
        assert failed_replay.failed_step == "inner", failed_replay.format()
        assert failed_replay.entry("inner/inner-a").status == COMPENSATED

        escalated_replay = replay(log, unknown.trace_id)
        assert escalated_replay.escalated == ("one-shot",), \
            escalated_replay.format()
        assert escalated_replay.entry("one-shot").status == ESCALATED, \
            "the log must carry the status the run ended on"
        assert escalated_replay.entry("one-shot").recovery == "escalate"

        assert not replay(log, "0" * 16).found

        # --- the ledger reached the event log -----------------------------
        kinds = [e.type for e in log.events()]
        for wanted in ("orchestrator.plan", "orchestrator.step",
                       "orchestrator.step.done", "orchestrator.rollback",
                       "orchestrator.done"):
            assert wanted in kinds, f"{wanted} was never sealed"

        print(out.format())
        print(bad.format())
        print(broke.format())
        print(escalated_replay.format())
        print("ORCHESTRATOR SELF-TEST PASS")
    finally:
        os.chdir(here)
        shutil.rmtree(workdir, ignore_errors=True)

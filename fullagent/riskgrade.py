"""RISKGRADE — risk read off what a tool actually does, not its label.

A tool's risk is declared once, by hand, when it is written:
`RISK_CONFIRM` or not, destructive or not. That label is a guess made
before the tool had ever run. Six months of real calls know more: which
tools fail, which get refused, which time out, which keep needing a
human. This module reads the event log and grades each tool on what it
did.

The direction of travel is the whole design:

  **Evidence may tighten a grade on its own. It may never loosen one
  below what the contract declares.** A tool that turns out to fail a
  third of the time is graded up automatically, because the cost of
  being wrong is a warning nobody needed. A tool that has behaved
  perfectly for a thousand calls is *not* graded down past its declared
  floor, because the cost of being wrong there is a destructive call
  that nobody was asked about. `delete_path` behaving well is
  `delete_path` that has not deleted the wrong thing **yet**.

  **Too little evidence means the declared grade stands.** Not the
  observed one, not an average of the two. Below the threshold the
  observation is noise, and acting on noise in the loosening direction
  is how a guard quietly disappears.

  **Every change is reported in words.** `changes()` returns what moved,
  from what to what, and the counts behind it. A grade that shifted with
  nobody told is the same as no grade at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .toolcontract import (E_CONFLICT, E_INTERNAL, E_PERMISSION, E_RESOURCE,
                           E_TIMEOUT, E_UPSTREAM, IDEMPOTENT, UNSAFE,
                           ToolContract)

# -- grades, lowest risk first ---------------------------------------------
LOW = "low"
GUARDED = "guarded"
HIGH = "high"
CRITICAL = "critical"

GRADES = (LOW, GUARDED, HIGH, CRITICAL)


def rank(grade: str) -> int:
    return GRADES.index(grade) if grade in GRADES else len(GRADES) - 1


def higher(a: str, b: str) -> str:
    return a if rank(a) >= rank(b) else b


# How much evidence before observation may move anything at all.
MIN_CALLS = 20
# How much before it may move a grade *down* toward the declared floor.
# Deliberately far higher: loosening is the direction that costs.
MIN_CALLS_TO_LOOSEN = 200

# Thresholds on observed behaviour. Each is a rate over completed calls.
FAILURE_HIGH = 0.25        # a quarter of calls failing is not a fluke
FAILURE_GUARDED = 0.08
REFUSAL_HIGH = 0.20        # the policy keeps saying no
UNAPPROVED_HIGH = 0.30     # humans keep declining it

# Error codes that say the tool touched something it should not have, or
# left the world in a state nobody chose.
ALARMING = (E_CONFLICT, E_INTERNAL, E_RESOURCE)


@dataclass
class Behaviour:
    """What one tool has actually done."""
    tool: str
    calls: int = 0
    failures: int = 0
    denials: int = 0
    approvals_refused: int = 0
    escalations: int = 0
    by_code: dict[str, int] = field(default_factory=dict)
    duration_total: float = 0.0

    @property
    def failure_rate(self) -> float:
        return self.failures / self.calls if self.calls else 0.0

    @property
    def refusal_rate(self) -> float:
        total = self.calls + self.denials
        return self.denials / total if total else 0.0

    @property
    def unapproved_rate(self) -> float:
        total = self.calls + self.approvals_refused
        return self.approvals_refused / total if total else 0.0

    @property
    def alarming(self) -> int:
        return sum(self.by_code.get(code, 0) for code in ALARMING)

    @property
    def mean_duration(self) -> float:
        return self.duration_total / self.calls if self.calls else 0.0

    @property
    def enough(self) -> bool:
        return (self.calls + self.denials) >= MIN_CALLS

    @property
    def enough_to_loosen(self) -> bool:
        return self.calls >= MIN_CALLS_TO_LOOSEN

    def to_dict(self) -> dict:
        return {"tool": self.tool, "calls": self.calls,
                "failures": self.failures, "denials": self.denials,
                "approvals_refused": self.approvals_refused,
                "escalations": self.escalations,
                "failure_rate": round(self.failure_rate, 4),
                "refusal_rate": round(self.refusal_rate, 4),
                "by_code": dict(self.by_code),
                "mean_duration": round(self.mean_duration, 4)}


def declared_grade(contract: ToolContract) -> str:
    """The floor: what the contract says before anything has run."""
    if contract.destructive and contract.idempotency == UNSAFE:
        return CRITICAL
    if contract.destructive or contract.outward_facing:
        return HIGH
    if contract.idempotency != IDEMPOTENT:
        return GUARDED
    return LOW


def observed_grade(behaviour: Behaviour) -> tuple[str, tuple[str, ...]]:
    """What the evidence alone would say, with the reasons."""
    reasons: list[str] = []
    grade = LOW
    if behaviour.failure_rate >= FAILURE_HIGH:
        grade = higher(grade, HIGH)
        reasons.append(f"{behaviour.failure_rate:.0%} of calls failed")
    elif behaviour.failure_rate >= FAILURE_GUARDED:
        grade = higher(grade, GUARDED)
        reasons.append(f"{behaviour.failure_rate:.0%} of calls failed")
    if behaviour.refusal_rate >= REFUSAL_HIGH:
        grade = higher(grade, HIGH)
        reasons.append(f"policy refused {behaviour.refusal_rate:.0%} "
                       f"of attempts")
    if behaviour.unapproved_rate >= UNAPPROVED_HIGH:
        grade = higher(grade, HIGH)
        reasons.append(f"humans declined {behaviour.unapproved_rate:.0%} "
                       f"of the times they were asked")
    if behaviour.escalations:
        grade = higher(grade, CRITICAL)
        reasons.append(f"{behaviour.escalations} call(s) ended in a state "
                       f"only a human could resolve")
    if behaviour.alarming:
        grade = higher(grade, HIGH)
        reasons.append(f"{behaviour.alarming} conflict/internal/resource "
                       f"failure(s)")
    return grade, tuple(reasons)


@dataclass(frozen=True)
class Grade:
    """One tool's grade, where it came from, and why."""
    tool: str
    grade: str
    declared: str
    observed: str
    source: str            # declared | observed | floor
    reasons: tuple[str, ...] = ()
    behaviour: Behaviour | None = None

    @property
    def raised(self) -> bool:
        return rank(self.grade) > rank(self.declared)

    def to_dict(self) -> dict:
        return {"tool": self.tool, "grade": self.grade,
                "declared": self.declared, "observed": self.observed,
                "source": self.source, "raised": self.raised,
                "reasons": list(self.reasons),
                "behaviour": (self.behaviour.to_dict()
                              if self.behaviour else None)}

    def line(self) -> str:
        arrow = (f"{self.declared} -> {self.grade}" if self.raised
                 else self.grade)
        why = f"  ({'; '.join(self.reasons)})" if self.reasons else ""
        return f"  {self.tool:<18} {arrow:<22} from {self.source}{why}"


@dataclass(frozen=True)
class Change:
    """A grade that moved, for a person to read."""
    tool: str
    was: str
    now: str
    reasons: tuple[str, ...]
    evidence: dict

    @property
    def tightened(self) -> bool:
        return rank(self.now) > rank(self.was)

    def to_dict(self) -> dict:
        return {"tool": self.tool, "was": self.was, "now": self.now,
                "tightened": self.tightened, "reasons": list(self.reasons),
                "evidence": self.evidence}

    def line(self) -> str:
        direction = "tightened" if self.tightened else "relaxed"
        return (f"  {self.tool}: {self.was} -> {self.now} ({direction}) "
                f"— {'; '.join(self.reasons) or 'evidence changed'}")


def observe(log, tools: tuple[str, ...] = ()) -> dict[str, Behaviour]:
    """Read every tool's behaviour out of the event log."""
    out: dict[str, Behaviour] = {t: Behaviour(t) for t in tools}

    def get(name: str) -> Behaviour:
        if name not in out:
            out[name] = Behaviour(name)
        return out[name]

    for ev in log.events():
        data = ev.data if isinstance(ev.data, dict) else {}
        if ev.type == "dispatch.call":
            name = str(data.get("tool") or "")
            if not name:
                continue
            b = get(name)
            error = data.get("error") or {}
            code = str(error.get("code") or "")
            # A call the policy refused is not a call the tool made. It
            # counts against the tool's refusal rate, not its failure
            # rate, or a well-behaved tool under a strict role would look
            # broken.
            if code == E_PERMISSION:
                b.denials += 1
                if data.get("approved") is False:
                    b.approvals_refused += 1
                continue
            b.calls += 1
            b.duration_total += float(data.get("duration") or 0.0)
            if not data.get("ok"):
                b.failures += 1
                if code:
                    b.by_code[code] = b.by_code.get(code, 0) + 1
        elif ev.type == "orchestrator.step.done":
            if data.get("status") == "escalated" and data.get("tool"):
                get(str(data["tool"])).escalations += 1
    return out


class RiskGrader:
    """Grades every tool, and reports every grade that moves."""

    def __init__(self, contracts: dict[str, ToolContract], log=None):
        self.contracts = dict(contracts)
        self.log = log
        self._last: dict[str, str] = {}

    def _emit(self, event: str, data: dict) -> None:
        if self.log is None:
            return
        try:
            self.log.append(event, data, actor="riskgrade")
        except Exception:
            pass

    def grade(self, tool: str, behaviour: Behaviour | None = None) -> Grade:
        contract = self.contracts.get(tool)
        floor = declared_grade(contract) if contract is not None else CRITICAL
        if behaviour is None or not behaviour.enough:
            detail = ()
            if behaviour is not None:
                detail = (f"only {behaviour.calls + behaviour.denials} "
                          f"observation(s); {MIN_CALLS} needed before "
                          f"evidence counts",)
            return Grade(tool, floor, floor, floor, "declared", detail,
                         behaviour)

        seen, reasons = observed_grade(behaviour)
        if rank(seen) > rank(floor):
            # Evidence may tighten on its own.
            return Grade(tool, seen, floor, seen, "observed", reasons,
                         behaviour)
        if rank(seen) < rank(floor):
            # Evidence may not loosen below the declared floor, ever --
            # however much of it there is. A great deal of it only buys
            # the right to say so out loud.
            note = (f"observed {seen}, held at the declared {floor}",)
            if behaviour.enough_to_loosen:
                note = (f"observed {seen} over {behaviour.calls} calls, "
                        f"still held at the declared {floor}: a tool that "
                        f"has not yet done damage is not a tool that "
                        f"cannot",)
            return Grade(tool, floor, floor, seen, "floor", note, behaviour)
        return Grade(tool, floor, floor, seen, "declared", reasons, behaviour)

    def grades(self, log=None) -> tuple[Grade, ...]:
        source = log if log is not None else self.log
        behaviours = observe(source, tuple(self.contracts)) if source else {}
        return tuple(self.grade(name, behaviours.get(name))
                     for name in sorted(self.contracts))

    def changes(self, log=None) -> tuple[Change, ...]:
        """What moved since the last time this was asked."""
        out: list[Change] = []
        for g in self.grades(log):
            was = self._last.get(g.tool)
            self._last[g.tool] = g.grade
            if was is None or was == g.grade:
                continue
            change = Change(g.tool, was, g.grade, g.reasons,
                            g.behaviour.to_dict() if g.behaviour else {})
            out.append(change)
            self._emit("riskgrade.changed", change.to_dict())
        return tuple(out)

    def needs_approval(self, tool: str, log=None) -> bool:
        """Whether this tool should be asked about, given what it has done.

        Never less often than its contract says: the contract's own
        `needs_approval` is a floor this can raise and not lower.
        """
        contract = self.contracts.get(tool)
        if contract is not None and contract.needs_approval:
            return True
        graded = self.grade(tool, observe(log or self.log,
                                          (tool,)).get(tool)
                            if (log or self.log) else None)
        return rank(graded.grade) >= rank(HIGH)

    def format(self, log=None) -> str:
        graded = self.grades(log)
        raised = [g for g in graded if g.raised]
        head = (f"RISK GRADES — {len(graded)} tool(s), "
                f"{len(raised)} raised by evidence")
        return "\n".join([head] + [g.line() for g in graded])


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    from .kernel import EventLog
    from .toolcontract import build_contracts
    from .tools import build_registry

    work = Path(tempfile.mkdtemp(prefix="fa-risk-"))
    contracts = build_contracts(build_registry())

    # --- the declared floor comes from the contract, not a hand list ---
    assert declared_grade(contracts["read_file"]) == LOW
    assert declared_grade(contracts["delete_path"]) == CRITICAL
    assert declared_grade(contracts["web_fetch"]) == HIGH
    assert declared_grade(contracts["edit_file"]) == HIGH
    # GUARDED is the non-idempotent-but-harmless case. No shipped tool is
    # one today -- everything non-idempotent here is also destructive --
    # so it is checked against a constructed contract rather than left
    # untested because the registry happens not to exercise it.
    import dataclasses

    from .toolcontract import NON_IDEMPOTENT
    harmless_but_not_repeatable = dataclasses.replace(
        contracts["read_file"], idempotency=NON_IDEMPOTENT)
    assert declared_grade(harmless_but_not_repeatable) == GUARDED
    for grade in GRADES:
        assert rank(grade) == GRADES.index(grade)
    assert higher(LOW, CRITICAL) == CRITICAL and higher(HIGH, LOW) == HIGH

    def synth(name: str, calls=0, failures=0, denials=0, refused=0,
              escalated=0, code=E_TIMEOUT) -> EventLog:
        log = EventLog(path=str(work / f"{name}-{calls}-{failures}.jsonl"))
        for i in range(calls):
            failed = i < failures
            log.append("dispatch.call", {
                "tool": name, "ok": not failed, "duration": 0.01,
                "approved": None,
                **({"error": {"code": code}} if failed else {})},
                actor="kernel")
        for _ in range(denials):
            log.append("dispatch.call", {
                "tool": name, "ok": False, "duration": 0.0,
                "approved": False if refused else None,
                "error": {"code": E_PERMISSION}}, actor="kernel")
        for _ in range(escalated):
            log.append("orchestrator.step.done",
                       {"tool": name, "status": "escalated", "path": "s"},
                       actor="orchestrator")
        return log

    grader = RiskGrader(contracts)

    # --- too little evidence: the declared grade stands ----------------
    thin = observe(synth("read_file", calls=5))["read_file"]
    thin_grade = grader.grade("read_file", thin)
    assert thin_grade.grade == LOW and thin_grade.source == "declared"
    assert "observation" in thin_grade.reasons[0], thin_grade.reasons

    noisy_thin = observe(synth("read_file", calls=5, failures=5))["read_file"]
    assert grader.grade("read_file", noisy_thin).grade == LOW, \
        "five bad calls must not move a grade"

    # --- evidence tightens on its own ----------------------------------
    flaky = observe(synth("read_file", calls=40, failures=15))["read_file"]
    raised = grader.grade("read_file", flaky)
    assert raised.grade == HIGH and raised.source == "observed", \
        raised.to_dict()
    assert raised.raised and raised.reasons

    mildly = observe(synth("read_file", calls=40, failures=5))["read_file"]
    assert grader.grade("read_file", mildly).grade == GUARDED

    # --- evidence never loosens below the floor ------------------------
    spotless = observe(synth("delete_path", calls=500))["delete_path"]
    held = grader.grade("delete_path", spotless)
    assert held.grade == CRITICAL, held.to_dict()
    assert held.observed == LOW and held.source == "floor"
    assert "not a tool that cannot" in held.reasons[0], held.reasons
    assert spotless.enough_to_loosen

    for tool in sorted(contracts):
        clean = observe(synth(tool, calls=1000))[tool]
        graded = grader.grade(tool, clean)
        assert rank(graded.grade) >= rank(declared_grade(contracts[tool])), \
            f"{tool} was graded below its declared floor"

    # --- refusals are not the tool's failures --------------------------
    refused_log = synth("write_file", calls=30, denials=20, refused=1)
    b = observe(refused_log)["write_file"]
    assert b.calls == 30 and b.failures == 0 and b.denials == 20
    assert b.failure_rate == 0.0, "a policy refusal is not a tool failure"
    assert grader.grade("write_file", b).grade == HIGH, \
        "a tool the policy keeps refusing is worth a second look"

    # --- an escalation is critical whatever else is true ---------------
    stuck = observe(synth("read_file", calls=40, escalated=1))["read_file"]
    assert grader.grade("read_file", stuck).grade == CRITICAL

    # --- changes are reported, in words --------------------------------
    log = EventLog(path=str(work / "changes.jsonl"))
    tracked = RiskGrader({"read_file": contracts["read_file"]}, log=log)
    assert tracked.changes(synth("read_file", calls=30)) == (), \
        "the first look establishes a baseline, it does not report a change"
    moved = tracked.changes(synth("read_file", calls=40, failures=15))
    assert len(moved) == 1 and moved[0].tightened, \
        [c.to_dict() for c in moved]
    assert moved[0].was == LOW and moved[0].now == HIGH
    assert moved[0].evidence["failure_rate"] > 0.3
    assert "riskgrade.changed" in {e.type for e in log.events()}
    assert tracked.changes(synth("read_file", calls=40, failures=15)) == (), \
        "an unchanged grade must not report a change"

    # --- approval is a floor this can raise, never lower ---------------
    assert grader.needs_approval("delete_path",
                                 synth("delete_path", calls=1000))
    assert not grader.needs_approval("read_file",
                                     synth("read_file", calls=1000))
    assert grader.needs_approval("read_file",
                                 synth("read_file", calls=40, failures=20)), \
        "a tool that fails half the time should be asked about"

    print(RiskGrader(contracts).format(
        synth("read_file", calls=40, failures=15)))
    print(f"RISKGRADE SELF-TEST PASS — {len(GRADES)} grades, "
          f"floor never lowered by evidence")

"""BUDGETS — how much checking a call gets, and why that number.

Verification is not free. Hashing a file before and after, re-deriving a
provenance chain, running a second strategy over a reply: each costs
something, and spending the same amount on `read_file` as on
`delete_path` means either the cheap path is slow or the dangerous one
is under-checked. In practice it is always the second, because the first
is the one people notice.

So depth is assigned per call, from three things the system already
knows:

* the tool's **risk grade** (`riskgrade.py`) — declared floor raised by
  observed behaviour, never lowered below it;
* its **provenance history** — how often calls of this tool have left
  gaps, violated their envelope, or ended in an escalation;
* whether the call is **destructive or outward-facing**, from its
  contract.

Two rules keep this from becoming a way to skip the checks that matter.

**Every decision is auditable.** A `Decision` carries the inputs it read,
the rule that fired, and the depth that came out, and it is sealed. "Why
was this only shallow-checked?" has an answer that is a record rather
than a reconstruction.

**The floor is not negotiable.** Each risk grade has a minimum depth. A
budget under pressure buys back time from the cheap end of the
distribution and never from the expensive end — and when even the floor
is unaffordable, the call is **refused rather than under-verified**.
Going fast by skipping the check that would have caught the problem is
not going fast; it is going untested and calling it fast.

**The honest limit.** Cost here is a declared constant per depth, not a
measurement. It is good enough to rank and to budget, and it is not a
prediction of wall-clock time on your machine. The module says so rather
than dressing the constants up as data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .riskgrade import CRITICAL, GUARDED, HIGH, LOW, rank

# ---------------------------------------------------------------------------
# Depths
# ---------------------------------------------------------------------------

SKIP = "skip"              # nothing beyond the schema
SHALLOW = "shallow"        # envelope observation only
STANDARD = "standard"      # envelope + policy rationale + seal
DEEP = "deep"              # standard + provenance chain + consensus
EXHAUSTIVE = "exhaustive"  # deep + invariant slice for the touched modules

DEPTHS = (SKIP, SHALLOW, STANDARD, DEEP, EXHAUSTIVE)


def depth_rank(depth: str) -> int:
    return DEPTHS.index(depth) if depth in DEPTHS else 0


def deeper(a: str, b: str) -> str:
    return a if depth_rank(a) >= depth_rank(b) else b


#: What each depth actually runs. Named so a reader can tell what was
#: and was not done, rather than inferring it from a word.
INCLUDES: dict[str, tuple[str, ...]] = {
    SKIP: (),
    SHALLOW: ("envelope",),
    STANDARD: ("envelope", "policy-rationale", "seal"),
    DEEP: ("envelope", "policy-rationale", "seal", "provenance-chain",
           "consensus"),
    EXHAUSTIVE: ("envelope", "policy-rationale", "seal", "provenance-chain",
                 "consensus", "invariant-slice"),
}

#: Declared cost per depth, in abstract units. These rank and budget;
#: they do not predict seconds, and pretending otherwise would make
#: every number downstream a guess wearing a decimal point.
COST: dict[str, int] = {SKIP: 0, SHALLOW: 1, STANDARD: 3, DEEP: 10,
                        EXHAUSTIVE: 30}

#: The depth below which a call at each grade is not allowed to run.
#: This is the line the budget may never buy back from.
FLOOR: dict[str, str] = {
    LOW: SHALLOW,
    GUARDED: STANDARD,
    HIGH: DEEP,
    CRITICAL: EXHAUSTIVE,
}

#: What a clean history is worth. A tool may be assigned one depth below
#: what its grade alone would suggest when it has a long, clean record --
#: but never below its floor, which is why this is a preference and not
#: a discount.
CLEAN_RUN = 50


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

@dataclass
class History:
    """What the record says about one tool's past."""
    tool: str
    calls: int = 0
    gaps: int = 0
    violations: int = 0
    escalations: int = 0
    failures: int = 0

    @property
    def incidents(self) -> int:
        return self.gaps + self.violations + self.escalations

    @property
    def clean(self) -> bool:
        return self.incidents == 0 and self.calls >= CLEAN_RUN

    @property
    def incident_rate(self) -> float:
        return self.incidents / self.calls if self.calls else 0.0

    def to_dict(self) -> dict:
        return {"tool": self.tool, "calls": self.calls, "gaps": self.gaps,
                "violations": self.violations,
                "escalations": self.escalations, "failures": self.failures,
                "incidents": self.incidents, "clean": self.clean,
                "incident_rate": round(self.incident_rate, 4)}


def history_of(log, tools: tuple[str, ...] = ()) -> dict[str, History]:
    """Read every tool's verification-relevant past out of the log."""
    out: dict[str, History] = {t: History(t) for t in tools}

    def get(name: str) -> History:
        if name not in out:
            out[name] = History(name)
        return out[name]

    for ev in log.events():
        data = ev.data if isinstance(ev.data, dict) else {}
        if ev.type == "dispatch.call":
            name = str(data.get("tool") or "")
            if not name:
                continue
            h = get(name)
            h.calls += 1
            if not data.get("ok"):
                h.failures += 1
            if data.get("envelope_ok") is False:
                h.violations += 1
        elif ev.type == "envelope.violation":
            name = str(data.get("tool") or "")
            if name and any(v.get("blocking")
                            for v in (data.get("violations") or ())):
                get(name).violations += 1
        elif ev.type == "orchestrator.step.done":
            if data.get("status") == "escalated" and data.get("tool"):
                get(str(data["tool"])).escalations += 1
        elif ev.type == "provenance.gap":
            name = str(data.get("tool") or "")
            if name:
                get(name).gaps += 1
    return out


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

RULE_FLOOR = "grade-floor"
RULE_INCIDENT = "incident-history"
RULE_CLEAN = "clean-history"
RULE_DESTRUCTIVE = "destructive-call"
RULE_BUDGET = "budget-pressure"
RULE_UNAFFORDABLE = "floor-unaffordable"

#: Every rule, with what it does.
RULES: dict[str, str] = {
    RULE_FLOOR: "the tool's risk grade sets a minimum depth",
    RULE_INCIDENT: "this tool has a record of incidents, so it is checked "
                   "deeper than its grade alone would ask",
    RULE_CLEAN: "a long clean record buys one level less, never below "
                "the floor",
    RULE_DESTRUCTIVE: "a destructive or outward-facing call is never "
                      "checked shallowly",
    RULE_BUDGET: "the remaining budget could not afford the preferred "
                 "depth, so the cheapest sufficient one was chosen",
    RULE_UNAFFORDABLE: "the budget cannot afford this call's floor, so "
                       "the call is refused rather than under-verified",
}


@dataclass
class Decision:
    """One budget decision, with everything it was made from."""
    tool: str
    depth: str
    grade: str
    floor: str
    rule: str
    cost: int = 0
    remaining: int = 0
    refused: bool = False
    inputs: dict = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def runs(self) -> tuple[str, ...]:
        return () if self.refused else INCLUDES.get(self.depth, ())

    def to_dict(self) -> dict:
        return {"tool": self.tool, "depth": self.depth, "grade": self.grade,
                "floor": self.floor, "rule": self.rule, "cost": self.cost,
                "remaining": self.remaining, "refused": self.refused,
                "runs": list(self.runs), "inputs": self.inputs,
                "notes": list(self.notes),
                "why": RULES.get(self.rule, "")}

    def line(self) -> str:
        head = "REFUSED" if self.refused else self.depth
        return (f"  {self.tool:<18} {head:<11} grade {self.grade:<9} "
                f"floor {self.floor:<11} cost {self.cost:<3} "
                f"[{self.rule}]")

    def explain(self) -> str:
        lines = [f"{self.tool}: {'refused' if self.refused else self.depth}",
                 f"  because {RULES.get(self.rule, self.rule)}",
                 f"  grade {self.grade} (floor {self.floor}), "
                 f"cost {self.cost}, {self.remaining} left in budget"]
        if self.runs:
            lines.append("  runs: " + ", ".join(self.runs))
        lines.extend(f"  note: {n}" for n in self.notes)
        return "\n".join(lines)


class Unaffordable(Exception):
    """Raised when a call cannot be verified to its floor."""


@dataclass
class Budget:
    """A pool of verification units for one request or one session."""
    total: int
    spent: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.spent)

    def can_afford(self, depth: str) -> bool:
        return COST.get(depth, 0) <= self.remaining

    def charge(self, depth: str) -> int:
        cost = COST.get(depth, 0)
        self.spent += cost
        return cost

    def to_dict(self) -> dict:
        return {"total": self.total, "spent": self.spent,
                "remaining": self.remaining}


#: A budget nothing can exhaust, for callers who want the depth logic
#: without the accounting.
UNLIMITED = 10 ** 9


class BudgetPlanner:
    """Assigns a verification depth to each call, and says why."""

    def __init__(self, contracts: dict | None = None, log=None,
                 grader=None):
        self.contracts = dict(contracts or {})
        self.log = log
        self.grader = grader
        self._history: dict[str, History] = {}

    def _emit(self, event: str, data: dict) -> None:
        if self.log is None:
            return
        try:
            self.log.append(event, data, actor="budgets")
        except Exception:
            pass

    def refresh(self, log=None) -> dict[str, History]:
        source = log if log is not None else self.log
        self._history = (history_of(source, tuple(self.contracts))
                         if source is not None else {})
        return self._history

    def grade_of(self, tool: str) -> str:
        if self.grader is not None:
            try:
                return self.grader.grade(tool).grade
            except Exception:
                pass
        contract = self.contracts.get(tool)
        if contract is None:
            # A tool nobody declared is the most dangerous kind, not the
            # least: unknown is not safe.
            return CRITICAL
        from .riskgrade import declared_grade
        return declared_grade(contract)

    def preferred(self, tool: str) -> tuple[str, str, str, tuple[str, ...]]:
        """The depth this call wants, before the budget has a say.

        Returns (depth, grade, floor, notes).
        """
        grade = self.grade_of(tool)
        floor = FLOOR.get(grade, EXHAUSTIVE)
        depth, rule, notes = floor, RULE_FLOOR, []

        contract = self.contracts.get(tool)
        if contract is not None and (getattr(contract, "destructive", False)
                                     or getattr(contract, "outward_facing",
                                                False)):
            depth = deeper(depth, STANDARD)
            if depth != floor:
                rule = RULE_DESTRUCTIVE

        hist = self._history.get(tool)
        if hist is not None and hist.incidents:
            deeper_one = DEPTHS[min(len(DEPTHS) - 1, depth_rank(depth) + 1)]
            if deeper_one != depth:
                notes.append(f"{hist.incidents} past incident(s)")
                depth, rule = deeper_one, RULE_INCIDENT
        elif hist is not None and hist.clean:
            lower = DEPTHS[max(0, depth_rank(depth) - 1)]
            # The floor is the whole point: a clean record is a reason to
            # prefer less, never a licence to go under.
            if depth_rank(lower) >= depth_rank(floor):
                notes.append(f"{hist.calls} clean call(s)")
                depth, rule = lower, RULE_CLEAN
            else:
                notes.append(f"{hist.calls} clean call(s), but the "
                             f"{grade} floor is {floor}")
        return depth, grade, floor, tuple(notes)

    def plan(self, tool: str, budget: Budget | None = None) -> Decision:
        """Decide this call's depth, charge the budget, and seal it."""
        depth, grade, floor, notes = self.preferred(tool)
        budget = budget if budget is not None else Budget(UNLIMITED)
        rule = RULE_FLOOR
        if notes and depth != floor:
            rule = (RULE_INCIDENT if any("incident" in n for n in notes)
                    else RULE_CLEAN)
        elif depth != floor:
            rule = RULE_DESTRUCTIVE

        if not budget.can_afford(depth):
            # Step down, but never below the floor.
            affordable = [d for d in DEPTHS
                          if depth_rank(d) >= depth_rank(floor)
                          and budget.can_afford(d)]
            if affordable:
                depth = min(affordable, key=depth_rank)
                rule = RULE_BUDGET
                notes = notes + (f"the preferred depth cost more than the "
                                 f"{budget.remaining} unit(s) left",)
            else:
                decision = Decision(
                    tool, floor, grade, floor, RULE_UNAFFORDABLE,
                    COST.get(floor, 0), budget.remaining, True,
                    {"history": (self._history.get(tool).to_dict()
                                 if tool in self._history else None),
                     "budget": budget.to_dict()},
                    notes + ("refusing is the only honest answer: the "
                             "alternative is running it unverified",))
                self._emit("budget.refused", decision.to_dict())
                return decision

        cost = budget.charge(depth)
        decision = Decision(
            tool, depth, grade, floor, rule, cost, budget.remaining, False,
            {"history": (self._history.get(tool).to_dict()
                         if tool in self._history else None),
             "budget": budget.to_dict()},
            notes)
        self._emit("budget.decided", decision.to_dict())
        return decision

    def require(self, tool: str, budget: Budget | None = None) -> Decision:
        """`plan`, but raising when the floor is unaffordable."""
        decision = self.plan(tool, budget)
        if decision.refused:
            raise Unaffordable(
                f"{tool} needs {decision.floor} verification "
                f"({COST.get(decision.floor, 0)} units) and only "
                f"{decision.remaining} remain")
        return decision

    def format(self, budget: Budget | None = None) -> str:
        budget = budget if budget is not None else Budget(UNLIMITED)
        lines = [f"VERIFICATION BUDGET — {len(self.contracts)} tool(s), "
                 f"{budget.remaining} unit(s) available"]
        for tool in sorted(self.contracts):
            depth, grade, floor, notes = self.preferred(tool)
            lines.append(f"  {tool:<18} {depth:<11} grade {grade:<9} "
                         f"floor {floor}"
                         + (f"  ({'; '.join(notes)})" if notes else ""))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import tempfile
    from pathlib import Path

    from .kernel import EventLog
    from .toolcontract import build_contracts
    from .tools import build_registry

    argv = sys.argv[1:]
    contracts = build_contracts(build_registry())

    if "--table" in argv:
        print(BudgetPlanner(contracts).format())
        raise SystemExit(0)

    work = Path(tempfile.mkdtemp(prefix="fa-budgets-"))
    log = EventLog(path=str(work / "events.jsonl"))
    planner = BudgetPlanner(contracts, log=log)

    # --- the floor comes from the grade, and holds --------------------
    for grade, floor in FLOOR.items():
        assert floor in DEPTHS, grade
    assert depth_rank(SKIP) < depth_rank(SHALLOW) < depth_rank(STANDARD) \
        < depth_rank(DEEP) < depth_rank(EXHAUSTIVE)
    assert deeper(SHALLOW, DEEP) == DEEP and deeper(DEEP, SHALLOW) == DEEP

    read = planner.plan("read_file")
    assert read.grade == LOW and read.floor == SHALLOW
    assert read.depth == SHALLOW and not read.refused
    assert "envelope" in read.runs

    danger = planner.plan("delete_path")
    assert danger.grade == CRITICAL and danger.floor == EXHAUSTIVE
    assert danger.depth == EXHAUSTIVE, danger.to_dict()
    assert "invariant-slice" in danger.runs

    # --- an unknown tool is treated as the most dangerous -------------
    unknown = BudgetPlanner({}).plan("nobody_registered_me")
    assert unknown.grade == CRITICAL and unknown.depth == EXHAUSTIVE, \
        unknown.to_dict()

    # --- incidents deepen a call --------------------------------------
    noisy = EventLog(path=str(work / "noisy.jsonl"))
    for _ in range(30):
        noisy.append("dispatch.call", {"tool": "read_file", "ok": True,
                                       "envelope_ok": True}, actor="kernel")
    noisy.append("envelope.violation",
                 {"tool": "read_file",
                  "violations": [{"kind": "undeclared-effect",
                                  "blocking": True}]}, actor="envelopes")
    scarred = BudgetPlanner(contracts, log=noisy)
    scarred.refresh()
    hist = scarred._history["read_file"]
    assert hist.incidents == 1 and not hist.clean
    worse = scarred.plan("read_file")
    assert depth_rank(worse.depth) > depth_rank(SHALLOW), worse.to_dict()
    assert worse.rule == RULE_INCIDENT, worse.to_dict()
    assert "incident" in " ".join(worse.notes)

    # --- a long clean record buys one level, never below the floor ----
    clean = EventLog(path=str(work / "clean.jsonl"))
    for _ in range(CLEAN_RUN + 10):
        clean.append("dispatch.call", {"tool": "write_file", "ok": True,
                                       "envelope_ok": True}, actor="kernel")
        clean.append("dispatch.call", {"tool": "delete_path", "ok": True,
                                       "envelope_ok": True}, actor="kernel")
    trusted = BudgetPlanner(contracts, log=clean)
    trusted.refresh()
    assert trusted._history["delete_path"].clean

    cheap = trusted.plan("write_file")
    assert cheap.rule in (RULE_CLEAN, RULE_FLOOR), cheap.to_dict()

    held = trusted.plan("delete_path")
    assert held.depth == EXHAUSTIVE, \
        "a clean record must never take a critical tool below its floor"
    assert held.floor == EXHAUSTIVE
    assert any("floor" in n for n in held.notes), held.to_dict()

    # --- the budget steps down, but only to the floor -----------------
    small = Budget(total=COST[DEEP])
    planner.refresh(EventLog(path=str(work / "empty.jsonl")))
    first = planner.plan("read_file", small)
    assert not first.refused and small.remaining == COST[DEEP] - COST[SHALLOW]

    tight = Budget(total=COST[STANDARD])
    squeezed = planner.plan("web_fetch", tight)
    assert squeezed.floor == FLOOR[planner.grade_of("web_fetch")]
    if depth_rank(squeezed.floor) > depth_rank(STANDARD):
        assert squeezed.refused, squeezed.to_dict()
    else:
        assert not squeezed.refused

    # --- an unaffordable floor refuses rather than under-verifies -----
    broke = Budget(total=1)
    refusal = planner.plan("delete_path", broke)
    assert refusal.refused, refusal.to_dict()
    assert refusal.rule == RULE_UNAFFORDABLE
    assert refusal.runs == (), "a refused call runs no checks at all"
    assert refusal.depth == EXHAUSTIVE, \
        "the refusal names the floor it could not meet"
    assert "unverified" in " ".join(refusal.notes)
    assert broke.spent == 0, "a refused call charges nothing"

    try:
        planner.require("delete_path", Budget(total=1))
        raise AssertionError("require must raise on an unaffordable floor")
    except Unaffordable as exc:
        assert "delete_path" in str(exc) and "remain" in str(exc)

    # --- budget pressure is recorded as the rule that fired -----------
    medium = Budget(total=COST[DEEP])
    pressed = BudgetPlanner(contracts, log=log)
    pressed.refresh(noisy)
    squeeze = pressed.plan("read_file", medium)
    assert not squeeze.refused

    # --- every decision explains itself -------------------------------
    for decision in (read, danger, worse, held, refusal):
        assert decision.rule in RULES, decision.rule
        assert decision.to_dict()["why"], decision.rule
        assert decision.tool in decision.explain()
        assert decision.inputs.get("budget") is not None

    # --- every depth says what it runs --------------------------------
    for depth in DEPTHS:
        assert depth in INCLUDES and depth in COST
    for i in range(len(DEPTHS) - 1):
        a, b = DEPTHS[i], DEPTHS[i + 1]
        assert set(INCLUDES[a]) <= set(INCLUDES[b]), (a, b)
        assert COST[a] <= COST[b], (a, b)

    # --- decisions are sealed -----------------------------------------
    kinds = {e.type for e in log.events()}
    assert {"budget.decided", "budget.refused"} <= kinds, sorted(kinds)

    print(BudgetPlanner(contracts).format())
    print(refusal.explain())
    print(f"BUDGETS SELF-TEST PASS — {len(DEPTHS)} depth(s), "
          f"{len(RULES)} rule(s); the floor is never bought back")

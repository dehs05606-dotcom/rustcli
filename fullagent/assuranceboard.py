"""HEADLESS ASSURANCE DASHBOARD — one deterministic answer to "where do we
stand", with every number attached to the thing it came from.

A dashboard is where a compliance stack goes to start lying. Not by
inventing numbers, but by rendering them: a cell that recomputes on view
shows a figure nobody sealed, a cell with no data shows a reassuring zero,
and a percentage on its own turns "three failure classes have never been
exercised" into "97%". Each of those is a small convenience and each one
breaks the chain between what is displayed and what was actually checked.

So this board has one rule, and the whole module is built around enforcing
it: **a number that cannot name the sealed event it came from is not
displayed as a number.** It renders as `unknown`, with the reason. There
is no path through `collect()` that computes a figure on the spot -- it
reads the event log and nothing else. Running the checks is a separate
verb (`refresh()`), it seals what it finds, and only then can the board
show it.

That makes two states distinguishable that a normal dashboard merges:

  * `unknown` -- nobody has run this, so there is nothing to say. Shown
    in the same place and just as loudly as a failure, because "we never
    checked" and "we checked and it was fine" are not close to the same
    fact and the difference is exactly what an auditor is there for.
  * `ok` -- run, sealed, and clean, with the sequence number of the seal
    beside it.

`stale` is the third: a cell whose seal is older than the rules it
describes. A green figure taken against a rule set that has since changed
is worse than no figure, because it looks current.

The board is queryable by key and emits JSON, so it answers a question
without a person reading a screen -- which is the point of "headless":
CI asks it, a script asks it, and both get the same bytes for the same
log.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

# -- cell states -----------------------------------------------------------

OK = "ok"
ATTENTION = "attention"      # run, sealed, and something needs a person
UNKNOWN = "unknown"          # never run, so there is nothing to display
STALE = "stale"              # sealed against a rule set that has moved on

STATES = (OK, ATTENTION, UNKNOWN, STALE)

#: How loud each state is, for sorting and for the exit code. `UNKNOWN`
#: outranks `ATTENTION` on purpose: a known problem has an owner, and an
#: unmeasured surface does not even have that.
LOUDNESS = {OK: 0, STALE: 1, ATTENTION: 2, UNKNOWN: 3}


@dataclass(frozen=True)
class Cell:
    """One figure, and the sealed event it is traceable to."""
    key: str
    label: str
    state: str
    value: str = ""
    detail: str = ""
    #: The event type and sequence number this was read from. Empty for
    #: an `unknown` cell, which is the only kind allowed to have none.
    source: str = ""
    seq: int = -1
    facts: dict = field(default_factory=dict)

    @property
    def traceable(self) -> bool:
        return bool(self.source) and self.seq >= 0

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "state": self.state,
                "value": self.value, "detail": self.detail,
                "source": self.source, "seq": self.seq,
                "traceable": self.traceable, "facts": self.facts}

    def line(self) -> str:
        mark = {OK: "ok  ", ATTENTION: "!!  ", UNKNOWN: "?   ",
                STALE: "~   "}.get(self.state, "    ")
        where = f"  [{self.source}@{self.seq}]" if self.traceable else ""
        tail = f"  {self.detail}" if self.detail else ""
        return f"  {mark}{self.label:<34}{self.value:<22}{tail}{where}"


# ---------------------------------------------------------------------------
# Readers — each one reads the log and nothing else
# ---------------------------------------------------------------------------

def _seq_of(event) -> int:
    value = getattr(event, "seq", None)
    return int(value) if isinstance(value, int) else -1


def _newest(log, event_type: str) -> tuple[dict, int]:
    """The newest sealed event of a type, with its sequence number."""
    data, seq = {}, -1
    for ev in log.events():
        if ev.type == event_type:
            data = ev.data if isinstance(ev.data, dict) else {}
            seq = _seq_of(ev)
    return data, seq


def _unknown(key: str, label: str, what: str) -> Cell:
    return Cell(key, label, UNKNOWN, "unknown",
                f"nothing sealed yet — {what}")


def _cell_assurance(log) -> Cell:
    data, seq = _newest(log, "assurance.case")
    if seq < 0:
        return _unknown("assurance", "Assurance case",
                        "run `python -m fullagent.assurance --check`")
    nodes = data.get("nodes") or []
    defects = data.get("defects") or []
    holds = sum(1 for n in nodes if n.get("status") == "holds")
    assumed = sum(1 for n in nodes if n.get("status") == "assumed")
    unsupported = [n.get("id") for n in nodes
                   if n.get("status") == "unsupported"]
    state = ATTENTION if (defects or unsupported) else OK
    detail = ""
    if defects:
        detail = f"{len(defects)} defect(s): " + ", ".join(
            sorted({str(d.get("kind")) for d in defects})[:3])
    elif unsupported:
        detail = f"unsupported: {', '.join(str(u) for u in unsupported[:3])}"
    return Cell("assurance", "Assurance case", state,
                f"{holds} hold, {assumed} assumed", detail,
                "assurance.case", seq,
                {"nodes": len(nodes), "holds": holds, "assumed": assumed,
                 "defects": len(defects)})


def _cell_faults(log) -> Cell:
    data, seq = _newest(log, "faultcatalogue.run")
    if seq < 0:
        return _unknown("faults", "Failure classes provoked",
                        "run `python -m fullagent.faultcatalogue --check`")
    classes = int(data.get("classes") or 0)
    provoked = int(data.get("provoked") or 0)
    uncovered = [f"{d.get('surface')}/{d.get('subject')}"
                 for d in (data.get("defects") or ())
                 if d.get("kind") == "failure-class-uncovered"]
    unreachable = sum(len(c.get("unreachable") or ())
                      for c in (data.get("coverage") or ()))
    detail = ("uncovered: " + ", ".join(uncovered[:3])) if uncovered else (
        f"{unreachable} declared unreachable" if unreachable else "")
    return Cell("faults", "Failure classes provoked",
                ATTENTION if uncovered else OK,
                f"{provoked}/{classes}", detail,
                "faultcatalogue.run", seq,
                {"classes": classes, "provoked": provoked,
                 "uncovered": uncovered, "unreachable": unreachable})


def _cell_policy_meta(log) -> Cell:
    data, seq = _newest(log, "policymeta.run")
    if seq < 0:
        return _unknown("policy-meta", "Policy meta-properties",
                        "run `python -m fullagent.policymeta --check`")
    proved = int(data.get("proved") or 0)
    total = len(data.get("properties") or ())
    failing = [str(p.get("id")) for p in (data.get("properties") or ())
               if p.get("failures")]
    return Cell("policy-meta", "Policy meta-properties",
                ATTENTION if failing else OK,
                f"{proved} proved of {total}",
                ("failing: " + ", ".join(failing[:3])) if failing else
                f"{int(data.get('cases') or 0)} cases",
                "policymeta.run", seq,
                {"proved": proved, "properties": total,
                 "failing": failing, "digest": data.get("digest", "")})


def _cell_runbooks(log) -> Cell:
    data, seq = _newest(log, "runbook.run")
    if seq < 0:
        return _unknown("runbooks", "Recovery runbooks",
                        "run `python -m fullagent.runbook --check`")
    from .runbook import playbook_digest
    total = int(data.get("runbooks") or 0)
    passed = int(data.get("passed") or 0)
    defects = data.get("defects") or []
    sealed_digest = str(data.get("digest") or "")
    if sealed_digest and sealed_digest != playbook_digest():
        return Cell("runbooks", "Recovery runbooks", STALE,
                    f"{passed}/{total}",
                    "sealed against playbooks that have since changed",
                    "runbook.run", seq, {"digest": sealed_digest})
    return Cell("runbooks", "Recovery runbooks",
                ATTENTION if defects else OK, f"{passed}/{total}",
                (f"{len(defects)} defect(s)" if defects else ""),
                "runbook.run", seq,
                {"passed": passed, "runbooks": total,
                 "defects": len(defects)})


def _cell_strategies(log) -> Cell:
    """Verifiers that have not been measured enough to be trusted.

    Derived from the sealed consensus audits rather than from a stored
    summary, because the audits are the record and a summary of them is
    a second copy to keep in step.
    """
    from .calibration import MIN_AUDITS, calibrate
    audits = [ev for ev in log.events() if ev.type == "consensus.audit"]
    if not audits:
        return _unknown("strategies", "Verifier calibration",
                        "no consensus audit has been sealed")
    seq = _seq_of(audits[-1])
    cal = calibrate(log)
    unmeasured = sorted(name for name, st in cal.per_strategy.items()
                        if st.total < MIN_AUDITS)
    degenerate = sorted(cal.downgraded)
    problems = unmeasured or degenerate
    detail = ""
    if unmeasured:
        detail = (f"unmeasured (<{MIN_AUDITS} audits): "
                  + ", ".join(unmeasured[:3]))
    elif degenerate:
        detail = "degenerate: " + ", ".join(degenerate[:3])
    return Cell("strategies", "Verifier calibration",
                ATTENTION if problems else OK,
                f"{cal.audits} audit(s), {len(cal.per_strategy)} strategy",
                detail, "consensus.audit", seq,
                {"audits": cal.audits, "unmeasured": unmeasured,
                 "degenerate": degenerate})


def _cell_candidates(log, root: Path | None = None) -> Cell:
    """Invariant candidates nobody has accepted or rejected.

    The ledger is a file, not a log event, so this cell is traceable to
    the harvest that produced it: the sealed `invariantloop.harvest`.
    Without one, the ledger on disk could be anything and the board says
    `unknown` rather than reading it and implying it was checked.
    """
    from .invariantloop import LEDGER_NAME, load_ledger
    data, seq = _newest(log, "invariantloop.harvest")
    if seq < 0:
        return _unknown("candidates", "Invariant candidates",
                        "no harvest has been sealed")
    path = Path(root or Path.cwd()) / LEDGER_NAME
    if not path.exists():
        return Cell("candidates", "Invariant candidates", ATTENTION,
                    f"{int(data.get('open') or 0)} open",
                    f"the harvest was sealed but {LEDGER_NAME} is missing",
                    "invariantloop.harvest", seq, {})
    ledger = load_ledger(path)
    open_now = ledger.open
    return Cell("candidates", "Invariant candidates",
                ATTENTION if open_now else OK,
                f"{len(open_now)} undecided",
                ("oldest: " + open_now[0].id) if open_now else "",
                "invariantloop.harvest", seq,
                {"open": [c.id for c in open_now],
                 "total": len(ledger.candidates)})


def _cell_budgets(log) -> Cell:
    refusals = [ev for ev in log.events() if ev.type == "budget.refused"]
    decided = [ev for ev in log.events() if ev.type == "budget.decided"]
    if not refusals and not decided:
        return _unknown("budgets", "Verification budget",
                        "no budget decision has been sealed")
    newest = (refusals or decided)[-1]
    by_rule: dict[str, int] = {}
    for ev in refusals:
        data = ev.data if isinstance(ev.data, dict) else {}
        rule = str(data.get("rule") or "?")
        by_rule[rule] = by_rule.get(rule, 0) + 1
    return Cell("budgets", "Verification budget",
                ATTENTION if refusals else OK,
                f"{len(refusals)} refused of {len(decided) + len(refusals)}",
                ("; ".join(f"{k}: {v}" for k, v in sorted(by_rule.items()))
                 if by_rule else ""),
                newest.type, _seq_of(newest),
                {"refused": len(refusals), "decided": len(decided),
                 "by_rule": by_rule})


def _cell_release(log) -> Cell:
    data, seq = _newest(log, "release.gate")
    if seq < 0:
        return _unknown("release", "Release gate",
                        "no release has been attempted")
    if data.get("allowed"):
        return Cell("release", "Release gate", OK, "allowed",
                    str((data.get("range") or {}).get("label") or ""),
                    "release.gate", seq, {"allowed": True})
    codes = [str(r.get("code")) for r in (data.get("reasons") or ())]
    return Cell("release", "Release gate", ATTENTION, "refused",
                ", ".join(codes[:3]), "release.gate", seq,
                {"allowed": False, "reasons": codes})


def _cell_history(log) -> Cell:
    from .historicaudit import AUDIT_EVENT, V_DIFFERS
    data, seq = _newest(log, AUDIT_EVENT)
    if seq < 0:
        return _unknown("history", "Historical audit",
                        "no past decision has been re-checked")
    judged = int(data.get("judged") or 0)
    decisions = int(data.get("decisions") or 0)
    differing = int((data.get("counts") or {}).get(V_DIFFERS, 0))
    if differing:
        state, detail = ATTENTION, f"{differing} decided differently now"
    elif judged < decisions:
        state = ATTENTION
        detail = f"{decisions - judged} could not be re-checked"
    else:
        state, detail = OK, ""
    return Cell("history", "Historical audit", state,
                f"{judged}/{decisions} re-checked", detail,
                AUDIT_EVENT, seq,
                {"judged": judged, "decisions": decisions,
                 "differing": differing,
                 "rulesets": len(data.get("rulesets") or ())})


def _cell_regression(log) -> Cell:
    data, seq = _newest(log, "regression.gate")
    if seq < 0:
        return _unknown("regression", "Regression gate",
                        "run `python -m fullagent.regressiongate --check`")
    if data.get("allowed"):
        return Cell("regression", "Regression gate", OK, "passing", "",
                    "regression.gate", seq, {"allowed": True})
    codes = [str(r.get("code")) for r in (data.get("reasons") or ())]
    return Cell("regression", "Regression gate", ATTENTION, "blocked",
                ", ".join(codes[:3]), "regression.gate", seq,
                {"allowed": False, "reasons": codes})


#: Every cell, in the order a person reads them: what the argument says,
#: then what has actually been exercised, then what is waiting on someone.
READERS = (
    ("assurance", _cell_assurance),
    ("faults", _cell_faults),
    ("policy-meta", _cell_policy_meta),
    ("runbooks", _cell_runbooks),
    ("regression", _cell_regression),
    ("strategies", _cell_strategies),
    ("candidates", _cell_candidates),
    ("budgets", _cell_budgets),
    ("release", _cell_release),
    ("history", _cell_history),
)


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------

@dataclass
class Board:
    cells: tuple[Cell, ...] = ()
    at: float = 0.0

    def get(self, key: str) -> Cell | None:
        for cell in self.cells:
            if cell.key == key:
                return cell
        return None

    def of_state(self, state: str) -> tuple[Cell, ...]:
        return tuple(c for c in self.cells if c.state == state)

    @property
    def attention(self) -> tuple[Cell, ...]:
        """Everything a person has to look at, loudest first.

        Unmeasured outranks failing: a failing check has a number and an
        owner; an unmeasured surface has neither, and reading it as
        "nothing wrong here" is the mistake this board exists to stop.
        """
        return tuple(sorted(
            (c for c in self.cells if c.state != OK),
            key=lambda c: (-LOUDNESS[c.state], c.key)))

    @property
    def ok(self) -> bool:
        return not self.attention

    @property
    def traceable(self) -> bool:
        """Whether every displayed figure names the seal it came from.

        The invariant the module is built on. An `unknown` cell displays
        no figure, so it is vacuously traceable; anything else that
        cannot name its seal is a bug in a reader, not a finding about
        the system.
        """
        return all(c.traceable for c in self.cells if c.state != UNKNOWN)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "at": self.at, "traceable": self.traceable,
                "states": {s: len(self.of_state(s)) for s in STATES},
                "cells": [c.to_dict() for c in self.cells]}

    def query(self, key: str) -> dict:
        cell = self.get(key)
        return cell.to_dict() if cell else {"key": key, "state": UNKNOWN,
                                            "detail": "no such cell"}

    def format(self) -> str:
        counts = {s: len(self.of_state(s)) for s in STATES}
        head = (f"ASSURANCE BOARD — {counts[OK]} ok, "
                f"{counts[ATTENTION]} need attention, "
                f"{counts[UNKNOWN]} never measured, {counts[STALE]} stale")
        lines = [head]
        lines.extend(c.line() for c in self.cells)
        if not self.traceable:
            lines.append("  !! a displayed figure has no sealed source; "
                         "that is a bug in a reader")
        return "\n".join(lines)


def collect(log, root: Path | None = None) -> Board:
    """Read the board from the log. Runs no checks and computes no figures.

    A reader that raises produces an `unknown` cell rather than taking
    the board down: one broken reader must not be able to hide the other
    nine, and a cell that says "could not be read" is still an honest
    cell.
    """
    cells: list[Cell] = []
    for key, reader in READERS:
        try:
            cells.append(reader(log, root) if key == "candidates"
                         else reader(log))
        except Exception as exc:       # noqa: BLE001
            cells.append(Cell(key, key.replace("-", " ").title(), UNKNOWN,
                              "unknown",
                              f"reader raised {type(exc).__name__}: {exc}"))
    return Board(tuple(cells), time.time())


#: What `refresh` runs, and the event each one seals. Kept as data so the
#: board can say which command fills a cell it is missing.
FILLS: dict[str, tuple[str, str]] = {
    "assurance": ("assurance.case", "python -m fullagent.assurance --check"),
    "faults": ("faultcatalogue.run",
               "python -m fullagent.faultcatalogue --check"),
    "policy-meta": ("policymeta.run",
                    "python -m fullagent.policymeta --check"),
    "runbooks": ("runbook.run", "python -m fullagent.runbook --check"),
    "regression": ("regression.gate",
                   "python -m fullagent.regressiongate --check"),
    "history": ("historicaudit.run", "python -m fullagent.historicaudit"),
}


def refresh(log, root: Path | None = None) -> Board:
    """Run the checks the board reads, seal each, then read the board.

    Deliberately a separate verb from `collect`. Merging the two is the
    convenience that makes a dashboard untrustworthy: a view that runs
    what it displays can always show something, so "never measured"
    stops being a state anyone ever sees.
    """
    from . import assurance, faultcatalogue, policymeta, runbook

    try:
        case = assurance.assess(assurance.shipped_case(), log)
        seals = assurance.verify_seals(case, log)
        case.defects = case.defects + seals
        log.append("assurance.case", case.to_dict(), actor="assuranceboard")
    except Exception:
        pass
    try:
        report = policymeta.verify_metamodel()
        log.append("policymeta.run", report.to_dict(),
                   actor="assuranceboard")
    except Exception:
        pass
    try:
        faultcatalogue.run_all(log=log)
    except Exception:
        pass
    try:
        runbook.run_all(log=log)
    except Exception:
        pass
    return collect(log, root)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import tempfile

    from .kernel import EventLog

    argv = sys.argv[1:]
    work = Path(tempfile.mkdtemp(prefix="fa-board-"))

    if "--check" in argv or "--json" in argv or "--query" in argv:
        log = EventLog(path=Path(".fullagent") / "events.jsonl") \
            if Path(".fullagent").exists() else \
            EventLog(path=work / "empty.jsonl")
        board = refresh(log) if "--refresh" in argv else collect(log)
        if "--query" in argv:
            key = argv[argv.index("--query") + 1]
            print(json.dumps(board.query(key), indent=2))
            raise SystemExit(0)
        print(json.dumps(board.to_dict(), indent=2) if "--json" in argv
              else board.format())
        raise SystemExit(0 if board.ok else 1)

    # -- an empty log: every cell unknown, and loudly so ----------------
    empty = EventLog(path=work / "empty.jsonl")
    blank = collect(empty)
    assert len(blank.cells) == len(READERS)
    assert all(c.state == UNKNOWN for c in blank.cells), blank.format()
    assert not blank.ok, "never measured is not a pass"
    assert blank.traceable, "an unknown cell displays no figure"
    assert all(c.value == "unknown" for c in blank.cells)
    assert all(not c.traceable for c in blank.cells)
    # The distinction the module exists for: nothing reads as zero.
    assert "0" not in " ".join(c.value for c in blank.cells), blank.format()

    # -- collect() runs nothing --------------------------------------------
    before = len(list(empty.events()))
    collect(empty)
    assert len(list(empty.events())) == before, \
        "reading the board must not write to the log"

    # -- refresh() runs the checks, seals them, and the board fills in --
    live = EventLog(path=work / "live.jsonl")
    board = refresh(live, work)
    for key in ("assurance", "faults", "policy-meta", "runbooks"):
        cell = board.get(key)
        assert cell is not None and cell.state != UNKNOWN, \
            f"{key} should be filled after a refresh: {cell}"
        assert cell.traceable, cell
        assert cell.state == OK, cell.line()
    assert board.traceable

    # Every filled cell names an event that is really in the log.
    types = {ev.type for ev in live.events()}
    for cell in board.cells:
        if cell.state != UNKNOWN:
            assert cell.source in types, cell
            assert any(_seq_of(ev) == cell.seq and ev.type == cell.source
                       for ev in live.events()), cell

    # -- unknown outranks attention in the attention list ---------------
    assert [c.key for c in blank.attention] == sorted(c.key
                                                      for c in blank.cells)
    mixed = Board((Cell("a", "A", ATTENTION, "1", source="x", seq=1),
                   Cell("b", "B", UNKNOWN, "unknown"),
                   Cell("c", "C", OK, "2", source="x", seq=2)))
    assert [c.key for c in mixed.attention] == ["b", "a"], mixed.attention
    assert not mixed.ok and mixed.get("c").state == OK

    # -- a stale seal is neither ok nor a failure -----------------------
    stale_log = EventLog(path=work / "stale.jsonl")
    stale_log.append("runbook.run",
                     {"runbooks": 15, "passed": 15, "defects": [],
                      "digest": "not-the-current-playbooks"},
                     actor="test")
    cell = collect(stale_log).get("runbooks")
    assert cell.state == STALE, cell
    assert cell.traceable and "since changed" in cell.detail

    # -- a refusal is shown with its typed reasons ----------------------
    refused = EventLog(path=work / "refused.jsonl")
    refused.append("release.gate",
                   {"allowed": False,
                    "reasons": [{"code": "provenance-gaps"},
                                {"code": "regression-gate-not-run"}],
                    "range": {"label": "v9"}}, actor="test")
    cell = collect(refused).get("release")
    assert cell.state == ATTENTION and cell.value == "refused"
    assert "provenance-gaps" in cell.detail

    # -- a broken reader produces an unknown cell, not a broken board ---
    saved = READERS
    try:
        def _explodes(log):
            raise RuntimeError("this reader is broken")

        globals()["READERS"] = tuple(
            (key, _explodes) if key == "faults" else (key, reader)
            for key, reader in saved)
        survived = collect(live)
        broken = survived.get("faults")
        assert broken.state == UNKNOWN and "RuntimeError" in broken.detail
        assert len(survived.cells) == len(saved), \
            "one broken reader must not hide the others"
        assert survived.get("assurance").state == OK
    finally:
        globals()["READERS"] = saved

    # -- queryable, headless, and deterministic -------------------------
    assert board.query("faults")["key"] == "faults"
    assert board.query("no-such-cell")["state"] == UNKNOWN
    payload = board.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["traceable"] is True
    assert sum(payload["states"].values()) == len(READERS)
    # The same log gives the same board, twice: nothing is recomputed on
    # view and nothing depends on the clock beyond the timestamp.
    again = collect(live, work).to_dict()
    del payload["at"], again["at"]
    assert again == payload, "the board must be a function of the log"

    # -- every unknown cell says which command fills it -----------------
    for key, (event, command) in FILLS.items():
        cell = blank.get(key)
        assert cell is not None and cell.state == UNKNOWN
        assert command in cell.detail or "no " in cell.detail, cell

    print(board.format())
    print(f"ASSURANCEBOARD SELF-TEST PASS — {len(board.cells)} cell(s), "
          f"{len(board.attention)} needing attention, every figure "
          f"traceable to a sealed event")

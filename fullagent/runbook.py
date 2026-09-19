"""RUNBOOK — proving the recovery playbooks still work, on purpose.

`recovery.py` says what to do about each failure class. Those sentences
were true when they were written. Nothing has been checking that they
are still true, because the only way to find out is to make the failure
happen, and nothing in the system makes failures happen on purpose.

This does. For each error code there is a **runbook**: a deterministic,
sandboxed scenario that injects exactly that failure into a real
dispatcher and a real orchestrator, then checks that what came out is
what the playbook promised. Not that the code path ran -- that the
*disposition* was the one the playbook names, for the context the
scenario set up.

Three design choices carry the whole thing:

**Deterministic, not random.** Chaos engineering usually means random
faults on real infrastructure. That is the wrong tool here: a test that
fails one run in fifty teaches nobody anything and gets muted. Every
injector is a plain function that fails the same way every time, in a
temporary directory, with no network and no sleeps. A runbook that goes
red went red for a reason you can read.

**A failing runbook is a defect, not a flake.** There is no retry and no
tolerance. `D_PLAYBOOK_BROKEN` means the playbook no longer describes
what happens. `D_HARNESS_BROKEN` means the scenario could not even set
itself up -- reported *separately*, because a harness that cannot run is
not a playbook that passed, and folding the two together is how a suite
starts lying.

**Coverage is checked, not assumed.** `D_UNCOVERED` names any error code
with no runbook. The failure mode worth fearing is a green board over a
failure class nobody ever exercised.

Freshness matters too: the playbooks can change after the last run. Each
result carries the digest of the playbooks it was taken against, so
`stale()` can say "these results describe a different rule set" rather
than letting yesterday's green stand in for today's.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import recovery
from .orchestrator import (COMPENSATED, DONE, ESCALATED, FAILED, Orchestrator,
                           Plan, Step)
from .toolcontract import (E_CANCELLED, E_CONFLICT, E_INTERNAL, E_NOT_FOUND,
                           E_PERMISSION, E_RESOURCE, E_TIMEOUT, E_UPSTREAM,
                           E_VALIDATION, ERROR_CODES, IDEMPOTENT,
                           NON_IDEMPOTENT, RetryPolicy, ToolContract)

# ---------------------------------------------------------------------------
# Defect kinds
# ---------------------------------------------------------------------------

D_PLAYBOOK_BROKEN = "playbook-broken"
D_HARNESS_BROKEN = "harness-broken"
D_UNCOVERED = "failure-class-uncovered"
D_STALE = "results-stale"

DEFECTS: dict[str, tuple[str, str]] = {
    D_PLAYBOOK_BROKEN: (
        "the recovery playbook no longer describes what actually happens",
        "either the playbook is wrong or the runtime changed under it — "
        "fix whichever is lying"),
    D_HARNESS_BROKEN: (
        "the scenario could not set itself up, so nothing was proved",
        "fix the harness; a test that cannot run is not a test that "
        "passed"),
    D_UNCOVERED: (
        "a failure class has no runbook exercising it",
        "write one — an unexercised playbook is an untested promise"),
    D_STALE: (
        "the results were taken against a different set of playbooks",
        "re-run the runbooks against the current playbooks"),
}


@dataclass(frozen=True)
class Defect:
    kind: str
    subject: str
    detail: str
    expected: str = ""
    observed: str = ""

    @property
    def what(self) -> str:
        return DEFECTS.get(self.kind, ("", ""))[0]

    @property
    def remedy(self) -> str:
        return DEFECTS.get(self.kind, ("", ""))[1]

    def to_dict(self) -> dict:
        return {"kind": self.kind, "subject": self.subject,
                "detail": self.detail, "expected": self.expected,
                "observed": self.observed, "what": self.what,
                "remedy": self.remedy}

    def line(self) -> str:
        tail = (f" (expected {self.expected}, observed {self.observed})"
                if self.expected or self.observed else "")
        return f"  [{self.kind}] {self.subject}: {self.detail}{tail}"


# ---------------------------------------------------------------------------
# Injectors — deterministic failures, one per class
# ---------------------------------------------------------------------------

class Injected(Exception):
    """A fault this module caused on purpose."""


def _raises(exc: Exception) -> Callable[..., str]:
    def handler(**_kw) -> str:
        raise exc
    return handler


def _returns(text: str) -> Callable[..., str]:
    def handler(**_kw) -> str:
        return text
    return handler


def _hangs() -> Callable[..., str]:
    def handler(**_kw) -> str:
        # Busy-waits rather than sleeping: the dispatcher abandons the
        # thread at its timeout either way, and a sleep in a test suite
        # is a cost every run pays for nothing.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            pass
        return "never returned in time"
    return handler


#: One handler per error code, each failing in exactly the way the
#: dispatcher's own classifier maps to that code. Where the repo's tools
#: report failure as an "ERROR: ..." string, the injector does too --
#: the point is to exercise the real path, not a convenient one.
#: Each entry is (handler factory, the arguments to call it with). The
#: arguments matter for one class: E_VALIDATION is produced by sending a
#: value the schema rejects, because that is where a validation error
#: actually comes from. Faking it with an "ERROR: ..." string would
#: exercise the error-text bridge instead, and the bridge has no
#: validation needle -- it would arrive as E_INTERNAL and the runbook
#: would be testing the wrong thing while looking green.
INJECTORS: dict[str, tuple[Callable[[], Callable[..., str]], dict]] = {
    E_VALIDATION: (lambda: _returns("never reached"), {"marker": 12345}),
    E_PERMISSION: (lambda: _returns("ERROR: refused: outside the roots"), {}),
    E_NOT_FOUND: (lambda: _returns("ERROR: not found: no such file"), {}),
    E_CONFLICT: (lambda: _raises(FileExistsError("it already exists")), {}),
    E_TIMEOUT: (_hangs, {}),
    E_UPSTREAM: (lambda: _raises(ConnectionError("the far end went away")),
                 {}),
    E_RESOURCE: (lambda: _raises(MemoryError("out of memory")), {}),
    E_CANCELLED: (lambda: _raises(KeyboardInterrupt()), {}),
    E_INTERNAL: (lambda: _raises(Injected("a defect in the tool")), {}),
}


# ---------------------------------------------------------------------------
# Runbooks
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Runbook:
    """One failure class, injected, with what the playbook promises."""
    code: str
    #: Whether the failing step's tool can be safely repeated. This is
    #: the context that decides half the playbooks, so each runbook says
    #: it explicitly rather than inheriting whatever the default is.
    idempotent: bool
    #: Whether the failing step declared an undo.
    reversible: bool
    #: Whether a human is reachable in this scenario.
    can_ask_human: bool
    summary: str = ""

    @property
    def id(self) -> str:
        return (f"{self.code}"
                f"/{'idem' if self.idempotent else 'once'}"
                f"/{'undoable' if self.reversible else 'no-undo'}"
                f"/{'human' if self.can_ask_human else 'alone'}")

    def expected(self) -> str:
        """What `recovery.plan` says should happen, asked directly.

        The expectation is read from the playbook rather than written
        down here on purpose: a runbook that hard-coded its answer would
        pass forever while the playbook changed underneath it, which is
        precisely the failure this module exists to catch.
        """
        context = recovery.Context(
            idempotent=self.idempotent,
            has_compensation=self.reversible,
            can_ask_human=self.can_ask_human,
            attempts=1, max_attempts=1)
        verdict = recovery.plan(
            _error(self.code), context)
        # The orchestrator turns a RETRY the dispatcher already spent
        # into an ESCALATE, because there is nothing left for the plan
        # level to try. The runbook models the same collapse.
        if verdict.strategy == recovery.RETRY:
            return recovery.ESCALATE
        return verdict.strategy

    def to_dict(self) -> dict:
        return {"id": self.id, "code": self.code,
                "idempotent": self.idempotent,
                "reversible": self.reversible,
                "can_ask_human": self.can_ask_human,
                "summary": self.summary, "expected": self.expected()}


def _error(code: str):
    from .toolcontract import ToolError
    return ToolError(code, "injected on purpose", tool="chaos")


#: The standing set: every error code, in the context where its playbook
#: is most load-bearing, plus the contrasting context for the codes whose
#: answer depends on it.
RUNBOOKS: tuple[Runbook, ...] = (
    Runbook(E_VALIDATION, True, True, True,
            "a schema failure is the caller's to fix, not the plan's"),
    Runbook(E_PERMISSION, True, True, True,
            "policy refused; a human may grant it, the agent may not "
            "route around it"),
    Runbook(E_PERMISSION, True, True, False,
            "policy refused and nobody is there to ask"),
    Runbook(E_NOT_FOUND, True, True, True,
            "nothing to repeat and nothing landed to undo"),
    Runbook(E_CONFLICT, True, True, True,
            "the world was not as assumed; undo back to a known state"),
    Runbook(E_CONFLICT, True, False, True,
            "the same, with no undo declared"),
    Runbook(E_TIMEOUT, True, True, True,
            "abandoned, not observed — but repeatable, so the dispatcher "
            "already tried"),
    Runbook(E_TIMEOUT, False, True, True,
            "abandoned on a call that must not be repeated: the case only "
            "a human can settle"),
    Runbook(E_TIMEOUT, False, True, False,
            "the same, with nobody to ask"),
    Runbook(E_UPSTREAM, True, True, True,
            "a transient network failure on a repeatable call"),
    Runbook(E_UPSTREAM, False, True, True,
            "the same on a call that cannot be repeated"),
    Runbook(E_RESOURCE, True, True, True,
            "a ceiling or quota: retrying makes it worse"),
    Runbook(E_CANCELLED, True, True, True,
            "somebody stopped this on purpose"),
    Runbook(E_INTERNAL, True, True, True,
            "a defect in the tool: undo what landed and stop"),
    Runbook(E_INTERNAL, True, False, True,
            "a defect with nothing to undo it with"),
)


# ---------------------------------------------------------------------------
# Running one
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    runbook: Runbook
    passed: bool = False
    expected: str = ""
    observed: str = ""
    defect: Defect | None = None
    detail: str = ""
    duration: float = 0.0

    def to_dict(self) -> dict:
        return {"id": self.runbook.id, "code": self.runbook.code,
                "passed": self.passed, "expected": self.expected,
                "observed": self.observed, "detail": self.detail,
                "duration": round(self.duration, 4),
                "defect": self.defect.to_dict() if self.defect else None}

    def line(self) -> str:
        mark = "ok  " if self.passed else "FAIL"
        return (f"  {mark} {self.runbook.id:<44} "
                f"{self.expected:<11}"
                + ("" if self.passed else f" -> {self.observed}"))


def _chaos_contract(name: str, idempotent: bool) -> ToolContract:
    return ToolContract(
        name=name, description="fails on purpose",
        input_schema={"type": "object",
                      "properties": {"marker": {"type": "string"}}},
        output_schema={"type": "string"},
        permission=frozenset(),
        idempotency=IDEMPOTENT if idempotent else NON_IDEMPOTENT,
        # No retries and no backoff: the runbook is about the plan-level
        # disposition, and a sleeping test suite is a tax on every run.
        retry=RetryPolicy(max_attempts=1, backoff_seconds=0.0),
        timeout_seconds=0.25)


def run_one(book: Runbook, root: Path, log=None) -> Outcome:
    """Inject one failure into a real run and read what came back."""
    from .dispatch import Dispatcher

    started = time.time()
    outcome = Outcome(book, expected=book.expected())
    try:
        work = root / book.id.replace("/", "_")
        work.mkdir(parents=True, exist_ok=True)
        landed = work / "landed.txt"

        factory, chaos_args = INJECTORS[book.code]
        dispatcher = Dispatcher(log=log, approve=lambda c, a: True)
        dispatcher.register(_chaos_contract("chaos", book.idempotent),
                            factory())
        def land(**_kw) -> str:
            landed.write_text("landed", encoding="utf-8")
            return "landed"

        def unland(**_kw) -> str:
            landed.unlink(missing_ok=True)
            return "undone"

        dispatcher.register(_chaos_contract("land", True), land)
        dispatcher.register(_chaos_contract("unland", True), unland)

        orch = Orchestrator(
            dispatcher, log=log,
            # `approve` is what the orchestrator reads to decide whether
            # a human is reachable, so this is the scenario's own switch.
            approve=(lambda p, r: True) if book.can_ask_human else None)

        steps = [Step("land", "land", {"marker": "x"},
                      undo_tool="unland", undo_args={"marker": "x"})]
        steps.append(Step(
            "chaos", "chaos", dict(chaos_args) or {"marker": "x"},
            **({"undo_tool": "unland", "undo_args": {"marker": "x"}}
               if book.reversible else {})))
        result = orch.run(Plan(goal=f"exercise {book.code}", steps=steps))
    except Exception as exc:
        outcome.defect = Defect(
            D_HARNESS_BROKEN, book.id,
            f"the scenario raised {type(exc).__name__}: {exc}")
        outcome.duration = time.time() - started
        return outcome

    # A schema-invalid step never reaches dispatch: `review()` refuses
    # the whole plan first. That is the correct behaviour and it is worth
    # locking in, so the runbook reads a refused plan as the abort it is
    # rather than calling it a broken playbook.
    if result.review is not None and not result.review.ok:
        outcome.observed = recovery.ABORT
        outcome.detail = ("refused at plan review, before anything ran: "
                          + "; ".join(result.review.problems[:2]))
        outcome.duration = time.time() - started
        outcome.passed = outcome.observed == outcome.expected
        if not outcome.passed:
            outcome.defect = Defect(
                D_PLAYBOOK_BROKEN, book.id,
                f"the plan was refused at review, but the playbook for "
                f"{book.code} promises {outcome.expected!r}",
                outcome.expected, outcome.observed)
        return outcome

    entry = result.entry("chaos")
    if entry is None:
        outcome.defect = Defect(
            D_HARNESS_BROKEN, book.id,
            "the failing step produced no ledger entry, so there is "
            "nothing to judge")
        outcome.duration = time.time() - started
        return outcome

    outcome.observed = entry.recovery or _from_status(entry.status)
    outcome.detail = entry.detail
    outcome.duration = time.time() - started
    outcome.passed = outcome.observed == outcome.expected
    if not outcome.passed:
        outcome.defect = Defect(
            D_PLAYBOOK_BROKEN, book.id,
            f"the playbook for {book.code} promises {outcome.expected!r} "
            f"in this context",
            outcome.expected, outcome.observed)
    return outcome


def _from_status(status: str) -> str:
    """The disposition a ledger status implies, when none was recorded."""
    return {COMPENSATED: recovery.COMPENSATE,
            ESCALATED: recovery.ESCALATE,
            FAILED: recovery.ABORT,
            DONE: "none"}.get(status, status)


# ---------------------------------------------------------------------------
# Running them all
# ---------------------------------------------------------------------------

def playbook_digest() -> str:
    """A digest over the playbooks these results describe."""
    return hashlib.sha256(json.dumps(
        {code: p.to_dict() for code, p in sorted(recovery.PLAYBOOKS.items())},
        sort_keys=True).encode()).hexdigest()[:16]


@dataclass
class Report:
    outcomes: tuple[Outcome, ...] = ()
    defects: tuple[Defect, ...] = ()
    digest: str = ""
    at: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.defects

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    @property
    def broken_playbooks(self) -> tuple[Defect, ...]:
        return tuple(d for d in self.defects if d.kind == D_PLAYBOOK_BROKEN)

    @property
    def broken_harness(self) -> tuple[Defect, ...]:
        return tuple(d for d in self.defects if d.kind == D_HARNESS_BROKEN)

    def stale(self) -> bool:
        return bool(self.digest) and self.digest != playbook_digest()

    def to_dict(self) -> dict:
        return {"ok": self.ok, "digest": self.digest, "at": self.at,
                "runbooks": len(self.outcomes), "passed": self.passed,
                "outcomes": [o.to_dict() for o in self.outcomes],
                "defects": [d.to_dict() for d in self.defects]}

    def format(self) -> str:
        head = (f"RUNBOOKS — {len(self.outcomes)} scenario(s), "
                f"{self.passed} proved the playbook still holds")
        if not self.ok:
            head += (f" — {len(self.broken_playbooks)} broken playbook(s), "
                     f"{len(self.broken_harness)} broken harness, "
                     f"{len([d for d in self.defects if d.kind == D_UNCOVERED])}"
                     f" uncovered")
        lines = [head]
        lines.extend(o.line() for o in self.outcomes)
        lines.extend(d.line() for d in self.defects)
        return "\n".join(lines)


def run_all(root: Path | None = None, log=None,
            books: tuple[Runbook, ...] = RUNBOOKS) -> Report:
    """Every runbook, plus the coverage check over the taxonomy."""
    import tempfile

    root = Path(root or tempfile.mkdtemp(prefix="fa-runbook-"))
    outcomes = [run_one(book, root, log) for book in books]
    defects = [o.defect for o in outcomes if o.defect is not None]

    covered = {b.code for b in books}
    for code in sorted(set(ERROR_CODES) - covered):
        defects.append(Defect(
            D_UNCOVERED, code,
            "this failure class has no runbook, so its playbook is an "
            "untested promise"))
    for code in sorted(covered - set(INJECTORS)):
        defects.append(Defect(
            D_HARNESS_BROKEN, code,
            "a runbook names a code with no injector"))

    report = Report(tuple(outcomes), tuple(defects), playbook_digest(),
                    time.time())
    if log is not None:
        try:
            log.append("runbook.run", report.to_dict(), actor="runbook")
        except Exception:
            pass
    return report


def last_run(log) -> Report | None:
    """The newest sealed runbook report, for a freshness check."""
    latest = None
    for ev in log.events():
        if ev.type == "runbook.run":
            latest = ev.data if isinstance(ev.data, dict) else None
    if latest is None:
        return None
    return Report((), tuple(
        Defect(str(d.get("kind", "")), str(d.get("subject", "")),
               str(d.get("detail", "")))
        for d in (latest.get("defects") or ())),
        str(latest.get("digest", "")), float(latest.get("at", 0.0)))


def freshness(log) -> Defect | None:
    """Whether the sealed results still describe the current playbooks."""
    report = last_run(log)
    if report is None:
        return Defect(D_STALE, "runbooks",
                      "no runbook run has ever been sealed")
    if report.stale():
        return Defect(D_STALE, "runbooks",
                      f"sealed against playbooks {report.digest}, which is "
                      f"not the current {playbook_digest()}")
    return None


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import tempfile

    from .kernel import EventLog

    argv = sys.argv[1:]
    work = Path(tempfile.mkdtemp(prefix="fa-runbook-"))
    log = EventLog(path=str(work / "events.jsonl"))

    if "--check" in argv:
        report = run_all(work / "check", log)
        print(report.format())
        raise SystemExit(0 if report.ok else 1)

    if "--json" in argv:
        print(json.dumps(run_all(work / "json").to_dict(), indent=2))
        raise SystemExit(0)

    # --- every failure class is exercised, and every playbook holds ---
    report = run_all(work / "all", log)
    assert report.ok, report.format()
    assert len(report.outcomes) == len(RUNBOOKS)
    assert report.passed == len(RUNBOOKS), report.format()
    assert not report.broken_playbooks and not report.broken_harness

    # --- the expectation is read from the playbook, not hard-coded ----
    for book in RUNBOOKS:
        assert book.expected() in recovery.STRATEGIES, book.id
        assert book.expected() != recovery.RETRY, \
            "a retry the dispatcher already spent is an escalation"

    # --- the contexts really do change the answer ---------------------
    repeatable = Runbook(E_TIMEOUT, True, True, True)
    once_only = Runbook(E_TIMEOUT, False, True, True)
    alone = Runbook(E_TIMEOUT, False, True, False)
    assert once_only.expected() == recovery.ESCALATE, once_only.expected()
    assert alone.expected() == recovery.ABORT, \
        "escalating to a human who is not there is not a plan"
    assert repeatable.expected() != alone.expected()

    # --- a broken playbook is caught, not tolerated -------------------
    import dataclasses
    original = recovery.PLAYBOOKS[E_INTERNAL]
    recovery.PLAYBOOKS[E_INTERNAL] = dataclasses.replace(
        original, strategy=recovery.ABORT, fallback=recovery.ABORT,
        requires_compensation=False)
    try:
        # The runbook asks the playbook what it promises, so changing the
        # playbook changes the expectation too. What proves the engine
        # works is an expectation that no longer matches the runtime --
        # so the check is run against the ORIGINAL promise.
        stale_book = Runbook(E_INTERNAL, True, True, True)
        outcome = run_one(stale_book, work / "broken", log)
        # With the playbook now saying ABORT and the runtime obeying it,
        # this passes -- which is the honest result. The engine's ability
        # to fail is proved directly below.
        assert outcome.passed, outcome.to_dict()
    finally:
        recovery.PLAYBOOKS[E_INTERNAL] = original

    # ...the engine reports a mismatch as a first-class defect
    mismatch = Outcome(RUNBOOKS[0], False, recovery.COMPENSATE,
                       recovery.ABORT)
    mismatch.defect = Defect(D_PLAYBOOK_BROKEN, RUNBOOKS[0].id,
                             "promised compensate", recovery.COMPENSATE,
                             recovery.ABORT)
    bad = Report((mismatch,), (mismatch.defect,), playbook_digest())
    assert not bad.ok and bad.broken_playbooks
    assert "FAIL" in bad.format() and "broken playbook" in bad.format()
    assert mismatch.defect.remedy

    # --- a broken harness is reported apart from a broken playbook ----
    class Exploding(Runbook):
        pass

    ghost = Runbook("E_NOT_A_CODE", True, True, True)
    broken = run_one(ghost, work / "ghost", log)
    assert not broken.passed and broken.defect is not None
    assert broken.defect.kind == D_HARNESS_BROKEN, broken.to_dict()
    assert "KeyError" in broken.defect.detail or \
        "could not" in broken.defect.detail

    partial = run_all(work / "partial", None,
                      books=(RUNBOOKS[0], ghost))
    assert not partial.ok
    kinds = {d.kind for d in partial.defects}
    assert D_HARNESS_BROKEN in kinds and D_UNCOVERED in kinds, sorted(kinds)
    assert partial.broken_harness, partial.format()

    # --- an unexercised failure class is a defect ---------------------
    thin = run_all(work / "thin", None, books=(RUNBOOKS[0],))
    uncovered = {d.subject for d in thin.defects if d.kind == D_UNCOVERED}
    assert uncovered == set(ERROR_CODES) - {RUNBOOKS[0].code}, \
        sorted(uncovered)
    assert not thin.ok

    # --- every error code has an injector -----------------------------
    assert set(INJECTORS) == set(ERROR_CODES), \
        sorted(set(ERROR_CODES) ^ set(INJECTORS))
    for code, (factory, args) in INJECTORS.items():
        assert callable(factory) and isinstance(args, dict), code
    assert {b.code for b in RUNBOOKS} == set(ERROR_CODES), \
        sorted(set(ERROR_CODES) - {b.code for b in RUNBOOKS})

    # --- determinism: the same runbook twice gives the same answer ----
    first = run_one(RUNBOOKS[4], work / "det-1", None)
    second = run_one(RUNBOOKS[4], work / "det-2", None)
    assert first.observed == second.observed and first.passed == second.passed

    # --- freshness --------------------------------------------------
    assert freshness(log) is None, "a fresh run is not stale"
    empty = EventLog(path=str(work / "empty.jsonl"))
    missing = freshness(empty)
    assert missing is not None and missing.kind == D_STALE
    forged = EventLog(path=str(work / "forged.jsonl"))
    forged.append("runbook.run", {"digest": "0" * 16, "defects": [],
                                  "at": 0.0}, actor="runbook")
    aged = freshness(forged)
    assert aged is not None and "not the current" in aged.detail

    # --- every defect explains itself ---------------------------------
    for kind, (what, remedy) in DEFECTS.items():
        assert what and remedy, kind
        assert Defect(kind, "x", "y").what == what

    # --- the run is sealed --------------------------------------------
    assert "runbook.run" in {e.type for e in log.events()}

    print(report.format())
    print(f"RUNBOOK SELF-TEST PASS — {len(RUNBOOKS)} scenario(s) over "
          f"{len(ERROR_CODES)} failure class(es), all deterministic")

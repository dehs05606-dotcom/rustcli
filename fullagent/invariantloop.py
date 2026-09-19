"""INVARIANTLOOP — the system proposing its own next invariant.

`invariants.py` holds what someone thought to state. The interesting
claims are the ones nobody thought of, and the system already knows
where they are: every place it had to record that it could not explain
something.

Three such places exist, each one already a first-class object:

* a provenance **Gap** — a decision whose cause was never sealed,
* a consensus **HOLD** — two readings of a reply that did not agree,
* a recovery **ESCALATE** — a failure the code declined to decide.

Each is the system saying "here is something I cannot account for". A
candidate invariant is that sentence turned round: *this should never
have been unaccountable*. The loop reads the log, groups the evidence,
and writes the claim someone would have to make for the gap to be a
defect rather than a fact of life.

Two rules hold the whole thing up, and neither is negotiable:

**Never auto-adopted.** A candidate is a *proposal*. Accepting one is a
rule change, so it has to name a human and pass the regression
constitution gate — which is exactly the gate that already refuses a
rule change with no benchmark behind it. A system that writes its own
rules and adopts them is a system that can quietly lose the rules it
started with.

**Never silently dropped.** Rejecting a candidate needs a reason and a
name. A candidate nobody decided stays `proposed` forever and shows up
in every report until somebody deals with it. The failure mode this
guards against is not a bad invariant getting in; it is a real one
getting quietly binned because reading it was inconvenient.

The ledger is content-addressed: the same evidence produces the same
candidate id on every run, so re-running the loop after a rejection
re-finds the candidate and sees that it was already decided, rather than
proposing a fresh copy under a new name.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import provenance as prov
from .invariants import CLOSURE, CONSISTENCY, KINDS as INVARIANT_KINDS
from .invariants import POSTCONDITION, TOTALITY

LEDGER_NAME = "invariant-candidates.json"

# ---------------------------------------------------------------------------
# Where candidates come from
# ---------------------------------------------------------------------------

S_GAP = "provenance-gap"
S_HOLD = "consensus-hold"
S_ESCALATION = "recovery-escalation"
S_ENVELOPE = "envelope-violation"

SOURCES = (S_GAP, S_HOLD, S_ESCALATION, S_ENVELOPE)

#: What each source means, and the shape of claim it suggests.
SOURCE_MEANING: dict[str, tuple[str, str]] = {
    S_GAP: (
        "the record could not explain why something happened",
        "every X must have a sealed cause"),
    S_HOLD: (
        "two independent readings of a reply disagreed",
        "a reply of this shape must carry the evidence that settles it"),
    S_ESCALATION: (
        "a failure was handed to a human because the code would not "
        "decide it",
        "this failure class must have a reachable decision"),
    S_ENVELOPE: (
        "a call did something its behavioural contract does not allow",
        "this tool must not produce this effect"),
}

# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

PROPOSED = "proposed"
ACCEPTED = "accepted"
REJECTED = "rejected"

STATUSES = (PROPOSED, ACCEPTED, REJECTED)


@dataclass
class Candidate:
    """One invariant the system thinks it should have had."""
    id: str
    source: str
    module: str
    kind: str
    statement: str
    #: The events that prompted it, as short human-readable lines.
    evidence: tuple[str, ...] = ()
    occurrences: int = 1
    first_seen: float = 0.0
    status: str = PROPOSED
    decided_by: str = ""
    why: str = ""
    decided_at: float = 0.0
    #: The regression gate verdict at the moment of acceptance.
    gate_digest: str = ""

    @property
    def open(self) -> bool:
        return self.status == PROPOSED

    def to_dict(self) -> dict:
        return {"id": self.id, "source": self.source, "module": self.module,
                "kind": self.kind, "statement": self.statement,
                "evidence": list(self.evidence),
                "occurrences": self.occurrences,
                "first_seen": self.first_seen, "status": self.status,
                "decided_by": self.decided_by, "why": self.why,
                "decided_at": self.decided_at,
                "gate_digest": self.gate_digest}

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        return cls(
            id=str(d["id"]), source=str(d.get("source", "")),
            module=str(d.get("module", "")), kind=str(d.get("kind", "")),
            statement=str(d.get("statement", "")),
            evidence=tuple(d.get("evidence") or ()),
            occurrences=int(d.get("occurrences", 1)),
            first_seen=float(d.get("first_seen", 0.0)),
            status=str(d.get("status", PROPOSED)),
            decided_by=str(d.get("decided_by", "")),
            why=str(d.get("why", "")),
            decided_at=float(d.get("decided_at", 0.0)),
            gate_digest=str(d.get("gate_digest", "")))

    def line(self) -> str:
        mark = {PROPOSED: "?", ACCEPTED: "+", REJECTED: "-"}[self.status]
        tail = ""
        if self.status != PROPOSED:
            tail = f"  ({self.status} by {self.decided_by}: {self.why})"
        return (f"  {mark} [{self.source}] {self.statement}"
                f"  x{self.occurrences}{tail}")


def _ident(source: str, module: str, statement: str) -> str:
    """A candidate's name, derived from what it claims.

    Content-addressed so the same finding re-proposes itself under the
    same id. Without this, rejecting a candidate would only make it come
    back tomorrow wearing a different name.
    """
    return hashlib.sha256(
        f"{source}|{module}|{statement}".encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Harvesting
# ---------------------------------------------------------------------------

def _candidate(source, module, kind, statement, evidence, at) -> Candidate:
    return Candidate(_ident(source, module, statement), source, module,
                     kind, statement, tuple(evidence), 1, at)


def from_gaps(log) -> list[Candidate]:
    """A Gap is provenance admitting it could not find a cause."""
    graph = prov.build(log)
    by_shape: dict[tuple[str, str], list[prov.Gap]] = {}
    for gap in graph.gaps:
        node = graph.nodes.get(gap.node)
        kind = node.kind if node is not None else "?"
        by_shape.setdefault((kind, gap.wanted), []).append(gap)
    out = []
    for (kind, wanted), gaps in sorted(by_shape.items()):
        statement = (f"every {kind} node must have a {wanted} edge to a "
                     f"sealed cause")
        cand = _candidate(
            S_GAP, "provenance", CONSISTENCY, statement,
            [g.detail for g in gaps[:3]], time.time())
        cand.occurrences = len(gaps)
        out.append(cand)
    return out


def from_holds(log) -> list[Candidate]:
    """A HOLD is two readings of one reply that did not agree."""
    by_question: dict[str, list[str]] = {}
    for ev in log.events():
        if ev.type != "consensus.audit":
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("outcome") != "hold":
            continue
        objections = [str(f.get("why") or "")
                      for o in (data.get("opinions") or ())
                      for f in (o.get("findings") or ())]
        key = objections[0] if objections else "the strategies gave no reason"
        by_question.setdefault(key, []).append(
            str((data.get("disagreement") or {}).get("question", "")))
    out = []
    for objection, seen in sorted(by_question.items()):
        statement = (f"a released reply must never leave this objection "
                     f"unanswered: {objection}")
        cand = _candidate(S_HOLD, "consensus", POSTCONDITION, statement,
                          [s for s in seen[:3] if s], time.time())
        cand.occurrences = len(seen)
        out.append(cand)
    return out


def from_escalations(log) -> list[Candidate]:
    """An ESCALATE is the code declining to decide a failure class."""
    by_code: dict[str, list[str]] = {}
    for ev in log.events():
        if ev.type != "orchestrator.step.done":
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("status") != "escalated":
            continue
        code = str(data.get("error_code") or "an unclassified failure")
        by_code.setdefault(code, []).append(
            str(data.get("detail") or data.get("path") or ""))
    out = []
    for code, details in sorted(by_code.items()):
        statement = (f"a step failing with {code} must reach a decision "
                     f"without a human, or the playbook must say why it "
                     f"cannot")
        cand = _candidate(S_ESCALATION, "recovery", TOTALITY, statement,
                          [d for d in details[:3] if d], time.time())
        cand.occurrences = len(details)
        out.append(cand)
    return out


def from_envelope_violations(log) -> list[Candidate]:
    """A violation is a tool doing what its contract forbids."""
    by_pair: dict[tuple[str, str], int] = {}
    for ev in log.events():
        if ev.type != "envelope.violation":
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        tool = str(data.get("tool") or "?")
        for v in (data.get("violations") or ()):
            if not v.get("blocking"):
                continue
            key = (tool, str(v.get("kind") or "?"))
            by_pair[key] = by_pair.get(key, 0) + 1
    out = []
    for (tool, kind), count in sorted(by_pair.items()):
        statement = (f"{tool} must never be sealed with a {kind} "
                     f"violation")
        cand = _candidate(S_ENVELOPE, "envelopes", CLOSURE, statement,
                          [f"{count} occurrence(s) of {kind}"], time.time())
        cand.occurrences = count
        out.append(cand)
    return out


HARVESTERS = (from_gaps, from_holds, from_escalations,
              from_envelope_violations)


def harvest(log) -> list[Candidate]:
    """Every candidate the log supports, deduplicated by id."""
    found: dict[str, Candidate] = {}
    for harvester in HARVESTERS:
        try:
            produced = harvester(log)
        except Exception:
            # A harvester that breaks must not stop the others. It has
            # found nothing, which is different from there being nothing.
            continue
        for cand in produced:
            existing = found.get(cand.id)
            if existing is None:
                found[cand.id] = cand
            else:
                existing.occurrences += cand.occurrences
    return [found[k] for k in sorted(found)]


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

class NotGated(Exception):
    """Raised when a candidate is accepted without a passing gate."""


@dataclass
class Ledger:
    """Every candidate ever proposed, and what was decided about it."""
    candidates: dict[str, Candidate] = field(default_factory=dict)

    # -- reading -------------------------------------------------------
    def of_status(self, status: str) -> tuple[Candidate, ...]:
        return tuple(c for c in self.sorted() if c.status == status)

    def sorted(self) -> tuple[Candidate, ...]:
        return tuple(sorted(self.candidates.values(),
                            key=lambda c: (-c.occurrences, c.id)))

    @property
    def open(self) -> tuple[Candidate, ...]:
        return self.of_status(PROPOSED)

    # -- writing -------------------------------------------------------
    def observe(self, found: list[Candidate]) -> tuple[Candidate, ...]:
        """Fold a harvest in. Returns the candidates that are new.

        A candidate already decided is not re-proposed; its occurrence
        count is updated so a rejected finding that keeps happening is
        visible as a rejected finding that keeps happening.
        """
        fresh: list[Candidate] = []
        for cand in found:
            existing = self.candidates.get(cand.id)
            if existing is None:
                self.candidates[cand.id] = cand
                fresh.append(cand)
                continue
            existing.occurrences = max(existing.occurrences,
                                       cand.occurrences)
            if not existing.evidence:
                existing.evidence = cand.evidence
        return tuple(fresh)

    def accept(self, ident: str, who: str, why: str,
               gate=None) -> Candidate:
        """Adopt a candidate. Requires a name AND a passing gate."""
        cand = self._decidable(ident)
        if not who:
            raise ValueError("accepting an invariant must name who did it")
        if gate is None:
            raise NotGated(
                "a new invariant is a rule change, so it may only be "
                "accepted alongside a regression gate verdict")
        allowed = getattr(gate, "allowed", None)
        if allowed is not True:
            raise NotGated(
                "the regression gate did not pass, so this invariant "
                "cannot be adopted: "
                + ", ".join(getattr(r, "code", "?")
                            for r in getattr(gate, "reasons", ())))
        cand.status = ACCEPTED
        cand.decided_by = who
        cand.why = why or "accepted"
        cand.decided_at = time.time()
        cand.gate_digest = _digest(gate.to_dict()
                                   if hasattr(gate, "to_dict") else str(gate))
        return cand

    def reject(self, ident: str, who: str, why: str) -> Candidate:
        """Decline a candidate. A reason is mandatory."""
        cand = self._decidable(ident)
        if not who:
            raise ValueError("rejecting an invariant must name who did it")
        if not why:
            raise ValueError(
                "a rejection must say why; a candidate dropped without a "
                "reason is a finding that was binned, not decided")
        cand.status = REJECTED
        cand.decided_by = who
        cand.why = why
        cand.decided_at = time.time()
        return cand

    def _decidable(self, ident: str) -> Candidate:
        cand = self.candidates.get(ident)
        if cand is None:
            raise KeyError(f"no candidate {ident!r}")
        if cand.status != PROPOSED:
            raise ValueError(
                f"candidate {ident!r} was already {cand.status} by "
                f"{cand.decided_by}; re-deciding it would erase that "
                f"record")
        return cand

    # -- persistence ---------------------------------------------------
    def to_dict(self) -> dict:
        return {"candidates": [c.to_dict() for c in self.sorted()]}

    @classmethod
    def from_dict(cls, d: dict) -> "Ledger":
        out = cls()
        for row in (d.get("candidates") or ()):
            try:
                cand = Candidate.from_dict(row)
            except (KeyError, TypeError, ValueError):
                continue
            out.candidates[cand.id] = cand
        return out

    def format(self) -> str:
        head = (f"INVARIANT CANDIDATES — {len(self.candidates)} total: "
                f"{len(self.of_status(PROPOSED))} proposed, "
                f"{len(self.of_status(ACCEPTED))} accepted, "
                f"{len(self.of_status(REJECTED))} rejected")
        return "\n".join([head] + [c.line() for c in self.sorted()])


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def load_ledger(path: str | Path) -> Ledger:
    try:
        return Ledger.from_dict(
            json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return Ledger()


def write_ledger(path: str | Path, ledger: Ledger) -> str:
    text = json.dumps(ledger.to_dict(), indent=2, sort_keys=True) + "\n"
    Path(path).write_text(text, encoding="utf-8")
    return text


class EvolutionLoop:
    """Harvest, propose, and seal — but never decide."""

    def __init__(self, ledger: Ledger | None = None, log=None):
        self.ledger = ledger if ledger is not None else Ledger()
        self.log = log

    def _emit(self, event: str, data: dict) -> None:
        if self.log is None:
            return
        try:
            self.log.append(event, data, actor="invariantloop")
        except Exception:
            pass

    def run(self, log=None) -> tuple[Candidate, ...]:
        source = log if log is not None else self.log
        if source is None:
            return ()
        fresh = self.ledger.observe(harvest(source))
        for cand in fresh:
            self._emit("invariant.proposed", cand.to_dict())
        return fresh

    def accept(self, ident: str, who: str, why: str, gate=None) -> Candidate:
        cand = self.ledger.accept(ident, who, why, gate)
        self._emit("invariant.accepted", cand.to_dict())
        return cand

    def reject(self, ident: str, who: str, why: str) -> Candidate:
        cand = self.ledger.reject(ident, who, why)
        self._emit("invariant.rejected", cand.to_dict())
        return cand


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import tempfile

    from .kernel import EventLog

    argv = sys.argv[1:]
    work = Path(tempfile.mkdtemp(prefix="fa-invloop-"))

    if "--report" in argv:
        print(load_ledger(Path.cwd() / LEDGER_NAME).format())
        raise SystemExit(0)

    log = EventLog(path=str(work / "events.jsonl"))

    # A session that could not account for four different things.
    log.append("orchestrator.step",
               {"path": "orphan", "trace_id": "t-nowhere",
                "tool": "write_file"}, actor="kernel")       # a gap
    log.append("orchestrator.plan",
               {"goal": "g", "trace_id": "t1", "ok": True, "steps": []},
               actor="kernel")
    log.append("orchestrator.step",
               {"path": "risky", "trace_id": "t1", "tool": "run_command"},
               actor="kernel")
    log.append("orchestrator.step.done",
               {"path": "risky", "trace_id": "t1", "status": "escalated",
                "error_code": "E_UPSTREAM",
                "detail": "the call is not repeatable"}, actor="kernel")
    log.append("consensus.audit",
               {"outcome": "hold",
                "opinions": [
                    {"strategy": "guardrail", "verdict": "fail",
                     "findings": [{"why": "a success is claimed with "
                                          "nothing shown"}]},
                    {"strategy": "independent", "verdict": "pass",
                     "findings": []}],
                "disagreement": {"question": "guardrail says fail, "
                                             "independent says pass"}},
               actor="consensus")
    log.append("envelope.violation",
               {"tool": "read_file", "ok": False,
                "violations": [{"kind": "undeclared-effect",
                                "blocking": True,
                                "detail": "it modifies a path"}]},
               actor="envelopes")

    # --- every source produces a candidate ----------------------------
    found = harvest(log)
    sources = {c.source for c in found}
    assert sources == set(SOURCES), sorted(sources)
    for cand in found:
        assert cand.statement and cand.id and cand.kind in INVARIANT_KINDS
        assert cand.status == PROPOSED
        assert cand.module

    # --- ids are content-addressed and stable -------------------------
    again = harvest(log)
    assert {c.id for c in again} == {c.id for c in found}, \
        "the same evidence must produce the same candidate id"

    # --- the ledger proposes once, then only counts -------------------
    loop = EvolutionLoop(log=log)
    fresh = loop.run()
    assert len(fresh) == len(found), (len(fresh), len(found))
    assert loop.run() == (), "a second pass must not re-propose"
    assert len(loop.ledger.open) == len(found)

    # --- nothing is adopted without a human ---------------------------
    target = loop.ledger.open[0]

    class Gate:
        def __init__(self, allowed, reasons=()):
            self.allowed = allowed
            self.reasons = reasons

        def to_dict(self):
            return {"allowed": self.allowed}

    try:
        loop.accept(target.id, "", "looks right", Gate(True))
        raise AssertionError("acceptance must name a human")
    except ValueError:
        pass

    # --- ...and never without a passing regression gate ---------------
    try:
        loop.accept(target.id, "the operator", "looks right")
        raise AssertionError("acceptance must be gated")
    except NotGated as exc:
        assert "rule change" in str(exc)

    class Reason:
        code = "false-negative"

    try:
        loop.accept(target.id, "the operator", "looks right",
                    Gate(False, (Reason(),)))
        raise AssertionError("a failing gate must not adopt anything")
    except NotGated as exc:
        assert "false-negative" in str(exc), str(exc)

    accepted = loop.accept(target.id, "the operator",
                           "the gap is real and should never happen",
                           Gate(True))
    assert accepted.status == ACCEPTED and accepted.gate_digest
    assert accepted.decided_by == "the operator"

    # --- a decision is not re-decidable -------------------------------
    try:
        loop.reject(target.id, "somebody else", "changed my mind")
        raise AssertionError("a decided candidate must not be re-decided")
    except ValueError as exc:
        assert "already" in str(exc)

    # --- nothing is dropped without a reason --------------------------
    other = loop.ledger.open[0]
    try:
        loop.reject(other.id, "the operator", "")
        raise AssertionError("a rejection must carry a reason")
    except ValueError as exc:
        assert "why" in str(exc)
    rejected = loop.reject(other.id, "the operator",
                           "this is a property of the harness, not a bug")
    assert rejected.status == REJECTED and rejected.why

    # --- a rejected finding that keeps happening stays visible --------
    for _ in range(4):
        log.append("envelope.violation",
                   {"tool": "read_file",
                    "violations": [{"kind": "undeclared-effect",
                                    "blocking": True}]}, actor="envelopes")
    loop.run()
    env = [c for c in loop.ledger.sorted() if c.source == S_ENVELOPE][0]
    assert env.occurrences >= 5, env.to_dict()

    # --- an unknown id is an error, not a silent no-op ----------------
    try:
        loop.reject("not-a-candidate", "x", "y")
        raise AssertionError("an unknown candidate must raise")
    except KeyError:
        pass

    # --- open candidates never disappear ------------------------------
    assert loop.ledger.open, "undecided candidates must stay visible"
    report = loop.ledger.format()
    assert "proposed" in report and "accepted" in report

    # --- the ledger round-trips ---------------------------------------
    path = work / LEDGER_NAME
    write_ledger(path, loop.ledger)
    back = load_ledger(path)
    assert set(back.candidates) == set(loop.ledger.candidates)
    restored = back.candidates[accepted.id]
    assert restored.status == ACCEPTED and restored.decided_by == \
        "the operator"
    assert load_ledger(work / "absent.json").candidates == {}

    # --- a broken harvester does not stop the rest --------------------
    import fullagent.invariantloop as module
    original = module.HARVESTERS

    def explodes(_log):
        raise RuntimeError("this harvester is broken")

    module.HARVESTERS = (explodes,) + original
    try:
        assert harvest(log), "the working harvesters must still run"
    finally:
        module.HARVESTERS = original

    # --- everything the loop did is sealed ----------------------------
    kinds = {e.type for e in log.events()}
    assert {"invariant.proposed", "invariant.accepted",
            "invariant.rejected"} <= kinds, sorted(kinds)

    print(loop.ledger.format())
    print(f"INVARIANT LOOP SELF-TEST PASS — {len(SOURCES)} source(s), "
          f"{len(loop.ledger.candidates)} candidate(s); none adopted "
          f"without a human and a passing gate")

"""RELEASEGATE — a release you cannot build without complete provenance.

Every other gate in this stack answers "is this allowed?" and can be
overruled by someone who does not read the answer. This one is built the
other way round: a `Release` object has no public constructor. The only
thing that returns one is `build_release()`, and it returns a `Refusal`
instead whenever the record behind the release is incomplete.

That distinction is the whole point. "We checked and it was fine" is a
claim; "there is no way to hold a Release whose provenance has a hole in
it" is a property of the type. Anything downstream that takes a
`Release` is therefore reading a range whose record is complete, without
having to re-verify it or trust that someone did.

**What complete means**, precisely:

1. The provenance graph over the range has **no `Gap` nodes**. A Gap is
   provenance's own record of somewhere it could not find a cause; it is
   never filled in by inference, so a Gap in the range means a decision
   in that range has no traceable reason.
2. The graph **verifies under its signing key**. An unsigned or tampered
   graph is not evidence.
3. Every consensus **HOLD** in the range carries a `Resolution`. A held
   reply that nobody settled is an open question, and shipping over an
   open question is exactly the silence `consensus.py` refuses.
4. Every orchestrator **ESCALATED** step has a sealed human decision. An
   escalation is a request that went unanswered until someone answers
   it.
5. Every **envelope violation** in the range is accounted for. A call
   that went outside its behavioural contract is a defect, and a release
   is not the place to discover it.
6. The **regression constitution gate** passed for the range.

Each failure is a typed reason carrying what would clear it, because a
blocked release that does not say what to fix is an outage with extra
steps.

**What this is not.** It gates on the *record*, not on the code. A range
whose provenance is complete can still be a bad release; this refuses
the ones nobody could explain afterwards. Saying which of the two it is
matters more than the gate itself.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field

from . import provenance as prov

# ---------------------------------------------------------------------------
# Typed reasons
# ---------------------------------------------------------------------------

R_EMPTY_RANGE = "empty-range"
R_PROVENANCE_GAPS = "provenance-gaps"
R_UNSIGNED = "provenance-unsigned"
R_TAMPERED = "provenance-tampered"
R_UNRESOLVED_HOLD = "unresolved-hold"
R_OPEN_ESCALATION = "open-escalation"
R_ENVELOPE_VIOLATION = "envelope-violation"
R_GATE_NOT_RUN = "regression-gate-not-run"
R_GATE_FAILED = "regression-gate-failed"
R_UNVERIFIED_CALL = "call-without-verdict"

#: Every reason, with what it means and what clears it.
REASONS: dict[str, tuple[str, str]] = {
    R_EMPTY_RANGE: (
        "the range contains nothing to release",
        "widen the range, or do not cut a release from it"),
    R_PROVENANCE_GAPS: (
        "the provenance graph has holes: decisions with no traceable cause",
        "seal the missing events, or explain the gap and re-run — a gap is "
        "never filled in by inference"),
    R_UNSIGNED: (
        "the provenance graph was never signed",
        "build the graph with a signing key; an unsigned graph is not "
        "evidence"),
    R_TAMPERED: (
        "a provenance node no longer matches its signature",
        "the record was altered after the fact; investigate before "
        "releasing anything from it"),
    R_UNRESOLVED_HOLD: (
        "a consensus hold in this range was never resolved",
        "record a Resolution naming who decided and why"),
    R_OPEN_ESCALATION: (
        "a step escalated to a human and no decision was sealed",
        "answer the escalation; an unanswered question is not a pass"),
    R_ENVELOPE_VIOLATION: (
        "a call went outside its behavioural envelope in this range",
        "fix the tool, or widen the envelope if the effect is intended"),
    R_GATE_NOT_RUN: (
        "the regression constitution gate was not run for this range",
        "run `python -m fullagent.regressiongate --check` and seal it"),
    R_GATE_FAILED: (
        "the regression constitution gate blocked this range",
        "read its typed reasons; each one says what would clear it"),
    R_UNVERIFIED_CALL: (
        "a dispatched call in this range carries no envelope verdict",
        "run the dispatcher with an EnvelopeChecker so calls are judged "
        "as they happen"),
}


@dataclass(frozen=True)
class Reason:
    """One typed objection to cutting a release."""
    code: str
    what: str
    evidence: str = ""
    count: int = 1

    @property
    def remedy(self) -> str:
        return REASONS.get(self.code, ("", ""))[1]

    def to_dict(self) -> dict:
        return {"code": self.code, "what": self.what,
                "evidence": self.evidence, "count": self.count,
                "remedy": self.remedy}

    def format(self) -> str:
        head = f"  [{self.code}] {self.what}"
        if self.count > 1:
            head += f" (x{self.count})"
        if self.evidence:
            head += f"\n      evidence: {self.evidence}"
        return head + f"\n      to clear: {self.remedy}"


# ---------------------------------------------------------------------------
# The range
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Range:
    """The slice of the record a release is cut from.

    Expressed in event sequence numbers rather than commits, because the
    record this gate reads is the event log. `label` is whatever the
    caller calls the range -- a commit range, a tag, a date.
    """
    label: str
    start_seq: int = 0
    end_seq: int = 10 ** 12

    def holds(self, seq: int) -> bool:
        return self.start_seq <= seq <= self.end_seq

    def to_dict(self) -> dict:
        return {"label": self.label, "start_seq": self.start_seq,
                "end_seq": self.end_seq}


# ---------------------------------------------------------------------------
# The release — constructible only through the gate
# ---------------------------------------------------------------------------

#: A module-private token. `Release.__init__` demands it, and nothing
#: outside this module has one, so `Release(...)` raises everywhere else.
#: This is the difference between a gate someone can skip and a type
#: nobody can build the wrong way.
_GATE_TOKEN = object()


class ReleaseRefused(Exception):
    """Raised by `Release(...)` when called outside the gate."""


@dataclass(frozen=True)
class Evidence:
    """What the gate looked at, kept with the release it allowed."""
    nodes: int = 0
    edges: int = 0
    traces: tuple[str, ...] = ()
    calls: int = 0
    holds_resolved: int = 0
    escalations_answered: int = 0
    checked: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"nodes": self.nodes, "edges": self.edges,
                "traces": list(self.traces), "calls": self.calls,
                "holds_resolved": self.holds_resolved,
                "escalations_answered": self.escalations_answered,
                "checked": list(self.checked)}


class Release:
    """A release whose record is complete. Cannot be built any other way."""

    def __init__(self, token, rng: Range, digest: str, evidence: Evidence,
                 cut_at: float):
        if token is not _GATE_TOKEN:
            raise ReleaseRefused(
                "a Release is only constructible through build_release(); "
                "the point of this type is that holding one means the "
                "provenance behind it was complete")
        self.range = rng
        self.digest = digest
        self.evidence = evidence
        self.cut_at = cut_at

    def to_dict(self) -> dict:
        return {"range": self.range.to_dict(), "digest": self.digest,
                "cut_at": self.cut_at, "evidence": self.evidence.to_dict()}

    def format(self) -> str:
        e = self.evidence
        return "\n".join([
            f"RELEASE {self.range.label} — {self.digest}",
            f"  {e.nodes} provenance node(s), {e.edges} edge(s), "
            f"0 gaps, {len(e.traces)} trace(s)",
            f"  {e.calls} call(s) judged, {e.holds_resolved} hold(s) "
            f"resolved, {e.escalations_answered} escalation(s) answered",
            f"  checked: {', '.join(e.checked)}"])


@dataclass
class Refusal:
    """Why no release could be cut, in terms somebody can act on."""
    rng: Range
    reasons: tuple[Reason, ...]

    @property
    def allowed(self) -> bool:
        return False

    def to_dict(self) -> dict:
        return {"allowed": False, "range": self.rng.to_dict(),
                "reasons": [r.to_dict() for r in self.reasons]}

    def format(self) -> str:
        lines = [f"RELEASE REFUSED {self.rng.label} — "
                 f"{len(self.reasons)} reason(s)"]
        lines.extend(r.format() for r in self.reasons)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

#: Events the gate reads beyond the provenance graph itself.
WITNESS_EVENTS = ("consensus.audit", "consensus.resolved",
                  "orchestrator.step.done", "orchestrator.escalation.answered",
                  "envelope.violation", "dispatch.call", "regression.gate")


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def _in_range(log, rng: Range):
    for ev in log.events():
        if rng.holds(int(getattr(ev, "seq", 0) or 0)):
            yield ev


def build_release(log, rng: Range, key: bytes | None = None,
                  require_gate: bool = True,
                  require_verdicts: bool = False):
    """Cut a release, or explain why the record will not support one.

    Returns a `Release` or a `Refusal`. There is no third outcome and no
    override argument: a caller who wants to ship anyway has to say so
    somewhere this function can never see, which is the right place for
    that decision to live.
    """
    reasons: list[Reason] = []
    events = list(_in_range(log, rng))
    if not events:
        return Refusal(rng, (Reason(
            R_EMPTY_RANGE, "no events fall inside this range",
            f"seq {rng.start_seq}..{rng.end_seq}"),))

    # -- 1. the graph, and its holes -----------------------------------
    graph = prov.build(log, key)
    in_range_nodes = tuple(n for n in graph.order() if rng.holds(n.seq))
    node_ids = {n.id for n in in_range_nodes}
    gaps = tuple(g for g in graph.gaps if g.node in node_ids)
    if gaps:
        reasons.append(Reason(
            R_PROVENANCE_GAPS,
            "decisions in this range have no traceable cause",
            "; ".join(f"{g.wanted}: {g.detail}" for g in gaps[:3]),
            len(gaps)))

    # -- 2. is the record evidence at all? -----------------------------
    if key is None:
        reasons.append(Reason(
            R_UNSIGNED, "the provenance graph was built without a key",
            rng.label))
    else:
        ok, bad = graph.verify(key)
        if not ok:
            reasons.append(Reason(
                R_TAMPERED,
                "provenance nodes do not match their signatures",
                ", ".join(bad[:3]), len(bad)))

    # -- 3..6. the witnesses -------------------------------------------
    holds: dict[str, dict] = {}
    resolved: set[str] = set()
    escalated: dict[str, dict] = {}
    answered: set[str] = set()
    violations: list[str] = []
    unverified = 0
    calls = 0
    gate_seen = False
    gate_ok = False
    gate_detail = ""

    for ev in events:
        data = ev.data if isinstance(ev.data, dict) else {}
        if ev.type == "consensus.audit":
            if data.get("outcome") == "hold":
                holds[_audit_key(data)] = data
        elif ev.type == "consensus.resolved":
            resolved.add(_audit_key(data))
        elif ev.type == "orchestrator.step.done":
            path = str(data.get("path") or data.get("step") or "?")
            if data.get("status") == "escalated":
                escalated[f"{data.get('trace_id', '')}/{path}"] = data
        elif ev.type == "orchestrator.escalation.answered":
            path = str(data.get("path") or data.get("step") or "?")
            answered.add(f"{data.get('trace_id', '')}/{path}")
        elif ev.type == "envelope.violation":
            for v in (data.get("violations") or ()):
                if v.get("blocking"):
                    violations.append(f"{data.get('tool', '?')}: "
                                      f"{v.get('kind', '?')}")
        elif ev.type == "dispatch.call":
            calls += 1
            if data.get("envelope_ok") is None:
                unverified += 1
            elif data.get("envelope_ok") is False:
                violations.append(f"{data.get('tool', '?')}: sealed as "
                                  f"outside its envelope")
        elif ev.type == "regression.gate":
            gate_seen = True
            gate_ok = bool(data.get("allowed"))
            gate_detail = ", ".join(
                str(r.get("code")) for r in (data.get("reasons") or ()))

    open_holds = [k for k in holds if k not in resolved]
    if open_holds:
        reasons.append(Reason(
            R_UNRESOLVED_HOLD,
            "a consensus hold in this range was never settled",
            "; ".join(open_holds[:3]), len(open_holds)))

    open_escalations = [k for k in escalated if k not in answered]
    if open_escalations:
        reasons.append(Reason(
            R_OPEN_ESCALATION,
            "a step asked for a human and never got one",
            "; ".join(open_escalations[:3]), len(open_escalations)))

    if violations:
        reasons.append(Reason(
            R_ENVELOPE_VIOLATION,
            "a call did something its behavioural envelope forbids",
            "; ".join(sorted(set(violations))[:3]), len(violations)))

    if require_verdicts and unverified:
        reasons.append(Reason(
            R_UNVERIFIED_CALL,
            "calls in this range were dispatched with nothing judging "
            "their effects",
            f"{unverified} of {calls} call(s)", unverified))

    if require_gate:
        if not gate_seen:
            reasons.append(Reason(
                R_GATE_NOT_RUN,
                "no regression gate verdict was sealed in this range",
                rng.label))
        elif not gate_ok:
            reasons.append(Reason(
                R_GATE_FAILED, "the regression gate blocked this range",
                gate_detail or "see the sealed verdict"))

    if reasons:
        return Refusal(rng, tuple(reasons))

    checked = ["provenance gaps", "signatures", "consensus holds",
               "escalations", "envelopes"]
    if require_gate:
        checked.append("regression gate")
    if require_verdicts:
        checked.append("call verdicts")
    evidence = Evidence(
        nodes=len(in_range_nodes),
        edges=len([e for e in graph.edges if e.source in node_ids]),
        traces=tuple(sorted({n.trace_id for n in in_range_nodes
                             if n.trace_id})),
        calls=calls, holds_resolved=len(resolved),
        escalations_answered=len(answered), checked=tuple(checked))
    digest = _digest({"range": rng.to_dict(),
                      "nodes": [n.content_hash() for n in in_range_nodes]})
    return Release(_GATE_TOKEN, rng, digest, evidence, time.time())


def _audit_key(data: dict) -> str:
    """A stable name for one consensus audit, so a resolution can find it.

    Derived from the opinions rather than carried as an id, because the
    audit events were already being written before this gate existed and
    inventing an id now would mean two shapes of the same event.
    """
    opinions = data.get("opinions") or []
    return _digest([[o.get("strategy"), o.get("verdict"),
                     [f.get("why") for f in (o.get("findings") or [])]]
                    for o in opinions])


def gate_release(log, rng: Range, key: bytes | None = None, **kw):
    """`build_release`, with the verdict sealed to the log."""
    out = build_release(log, rng, key, **kw)
    try:
        log.append("release.gate",
                   out.to_dict() if isinstance(out, Refusal)
                   else {"allowed": True, **out.to_dict()},
                   actor="releasegate")
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    from .kernel import EventLog

    work = Path(tempfile.mkdtemp(prefix="fa-release-"))
    KEY = b"the release signing key"

    def fresh(name: str) -> EventLog:
        return EventLog(path=str(work / f"{name}.jsonl"))

    def clean_history(log) -> None:
        """A small, complete record: a plan, two steps, both sealed."""
        log.append("orchestrator.plan",
                   {"goal": "two things", "trace_id": "t1", "ok": True,
                    "steps": []}, actor="kernel")
        for path in ("first", "second"):
            log.append("orchestrator.step",
                       {"path": path, "trace_id": "t1", "tool": "write_file"},
                       actor="kernel")
            log.append("orchestrator.step.done",
                       {"path": path, "trace_id": "t1", "status": "ok"},
                       actor="kernel")
        log.append("policy.decision",
                   {"tool": "write_file", "outcome": "allow",
                    "rule": "capability", "reason": "role holds fs.write",
                    "trace_id": "t1"}, actor="kernel")
        log.append("dispatch.call",
                   {"tool": "write_file", "ok": True, "trace_id": "t1",
                    "effects": ["creates"], "envelope_ok": True},
                   actor="kernel")
        log.append("regression.gate", {"allowed": True, "reasons": []},
                   actor="regressiongate")

    ALL = Range("v1.0.0")

    # --- a complete record yields a release ---------------------------
    log = fresh("clean")
    clean_history(log)
    release = build_release(log, ALL, KEY)
    assert isinstance(release, Release), release.format()
    assert release.evidence.nodes and release.digest
    assert "provenance gaps" in release.evidence.checked
    assert "0 gaps" in release.format()

    # --- and the type cannot be built any other way -------------------
    try:
        Release(object(), ALL, "x", Evidence(), 0.0)
        raise AssertionError("a Release must not be constructible directly")
    except ReleaseRefused as exc:
        assert "build_release" in str(exc)

    # --- a gap refuses ------------------------------------------------
    log = fresh("gappy")
    clean_history(log)
    # a step with no plan sealed for its trace: provenance records a Gap
    log.append("orchestrator.step",
               {"path": "orphan", "trace_id": "t-nowhere",
                "tool": "write_file"}, actor="kernel")
    refused = build_release(log, ALL, KEY)
    assert isinstance(refused, Refusal), "a gap must refuse"
    codes = [r.code for r in refused.reasons]
    assert R_PROVENANCE_GAPS in codes, codes
    assert refused.reasons[0].remedy
    assert "REFUSED" in refused.format()

    # --- an unsigned graph is not evidence ----------------------------
    log = fresh("unsigned")
    clean_history(log)
    unsigned = build_release(log, ALL, None)
    assert isinstance(unsigned, Refusal)
    assert R_UNSIGNED in [r.code for r in unsigned.reasons]

    # --- an unresolved hold refuses -----------------------------------
    log = fresh("held")
    clean_history(log)
    held_audit = {"outcome": "hold",
                  "opinions": [{"strategy": "guardrail", "verdict": "fail",
                                "findings": [{"why": "no evidence"}]},
                               {"strategy": "independent",
                                "verdict": "pass", "findings": []}]}
    log.append("consensus.audit", held_audit, actor="consensus")
    open_hold = build_release(log, ALL, KEY)
    assert isinstance(open_hold, Refusal)
    assert R_UNRESOLVED_HOLD in [r.code for r in open_hold.reasons]

    # ...and resolving it clears exactly that reason
    log.append("consensus.resolved",
               {**held_audit, "resolution": {"by": "the operator",
                                             "release": False}},
               actor="consensus")
    settled = build_release(log, ALL, KEY)
    assert isinstance(settled, Release), settled.format()
    assert settled.evidence.holds_resolved == 1

    # --- an unanswered escalation refuses -----------------------------
    log = fresh("escalated")
    clean_history(log)
    log.append("orchestrator.step",
               {"path": "risky", "trace_id": "t1", "tool": "run_command"},
               actor="kernel")
    log.append("orchestrator.step.done",
               {"path": "risky", "trace_id": "t1", "status": "escalated",
                "detail": "not repeatable"}, actor="kernel")
    stuck = build_release(log, ALL, KEY)
    assert isinstance(stuck, Refusal)
    assert R_OPEN_ESCALATION in [r.code for r in stuck.reasons]

    log.append("orchestrator.escalation.answered",
               {"path": "risky", "trace_id": "t1", "by": "the operator",
                "decision": "accepted the partial state"}, actor="kernel")
    answered_now = build_release(log, ALL, KEY)
    assert isinstance(answered_now, Release), answered_now.format()
    assert answered_now.evidence.escalations_answered == 1

    # --- an envelope violation refuses --------------------------------
    log = fresh("violated")
    clean_history(log)
    log.append("dispatch.call",
               {"tool": "read_file", "ok": True, "trace_id": "t1",
                "effects": ["modifies"], "envelope_ok": False},
               actor="kernel")
    breached = build_release(log, ALL, KEY)
    assert isinstance(breached, Refusal)
    assert R_ENVELOPE_VIOLATION in [r.code for r in breached.reasons]

    # --- a missing or failed regression gate refuses ------------------
    log = fresh("ungated")
    log.append("orchestrator.plan",
               {"goal": "g", "trace_id": "t1", "ok": True, "steps": []},
               actor="kernel")
    ungated = build_release(log, ALL, KEY)
    assert isinstance(ungated, Refusal)
    assert R_GATE_NOT_RUN in [r.code for r in ungated.reasons]
    assert isinstance(build_release(log, ALL, KEY, require_gate=False),
                      Release)

    log = fresh("gate-failed")
    clean_history(log)
    log.append("regression.gate",
               {"allowed": False,
                "reasons": [{"code": "false-negative"}]},
               actor="regressiongate")
    blocked = build_release(log, ALL, KEY)
    assert isinstance(blocked, Refusal)
    assert R_GATE_FAILED in [r.code for r in blocked.reasons]
    assert "false-negative" in blocked.format()

    # --- unjudged calls, only when the caller asks --------------------
    log = fresh("unjudged")
    clean_history(log)
    log.append("dispatch.call", {"tool": "read_file", "ok": True,
                                 "trace_id": "t1"}, actor="kernel")
    assert isinstance(build_release(log, ALL, KEY), Release)
    strict = build_release(log, ALL, KEY, require_verdicts=True)
    assert isinstance(strict, Refusal)
    assert R_UNVERIFIED_CALL in [r.code for r in strict.reasons]

    # --- an empty range refuses ---------------------------------------
    empty = build_release(fresh("empty"), ALL, KEY)
    assert isinstance(empty, Refusal)
    assert [r.code for r in empty.reasons] == [R_EMPTY_RANGE]

    # --- the range actually slices ------------------------------------
    log = fresh("sliced")
    clean_history(log)
    head = log.head()
    log.append("orchestrator.step",
               {"path": "orphan", "trace_id": "t-nowhere"}, actor="kernel")
    # the whole log has the gap; the earlier slice does not
    assert isinstance(build_release(log, Range("all"), KEY), Refusal)
    early = build_release(log, Range("early", 0, head), KEY)
    assert isinstance(early, Release), early.format()

    # --- every reason has a remedy ------------------------------------
    for code, (what, remedy) in REASONS.items():
        assert what and remedy, code
        assert Reason(code, what).remedy == remedy

    # --- the verdict is sealed ----------------------------------------
    log = fresh("sealed")
    clean_history(log)
    gate_release(log, ALL, KEY)
    assert "release.gate" in {e.type for e in log.events()}

    print(release.format())
    print(blocked.format())
    print(f"RELEASE GATE SELF-TEST PASS — {len(REASONS)} typed reason(s); "
          f"a Release is unconstructible outside the gate")

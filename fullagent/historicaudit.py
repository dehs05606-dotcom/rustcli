"""REPRODUCIBLE HISTORICAL AUDIT — re-checking a past decision against the
rules that were in force when it was made.

"Was that release compliant?" is asked months after the release. The
tempting way to answer it is to run today's checks over the old record.
That answers a different question — *would we decide this the same way
now* — and answers it without saying so. The two diverge the moment a
rule changes, which is the whole reason anyone is asking.

So this module keeps **ruleset bundles**: a content-addressed, signed
snapshot of the rule data a policy decision depends on — stage order,
roles and their ceilings and roots, the tool capability manifest, the
destructive-command pattern, the path-argument map — sealed to the event
log at the moment it was captured. A decision's *governing* bundle is the
newest one sealed at or before that decision. Replay rebuilds the request
from the facts sealed with the decision and re-runs it against the
governing bundle.

Four rules the module is built on:

1. **Replay never falls back to current rules.** If no bundle governs a
   decision, or the bundle will not verify, or the facts needed to
   rebuild the request were never sealed, the verdict is a typed
   statement of that — `V_NO_RULESET`, `V_TAMPERED`, `V_UNREPLAYABLE` —
   and the decision is left unjudged. Quietly re-deciding it under
   today's rules would produce a confident answer to a question nobody
   asked.

2. **Not replayable is a verdict, not a gap.** The counts are reported
   per kind and the report is only `complete` when every decision in
   range got a real answer. A summary that says "12 of 12 agree" while
   400 were skipped is the failure mode this exists to prevent.

3. **The bundle stores rule data, not rule code.** Replay executes the
   *current* stage classes over the *historical* rule data. That is what
   "the rules as of then" can mean here, and it is less than it sounds:
   if the code inside `PathStage` changed, replay uses the new code. The
   bundle records the stage names it was captured with, so a stage that
   has since disappeared is reported as `V_STAGE_GONE` rather than
   silently dropped — but a stage whose *implementation* changed under
   the same name is invisible to this module, and `REPLAY_LIMITS` says
   so where a reader will find it.

4. **A disagreement is a finding about the code, not about the past.**
   `V_DIFFERS` means the same request, under the same rules, is decided
   differently today. That is a regression in the decision path, and it
   is the one verdict here that should stop a build.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .toolpolicy import ALLOW

BUNDLE_EVENT = "ruleset.recorded"
AUDIT_EVENT = "historicaudit.run"

#: What replay cannot see, stated where it is read rather than in a
#: changelog. Quoted verbatim by the dashboard and the assurance case.
REPLAY_LIMITS: tuple[str, ...] = (
    "a bundle stores the rule data, not the code of the stages that read "
    "it, so a stage whose implementation changed under the same name "
    "replays with today's behaviour and no verdict marks it",
    "only decisions that were sealed can be replayed, and the policy "
    "seals refusals and questions, not permissions — a past allow is not "
    "in the record to re-check",
    "a decision sealed before this module existed carries no request "
    "facts and comes back V_UNREPLAYABLE, which is a statement about the "
    "record and not about the decision",
)

# -- verdict kinds ---------------------------------------------------------

V_AGREES = "agrees"
V_DIFFERS = "differs"
V_UNREPLAYABLE = "unreplayable"
V_NO_RULESET = "no-governing-ruleset"
V_TAMPERED = "ruleset-tampered"
V_STAGE_GONE = "stage-no-longer-exists"

VERDICTS: dict[str, tuple[str, str]] = {
    V_AGREES: ("re-deciding it under the rules of the day gives the same "
               "answer", ""),
    V_DIFFERS: ("the same request under the same rules is decided "
                "differently now",
                "a regression in the decision path — find what changed "
                "between the stage code then and now"),
    V_UNREPLAYABLE: ("the record does not carry the facts the decision was "
                     "made from",
                     "nothing can be concluded about this decision; newer "
                     "decisions seal their inputs"),
    V_NO_RULESET: ("no ruleset bundle was sealed at or before this "
                   "decision",
                   "capture a bundle — until one exists, decisions before "
                   "it can only be read, not re-checked"),
    V_TAMPERED: ("the governing bundle does not match its signature",
                 "the bundle is not evidence; do not replay against it"),
    V_STAGE_GONE: ("the bundle names a policy stage this build no longer "
                   "has",
                   "replay would silently drop the stage, so it is refused "
                   "instead"),
}

#: The verdicts that mean a decision was actually re-checked.
JUDGED = (V_AGREES, V_DIFFERS)


def _sha(payload) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Bundles
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Ruleset:
    """A snapshot of the rule data a policy decision depends on."""
    digest: str
    at: float
    rules: dict = field(default_factory=dict)
    surfaces: dict = field(default_factory=dict)
    label: str = ""
    signature: str = ""
    #: The log sequence this bundle was sealed at. -1 for one that has
    #: only been captured and not recorded.
    seq: int = -1

    def sign(self, key: bytes) -> str:
        return hmac.new(key, self.digest.encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def verify(self, key: bytes | None) -> bool:
        """Whether this bundle is still evidence.

        With no key there is nothing to verify against, and the honest
        answer is False: an unsigned bundle is a record, not a proof.
        """
        if key is None or not self.signature:
            return False
        if _sha(self.rules) != self.digest:
            return False
        return hmac.compare_digest(self.sign(key), self.signature)

    def stages(self) -> tuple[str, ...]:
        return tuple(self.rules.get("stages") or ())

    def to_dict(self) -> dict:
        return {"digest": self.digest, "at": self.at, "label": self.label,
                "rules": self.rules, "surfaces": self.surfaces,
                "signature": self.signature, "seq": self.seq}

    @classmethod
    def from_dict(cls, data: dict, seq: int = -1) -> "Ruleset":
        """Rebuild a bundle from a sealed event.

        `seq` is the event's own position and wins over anything in the
        payload: the payload was written *before* the append, so it
        carries the -1 of a bundle that had not been recorded yet. A
        bundle that believes it has no position governs nothing, and
        every decision after it then reads as ungoverned.
        """
        stored = data.get("seq")
        fallback = int(stored) if isinstance(stored, int) else -1
        return cls(str(data.get("digest") or ""),
                   float(data.get("at") or 0.0),
                   dict(data.get("rules") or {}),
                   dict(data.get("surfaces") or {}),
                   str(data.get("label") or ""),
                   str(data.get("signature") or ""),
                   seq if seq >= 0 else fallback)


def capture(label: str = "", key: bytes | None = None,
            root: Path | None = None) -> Ruleset:
    """Snapshot the rules as they stand right now.

    Reads the live modules rather than a configuration file, for the same
    reason contracts are derived and not restated: a second copy of the
    rules is a second thing to keep in step.
    """
    from .policypipeline import DEFAULT_STAGES
    from .toolpolicy import (DESTRUCTIVE_RE, PATH_ARGS, ROLES,
                             TOOL_CAPABILITIES)

    rules = {
        "stages": [s.name for s in DEFAULT_STAGES],
        "roles": {name: {"capabilities": sorted(r.capabilities),
                         "ask": sorted(r.ask_capabilities),
                         "ceilings": dict(sorted(r.ceilings.items())),
                         "roots": list(r.roots),
                         "destructive": r.allow_destructive_commands,
                         "hosts": list(r.allowed_hosts)}
                  for name, r in sorted(ROLES.items())},
        "manifest": {tool: sorted(caps)
                     for tool, caps in sorted(TOOL_CAPABILITIES.items())},
        "destructive_pattern": DESTRUCTIVE_RE.pattern,
        "path_args": {k: list(v) for k, v in sorted(PATH_ARGS.items())},
    }
    surfaces: dict = {}
    try:
        from .regressiongate import fingerprint
        surfaces = fingerprint(root=root).to_dict()
    except Exception:
        # A fingerprint we could not take is left empty rather than
        # guessed at; the bundle is still a valid snapshot of the rules.
        surfaces = {}

    digest = _sha(rules)
    bundle = Ruleset(digest, time.time(), rules, surfaces, label)
    if key is not None:
        bundle = Ruleset(digest, bundle.at, rules, surfaces, label,
                         bundle.sign(key))
    return bundle


def record(log, bundle: Ruleset) -> Ruleset:
    """Seal a bundle to the log, which is what gives it a position in time."""
    log.append(BUNDLE_EVENT, bundle.to_dict(), actor="historicaudit")
    return Ruleset(bundle.digest, bundle.at, bundle.rules, bundle.surfaces,
                   bundle.label, bundle.signature, _last_seq(log))


def _seq_of(event) -> int:
    """An event's position, with zero meaning zero.

    Written out rather than `int(getattr(ev, "seq", -1) or -1)`, which is
    the shape this started as: seq 0 is falsy, so the very first event in
    a log came back as -1. The first bundle ever recorded is exactly the
    one that lands at seq 0, so it governed nothing and every decision
    after it read as ungoverned.
    """
    value = getattr(event, "seq", None)
    return int(value) if isinstance(value, int) else -1


def _last_seq(log) -> int:
    try:
        events = list(log.events())
        return _seq_of(events[-1]) if events else -1
    except Exception:
        return -1


def bundles(log) -> tuple[Ruleset, ...]:
    """Every bundle in the record, oldest first."""
    out: list[Ruleset] = []
    for ev in log.events():
        if ev.type != BUNDLE_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        out.append(Ruleset.from_dict(data, _seq_of(ev)))
    return tuple(out)


def governing(known: tuple[Ruleset, ...], seq: int) -> Ruleset | None:
    """The newest bundle sealed at or before this decision.

    Strictly at-or-before: a rule written after a decision did not govern
    it, however much it looks like it should have.
    """
    best: Ruleset | None = None
    for bundle in known:
        if 0 <= bundle.seq <= seq and (best is None or bundle.seq > best.seq):
            best = bundle
    return best


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

#: The facts a sealed decision must carry to be re-runnable. Named here so
#: an unreplayable verdict can say which one was missing rather than
#: "malformed".
REQUIRED_FACTS = ("tool", "role", "capabilities", "roots", "known")


@dataclass(frozen=True)
class Replay:
    """One historical decision, re-checked or explained."""
    seq: int
    tool: str
    kind: str
    recorded: str = ""
    replayed: str = ""
    ruleset: str = ""
    detail: str = ""

    @property
    def judged(self) -> bool:
        return self.kind in JUDGED

    @property
    def what(self) -> str:
        return VERDICTS.get(self.kind, ("", ""))[0]

    def to_dict(self) -> dict:
        return {"seq": self.seq, "tool": self.tool, "kind": self.kind,
                "recorded": self.recorded, "replayed": self.replayed,
                "ruleset": self.ruleset, "detail": self.detail,
                "what": self.what}

    def line(self) -> str:
        mark = {V_AGREES: "ok  ", V_DIFFERS: "FAIL"}.get(self.kind, "--  ")
        tail = f" ({self.recorded} -> {self.replayed})" \
            if self.kind == V_DIFFERS else \
            (f" — {self.detail}" if self.detail else "")
        return f"  {mark} seq {self.seq:<6} {self.tool:<16} {self.kind}{tail}"


def _role_from(rules: dict, name: str):
    """Rebuild the role as it was, not as it is."""
    from .toolpolicy import Role
    spec = (rules.get("roles") or {}).get(name)
    if spec is None:
        return None
    return Role(name=name,
                capabilities=frozenset(spec.get("capabilities") or ()),
                roots=tuple(spec.get("roots") or ()),
                ask_capabilities=frozenset(spec.get("ask") or ()),
                ceilings=dict(spec.get("ceilings") or {}),
                allow_destructive_commands=bool(spec.get("destructive")),
                allowed_hosts=tuple(spec.get("hosts") or ()))


def _pipeline_from(rules: dict):
    """The pipeline in the order the bundle recorded.

    Returns `(pipeline, missing)`. A stage the bundle names and this build
    no longer has is *not* skipped: replay is refused, because a pipeline
    short one stage is a more permissive pipeline and its answers would
    be quietly wrong in the dangerous direction.
    """
    from .policypipeline import DEFAULT_STAGES, PolicyPipeline
    by_name = {s.name: s for s in DEFAULT_STAGES}
    wanted = list(rules.get("stages") or [])
    missing = [n for n in wanted if n not in by_name]
    if missing:
        return None, tuple(missing)
    return PolicyPipeline(tuple(by_name[n] for n in wanted)), ()


def replay_one(data: dict, seq: int, bundle: Ruleset | None,
               key: bytes | None) -> Replay:
    """Re-decide one sealed decision under the rules that governed it."""
    recorded = str(data.get("outcome") or "")
    facts = data.get("request")
    tool = str((facts or {}).get("tool") or data.get("tool") or "?")

    if bundle is None:
        return Replay(seq, tool, V_NO_RULESET, recorded,
                      detail="no bundle was sealed at or before this point")
    if not bundle.verify(key):
        return Replay(seq, tool, V_TAMPERED, recorded,
                      ruleset=bundle.digest[:12],
                      detail="the bundle will not verify, so it is not "
                             "evidence to replay against")
    if not isinstance(facts, dict):
        return Replay(seq, tool, V_UNREPLAYABLE, recorded,
                      ruleset=bundle.digest[:12],
                      detail="the decision was sealed without the request "
                             "facts it was made from")
    absent = [k for k in REQUIRED_FACTS if k not in facts]
    if absent:
        return Replay(seq, tool, V_UNREPLAYABLE, recorded,
                      ruleset=bundle.digest[:12],
                      detail=f"sealed without {', '.join(absent)}")

    pipeline, gone = _pipeline_from(bundle.rules)
    if pipeline is None:
        return Replay(seq, tool, V_STAGE_GONE, recorded,
                      ruleset=bundle.digest[:12],
                      detail=f"this build has no {', '.join(gone)} stage")

    role = _role_from(bundle.rules, str(facts.get("role") or ""))
    if role is None:
        return Replay(seq, tool, V_UNREPLAYABLE, recorded,
                      ruleset=bundle.digest[:12],
                      detail=f"role {facts.get('role')!r} is not in the "
                             f"bundle, so its limits are unknown")

    from .policypipeline import Request
    request = Request(
        tool=tool, args=dict(facts.get("args") or {}), role=role,
        capabilities=frozenset(facts.get("capabilities") or ()),
        roots=tuple(facts.get("roots") or ()),
        counts=dict(facts.get("counts") or {}),
        known=bool(facts.get("known")))
    try:
        again = pipeline.decide(request).outcome
    except Exception as exc:       # noqa: BLE001
        return Replay(seq, tool, V_UNREPLAYABLE, recorded,
                      ruleset=bundle.digest[:12],
                      detail=f"replay raised {type(exc).__name__}: {exc}")

    kind = V_AGREES if again == recorded else V_DIFFERS
    return Replay(seq, tool, kind, recorded, again, bundle.digest[:12])


@dataclass
class AuditReport:
    replays: tuple[Replay, ...] = ()
    known: tuple[Ruleset, ...] = ()
    label: str = ""
    at: float = 0.0

    @property
    def counts(self) -> dict[str, int]:
        out = {kind: 0 for kind in VERDICTS}
        for r in self.replays:
            out[r.kind] = out.get(r.kind, 0) + 1
        return out

    @property
    def judged(self) -> int:
        return sum(1 for r in self.replays if r.judged)

    @property
    def differing(self) -> tuple[Replay, ...]:
        return tuple(r for r in self.replays if r.kind == V_DIFFERS)

    @property
    def complete(self) -> bool:
        """Whether every decision in range actually got re-checked.

        Kept apart from `ok` on purpose. A run with nothing to replay is
        not a run that proved anything, and the two questions — "did any
        decision change" and "did we manage to check them all" — have
        different answers and different remedies.
        """
        return bool(self.replays) and self.judged == len(self.replays)

    @property
    def ok(self) -> bool:
        return not self.differing

    def to_dict(self) -> dict:
        return {"ok": self.ok, "complete": self.complete, "label": self.label,
                "at": self.at, "judged": self.judged,
                "decisions": len(self.replays), "counts": self.counts,
                "rulesets": [{"digest": b.digest[:12], "seq": b.seq,
                              "label": b.label, "at": b.at}
                             for b in self.known],
                "replays": [r.to_dict() for r in self.replays],
                "limits": list(REPLAY_LIMITS)}

    def format(self) -> str:
        head = (f"HISTORICAL AUDIT — {self.judged} of {len(self.replays)} "
                f"sealed decision(s) re-checked against the rules of the day")
        if not self.ok:
            head += f" — {len(self.differing)} now decided differently"
        elif not self.complete:
            head += " — incomplete"
        lines = [head,
                 f"  {len(self.known)} ruleset bundle(s) in the record"]
        for kind, count in sorted(self.counts.items()):
            if count:
                lines.append(f"  {count:>5}  {kind}")
        lines.extend(r.line() for r in self.replays
                     if r.kind != V_AGREES)
        return "\n".join(lines)


def audit(log, key: bytes | None = None, start_seq: int = 0,
          end_seq: int = 10 ** 12, label: str = "") -> AuditReport:
    """Re-check every sealed decision in a range against its own ruleset."""
    known = bundles(log)
    replays: list[Replay] = []
    for ev in log.events():
        if ev.type != "policy.decision":
            continue
        seq = _seq_of(ev)
        if not (start_seq <= seq <= end_seq):
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        replays.append(replay_one(data, seq, governing(known, seq), key))

    report = AuditReport(tuple(replays), known, label, time.time())
    try:
        log.append(AUDIT_EVENT, report.to_dict(), actor="historicaudit")
    except Exception:
        pass
    return report


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import dataclasses
    import sys
    import tempfile

    from .kernel import EventLog
    from .toolpolicy import ROLES, ToolPolicy

    argv = sys.argv[1:]
    work = Path(tempfile.mkdtemp(prefix="fa-history-"))
    key = b"historical-audit-key"

    if "--capture" in argv:
        print(json.dumps(capture("cli").to_dict(), indent=2))
        raise SystemExit(0)

    # -- a real session: capture the rules, then make real decisions ----
    log = EventLog(path=work / "session.jsonl")
    bundle = record(log, capture("v1", key))
    assert bundle.seq >= 0 and bundle.verify(key)
    assert not bundle.verify(b"another key"), "a wrong key must not verify"
    assert not bundle.verify(None), "an unsigned bundle is not evidence"

    policy = ToolPolicy(role="developer", log=log, roots=[str(work)])
    denied = policy.evaluate("read_file", {"path": "/etc/passwd"})
    assert denied.denied
    asked = policy.evaluate("delete_path",
                            {"path": str(work / "a.txt")})
    assert asked.outcome != ALLOW, asked.to_dict()
    blocked = policy.evaluate("web_fetch",
                              {"url": "http://169.254.169.254/"})
    assert blocked.denied

    report = audit(log, key, label="v1")
    assert report.ok and report.complete, report.format()
    assert len(report.replays) == 3, report.format()
    assert report.judged == 3
    assert report.counts[V_AGREES] == 3, report.counts

    # The sealed facts are the decision's inputs and nothing more: no
    # file contents, no prompt text.
    facts = [e.data["request"] for e in log.events()
             if e.type == "policy.decision"]
    assert all(set(f) == set(REQUIRED_FACTS) | {"args", "counts"}
               for f in facts), facts
    written = policy.replayable_facts(
        "write_file", {"path": "p", "content": "a secret"})
    assert "content" not in written["args"], written
    assert written["args"] == {"path": "p"}, written

    # -- a rule change, and the same request under the old rules --------
    #
    # The decision is re-checked against the bundle that governed it, so
    # tightening the rules afterwards does not retroactively make a past
    # allow look like a refusal. That is the whole claim of this module,
    # so it gets a test with a real rule change in it.
    tight = dataclasses.replace(ROLES["developer"],
                                ceilings={"read_file": 0})
    tightened = dict(bundle.rules)
    tightened["roles"] = dict(tightened["roles"])
    tightened["roles"]["developer"] = {
        **tightened["roles"]["developer"], "ceilings": {"read_file": 0}}
    newer = Ruleset(_sha(tightened), time.time(), tightened, {}, "v2")
    newer = dataclasses.replace(newer, signature=newer.sign(key))
    after = record(log, newer)
    assert after.seq > bundle.seq

    later = policy.evaluate("read_file", {"path": "/etc/passwd"})
    assert later.denied
    second = audit(log, key)
    # Every decision still agrees: the three old ones were judged under
    # v1, the new one under v2. If governing() had used the newest bundle
    # for everything, the old ones would now be measured against a
    # ceiling that did not exist when they were made.
    assert second.ok and second.complete, second.format()
    assert second.counts[V_AGREES] == 4, second.counts
    assert {r.ruleset for r in second.replays} == {
        bundle.digest[:12], newer.digest[:12]}, second.format()
    assert governing(bundles(log), bundle.seq).digest == bundle.digest
    assert governing(bundles(log), 10 ** 9).digest == newer.digest

    # -- replay never falls back to today's rules -----------------------
    orphan = EventLog(path=work / "orphan.jsonl")
    lonely = ToolPolicy(role="developer", log=orphan, roots=[str(work)])
    lonely.evaluate("read_file", {"path": "/etc/passwd"})
    nothing = audit(orphan, key)
    assert len(nothing.replays) == 1
    assert nothing.replays[0].kind == V_NO_RULESET, nothing.format()
    assert not nothing.complete, "an unjudged decision is not a pass"
    assert nothing.ok, "nothing disagreed; that is a separate question"

    # -- a decision sealed before this module existed -------------------
    old = EventLog(path=work / "old.jsonl")
    record(old, capture("v0", key))
    old.append("policy.decision",
               {"outcome": "deny", "tool": "read_file",
                "reason": "outside the roots", "role": "developer",
                "rule": "path-confinement", "rationale": []},
               actor="kernel")
    legacy = audit(old, key)
    assert legacy.replays[0].kind == V_UNREPLAYABLE, legacy.format()
    assert "request facts" in legacy.replays[0].detail
    assert not legacy.complete

    # -- a bundle that will not verify is not replayed against ----------
    bad = EventLog(path=work / "bad.jsonl")
    record(bad, capture("unsigned"))          # captured with no key
    ToolPolicy(role="developer", log=bad,
               roots=[str(work)]).evaluate("read_file",
                                           {"path": "/etc/passwd"})
    untrusted = audit(bad, key)
    assert untrusted.replays[0].kind == V_TAMPERED, untrusted.format()

    forged = EventLog(path=work / "forged.jsonl")
    signed = capture("v1", key)
    mangled = dict(signed.rules)
    mangled["destructive_pattern"] = "never-matches-anything"
    record(forged, dataclasses.replace(signed, rules=mangled))
    ToolPolicy(role="developer", log=forged,
               roots=[str(work)]).evaluate("read_file",
                                           {"path": "/etc/passwd"})
    edited = audit(forged, key)
    assert edited.replays[0].kind == V_TAMPERED, edited.format()

    # -- a stage that no longer exists refuses replay, never drops it ---
    gone = EventLog(path=work / "gone.jsonl")
    extended = dict(signed.rules)
    extended["stages"] = list(extended["stages"]) + ["stage-from-the-future"]
    future = Ruleset(_sha(extended), time.time(), extended, {}, "v-future")
    record(gone, dataclasses.replace(future, signature=future.sign(key)))
    ToolPolicy(role="developer", log=gone,
               roots=[str(work)]).evaluate("read_file",
                                           {"path": "/etc/passwd"})
    missing = audit(gone, key)
    assert missing.replays[0].kind == V_STAGE_GONE, missing.format()
    assert "stage-from-the-future" in missing.replays[0].detail

    # -- a real disagreement is caught ----------------------------------
    #
    # Same request, same bundle, but the sealed verdict says allow. This
    # is what a regression in the decision path looks like from here, and
    # it is the one verdict that fails the report.
    drift = EventLog(path=work / "drift.jsonl")
    record(drift, signed)
    drift.append("policy.decision",
                 {"outcome": "allow", "tool": "read_file",
                  "role": "developer", "rule": "path-confinement",
                  "rationale": [],
                  "request": {"tool": "read_file",
                              "args": {"path": "/etc/passwd"},
                              "role": "developer",
                              "capabilities": ["fs.read"],
                              "roots": [str(work)],
                              "counts": {}, "known": True}},
                 actor="kernel")
    regressed = audit(drift, key)
    assert not regressed.ok, regressed.format()
    assert regressed.differing[0].kind == V_DIFFERS
    assert regressed.differing[0].recorded == "allow"
    assert regressed.differing[0].replayed == "deny"

    # -- the limits are carried in the report, not just in a docstring --
    payload = second.to_dict()
    assert payload["limits"] == list(REPLAY_LIMITS)
    assert payload["complete"] is True and payload["judged"] == 4
    assert json.loads(json.dumps(payload)) == payload
    assert any(e.type == AUDIT_EVENT for e in log.events())

    print(second.format())
    print(f"HISTORICAUDIT SELF-TEST PASS — {second.judged} decision(s) "
          f"re-checked across {len(second.known)} ruleset(s), "
          f"{len(REPLAY_LIMITS)} stated limit(s)")

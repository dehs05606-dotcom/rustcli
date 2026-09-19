"""ENVELOPES — what a call is allowed to DO, not just what it may say.

A JSON Schema decides whether a call is well-formed. It has nothing to
say about whether the call did what it claimed. `write_file` that
returns "wrote 40 bytes" and leaves no file is schema-valid and a lie;
`read_file` that quietly rewrites the file it read is schema-valid and
much worse.

A **behavioural envelope** is the second contract: the set of state
transitions a tool may cause, the class of repetition it belongs to, and
which of its arguments name things in the world. A call outside its
envelope is a defect even when every field type-checks.

Three properties make this worth having rather than decorative:

**Observation is bounded and honest.** Only the paths the call's own
arguments name are observed, before and after. That is cheap -- a few
stats and hashes -- and it is the whole scope of the claim. A tool that
writes somewhere it never named is invisible here, and this module says
so rather than implying whole-filesystem coverage it does not have.

**The check runs twice, independently.** Once at dispatch, against the
real filesystem, where a violation turns a "successful" call into the
typed defect it is. Once at seal time, re-derived from what the log
recorded, where a checker that was buggy or switched off at dispatch is
caught by a second reading of the same evidence. Two derivations of one
claim disagree when one of them is wrong -- the same reason
`consensus.py` exists one layer up.

**A tool with no envelope is reported, never assumed clean.** The
failure mode worth fearing is a green board over a measurement that
never fired, so an unenveloped tool is a finding with its own code.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .toolcontract import (IDEMPOTENT, NON_IDEMPOTENT, UNSAFE,
                           ToolContract)

# ---------------------------------------------------------------------------
# The vocabulary of effects
# ---------------------------------------------------------------------------

FX_READ = "reads"          # observes the world, changes nothing
FX_CREATE = "creates"      # a path that did not exist now does
FX_MODIFY = "modifies"     # a path that existed has different content
FX_DELETE = "deletes"      # a path that existed no longer does
FX_EXECUTE = "executes"    # runs a subprocess; effects unbounded by design
FX_EGRESS = "egress"       # leaves the machine

EFFECTS = (FX_READ, FX_CREATE, FX_MODIFY, FX_DELETE, FX_EXECUTE, FX_EGRESS)

#: Effects this module can actually observe. `executes` and `egress`
#: are declared and reasoned about but not measured -- naming them here
#: keeps the difference between "checked" and "declared" explicit rather
#: than leaving a reader to assume the stronger one.
OBSERVABLE = frozenset({FX_READ, FX_CREATE, FX_MODIFY, FX_DELETE})

# ---------------------------------------------------------------------------
# Repetition classes
# ---------------------------------------------------------------------------

PURE = "pure"                  # no effect at all; same answer every time
REPEATABLE = "repeatable"      # safe to repeat: same end state
AT_MOST_ONCE = "at-most-once"  # repeating changes the world again
IRREVERSIBLE = "irreversible"  # repeating may destroy something else

CLASSES = (PURE, REPEATABLE, AT_MOST_ONCE, IRREVERSIBLE)

#: Which contract idempotency each class is consistent with. A tool
#: cannot be `pure` in its envelope and non-idempotent in its contract:
#: one of the two is wrong, and an invariant says which pairs are legal
#: rather than leaving it to whoever edits next.
CONSISTENT_WITH = {
    PURE: (IDEMPOTENT,),
    REPEATABLE: (IDEMPOTENT,),
    AT_MOST_ONCE: (NON_IDEMPOTENT,),
    IRREVERSIBLE: (UNSAFE,),
}


# ---------------------------------------------------------------------------
# Violations
# ---------------------------------------------------------------------------

V_UNDECLARED = "undeclared-effect"
V_MISSING = "missing-effect"
V_PRECONDITION = "precondition-unmet"
V_CLASS = "class-disagrees-with-contract"
V_NO_ENVELOPE = "tool-has-no-envelope"
V_UNSEALED = "effects-not-sealed"

#: Every violation kind, with what it means and what clears it.
KINDS: dict[str, tuple[str, str]] = {
    V_UNDECLARED: (
        "the call caused an effect its envelope does not allow",
        "widen the envelope if the effect is intended, or fix the tool"),
    V_MISSING: (
        "the call reported success but its declared effect did not happen",
        "the tool is reporting a success it did not achieve"),
    V_PRECONDITION: (
        "the call succeeded on a path its envelope says must already exist",
        "the tool should have refused; check its existence handling"),
    V_CLASS: (
        "the envelope's repetition class contradicts the contract's "
        "idempotency",
        "one of the two is wrong — decide which and change that one"),
    V_NO_ENVELOPE: (
        "a tool was called that has no behavioural envelope",
        "declare one in ENVELOPES; an unenveloped tool is unchecked, not "
        "clean"),
    V_UNSEALED: (
        "a call was sealed without the effects it caused",
        "the dispatcher ran without an envelope checker, so the seal-time "
        "reading has nothing to re-derive from"),
}


@dataclass(frozen=True)
class Violation:
    kind: str
    tool: str
    detail: str
    evidence: str = ""

    @property
    def blocking(self) -> bool:
        """Whether this turns a successful call into a failed one.

        A call that did something undeclared, or claimed an effect it did
        not produce, did not do what it said. The other kinds are
        findings about the *declarations*, which a running call cannot be
        blamed for.
        """
        return self.kind in (V_UNDECLARED, V_MISSING, V_PRECONDITION)

    @property
    def remedy(self) -> str:
        return KINDS.get(self.kind, ("", ""))[1]

    def to_dict(self) -> dict:
        return {"kind": self.kind, "tool": self.tool, "detail": self.detail,
                "evidence": self.evidence, "blocking": self.blocking}

    def line(self) -> str:
        mark = "!!" if self.blocking else " ~"
        return f"  {mark} [{self.kind}] {self.tool}: {self.detail}"


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Envelope:
    """One tool's behavioural contract."""
    tool: str
    effects: frozenset[str]
    klass: str
    path_args: tuple[str, ...] = ()
    #: Path arguments whose target must already exist for the call to be
    #: able to succeed honestly.
    requires_existing: tuple[str, ...] = ()
    #: Path arguments the call is allowed to leave absent afterwards.
    summary: str = ""

    def paths(self, args: dict) -> tuple[str, ...]:
        out = []
        for key in self.path_args:
            value = args.get(key)
            if isinstance(value, str) and value:
                out.append(value)
        return tuple(out)

    @property
    def observable(self) -> frozenset[str]:
        return frozenset(self.effects) & OBSERVABLE

    @property
    def measurable(self) -> bool:
        """Whether a run of this tool can be checked at all.

        A tool that declares only `executes` has no observable effect
        this module can measure. Saying that out loud is the difference
        between a check that passed and a check that never ran.
        """
        return bool(self.path_args) and bool(self.observable - {FX_READ})

    def to_dict(self) -> dict:
        return {"tool": self.tool, "effects": sorted(self.effects),
                "class": self.klass, "path_args": list(self.path_args),
                "requires_existing": list(self.requires_existing),
                "summary": self.summary}


def _env(tool, effects, klass, path_args=(), requires_existing=(),
         summary="") -> Envelope:
    return Envelope(tool, frozenset(effects), klass, tuple(path_args),
                    tuple(requires_existing), summary)


#: The shipped envelopes, one per registered tool.
ENVELOPES: dict[str, Envelope] = {e.tool: e for e in (
    _env("read_file", {FX_READ}, PURE, ("path",), ("path",),
         "reads one file and changes nothing"),
    _env("file_info", {FX_READ}, PURE, ("path",), ("path",),
         "stats one path and changes nothing"),
    _env("list_dir", {FX_READ}, PURE, ("path",), (),
         "lists a directory and changes nothing"),
    _env("glob_files", {FX_READ}, PURE, ("path",), (),
         "matches names under a directory and changes nothing"),
    _env("search_files", {FX_READ}, PURE, ("path",), (),
         "searches the tree and changes nothing"),
    _env("write_file", {FX_CREATE, FX_MODIFY}, REPEATABLE, ("path",), (),
         "leaves the path existing with exactly the given content"),
    _env("edit_file", {FX_MODIFY, FX_READ}, AT_MOST_ONCE, ("path",),
         ("path",),
         "changes part of a file that must already be there"),
    _env("apply_patch", {FX_MODIFY, FX_CREATE, FX_DELETE, FX_READ},
         AT_MOST_ONCE, (), (),
         "applies a diff; the paths come from the patch, not the args"),
    _env("create_directory", {FX_CREATE}, REPEATABLE, ("path",), (),
         "leaves the directory existing"),
    _env("delete_path", {FX_DELETE, FX_READ}, IRREVERSIBLE, ("path",), (),
         "leaves the path absent; repeating may remove a new occupant"),
    _env("move_path", {FX_CREATE, FX_DELETE, FX_MODIFY, FX_READ},
         IRREVERSIBLE, ("src", "dst"), ("src",),
         "the source goes away and the destination appears"),
    _env("copy_path", {FX_CREATE, FX_MODIFY, FX_READ}, REPEATABLE,
         ("src", "dst"), ("src",),
         "the destination appears; the source is untouched"),
    _env("run_command", {FX_EXECUTE}, IRREVERSIBLE, (), (),
         "runs a subprocess; its effects are outside what this observes"),
    _env("live_shell", {FX_EXECUTE}, IRREVERSIBLE, (), (),
         "runs in a persistent shell; effects outside what this observes"),
    _env("live_shell_reset", {FX_EXECUTE}, REPEATABLE, (), (),
         "discards the persistent shell"),
    _env("web_fetch", {FX_EGRESS, FX_READ}, REPEATABLE, (), (),
         "leaves the machine; changes nothing locally"),
    _env("web_search", {FX_EGRESS, FX_READ}, REPEATABLE, (), (),
         "leaves the machine; changes nothing locally"),
)}

# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------

#: Files larger than this are compared by size and mtime rather than by
#: content. Hashing a 400MB artifact to decide whether `edit_file` did
#: anything would make the check cost more than the call.
HASH_LIMIT = 4 * 1024 * 1024


@dataclass(frozen=True)
class PathState:
    """What one path looked like at one moment."""
    path: str
    exists: bool
    is_dir: bool = False
    size: int = -1
    digest: str = ""
    mtime: float = 0.0

    def same_content_as(self, other: "PathState") -> bool:
        if self.digest and other.digest:
            return self.digest == other.digest
        return self.size == other.size and self.mtime == other.mtime

    def to_dict(self) -> dict:
        return {"path": self.path, "exists": self.exists,
                "is_dir": self.is_dir, "size": self.size,
                "digest": self.digest}


def look(path: str) -> PathState:
    """One path, as cheaply as the question allows. Never raises."""
    try:
        p = Path(path)
        if not p.exists():
            return PathState(path, False)
        stat = p.stat()
        if p.is_dir():
            return PathState(path, True, True, -1, "", stat.st_mtime)
        digest = ""
        if stat.st_size <= HASH_LIMIT:
            digest = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        return PathState(path, True, False, stat.st_size, digest,
                         stat.st_mtime)
    except (OSError, ValueError):
        # An unreadable path is not an absent one, and calling it absent
        # would manufacture a `deletes` effect out of a permission error.
        return PathState(path, True, False, -1, "", 0.0)


@dataclass
class Observation:
    """The world before a call, and after it, at the paths it named."""
    tool: str
    paths: tuple[str, ...] = ()
    before: dict[str, PathState] = field(default_factory=dict)
    after: dict[str, PathState] = field(default_factory=dict)

    def effects(self) -> frozenset[str]:
        """What actually happened, in the vocabulary of effects."""
        seen: set[str] = set()
        for path in self.paths:
            was, now = self.before.get(path), self.after.get(path)
            if was is None or now is None:
                continue
            if not was.exists and now.exists:
                seen.add(FX_CREATE)
            elif was.exists and not now.exists:
                seen.add(FX_DELETE)
            elif was.exists and now.exists and not was.same_content_as(now):
                seen.add(FX_MODIFY)
        return frozenset(seen)

    def to_dict(self) -> dict:
        return {"tool": self.tool, "paths": list(self.paths),
                "effects": sorted(self.effects()),
                "before": {k: v.to_dict() for k, v in self.before.items()},
                "after": {k: v.to_dict() for k, v in self.after.items()}}


def observe(tool: str, paths: tuple[str, ...]) -> Observation:
    obs = Observation(tool, paths)
    obs.before = {p: look(p) for p in paths}
    return obs


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    """One call, judged against its envelope."""
    tool: str
    violations: tuple[Violation, ...] = ()
    observed: frozenset[str] = frozenset()
    checked: bool = False
    why_unchecked: str = ""

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def blocking(self) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.blocking)

    def to_dict(self) -> dict:
        return {"tool": self.tool, "ok": self.ok, "checked": self.checked,
                "observed": sorted(self.observed),
                "why_unchecked": self.why_unchecked,
                "violations": [v.to_dict() for v in self.violations]}


def judge(envelope: Envelope | None, tool: str, args: dict, ok: bool,
          obs: Observation | None) -> Verdict:
    """Compare one call's envelope against what was observed.

    A pure function of (envelope, args, outcome, observation), so the
    dispatch-time check and the seal-time check are literally the same
    code reading two different sources of the same facts.
    """
    if envelope is None:
        return Verdict(tool, (Violation(
            V_NO_ENVELOPE, tool,
            "this tool has no behavioural envelope, so nothing about its "
            "effects was checked"),), frozenset(), False,
            "no envelope declared")

    if obs is None:
        return Verdict(tool, (), frozenset(), False,
                       "no observation was taken")

    observed = obs.effects()
    violations: list[Violation] = []

    # -- effects outside the envelope ---------------------------------
    for effect in sorted(observed - envelope.effects):
        violations.append(Violation(
            V_UNDECLARED, tool,
            f"it {effect} a path, which its envelope does not allow",
            ", ".join(_changed(obs, effect)[:3])))

    if not ok:
        # A failed call may legitimately have done nothing. What it may
        # NOT have done is change something, and that is already covered
        # above -- so there is nothing further to require of it.
        return Verdict(tool, tuple(violations), observed, True)

    # -- a success that produced none of its declared effects ----------
    wanted = envelope.observable - {FX_READ}
    if wanted and obs.paths and not observed:
        violations.append(Violation(
            V_MISSING, tool,
            f"it reported success and declares {'/'.join(sorted(wanted))}, "
            f"but nothing changed at "
            f"{', '.join(obs.paths[:3])}",
            ", ".join(obs.paths[:3])))

    # -- a success on a path that was never there ----------------------
    for key in envelope.requires_existing:
        path = args.get(key)
        if not isinstance(path, str) or not path:
            continue
        was = obs.before.get(path)
        if was is not None and not was.exists:
            violations.append(Violation(
                V_PRECONDITION, tool,
                f"it reported success with {key}={path!r}, which did not "
                f"exist when the call started", path))

    return Verdict(tool, tuple(violations), observed, True)


def _changed(obs: Observation, effect: str) -> list[str]:
    out = []
    for path in obs.paths:
        was, now = obs.before.get(path), obs.after.get(path)
        if was is None or now is None:
            continue
        if effect == FX_CREATE and not was.exists and now.exists:
            out.append(path)
        elif effect == FX_DELETE and was.exists and not now.exists:
            out.append(path)
        elif (effect == FX_MODIFY and was.exists and now.exists
                and not was.same_content_as(now)):
            out.append(path)
    return out


class EnvelopeChecker:
    """The dispatch-time hook. Cheap enough to leave on."""

    def __init__(self, envelopes: dict[str, Envelope] | None = None,
                 log=None):
        self.envelopes = dict(ENVELOPES if envelopes is None else envelopes)
        self.log = log

    def before(self, tool: str, args: dict) -> Observation | None:
        env = self.envelopes.get(tool)
        if env is None:
            return None
        return observe(tool, env.paths(args))

    def after(self, tool: str, args: dict, ok: bool,
              obs: Observation | None) -> Verdict:
        env = self.envelopes.get(tool)
        if obs is not None:
            obs.after = {p: look(p) for p in obs.paths}
        verdict = judge(env, tool, args, ok, obs)
        if self.log is not None and not verdict.ok:
            try:
                self.log.append("envelope.violation", verdict.to_dict(),
                                actor="envelopes")
            except Exception:
                pass
        return verdict


# ---------------------------------------------------------------------------
# Static checks — the declarations against each other
# ---------------------------------------------------------------------------

def check_declarations(contracts: dict[str, ToolContract],
                       envelopes: dict[str, Envelope] | None = None
                       ) -> tuple[Violation, ...]:
    """Every envelope against its contract, and every tool against the
    envelope set. Runs without calling anything."""
    envelopes = ENVELOPES if envelopes is None else envelopes
    out: list[Violation] = []
    for name, contract in sorted(contracts.items()):
        env = envelopes.get(name)
        if env is None:
            out.append(Violation(
                V_NO_ENVELOPE, name,
                "registered tool with no behavioural envelope"))
            continue
        allowed = CONSISTENT_WITH.get(env.klass, ())
        if contract.idempotency not in allowed:
            out.append(Violation(
                V_CLASS, name,
                f"envelope class {env.klass!r} allows "
                f"{'/'.join(allowed)}, but the contract says "
                f"{contract.idempotency!r}",
                f"{env.klass} vs {contract.idempotency}"))
    for name in sorted(set(envelopes) - set(contracts)):
        out.append(Violation(
            V_NO_ENVELOPE, name,
            "an envelope names a tool that is not registered"))
    return tuple(out)


# ---------------------------------------------------------------------------
# Seal-time — the same judgement, re-derived from the log
# ---------------------------------------------------------------------------

@dataclass
class SealReport:
    """What the log says about calls that were already judged once."""
    calls: int = 0
    judged: int = 0
    unsealed: int = 0
    violations: tuple[Violation, ...] = ()
    disagreements: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations and not self.disagreements

    def to_dict(self) -> dict:
        return {"ok": self.ok, "calls": self.calls, "judged": self.judged,
                "unsealed": self.unsealed,
                "violations": [v.to_dict() for v in self.violations],
                "disagreements": list(self.disagreements)}

    def format(self) -> str:
        head = (f"ENVELOPE SEAL — {self.calls} sealed call(s), "
                f"{self.judged} carrying effects, {self.unsealed} not")
        if self.ok:
            return head + " — consistent"
        lines = [head + f" — {len(self.violations)} violation(s), "
                        f"{len(self.disagreements)} disagreement(s)"]
        lines.extend(v.line() for v in self.violations)
        lines.extend(f"  !! {d}" for d in self.disagreements)
        return "\n".join(lines)


def audit_seal(log, envelopes: dict[str, Envelope] | None = None,
               require_effects: bool = False) -> SealReport:
    """Re-judge every sealed call from what the log recorded.

    The dispatch-time check reads the filesystem; this one reads the
    seal. When they disagree, one of the two is wrong and the report
    says so rather than preferring either -- the same rule the consensus
    layer applies to replies.
    """
    envelopes = ENVELOPES if envelopes is None else envelopes
    report = SealReport()
    violations: list[Violation] = []
    disagreements: list[str] = []

    for ev in log.events():
        if ev.type != "dispatch.call":
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        tool = str(data.get("tool") or "")
        if not tool:
            continue
        report.calls += 1
        env = envelopes.get(tool)
        if env is None:
            violations.append(Violation(
                V_NO_ENVELOPE, tool,
                "a sealed call whose tool has no envelope"))
            continue
        if "effects" not in data:
            report.unsealed += 1
            if require_effects:
                violations.append(Violation(
                    V_UNSEALED, tool,
                    "sealed without the effects it caused, so the "
                    "seal-time reading has nothing to check"))
            continue
        report.judged += 1
        observed = frozenset(data.get("effects") or ())
        for effect in sorted(observed - env.effects):
            violations.append(Violation(
                V_UNDECLARED, tool,
                f"the seal records {effect}, which the envelope does not "
                f"allow", f"seq {getattr(ev, 'seq', '?')}"))
        # The dispatcher's own verdict rides along. If it said clean and
        # this reading does not, the two disagree about the same facts.
        sealed_ok = data.get("envelope_ok")
        if sealed_ok is True and (observed - env.effects):
            disagreements.append(
                f"{tool}: the dispatcher sealed this call as clean, but "
                f"its recorded effects are outside the envelope")
        if sealed_ok is False and not (observed - env.effects):
            disagreements.append(
                f"{tool}: the dispatcher sealed a violation that the "
                f"recorded effects do not show — one of the two readings "
                f"is wrong")

    report.violations = tuple(violations)
    report.disagreements = tuple(disagreements)
    return report


def format_envelopes(envelopes: dict[str, Envelope] | None = None) -> str:
    envelopes = ENVELOPES if envelopes is None else envelopes
    measurable = [e for e in envelopes.values() if e.measurable]
    lines = [f"ENVELOPES — {len(envelopes)} tool(s), "
             f"{len(measurable)} with effects this module can measure"]
    for name in sorted(envelopes):
        e = envelopes[name]
        mark = "m" if e.measurable else "-"
        lines.append(f"  {mark} {name:<18} {e.klass:<14} "
                     f"{', '.join(sorted(e.effects)) or 'nothing'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import tempfile

    from .kernel import EventLog
    from .toolcontract import build_contracts
    from .tools import build_registry

    argv = sys.argv[1:]
    contracts = build_contracts(build_registry())

    if "--check" in argv:
        found = check_declarations(contracts)
        print(format_envelopes())
        for v in found:
            print(v.line())
        raise SystemExit(1 if found else 0)

    if "--json" in argv:
        print(json.dumps({n: e.to_dict() for n, e in ENVELOPES.items()},
                         indent=2, sort_keys=True))
        raise SystemExit(0)

    work = Path(tempfile.mkdtemp(prefix="fa-envelopes-"))
    log = EventLog(path=str(work / "events.jsonl"))
    checker = EnvelopeChecker(log=log)

    # --- the declarations agree with the contracts --------------------
    problems = check_declarations(contracts)
    assert not problems, "\n".join(p.line() for p in problems)
    assert set(ENVELOPES) >= set(contracts), \
        sorted(set(contracts) - set(ENVELOPES))

    # --- an honest write is clean -------------------------------------
    target = work / "a.txt"
    args = {"path": str(target), "content": "hello"}
    obs = checker.before("write_file", args)
    target.write_text("hello", encoding="utf-8")
    verdict = checker.after("write_file", args, True, obs)
    assert verdict.ok, verdict.to_dict()
    assert verdict.observed == frozenset({FX_CREATE}), verdict.observed
    assert verdict.checked

    # --- a write that wrote nothing is caught -------------------------
    ghost = {"path": str(work / "never.txt"), "content": "x"}
    obs = checker.before("write_file", ghost)
    lying = checker.after("write_file", ghost, True, obs)
    assert not lying.ok and lying.blocking, lying.to_dict()
    assert lying.blocking[0].kind == V_MISSING, lying.to_dict()
    assert lying.blocking[0].remedy

    # --- a read that modified the file is caught ----------------------
    args = {"path": str(target)}
    obs = checker.before("read_file", args)
    target.write_text("tampered", encoding="utf-8")
    meddling = checker.after("read_file", args, True, obs)
    assert not meddling.ok, meddling.to_dict()
    kinds = {v.kind for v in meddling.violations}
    assert kinds == {V_UNDECLARED}, meddling.to_dict()
    assert meddling.violations[0].blocking

    # --- a read that reads is clean -----------------------------------
    obs = checker.before("read_file", args)
    assert checker.after("read_file", args, True, obs).ok

    # --- success on a path that was never there -----------------------
    absent = {"path": str(work / "absent.txt")}
    obs = checker.before("read_file", absent)
    impossible = checker.after("read_file", absent, True, obs)
    assert any(v.kind == V_PRECONDITION for v in impossible.violations), \
        impossible.to_dict()
    # ...but the same call honestly reporting failure is fine
    obs = checker.before("read_file", absent)
    assert checker.after("read_file", absent, False, obs).ok

    # --- a delete that deleted is clean, one that did not is not ------
    args = {"path": str(target)}
    obs = checker.before("delete_path", args)
    target.unlink()
    gone = checker.after("delete_path", args, True, obs)
    assert gone.ok and gone.observed == frozenset({FX_DELETE}), gone.to_dict()

    kept = work / "kept.txt"
    kept.write_text("still here", encoding="utf-8")
    args = {"path": str(kept)}
    obs = checker.before("delete_path", args)
    pretending = checker.after("delete_path", args, True, obs)
    assert any(v.kind == V_MISSING for v in pretending.violations), \
        pretending.to_dict()

    # --- a failed call that changed nothing is not a violation --------
    obs = checker.before("delete_path", args)
    assert checker.after("delete_path", args, False, obs).ok

    # --- a failed call that DID change something is -------------------
    obs = checker.before("delete_path", args)
    kept.unlink()
    (work / "kept.txt").write_text("resurrected", encoding="utf-8")
    # it exists again with different content: modify, not delete
    half = checker.after("delete_path", args, False, obs)
    assert any(v.kind == V_UNDECLARED for v in half.violations), \
        half.to_dict()

    # --- a tool with no envelope is reported, never assumed clean -----
    orphan = judge(None, "nobody_declared_me", {}, True, None)
    assert not orphan.ok and not orphan.checked
    assert orphan.violations[0].kind == V_NO_ENVELOPE
    assert not orphan.violations[0].blocking, \
        "a missing declaration is a finding about us, not about the call"

    # --- an unmeasurable envelope says so rather than passing ---------
    assert not ENVELOPES["run_command"].measurable
    assert ENVELOPES["write_file"].measurable
    unmeasured = checker.after("run_command", {"command": "true"}, True,
                               checker.before("run_command",
                                              {"command": "true"}))
    assert unmeasured.ok and unmeasured.observed == frozenset()

    # --- a class that contradicts its contract is caught --------------
    import dataclasses
    wrong = dict(ENVELOPES)
    wrong["write_file"] = dataclasses.replace(ENVELOPES["write_file"],
                                              klass=AT_MOST_ONCE)
    found = check_declarations(contracts, wrong)
    assert [v.kind for v in found] == [V_CLASS], [v.to_dict() for v in found]

    orphaned = dict(ENVELOPES)
    orphaned["not_a_tool"] = _env("not_a_tool", {FX_READ}, PURE)
    assert any(v.kind == V_NO_ENVELOPE
               for v in check_declarations(contracts, orphaned))

    # --- seal time: the same judgement from the log alone -------------
    seal = EventLog(path=str(work / "seal.jsonl"))
    seal.append("dispatch.call",
                {"tool": "read_file", "ok": True, "effects": [],
                 "envelope_ok": True}, actor="kernel")
    seal.append("dispatch.call",
                {"tool": "write_file", "ok": True, "effects": [FX_CREATE],
                 "envelope_ok": True}, actor="kernel")
    clean = audit_seal(seal)
    assert clean.ok, clean.format()
    assert clean.calls == 2 and clean.judged == 2

    seal.append("dispatch.call",
                {"tool": "read_file", "ok": True, "effects": [FX_MODIFY],
                 "envelope_ok": True}, actor="kernel")
    caught = audit_seal(seal)
    assert not caught.ok, caught.format()
    assert any(v.kind == V_UNDECLARED for v in caught.violations)
    assert caught.disagreements, \
        "a call sealed clean whose effects are outside its envelope is a " \
        "disagreement between the two readings"

    # a call sealed without effects is counted, and only demanded when asked
    bare = EventLog(path=str(work / "bare.jsonl"))
    bare.append("dispatch.call", {"tool": "read_file", "ok": True},
                actor="kernel")
    assert audit_seal(bare).ok and audit_seal(bare).unsealed == 1
    assert not audit_seal(bare, require_effects=True).ok

    # a sealed violation the effects do not support is also a disagreement
    odd = EventLog(path=str(work / "odd.jsonl"))
    odd.append("dispatch.call",
               {"tool": "read_file", "ok": True, "effects": [],
                "envelope_ok": False}, actor="kernel")
    assert audit_seal(odd).disagreements, audit_seal(odd).format()

    # --- violations were sealed as they happened ----------------------
    kinds = {e.type for e in log.events()}
    assert "envelope.violation" in kinds

    print(format_envelopes())
    print(f"ENVELOPES SELF-TEST PASS — {len(ENVELOPES)} envelope(s), "
          f"{len(KINDS)} violation kind(s), "
          f"{len([e for e in ENVELOPES.values() if e.measurable])} "
          f"measurable")

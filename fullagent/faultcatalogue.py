"""COUNTERFACTUAL FAILURE CATALOGUE — one provoked fault per typed refusal.

`runbook.py` proves the *recovery playbooks* still work: for each of the
nine error codes it makes that failure happen and checks the disposition
matches what `recovery.py` promises. That covers one surface. The rest of
the platform refuses things too, and each of those refusals has a typed
code — a policy stage's reason, an envelope violation kind, a release
gate reason, an assurance-case defect. Every one of those codes is a
promise that something specific gets caught.

Nothing was checking those promises. A code can sit in a `dict` with a
nicely-worded explanation for a year while the branch that emits it has
been dead since a refactor, and every test stays green, because tests
check that good input passes. The board is greenest exactly when no one
has tried to break anything.

So: for every typed refusal the platform can emit, a **counterfactual** —
a small deterministic scenario that makes that exact thing go wrong and
reads back which code came out. Three things fall out of that, and the
third is the one worth having:

  * **Coverage is per class, not a percentage over a suite.** A surface
    reports which of its codes have a counterfactual and which do not.
  * **An uncovered class is a typed defect, not a warning.** Same rule as
    `runbook.D_UNCOVERED`, extended from the nine error codes to every
    surveyed surface. It fails the gate; it does not print a note.
  * **Unreachable is a verdict, and it is falsifiable.** Some codes
    cannot be provoked through the entry point that owns them — the
    branch guards a caller that does not exist yet. Declaring one
    unreachable is allowed, requires a written reason, and is *checked*:
    the counterfactual still runs, and if the code does come out, the
    declaration is stale and that is a typed defect of its own
    (`C_REACHED_THE_UNREACHABLE`). A claim that something cannot happen
    is worth exactly as much as the attempt to make it happen.

The universes are **derived, never restated**. A surface declares its
classes by reading the real module's own constants, so adding a reason
code to `releasegate.REASONS` makes this catalogue go red until somebody
either writes the counterfactual or declares the code unreachable with a
reason. That is the forcing function; a hand-copied list would just drift
quietly and keep passing.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Defect kinds
# ---------------------------------------------------------------------------

C_UNCOVERED = "failure-class-uncovered"
C_WRONG_CLASS = "fault-came-back-as-another-class"
C_SILENT = "fault-produced-no-typed-outcome"
C_HARNESS_BROKEN = "counterfactual-could-not-run"
C_PHANTOM = "counterfactual-targets-a-class-that-is-gone"
C_REACHED_THE_UNREACHABLE = "class-declared-unreachable-was-reached"
C_UNREADABLE = "surface-universe-could-not-be-read"

DEFECTS: dict[str, tuple[str, str]] = {
    C_UNCOVERED: (
        "a typed refusal has no counterfactual, so nothing has ever "
        "confirmed it fires",
        "write a counterfactual for it, or declare it unreachable with a "
        "reason in UNREACHABLE"),
    C_WRONG_CLASS: (
        "the injected fault was caught, but reported as a different class",
        "either the classifier is wrong or the counterfactual provokes "
        "something other than it claims — both are worth knowing"),
    C_SILENT: (
        "the injected fault produced no typed outcome at all",
        "the surface did not notice; this is the failure mode the whole "
        "catalogue exists for"),
    C_HARNESS_BROKEN: (
        "the counterfactual could not set itself up",
        "fix the scenario — a harness that cannot run is not a class that "
        "passed, and folding the two together is how a suite starts lying"),
    C_PHANTOM: (
        "a counterfactual targets a class its surface no longer declares",
        "delete the counterfactual, or restore the class"),
    C_REACHED_THE_UNREACHABLE: (
        "a class declared unreachable was provoked after all",
        "remove the UNREACHABLE entry and keep the counterfactual — the "
        "reason it was written for no longer holds"),
    C_UNREADABLE: (
        "a surface's universe could not be read from its module",
        "the surface's `universe` callable is broken, so its coverage is "
        "unknown rather than complete"),
}


# ---------------------------------------------------------------------------
# Surfaces — each declares its universe by reading the real module
# ---------------------------------------------------------------------------

S_TAXONOMY = "error-taxonomy"
S_POLICY = "policy-pipeline"
S_ENVELOPE = "behavioural-envelope"
S_RELEASE = "release-gate"
S_ASSURANCE = "assurance-case"


@dataclass(frozen=True)
class Surface:
    """A place the platform refuses things, and the codes it refuses with.

    `universe` is a callable and not a tuple on purpose: it reads the
    owning module's own constants at run time, so this catalogue cannot
    hold a stale copy of a list that has moved on.
    """
    id: str
    what: str
    universe: Callable[[], tuple[str, ...]]


def _taxonomy_universe() -> tuple[str, ...]:
    from .toolcontract import ERROR_CODES
    return tuple(sorted(ERROR_CODES))


def _policy_universe() -> tuple[str, ...]:
    """Every code a policy stage can refuse or question with.

    Derived from the metamodel's declared per-stage code sets, minus the
    two codes that mean "nothing to say here" — a satisfied stage and a
    skipped one are not refusals and have nothing to provoke.
    """
    from .policymeta import STAGE_MODELS
    from .policypipeline import R_NOT_APPLICABLE, R_SATISFIED
    codes = {c for m in STAGE_MODELS.values() for c in m.codes}
    codes -= {R_SATISFIED, R_NOT_APPLICABLE}
    codes.add("stage-error")   # the pipeline's own fail-closed code
    return tuple(sorted(codes))


def _envelope_universe() -> tuple[str, ...]:
    from .envelopes import KINDS
    return tuple(sorted(KINDS))


def _release_universe() -> tuple[str, ...]:
    from .releasegate import REASONS
    return tuple(sorted(REASONS))


def _assurance_universe() -> tuple[str, ...]:
    from .assurance import DEFECTS as A_DEFECTS
    return tuple(sorted(A_DEFECTS))


SURFACES: tuple[Surface, ...] = (
    Surface(S_TAXONOMY,
            "how a tool failure is classified at the dispatch boundary",
            _taxonomy_universe),
    Surface(S_POLICY, "why a permission decision refused or asked",
            _policy_universe),
    Surface(S_ENVELOPE, "how a call broke its behavioural contract",
            _envelope_universe),
    Surface(S_RELEASE, "why the record will not support a release",
            _release_universe),
    Surface(S_ASSURANCE, "how an argument tree fails to be an argument",
            _assurance_universe),
)


#: Classes that cannot be provoked through the entry point that owns
#: them, each with the reason. This is a claim, not an exemption: the
#: counterfactual still runs, and reaching one of these is a defect.
UNREACHABLE: dict[str, str] = {
    "provenance-tampered":
        "`build_release` derives the provenance graph and verifies it "
        "inside one call, signing and checking with the same key, so no "
        "input it accepts can make a node disagree with its own "
        "signature. The branch guards a future caller that hands in a "
        "graph signed elsewhere; until one exists the code is reachable "
        "only from `provenance.Graph.verify`, where it is tested "
        "directly. Delete this entry the day such a caller lands.",
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Defect:
    kind: str
    surface: str
    subject: str
    detail: str

    @property
    def what(self) -> str:
        return DEFECTS.get(self.kind, ("", ""))[0]

    @property
    def remedy(self) -> str:
        return DEFECTS.get(self.kind, ("", ""))[1]

    def to_dict(self) -> dict:
        return {"kind": self.kind, "surface": self.surface,
                "subject": self.subject, "detail": self.detail,
                "what": self.what, "remedy": self.remedy}

    def line(self) -> str:
        return f"  [{self.kind}] {self.surface}/{self.subject}: {self.detail}"


@dataclass
class Outcome:
    """One counterfactual, run."""
    id: str
    surface: str
    target: str
    provoked: bool = False
    observed: tuple[str, ...] = ()
    defect: Defect | None = None
    detail: str = ""
    duration: float = 0.0

    def to_dict(self) -> dict:
        return {"id": self.id, "surface": self.surface,
                "target": self.target, "provoked": self.provoked,
                "observed": list(self.observed), "detail": self.detail,
                "duration": round(self.duration, 4),
                "defect": self.defect.to_dict() if self.defect else None}

    def line(self) -> str:
        mark = "ok  " if self.defect is None else "FAIL"
        tail = "" if self.provoked else \
            f" -> {', '.join(self.observed) or 'nothing'}"
        return f"  {mark} {self.surface:<22} {self.target:<34}{tail}"


@dataclass
class Coverage:
    """One surface's per-class tally. Not a percentage on its own."""
    surface: str
    declared: tuple[str, ...] = ()
    covered: tuple[str, ...] = ()
    unreachable: tuple[str, ...] = ()
    uncovered: tuple[str, ...] = ()

    @property
    def ratio(self) -> float:
        """Covered-or-declared-unreachable over declared.

        Reported beside the class lists, never instead of them: "83%" is
        not an answer to "which failure can we not catch".
        """
        if not self.declared:
            return 1.0
        return (len(self.covered) + len(self.unreachable)) \
            / len(self.declared)

    def to_dict(self) -> dict:
        return {"surface": self.surface, "declared": list(self.declared),
                "covered": list(self.covered),
                "unreachable": list(self.unreachable),
                "uncovered": list(self.uncovered),
                "ratio": round(self.ratio, 4)}

    def line(self) -> str:
        mark = "ok  " if not self.uncovered else "FAIL"
        extra = f", {len(self.unreachable)} unreachable" \
            if self.unreachable else ""
        return (f"  {mark} {self.surface:<22} "
                f"{len(self.covered)}/{len(self.declared)} provoked{extra}")


@dataclass
class Catalogue:
    outcomes: tuple[Outcome, ...] = ()
    coverage: tuple[Coverage, ...] = ()
    defects: tuple[Defect, ...] = ()
    digest: str = ""
    at: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.defects

    @property
    def provoked(self) -> int:
        return sum(1 for o in self.outcomes if o.provoked)

    @property
    def classes(self) -> int:
        return sum(len(c.declared) for c in self.coverage)

    def uncovered(self) -> tuple[Defect, ...]:
        return tuple(d for d in self.defects if d.kind == C_UNCOVERED)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "digest": self.digest, "at": self.at,
                "classes": self.classes, "provoked": self.provoked,
                "outcomes": [o.to_dict() for o in self.outcomes],
                "coverage": [c.to_dict() for c in self.coverage],
                "defects": [d.to_dict() for d in self.defects]}

    def format(self) -> str:
        lines = [f"FAULT CATALOGUE — {self.provoked} of {self.classes} "
                 f"typed failure class(es) provoked on purpose"
                 + ("" if self.ok else
                    f" — {len(self.defects)} defect(s)"),
                 f"  digest {self.digest}"]
        lines.extend(c.line() for c in self.coverage)
        lines.extend(o.line() for o in self.outcomes if o.defect is not None)
        lines.extend(d.line() for d in self.defects)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Counterfactuals
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Counterfactual:
    """One deterministic way to break one thing, and what should come back.

    `run` returns every typed code the surface emitted. The target must be
    among them; the rest are kept because a fault that trips three checks
    at once is a fact about the system worth seeing, not noise to discard.
    """
    surface: str
    target: str
    what: str
    run: Callable[[Path], tuple[str, ...]]

    @property
    def id(self) -> str:
        return f"{self.surface}/{self.target}"


class Injected(Exception):
    """A fault this module caused on purpose."""


# -- the error taxonomy ----------------------------------------------------
#
# The runbook already makes each of these failures happen and checks the
# *recovery disposition*. This checks the step before it: that the
# dispatch boundary classified the failure as the code it claims. A
# misclassification leaves the runbook green -- it would faithfully prove
# the playbook for the wrong class.

def _taxonomy_case(code: str) -> Callable[[Path], tuple[str, ...]]:
    def run(root: Path) -> tuple[str, ...]:
        from .dispatch import Dispatcher
        from .runbook import INJECTORS, _chaos_contract
        factory, args = INJECTORS[code]
        dispatcher = Dispatcher(approve=lambda c, a: True)
        dispatcher.register(_chaos_contract("chaos", True), factory())
        result = dispatcher.call("chaos", dict(args))
        if result.ok:
            return ()
        return (result.error.code,) if result.error else ()
    return run


# -- the policy pipeline ---------------------------------------------------

#: Stand-in for "a path inside this scenario's own root". A relative path
#: resolves against the working directory, which is outside the root, so
#: writing one by hand makes every scenario a path-confinement test by
#: accident -- which is exactly how `needs-confirmation` first came back
#: as `outside-roots`.
IN_ROOT = "<root>"


def _policy_request(root: Path, tool: str, args: dict, role_name: str,
                    caps: tuple, counts: dict | None = None,
                    known: bool = True):
    from .policypipeline import Request
    from .toolpolicy import ROLES
    resolved = {k: (str(root / "subject.txt") if v == IN_ROOT else v)
                for k, v in args.items()}
    return Request(tool, resolved, ROLES[role_name], frozenset(caps),
                   (str(root),), dict(counts or {}), known)


def _policy_codes(decision) -> tuple[str, ...]:
    """Every code a stage refused or questioned with."""
    from .toolpolicy import ASK, DENY
    return tuple(r.code for r in decision.rationale
                 if r.outcome in (DENY, ASK))


def _policy_case(tool: str, args: dict, role_name: str = "developer",
                 caps: tuple = ("fs.read",), counts: dict | None = None,
                 known: bool = True) -> Callable[[Path], tuple[str, ...]]:
    def run(root: Path) -> tuple[str, ...]:
        from .policypipeline import DEFAULT_PIPELINE
        return _policy_codes(DEFAULT_PIPELINE.decide(
            _policy_request(root, tool, args, role_name, caps, counts,
                            known)))
    return run


def _policy_stage_error(root: Path) -> tuple[str, ...]:
    """A stage that raises. The pipeline must fail closed, with a code."""
    from .policypipeline import ManifestStage, PolicyPipeline, PolicyStage

    class Broken(PolicyStage):
        name = "broken"

        def check(self, request):
            raise Injected("this stage is broken on purpose")

    pipe = PolicyPipeline((ManifestStage(), Broken()))
    return _policy_codes(pipe.decide(
        _policy_request(root, "read_file", {}, "developer", ("fs.read",))))


# -- behavioural envelopes -------------------------------------------------

def _state(path: str, exists: bool, digest: str = ""):
    from .envelopes import PathState
    return PathState(path=path, exists=exists, digest=digest,
                     size=len(digest), mtime=1.0)


def _observation(tool: str, path: str, before, after):
    from .envelopes import Observation
    obs = Observation(tool, (path,))
    obs.before = {path: before}
    obs.after = {path: after}
    return obs


def _envelope_case(kind: str) -> Callable[[Path], tuple[str, ...]]:
    def run(root: Path) -> tuple[str, ...]:
        from .envelopes import (FX_CREATE, FX_MODIFY, FX_READ, IRREVERSIBLE,
                                REPEATABLE, Envelope, V_CLASS, V_UNSEALED,
                                audit_seal, check_declarations, judge)
        target = str(root / "subject.txt")

        if kind == "tool-has-no-envelope":
            # A tool nothing declares an envelope for. Judging it must say
            # "nothing was checked", not quietly report a clean call.
            return tuple(v.kind for v in judge(None, "ghost", {}, True,
                                               None).violations)

        if kind == "undeclared-effect":
            env = Envelope("probe", frozenset({FX_READ}), REPEATABLE,
                           ("path",))
            obs = _observation("probe", target,
                               _state(target, False),
                               _state(target, True, "abc"))
            return tuple(v.kind for v in
                         judge(env, "probe", {"path": target}, True,
                               obs).violations)

        if kind == "missing-effect":
            env = Envelope("probe", frozenset({FX_MODIFY}), REPEATABLE,
                           ("path",), requires_existing=("path",))
            obs = _observation("probe", target,
                               _state(target, True, "abc"),
                               _state(target, True, "abc"))
            return tuple(v.kind for v in
                         judge(env, "probe", {"path": target}, True,
                               obs).violations)

        if kind == "precondition-unmet":
            env = Envelope("probe", frozenset({FX_MODIFY}), REPEATABLE,
                           ("path",), requires_existing=("path",))
            obs = _observation("probe", target,
                               _state(target, False),
                               _state(target, True, "abc"))
            return tuple(v.kind for v in
                         judge(env, "probe", {"path": target}, True,
                               obs).violations)

        if kind == V_CLASS:
            # An envelope whose repetition class contradicts the
            # contract's idempotency. Caught without running anything.
            from .toolcontract import build_contracts
            from .tools import build_registry
            contracts = build_contracts(build_registry())
            name = sorted(contracts)[0]
            broken = {name: Envelope(name, frozenset({FX_READ}),
                                     IRREVERSIBLE, ())}
            return tuple(v.kind for v in
                         check_declarations({name: contracts[name]}, broken))

        if kind == V_UNSEALED:
            # A call sealed with no effects recorded at all. Auditing the
            # seal must report that nothing judged it -- "not checked" is
            # a different fact from "checked and clean".
            from .kernel import EventLog
            log = EventLog(path=root / "unsealed.jsonl")
            log.append("dispatch.call",
                       {"tool": "read_file", "ok": True,
                        "args": {"path": target}}, actor="probe")
            report = audit_seal(log, require_effects=True)
            return tuple(v.kind for v in report.violations)

        raise Injected(f"no counterfactual written for {kind}")
    return run


# -- the release gate ------------------------------------------------------

def _release_log(root: Path, name: str):
    from .kernel import EventLog
    return EventLog(path=root / f"{name}.jsonl")


def _release_codes(out) -> tuple[str, ...]:
    from .releasegate import Refusal
    if isinstance(out, Refusal):
        return tuple(r.code for r in out.reasons)
    return ()


def _seal_gate(log, allowed: bool = True) -> None:
    log.append("regression.gate",
               {"allowed": allowed,
                "reasons": [] if allowed else [{"code": "surface-drift"}]},
               actor="regressiongate")


def _seal_call(log, tool: str = "read_file", **extra) -> None:
    log.append("dispatch.call",
               {"tool": tool, "ok": True, "args": {}, **extra},
               actor="dispatch")


def _release_case(kind: str) -> Callable[[Path], tuple[str, ...]]:
    def run(root: Path) -> tuple[str, ...]:
        from .releasegate import Range, build_release
        key = b"counterfactual-key"
        log = _release_log(root, kind.replace("/", "_"))
        rng = Range(kind, 0, 10_000)

        if kind == "empty-range":
            return _release_codes(build_release(log, rng, key))

        if kind == "provenance-unsigned":
            _seal_call(log, envelope_ok=True)
            _seal_gate(log)
            # No key: the record may be complete, but nothing makes it
            # evidence, and the gate must say which of the two it is.
            return _release_codes(build_release(log, rng, None))

        if kind == "regression-gate-not-run":
            _seal_call(log, envelope_ok=True)
            return _release_codes(build_release(log, rng, key))

        if kind == "regression-gate-failed":
            _seal_call(log, envelope_ok=True)
            _seal_gate(log, allowed=False)
            return _release_codes(build_release(log, rng, key))

        if kind == "call-without-verdict":
            _seal_call(log)          # envelope_ok absent: nothing judged it
            _seal_gate(log)
            return _release_codes(build_release(log, rng, key,
                                                require_verdicts=True))

        if kind == "envelope-violation":
            _seal_call(log, envelope_ok=False)
            _seal_gate(log)
            return _release_codes(build_release(log, rng, key))

        if kind == "unresolved-hold":
            log.append("consensus.audit",
                       {"outcome": "hold",
                        "opinions": [{"strategy": "guardrail",
                                      "verdict": "block",
                                      "findings": [{"why": "a rule"}]}]},
                       actor="consensus")
            _seal_call(log, envelope_ok=True)
            _seal_gate(log)
            return _release_codes(build_release(log, rng, key))

        if kind == "open-escalation":
            log.append("orchestrator.step.done",
                       {"status": "escalated", "path": "plan/step",
                        "trace_id": "t1"}, actor="orchestrator")
            _seal_call(log, envelope_ok=True)
            _seal_gate(log)
            return _release_codes(build_release(log, rng, key))

        if kind == "provenance-gaps":
            # An outcome whose step was never sealed: the graph can see
            # the effect and not its cause, which is exactly a Gap.
            log.append("orchestrator.step.done",
                       {"status": "done", "path": "plan/orphan",
                        "trace_id": "t-gap", "step": "orphan"},
                       actor="orchestrator")
            _seal_call(log, envelope_ok=True)
            _seal_gate(log)
            return _release_codes(build_release(log, rng, key))

        if kind == "provenance-tampered":
            # Attempted, and expected to fail: see UNREACHABLE. The gate
            # signs and verifies inside one call, so no input it accepts
            # can make a signature disagree with its own node.
            _seal_call(log, envelope_ok=True)
            _seal_gate(log)
            return _release_codes(build_release(log, rng, key))

        raise Injected(f"no counterfactual written for {kind}")
    return run


# -- the assurance case ----------------------------------------------------

def _assurance_case(kind: str) -> Callable[[Path], tuple[str, ...]]:
    def run(root: Path) -> tuple[str, ...]:
        from .assurance import (Case, assess, assumption, claim, evidence,
                                inference, verify_seals)
        from .kernel import EventLog

        if kind == "claim-without-support":
            case = Case().add(claim("c", "a claim with nothing under it"))
            case.roots = ("c",)
            return tuple(d.kind for d in assess(case).defects)

        if kind == "inference-without-children":
            case = Case().add(claim("c", "a claim", "i"),
                              inference("i", "an inference over nothing"))
            case.roots = ("c",)
            return tuple(d.kind for d in assess(case).defects)

        if kind == "argument-has-a-cycle":
            case = Case().add(claim("a", "a rests on b", "b"),
                              claim("b", "b rests on a", "a"))
            case.roots = ("a",)
            return tuple(d.kind for d in assess(case).defects)

        if kind == "node-names-a-missing-child":
            case = Case().add(claim("c", "names a node that is not here",
                                    "nowhere"))
            case.roots = ("c",)
            return tuple(d.kind for d in assess(case).defects)

        if kind == "assumption-without-a-reason":
            case = Case().add(claim("c", "a claim", "a"), assumption("a", ""))
            case.roots = ("c",)
            return tuple(d.kind for d in assess(case).defects)

        if kind == "node-reachable-from-no-root":
            case = Case().add(claim("c", "the root", "e"),
                              evidence("e", "fine", lambda: (True, "ok", {})),
                              claim("stray", "attached to nothing"))
            case.roots = ("c",)
            return tuple(d.kind for d in assess(case).defects)

        if kind == "exhaustion-does-not-cover-its-universe":
            case = Case().add(
                claim("c", "every colour is accounted for", "i"),
                inference("i", "one child per colour", "e",
                          kind="exhaustion",
                          universe=("red", "green", "blue")),
                evidence("e", "red is handled",
                         lambda: (True, "ok", {}), covers=("red",)))
            case.roots = ("c",)
            return tuple(d.kind for d in assess(case).defects)

        if kind == "evidence-without-seal":
            # Assessed with no log at all, then asked to prove its
            # findings were sealed. Nothing was, and it must say so.
            case = Case().add(
                claim("c", "a claim", "e"),
                evidence("e", "some evidence", lambda: (True, "ok", {})))
            case.roots = ("c",)
            assess(case)
            log = EventLog(path=root / "unsealed-case.jsonl")
            return tuple(d.kind for d in verify_seals(case, log))

        if kind == "evidence-seal-mismatch":
            # Sealed once, then the evidence changes underneath. The
            # digest must not still match: that is the whole point of
            # sealing a finding rather than a sentence about it.
            answers = iter([(True, "ok", {"n": 1}), (True, "ok", {"n": 2})])
            case = Case().add(
                claim("c", "a claim", "e"),
                evidence("e", "evidence that moves",
                         lambda: next(answers)))
            case.roots = ("c",)
            log = EventLog(path=root / "moving-case.jsonl")
            assess(case, log)        # seals n=1
            assess(case, None)       # recomputes as n=2, seals nothing
            return tuple(d.kind for d in verify_seals(case, log))

        raise Injected(f"no counterfactual written for {kind}")
    return run


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

def _taxonomy_counterfactuals() -> tuple[Counterfactual, ...]:
    """One per error code, derived from the runbook's own injectors.

    Derived rather than listed: a new error code with a runbook injector
    gets a classification counterfactual for free, and a new code with no
    injector shows up as uncovered instead of being quietly absent.
    """
    from .runbook import INJECTORS
    return tuple(
        Counterfactual(S_TAXONOMY, code,
                       f"a tool that fails in the way {code} names",
                       _taxonomy_case(code))
        for code in sorted(INJECTORS))


POLICY_COUNTERFACTUALS: tuple[Counterfactual, ...] = (
    Counterfactual(S_POLICY, "unknown-tool", "a tool nobody registered",
                   _policy_case("teleport", {}, known=False)),
    Counterfactual(S_POLICY, "declares-no-capability",
                   "a registered tool that declares nothing",
                   _policy_case("mystery", {}, caps=())),
    Counterfactual(S_POLICY, "capability-not-held",
                   "a write from a role that only reads",
                   _policy_case("write_file", {"path": IN_ROOT},
                                role_name="readonly", caps=("fs.write",))),
    Counterfactual(S_POLICY, "outside-roots",
                   "a path that resolves outside every permitted root",
                   _policy_case("read_file", {"path": "/etc/passwd"})),
    Counterfactual(S_POLICY, "path-unresolvable",
                   "a path the filesystem cannot even be asked about",
                   _policy_case("read_file", {"path": "bad\x00name"})),
    Counterfactual(S_POLICY, "destructive-command",
                   "a shell command that destroys things",
                   _policy_case("run_command", {"command": "rm -rf /tmp/x"},
                                caps=("proc.exec",))),
    Counterfactual(S_POLICY, "host-blocked",
                   "a fetch of the cloud metadata address",
                   _policy_case("web_fetch",
                                {"url": "http://169.254.169.254/"},
                                caps=("net.fetch",))),
    Counterfactual(S_POLICY, "ceiling-reached",
                   "a tool that has spent its session ceiling",
                   _policy_case("run_command", {"command": "ls"},
                                caps=("proc.exec",),
                                counts={"run_command": 10_000})),
    Counterfactual(S_POLICY, "needs-confirmation",
                   "a capability the role holds only on confirmation",
                   _policy_case("delete_path", {"path": IN_ROOT},
                                caps=("fs.delete",))),
    Counterfactual(S_POLICY, "stage-error",
                   "a policy stage that raises mid-decision",
                   _policy_stage_error),
)


def _envelope_counterfactuals() -> tuple[Counterfactual, ...]:
    from .envelopes import KINDS
    what = {k: v[0] for k, v in KINDS.items()}
    return tuple(
        Counterfactual(S_ENVELOPE, kind, what.get(kind, kind),
                       _envelope_case(kind))
        for kind in sorted(KINDS))


def _release_counterfactuals() -> tuple[Counterfactual, ...]:
    from .releasegate import REASONS
    what = {k: v[0] for k, v in REASONS.items()}
    return tuple(
        Counterfactual(S_RELEASE, code, what.get(code, code),
                       _release_case(code))
        for code in sorted(REASONS))


def _assurance_counterfactuals() -> tuple[Counterfactual, ...]:
    from .assurance import DEFECTS as A_DEFECTS
    what = {k: v[0] for k, v in A_DEFECTS.items()}
    return tuple(
        Counterfactual(S_ASSURANCE, kind, what.get(kind, kind),
                       _assurance_case(kind))
        for kind in sorted(A_DEFECTS))


def counterfactuals() -> tuple[Counterfactual, ...]:
    """Every scenario, assembled from the surfaces' own vocabularies."""
    return (_taxonomy_counterfactuals()
            + POLICY_COUNTERFACTUALS
            + _envelope_counterfactuals()
            + _release_counterfactuals()
            + _assurance_counterfactuals())


def run_one(case: Counterfactual, root: Path) -> Outcome:
    """Provoke one fault and read back which typed code came out."""
    started = time.time()
    outcome = Outcome(case.id, case.surface, case.target)
    work = root / case.surface / case.target.replace("/", "_")
    try:
        work.mkdir(parents=True, exist_ok=True)
        observed = tuple(case.run(work))
    except Exception as exc:       # noqa: BLE001
        outcome.duration = time.time() - started
        outcome.defect = Defect(
            C_HARNESS_BROKEN, case.surface, case.target,
            f"the scenario raised {type(exc).__name__}: {exc}")
        return outcome

    outcome.observed = observed
    outcome.duration = time.time() - started
    declared_unreachable = case.target in UNREACHABLE

    if case.target in observed:
        outcome.provoked = True
        if declared_unreachable:
            outcome.defect = Defect(
                C_REACHED_THE_UNREACHABLE, case.surface, case.target,
                f"declared unreachable because {UNREACHABLE[case.target]!r}, "
                f"but the scenario provoked it")
        return outcome

    if declared_unreachable:
        # The declaration held: the attempt was made and the code did not
        # come out. Not a pass and not a defect -- a checked claim.
        outcome.detail = UNREACHABLE[case.target]
        return outcome

    outcome.defect = Defect(
        C_SILENT if not observed else C_WRONG_CLASS,
        case.surface, case.target,
        f"the injected fault came back as "
        f"{', '.join(observed) if observed else 'nothing at all'}")
    return outcome


def catalogue_digest(cases: tuple[Counterfactual, ...] | None = None) -> str:
    """A digest over the surveyed universe and the scenarios covering it.

    Carried by `regressiongate.fingerprint()`, so widening a surface's
    vocabulary or dropping a counterfactual is a governed change rather
    than a quiet one.
    """
    cases = counterfactuals() if cases is None else cases
    payload = {
        "surfaces": [{"id": s.id, "universe": list(_safe_universe(s)[0])}
                     for s in SURFACES],
        "cases": sorted(c.id for c in cases),
        "unreachable": dict(sorted(UNREACHABLE.items())),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _safe_universe(surface: Surface) -> tuple[tuple[str, ...], str]:
    try:
        return tuple(surface.universe()), ""
    except Exception as exc:       # noqa: BLE001
        return (), f"{type(exc).__name__}: {exc}"


def run_all(root: Path | None = None, log=None,
            cases: tuple[Counterfactual, ...] | None = None) -> Catalogue:
    """Every counterfactual, plus the per-class coverage check."""
    import tempfile

    root = Path(root or tempfile.mkdtemp(prefix="fa-faults-"))
    cases = counterfactuals() if cases is None else cases
    outcomes = [run_one(case, root) for case in cases]
    defects = [o.defect for o in outcomes if o.defect is not None]

    by_surface: dict[str, set[str]] = {}
    for outcome in outcomes:
        if outcome.provoked:
            by_surface.setdefault(outcome.surface, set()).add(outcome.target)

    coverage: list[Coverage] = []
    for surface in SURFACES:
        declared, problem = _safe_universe(surface)
        if problem:
            defects.append(Defect(C_UNREADABLE, surface.id, "universe",
                                  problem))
        provoked = by_surface.get(surface.id, set())
        unreachable = tuple(c for c in declared
                            if c in UNREACHABLE and c not in provoked)
        uncovered = tuple(c for c in declared
                          if c not in provoked and c not in UNREACHABLE)
        for code in uncovered:
            defects.append(Defect(
                C_UNCOVERED, surface.id, code,
                "no counterfactual provokes this class, so nothing has "
                "ever confirmed the platform emits it"))
        coverage.append(Coverage(surface.id, tuple(declared),
                                 tuple(sorted(provoked)), unreachable,
                                 uncovered))

    known = {s.id: set(_safe_universe(s)[0]) for s in SURFACES}
    for case in cases:
        if case.target not in known.get(case.surface, set()):
            defects.append(Defect(
                C_PHANTOM, case.surface, case.target,
                "this counterfactual targets a class the surface no "
                "longer declares"))

    report = Catalogue(tuple(outcomes), tuple(coverage), tuple(defects),
                       catalogue_digest(cases), time.time())
    if log is not None:
        try:
            log.append("faultcatalogue.run", report.to_dict(),
                       actor="faultcatalogue")
        except Exception:
            pass
    return report


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import tempfile

    argv = sys.argv[1:]

    if "--check" in argv or "--json" in argv:
        report = run_all()
        print(json.dumps(report.to_dict(), indent=2) if "--json" in argv
              else report.format())
        raise SystemExit(0 if report.ok else 1)

    if "--digest" in argv:
        print(catalogue_digest())
        raise SystemExit(0)

    root = Path(tempfile.mkdtemp(prefix="fa-faults-self-"))
    report = run_all(root / "shipped")
    assert report.ok, report.format()
    assert report.classes == 44, report.classes
    assert report.provoked == 43, report.provoked

    # Every surveyed surface is fully accounted for: provoked, or declared
    # unreachable with a written reason. Nothing is merely absent.
    for cover in report.coverage:
        assert not cover.uncovered, cover
        assert cover.ratio == 1.0, cover
        for code in cover.unreachable:
            assert UNREACHABLE.get(code, "").strip(), \
                f"{code} is declared unreachable with no reason"

    # The declaration is a claim that was tested, not an exemption: the
    # scenario for it ran and observed something, it simply was not the
    # code in question.
    skipped = [o for o in report.outcomes if not o.provoked]
    assert len(skipped) == 1 and skipped[0].target == "provenance-tampered"
    assert skipped[0].defect is None and skipped[0].detail

    # -- NEGATIVE CONTROLS ----------------------------------------------
    # Each defect kind is provoked in the catalogue itself. A coverage
    # checker that cannot report a gap is a green light with no bulb.

    def _surface(ident, codes):
        return Surface(ident, "a surface for the self-test",
                       lambda: tuple(codes))

    saved = SURFACES
    try:
        # A class nobody wrote a scenario for.
        globals()["SURFACES"] = (_surface("probe", ("never-covered",)),)
        gap = run_all(root / "gap", cases=())
        assert not gap.ok
        assert gap.defects[0].kind == C_UNCOVERED, gap.defects
        assert gap.coverage[0].uncovered == ("never-covered",)
        assert gap.coverage[0].ratio == 0.0

        # A scenario aimed at a class the surface no longer declares.
        stale = run_all(root / "stale", cases=(Counterfactual(
            "probe", "class-that-left", "gone",
            lambda w: ("class-that-left",)),))
        assert any(d.kind == C_PHANTOM for d in stale.defects), stale.defects

        # A scenario that cannot set itself up. Reported apart from a
        # class that failed to fire, because a harness that cannot run is
        # not a class that passed.
        broken = run_all(root / "broken", cases=(Counterfactual(
            "probe", "never-covered", "raises",
            lambda w: (_ for _ in ()).throw(Injected("no setup"))),))
        assert any(d.kind == C_HARNESS_BROKEN for d in broken.defects)

        # A fault the surface did not notice at all — the failure mode the
        # whole catalogue exists for.
        silent = run_all(root / "silent", cases=(Counterfactual(
            "probe", "never-covered", "provokes nothing", lambda w: ()),))
        assert any(d.kind == C_SILENT for d in silent.defects)

        # A fault that was caught, but filed under the wrong class.
        globals()["SURFACES"] = (_surface("probe", ("wanted", "other")),)
        wrong = run_all(root / "wrong", cases=(
            Counterfactual("probe", "wanted", "misfires",
                           lambda w: ("other",)),
            Counterfactual("probe", "other", "fires",
                           lambda w: ("other",))))
        assert any(d.kind == C_WRONG_CLASS for d in wrong.defects)

        # A surface whose universe cannot be read has unknown coverage,
        # not complete coverage.
        def _explodes():
            raise Injected("cannot read the universe")

        globals()["SURFACES"] = (Surface("probe", "broken", _explodes),)
        unreadable = run_all(root / "unreadable", cases=())
        assert any(d.kind == C_UNREADABLE for d in unreadable.defects)

        # An unreachability claim that turns out to be false.
        globals()["SURFACES"] = (_surface("probe", ("claimed-dead",)),)
        UNREACHABLE["claimed-dead"] = "nothing can reach this"
        alive = run_all(root / "alive", cases=(Counterfactual(
            "probe", "claimed-dead", "reaches it anyway",
            lambda w: ("claimed-dead",)),))
        assert any(d.kind == C_REACHED_THE_UNREACHABLE
                   for d in alive.defects), alive.defects
    finally:
        globals()["SURFACES"] = saved
        UNREACHABLE.pop("claimed-dead", None)

    # -- the digest tracks the surveyed universe -------------------------
    first = catalogue_digest()
    assert first == catalogue_digest()
    assert catalogue_digest(counterfactuals()[:-1]) != first, \
        "dropping a counterfactual must change the digest"

    # -- the report is machine-readable ----------------------------------
    payload = report.to_dict()
    assert payload["ok"] is True and payload["classes"] == 44
    assert json.loads(json.dumps(payload)) == payload

    print(report.format())
    print(f"FAULTCATALOGUE SELF-TEST PASS — {report.provoked} of "
          f"{report.classes} classes provoked, {len(UNREACHABLE)} "
          f"declared unreachable, digest {report.digest}")

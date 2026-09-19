"""POLICY METAMODEL — proving things *about* the pipeline, not inside it.

`policypipeline.py` makes a decision. Its own self-test checks that a
handful of hand-written calls come out the way they should. That is a
test of the stages. It is not a test of the *combinator* — the small
amount of code in `PolicyPipeline.decide` that decides what a list of
stage answers adds up to — and the combinator is where the interesting
mistakes live, because it is the only part whose behaviour depends on
every stage at once.

The three faults that shape this module are all combinator faults:

  * **A deny that an ask hid.** The pre-pipeline `evaluate()` returned at
    the first objection, so a call that a later stage would have refused
    could be handed to a human as an approval prompt. Approving it was
    then the way to make the machine stop complaining.
  * **A widen by addition.** Adding a stage is supposed to be incapable
    of permitting something the shorter pipeline refused. Nothing in the
    code says so, and nothing failed when it was not so.
  * **A reorder that changed the answer.** Stage order is a policy
    statement ("cheap and categorical first"), not a correctness
    requirement — but only if the final outcome is genuinely invariant
    under permutation. If it is not, the ordering comment is load-bearing
    and nobody knows it.

So the pipeline is modelled here as data: the stages it has, the outcomes
each may emit, the reason codes each may cite, and the ordering laws the
shipped order is supposed to satisfy. Then the meta-properties are proved
by enumeration against the *real* `PolicyPipeline`, driven by scripted
stages that emit a chosen outcome — every combination of answers seven
stages could give, not the handful a person thinks to write down.

Two rules this module is built on, and the reason for each:

1. **The specification is written separately from the implementation.**
   `spec_outcome()` says what a vector of stage answers means, as a rule
   ("a deny anywhere, else an ask anywhere, else allow"). `decide()`
   computes it by walking and short-circuiting. Proving they agree over
   every vector is the whole content; writing the spec as a walk would
   prove only that the code equals itself.

2. **The model must be falsifiable against the real stages.** A model
   that merely restates the code drifts silently. `P_MODEL_FAITHFUL`
   runs every real stage over a corpus of real requests and fails if a
   stage emits an outcome or a code its model does not declare, and
   `P_MODELLED` fails if the shipped pipeline grows a stage with no model
   at all. That is what makes "every pipeline change ships with its
   meta-proof" enforceable rather than aspirational.

A note on what "proved" means here, because it is not the same word as
"tested". A property is `PROVED` when the enumeration was exhaustive over
a declared finite universe — every outcome vector, every permutation,
every subset — so there is no untried case of that shape. It is `CHECKED`
when it was sampled over a corpus of hand-built requests, which is a test
and says nothing about the cases not in the corpus. The report counts the
two separately and the CLI prints both, because collapsing them is how a
sampled check ends up quoted as a guarantee.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .policypipeline import (DEFAULT_STAGES, R_BLOCKED_HOST, R_CEILING,
                             R_DESTRUCTIVE, R_NEEDS_CONFIRMATION,
                             R_NOT_APPLICABLE, R_NO_CAPABILITY,
                             R_NO_MANIFEST, R_OUTSIDE_ROOTS, R_SATISFIED,
                             R_UNKNOWN_TOOL, R_UNRESOLVABLE, SKIP,
                             STAGE_APPROVAL, STAGE_CAPABILITY, STAGE_COMMAND,
                             STAGE_MANIFEST, STAGE_NETWORK, STAGE_PATH,
                             STAGE_RATE, PolicyPipeline, PolicyStage,
                             Rationale, Request)
from .toolpolicy import ALLOW, ASK, DENY

# -- The outcome alphabet, and the order that makes "widen" a word with a
# -- meaning. A SKIP is *less* restrictive than an ALLOW on purpose: a
# -- stage that skipped did not approve anything, it simply had nothing to
# -- say, and a pipeline of nothing but skips must never be reported as a
# -- pipeline that checked something.
OUTCOMES: tuple[str, ...] = (SKIP, ALLOW, ASK, DENY)
RESTRICTIVENESS: dict[str, int] = {SKIP: 0, ALLOW: 1, ASK: 2, DENY: 3}

#: The code a scripted stage cites. Not a real reason; it exists so the
#: enumeration never has to invent one that a model might declare.
R_SCRIPTED = "scripted"

# -- Verification method. See the module docstring: these are two
# -- different claims and are never added together.
PROVED = "proved"      # exhaustive over a declared finite universe
CHECKED = "checked"    # sampled over a corpus

# -- Typed failures. A meta-property that fails names *which* law broke,
# -- not just that something did.
F_SPEC_MISMATCH = "outcome-disagrees-with-spec"
F_ASK_HID_DENY = "ask-returned-before-a-deny"
F_WIDENED = "longer-pipeline-permitted-more"
F_REORDERED = "outcome-changed-under-permutation"
F_CRASH_PERMITTED = "crashing-stage-did-not-deny"
F_RATIONALE_SHAPE = "rationale-is-not-the-decided-prefix"
F_DECIDER_WRONG = "deciding-stage-does-not-match-outcome"
F_SKIP_DECIDED = "a-stage-that-skipped-was-named-as-the-reason"
F_UNTYPED_DENY = "deny-without-a-declared-code"
F_UNMODELLED = "pipeline-stage-with-no-model"
F_PHANTOM = "model-for-a-stage-that-is-not-in-the-pipeline"
F_UNFAITHFUL = "stage-emitted-something-its-model-forbids"
F_STATEFUL = "stage-carries-state-between-calls"
F_NONDETERMINISTIC = "same-request-decided-twice-differently"
F_LAW_BROKEN = "shipped-order-violates-a-declared-law"

FAILURES: dict[str, str] = {
    F_SPEC_MISMATCH: "the pipeline's answer differs from what the ordering "
                     "rule says that combination of stage answers means",
    F_ASK_HID_DENY: "a human would have been asked to approve a call a "
                    "later stage refuses",
    F_WIDENED: "adding a stage permitted something the shorter pipeline "
               "did not",
    F_REORDERED: "the outcome depends on stage order, so the ordering "
                 "comment is load-bearing",
    F_CRASH_PERMITTED: "a stage that raised did not fail closed",
    F_RATIONALE_SHAPE: "the audit trail is not the stages that actually ran",
    F_DECIDER_WRONG: "the stage reported as the reason is not the stage "
                     "that produced the outcome",
    F_SKIP_DECIDED: "a stage with no opinion was reported as the reason",
    F_UNTYPED_DENY: "a refusal cited a code outside the declared set",
    F_UNMODELLED: "the pipeline has a stage this metamodel does not model",
    F_PHANTOM: "this metamodel models a stage the pipeline does not have",
    F_UNFAITHFUL: "a real stage emitted an outcome or code its model "
                  "does not declare",
    F_STATEFUL: "a stage holds instance state, so reorder-safety and "
                "stage-independence cannot be claimed",
    F_NONDETERMINISTIC: "the same request produced two different decisions",
    F_LAW_BROKEN: "the shipped stage order breaks a constraint it declares",
}


# ---------------------------------------------------------------------------
# The metamodel: the pipeline as data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StageModel:
    """What one stage is allowed to do.

    `emits` and `codes` are the falsifiable part: a stage that returns
    anything outside them fails `P_MODEL_FAITHFUL`. `reads` is
    documentation of the `Request` fields the stage consults, and is what
    makes "stages share no state" a statement with content — a stage that
    needed anything outside a `Request` could not be listed here.
    """
    name: str
    question: str
    emits: frozenset[str]
    codes: frozenset[str]
    reads: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"name": self.name, "question": self.question,
                "emits": sorted(self.emits), "codes": sorted(self.codes),
                "reads": list(self.reads)}


def _model(name, question, emits, codes, reads) -> StageModel:
    return StageModel(name, question, frozenset(emits), frozenset(codes),
                      tuple(reads))


STAGE_MODELS: dict[str, StageModel] = {
    m.name: m for m in (
        _model(STAGE_MANIFEST, "have we heard of this tool, and does it "
                               "declare anything?",
               (ALLOW, DENY), (R_SATISFIED, R_UNKNOWN_TOOL, R_NO_MANIFEST),
               ("tool", "known", "capabilities")),
        _model(STAGE_CAPABILITY, "does the role hold everything the tool "
                                 "declares?",
               (ALLOW, DENY), (R_SATISFIED, R_NO_CAPABILITY),
               ("capabilities", "role")),
        _model(STAGE_PATH, "does every path argument resolve inside a "
                           "permitted root?",
               (ALLOW, DENY, SKIP),
               (R_SATISFIED, R_NOT_APPLICABLE, R_OUTSIDE_ROOTS,
                R_UNRESOLVABLE),
               ("tool", "args", "roots")),
        _model(STAGE_COMMAND, "is this shell command one of the few that "
                              "destroy things?",
               (ALLOW, ASK, DENY, SKIP),
               (R_SATISFIED, R_NOT_APPLICABLE, R_DESTRUCTIVE),
               ("tool", "args", "role")),
        _model(STAGE_NETWORK, "is this URL one we are willing to reach?",
               (ALLOW, DENY, SKIP),
               (R_SATISFIED, R_NOT_APPLICABLE, R_BLOCKED_HOST),
               ("capabilities", "args", "role")),
        _model(STAGE_RATE, "has this tool used up its share of the session?",
               (ALLOW, DENY, SKIP),
               (R_SATISFIED, R_NOT_APPLICABLE, R_CEILING),
               ("tool", "role", "counts")),
        _model(STAGE_APPROVAL, "does the role hold this only on condition "
                               "of asking?",
               (ASK, SKIP), (R_NOT_APPLICABLE, R_NEEDS_CONFIRMATION),
               ("capabilities", "role")),
    )
}


@dataclass(frozen=True)
class Law:
    """An ordering constraint the shipped stage order must satisfy.

    `later` may be the sentinel `LAST`, meaning "nothing may come after
    `earlier`". These are the reasons the order is what it is; without
    them the order is a comment, and a comment does not fail a build.
    """
    earlier: str
    later: str
    why: str

    def to_dict(self) -> dict:
        return {"earlier": self.earlier, "later": self.later, "why": self.why}


LAST = "<end>"

LAWS: tuple[Law, ...] = (
    Law(STAGE_MANIFEST, STAGE_CAPABILITY,
        "an unknown tool must be refused as unknown, not as unauthorised — "
        "the audit has to tell those apart"),
    Law(STAGE_MANIFEST, STAGE_PATH,
        "never resolve a filesystem path on behalf of a tool we have never "
        "heard of"),
    Law(STAGE_MANIFEST, STAGE_RATE,
        "an unknown tool must not be measured against a ceiling; that "
        "would report a typo as a quota problem"),
    Law(STAGE_CAPABILITY, STAGE_COMMAND,
        "do not inspect a command for a role that cannot execute commands "
        "at all"),
    Law(STAGE_CAPABILITY, STAGE_APPROVAL,
        "never ask a human to approve something the role does not hold"),
    Law(STAGE_APPROVAL, LAST,
        "a human is only ever asked about a call every other stage has "
        "already accepted"),
)


# ---------------------------------------------------------------------------
# The specification, written as a rule rather than as a walk
# ---------------------------------------------------------------------------

def spec_outcome(vector: Iterable[str]) -> str:
    """What the ordering rule says a vector of stage answers means.

    This is the whole policy in one sentence: a deny anywhere beats an ask
    anywhere, an ask beats an allow, and a pipeline nobody objected to
    allows. It is deliberately order-free and deliberately not a loop with
    a `break` in it — `PolicyPipeline.decide` is the loop with the break,
    and the point of the enumeration below is that the two agree.
    """
    seen = set(vector)
    if DENY in seen:
        return DENY
    if ASK in seen:
        return ASK
    return ALLOW


def spec_trail_length(vector: Iterable[str]) -> int:
    """How many stages should appear in the audit trail.

    A deny is final, so the trail stops there; anything else runs every
    stage, because "why was this permitted" is as much an audit question
    as "why was this refused".
    """
    seq = tuple(vector)
    for index, outcome in enumerate(seq):
        if outcome == DENY:
            return index + 1
    return len(seq)


# ---------------------------------------------------------------------------
# Scripted stages — the instrument the enumeration drives the real
# combinator with
# ---------------------------------------------------------------------------

class ScriptedStage(PolicyStage):
    """A stage that emits a chosen outcome and looks at nothing.

    Real stages cannot be driven into every combination of answers: you
    cannot build one request that makes the network stage deny and the
    path stage ask, because the path stage never asks. Scripting the
    answers separates the two questions — "does each stage answer
    correctly" (the pipeline's own self-test) from "does the combinator
    add answers up correctly" (this module).
    """

    def __init__(self, name: str, outcome: str):
        self.name = name
        self.outcome = outcome

    def check(self, request: Request) -> Rationale:
        if self.outcome == SKIP:
            return Rationale(self.name, SKIP, R_NOT_APPLICABLE,
                             "scripted skip")
        return Rationale(self.name, self.outcome, R_SCRIPTED,
                         f"scripted {self.outcome}")


class CrashingStage(PolicyStage):
    """A stage that raises. Fails closed, or this module says so."""

    def __init__(self, name: str):
        self.name = name

    def check(self, request: Request) -> Rationale:
        raise RuntimeError(f"{self.name} is broken")


def _scripted_pipeline(vector: Iterable[str],
                       crash_at: int | None = None) -> PolicyPipeline:
    stages: list[PolicyStage] = []
    for index, outcome in enumerate(vector):
        name = f"s{index}"
        stages.append(CrashingStage(name) if index == crash_at
                      else ScriptedStage(name, outcome))
    return PolicyPipeline(tuple(stages))


def _probe_request() -> Request:
    """A request the scripted stages ignore entirely."""
    from .toolpolicy import ROLES
    return Request("read_file", {}, ROLES["developer"],
                   frozenset({"fs.read"}), ("/",), {}, True)


# ---------------------------------------------------------------------------
# Report types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Failure:
    """One counterexample, named by the law it breaks."""
    id: str
    property: str
    detail: str
    facts: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"id": self.id, "property": self.property,
                "detail": self.detail, "facts": self.facts}

    def format(self) -> str:
        return f"  FAIL {self.property}: {FAILURES.get(self.id, self.id)}\n" \
               f"       {self.detail}"


@dataclass
class Property:
    """One meta-property, its verdict, and how hard the verdict is."""
    id: str
    statement: str
    method: str = PROVED
    universe: str = ""
    cases: int = 0
    failures: tuple[Failure, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict:
        return {"id": self.id, "statement": self.statement,
                "method": self.method, "universe": self.universe,
                "cases": self.cases,
                "failures": [f.to_dict() for f in self.failures]}


@dataclass
class MetaReport:
    """Everything this module can say about the shipped pipeline."""
    properties: tuple[Property, ...] = ()
    digest: str = ""

    @property
    def ok(self) -> bool:
        return all(p.ok for p in self.properties)

    @property
    def failures(self) -> tuple[Failure, ...]:
        return tuple(f for p in self.properties for f in p.failures)

    @property
    def cases(self) -> int:
        return sum(p.cases for p in self.properties)

    @property
    def proved(self) -> int:
        """Properties settled by exhaustive enumeration, and holding.

        Deliberately not "properties that passed": a sampled check that
        passed is not a proof, and adding the two together is how a
        corpus of nine requests gets quoted as a guarantee.
        """
        return sum(1 for p in self.properties
                   if p.ok and p.method == PROVED)

    @property
    def checked(self) -> int:
        return sum(1 for p in self.properties
                   if p.ok and p.method == CHECKED)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "digest": self.digest,
                "proved": self.proved, "checked": self.checked,
                "cases": self.cases,
                "properties": [p.to_dict() for p in self.properties]}

    def format(self) -> str:
        head = "POLICY METAMODEL — " + ("ok" if self.ok else "FAILED")
        lines = [head, f"  digest {self.digest}",
                 f"  {self.proved} proved exhaustively, "
                 f"{self.checked} checked by corpus, "
                 f"{self.cases} case(s)"]
        for prop in self.properties:
            mark = "ok  " if prop.ok else "FAIL"
            lines.append(f"  {mark} {prop.id:<22} {prop.method:<7} "
                         f"{prop.cases:>7} {prop.universe}")
            for failure in prop.failures:
                lines.append(failure.format())
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# The properties
# ---------------------------------------------------------------------------

P_DENY_DOMINANCE = "deny-dominance"
P_ASK_NOT_FINAL = "ask-is-not-final"
P_NO_SILENT_WIDEN = "no-silent-widen"
P_REORDER_SAFE = "stage-reorder-safety"
P_CRASH_DENIES = "crash-fails-closed"
P_TRAIL_SHAPE = "audit-trail-is-the-prefix"
P_DECIDER_SOUND = "deciding-stage-is-sound"
P_MODELLED = "every-stage-is-modelled"
P_MODEL_FAITHFUL = "model-matches-the-stages"
P_STATELESS = "stages-carry-no-state"
P_DETERMINISTIC = "decisions-are-deterministic"
P_TYPED_DENIALS = "denials-are-typed"
P_LAWS_HOLD = "ordering-laws-hold"

#: How wide the exhaustive enumerations go. Seven is the shipped arity, so
#: `4**7 = 16384` vectors is every answer the real pipeline could ever
#: receive. The permutation and powerset properties multiply by `n!` and
#: `2**n`, so they run at a smaller arity where the universe is still
#: finite and still enumerated whole — the claim is then about pipelines
#: of that arity, and the report says so in `universe` rather than
#: implying it covers seven.
SHIPPED_ARITY = len(DEFAULT_STAGES)
PERM_ARITY = 5
SUBSET_ARITY = 5


def _vectors(arity: int) -> Iterable[tuple[str, ...]]:
    return itertools.product(OUTCOMES, repeat=arity)


def _prove_deny_dominance(factory: Callable = _scripted_pipeline
                          ) -> tuple[Property, Property, Property, Property]:
    """One walk of the whole universe, four properties read off it.

    Deny-dominance, ask-is-not-final, trail shape and decider soundness
    are all statements about the same object — the decision a vector of
    answers produces — so they are enumerated together rather than
    walking `4**7` vectors four times.
    """
    request = _probe_request()
    dominance = Property(
        P_DENY_DOMINANCE,
        "if any stage denies, the decision is deny, whatever the other "
        "stages said and wherever the deny sits",
        PROVED, f"all {len(OUTCOMES)}**{SHIPPED_ARITY} outcome vectors")
    ask_final = Property(
        P_ASK_NOT_FINAL,
        "an ask never ends the pipeline: a deny after an ask still wins, "
        "so nobody is asked to approve a call that would be refused",
        PROVED, f"vectors with an ask before a deny")
    trail = Property(
        P_TRAIL_SHAPE,
        "the audit trail is exactly the stages that ran: every stage when "
        "nothing denied, and the prefix up to the first deny when one did",
        PROVED, f"all {len(OUTCOMES)}**{SHIPPED_ARITY} outcome vectors")
    decider = Property(
        P_DECIDER_SOUND,
        "the stage reported as the reason produced the outcome, and a "
        "stage that skipped is never reported as the reason",
        PROVED, f"all {len(OUTCOMES)}**{SHIPPED_ARITY} outcome vectors")

    dom_bad: list[Failure] = []
    ask_bad: list[Failure] = []
    trail_bad: list[Failure] = []
    dec_bad: list[Failure] = []

    for vector in _vectors(SHIPPED_ARITY):
        decision = factory(vector).decide(request)
        expected = spec_outcome(vector)
        dominance.cases += 1
        if decision.outcome != expected:
            dom_bad.append(Failure(
                F_SPEC_MISMATCH, P_DENY_DOMINANCE,
                f"{list(vector)} decided {decision.outcome}, "
                f"the rule says {expected}", {"vector": list(vector)}))

        if ASK in vector and DENY in vector \
                and vector.index(ASK) < vector.index(DENY):
            ask_final.cases += 1
            if decision.outcome != DENY:
                ask_bad.append(Failure(
                    F_ASK_HID_DENY, P_ASK_NOT_FINAL,
                    f"{list(vector)} decided {decision.outcome}",
                    {"vector": list(vector)}))

        trail.cases += 1
        if len(decision.rationale) != spec_trail_length(vector):
            trail_bad.append(Failure(
                F_RATIONALE_SHAPE, P_TRAIL_SHAPE,
                f"{list(vector)} recorded {len(decision.rationale)} "
                f"stage(s), expected {spec_trail_length(vector)}",
                {"vector": list(vector)}))

        decider.cases += 1
        deciding = decision.deciding()
        if deciding is None:
            dec_bad.append(Failure(F_DECIDER_WRONG, P_DECIDER_SOUND,
                                   f"{list(vector)} named no stage at all",
                                   {"vector": list(vector)}))
        elif decision.outcome in (DENY, ASK):
            if deciding.outcome != decision.outcome:
                dec_bad.append(Failure(
                    F_DECIDER_WRONG, P_DECIDER_SOUND,
                    f"{list(vector)} decided {decision.outcome} but named "
                    f"a stage that answered {deciding.outcome}",
                    {"vector": list(vector)}))
        elif ALLOW in vector and deciding.outcome != ALLOW:
            # An allow that names a stage which merely skipped is a lie of
            # omission: it reports "permitted because X was satisfied" when
            # X had no opinion.
            dec_bad.append(Failure(
                F_SKIP_DECIDED, P_DECIDER_SOUND,
                f"{list(vector)} allowed but named a stage that answered "
                f"{deciding.outcome}", {"vector": list(vector)}))

    dominance.failures = tuple(dom_bad)
    ask_final.failures = tuple(ask_bad)
    trail.failures = tuple(trail_bad)
    decider.failures = tuple(dec_bad)
    return dominance, ask_final, trail, decider


def _prove_no_silent_widen(factory: Callable = _scripted_pipeline) -> Property:
    """Removing stages never tightens; adding them never widens.

    Enumerated over every vector *and every subset of it*, so the claim
    covers pipelines built by deleting any combination of stages, not
    just by deleting one.
    """
    request = _probe_request()
    prop = Property(
        P_NO_SILENT_WIDEN,
        "a longer pipeline is never more permissive than any pipeline "
        "made by dropping stages from it, so adding a stage cannot widen",
        PROVED,
        f"all {len(OUTCOMES)}**{SUBSET_ARITY} vectors x all "
        f"2**{SUBSET_ARITY} subsets")
    bad: list[Failure] = []
    positions = range(SUBSET_ARITY)
    subsets = [frozenset(c) for size in range(SUBSET_ARITY + 1)
               for c in itertools.combinations(positions, size)]
    for vector in _vectors(SUBSET_ARITY):
        full = RESTRICTIVENESS[factory(vector).decide(request).outcome]
        for keep in subsets:
            reduced = tuple(o for i, o in enumerate(vector) if i in keep)
            prop.cases += 1
            if not reduced:
                # An empty pipeline has checked nothing. It must not be
                # more restrictive than the real one — that direction is
                # the widen — but it is allowed to be less.
                continue
            got = RESTRICTIVENESS[factory(reduced).decide(request).outcome]
            if got > full:
                bad.append(Failure(
                    F_WIDENED, P_NO_SILENT_WIDEN,
                    f"dropping {sorted(set(positions) - keep)} from "
                    f"{list(vector)} tightened the answer, so adding those "
                    f"stages back widens it",
                    {"vector": list(vector), "kept": sorted(keep)}))
    prop.failures = tuple(bad)
    return prop


def _prove_reorder_safety(factory: Callable = _scripted_pipeline) -> Property:
    """The outcome does not depend on stage order.

    The *trail* does, and is meant to: a deny stops the walk, so reordering
    changes which stages are on the record. That is why this property is
    about `outcome` alone, and why the ordering laws below are a separate
    property — order is a policy statement about who gets asked what, not a
    correctness requirement about the answer.
    """
    request = _probe_request()
    prop = Property(
        P_REORDER_SAFE,
        "permuting the stages never changes the outcome, so stage order "
        "is a policy choice and not a correctness requirement",
        PROVED,
        f"all {len(OUTCOMES)}**{PERM_ARITY} vectors x all {PERM_ARITY}! "
        f"permutations")
    bad: list[Failure] = []
    perms = list(itertools.permutations(range(PERM_ARITY)))
    for vector in _vectors(PERM_ARITY):
        base = factory(vector).decide(request).outcome
        for perm in perms:
            shuffled = tuple(vector[i] for i in perm)
            prop.cases += 1
            got = factory(shuffled).decide(request).outcome
            if got != base:
                bad.append(Failure(
                    F_REORDERED, P_REORDER_SAFE,
                    f"{list(vector)} decided {base}, but reordered to "
                    f"{list(shuffled)} it decided {got}",
                    {"vector": list(vector), "permutation": list(perm)}))
    prop.failures = tuple(bad)
    return prop


def _prove_crash_denies(factory: Callable = _scripted_pipeline) -> Property:
    """A stage that raises denies, from any position, whatever else said."""
    request = _probe_request()
    arity = SHIPPED_ARITY
    prop = Property(
        P_CRASH_DENIES,
        "a stage that raises denies the call from any position, whatever "
        "the other stages answered: not knowing is not permission",
        PROVED,
        f"{arity} positions x all {len(OUTCOMES)}**{arity - 1} vectors "
        f"for the rest")
    bad: list[Failure] = []
    for position in range(arity):
        for rest in _vectors(arity - 1):
            vector = rest[:position] + (ALLOW,) + rest[position:]
            prop.cases += 1
            decision = factory(vector, crash_at=position).decide(request)
            if not decision.denied:
                bad.append(Failure(
                    F_CRASH_PERMITTED, P_CRASH_DENIES,
                    f"a crash at position {position} among {list(vector)} "
                    f"decided {decision.outcome}",
                    {"position": position, "vector": list(vector)}))
    prop.failures = tuple(bad)
    return prop


# ---------------------------------------------------------------------------
# Properties about the real stages
# ---------------------------------------------------------------------------

def _prove_modelled(pipeline: PolicyPipeline,
                    models: dict[str, StageModel]) -> Property:
    """Every shipped stage has a model, and every model a shipped stage.

    This is the forcing function behind "every pipeline change ships with
    its meta-proof". Adding a stage to `DEFAULT_STAGES` and nothing else
    fails here, by name, rather than quietly being excluded from every
    property below it.
    """
    prop = Property(
        P_MODELLED,
        "the metamodel and the shipped pipeline name exactly the same "
        "stages, so no stage escapes verification by being new",
        PROVED, "every shipped stage and every modelled stage")
    bad: list[Failure] = []
    shipped = list(pipeline.names())
    prop.cases = len(shipped) + len(models)
    for name in shipped:
        if name not in models:
            bad.append(Failure(
                F_UNMODELLED, P_MODELLED,
                f"stage '{name}' is in the pipeline with no model; add one "
                f"to STAGE_MODELS", {"stage": name}))
    for name in models:
        if name not in shipped:
            bad.append(Failure(
                F_PHANTOM, P_MODELLED,
                f"stage '{name}' is modelled but not in the pipeline",
                {"stage": name}))
    prop.failures = tuple(bad)
    return prop


def _prove_stateless(pipeline: PolicyPipeline) -> Property:
    """No stage carries instance state.

    Reorder-safety and stage-independence are claims about stages that
    cannot remember. A stage with an attribute could accumulate one, and
    then both claims are about a system that no longer exists.
    """
    prop = Property(
        P_STATELESS,
        "no stage holds instance state, so no stage can remember an "
        "earlier call or be influenced by the stage before it",
        PROVED, "every shipped stage")
    bad: list[Failure] = []
    for stage in pipeline.stages:
        prop.cases += 1
        held = sorted(vars(stage))
        if held:
            bad.append(Failure(
                F_STATEFUL, P_STATELESS,
                f"stage '{stage.name}' holds {', '.join(held)}",
                {"stage": stage.name, "attributes": held}))
    prop.failures = tuple(bad)
    return prop


def _prove_laws(pipeline: PolicyPipeline,
                laws: tuple[Law, ...] = LAWS) -> Property:
    """The shipped order satisfies every constraint it declares."""
    prop = Property(
        P_LAWS_HOLD,
        "the shipped stage order satisfies every ordering constraint the "
        "module declares, each with the reason it exists",
        PROVED, f"all {len(laws)} declared laws")
    bad: list[Failure] = []
    order = list(pipeline.names())
    index = {name: i for i, name in enumerate(order)}
    for law in laws:
        prop.cases += 1
        if law.earlier not in index:
            bad.append(Failure(
                F_LAW_BROKEN, P_LAWS_HOLD,
                f"law names '{law.earlier}', which is not in the pipeline",
                law.to_dict()))
            continue
        if law.later == LAST:
            if index[law.earlier] != len(order) - 1:
                bad.append(Failure(
                    F_LAW_BROKEN, P_LAWS_HOLD,
                    f"'{law.earlier}' must be last, but "
                    f"{order[index[law.earlier] + 1]} runs after it — "
                    f"{law.why}", law.to_dict()))
            continue
        if law.later not in index:
            bad.append(Failure(
                F_LAW_BROKEN, P_LAWS_HOLD,
                f"law names '{law.later}', which is not in the pipeline",
                law.to_dict()))
            continue
        if index[law.earlier] >= index[law.later]:
            bad.append(Failure(
                F_LAW_BROKEN, P_LAWS_HOLD,
                f"'{law.earlier}' must run before '{law.later}' — {law.why}",
                law.to_dict()))
    prop.failures = tuple(bad)
    return prop


def corpus(root: str) -> tuple[Request, ...]:
    """Real requests, for the properties that need real stages.

    Hand-built, therefore a sample: everything verified against this is
    reported as `checked`, never as `proved`. It is here to falsify the
    model — a stage that emits something `STAGE_MODELS` forbids — not to
    stand in for the enumerations above.
    """
    import os

    from .toolpolicy import (FS_DELETE, FS_READ, FS_WRITE, NET_FETCH,
                             PROC_EXEC, ROLES)
    dev = ROLES["developer"]
    readonly = ROLES["readonly"]
    operator = ROLES["operator"]
    inside = os.path.join(root, "a.txt")

    def req(tool, args=None, role=dev, caps=(), counts=None, known=True):
        return Request(tool, dict(args or {}), role, frozenset(caps),
                       (root,), dict(counts or {}), known)

    return (
        req("teleport", known=False),
        req("mystery", caps=()),
        req("read_file", {"path": inside}, caps=[FS_READ]),
        req("read_file", {"path": "/etc/passwd"}, caps=[FS_READ]),
        req("read_file", {"path": inside}, role=readonly, caps=[FS_READ]),
        req("write_file", {"path": inside}, role=readonly, caps=[FS_WRITE]),
        req("delete_path", {"path": inside}, caps=[FS_DELETE]),
        req("run_command", {"command": "ls -la"}, caps=[PROC_EXEC]),
        req("run_command", {"command": "rm -rf /tmp/x"}, caps=[PROC_EXEC]),
        req("run_command", {"command": "rm -rf /tmp/x"}, role=operator,
            caps=[PROC_EXEC]),
        req("run_command", {"command": "ls"}, caps=[PROC_EXEC],
            counts={"run_command": 10_000}),
        req("web_fetch", {"url": "https://example.test/x"},
            caps=[NET_FETCH]),
        req("web_fetch", {"url": "http://169.254.169.254/"},
            caps=[NET_FETCH]),
        req("web_fetch", {}, caps=[NET_FETCH]),
        req("think", {"thought": "hm"}, caps=[FS_READ]),
    )


def _check_faithful(pipeline: PolicyPipeline,
                    models: dict[str, StageModel],
                    requests: tuple[Request, ...]) -> tuple[Property,
                                                            Property,
                                                            Property]:
    """Run the real stages and hold them to their models.

    Also collects the two corpus-only properties — typed denials and
    determinism — from the same walk.
    """
    faithful = Property(
        P_MODEL_FAITHFUL,
        "every real stage emits only the outcomes and reason codes its "
        "model declares, so the model cannot drift away from the code",
        CHECKED, f"{len(requests)} request(s) x {len(pipeline.stages)} "
                 f"stage(s)")
    typed = Property(
        P_TYPED_DENIALS,
        "every refusal cites a declared code and a sentence, so denials "
        "can be counted by cause and not only read",
        CHECKED, f"{len(requests)} request(s)")
    deterministic = Property(
        P_DETERMINISTIC,
        "the same request decided twice gives the same decision, stage "
        "for stage",
        CHECKED, f"{len(requests)} request(s)")

    unfaithful: list[Failure] = []
    untyped: list[Failure] = []
    unstable: list[Failure] = []

    for request in requests:
        for stage in pipeline.stages:
            faithful.cases += 1
            model = models.get(stage.name)
            if model is None:
                continue        # P_MODELLED already reports this
            try:
                rationale = stage.check(request)
            except Exception as exc:   # noqa: BLE001
                unfaithful.append(Failure(
                    F_UNFAITHFUL, P_MODEL_FAITHFUL,
                    f"{stage.name} raised {type(exc).__name__} on "
                    f"{request.tool}: {exc}",
                    {"stage": stage.name, "tool": request.tool}))
                continue
            if rationale.outcome not in model.emits:
                unfaithful.append(Failure(
                    F_UNFAITHFUL, P_MODEL_FAITHFUL,
                    f"{stage.name} answered '{rationale.outcome}' on "
                    f"{request.tool}, which its model does not declare",
                    {"stage": stage.name, "outcome": rationale.outcome,
                     "declared": sorted(model.emits)}))
            if rationale.code not in model.codes:
                unfaithful.append(Failure(
                    F_UNFAITHFUL, P_MODEL_FAITHFUL,
                    f"{stage.name} cited code '{rationale.code}' on "
                    f"{request.tool}, which its model does not declare",
                    {"stage": stage.name, "code": rationale.code,
                     "declared": sorted(model.codes)}))

        typed.cases += 1
        decision = pipeline.decide(request)
        declared = {c for m in models.values() for c in m.codes}
        declared.add("stage-error")
        for rationale in decision.rationale:
            if rationale.outcome != DENY:
                continue
            if rationale.code not in declared:
                untyped.append(Failure(
                    F_UNTYPED_DENY, P_TYPED_DENIALS,
                    f"{rationale.stage} denied {request.tool} citing "
                    f"'{rationale.code}', which no model declares",
                    {"stage": rationale.stage, "code": rationale.code}))
            if not rationale.reason:
                untyped.append(Failure(
                    F_UNTYPED_DENY, P_TYPED_DENIALS,
                    f"{rationale.stage} denied {request.tool} with no "
                    f"sentence for a person to read",
                    {"stage": rationale.stage}))

        deterministic.cases += 1
        again = pipeline.decide(request)
        if again.to_dict() != decision.to_dict():
            unstable.append(Failure(
                F_NONDETERMINISTIC, P_DETERMINISTIC,
                f"{request.tool} decided {decision.outcome} then "
                f"{again.outcome}", {"tool": request.tool}))

    faithful.failures = tuple(unfaithful)
    typed.failures = tuple(untyped)
    deterministic.failures = tuple(unstable)
    return faithful, typed, deterministic


# ---------------------------------------------------------------------------
# Digest — so a pipeline change cannot ship without a fresh meta-proof
# ---------------------------------------------------------------------------

def metamodel_digest(pipeline: PolicyPipeline | None = None,
                     models: dict[str, StageModel] | None = None,
                     laws: tuple[Law, ...] = LAWS) -> str:
    """A content digest over the pipeline *as modelled*.

    `regressiongate.fingerprint()` carries this, so reordering the stages,
    adding one, or widening what a stage may emit is a governed change
    that has to be re-recorded by a named person — not a diff that lands
    with the tests still green.
    """
    pipeline = pipeline or PolicyPipeline()
    models = models if models is not None else STAGE_MODELS
    payload = {
        "order": list(pipeline.names()),
        "models": [models[n].to_dict() for n in sorted(models)],
        "laws": [law.to_dict() for law in laws],
        "outcomes": list(OUTCOMES),
        "restrictiveness": dict(RESTRICTIVENESS),
        "properties": sorted(PROPERTY_IDS),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


PROPERTY_IDS: tuple[str, ...] = (
    P_DENY_DOMINANCE, P_ASK_NOT_FINAL, P_TRAIL_SHAPE, P_DECIDER_SOUND,
    P_NO_SILENT_WIDEN, P_REORDER_SAFE, P_CRASH_DENIES, P_MODELLED,
    P_STATELESS, P_LAWS_HOLD, P_MODEL_FAITHFUL, P_TYPED_DENIALS,
    P_DETERMINISTIC,
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def verify_metamodel(pipeline: PolicyPipeline | None = None,
                     models: dict[str, StageModel] | None = None,
                     requests: tuple[Request, ...] | None = None,
                     ) -> MetaReport:
    """Prove every meta-property, and report proved and checked apart."""
    import tempfile

    pipeline = pipeline or PolicyPipeline()
    models = models if models is not None else STAGE_MODELS
    if requests is None:
        root = str(Path(tempfile.mkdtemp(prefix="fa-meta-")).resolve())
        requests = corpus(root)

    dominance, ask_final, trail, decider = _prove_deny_dominance()
    properties = (
        dominance, ask_final, trail, decider,
        _prove_no_silent_widen(),
        _prove_reorder_safety(),
        _prove_crash_denies(),
        _prove_modelled(pipeline, models),
        _prove_stateless(pipeline),
        _prove_laws(pipeline),
        *_check_faithful(pipeline, models, requests),
    )
    return MetaReport(properties, metamodel_digest(pipeline, models))


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import tempfile

    argv = sys.argv[1:]

    if "--check" in argv or "--json" in argv:
        report = verify_metamodel()
        print(json.dumps(report.to_dict(), indent=2) if "--json" in argv
              else report.format())
        raise SystemExit(0 if report.ok else 1)

    if "--digest" in argv:
        print(metamodel_digest())
        raise SystemExit(0)

    # -- the shipped pipeline ------------------------------------------
    report = verify_metamodel()
    assert report.ok, report.format()
    assert report.proved == 10, report.format()
    assert report.checked == 3, report.format()
    assert len(report.properties) == len(PROPERTY_IDS)
    assert {p.id for p in report.properties} == set(PROPERTY_IDS)
    assert report.cases > 200_000, report.cases

    # the spec is order-free; that is the point of writing it separately
    assert spec_outcome([ALLOW, ASK, DENY]) == DENY
    assert spec_outcome([DENY, ASK, ALLOW]) == DENY
    assert spec_outcome([SKIP, ASK, ALLOW]) == ASK
    assert spec_outcome([SKIP, SKIP]) == ALLOW
    assert spec_outcome([]) == ALLOW
    assert spec_trail_length([ALLOW, DENY, ALLOW]) == 2
    assert spec_trail_length([ALLOW, ASK, ALLOW]) == 3

    # -- NEGATIVE CONTROLS ----------------------------------------------
    # A verifier that cannot fail proves nothing. Each property below is
    # handed a pipeline that genuinely breaks it, and must say so. Without
    # this block a refactor that turned any property into a no-op would
    # leave the report just as green as it is now.

    class _AskWins(PolicyPipeline):
        """The bug the pipeline replaced: the first objection returns."""

        def decide(self, request):
            from .policypipeline import PipelineDecision
            rationale = []
            for stage in self.stages:
                result = stage.check(request)
                rationale.append(result)
                if result.decisive:
                    return PipelineDecision(result.outcome, request.tool,
                                            request.role.name,
                                            tuple(rationale))
            return PipelineDecision(ALLOW, request.tool, request.role.name,
                                    tuple(rationale))

    def _broken(vector, crash_at=None):
        stages = []
        for index, outcome in enumerate(vector):
            name = f"s{index}"
            stages.append(CrashingStage(name) if index == crash_at
                          else ScriptedStage(name, outcome))
        return _AskWins(tuple(stages))

    caught = _prove_deny_dominance(_broken)
    assert not caught[0].ok, "an ask that hides a deny must be caught"
    assert caught[0].failures[0].id == F_SPEC_MISMATCH
    assert not caught[1].ok, "ask-is-not-final must be caught too"
    assert caught[1].failures[0].id == F_ASK_HID_DENY

    # An empty pipeline permits everything, so a pipeline that dropped its
    # stages would widen. Prove the widen check can see that.
    def _drops_tail(vector, crash_at=None):
        """Only ever consults the first stage — so the long pipeline is
        no more restrictive than its own one-stage prefix, which is the
        widen this property exists to catch."""
        return PolicyPipeline(tuple(ScriptedStage(f"s{i}", o)
                                    for i, o in enumerate(vector))[:1])

    widen = _prove_no_silent_widen(_drops_tail)
    assert not widen.ok, "ignoring later stages widens and must be caught"
    assert widen.failures[0].id == F_WIDENED

    class _NeverCatches(PolicyPipeline):
        """Fails open on a stage error — the fault this property pins."""

        def decide(self, request):
            from .policypipeline import PipelineDecision
            rationale = []
            for stage in self.stages:
                try:
                    rationale.append(stage.check(request))
                except Exception:
                    continue       # the bug: a crash is silently skipped
            return PipelineDecision(spec_outcome(r.outcome for r in rationale),
                                    request.tool, request.role.name,
                                    tuple(rationale))

    crash = _prove_crash_denies(
        lambda v, crash_at=None: _NeverCatches(tuple(
            CrashingStage(f"s{i}") if i == crash_at
            else ScriptedStage(f"s{i}", o) for i, o in enumerate(v))))
    assert not crash.ok, "a crash that is swallowed must be caught"
    assert crash.failures[0].id == F_CRASH_PERMITTED

    # A new stage with no model must fail by name, not be skipped.
    from .policypipeline import ManifestStage
    extra = PolicyPipeline(DEFAULT_STAGES + (ScriptedStage("newcomer",
                                                           ALLOW),))
    unmodelled = _prove_modelled(extra, STAGE_MODELS)
    assert not unmodelled.ok and unmodelled.failures[0].id == F_UNMODELLED
    phantom = _prove_modelled(PolicyPipeline((ManifestStage(),)),
                              STAGE_MODELS)
    assert not phantom.ok
    assert any(f.id == F_PHANTOM for f in phantom.failures)

    # A stage that remembers must be caught. ScriptedStage holds its own
    # outcome, so it is a ready-made stateful stage.
    stateful = _prove_stateless(PolicyPipeline((ScriptedStage("s0", ALLOW),)))
    assert not stateful.ok and stateful.failures[0].id == F_STATEFUL

    # Approval must be last; move it and the law must break, with the
    # reason attached rather than a bare assertion failure.
    shuffled = PolicyPipeline(
        (DEFAULT_STAGES[0], DEFAULT_STAGES[-1]) + DEFAULT_STAGES[1:-1])
    laws = _prove_laws(shuffled)
    assert not laws.ok and laws.failures[0].id == F_LAW_BROKEN
    # Two laws break at once, and both name their reason: a human must not
    # be asked before the role is known to hold the capability, and nothing
    # may run after the approval stage.
    reasons = " ".join(f.detail for f in laws.failures)
    assert "does not hold" in reasons, laws.failures
    assert "already accepted" in reasons, laws.failures

    # A stage that emits outside its model must be caught.
    class _Chatty(PolicyStage):
        name = STAGE_MANIFEST

        def check(self, request):
            return Rationale(self.name, ASK, R_NEEDS_CONFIRMATION, "hm")

    root = str(Path(tempfile.mkdtemp(prefix="fa-meta-neg-")).resolve())
    faithful, typed, _det = _check_faithful(
        PolicyPipeline((_Chatty(),)), STAGE_MODELS, corpus(root))
    assert not faithful.ok, "the manifest stage never asks; that must fail"
    assert faithful.failures[0].id == F_UNFAITHFUL
    assert any("does not declare" in f.detail for f in faithful.failures)

    # -- the digest is stable and covers what it claims to ---------------
    first = metamodel_digest()
    assert first == metamodel_digest(), "the digest must be deterministic"
    assert metamodel_digest(shuffled) != first, \
        "reordering the stages must change the digest"
    widened = dict(STAGE_MODELS)
    widened[STAGE_APPROVAL] = _model(
        STAGE_APPROVAL, "q", (ALLOW, ASK, DENY, SKIP),
        STAGE_MODELS[STAGE_APPROVAL].codes, ())
    assert metamodel_digest(models=widened) != first, \
        "widening what a stage may emit must change the digest"

    # -- the report is machine-readable ---------------------------------
    payload = report.to_dict()
    assert payload["ok"] is True
    assert payload["digest"] == first
    assert len(payload["properties"]) == len(PROPERTY_IDS)
    assert all(prop["cases"] > 0 for prop in payload["properties"])
    assert json.loads(json.dumps(payload)) == payload

    print(report.format())
    print(f"POLICYMETA SELF-TEST PASS — {report.proved} proved, "
          f"{report.checked} checked, {report.cases} cases, "
          f"digest {report.digest}")

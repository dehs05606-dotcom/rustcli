"""ASSURANCE — "compliant" as a proof you can walk, not a test count.

A green suite says some checks passed. It does not say *which claim*
they support, whether the claims compose into the thing anyone actually
cares about, or which parts of that thing are resting on an assumption
nobody wrote down. Those are different questions, and a number cannot
answer them.

So compliance is assembled here as an **argument tree**, in the shape
safety engineering has used for decades and this repo can actually
check:

    Claim        something asserted to be true
      Inference  how its children add up to it
        Claim    ...recursively
        Evidence a producer that RUNS NOW and seals what it found
        Assumption  something believed and not proved, stated out loud

Four properties make this a proof rather than a diagram:

**Evidence is derived, never asserted.** An `Evidence` node is not a
sentence about a passing test; it is a callable. `assess()` runs it, and
the node carries the verdict that call returned and the digest of it.
An argument whose evidence is prose is an argument that goes stale
silently.

**Every evidence node seals what it found.** Running one appends an
`assurance.evidence` event carrying its digest, and a node whose digest
has no matching seal is a typed defect. That is what makes a number in
the dashboard traceable: you can follow it to the sealed event it came
from.

**A claim with no support is a defect, not a pass.** `A_UNSUPPORTED`
fires on any claim whose subtree bottoms out in nothing. The failure
mode worth fearing is not a red board; it is a green one over a claim
nobody ever supported.

**Assumptions are nodes, not footnotes.** Everything this stack cannot
prove -- the shell deny-list, the absence of a process sandbox, the
third of the prompt that is advisory -- is an `Assumption` node in the
tree, visible in every report, and a claim that rests on one is reported
as `assumed` rather than `holds`. An honest argument names where it
stops.

`EXHAUSTION` is the inference that earns its keep: it requires a
declared universe and checks the children cover it, so "every error code
has a playbook" cannot quietly become "the seven error codes somebody
remembered".
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Node kinds and statuses
# ---------------------------------------------------------------------------

CLAIM = "claim"
INFERENCE = "inference"
EVIDENCE = "evidence"
ASSUMPTION = "assumption"

KINDS = (CLAIM, INFERENCE, EVIDENCE, ASSUMPTION)

HOLDS = "holds"
FAILS = "fails"
ASSUMED = "assumed"          # true only because an assumption says so
UNSUPPORTED = "unsupported"  # nothing under it at all

STATUSES = (HOLDS, FAILS, ASSUMED, UNSUPPORTED)

# -- how children compose ---------------------------------------------------
ALL_OF = "all-of"            # every child must hold
ANY_OF = "any-of"            # one child is enough
EXHAUSTION = "exhaustion"    # the children enumerate a declared universe

INFERENCES = (ALL_OF, ANY_OF, EXHAUSTION)


# ---------------------------------------------------------------------------
# Typed defects
# ---------------------------------------------------------------------------

A_UNSUPPORTED = "claim-without-support"
A_UNSEALED = "evidence-without-seal"
A_STALE_SEAL = "evidence-seal-mismatch"
A_EMPTY_INFERENCE = "inference-without-children"
A_CYCLE = "argument-has-a-cycle"
A_DANGLING = "node-names-a-missing-child"
A_UNCOVERED = "exhaustion-does-not-cover-its-universe"
A_HIDDEN_ASSUMPTION = "assumption-without-a-reason"
A_ORPHAN = "node-reachable-from-no-root"

DEFECTS: dict[str, tuple[str, str]] = {
    A_UNSUPPORTED: (
        "a claim has nothing under it",
        "give it evidence, sub-claims, or an assumption that says plainly "
        "what is being taken on trust"),
    A_UNSEALED: (
        "an evidence node produced a verdict that was never sealed",
        "run the case with a log; unsealed evidence cannot be traced back "
        "to anything"),
    A_STALE_SEAL: (
        "an evidence node's digest does not match what was sealed",
        "the evidence changed after it was recorded — re-run the case"),
    A_EMPTY_INFERENCE: (
        "an inference combines nothing",
        "give it children, or delete it; an inference over nothing proves "
        "nothing"),
    A_CYCLE: (
        "the argument contains a cycle, so something supports itself",
        "break the cycle — a claim that is its own evidence is not an "
        "argument"),
    A_DANGLING: (
        "a node names a child that is not in the case",
        "add the child or stop naming it"),
    A_UNCOVERED: (
        "an exhaustion argument does not cover the universe it declared",
        "add the missing members, or narrow the universe and say so"),
    A_HIDDEN_ASSUMPTION: (
        "an assumption does not say what is being assumed or why",
        "write it out; an unstated assumption is the one that bites"),
    A_ORPHAN: (
        "a node is in the case but no root reaches it",
        "attach it, or remove it — an unreachable node supports nothing"),
}


@dataclass(frozen=True)
class Defect:
    kind: str
    node: str
    detail: str = ""

    @property
    def what(self) -> str:
        return DEFECTS.get(self.kind, ("", ""))[0]

    @property
    def remedy(self) -> str:
        return DEFECTS.get(self.kind, ("", ""))[1]

    def to_dict(self) -> dict:
        return {"kind": self.kind, "node": self.node, "detail": self.detail,
                "what": self.what, "remedy": self.remedy}

    def line(self) -> str:
        tail = f" — {self.detail}" if self.detail else ""
        return f"  [{self.kind}] {self.node}{tail}"


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


@dataclass
class Finding:
    """What one evidence producer returned, this run."""
    ok: bool
    detail: str = ""
    facts: dict = field(default_factory=dict)
    digest: str = ""
    seq: int = -1
    error: str = ""

    @property
    def sealed(self) -> bool:
        return self.seq >= 0 and bool(self.digest)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "detail": self.detail, "facts": self.facts,
                "digest": self.digest, "seq": self.seq, "error": self.error}


#: A producer returns (ok, detail, facts). Anything it raises becomes a
#: failed finding -- evidence that could not be gathered is not evidence
#: that came back clean.
Producer = Callable[[], tuple]


@dataclass
class Node:
    """One node of the argument."""
    id: str
    kind: str
    text: str
    children: tuple[str, ...] = ()
    inference: str = ALL_OF
    #: For an EXHAUSTION inference: what the children have to cover, and
    #: which member each child accounts for.
    universe: tuple[str, ...] = ()
    covers: tuple[str, ...] = ()
    producer: Producer | None = None
    #: Filled in by `assess`.
    finding: Finding | None = None
    status: str = UNSUPPORTED

    def to_dict(self) -> dict:
        payload = {"id": self.id, "kind": self.kind, "text": self.text,
                   "children": list(self.children), "status": self.status}
        if self.kind == INFERENCE:
            payload["inference"] = self.inference
            if self.universe:
                payload["universe"] = list(self.universe)
        if self.covers:
            payload["covers"] = list(self.covers)
        if self.finding is not None:
            payload["finding"] = self.finding.to_dict()
        return payload


def claim(ident: str, text: str, *children: str, covers: tuple = ()) -> Node:
    return Node(ident, CLAIM, text, tuple(children), covers=tuple(covers))


def inference(ident: str, text: str, *children: str, kind: str = ALL_OF,
              universe: tuple = ()) -> Node:
    return Node(ident, INFERENCE, text, tuple(children), kind,
                tuple(universe))


def evidence(ident: str, text: str, producer: Producer,
             covers: tuple = ()) -> Node:
    return Node(ident, EVIDENCE, text, (), producer=producer,
                covers=tuple(covers))


def assumption(ident: str, text: str, covers: tuple = ()) -> Node:
    return Node(ident, ASSUMPTION, text, covers=tuple(covers))


# ---------------------------------------------------------------------------
# The case
# ---------------------------------------------------------------------------

@dataclass
class Case:
    """A whole argument: nodes, roots, and what assessing them found."""
    nodes: dict[str, Node] = field(default_factory=dict)
    roots: tuple[str, ...] = ()
    defects: tuple[Defect, ...] = ()
    assessed_at: float = 0.0

    def add(self, *nodes: Node) -> "Case":
        for node in nodes:
            self.nodes[node.id] = node
        return self

    @property
    def ok(self) -> bool:
        return (not self.defects
                and all(self.nodes[r].status in (HOLDS, ASSUMED)
                        for r in self.roots if r in self.nodes))

    @property
    def proved(self) -> tuple[Node, ...]:
        return tuple(n for n in self._sorted() if n.status == HOLDS)

    @property
    def assumed(self) -> tuple[Node, ...]:
        return tuple(n for n in self._sorted() if n.kind == ASSUMPTION)

    @property
    def resting_on_assumption(self) -> tuple[Node, ...]:
        return tuple(n for n in self._sorted()
                     if n.kind == CLAIM and n.status == ASSUMED)

    def _sorted(self) -> tuple[Node, ...]:
        return tuple(self.nodes[k] for k in sorted(self.nodes))

    def reachable(self) -> set[str]:
        seen: set[str] = set()
        stack = list(self.roots)
        while stack:
            current = stack.pop()
            if current in seen or current not in self.nodes:
                continue
            seen.add(current)
            stack.extend(self.nodes[current].children)
        return seen

    def path_to(self, ident: str) -> tuple[str, ...]:
        """One route from a root to this node, for a person reading back."""
        for root in self.roots:
            found = self._path(root, ident, set())
            if found:
                return found
        return ()

    def _path(self, current: str, target: str,
              seen: set[str]) -> tuple[str, ...]:
        if current in seen or current not in self.nodes:
            return ()
        seen = seen | {current}
        if current == target:
            return (current,)
        for child in self.nodes[current].children:
            tail = self._path(child, target, seen)
            if tail:
                return (current,) + tail
        return ()

    def to_dict(self) -> dict:
        return {"ok": self.ok, "roots": list(self.roots),
                "assessed_at": self.assessed_at,
                "nodes": [n.to_dict() for n in self._sorted()],
                "defects": [d.to_dict() for d in self.defects]}

    def format(self) -> str:
        head = (f"ASSURANCE CASE — {len(self.nodes)} node(s): "
                f"{len(self.proved)} hold, "
                f"{len(self.resting_on_assumption)} rest on an assumption, "
                f"{len(self.assumed)} assumption(s) stated")
        if not self.ok:
            head += f" — {len(self.defects)} defect(s)"
        lines = [head]
        for root in self.roots:
            lines.extend(self._render(root, 1, set()))
        lines.extend(d.line() for d in self.defects)
        return "\n".join(lines)

    def _render(self, ident: str, depth: int, seen: set[str]) -> list[str]:
        if ident not in self.nodes:
            return [f"{'  ' * depth}?? {ident} (missing)"]
        if ident in seen:
            return [f"{'  ' * depth}.. {ident} (already shown)"]
        seen = seen | {ident}
        node = self.nodes[ident]
        mark = {HOLDS: "ok ", FAILS: "FAIL", ASSUMED: "asm",
                UNSUPPORTED: "??? "}.get(node.status, "?")
        tag = {CLAIM: "", INFERENCE: f"[{node.inference}] ",
               EVIDENCE: "* ", ASSUMPTION: "~ "}[node.kind]
        out = [f"{'  ' * depth}{mark} {tag}{node.text}"]
        for child in node.children:
            out.extend(self._render(child, depth + 1, seen))
        return out


# ---------------------------------------------------------------------------
# Assessing
# ---------------------------------------------------------------------------

def assess(case: Case, log=None) -> Case:
    """Run every evidence producer, fold the tree, and find the defects.

    Evidence runs first and is sealed as it runs, so the statuses above
    it are computed from findings that exist rather than from a cached
    claim about them.
    """
    defects: list[Defect] = []

    # -- structure, before anything is run -----------------------------
    for node in case._sorted():
        for child in node.children:
            if child not in case.nodes:
                defects.append(Defect(A_DANGLING, node.id,
                                      f"names {child!r}"))
        if node.kind == INFERENCE and not node.children:
            defects.append(Defect(A_EMPTY_INFERENCE, node.id, node.text))
        if node.kind == ASSUMPTION and not node.text.strip():
            defects.append(Defect(A_HIDDEN_ASSUMPTION, node.id,
                                  "no text"))
    cycle = _find_cycle(case)
    if cycle:
        defects.append(Defect(A_CYCLE, cycle[0], " -> ".join(cycle)))

    reachable = case.reachable()
    for ident in sorted(set(case.nodes) - reachable):
        defects.append(Defect(A_ORPHAN, ident, case.nodes[ident].text))

    # -- evidence: run it, seal it -------------------------------------
    for node in case._sorted():
        if node.kind != EVIDENCE:
            continue
        node.finding = _run(node, log)
        node.status = HOLDS if node.finding.ok else FAILS
        if log is not None and not node.finding.sealed:
            defects.append(Defect(A_UNSEALED, node.id, node.text))

    for node in case._sorted():
        if node.kind == ASSUMPTION:
            node.status = ASSUMED

    # -- fold, bottom up ------------------------------------------------
    resolving: set[str] = set()
    for root in case.roots:
        _fold(case, root, resolving, defects)
    # Nodes outside every root still get a status, so a report over an
    # orphan is not silently blank.
    for ident in sorted(set(case.nodes) - reachable):
        _fold(case, ident, set(), [])

    # -- exhaustion coverage --------------------------------------------
    for node in case._sorted():
        if node.kind != INFERENCE or node.inference != EXHAUSTION:
            continue
        covered: set[str] = set()
        for child in node.children:
            kid = case.nodes.get(child)
            if kid is not None:
                covered.update(kid.covers)
        missing = sorted(set(node.universe) - covered)
        if missing:
            defects.append(Defect(
                A_UNCOVERED, node.id,
                f"{len(missing)} uncovered: " + ", ".join(missing[:5])))

    # -- claims with nothing under them ---------------------------------
    for node in case._sorted():
        if node.kind == CLAIM and node.status == UNSUPPORTED:
            defects.append(Defect(A_UNSUPPORTED, node.id, node.text))

    case.defects = tuple(defects)
    case.assessed_at = time.time()
    if log is not None:
        try:
            log.append("assurance.case", case.to_dict(), actor="assurance")
        except Exception:
            pass
    return case


def _run(node: Node, log) -> Finding:
    if node.producer is None:
        return Finding(False, "this evidence node has no producer",
                       error="no producer")
    try:
        result = node.producer()
    except Exception as exc:
        # Evidence that could not be gathered is not evidence that came
        # back clean, and saying so is the whole difference.
        return Finding(False, f"the producer raised "
                              f"{type(exc).__name__}: {exc}",
                       error=type(exc).__name__)
    ok, detail, facts = _unpack(result)
    finding = Finding(bool(ok), str(detail), dict(facts or {}))
    finding.digest = _digest({"id": node.id, "ok": finding.ok,
                              "facts": finding.facts})
    if log is not None:
        try:
            log.append("assurance.evidence",
                       {"node": node.id, "ok": finding.ok,
                        "detail": finding.detail, "facts": finding.facts,
                        "digest": finding.digest}, actor="assurance")
            finding.seq = log.head()
        except Exception:
            finding.seq = -1
    return finding


def _unpack(result) -> tuple:
    if isinstance(result, tuple):
        if len(result) == 3:
            return result
        if len(result) == 2:
            return result[0], result[1], {}
        if len(result) == 1:
            return result[0], "", {}
    return bool(result), "", {}


def _fold(case: Case, ident: str, resolving: set[str],
          defects: list[Defect]) -> str:
    node = case.nodes.get(ident)
    if node is None:
        return FAILS
    if ident in resolving:          # a cycle; already reported
        return FAILS
    if node.kind in (EVIDENCE, ASSUMPTION):
        return node.status
    resolving = resolving | {ident}

    statuses = [_fold(case, child, resolving, defects)
                for child in node.children]
    if not statuses:
        node.status = UNSUPPORTED
        return node.status

    if node.kind == INFERENCE and node.inference == ANY_OF:
        if any(s == HOLDS for s in statuses):
            node.status = HOLDS
        elif any(s == ASSUMED for s in statuses):
            node.status = ASSUMED
        else:
            node.status = FAILS
        return node.status

    # ALL_OF, EXHAUSTION and a claim over its children all conjoin. An
    # assumption anywhere underneath makes the whole thing assumed, not
    # proved -- which is the point of tracking them as nodes.
    if any(s == FAILS for s in statuses):
        node.status = FAILS
    elif any(s == UNSUPPORTED for s in statuses):
        node.status = UNSUPPORTED
    elif any(s == ASSUMED for s in statuses):
        node.status = ASSUMED
    else:
        node.status = HOLDS
    return node.status


def _find_cycle(case: Case) -> tuple[str, ...]:
    colour: dict[str, int] = {}

    def walk(ident: str, path: list[str]) -> tuple[str, ...]:
        if colour.get(ident) == 1:
            start = path.index(ident) if ident in path else 0
            return tuple(path[start:] + [ident])
        if colour.get(ident) == 2 or ident not in case.nodes:
            return ()
        colour[ident] = 1
        for child in case.nodes[ident].children:
            found = walk(child, path + [ident])
            if found:
                return found
        colour[ident] = 2
        return ()

    for ident in sorted(case.nodes):
        found = walk(ident, [])
        if found:
            return found
    return ()


# ---------------------------------------------------------------------------
# Seal verification — every number traceable to a sealed node
# ---------------------------------------------------------------------------

def _seq_of(event) -> int:
    """An event's position, with zero meaning zero.

    `getattr(ev, "seq", -1) or -1` reads naturally and is wrong: seq 0 is
    falsy, so the first event in a log reports -1.
    """
    value = getattr(event, "seq", None)
    return int(value) if isinstance(value, int) else -1


def verify_seals(case: Case, log) -> tuple[Defect, ...]:
    """Check every evidence finding against what the log actually holds."""
    sealed: dict[str, dict] = {}
    for ev in log.events():
        if ev.type != "assurance.evidence":
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        sealed[str(data.get("node") or "")] = {
            **data, "seq": _seq_of(ev)}
    out: list[Defect] = []
    for node in case._sorted():
        if node.kind != EVIDENCE or node.finding is None:
            continue
        row = sealed.get(node.id)
        if row is None:
            out.append(Defect(A_UNSEALED, node.id, node.text))
            continue
        if row.get("digest") != node.finding.digest:
            out.append(Defect(
                A_STALE_SEAL, node.id,
                f"sealed {row.get('digest')}, computed "
                f"{node.finding.digest}"))
    return tuple(out)


# ---------------------------------------------------------------------------
# The shipped case
# ---------------------------------------------------------------------------

def _ev_invariants() -> tuple:
    from .invariants import verify
    report = verify()
    return (report.ok,
            f"{len(report.checked)} claim(s) over {report.cases} case(s); "
            f"{report.proved} proved exhaustively, {report.sampled} sampled",
            {"invariants": len(report.checked), "cases": report.cases,
             "proved": report.proved, "sampled": report.sampled,
             "failures": [c.invariant.id for c in report.failures]})


def _ev_runbooks() -> tuple:
    import tempfile

    from .runbook import run_all
    report = run_all(Path(tempfile.mkdtemp(prefix="fa-assurance-")))
    return (report.ok,
            f"{report.passed}/{len(report.outcomes)} playbooks still hold",
            {"scenarios": len(report.outcomes), "passed": report.passed,
             "defects": [d.kind for d in report.defects]})


def _ev_envelopes() -> tuple:
    from .envelopes import check_declarations
    from .toolcontract import build_contracts
    from .tools import build_registry
    found = check_declarations(build_contracts(build_registry()))
    return (not found,
            f"{len(found)} declaration problem(s)",
            {"problems": [v.kind for v in found]})


def _ev_regression_gate() -> tuple:
    import tempfile

    from .regressiongate import check_repo
    verdict, _fp = check_repo(
        Path(__file__).resolve().parent.parent,
        Path(tempfile.mkdtemp(prefix="fa-assurance-gate-")))
    return (verdict.allowed,
            "the rule set still holds and still catches"
            if verdict.allowed else "the gate blocked",
            {"reasons": [r.code for r in verdict.reasons],
             "score": verdict.bench.score if verdict.bench else None})


def _ev_contract_lock() -> tuple:
    from .contractmanifest import LOCK_NAME, manifest, read_lock
    from .governance import VERSIONS
    from .toolcontract import build_contracts
    from .tools import build_registry
    root = Path(__file__).resolve().parent.parent
    locked = read_lock(root / LOCK_NAME) or {}
    current = manifest(build_contracts(build_registry()), VERSIONS)
    same = locked.get("digest") == current.get("digest")
    return (same, "the lock matches the registry" if same
            else "the lock and the registry disagree",
            {"locked": locked.get("digest"), "current": current.get("digest")})


def _ev_governance() -> tuple:
    from .governance import check_repo
    result = check_repo(Path(__file__).resolve().parent.parent)
    return (result.ok,
            f"{len(result.refusals)} refusal(s)",
            {"refusals": [r.code for r in result.refusals]})


def _ev_taxonomy_total() -> tuple:
    from .recovery import PLAYBOOKS
    from .toolcontract import ERROR_CODES
    missing = sorted(set(ERROR_CODES) - set(PLAYBOOKS))
    return (not missing,
            f"{len(ERROR_CODES)} error code(s), every one with a playbook",
            {"codes": list(ERROR_CODES), "missing": missing})


def _ev_release_unconstructible() -> tuple:
    from .releasegate import Evidence as _E
    from .releasegate import Range, Release, ReleaseRefused
    try:
        Release(object(), Range("probe"), "d", _E(), 0.0)
    except ReleaseRefused:
        return (True, "a Release cannot be built outside the gate", {})
    except Exception as exc:
        return (False, f"Release raised {type(exc).__name__}", {})
    return (False, "a Release was constructed outside the gate", {})


def _ev_budget_floors() -> tuple:
    from .budgets import Budget, BudgetPlanner, depth_rank
    from .toolcontract import build_contracts
    from .tools import build_registry
    contracts = build_contracts(build_registry())
    planner = BudgetPlanner(contracts)
    breaches = []
    checked = 0
    for tool in sorted(contracts):
        for total in (0, 1, 3, 10, 30, 100):
            d = planner.plan(tool, Budget(total))
            checked += 1
            if not d.refused and depth_rank(d.depth) < depth_rank(d.floor):
                breaches.append(f"{tool}@{total}")
    return (not breaches,
            f"{checked} tool/budget pair(s), none verified below its floor",
            {"checked": checked, "breaches": breaches})


def _ev_consensus_holds() -> tuple:
    from .consensus import CONSERVATIVE, Audit, HOLD
    audit = Audit(HOLD, ())
    unresolved = not audit.released
    audit.resolution = CONSERVATIVE
    conservative_blocks = not audit.released
    return (unresolved and conservative_blocks,
            "a hold is not released by silence, and the conservative "
            "resolution blocks", {})


def _ev_loop_needs_a_gate() -> tuple:
    from .invariantloop import Candidate, Ledger, NotGated
    from .invariants import CONSISTENCY
    led = Ledger()
    led.observe([Candidate("probe", "provenance-gap", "provenance",
                           CONSISTENCY, "a probe")])
    try:
        led.accept("probe", "someone", "why", None)
    except NotGated:
        return (True, "no candidate is adopted without a passing gate", {})
    except Exception as exc:
        return (False, f"raised {type(exc).__name__}", {})
    return (False, "a candidate was adopted with no gate at all", {})


def shipped_case() -> Case:
    """The argument this repo actually makes about itself."""
    from .toolcontract import ERROR_CODES

    case = Case()
    case.add(
        # -- the root ------------------------------------------------
        claim("root",
              "Nothing that violates the rules gets through, and every "
              "decision can be explained afterwards",
              "inf-root"),
        inference("inf-root", "both halves must hold",
                  "c-blocked", "c-explained", "c-honest"),

        # -- half one: nothing non-compliant executes -----------------
        claim("c-blocked",
              "A call or reply that violates the rules does not survive",
              "inf-blocked"),
        inference("inf-blocked", "the decision points, taken together",
                  "c-contract", "c-policy", "c-behaviour", "c-reply"),

        claim("c-contract",
              "A call that does not fit its contract never runs",
              "inf-contract"),
        inference("inf-contract", "the contract layer's own checks",
                  "e-lock", "e-governance", "e-invariants"),
        evidence("e-lock",
                 "the contract lock matches the live registry",
                 _ev_contract_lock),
        evidence("e-governance",
                 "no breaking contract change is unversioned or "
                 "uncarried", _ev_governance),
        evidence("e-invariants",
                 "every stated guarantee is machine-checked",
                 _ev_invariants),

        claim("c-policy",
              "A call the policy refuses does not execute",
              "inf-policy"),
        inference("inf-policy", "the pipeline's own proofs",
                  "e-policy-meta", "a-no-sandbox"),
        evidence("e-policy-meta",
                 "the policy metamodel's meta-properties hold",
                 _ev_policy_meta),
        assumption("a-no-sandbox",
                   "the policy refuses calls but does not sandbox the "
                   "process: a tool runs with the agent's own privileges, "
                   "so refusal is a boundary in the call path and not in "
                   "the operating system"),

        claim("c-behaviour",
              "A call that does something its contract forbids is caught",
              "inf-behaviour"),
        inference("inf-behaviour", "envelopes, and what they cannot see",
                  "e-envelopes", "a-envelope-scope", "a-shell-denylist"),
        evidence("e-envelopes",
                 "every tool declares an envelope consistent with its "
                 "contract", _ev_envelopes),
        assumption("a-envelope-scope",
                   "an envelope observes only the paths the call's own "
                   "arguments name; a tool that writes somewhere it never "
                   "named is outside what this can see"),
        assumption("a-shell-denylist",
                   "the shell is governed by a deny-list, not an "
                   "allow-list, so a destructive command nobody wrote a "
                   "pattern for is permitted by it"),

        claim("c-reply",
              "A reply that violates the rules is not released",
              "inf-reply"),
        inference("inf-reply", "consensus, and what it cannot cover",
                  "e-consensus", "a-rule-coverage", "a-generation"),
        evidence("e-consensus",
                 "a held reply is never released by silence",
                 _ev_consensus_holds),
        assumption("a-rule-coverage",
                   "about a third of the prompt's rules compile to "
                   "predicates; the rest are advisory and no layer here "
                   "checks them"),
        assumption("a-generation",
                   "token generation happens outside this process, so no "
                   "layer here can make a model follow a prompt — only "
                   "stop what violates it from taking effect"),

        # -- half two: everything is explainable ----------------------
        claim("c-explained",
              "Every decision can be reconstructed from the record",
              "inf-explained"),
        inference("inf-explained", "the record and its gates",
                  "c-recovery", "e-release", "e-gate"),
        evidence("e-release",
                 "a release cannot be constructed without complete "
                 "provenance", _ev_release_unconstructible),
        evidence("e-gate",
                 "the regression gate passes on the current rule set",
                 _ev_regression_gate),

        claim("c-recovery",
              "Every failure class has a tested answer",
              "inf-recovery"),
        inference("inf-recovery",
                  "one runbook per error code, enumerated",
                  "e-taxonomy", "e-runbooks", kind=EXHAUSTION,
                  universe=tuple(ERROR_CODES)),
        evidence("e-taxonomy",
                 "every error code has a recovery playbook",
                 _ev_taxonomy_total, covers=tuple(ERROR_CODES)),
        evidence("e-runbooks",
                 "every playbook is proved by an injected failure",
                 _ev_runbooks, covers=tuple(ERROR_CODES)),

        # -- half three: the checking is honest about itself ----------
        claim("c-honest",
              "The checking does not claim more than it checked",
              "inf-honest"),
        inference("inf-honest", "the places we refuse to guess",
                  "e-budgets", "e-loop", "a-scripted-gate"),
        evidence("e-budgets",
                 "no budget verifies a call below its risk floor",
                 _ev_budget_floors),
        evidence("e-loop",
                 "no invariant is adopted without a passing gate",
                 _ev_loop_needs_a_gate),
        assumption("a-scripted-gate",
                   "the regression gate runs scripted turns, so it "
                   "regression-tests the rule set and says nothing about "
                   "any model's behaviour"),
    )
    case.roots = ("root",)
    return case


def _ev_policy_meta() -> tuple:
    from .policymeta import verify_metamodel
    report = verify_metamodel()
    return (report.ok,
            f"{report.proved}/{len(report.properties)} meta-properties "
            f"proved over {report.cases} case(s)",
            {"properties": [p.id for p in report.properties],
             "failures": [f.id for f in report.failures]})


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import tempfile

    from .kernel import EventLog

    argv = sys.argv[1:]
    work = Path(tempfile.mkdtemp(prefix="fa-assurance-"))

    if "--check" in argv or "--json" in argv:
        log = EventLog(path=str(work / "case.jsonl"))
        case = assess(shipped_case(), log)
        seal_defects = verify_seals(case, log)
        case.defects = case.defects + seal_defects
        if "--json" in argv:
            print(json.dumps(case.to_dict(), indent=2))
        else:
            print(case.format())
        raise SystemExit(0 if case.ok else 1)

    log = EventLog(path=str(work / "events.jsonl"))

    # --- a small, sound argument holds --------------------------------
    good = Case().add(
        claim("top", "the thing is true", "inf"),
        inference("inf", "both halves", "e1", "e2"),
        evidence("e1", "half one", lambda: (True, "checked", {"n": 1})),
        evidence("e2", "half two", lambda: (True, "checked", {"n": 2})))
    good.roots = ("top",)
    assess(good, log)
    assert good.ok, good.format()
    assert good.nodes["top"].status == HOLDS
    assert good.nodes["e1"].finding.ok and good.nodes["e1"].finding.sealed
    assert good.nodes["e1"].finding.facts == {"n": 1}

    # --- one failing piece fails the whole thing ----------------------
    bad = Case().add(
        claim("top", "the thing is true", "inf"),
        inference("inf", "both halves", "e1", "e2"),
        evidence("e1", "half one", lambda: (True, "", {})),
        evidence("e2", "half two", lambda: (False, "it did not", {})))
    bad.roots = ("top",)
    assess(bad, log)
    assert not bad.ok and bad.nodes["top"].status == FAILS, bad.format()

    # --- evidence that raises is a failure, not a pass ----------------
    def explodes():
        raise RuntimeError("the producer is broken")

    raising = Case().add(
        claim("top", "t", "inf"),
        inference("inf", "i", "e"),
        evidence("e", "evidence", explodes))
    raising.roots = ("top",)
    assess(raising, log)
    assert raising.nodes["e"].status == FAILS
    assert "RuntimeError" in raising.nodes["e"].finding.detail
    assert not raising.ok

    # --- a claim with nothing under it is a defect --------------------
    empty = Case().add(claim("top", "asserted and unsupported"))
    empty.roots = ("top",)
    assess(empty, log)
    assert not empty.ok
    assert [d.kind for d in empty.defects] == [A_UNSUPPORTED], empty.format()
    assert empty.defects[0].remedy

    # --- an assumption makes a claim assumed, not proved --------------
    resting = Case().add(
        claim("top", "true, but only if you grant this", "inf"),
        inference("inf", "one proved half and one granted", "e", "a"),
        evidence("e", "the proved half", lambda: (True, "", {})),
        assumption("a", "the operating system is not hostile"))
    resting.roots = ("top",)
    assess(resting, log)
    assert resting.nodes["top"].status == ASSUMED, resting.format()
    assert resting.ok, "an argument may rest on a stated assumption"
    assert resting.resting_on_assumption
    assert resting.assumed and resting.assumed[0].id == "a"
    assert "~ " in resting.format()

    # --- an assumption with nothing written in it is a defect ---------
    silent = Case().add(
        claim("top", "t", "inf"),
        inference("inf", "i", "a"),
        assumption("a", "   "))
    silent.roots = ("top",)
    assess(silent, log)
    assert A_HIDDEN_ASSUMPTION in [d.kind for d in silent.defects]

    # --- an inference over nothing proves nothing ---------------------
    hollow = Case().add(
        claim("top", "t", "inf"), inference("inf", "nothing at all"))
    hollow.roots = ("top",)
    assess(hollow, log)
    assert A_EMPTY_INFERENCE in [d.kind for d in hollow.defects]

    # --- a cycle is caught, and does not hang -------------------------
    looped = Case().add(
        claim("a", "a because b", "b"), claim("b", "b because a", "a"))
    looped.roots = ("a",)
    assess(looped, log)
    assert A_CYCLE in [d.kind for d in looped.defects], looped.format()

    # --- a dangling child is caught -----------------------------------
    dangling = Case().add(claim("top", "t", "nowhere"))
    dangling.roots = ("top",)
    assess(dangling, log)
    assert A_DANGLING in [d.kind for d in dangling.defects]

    # --- an orphan supports nothing and is said so --------------------
    orphaned = Case().add(
        claim("top", "t", "inf"),
        inference("inf", "i", "e"),
        evidence("e", "used", lambda: (True, "", {})),
        evidence("unused", "nobody points at this",
                 lambda: (True, "", {})))
    orphaned.roots = ("top",)
    assess(orphaned, log)
    assert A_ORPHAN in [d.kind for d in orphaned.defects]

    # --- exhaustion has to cover its universe -------------------------
    partial = Case().add(
        claim("top", "every colour is handled", "inf"),
        inference("inf", "one per colour", "e1", kind=EXHAUSTION,
                  universe=("red", "green", "blue")),
        evidence("e1", "red and green", lambda: (True, "", {}),
                 covers=("red", "green")))
    partial.roots = ("top",)
    assess(partial, log)
    assert A_UNCOVERED in [d.kind for d in partial.defects], partial.format()
    assert "blue" in [d.detail for d in partial.defects
                      if d.kind == A_UNCOVERED][0]

    whole = Case().add(
        claim("top", "every colour is handled", "inf"),
        inference("inf", "one per colour", "e1", "e2", kind=EXHAUSTION,
                  universe=("red", "green", "blue")),
        evidence("e1", "red and green", lambda: (True, "", {}),
                 covers=("red", "green")),
        evidence("e2", "blue", lambda: (True, "", {}), covers=("blue",)))
    whole.roots = ("top",)
    assess(whole, log)
    assert whole.ok, whole.format()

    # --- any-of needs only one ----------------------------------------
    either = Case().add(
        claim("top", "one route is enough", "inf"),
        inference("inf", "either", "e1", "e2", kind=ANY_OF),
        evidence("e1", "this one works", lambda: (True, "", {})),
        evidence("e2", "this one does not", lambda: (False, "", {})))
    either.roots = ("top",)
    assess(either, log)
    assert either.nodes["top"].status == HOLDS, either.format()

    # --- seals: every finding is traceable ----------------------------
    sealed_log = EventLog(path=str(work / "sealed.jsonl"))
    traced = Case().add(
        claim("top", "t", "inf"),
        inference("inf", "i", "e"),
        evidence("e", "the evidence", lambda: (True, "ok", {"n": 7})))
    traced.roots = ("top",)
    assess(traced, sealed_log)
    assert not verify_seals(traced, sealed_log), \
        [d.to_dict() for d in verify_seals(traced, sealed_log)]
    node = traced.nodes["e"]
    assert node.finding.seq >= 0 and node.finding.digest
    rows = [e for e in sealed_log.events()
            if e.type == "assurance.evidence"]
    assert rows and rows[-1].data["digest"] == node.finding.digest

    # ...and a finding that changed after sealing is caught
    node.finding.digest = "0" * 16
    stale = verify_seals(traced, sealed_log)
    assert [d.kind for d in stale] == [A_STALE_SEAL], \
        [d.to_dict() for d in stale]

    # --- a path from a root to any node is navigable ------------------
    assert traced.path_to("e") == ("top", "inf", "e")
    assert traced.path_to("not-here") == ()

    # --- the shipped case ---------------------------------------------
    real_log = EventLog(path=str(work / "real.jsonl"))
    real = assess(shipped_case(), real_log)
    seal_defects = verify_seals(real, real_log)
    assert not seal_defects, [d.to_dict() for d in seal_defects]
    assert real.ok, real.format()
    assert real.nodes["root"].status == ASSUMED, \
        "the root rests on stated assumptions, and says so"
    assert len(real.assumed) >= 6, len(real.assumed)
    for node in real._sorted():
        if node.kind == EVIDENCE:
            assert node.finding is not None and node.finding.sealed, node.id

    # --- every defect explains itself ---------------------------------
    for kind, (what, remedy) in DEFECTS.items():
        assert what and remedy, kind
        assert Defect(kind, "x").what == what

    print(real.format())
    print(f"ASSURANCE SELF-TEST PASS — {len(real.nodes)} node(s), "
          f"{len(real.assumed)} stated assumption(s), "
          f"{len(DEFECTS)} defect kind(s)")

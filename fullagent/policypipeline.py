"""POLICY PIPELINE — the permission decision as ordered, testable stages.

`ToolPolicy.evaluate()` used to be one function with six checks and an
early return at each. That shape has three faults, and all three are the
kind that only show up once something goes wrong:

  * A stage could not be tested alone. Testing path confinement meant
    constructing a call that got past the capability check first, so a
    path test was also, silently, a capability test.
  * The first check to object won. A command that was merely *asked*
    about returned before the ceiling and the host were ever looked at,
    so an ask could hide a deny that came later in the function.
  * The reason was a sentence. An audit trail of English strings can be
    read by a person and by nothing else: you cannot count denials by
    capability, or answer "how often did the ceiling stop us" without
    parsing prose.

So a decision is now a pipeline. Each stage is a small object with a
name, a question, and no knowledge of the stages around it. Each returns
a `Rationale` carrying both a sentence for a human and `facts` for a
machine. The pipeline runs every stage that can still change the answer,
and the decision carries the whole list -- including the stages that
allowed -- because "why was this permitted" is as much an audit question
as "why was this refused".

Ordering rule, and the reason for it: **a deny anywhere beats an ask
anywhere**. An ask is a question for a human; a deny is an answer. Asking
someone to approve a call that a later stage would refuse teaches them
that approving is how you make the machine stop complaining.

Unknown tools are denied by a stage of their own, with a typed reason
(`E_UNKNOWN_TOOL`) rather than by falling off the end of a lookup. A
plugin that appears at runtime cannot widen its own reach by being
unfamiliar, and the audit can tell "we have never heard of this tool"
apart from "this role lacks the capability".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .toolpolicy import (ALLOW, ASK, DENY, DESTRUCTIVE_RE, NET_FETCH,
                         PATH_ARGS, Role, host_allowed)

# -- stage names. These are the `rule` values the audit trail records, and
# -- they are part of the module's contract: a dashboard counts by them.
STAGE_MANIFEST = "manifest"
STAGE_CAPABILITY = "capability"
STAGE_PATH = "path-confinement"
STAGE_COMMAND = "command-policy"
STAGE_NETWORK = "network-allow-list"
STAGE_RATE = "ceiling"
STAGE_APPROVAL = "ask-capability"

# -- typed reasons. A code is stable across wording changes; a sentence
# -- is not, and something downstream always ends up matching on it.
R_UNKNOWN_TOOL = "unknown-tool"
R_NO_MANIFEST = "declares-no-capability"
R_NO_CAPABILITY = "capability-not-held"
R_OUTSIDE_ROOTS = "outside-roots"
R_UNRESOLVABLE = "path-unresolvable"
R_DESTRUCTIVE = "destructive-command"
R_BLOCKED_HOST = "host-blocked"
R_CEILING = "ceiling-reached"
R_NEEDS_CONFIRMATION = "needs-confirmation"
R_SATISFIED = "satisfied"
R_NOT_APPLICABLE = "not-applicable"

SKIP = "skip"   # a stage that had nothing to say about this call


@dataclass(frozen=True)
class Request:
    """Everything a stage is allowed to look at.

    A stage receives this and nothing else -- no policy object, no
    session. That is what makes one testable on its own: the test builds
    a request and calls the stage, with no earlier stage to get past.
    """
    tool: str
    args: dict
    role: Role
    capabilities: frozenset[str] = frozenset()
    roots: tuple[str, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)
    known: bool = True

    def path_args(self) -> tuple[str, ...]:
        return PATH_ARGS.get(self.tool, ())


@dataclass(frozen=True)
class Rationale:
    """One stage's answer: a sentence for a person, facts for a machine."""
    stage: str
    outcome: str                 # allow | ask | deny | skip
    code: str = R_SATISFIED
    reason: str = ""
    capability: str = ""
    facts: dict = field(default_factory=dict)

    @property
    def decisive(self) -> bool:
        return self.outcome in (DENY, ASK)

    def to_dict(self) -> dict:
        return {"stage": self.stage, "outcome": self.outcome,
                "code": self.code, "reason": self.reason,
                "capability": self.capability, "facts": self.facts}


@dataclass(frozen=True)
class PipelineDecision:
    """The verdict, and every stage that contributed to it."""
    outcome: str
    tool: str
    role: str
    rationale: tuple[Rationale, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOW

    @property
    def denied(self) -> bool:
        return self.outcome == DENY

    def deciding(self) -> Rationale | None:
        """The stage that produced the outcome. A deny wins over an ask.

        For a call nothing objected to, the deciding stage is the last one
        that actually checked something -- a stage that skipped had no
        opinion, and reporting it as the reason for an allow would be a
        lie of omission.
        """
        for wanted in (DENY, ASK):
            for r in self.rationale:
                if r.outcome == wanted:
                    return r
        for r in reversed(self.rationale):
            if r.outcome == ALLOW:
                return r
        return self.rationale[-1] if self.rationale else None

    @property
    def rule(self) -> str:
        decider = self.deciding()
        return decider.stage if decider else STAGE_CAPABILITY

    @property
    def reason(self) -> str:
        decider = self.deciding()
        return decider.reason if decider else ""

    @property
    def capability(self) -> str:
        decider = self.deciding()
        return decider.capability if decider else ""

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "tool": self.tool,
                "role": self.role, "rule": self.rule, "reason": self.reason,
                "capability": self.capability,
                "rationale": [r.to_dict() for r in self.rationale]}

    def format(self) -> str:
        lines = [f"POLICY {self.outcome.upper()} — {self.tool} "
                 f"(role {self.role})"]
        for r in self.rationale:
            mark = {ALLOW: "ok", ASK: "ask", DENY: "DENY",
                    SKIP: "--"}.get(r.outcome, r.outcome)
            tail = f" — {r.reason}" if r.reason else ""
            lines.append(f"  {mark:>5}  {r.stage:<18} {r.code}{tail}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

class PolicyStage:
    """One question, asked of a request, answered with a rationale."""
    name = "stage"

    def check(self, request: Request) -> Rationale:  # pragma: no cover
        raise NotImplementedError

    def _ok(self, code: str = R_SATISFIED, **facts) -> Rationale:
        return Rationale(self.name, ALLOW, code, facts=facts)

    def _skip(self, reason: str = "") -> Rationale:
        return Rationale(self.name, SKIP, R_NOT_APPLICABLE, reason)


class ManifestStage(PolicyStage):
    """Have we ever heard of this tool, and does it declare anything?"""
    name = STAGE_MANIFEST

    def check(self, request: Request) -> Rationale:
        if not request.known:
            return Rationale(
                self.name, DENY, R_UNKNOWN_TOOL,
                f"'{request.tool}' is not a registered tool",
                facts={"tool": request.tool})
        if not request.capabilities:
            # Registered, but declaring nothing. A different fact from
            # "never heard of it", and worth being able to count apart:
            # this one is usually a missing TOOL_CAPABILITIES entry.
            return Rationale(
                self.name, DENY, R_NO_MANIFEST,
                f"{request.tool} declares no capabilities, so it holds none",
                facts={"tool": request.tool, "declared": []})
        return self._ok(declared=sorted(request.capabilities))


class CapabilityStage(PolicyStage):
    """Does the role hold everything the tool declares?"""
    name = STAGE_CAPABILITY

    def check(self, request: Request) -> Rationale:
        missing = sorted(c for c in request.capabilities
                         if not request.role.holds(c))
        if missing:
            return Rationale(
                self.name, DENY, R_NO_CAPABILITY,
                f"role '{request.role.name}' does not hold "
                f"{', '.join(missing)}",
                capability=missing[0],
                facts={"missing": missing,
                       "held": sorted(request.role.granted)})
        return self._ok(needed=sorted(request.capabilities))


class PathStage(PolicyStage):
    """Does every path argument resolve inside a permitted root?"""
    name = STAGE_PATH

    def check(self, request: Request) -> Rationale:
        keys = request.path_args()
        if not keys:
            return self._skip("this tool takes no path")
        for key in keys:
            value = request.args.get(key)
            if not value:
                continue
            resolved, failed = self._resolve(str(value))
            if failed:
                return Rationale(
                    self.name, DENY, R_UNRESOLVABLE,
                    f"{key}={value} cannot be resolved to a real path",
                    facts={"argument": key, "value": str(value)})
            if not self._inside(resolved, request.roots):
                return Rationale(
                    self.name, DENY, R_OUTSIDE_ROOTS,
                    f"{key}={value} resolves outside the permitted roots "
                    f"({', '.join(request.roots)})",
                    facts={"argument": key, "value": str(value),
                           "resolved": resolved,
                           "roots": list(request.roots)})
        return self._ok(checked=list(keys))

    @staticmethod
    def _resolve(candidate: str) -> tuple[str, bool]:
        try:
            target = Path(candidate)
            if not target.is_absolute():
                target = Path.cwd() / target
            # resolve() before comparing: '../..' and a symlink out of the
            # tree both look fine as written and are exactly what this is
            # here to catch.
            return str(target.resolve()), False
        except (OSError, RuntimeError, ValueError):
            return "", True

    @staticmethod
    def _inside(resolved: str, roots: tuple[str, ...]) -> bool:
        return any(resolved == root or resolved.startswith(root + "/")
                   for root in roots)


class CommandStage(PolicyStage):
    """Is this shell command one of the few that destroy things?"""
    name = STAGE_COMMAND

    def check(self, request: Request) -> Rationale:
        if request.tool not in ("run_command", "live_shell"):
            return self._skip("not a shell tool")
        command = str(request.args.get("command", ""))
        hit = DESTRUCTIVE_RE.search(command)
        if not hit:
            return self._ok(command=command[:80])
        facts = {"command": command[:200], "matched": hit.group(0)}
        if not request.role.allow_destructive_commands:
            return Rationale(
                self.name, DENY, R_DESTRUCTIVE,
                f"destructive command denied for role "
                f"'{request.role.name}': {command[:80]}",
                capability="proc.exec", facts=facts)
        return Rationale(
            self.name, ASK, R_DESTRUCTIVE,
            f"destructive command: {command[:80]}",
            capability="proc.exec", facts=facts)


class NetworkStage(PolicyStage):
    """Is this URL one we are willing to reach?"""
    name = STAGE_NETWORK

    def check(self, request: Request) -> Rationale:
        if NET_FETCH not in request.capabilities:
            return self._skip("this tool does not fetch")
        url = request.args.get("url")
        if not url:
            return self._skip("no url argument")
        problem = host_allowed(str(url), request.role.allowed_hosts)
        if problem:
            return Rationale(
                self.name, DENY, R_BLOCKED_HOST, problem,
                capability=NET_FETCH,
                facts={"url": str(url)[:200],
                       "allow_list": list(request.role.allowed_hosts)})
        return self._ok(url=str(url)[:200])


class RateStage(PolicyStage):
    """Has this tool used up its share of the session?"""
    name = STAGE_RATE

    def check(self, request: Request) -> Rationale:
        limit = request.role.ceilings.get(request.tool)
        if limit is None:
            return self._skip("no ceiling for this tool")
        used = request.counts.get(request.tool, 0)
        if used >= limit:
            return Rationale(
                self.name, DENY, R_CEILING,
                f"{request.tool} has hit its session ceiling of {limit}",
                facts={"used": used, "limit": limit})
        return self._ok(used=used, limit=limit)


class ApprovalStage(PolicyStage):
    """Does the role hold this only on condition of asking?"""
    name = STAGE_APPROVAL

    def check(self, request: Request) -> Rationale:
        asking = sorted(c for c in request.capabilities
                        if c in request.role.ask_capabilities)
        if not asking:
            return self._skip("nothing conditional here")
        return Rationale(
            self.name, ASK, R_NEEDS_CONFIRMATION,
            f"{', '.join(asking)} needs confirmation under role "
            f"'{request.role.name}'",
            capability=asking[0], facts={"conditional": asking})


# The order is the policy. Cheap and categorical first, so an unknown
# tool is never measured against a ceiling; approval last, so a human is
# only ever asked about a call every other stage has already accepted.
DEFAULT_STAGES: tuple[PolicyStage, ...] = (
    ManifestStage(), CapabilityStage(), PathStage(), CommandStage(),
    NetworkStage(), RateStage(), ApprovalStage(),
)


class PolicyPipeline:
    """Runs the stages in order and assembles the decision."""

    def __init__(self, stages: tuple[PolicyStage, ...] = DEFAULT_STAGES):
        names = [s.name for s in stages]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate stage names: {names}")
        self.stages = tuple(stages)

    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.stages)

    def decide(self, request: Request) -> PipelineDecision:
        rationale: list[Rationale] = []
        outcome = ALLOW
        for stage in self.stages:
            try:
                result = stage.check(request)
            except Exception as exc:
                # A stage that crashes has not approved anything. Failing
                # closed is the only safe reading of "we do not know".
                result = Rationale(
                    stage.name, DENY, "stage-error",
                    f"{stage.name} failed: {type(exc).__name__}: {exc}")
            rationale.append(result)
            if result.outcome == DENY:
                outcome = DENY
                break          # a deny is final; later stages cannot lift it
            if result.outcome == ASK:
                outcome = ASK  # remembered, but a later deny still wins
        return PipelineDecision(outcome, request.tool, request.role.name,
                                tuple(rationale))


DEFAULT_PIPELINE = PolicyPipeline()


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import tempfile

    from .toolpolicy import FS_READ, FS_WRITE, PROC_EXEC, ROLES

    root = str(Path(tempfile.mkdtemp(prefix="fa-pipe-")).resolve())
    dev = ROLES["developer"]
    readonly = ROLES["readonly"]

    def req(tool, args=None, role=dev, caps=None, counts=None, known=True):
        return Request(tool, args or {}, role,
                       frozenset({FS_READ} if caps is None else caps),
                       (root,), counts or {}, known)

    # --- every stage answers on its own, with no earlier stage to pass --
    unknown = ManifestStage().check(req("teleport", known=False))
    assert unknown.outcome == DENY and unknown.code == R_UNKNOWN_TOOL

    undeclared = ManifestStage().check(req("mystery", caps=[]))
    assert undeclared.outcome == DENY and undeclared.code == R_NO_MANIFEST

    lacking = CapabilityStage().check(
        req("write_file", role=readonly, caps=[FS_WRITE]))
    assert lacking.outcome == DENY and lacking.capability == FS_WRITE
    assert lacking.facts["missing"] == [FS_WRITE]
    assert FS_READ in lacking.facts["held"], lacking.facts

    escape = PathStage().check(req("read_file", {"path": "/etc/passwd"}))
    assert escape.outcome == DENY and escape.code == R_OUTSIDE_ROOTS
    assert escape.facts["resolved"] == "/etc/passwd"

    inside = PathStage().check(
        req("read_file", {"path": os.path.join(root, "a.txt")}))
    assert inside.outcome == ALLOW, inside

    # a symlink out of the tree is out of the tree
    outside = Path(tempfile.mkdtemp(prefix="fa-pipe-out-")).resolve()
    os.symlink(outside, os.path.join(root, "door"))
    through = PathStage().check(
        req("read_file", {"path": os.path.join(root, "door", "x")}))
    assert through.outcome == DENY, through

    nothing = PathStage().check(req("web_fetch", {"url": "https://x.test/"}))
    assert nothing.outcome == SKIP

    rm = CommandStage().check(
        req("run_command", {"command": "rm -rf /tmp/x"}, caps=[PROC_EXEC]))
    assert rm.outcome == DENY and rm.facts["matched"].startswith("rm")

    op = CommandStage().check(
        req("run_command", {"command": "rm -rf /tmp/x"},
            role=ROLES["operator"], caps=[PROC_EXEC]))
    assert op.outcome == ASK, op

    ssrf = NetworkStage().check(
        req("web_fetch", {"url": "http://169.254.169.254/"},
            caps=[NET_FETCH]))
    assert ssrf.outcome == DENY and ssrf.code == R_BLOCKED_HOST

    capped = RateStage().check(
        req("run_command", caps=[PROC_EXEC], counts={"run_command": 400}))
    assert capped.outcome == DENY and capped.facts["limit"] == 400

    # --- the pipeline: a deny anywhere beats an ask anywhere ------------
    pipe = PolicyPipeline()
    assert pipe.names()[0] == STAGE_MANIFEST
    assert pipe.names()[-1] == STAGE_APPROVAL

    allowed = pipe.decide(req("read_file",
                              {"path": os.path.join(root, "a.txt")}))
    assert allowed.allowed, allowed.format()
    assert len(allowed.rationale) == len(pipe.names()), \
        "an allowed call must record every stage, not just the last"

    # an ask from the command stage must not hide a ceiling deny after it
    import dataclasses
    spent = dataclasses.replace(ROLES["operator"],
                                ceilings={"run_command": 5})
    both = pipe.decide(req("run_command",
                           {"command": "rm -rf build"},
                           role=spent, caps=[PROC_EXEC],
                           counts={"run_command": 5}))
    assert both.denied, both.format()
    assert both.rule == STAGE_RATE, both.format()
    assert any(r.outcome == ASK for r in both.rationale), \
        "the ask still happened and must still be on the record"

    asked = pipe.decide(req("delete_path",
                            {"path": os.path.join(root, "a.txt")},
                            caps=["fs.delete"]))
    assert asked.outcome == ASK, asked.format()
    assert asked.rule == STAGE_APPROVAL

    # --- a stage that crashes denies, it does not approve ---------------
    class Broken(PolicyStage):
        name = "broken"

        def check(self, request):
            raise RuntimeError("stage is broken")

    broken = PolicyPipeline((ManifestStage(), Broken()))
    out = broken.decide(req("read_file"))
    assert out.denied and out.deciding().code == "stage-error", out.format()

    # --- the rationale is machine-readable, not just prose --------------
    payload = both.to_dict()
    assert payload["rationale"][-1]["facts"]["limit"] == 5
    assert {r["stage"] for r in payload["rationale"]} <= set(pipe.names())

    try:
        PolicyPipeline((ManifestStage(), ManifestStage()))
        raise AssertionError("duplicate stage names must be refused")
    except ValueError:
        pass

    print(allowed.format())
    print(both.format())
    print(f"POLICYPIPELINE SELF-TEST PASS — {len(pipe.names())} stages")

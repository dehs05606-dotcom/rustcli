"""Guardrail — the multi-layer verification pipeline and its retry loop.

`adherence.py` measures a finished turn and never touches it: by the time
it has a verdict the model has already spoken. This module is the other
half. It runs *before* an action executes and *before* a reply is handed
to the user, decides against the ratified constitution, and — when a
check fails — sends the turn back through the model with the violated
rule quoted at it.

Three stages, in the order a failure is cheapest to catch:

1. **Syntax contract.** Is the output even well-formed? Unterminated code
   fences, an empty reply, a tool-call payload leaking into prose.
2. **Semantic intent match.** Does the reply answer *this* request, and do
   the actions it claims match the actions the log recorded? "I ran the
   tests" with no command in the turn is caught here, deterministically.
3. **Rule-violation scan.** Every blocking policy whose predicate has a
   runtime implementation, evaluated against the turn's facts.

Two commitments, both of which cost coverage and are kept anyway:

- **A predicate with no implementation is reported, never assumed to
  pass.** `unchecked()` names them. A pipeline that returned "clean" for
  rules it never evaluated would be the most dangerous object in this
  codebase — it would make an unverified turn look verified.
- **Correction is a retry, not a residue.** The correction text is handed
  to one regeneration call and is not appended to the conversation. This
  matters because the failure mode being avoided is the one already
  removed from this repo once: a per-turn compliance banner that
  accumulated in history, cost tokens every turn, and was never measured.
  Here the correction exists only for the regeneration that answers it.

The pipeline enforces the *user's own rules against the model's output*.
It cannot make a model comply — no client-side layer can — and it makes
no attempt to work around a model's own safety behaviour: a refusal is
recorded as a refusal, and `no_correction_for_refusal` keeps the retry
loop from arguing with one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .adherence import success_claim
from .constitution import Constitution, PolicyObject
from .spec import terms

# Enforcement strength. The compliance engine moves a session along this
# ladder; the guardrail only reads it.
OBSERVE = 0    # evaluate and record; change nothing
ADVISE = 1     # record, and surface warnings to the caller
VERIFY = 2     # warnings, plus regeneration on a failed response stage
BLOCK = 3      # VERIFY, plus refusing actions that violate blocking policy

LEVEL_NAMES = {OBSERVE: "OBSERVE", ADVISE: "ADVISE",
               VERIFY: "VERIFY", BLOCK: "BLOCK"}

# Tools whose effects a later turn cannot take back.
IRREVERSIBLE_TOOLS = frozenset({"delete_path", "move_path"})
MUTATING_TOOLS = frozenset({"write_file", "edit_file", "apply_patch",
                            "delete_path", "move_path", "copy_path",
                            "create_directory"})
WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})
# Commands that destroy state. Deliberately narrow: a broad pattern that
# blocked ordinary work would be routed around within a day, and a
# guardrail people route around protects nothing.
DESTRUCTIVE_COMMAND_RE = re.compile(
    r"\brm\s+(-\w*[rf]\w*\s+)+|\bgit\s+push\s+(-{1,2}force|-f)\b|"
    r"\bgit\s+reset\s+--hard\b|\bdrop\s+(table|database)\b|"
    r"\bmkfs\b|\bdd\s+if=|>\s*/dev/sd", re.IGNORECASE)

_FENCE_RE = re.compile(r"^\s*(?:```|~~~)", re.MULTILINE)
_PATH_RE = re.compile(r"(?<![\w/.])((?:[\w.-]+/)+[\w.-]+\.\w{1,6})")
_CLAIMED_RUN_RE = re.compile(
    r"\b(?:i|we)\s+(?:just\s+)?(ran|executed|tested|built|installed)\b",
    re.IGNORECASE)
_TOOLCALL_LEAK_RE = re.compile(
    r'\{\s*"(?:name|function|tool_call|arguments)"\s*:', re.IGNORECASE)
_REFUSAL_RE = re.compile(
    r"\b(?:i(?:'m| am) (?:sorry|unable|not able)|i can(?:no|')t (?:help|assist|"
    r"comply|do that)|i (?:won't|will not) (?:help|assist)|"
    r"against my guidelines|as an ai (?:model|assistant))\b", re.IGNORECASE)

MAX_CORRECTION_CHARS = 1400

# `adherence.success_claim` is deliberately narrow — it feeds a ledger
# where a false positive would corrupt a long-running measurement. A
# guardrail wants the wider net: "Everything passes.", "the build
# succeeded", "all green" are all the same promise to a user, and only
# the first of them is something adherence.py will report. This detector
# is a superset of it, with the same discipline that makes it trustworthy
# — a hedge anywhere in the sentence disarms the claim inside it, so
# "this should make the tests pass" is not a claim and never was.
_CLAIM_SUBJECT = (r"(?:tests?|suite|build|compil\w+|install\w+|everything|"
                  r"all|it|this|that|they|the\s+\w+)")
_CLAIM_VERB = (r"(?:pass(?:es|ed|ing)?|succeed(?:s|ed)?|success\w*|work(?:s|ed|ing)?|"
               r"green|fixed|resolved|complete[ds]?|done|ready|clean)")
_BROAD_CLAIM_RE = re.compile(
    rf"\b{_CLAIM_SUBJECT}\s+(?:\w+\s+){{0,2}}{_CLAIM_VERB}\b|"
    r"\ball\s+green\b|\bno\s+(?:errors|failures)\b", re.IGNORECASE)
_HEDGE_RE = re.compile(
    r"\b(?:should|would|could|might|may|expect\w*|probabl\w+|likel\w+|"
    r"hopefull\w+|if|once|after|assum\w+|think|believe|appears?|seems?|"
    r"try|trying|attempt\w*|plan\s+to|intend\w*|let'?s|need\s+to|"
    r"cannot|can'?t|not\s+yet|unable|without)\b", re.IGNORECASE)
_CLAIM_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def claims_success(text: str) -> str:
    """The first unhedged success claim in `text`, or '' if there is none."""
    narrow = success_claim(text or "")
    if narrow:
        return narrow
    for sentence in _CLAIM_SENTENCE_RE.split(text or ""):
        if _HEDGE_RE.search(sentence):
            continue
        m = _BROAD_CLAIM_RE.search(sentence)
        if m:
            return m.group(0).strip()
    return ""


def is_refusal(text: str) -> bool:
    """Whether the model declined rather than answered.

    A refusal is a real answer from the model about what it will do. The
    retry loop must not treat one as a formatting defect and keep asking:
    that is how a correction loop turns into pressure, which is neither
    what the prompt asks for nor something this module will do.
    """
    return bool(_REFUSAL_RE.search(text or ""))


@dataclass(frozen=True)
class Violation:
    """One decided failure, always attributed to the policy it came from."""
    stage: str
    policy_id: str
    predicate: str
    severity: str          # "block" | "warn"
    why: str
    evidence: str = ""
    rule_text: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity == "block"

    def to_dict(self) -> dict:
        return {"stage": self.stage, "policy_id": self.policy_id,
                "predicate": self.predicate, "severity": self.severity,
                "why": self.why, "evidence": self.evidence[:300]}

    def line(self) -> str:
        mark = "✗" if self.blocking else "!"
        return f"  {mark} [{self.stage}] {self.why}  ({self.policy_id})"


@dataclass
class ActionFacts:
    """What is knowable about a tool call before it runs."""
    tool_name: str
    args: dict = field(default_factory=dict)
    reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    approvals: frozenset[str] = frozenset()
    autonomy: int = 3
    prior_tools: tuple[str, ...] = ()
    root: Path = field(default_factory=Path.cwd)

    def path_arg(self) -> str:
        for key in ("path", "src", "file", "target"):
            value = self.args.get(key)
            if value:
                return str(value)
        return ""


@dataclass
class ResponseFacts:
    """What is knowable about a reply before the user sees it."""
    text: str
    user_text: str = ""
    tools_called: tuple[str, ...] = ()
    reads: frozenset[str] = frozenset()
    verdicts_passed: int = 0
    verdicts_failed: int = 0
    tool_errors: int = 0
    root: Path = field(default_factory=Path.cwd)


@dataclass
class StageResult:
    name: str
    ok: bool
    violations: tuple[Violation, ...] = ()
    checked: int = 0
    skipped: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "checked": self.checked,
                "violations": [v.to_dict() for v in self.violations],
                "skipped": list(self.skipped)}


@dataclass
class PipelineResult:
    """The verdict on one response, stage by stage."""
    stages: tuple[StageResult, ...]
    level: int

    @property
    def violations(self) -> tuple[Violation, ...]:
        return tuple(v for s in self.stages for v in s.violations)

    @property
    def blocking(self) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.blocking)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def needs_regeneration(self) -> bool:
        """Whether this result should send the turn back to the model."""
        return self.level >= VERIFY and bool(self.blocking)

    def to_dict(self) -> dict:
        return {"level": LEVEL_NAMES[self.level], "ok": self.ok,
                "stages": [s.to_dict() for s in self.stages],
                "violations": len(self.violations),
                "blocking": len(self.blocking)}

    def format(self) -> str:
        head = "VERIFICATION " + ("PASS" if self.ok else "FAIL")
        lines = [f"{head} — level {LEVEL_NAMES[self.level]}"]
        for s in self.stages:
            mark = "✓" if s.ok else "✗"
            note = f"  ({len(s.skipped)} unchecked)" if s.skipped else ""
            lines.append(f"  {mark} {s.name}: {s.checked} checked{note}")
            for v in s.violations:
                lines.append("  " + v.line())
        return "\n".join(lines)


def _exists(root: Path, candidate: str) -> bool:
    p = Path(candidate)
    if p.is_absolute():
        return p.exists()
    return (root / p).exists()


# --------------------------------------------------------------------------
# Predicate implementations. Each returns a reason string when the policy is
# violated, or "" when it holds. A predicate id with no entry here is
# reported as unchecked — never silently passed.
# --------------------------------------------------------------------------

def _chk_read_before_write(f: ActionFacts) -> str:
    if f.tool_name not in WRITE_TOOLS:
        return ""
    path = f.path_arg()
    if not path or path in f.reads:
        return ""
    if not _exists(f.root, path):
        return ""      # creating a new file: there was nothing to read
    return f"{f.tool_name} would rewrite {path}, which was never read"


def _chk_ask_before_irreversible(f: ActionFacts) -> str:
    if f.tool_name in IRREVERSIBLE_TOOLS and f.tool_name not in f.approvals:
        return f"{f.tool_name} is irreversible and was not approved"
    if f.tool_name == "run_command":
        command = str(f.args.get("command", ""))
        if DESTRUCTIVE_COMMAND_RE.search(command) and \
                "run_command" not in f.approvals:
            return f"destructive command without approval: {command[:80]}"
    return ""


def _chk_understand_before_change(f: ActionFacts) -> str:
    if f.tool_name in MUTATING_TOOLS and not f.reads and not f.prior_tools:
        return (f"{f.tool_name} is the turn's first action and nothing "
                "has been read yet")
    return ""


def _chk_minimal_change(f: ActionFacts) -> str:
    if f.tool_name != "write_file":
        return ""
    path = f.path_arg()
    if path and _exists(f.root, path) and path in f.reads:
        return (f"write_file replaces all of {path}; edit_file makes the "
                "change without rewriting the rest")
    return ""


def _chk_no_unverified_success(f: ResponseFacts) -> str:
    claim = claims_success(f.text)
    if not claim:
        return ""
    if f.verdicts_passed or any(t in ("run_command", "live_shell")
                                for t in f.tools_called):
        return ""
    return f"success claimed with nothing run to show it: {claim[:120]}"


def _chk_no_fabrication(f: ResponseFacts) -> str:
    missing = [p for p in dict.fromkeys(_PATH_RE.findall(f.text))
               if not _exists(f.root, p)]
    if missing:
        return "cited paths that do not exist: " + ", ".join(missing[:4])
    return ""


ACTION_CHECKS: dict[str, Callable[[ActionFacts], str]] = {
    "read-before-write": _chk_read_before_write,
    "ask-before-irreversible": _chk_ask_before_irreversible,
    "understand-before-change": _chk_understand_before_change,
    "minimal-change": _chk_minimal_change,
}

RESPONSE_CHECKS: dict[str, Callable[[ResponseFacts], str]] = {
    "no-unverified-success": _chk_no_unverified_success,
    "no-fabrication": _chk_no_fabrication,
}

# Predicates the compiler can bind but no runtime check can decide. Named
# here so `unchecked()` can report them instead of leaving a caller to
# assume silence means compliance.
UNIMPLEMENTED = ("honest-uncertainty",)


@dataclass
class CorrectionResult:
    text: str
    attempts: int
    corrected: bool
    final: PipelineResult
    history: tuple[PipelineResult, ...] = ()

    def to_dict(self) -> dict:
        return {"attempts": self.attempts, "corrected": self.corrected,
                "final": self.final.to_dict()}


class Guardrail:
    """Decides actions and responses against a ratified constitution."""

    def __init__(self, constitution: Constitution | None, log=None,
                 level: int = VERIFY, max_attempts: int = 2):
        self.constitution = constitution
        self.log = log
        self.level = level
        self.max_attempts = max(1, max_attempts)

    # -- introspection -----------------------------------------------------

    def policies(self) -> tuple[PolicyObject, ...]:
        return self.constitution.policies if self.constitution else ()

    def governing(self) -> dict[str, PolicyObject]:
        """The strongest policy behind each bound predicate.

        Several sentences in a prompt routinely bind to one predicate —
        MAIN states "read before editing" as both a Prime Directive and a
        Tool Usage line. Running the check once per sentence would report
        the same failure twice and put it in the correction twice, so the
        strongest binding speaks for the predicate and the rest are
        represented by it.
        """
        best: dict[str, PolicyObject] = {}
        for policy in self.policies():
            predicate = policy.rule.get("predicate", "")
            if not predicate:
                continue
            held = best.get(predicate)
            if held is None or policy.priority < held.priority:
                best[predicate] = policy
        return best

    def unchecked(self) -> tuple[str, ...]:
        """Predicates the constitution binds that nothing here can decide."""
        bound = {p.rule.get("predicate", "") for p in self.policies()}
        bound.discard("")
        known = set(ACTION_CHECKS) | set(RESPONSE_CHECKS)
        return tuple(sorted(bound - known))

    def _severity(self, policy: PolicyObject) -> str:
        if self.level >= BLOCK and policy.blocking:
            return "block"
        if self.level >= VERIFY and policy.priority <= 1:
            return "block"
        return "warn"

    def _emit(self, event: str, data: dict) -> None:
        if self.log is None:
            return
        try:
            self.log.append(event, data, actor="kernel")
        except Exception:
            pass   # a guardrail that can crash the turn is not a guardrail

    # -- pre-flight --------------------------------------------------------

    def check_action(self, facts: ActionFacts) -> tuple[Violation, ...]:
        """Decide one tool call before it runs."""
        found: list[Violation] = []
        for predicate, policy in sorted(self.governing().items()):
            check = ACTION_CHECKS.get(predicate)
            if check is None:
                continue
            # The tools a policy names are NOT a filter. "Use `read_file`
            # to inspect files before editing them" names read_file and
            # governs edit_file — scoping it to the tool in its own text
            # meant the rule could never fire on the call it was about.
            # Applicability belongs to the predicate, which knows it.
            try:
                why = check(facts)
            except Exception:
                continue     # a broken predicate must not block real work
            if why:
                found.append(Violation(
                    stage="action", policy_id=policy.policy_id,
                    predicate=predicate, severity=self._severity(policy),
                    why=why, evidence=facts.tool_name,
                    rule_text=str(policy.rule.get("text", ""))))
        if found:
            self._emit("guardrail.action", {
                "tool": facts.tool_name, "level": LEVEL_NAMES[self.level],
                "violations": [v.to_dict() for v in found]})
        return tuple(found)

    def block_reason(self, facts: ActionFacts) -> str | None:
        """The reason to refuse this action, or None to let it proceed.

        Shaped for `Agent._gate`, which speaks in block reasons. Only a
        blocking violation stops the call; a warning is recorded and the
        action goes ahead, because a guardrail that stopped work on a
        "prefer" would be turned off within an hour.
        """
        blocking = [v for v in self.check_action(facts) if v.blocking]
        if not blocking:
            return None
        first = blocking[0]
        rule = first.rule_text.strip()
        suffix = f" — prompt rule: {rule[:160]}" if rule else ""
        return f"{first.why}{suffix}"

    # -- the three stages --------------------------------------------------

    def _stage_syntax(self, f: ResponseFacts) -> StageResult:
        v: list[Violation] = []
        text = f.text or ""
        checked = 3
        if not text.strip():
            v.append(Violation("syntax", "contract.non-empty", "syntax-contract",
                               "block", "the reply is empty"))
        if len(_FENCE_RE.findall(text)) % 2:
            v.append(Violation("syntax", "contract.fences", "syntax-contract",
                               "block", "a code fence is never closed",
                               evidence=text[-120:]))
        if _TOOLCALL_LEAK_RE.search(text):
            v.append(Violation("syntax", "contract.toolcall-leak",
                               "syntax-contract", "warn",
                               "a tool-call payload leaked into the prose",
                               evidence=text[:160]))
        return StageResult("syntax contract", not v, tuple(v), checked)

    def _stage_intent(self, f: ResponseFacts) -> StageResult:
        v: list[Violation] = []
        checked = 0
        asked = set(terms(f.user_text))
        # Term overlap only means anything when there is enough text on
        # both sides to mean it. "Where is the retry logic?" answered with
        # "It is in config.py." shares no terms at all and is a perfect
        # answer — scoring that as a deviation is how a guardrail earns
        # its reputation for crying wolf and gets switched off. So the
        # check is confined to the failure it can actually see: a long
        # reply, to a request with real content in it, that shares almost
        # nothing with what was asked.
        if len(asked) >= 6 and len(f.text) >= 400:
            checked += 1
            overlap = len(asked & set(terms(f.text))) / len(asked)
            if overlap < 0.08:
                v.append(Violation(
                    "intent", "contract.addresses-request",
                    "semantic-intent", "warn",
                    f"a {len(f.text)}-character reply shares {overlap:.0%} "
                    "of the request's terms and may answer something else"))
        checked += 1
        claimed = _CLAIMED_RUN_RE.search(f.text)
        if claimed and not any(t in ("run_command", "live_shell")
                               for t in f.tools_called):
            v.append(Violation(
                "intent", "contract.claim-matches-actions", "semantic-intent",
                "block", f"the reply says \"{claimed.group(0)}\" but no "
                         "command ran in this turn",
                evidence=claimed.group(0)))
        return StageResult("semantic intent", not v, tuple(v), checked)

    def _stage_rules(self, f: ResponseFacts) -> StageResult:
        v: list[Violation] = []
        checked = 0
        skipped: set[str] = set()
        for predicate, policy in sorted(self.governing().items()):
            check = RESPONSE_CHECKS.get(predicate)
            if check is None:
                if predicate not in ACTION_CHECKS:
                    skipped.add(predicate)
                continue
            checked += 1
            try:
                why = check(f)
            except Exception:
                continue
            if why:
                v.append(Violation(
                    "rules", policy.policy_id, predicate,
                    self._severity(policy), why,
                    rule_text=str(policy.rule.get("text", ""))))
        return StageResult("rule violation scan", not v, tuple(v),
                           checked, tuple(sorted(skipped)))

    def verify_response(self, facts: ResponseFacts) -> PipelineResult:
        """Run all three stages over one candidate reply."""
        result = PipelineResult(
            stages=(self._stage_syntax(facts), self._stage_intent(facts),
                    self._stage_rules(facts)),
            level=self.level)
        self._emit("guardrail.verify", result.to_dict())
        return result

    # -- correction --------------------------------------------------------

    def correction(self, result: PipelineResult) -> str:
        """The feedback a regeneration gets: the rule, and what is missing.

        The author's own sentence is quoted rather than paraphrased. A
        paraphrase is a second prompt competing with the first, and the
        whole point of the constitution is that there is one source.
        """
        lines = ["The previous reply did not satisfy the system prompt. "
                 "Fix these before answering again:"]
        for n, v in enumerate(result.blocking or result.violations, 1):
            lines.append(f"{n}. {v.why}")
            if v.rule_text:
                lines.append(f"   Rule: {v.rule_text.strip()[:220]}")
        lines.append("Answer the original request again, corrected. "
                     "Do not mention this note.")
        text = "\n".join(lines)
        return text[:MAX_CORRECTION_CHARS]

    def run_with_correction(
            self, generate: Callable[[str | None], str],
            facts_for: Callable[[str], ResponseFacts]) -> CorrectionResult:
        """Generate, verify, and regenerate with feedback until it holds.

        `generate(correction)` is called with None first and with the
        correction text on each retry. The correction is passed to that
        one call; nothing here writes it into the conversation.
        """
        history: list[PipelineResult] = []
        correction: str | None = None
        text = ""
        result: PipelineResult | None = None
        for attempt in range(1, self.max_attempts + 1):
            text = generate(correction)
            result = self.verify_response(facts_for(text))
            history.append(result)
            if not result.needs_regeneration:
                break
            if is_refusal(text):
                # The model declined. That is an answer, not a defect.
                self._emit("guardrail.refusal", {"attempt": attempt})
                break
            if attempt == self.max_attempts:
                break
            correction = self.correction(result)
            self._emit("guardrail.correction",
                       {"attempt": attempt,
                        "violations": [v.to_dict() for v in result.blocking],
                        "chars": len(correction)})
        assert result is not None
        return CorrectionResult(text=text, attempts=len(history),
                                corrected=len(history) > 1 and result.ok,
                                final=result, history=tuple(history))

    def format_status(self) -> str:
        const = self.constitution
        lines = [f"GUARDRAIL — level {LEVEL_NAMES[self.level]}, "
                 f"up to {self.max_attempts} attempts"]
        if const is None:
            lines.append("  no constitution in force — nothing is enforced")
            return "\n".join(lines)
        lines.append(f"  constitution v{const.version} "
                     f"(root {const.root[:12]}) · {len(const.policies)} policies")
        lines.append(f"  action checks {len(ACTION_CHECKS)} · "
                     f"response checks {len(RESPONSE_CHECKS)}")
        missing = self.unchecked()
        if missing:
            lines.append(f"  UNCHECKED (bound, not decidable): "
                         f"{', '.join(missing)}")
        return "\n".join(lines)


if __name__ == "__main__":
    import tempfile
    from pathlib import Path as _Path

    from . import systemprompt
    from .constitution import ConstitutionalCore
    from .kernel import EventLog

    with tempfile.TemporaryDirectory() as td:
        root = _Path(td)
        (root / "seen.py").write_text("x = 1\n")
        log = EventLog(root / "g.jsonl")
        core = ConstitutionalCore(log, app_dir=root)
        const = core.ratify_prompt(
            "main", systemprompt.MAIN,
            tool_names=frozenset({"read_file", "edit_file", "write_file",
                                  "run_command"}))
        g = Guardrail(const, log=log, level=BLOCK, max_attempts=3)

        # --- pre-flight: rewriting a file nobody read -------------------
        # MAIN states this one at EXPECTED strength ("Use `read_file` to
        # inspect files before editing them"), so it warns and lets the
        # work through. Only CRITICAL/REQUIRED policy stops an action.
        unread = ActionFacts("write_file", {"path": "seen.py"}, root=root)
        warned = g.check_action(unread)
        assert any("never read" in v.why for v in warned), warned
        assert not any(v.blocking for v in warned), warned
        assert g.block_reason(unread) is None
        # having read it, the same call is fine
        after_read = ActionFacts("write_file", {"path": "seen.py"},
                                 reads=frozenset({"seen.py"}), root=root)
        assert _chk_read_before_write(after_read) == ""
        # a brand-new file was never readable, so it is not a violation
        fresh = ActionFacts("write_file", {"path": "brand-new.py"}, root=root)
        assert _chk_read_before_write(fresh) == ""

        # --- pre-flight: irreversible and destructive -------------------
        # This one IS blocking: MAIN prohibits it at CRITICAL strength.
        doomed = ActionFacts("delete_path", {"path": "seen.py"}, root=root)
        stop = g.block_reason(doomed)
        assert stop and "irreversible" in stop, stop
        assert "prompt rule:" in stop, stop
        approved = ActionFacts("delete_path", {"path": "seen.py"},
                               approvals=frozenset({"delete_path"}), root=root)
        assert g.block_reason(approved) is None
        # one predicate, one violation, however many sentences bind it
        assert len([v for v in g.check_action(doomed)
                    if v.predicate == "ask-before-irreversible"]) == 1
        assert _chk_ask_before_irreversible(ActionFacts("delete_path", {}))
        assert not _chk_ask_before_irreversible(
            ActionFacts("delete_path", {}, approvals=frozenset({"delete_path"})))
        assert _chk_ask_before_irreversible(
            ActionFacts("run_command", {"command": "rm -rf build"}))
        assert not _chk_ask_before_irreversible(
            ActionFacts("run_command", {"command": "ls -la"}))

        # --- stage 1: syntax -------------------------------------------
        bad = g.verify_response(ResponseFacts("here you go\n```python\nx=1",
                                              root=root))
        assert any("fence" in v.why for v in bad.violations), bad.format()
        assert not g.verify_response(ResponseFacts("", root=root)).ok

        # --- stage 2: a claim with no action behind it ------------------
        lie = g.verify_response(ResponseFacts(
            "I ran the tests and they all pass.", user_text="run the tests",
            tools_called=(), root=root))
        assert lie.blocking, lie.format()
        honest = g.verify_response(ResponseFacts(
            "I ran the tests and they all pass.", user_text="run the tests",
            tools_called=("run_command",), verdicts_passed=1, root=root))
        assert not honest.blocking, honest.format()

        # --- stage 3: fabricated paths, unverified success --------------
        fab = g.verify_response(ResponseFacts(
            "See src/nowhere/ghost.py for the fix.", root=root))
        assert any(v.predicate == "no-fabrication" for v in fab.violations)
        real = g.verify_response(ResponseFacts("See seen.py for it.", root=root))
        assert not any(v.predicate == "no-fabrication" for v in real.violations)

        # --- unchecked predicates are named, never assumed clean --------
        assert "honest-uncertainty" in g.unchecked(), g.unchecked()
        assert any(s.skipped for s in
                   g.verify_response(ResponseFacts("ok", root=root)).stages)

        # --- the retry loop: one bad draft, then a good one -------------
        drafts = ["I ran the tests and everything passes.",
                  "Nothing was run yet; here is what I would run first."]
        seen_corrections: list[str | None] = []

        def generate(correction):
            seen_corrections.append(correction)
            return drafts[min(len(seen_corrections) - 1, len(drafts) - 1)]

        out = g.run_with_correction(
            generate, lambda t: ResponseFacts(t, user_text="run the tests",
                                              root=root))
        assert out.attempts == 2 and out.corrected, out.to_dict()
        assert seen_corrections[0] is None
        assert seen_corrections[1] and "no command ran" in seen_corrections[1]
        assert len(seen_corrections[1]) <= MAX_CORRECTION_CHARS

        # A violation that came from a POLICY quotes the author's own
        # sentence back; the two structural stages have no rule behind
        # them and correctly quote nothing.
        fab_result = g.verify_response(ResponseFacts(
            "Fixed it in src/nowhere/ghost.py.", root=root))
        fab_correction = g.correction(fab_result)
        assert "Rule:" in fab_correction, fab_correction
        assert "Never fabricate" in fab_correction, fab_correction
        assert "Do not mention this note" in fab_correction

        # --- a refusal ends the loop instead of arguing with it ---------
        refusals: list[str | None] = []

        def refuse(correction):
            refusals.append(correction)
            return "I'm sorry, I can't help with that."

        r = g.run_with_correction(
            refuse, lambda t: ResponseFacts(t, user_text="do the thing",
                                            root=root))
        assert r.attempts == 1, r.to_dict()
        assert is_refusal("I'm sorry, I can't help with that.")
        assert not is_refusal("I ran the tests.")

        # --- the wider claim detector, with the hedge discipline kept ---
        assert claims_success("The tests pass.")
        assert claims_success("Yes, the build succeeded.")
        assert claims_success("Everything passes.")
        assert claims_success("All green.")
        assert not claims_success("This should make the tests pass.")
        assert not claims_success("I could not get the build to succeed.")
        assert not claims_success("Once it compiles, everything passes.")
        assert not claims_success("Here is what I would run first.")
        unproven = g.verify_response(ResponseFacts(
            "Yes, the build succeeded.", tools_called=(), root=root))
        assert any(v.predicate == "no-unverified-success"
                   for v in unproven.violations), unproven.format()

        # --- OBSERVE changes nothing --------------------------------------
        quiet = Guardrail(const, log=log, level=OBSERVE)
        assert quiet.block_reason(doomed) is None
        assert not quiet.verify_response(ResponseFacts(
            "I ran the tests.", root=root)).needs_regeneration

        kinds = {e.type for e in log.events()}
        assert {"guardrail.verify", "guardrail.correction"} <= kinds, kinds
        assert "GUARDRAIL" in g.format_status()
        print(g.format_status())
        print("GUARDRAIL SELF-TEST PASS")

"""CONSENSUS — a second opinion on a reply, and no silent override.

The guardrail checks a reply against the compiled rules by binding each
decidable rule to a predicate. That is one reasoning strategy, and a bug
in it is invisible to itself: a predicate that never fires looks exactly
like a rule that was never broken.

So a second strategy checks the same reply against the same rules by a
**different route** -- from the rule text and the surface evidence,
rather than from the predicate bindings. Two implementations of the same
question disagree when one of them is wrong, which is the only signal
either can give that it might be the wrong one.

The rule that makes this worth having:

    **A disagreement is never resolved by picking a side.**

Not by majority, not by trusting the primary, not by trusting the
stricter one silently. A disagreement produces a `Disagreement` object
naming what each strategy concluded and the specific question between
them, and the reply is **held** -- treated as failing -- until something
records a `Resolution` saying who decided and why. Holding is the
conservative reading, and it is written down as a decision rather than
happening by default in the dark.

**About "cross-model".** The second strategy shipped here is
deterministic: no model call, no API key, runs in every test. That is
deliberate -- a verifier that needs the network is a verifier that is
absent exactly when things are going wrong. `ModelStrategy` wraps any
callable, so a second *model* can be plugged in as a third opinion where
one is available, and the same never-silently-override rule applies to
it. It is an addition to the deterministic pair, never a replacement:
two models agreeing is not evidence that either read the rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .guardrail import Guardrail, PipelineResult, ResponseFacts, Violation

# -- verdicts ---------------------------------------------------------------
PASS = "pass"
FAIL = "fail"
UNSURE = "unsure"      # the strategy could not decide; never counts as pass

VERDICTS = (PASS, FAIL, UNSURE)

# -- what the auditor does about the two answers ----------------------------
RELEASE = "release"
BLOCK = "block"
HOLD = "hold"          # they disagree; nothing is released until resolved

OUTCOMES = (RELEASE, BLOCK, HOLD)


@dataclass(frozen=True)
class Finding:
    """One strategy's objection, attributed to the rule behind it."""
    strategy: str
    rule_id: str
    why: str
    evidence: str = ""
    blocking: bool = True

    def to_dict(self) -> dict:
        return {"strategy": self.strategy, "rule_id": self.rule_id,
                "why": self.why, "evidence": self.evidence,
                "blocking": self.blocking}


@dataclass(frozen=True)
class Opinion:
    """What one strategy concluded, and what it looked at."""
    strategy: str
    verdict: str
    findings: tuple[Finding, ...] = ()
    checked: int = 0
    detail: str = ""

    @property
    def blocking(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.blocking)

    def to_dict(self) -> dict:
        return {"strategy": self.strategy, "verdict": self.verdict,
                "checked": self.checked, "detail": self.detail,
                "findings": [f.to_dict() for f in self.findings]}


class Strategy:
    """One way of deciding whether a reply obeys the rules."""
    name = "strategy"

    def check(self, facts: ResponseFacts) -> Opinion:  # pragma: no cover
        raise NotImplementedError


class GuardrailStrategy(Strategy):
    """The primary: the guardrail's three-stage predicate pipeline."""
    name = "guardrail"

    def __init__(self, guardrail: Guardrail):
        self.guardrail = guardrail

    def check(self, facts: ResponseFacts) -> Opinion:
        result: PipelineResult = self.guardrail.verify_response(facts)
        findings = tuple(
            Finding(self.name, v.policy_id, v.why, v.evidence, v.blocking)
            for v in result.violations)
        verdict = FAIL if result.violations else PASS
        return Opinion(self.name, verdict, findings,
                       checked=len(result.stages), detail="")


# The second strategy's own reading of the rules. These patterns look at
# the reply's surface rather than at the predicates, which is the point:
# an independent route to the same question.
_CLAIM = re.compile(
    r"\b(?:all (?:tests?|checks?) pass(?:ed|ing)?|tests? pass(?:ed|ing)?|"
    r"everything (?:works|passes)|it works now|fixed(?: it)?|done|"
    r"succeed(?:s|ed)|verified|confirmed|green)\b", re.I)
_EVIDENCE = re.compile(
    r"\b(?:ran|output|exit code|stdout|stderr|traceback|\d+ (?:passed|failed)|"
    r"OK\b|FAILED\b|line \d+|:\d+\b)", re.I)
_HEDGE = re.compile(
    r"\b(?:should|probably|likely|might|may|appears?|seems?|i think|"
    r"believe|expect|assum\w+|presumabl\w+|cannot verify|did not run|"
    r"untested|not tested)\b", re.I)
_CERTAIN = re.compile(
    r"\b(?:definitely|certainly|guaranteed?|always works|never fails|"
    r"100%|without a doubt)\b", re.I)


class IndependentStrategy(Strategy):
    """The second opinion: the rule text against the reply's surface.

    Reads each compiled rule's own words and asks a question shaped by
    its modality, rather than looking up a predicate. Where it cannot
    decide it says `unsure` -- which never counts as a pass, because a
    verifier that resolves its own ignorance in favour of release is not
    a verifier.
    """
    name = "independent"

    def __init__(self, guardrail: Guardrail):
        self.guardrail = guardrail

    def _policies(self):
        return self.guardrail.policies()

    def check(self, facts: ResponseFacts) -> Opinion:
        text = facts.text or ""
        findings: list[Finding] = []
        checked = 0
        undecidable = 0

        for policy in self._policies():
            rule = policy.rule if isinstance(policy.rule, dict) else {}
            modality = str(rule.get("modality", ""))
            rule_text = str(rule.get("text", ""))
            if not rule_text:
                continue
            checked += 1

            if modality in ("MUST_NOT", "SHOULD_NOT"):
                hit = self._prohibited(rule_text, text)
                if hit:
                    findings.append(Finding(
                        self.name, policy.policy_id,
                        f"the reply does what this rule prohibits: "
                        f"{rule_text[:90]}", hit,
                        blocking=modality == "MUST_NOT"))
                continue

            if "verif" in rule_text.lower() or "test" in rule_text.lower() \
                    or "evidence" in rule_text.lower():
                problem = self._unbacked_claim(text)
                if problem:
                    findings.append(Finding(
                        self.name, policy.policy_id,
                        "a success is claimed with nothing shown that "
                        "would support it", problem))
                continue
            undecidable += 1

        overclaim = self._overclaim(text)
        if overclaim:
            findings.append(Finding(
                self.name, "surface/overclaim",
                "the reply states certainty that nothing in it supports",
                overclaim))

        if findings:
            verdict = FAIL
        elif checked == 0:
            verdict = UNSURE
        else:
            verdict = PASS
        return Opinion(self.name, verdict, tuple(findings), checked,
                       f"{undecidable} rule(s) this strategy cannot decide")

    @staticmethod
    def _prohibited(rule_text: str, text: str) -> str:
        """Surface evidence that a prohibition was ignored."""
        lowered = rule_text.lower()
        for phrase, pattern in (
                ("fabricat", r"\b(?:i (?:assume|guess)|probably returns|"
                            r"should be something like)\b"),
                ("guess", r"\b(?:i (?:assume|guess)|my guess)\b"),
                ("invent", r"\b(?:i (?:assume|guess)|made up)\b")):
            if phrase in lowered:
                m = re.search(pattern, text, re.I)
                if m:
                    return m.group(0)
        return ""

    @staticmethod
    def _unbacked_claim(text: str) -> str:
        claim = _CLAIM.search(text)
        if not claim:
            return ""
        if _EVIDENCE.search(text) or _HEDGE.search(text):
            return ""
        return claim.group(0)

    @staticmethod
    def _overclaim(text: str) -> str:
        m = _CERTAIN.search(text)
        if m and not _EVIDENCE.search(text):
            return m.group(0)
        return ""


class ModelStrategy(Strategy):
    """A third opinion from a model, where one is available.

    The callable is handed the reply and the rules and returns
    `(verdict, [(rule_id, why)])`. Anything it raises, or any verdict it
    returns that is not in the vocabulary, becomes `unsure` -- which
    never counts as a pass. A verifier that fails open is not one.
    """
    name = "model"

    def __init__(self, ask: Callable[[str, tuple], tuple],
                 guardrail: Guardrail | None = None, name: str = "model"):
        self.ask = ask
        self.guardrail = guardrail
        self.name = name

    def check(self, facts: ResponseFacts) -> Opinion:
        rules = tuple(str((p.rule or {}).get("text", ""))
                      for p in (self.guardrail.policies()
                                if self.guardrail else ()))
        try:
            verdict, raw = self.ask(facts.text or "", rules)
        except Exception as exc:
            return Opinion(self.name, UNSURE, (), 0,
                           f"the model check failed: "
                           f"{type(exc).__name__}: {exc}")
        if verdict not in VERDICTS:
            return Opinion(self.name, UNSURE, (), 0,
                           f"returned {verdict!r}, which is not a verdict")
        findings = tuple(Finding(self.name, str(rid), str(why))
                         for rid, why in (raw or ()))
        return Opinion(self.name, verdict, findings, len(rules))


@dataclass(frozen=True)
class Disagreement:
    """Two strategies, two answers, and the question between them."""
    question: str
    opinions: tuple[Opinion, ...]

    @property
    def strategies(self) -> tuple[str, ...]:
        return tuple(o.strategy for o in self.opinions)

    def to_dict(self) -> dict:
        return {"question": self.question,
                "opinions": [o.to_dict() for o in self.opinions]}

    def format(self) -> str:
        lines = [f"DISAGREEMENT — {self.question}"]
        for o in self.opinions:
            lines.append(f"  {o.strategy}: {o.verdict}"
                         + (f" ({len(o.findings)} finding(s))"
                            if o.findings else ""))
            for f in o.findings[:3]:
                lines.append(f"      {f.why}")
        return "\n".join(lines)


@dataclass
class Audit:
    """The consensus verdict on one reply."""
    outcome: str
    opinions: tuple[Opinion, ...] = ()
    disagreement: Disagreement | None = None
    resolution: "Resolution | None" = None

    @property
    def released(self) -> bool:
        """Whether this reply may reach the user.

        A held audit is not released. The only way out of HOLD is a
        recorded resolution, so nothing is ever released by silence.
        """
        if self.outcome == RELEASE:
            return True
        if self.outcome == HOLD and self.resolution is not None:
            return self.resolution.release
        return False

    @property
    def findings(self) -> tuple[Finding, ...]:
        return tuple(f for o in self.opinions for f in o.findings)

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "released": self.released,
                "opinions": [o.to_dict() for o in self.opinions],
                "disagreement": (self.disagreement.to_dict()
                                 if self.disagreement else None),
                "resolution": (self.resolution.to_dict()
                               if self.resolution else None)}

    def format(self) -> str:
        head = f"CONSENSUS {self.outcome.upper()}"
        lines = [head + ("" if self.released else " — not released")]
        for o in self.opinions:
            lines.append(f"  {o.strategy}: {o.verdict} "
                         f"({len(o.findings)} finding(s))")
        if self.disagreement is not None:
            lines.append(self.disagreement.format())
        if self.resolution is not None:
            lines.append(f"  resolved by {self.resolution.by}: "
                         f"{'release' if self.resolution.release else 'block'}"
                         f" — {self.resolution.why}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Resolution:
    """How a disagreement was settled, and by what."""
    by: str
    release: bool
    why: str

    def to_dict(self) -> dict:
        return {"by": self.by, "release": self.release, "why": self.why}


CONSERVATIVE = Resolution(
    "the conservative rule", False,
    "the strategies disagreed and nothing resolved it, so the stricter "
    "reading stands")


class ConsensusAuditor:
    """Runs every strategy and refuses to break a tie by itself."""

    def __init__(self, strategies: tuple[Strategy, ...], log=None):
        if len(strategies) < 2:
            raise ValueError("consensus needs at least two strategies")
        names = [s.name for s in strategies]
        if len(names) != len(set(names)):
            raise ValueError(f"strategies must have distinct names: {names}")
        self.strategies = tuple(strategies)
        self.log = log

    def _emit(self, event: str, data: dict) -> None:
        if self.log is None:
            return
        try:
            self.log.append(event, data, actor="consensus")
        except Exception:
            pass

    def audit(self, facts: ResponseFacts) -> Audit:
        opinions = []
        for strategy in self.strategies:
            try:
                opinions.append(strategy.check(facts))
            except Exception as exc:
                # A strategy that crashes has not approved anything.
                opinions.append(Opinion(
                    strategy.name, UNSURE, (), 0,
                    f"the strategy raised {type(exc).__name__}: {exc}"))
        opinions = tuple(opinions)
        verdicts = {o.verdict for o in opinions}

        if verdicts == {PASS}:
            audit = Audit(RELEASE, opinions)
        elif FAIL in verdicts and PASS not in verdicts:
            audit = Audit(BLOCK, opinions)
        else:
            # Either they disagree outright, or one is unsure -- and an
            # unsure verifier beside a passing one is exactly the case
            # where releasing on the pass would be releasing on silence.
            audit = Audit(HOLD, opinions, Disagreement(
                self._question(opinions), opinions))
        self._emit("consensus.audit", audit.to_dict())
        return audit

    @staticmethod
    def _question(opinions: tuple[Opinion, ...]) -> str:
        said = ", ".join(f"{o.strategy} says {o.verdict}" for o in opinions)
        objections = [f.why for o in opinions for f in o.findings]
        if objections:
            return f"{said}. The objection to settle: {objections[0]}"
        return (f"{said}. Nothing was objected to, so the question is why "
                f"one strategy could not decide.")

    def resolve(self, audit: Audit, resolution: Resolution | None = None
                ) -> Audit:
        """Settle a held audit. Never happens by itself.

        Called with no resolution, the conservative one is recorded --
        the reply stays blocked, and the record says that is why, rather
        than leaving a hold that quietly became a release.
        """
        if audit.outcome != HOLD:
            raise ValueError("only a held audit needs resolving")
        audit.resolution = resolution or CONSERVATIVE
        self._emit("consensus.resolved", audit.to_dict())
        return audit

    def re_reason(self, audit: Audit) -> str:
        """The structured question a regeneration gets.

        It states the disagreement and asks for the evidence that would
        settle it. It never tells the model which side to take: a
        re-reasoning prompt that leads the witness is an override with
        extra steps.
        """
        if audit.disagreement is None:
            return ""
        lines = ["Two independent checks of the reply above disagreed.",
                 ""]
        for o in audit.disagreement.opinions:
            lines.append(f"- The {o.strategy} check concluded "
                         f"{o.verdict}."
                         + (f" It objected: "
                            f"{'; '.join(f.why for f in o.findings[:2])}"
                            if o.findings else ""))
        lines += ["",
                  "Do not argue for either conclusion. Revise the reply so "
                  "that the question does not arise: state only what the "
                  "session's own output supports, and show the evidence "
                  "for anything claimed."]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    from .constitution import ConstitutionalCore
    from .guardrail import VERIFY, Guardrail
    from .kernel import EventLog

    work = Path(tempfile.mkdtemp(prefix="fa-consensus-"))
    log = EventLog(path=str(work / "events.jsonl"))

    PROMPT = ("## Rules\n"
              "- You MUST verify a change by running its tests before "
              "claiming it works.\n"
              "- You must NEVER fabricate a command's output.\n"
              "- You MUST read a file before editing it.\n")
    core = ConstitutionalCore(log, app_dir=work)
    constitution = core.ratify_prompt("consensus-test", PROMPT)
    guard = Guardrail(constitution, log=log, level=VERIFY)

    primary = GuardrailStrategy(guard)
    second = IndependentStrategy(guard)
    auditor = ConsensusAuditor((primary, second), log=log)

    def facts(text, **kw):
        return ResponseFacts(text=text, root=work, **kw)

    # A reply that cites a path is checked against a real tree, so the
    # fixture builds the file it cites rather than asking the checkers to
    # look the other way.
    (work / "fullagent").mkdir(parents=True, exist_ok=True)
    (work / "fullagent" / "tools.py").write_text("# the cited file\n")

    # --- a modest, evidenced reply is released ------------------------
    # The facts are what make it evidenced: a command actually ran and a
    # verdict actually passed. Without those the reply is an unbacked
    # claim, and both strategies are right to say so.
    good = auditor.audit(facts(
        "I ran the suite: 303 passed in 12.0s. The parser change is in "
        "fullagent/tools.py:118.",
        user_text="run the suite",
        tools_called=("run_command",),
        verdicts_passed=1))
    assert good.outcome == RELEASE and good.released, good.format()

    # ...and the same sentence without the facts behind it is not.
    unevidenced = auditor.audit(facts(
        "I ran the suite: 303 passed in 12.0s.",
        user_text="run the suite"))
    assert not unevidenced.released, unevidenced.format()

    # --- an unbacked success claim is caught by the second strategy ---
    bare = auditor.audit(facts("Fixed it. Everything works now."))
    assert bare.outcome in (BLOCK, HOLD), bare.format()
    assert not bare.released, "an unbacked claim must not be released"
    assert any(f.strategy == "independent" for f in bare.findings), \
        bare.format()

    # --- a disagreement is held, not decided --------------------------
    class AlwaysPasses(Strategy):
        name = "always-passes"

        def check(self, f):
            return Opinion(self.name, PASS, (), 1)

    class AlwaysFails(Strategy):
        name = "always-fails"

        def check(self, f):
            return Opinion(self.name, FAIL,
                           (Finding(self.name, "r1", "it objects"),), 1)

    split = ConsensusAuditor((AlwaysPasses(), AlwaysFails()), log=log)
    held = split.audit(facts("anything at all"))
    assert held.outcome == HOLD, held.format()
    assert not held.released, "a held reply must not reach the user"
    assert held.disagreement is not None
    assert set(held.disagreement.strategies) == {"always-passes",
                                                 "always-fails"}
    assert "it objects" in held.disagreement.question, held.format()

    # holding stays holding until something records a resolution
    still_held = split.audit(facts("anything at all"))
    assert not still_held.released

    settled = split.resolve(held)
    assert settled.resolution is CONSERVATIVE
    assert not settled.released, "the conservative resolution blocks"

    overruled = split.resolve(split.audit(facts("x")),
                              Resolution("the operator", True,
                                         "checked by hand"))
    assert overruled.released and overruled.resolution.by == "the operator"
    assert "the operator" in overruled.format()

    try:
        split.resolve(good)
        raise AssertionError("only a held audit needs resolving")
    except ValueError:
        pass

    # --- unsure never counts as a pass --------------------------------
    class Unsure(Strategy):
        name = "unsure"

        def check(self, f):
            return Opinion(self.name, UNSURE, (), 0, "could not tell")

    doubtful = ConsensusAuditor((AlwaysPasses(), Unsure()), log=log)
    uncertain = doubtful.audit(facts("x"))
    assert uncertain.outcome == HOLD and not uncertain.released, \
        "a pass beside an unsure is a release on silence"

    # --- a strategy that crashes is unsure, never a pass --------------
    class Explodes(Strategy):
        name = "explodes"

        def check(self, f):
            raise RuntimeError("the strategy is broken")

    broken = ConsensusAuditor((AlwaysPasses(), Explodes()), log=log)
    crashed = broken.audit(facts("x"))
    assert crashed.outcome == HOLD and not crashed.released
    assert any(o.verdict == UNSURE and "broken" in o.detail
               for o in crashed.opinions), crashed.format()

    # --- two failures block outright, no hold needed ------------------
    both_fail = ConsensusAuditor((AlwaysFails(),
                                  ModelStrategy(lambda t, r: (FAIL,
                                                              [("r", "no")]),
                                                guard)), log=log)
    blocked = both_fail.audit(facts("x"))
    assert blocked.outcome == BLOCK and not blocked.released

    # --- the model strategy fails closed ------------------------------
    def raises(text, rules):
        raise ConnectionError("no network")

    offline = ModelStrategy(raises, guard).check(facts("x"))
    assert offline.verdict == UNSURE and "failed" in offline.detail

    nonsense = ModelStrategy(lambda t, r: ("looks fine", []),
                             guard).check(facts("x"))
    assert nonsense.verdict == UNSURE, nonsense.to_dict()

    working = ModelStrategy(lambda t, r: (PASS, []), guard).check(facts("x"))
    assert working.verdict == PASS and working.checked == len(guard.policies())

    # --- the re-reasoning prompt states the question, not the answer --
    question = split.re_reason(held)
    assert "disagreed" in question and "it objects" in question
    for leading in ("you must pass", "agree with", "the correct verdict",
                    "ignore the"):
        assert leading not in question.lower(), question

    # --- an auditor needs at least two distinct strategies ------------
    for bad in ((primary,), (primary, primary)):
        try:
            ConsensusAuditor(bad)
            raise AssertionError("must refuse a degenerate auditor")
        except ValueError:
            pass

    kinds = {e.type for e in log.events()}
    assert "consensus.audit" in kinds and "consensus.resolved" in kinds

    print(bare.format())
    print(held.format())
    print(f"CONSENSUS SELF-TEST PASS — {len(auditor.strategies)} strategies, "
          f"{len(OUTCOMES)} outcomes, nothing released by silence")

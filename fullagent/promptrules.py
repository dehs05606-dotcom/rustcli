"""System Prompt Enforcement Layer — the prompt as a machine-readable contract.

Everything else in this codebase treats the system prompt as prose: it is
sealed (`systemprompt.py`), placed (`PromptGate`), made searchable
(`spec.py`), audited as a document (`promptaudit.py`), and measured after
the fact (`adherence.py`). Nothing has ever turned it into something a
program can *decide against* before the model acts.

That is this module. `compile_prompt()` reads the author's own text and
produces a `PromptContract`: an ordered set of `Rule` objects, each with a
priority, a modality (MUST / MUST NOT / SHOULD / …), a kind, the tools it
scopes to, and — where one exists — the id of a deterministic predicate
that can actually check it.

Two design commitments hold this honest, and both matter more than the
feature list:

1. **Nothing is invented.** A rule's text is the author's sentence,
   verbatim, with its section path and character offset. The compiler
   assigns structure; it never assigns meaning the prompt does not carry.
   `compile_prompt()` on the same text always yields the same contract
   (`fingerprint` proves it), so a contract can be diffed across prompt
   edits like any other build artifact.

2. **Coverage is reported, not implied.** Most sentences in a 49k prompt
   are not machine-checkable, and a layer that pretended otherwise would
   be worse than none — it would green-light a turn it never inspected.
   Every rule that maps to no predicate is compiled as `advisory`, and
   `PromptContract.coverage()` states the enforceable fraction out loud.

The compiler is pure text processing: no model call, no network, no I/O.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .spec import slugify, split_sections, terms

# Priority bands. Lower is stronger; the guardrail blocks on P0/P1 and
# only advises on the rest, so this ordering is load-bearing.
P_CRITICAL = 0     # prohibitions with teeth: safety, honesty, destruction
P_REQUIRED = 1     # plain MUST / MUST NOT
P_EXPECTED = 2     # SHOULD / SHOULD NOT
P_OPTIONAL = 3     # MAY, and everything informational

PRIORITY_NAMES = {P_CRITICAL: "CRITICAL", P_REQUIRED: "REQUIRED",
                  P_EXPECTED: "EXPECTED", P_OPTIONAL: "OPTIONAL"}

# Modality detection, most specific pattern first. Order is the whole
# algorithm here: "must not" has to be seen before "must", or every
# prohibition in the prompt compiles as its own opposite.
_MODALITY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("MUST_NOT", r"\b(?:must\s+(?:not|never)|shall\s+not|may\s+not|"
                 r"never|do\s+not|don't|cannot|can't|refuse\s+to)\b"),
    ("SHOULD_NOT", r"\b(?:should\s+(?:not|never)|shouldn't|avoid|"
                   r"rather\s+than|instead\s+of)\b"),
    ("MUST", r"\b(?:must|always|ensure|required|require|shall|"
             r"has\s+to|have\s+to|need\s+to)\b"),
    ("SHOULD", r"\b(?:should|prefer|prefers|preferably|ought\s+to)\b"),
    ("MAY", r"\b(?:may|can|optionally|feel\s+free)\b"),
)

# A hand-written prompt gives most of its instructions with no modal verb
# at all — "Read the codebase before making changes.", "Use `edit_file`
# for precise replacements." Reading only modals threw those away, and
# they are the majority of the Tool Usage section. An imperative is a
# directive; it is simply a quieter one, so it lands a band lower than a
# spelled-out MUST unless the author raised their voice.
_IMPERATIVE_VERBS = frozenset("""
read write edit run use call prefer keep make check verify confirm cite
ask tell report state give split treat follow respect start stop avoid
inspect search list open close apply reach wait return record explain
""".split())

_LEAD_RE = re.compile(r"^[\s*_`>#-]*([a-z]+)", re.IGNORECASE)


def _imperative(text: str) -> bool:
    """Whether the sentence opens with a bare instruction to the reader."""
    m = _LEAD_RE.match(text)
    if not m:
        return False
    return m.group(1).lower() in _IMPERATIVE_VERBS

# Words that turn a prohibition into a critical one. These are the things
# that cannot be undone by a later turn: destroyed state, invented facts,
# leaked secrets.
_CRITICAL_TERMS = frozenset("""
fabricate fabricated fabrication invent invented invention hallucinate
delete deletes deleting destroy destroys destructive irreversible
overwrite secret secrets credential credentials token password key
force-push rm exfiltrate unauthorized
""".split())

# A rule is about the reply itself when it speaks about the reply itself.
_OUTPUT_TERMS = frozenset("""
reply replies respond response answer answers output outputs say says
said tell tells claim claims claiming report reports message format
formatted sentence sentences word words cite cites citation citations
""".split())

_TOOL_RE = re.compile(r"`([a-z_][a-z0-9_]{2,})`")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_EMPHASIS_RE = re.compile(r"\*\*[^*]+\*\*|__[^_]+__")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z`*\"'])")


@dataclass(frozen=True)
class Predicate:
    """A deterministic check a rule can be decided by.

    `words` is the semantic signature in plain English; it is stemmed
    through `spec.terms` — the very function the rules themselves go
    through — so the two sides can never drift. Writing the stems by hand
    is exactly how this went wrong first: "destructive" stems to
    "destructiv", not "destruct", and every safety rule in the prompt
    silently bound to no predicate at all while the contract reported
    itself healthy.

    Matching is a set overlap and nothing cleverer, which is why the
    library below is small and explicit rather than general — a fuzzy
    match that bound the wrong predicate to a rule would make the
    guardrail block correct work, and that is the one failure mode this
    layer must not have.
    """
    id: str
    words: str
    threshold: int
    description: str

    @property
    def terms(self) -> frozenset[str]:
        return _predicate_terms(self.id, self.words)


_PRED_TERM_CACHE: dict[str, frozenset[str]] = {}


def _predicate_terms(pred_id: str, words: str) -> frozenset[str]:
    cached = _PRED_TERM_CACHE.get(pred_id)
    if cached is None:
        cached = frozenset(terms(words))
        _PRED_TERM_CACHE[pred_id] = cached
    return cached


# The predicate library. Every entry here is implemented in guardrail.py;
# a rule that binds to none of them is advisory and says so. Signatures
# carry several surface forms on purpose — the stemmer keeps "verify" and
# "verified" apart, so a signature with only one of them matches half the
# sentences it should.
PREDICATES: tuple[Predicate, ...] = (
    Predicate("no-unverified-success",
              "verify verified verifying success succeeded evidence "
              "claim claims tests exit codes confirm confirmed check", 2,
              "a success claim in the reply needs evidence in the turn"),
    Predicate("read-before-write",
              "read reading inspect inspected edit editing edits file "
              "files before changes changing", 3,
              "a file is read before it is edited"),
    Predicate("no-fabrication",
              "fabricate fabricated invent invented real cite sources "
              "paths information", 2,
              "cited paths and sources must exist"),
    Predicate("ask-before-irreversible",
              "ask asking irreversible delete deletes destructive "
              "permission approval approve confirm explicit", 2,
              "an irreversible tool call needs approval"),
    Predicate("minimal-change",
              "minimal surgical small rewrite rewrites prefer changes", 3,
              "prefer narrow edits over whole-file rewrites"),
    Predicate("understand-before-change",
              "understand assume assumption read first codebase", 3,
              "inspect before mutating"),
    Predicate("honest-uncertainty",
              "honest honestly uncertainty uncertain know investigate "
              "unsure", 2,
              "say so rather than guessing"),
)


@dataclass(frozen=True)
class Rule:
    """One compiled directive: the author's sentence, made decidable."""
    id: str
    text: str
    section: str
    path: tuple[str, ...]
    modality: str
    priority: int
    kind: str                      # hard_constraint | output_contract | directive
    tools: tuple[str, ...] = ()
    predicate: str = ""            # "" means advisory: compiled, not checkable
    offset: int = 0
    terms: frozenset[str] = field(default_factory=frozenset)

    @property
    def enforceable(self) -> bool:
        return bool(self.predicate)

    @property
    def prohibitive(self) -> bool:
        return self.modality in ("MUST_NOT", "SHOULD_NOT")

    @property
    def blocking(self) -> bool:
        """Whether a violation of this rule should stop an action.

        Only the two strong bands block. An EXPECTED rule that stopped a
        tool call would make the agent unusable on the first prompt that
        said "prefer" about anything.
        """
        return self.priority <= P_REQUIRED and self.enforceable

    def to_dict(self) -> dict:
        return {"id": self.id, "text": self.text, "section": self.section,
                "modality": self.modality, "priority": self.priority,
                "kind": self.kind, "tools": list(self.tools),
                "predicate": self.predicate, "offset": self.offset}


# A negation sitting inside a subordinate clause describes the world; it
# does not instruct the reader. "parts that do not depend on each other"
# is a definition of independent work, not a prohibition, and compiling it
# as one put three descriptive sentences of the shipped prompt into the
# blocking band.
_SUBORDINATORS = frozenset("""
that which who whom whose when whenever if because since although though
while unless where whereas they it these those what
""".split())

# "do not need to" / "do not have to" grant permission with the grammar of
# a prohibition. Reading them as MUST NOT inverts their meaning exactly.
_PERMISSIVE_AFTER = frozenset({"need", "have", "want"})


def _directed(text: str, start: int, end: int) -> bool:
    """Whether a negation at [start:end) is aimed at the reader."""
    before = re.findall(r"[a-z']+", text[:start].lower())
    if before and before[-1] in _SUBORDINATORS:
        return False
    after = re.findall(r"[a-z']+", text[end:].lower())
    if after and after[0] in _PERMISSIVE_AFTER:
        return False
    return True


def _modality(text: str) -> str:
    """The strongest modality the sentence carries, or '' for none.

    Prohibitions are checked for direction before they are accepted; when
    one turns out to be descriptive the scan falls through to the weaker
    patterns rather than returning, so "prefer X over Y that does not Z"
    still compiles as the SHOULD it is.

    A question is never a rule, whatever modal it contains. "What should
    the agent do here?" is the prompt thinking aloud, and compiling it
    would put a rule with no instruction in it into the contract.
    """
    if text.rstrip().endswith("?"):
        return ""
    low = text.lower()
    for name, pattern in _MODALITY_PATTERNS:
        for m in re.finditer(pattern, low):
            if name.endswith("_NOT") and not _directed(low, m.start(), m.end()):
                continue
            return name
    return "IMPERATIVE" if _imperative(text) else ""


def _priority(text: str, modality: str, rule_terms: frozenset[str]) -> int:
    """Where the rule sits in the band order.

    Emphasis counts, because the author used it to mean something: a bold
    or shouted prohibition is the prompt's own signal that this one is not
    negotiable. So does subject matter — a prohibition about deleting or
    fabricating is critical even written flatly.
    """
    if modality in ("MUST_NOT", "MUST"):
        shouted = bool(_EMPHASIS_RE.search(text)) or _has_shout(text)
        touches_critical = bool(rule_terms & _critical_stems())
        if modality == "MUST_NOT" and (shouted or touches_critical):
            return P_CRITICAL
        if modality == "MUST" and shouted and touches_critical:
            return P_CRITICAL
        return P_REQUIRED
    if modality == "IMPERATIVE":
        shouted = bool(_EMPHASIS_RE.search(text)) or _has_shout(text)
        return P_REQUIRED if shouted else P_EXPECTED
    if modality in ("SHOULD", "SHOULD_NOT"):
        return P_EXPECTED
    return P_OPTIONAL


def _has_shout(text: str) -> bool:
    """A word of four or more capitals — the prompt raising its voice."""
    for word in re.findall(r"\b[A-Z]{4,}\b", text):
        if word not in ("HTTP", "HTTPS", "JSON", "YAML", "HTML", "TODO"):
            return True
    return False


_CRITICAL_STEMS: frozenset[str] | None = None


def _critical_stems() -> frozenset[str]:
    global _CRITICAL_STEMS
    if _CRITICAL_STEMS is None:
        _CRITICAL_STEMS = frozenset(terms(" ".join(_CRITICAL_TERMS)))
    return _CRITICAL_STEMS


_OUTPUT_STEMS: frozenset[str] | None = None


def _output_stems() -> frozenset[str]:
    global _OUTPUT_STEMS
    if _OUTPUT_STEMS is None:
        _OUTPUT_STEMS = frozenset(terms(" ".join(_OUTPUT_TERMS)))
    return _OUTPUT_STEMS


def _kind(modality: str, priority: int, rule_terms: frozenset[str]) -> str:
    if rule_terms & _output_stems():
        return "output_contract"
    if priority <= P_REQUIRED and modality in ("MUST_NOT", "MUST"):
        return "hard_constraint"
    return "directive"


def _bind_predicate(rule_terms: frozenset[str]) -> str:
    """The predicate this rule compiles to, or '' when it is advisory.

    Best overlap wins, and ties break on the predicate id so the result is
    stable across runs — a contract that reordered itself between two
    compiles of the same text would make the fingerprint worthless.
    """
    best, best_score = "", 0
    for pred in PREDICATES:
        score = len(rule_terms & pred.terms)
        if score < pred.threshold:
            continue
        if score > best_score or (score == best_score and pred.id < best):
            best, best_score = pred.id, score
    return best


def _statements(body: str) -> list[tuple[str, int]]:
    """The candidate directives in one section body, with their offsets.

    List items are statements on their own; prose is cut into sentences.
    Fenced code is skipped entirely — an example of a shell command is not
    a rule about anything, and compiling one would put noise into a
    contract whose whole value is that it can be trusted.
    """
    out: list[tuple[str, int]] = []
    offset = 0
    in_fence = False
    for line in body.splitlines(keepends=True):
        raw = line.rstrip("\n")
        stripped = raw.strip()
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            offset += len(line)
            continue
        if in_fence or not stripped or stripped.startswith("#"):
            offset += len(line)
            continue
        # Bullets are sentence-split like prose. A long bullet routinely
        # carries several directives ("Reach for it whenever … Two
        # subagents must never be given the same file"), and taking it
        # whole dilutes its terms until no predicate matches and buries
        # the strong sentence under the weak one.
        marker = _BULLET_RE.match(stripped)
        item = _BULLET_RE.sub("", stripped) if marker else stripped
        pos = offset + (marker.end() if marker else 0)
        for sentence in _SENTENCE_SPLIT_RE.split(item):
            if sentence.strip():
                out.append((sentence.strip(), pos))
            pos += len(sentence) + 1
        offset += len(line)
    return out


@dataclass(frozen=True)
class PromptContract:
    """A compiled prompt: what it demands, in a form a program can use."""
    name: str
    fingerprint: str
    rules: tuple[Rule, ...]
    sections: int
    chars: int

    def rule(self, rule_id: str) -> Rule | None:
        for r in self.rules:
            if r.id == rule_id:
                return r
        return None

    def by_priority(self, priority: int) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.priority == priority)

    def hard_constraints(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.kind == "hard_constraint")

    def output_contract(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.kind == "output_contract")

    def enforceable(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.enforceable)

    def blocking(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.blocking)

    def for_tool(self, tool_name: str) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if tool_name in r.tools)

    def for_predicate(self, predicate_id: str) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.predicate == predicate_id)

    def coverage(self) -> float:
        """The fraction of compiled rules a predicate can actually decide.

        This is the number that keeps the layer honest. A contract at 0.18
        coverage is not a failure — it is a 49k prompt, most of which is
        guidance no predicate could ever check — but a caller that treats
        a clean guardrail pass as "the prompt was followed" is wrong, and
        this is where it finds that out.
        """
        if not self.rules:
            return 0.0
        return round(len(self.enforceable()) / len(self.rules), 4)

    def to_dict(self) -> dict:
        return {"name": self.name, "fingerprint": self.fingerprint,
                "sections": self.sections, "chars": self.chars,
                "rules": [r.to_dict() for r in self.rules],
                "coverage": self.coverage()}

    def format_summary(self) -> str:
        lines = [f"PROMPT CONTRACT — {self.name}  ({self.fingerprint[:12]})",
                 f"  {self.chars:,} chars · {self.sections} sections · "
                 f"{len(self.rules)} rules"]
        for band in (P_CRITICAL, P_REQUIRED, P_EXPECTED, P_OPTIONAL):
            got = self.by_priority(band)
            if got:
                lines.append(f"  {PRIORITY_NAMES[band]:<9} {len(got):>4}")
        lines.append(f"  hard constraints {len(self.hard_constraints()):>4}")
        lines.append(f"  output contract  {len(self.output_contract()):>4}")
        lines.append(f"  enforceable      {len(self.enforceable()):>4}"
                     f"  ({self.coverage() * 100:.1f}% coverage)")
        return "\n".join(lines)


def compile_prompt(name: str, text: str,
                   tool_names: frozenset[str] | None = None) -> PromptContract:
    """Compile prompt text into a contract. Pure, deterministic, offline.

    `tool_names`, when given, restricts tool scoping to real tools — a
    prompt full of backticked identifiers would otherwise scope rules to
    things that are not tools at all.
    """
    rules: list[Rule] = []
    sections = split_sections(text)
    seen_ids: dict[str, int] = {}
    for section in sections:
        for statement, offset in _statements(section.body):
            modality = _modality(statement)
            if not modality:
                continue
            rule_terms = frozenset(terms(statement))
            priority = _priority(statement, modality, rule_terms)
            mentioned = tuple(sorted({
                t for t in _TOOL_RE.findall(statement)
                if tool_names is None or t in tool_names}))
            base = f"{section.id}.{slugify(statement)[:40] or 'rule'}"
            seen_ids[base] = seen_ids.get(base, 0) + 1
            rid = base if seen_ids[base] == 1 else f"{base}-{seen_ids[base]}"
            rules.append(Rule(
                id=rid, text=statement, section=section.id,
                path=section.path, modality=modality, priority=priority,
                kind=_kind(modality, priority, rule_terms),
                tools=mentioned, predicate=_bind_predicate(rule_terms),
                offset=section.start + offset, terms=rule_terms))
    rules.sort(key=lambda r: (r.priority, r.offset))
    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return PromptContract(name=name, fingerprint=fingerprint,
                          rules=tuple(rules), sections=len(sections),
                          chars=len(text))


def contract_delta(old: PromptContract, new: PromptContract) -> dict:
    """What changed between two compiled prompts.

    Prompt edits ship blind today. With this, a prompt change can be read
    the way a code change is: which rules appeared, which disappeared, and
    whether the enforceable surface grew or shrank.
    """
    old_ids = {r.id for r in old.rules}
    new_ids = {r.id for r in new.rules}
    repriced = [r.id for r in new.rules
                if (o := old.rule(r.id)) is not None
                and (o.priority, o.predicate) != (r.priority, r.predicate)]
    return {"added": sorted(new_ids - old_ids),
            "removed": sorted(old_ids - new_ids),
            "repriced": sorted(repriced),
            "coverage_before": old.coverage(),
            "coverage_after": new.coverage()}


if __name__ == "__main__":
    from . import systemprompt

    # --- modality detection, prohibition before permission ---------------
    assert _modality("You must not delete files") == "MUST_NOT"
    assert _modality("You must read first") == "MUST"
    assert _modality("Never claim success") == "MUST_NOT"
    assert _modality("Prefer small edits") == "SHOULD"
    assert _modality("You may ask") == "MAY"
    assert _modality("The sky is blue") == ""
    assert _modality("Read the codebase before making changes.") == "IMPERATIVE"
    assert _modality("Use `edit_file` for replacements.") == "IMPERATIVE"
    assert _modality("What should I do?") == ""
    # a descriptive negation is not an instruction
    assert _modality("parts that do not depend on each other") != "MUST_NOT"
    # and a permission wearing a prohibition's grammar is not one either
    assert _modality("You do not need to hold back.") != "MUST_NOT"

    # --- priority banding ------------------------------------------------
    t1 = frozenset(terms("Never fabricate file paths"))
    assert _priority("Never fabricate file paths", "MUST_NOT", t1) == P_CRITICAL
    t2 = frozenset(terms("You must use tabs"))
    assert _priority("You must use tabs", "MUST", t2) == P_REQUIRED
    t3 = frozenset(terms("Prefer small edits"))
    assert _priority("Prefer small edits", "SHOULD", t3) == P_EXPECTED
    assert _has_shout("This is IMPORTANT") and not _has_shout("Use JSON here")

    # --- fenced code never becomes a rule --------------------------------
    body = "Always read first.\n\n```\nrm -rf / # you must never run this\n```\n"
    got = [s for s, _ in _statements(body)]
    assert any("Always read first" in s for s in got)
    assert not any("rm -rf" in s for s in got), got

    # --- compiling the real prompt ---------------------------------------
    contract = compile_prompt("main", systemprompt.MAIN,
                              tool_names=frozenset({"read_file", "edit_file",
                                                    "write_file",
                                                    "run_command"}))
    assert contract.rules, "the shipped prompt must compile to rules"
    assert contract.fingerprint == hashlib.sha256(
        systemprompt.MAIN.encode()).hexdigest()
    assert contract.hard_constraints(), "MAIN states hard prohibitions"
    assert contract.output_contract(), "MAIN constrains what may be claimed"
    assert contract.enforceable(), "some rules must bind to a predicate"
    assert 0.0 < contract.coverage() < 1.0, contract.coverage()
    # rules arrive strongest-first, and that order is what the guardrail
    # walks — a weak rule reported before a critical one would bury it
    priorities = [r.priority for r in contract.rules]
    assert priorities == sorted(priorities)

    # --- determinism: same text in, same contract out --------------------
    again = compile_prompt("main", systemprompt.MAIN)
    assert [r.id for r in again.rules] == [
        r.id for r in compile_prompt("main", systemprompt.MAIN).rules]
    assert again.fingerprint == contract.fingerprint

    # --- tool scoping is restricted to real tools ------------------------
    scoped = compile_prompt(
        "t", "- You must use `read_file` before `not_a_tool` edits.\n",
        tool_names=frozenset({"read_file"}))
    assert scoped.rules[0].tools == ("read_file",), scoped.rules[0].tools

    # --- advisory rules are compiled, but not pretended to be checkable --
    advisory = compile_prompt("t", "- You must write in a friendly tone.\n")
    assert advisory.rules and not advisory.rules[0].enforceable
    assert not advisory.rules[0].blocking
    assert advisory.coverage() == 0.0

    # --- delta between two prompt versions -------------------------------
    v2 = compile_prompt("main", systemprompt.MAIN + "\n## Extra\n- Never guess.\n")
    delta = contract_delta(contract, v2)
    assert delta["added"] and not delta["removed"], delta

    assert "PROMPT CONTRACT" in contract.format_summary()
    print(contract.format_summary())
    print("PROMPTRULES SELF-TEST PASS")

"""Making a long prompt addressable.

A 4k prompt can be held in mind for a whole turn. A 49k one cannot, and
no amount of placement fixes that: it is seated once at messages[0], and
by iteration 120 the clause that governs the edit the model is about to
make is forty thousand tokens back, competing with the transcript of
everything that happened since. The usual answers are to shout (a
compliance banner), to repeat (a reminder tail), or to trim. The first
two are a second voice telling the model to obey the first. The third
throws away what the author wrote.

There is a fourth answer, and it is the one the rest of this codebase
already uses for large state: make it addressable. A prompt is a
document. Documents get an index. This module builds one — deterministic,
dependency-free, from the sealed text alone:

  split_sections()  cuts the prompt at its own headings, so the units are
                    the author's units, not an arbitrary window.
  PromptIndex       BM25 over those sections, heading terms weighted,
                    with the section's exact text served back verbatim.

The model reaches it through the `spec_lookup` tool — it asks what governs
what it is about to do, and gets back the author's own words, at the
moment they are relevant, at the position where the answer is being
written. Nothing is injected into the conversation: the model chooses to
look, exactly as it chooses to read a file. A prompt it can consult on
demand does not decay with depth, because the lookup happens at the
depth where it matters.

Retrieval is lexical on purpose (rung 1): no embeddings, no network, no
model call. A prompt lookup that could fail, cost money, or vary between
runs would be worse than no lookup at all.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Sectioning — cut the prompt at its own headings
# ---------------------------------------------------------------------------

# A heading is recognised only in forms an author uses deliberately. The
# conservative set matters: a false heading splits a directive in half and
# hands back a fragment that reads like the whole rule.
_ATX = re.compile(r"^(#{1,6})\s+(\S.*?)\s*#*$")
_NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)[.)]\s+(\S.{0,78})$")
_BOLD_ONLY = re.compile(r"^\*\*(\S.{0,78}?)\*\*:?$")
_UPPER = re.compile(r"^([A-Z][A-Z0-9 ,'/&()\-]{2,78})$")
_WORD = re.compile(r"[a-z0-9_]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Common words carry no retrieval signal and would let a one-word query
# match every section equally.
_STOP = frozenset("""
a an and are as at be been but by can cannot do does for from had has have
if in into is it its may must not of on or should so than that the their
them then there these they this to use used using was were what when which
while who will with would you your
""".split())


def _heading_of(line: str) -> tuple[str, int] | None:
    """(*heading text*, depth) if `line` is a heading, else None."""
    m = _ATX.match(line)
    if m:
        return m.group(2), len(m.group(1))
    m = _BOLD_ONLY.match(line)
    if m:
        return m.group(1), 4
    m = _NUMBERED.match(line)
    if m:
        return m.group(2), 2 + m.group(1).count(".")
    m = _UPPER.match(line)
    if m and any(c.isalpha() for c in m.group(1)):
        # an all-caps line is a heading only if it is not a sentence
        if not m.group(1).rstrip().endswith((".", "?", "!")):
            return m.group(1).strip(), 1
    return None


def slugify(text: str) -> str:
    words = _WORD.findall(_CAMEL.sub(" ", text).lower())
    return "-".join(words[:6]) or "section"


@dataclass(frozen=True)
class Section:
    """One addressable piece of a prompt, in the author's own words."""
    id: str
    heading: str
    path: tuple[str, ...]      # enclosing headings, outermost first
    body: str
    start: int                 # character offset in the prompt
    depth: int = 1

    @property
    def chars(self) -> int:
        return len(self.body)

    @property
    def label(self) -> str:
        return " › ".join(self.path) if self.path else self.heading

    def render(self) -> str:
        return f"{self.label}\n{self.body.strip()}"


def split_sections(text: str) -> list[Section]:
    """Cut `text` at its own headings. Always returns at least one section.

    Text before the first heading becomes a `(opening)` section rather
    than being dropped: in a hand-written prompt that opening is usually
    the part that matters most, and a splitter that loses it would make
    the index actively misleading."""
    lines = text.splitlines(keepends=True)
    marks: list[tuple[int, int, str, int]] = []   # line, offset, head, depth
    offset = 0
    for i, line in enumerate(lines):
        found = _heading_of(line.rstrip("\n"))
        if found:
            marks.append((i, offset, found[0], found[1]))
        offset += len(line)

    if not marks or marks[0][0] > 0:
        marks.insert(0, (0, 0, "(opening)", 0))

    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    seen: dict[str, int] = {}
    for n, (line_no, start, heading, depth) in enumerate(marks):
        end_line = marks[n + 1][0] if n + 1 < len(marks) else len(lines)
        body = "".join(lines[line_no:end_line])
        if not body.strip():
            continue
        while stack and stack[-1][0] >= depth:
            stack.pop()
        path = tuple(h for _, h in stack) + (heading,)
        # The synthetic opening is a section, never an ancestor: the text
        # before the first heading does not enclose what follows it.
        if depth:
            stack.append((depth, heading))
        base = slugify(heading)
        seen[base] = seen.get(base, 0) + 1
        sid = base if seen[base] == 1 else f"{base}-{seen[base]}"
        sections.append(Section(id=sid, heading=heading, path=path,
                                body=body, start=start, depth=depth))
    return sections


# ---------------------------------------------------------------------------
# PromptIndex — BM25 over the author's own sections
# ---------------------------------------------------------------------------

_K1 = 1.4
_B = 0.72
# A term in a heading is worth this many occurrences in the body. A
# heading is the author saying what a section is ABOUT, which is exactly
# what a lookup query is asking.
_HEADING_WEIGHT = 3


def stem(word: str) -> str:
    """A deliberately small suffix stemmer.

    It exists to unify the forms an author and a querying model will
    disagree on — "claim"/"claiming"/"claims", "edit"/"editing"/"edited"
    — and nothing more. An aggressive stemmer would start conflating
    words that are genuinely different directives, and a wrong section
    returned with full authority is worse than no section at all."""
    if len(word) > 4:
        if word.endswith("ies"):
            return word[:-3] + "y"
        for suffix in ("ing", "ed"):
            if word.endswith(suffix):
                base = word[:-len(suffix)]
                # A suffix is only a suffix if a word is left when you
                # take it off: "thing" is not "th" + ing, and a stemmer
                # that thinks it is will happily conflate "thing" with
                # "think", "third" and "this".
                if len(base) < 3:
                    return word
                # "running" -> "runn" -> "run", so it meets "run". The
                # l/s/z exception is Porter's and it earns its keep:
                # without it "falling" -> "fal" would stop meeting "fall".
                if base[-1] == base[-2] and base[-1] not in "lsz":
                    base = base[:-1]
                return base
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        # English adds "es" after a sibilant, so "pushes" is "push" + es
        # while "times" is "time" + s. Getting this wrong leaves "push"
        # and "pushes" as different words, which is exactly the pair an
        # audit needs to see through.
        if (word.endswith("es") and len(word) > 4
                and word[:-2].endswith(("s", "x", "z", "ch", "sh"))):
            word = word[:-2]
        else:
            word = word[:-1]
    # Finally, a trailing "e". Applied to every word, this unifies the
    # forms that differ only by it — "include"/"including" both land on
    # "includ", "rule"/"rules" both on "rul" — which is the whole job.
    if len(word) > 3 and word.endswith("e"):
        return word[:-1]
    return word


def terms(text: str) -> list[str]:
    return [stem(w) for w in _WORD.findall(_CAMEL.sub(" ", text).lower())
            if w not in _STOP and len(w) > 1]


@dataclass(frozen=True)
class Hit:
    section: Section
    score: float
    matched: tuple[str, ...] = ()


@dataclass
class PromptIndex:
    """A sealed prompt, cut into sections and made searchable.

    Built from the vault's sealed text, so what a lookup returns is
    byte-for-byte what the model was sent — an index that could drift
    from the prompt would be worse than none."""
    name: str
    text: str
    sections: list[Section] = field(default_factory=list)
    _tf: list[dict[str, int]] = field(default_factory=list, repr=False)
    _df: dict[str, int] = field(default_factory=dict, repr=False)
    _avg: float = 0.0

    @classmethod
    def build(cls, name: str, text: str) -> "PromptIndex":
        idx = cls(name=name, text=text, sections=split_sections(text))
        for sec in idx.sections:
            counts: dict[str, int] = {}
            for t in terms(sec.body):
                counts[t] = counts.get(t, 0) + 1
            for t in terms(" ".join(sec.path)):
                counts[t] = counts.get(t, 0) + _HEADING_WEIGHT
            idx._tf.append(counts)
            for t in counts:
                idx._df[t] = idx._df.get(t, 0) + 1
        lengths = [sum(c.values()) for c in idx._tf]
        idx._avg = (sum(lengths) / len(lengths)) if lengths else 0.0
        return idx

    def by_id(self, section_id: str) -> Section | None:
        for sec in self.sections:
            if sec.id == section_id:
                return sec
        return None

    def lookup(self, query: str, k: int = 3) -> list[Hit]:
        """The k sections that best answer `query`, best first.

        A query that matches nothing returns nothing. Returning the
        "closest" section to a query the prompt never addresses would
        hand the model a rule that does not apply, stated with the same
        authority as one that does — worse than an honest miss."""
        q = terms(query)
        if not q or not self.sections:
            return []
        n = len(self.sections)
        hits: list[Hit] = []
        for i, sec in enumerate(self.sections):
            counts = self._tf[i]
            length = sum(counts.values()) or 1
            score, matched = 0.0, []
            for t in q:
                f = counts.get(t, 0)
                if not f:
                    continue
                matched.append(t)
                df = self._df.get(t, 0)
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                norm = 1 - _B + _B * (length / (self._avg or 1))
                score += idf * (f * (_K1 + 1)) / (f + _K1 * norm)
            if score > 0:
                hits.append(Hit(sec, score, tuple(dict.fromkeys(matched))))
        hits.sort(key=lambda h: (-h.score, h.section.start))
        return hits[:k]

    def format_hits(self, hits: list[Hit], budget: int = 6_000) -> str:
        """The matched sections, verbatim, under a plain header.

        Verbatim is the point. A summary of a directive is a paraphrase of
        a rule, and a paraphrased rule is a different rule."""
        if not hits:
            return ("no section of this prompt addresses that — the prompt "
                    "does not speak to it, so decide it on the general "
                    "directives you already have")
        out, used = [], 0
        for h in hits:
            block = h.section.render()
            if used + len(block) > budget and out:
                out.append(f"(+{len(hits) - len(out)} more section(s) "
                           f"omitted for length)")
                break
            out.append(block)
            used += len(block)
        return "\n\n---\n\n".join(out)

    def stats(self) -> dict:
        bodies = [len(s.body) for s in self.sections]
        return {"sections": len(self.sections),
                "chars": len(self.text),
                "largest": max(bodies) if bodies else 0,
                "median": sorted(bodies)[len(bodies) // 2] if bodies else 0}

    def format_stats(self) -> str:
        s = self.stats()
        return (f"{self.name}: {s['chars']:,} chars in {s['sections']} "
                f"addressable section(s), largest {s['largest']:,} chars, "
                f"median {s['median']:,}")


if __name__ == "__main__":
    def _self_test() -> None:
        doc = """You are a careful engineer. Work from evidence.

# Reading before editing

Never edit a file you have not read in this session. Read it first, in
full if it is short, around the change if it is long.

# Verification

## Running tests

Run the project's own test command. Do not invent one. If the command
fails, say so and show the output.

## Claiming success

Never claim success without evidence. A passing check AFTER the last
edit is evidence; anything else is a hope.

# SECURITY

Never write a credential into a file that is tracked by git.
"""
        idx = PromptIndex.build("t", doc)
        ids = [s.id for s in idx.sections]
        # the text before the first heading is kept, not dropped
        assert ids[0] == "opening", ids
        assert "careful engineer" in idx.sections[0].body
        assert "reading-before-editing" in ids, ids
        assert "security" in ids, ids

        # nesting: a ## under a # carries the enclosing heading
        running = idx.by_id("running-tests")
        assert running is not None
        assert running.path == ("Verification", "Running tests"), running.path
        assert running.label == "Verification › Running tests"
        # and a section's body is its own, not its parent's
        assert "Never claim success" not in running.body

        # sections tile the prompt: every character is in exactly one
        joined = "".join(s.body for s in idx.sections)
        assert joined == doc, (len(joined), len(doc))

        # -- retrieval ---------------------------------------------------
        hits = idx.lookup("am I allowed to claim this succeeded?")
        assert hits and hits[0].section.id == "claiming-success", \
            [(h.section.id, round(h.score, 2)) for h in hits]
        # stemming is what makes that work: the author wrote "claim", the
        # query said "claiming", and an index that treated them as
        # different words would have missed the one rule that applies
        assert stem("claiming") == stem("claims") == "claim"
        assert stem("editing") == stem("edited") == "edit"
        # a doubled consonant is undoubled, so "running" meets "run" —
        # except after l/s/z, where "falling" must stay "fall"
        assert stem("running") == stem("runs") == "run"
        assert stem("stopping") == "stop"
        assert stem("falling") == stem("falls") == "fall"
        # and a suffix is only stripped when a word is left behind
        assert stem("thing") == "thing"
        assert stem("being") == "being"
        # "pushes" is "push" + es, "times" is "time" + s — an audit that
        # cannot see through that pair cannot see a restated rule either
        assert stem("pushes") == stem("push") == "push"
        assert stem("boxes") == stem("box") == "box"
        assert stem("times") == stem("time") == "tim"
        # and the forms that differ only by a trailing "e" land together
        assert stem("include") == stem("including") == "includ"
        assert stem("rule") == stem("rules") == "rul"
        # but only that far — these are different directives, not forms
        assert stem("verification") != stem("verify")

        hits = idx.lookup("about to edit a file I have not read")
        assert hits[0].section.id == "reading-before-editing", \
            [(h.section.id, round(h.score, 2)) for h in hits]
        hits = idx.lookup("credential in a git tracked file")
        assert hits[0].section.id == "security", \
            [(h.section.id, round(h.score, 2)) for h in hits]

        # a miss is an honest miss — never the closest thing on file
        assert idx.lookup("quarterly revenue in euros") == []
        assert "does not speak to it" in idx.format_hits([])
        # and lexical retrieval really does miss when the query shares no
        # vocabulary with the prompt. That is a known limit, not a bug to
        # paper over: the honest empty answer sends the model back to the
        # directives it already has, where a confidently wrong section
        # would have sent it somewhere else entirely.
        assert idx.lookup("is the outcome satisfactory") == []

        # what comes back is the author's words, byte-for-byte
        text = idx.format_hits(idx.lookup("claiming success"))
        assert "A passing check AFTER the last" in text
        assert "Claiming success" in text
        # and it respects a budget rather than blowing the context
        small = idx.format_hits(idx.lookup("test", k=3), budget=20)
        assert "omitted for length" in small or len(small) < 400

        # -- a heading-free prompt still indexes as one section ----------
        flat = PromptIndex.build("flat", "just be helpful, always")
        assert len(flat.sections) == 1
        assert flat.lookup("helpful")[0].section.id == "opening"

        # -- scale: a 49k prompt is what this exists for -----------------
        big = "\n".join(
            f"# Rule {i}\n\nDirective number {i} concerns "
            f"{'widgets' if i % 3 else 'gadgets'} and must be followed "
            f"when handling topic{i}.\n" for i in range(520))
        assert len(big) > 49_000, len(big)
        bidx = PromptIndex.build("big", big)
        assert len(bidx.sections) == 520
        hits = bidx.lookup("topic137")
        assert hits[0].section.heading == "Rule 137", hits[0].section.heading
        # the whole point: one small answer out of a very large document
        assert len(bidx.format_hits(hits)) < len(big) / 100
        assert "520 addressable section(s)" in bidx.format_stats()
        # and a lookup over 49k of prompt is still instant — this runs
        # inside a tool call, on every iteration that needs it
        import time as _t
        t0 = _t.perf_counter()
        for _ in range(50):
            bidx.lookup("directive about gadgets for topic402")
        assert _t.perf_counter() - t0 < 2.0

        # duplicate headings get distinct ids — an id must address exactly
        # one section or a lookup result cannot be cited
        dup = PromptIndex.build("dup", "# Notes\na\n\n# Notes\nb\n")
        assert [s.id for s in dup.sections] == ["notes", "notes-2"]

        print("SPEC SELF-TEST PASS")

    _self_test()

"""Auditing the prompt itself.

Everything else in the Mastermind treats the prompt as given and asks
what happened to it: was it sealed, where was it placed, did the model
follow it. This module asks the question nobody asks, because at 4,000
characters it does not need asking and at 49,000 it is the whole problem:

    is this prompt any good as a document?

A prompt that size is never written in one sitting. It accretes. A rule
gets added in March and added again in July with different wording. A
section written to fix one failure quietly contradicts a section written
to fix another. One section grows to a fifth of the whole and drowns the
rest. Sections end up with no distinctive vocabulary at all, so nothing
can ever retrieve them. None of that is visible to the author, who reads
the prompt as intent rather than as text — and all of it costs adherence
directly, because a contradiction is a coin flip and a redundancy is a
dilution.

Four findings, all decided from the text alone — no model call, no
judgement, no network:

  redundant      two sections saying the same thing in different words.
  contradictory  two sections about the same thing with opposite
                 polarity — a candidate list for you to read, never a
                 verdict.
  oversized      one section large enough to dominate the document.
  unreachable    a section with no distinctive vocabulary, which no
                 lookup can ever surface.

Nothing here rewrites anything. The audit is addressed to the author: it
says which part of the prompt is working against the rest, and leaves the
decision where it belongs.

What it does NOT do, stated plainly because a tool that hides its blind
spot is worse than one that has none: detection is lexical, so it finds
two sections that argue using the same words. A rule written in March
and flatly reversed in July by an author reaching for entirely different
vocabulary will not be caught here, and no clean report should be read
as "this prompt does not contradict itself". It should be read as "these
specific pairs do".
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from .spec import PromptIndex, Section, terms

# Polarity markers. A directive is a rule about what to do or not do, and
# these are how authors write the difference.
_NEGATIVE = re.compile(
    r"\b(?:never|don'?t|do not|must not|cannot|can'?t|avoid|refuse|"
    r"under no circumstances|at no point|without exception)\b", re.I)
_POSITIVE = re.compile(
    r"\b(?:always|must|should|required|require|ensure|make sure|"
    r"be sure to)\b", re.I)

# Thresholds. Deliberately conservative: a false finding on a 49k prompt
# sends the author to reread a section that was fine, and a few of those
# make the whole report ignorable.
# "About the same thing" is measured over each section's DISTINCTIVE
# terms — the ones not common to the document as a whole — using the
# overlap coefficient (shared terms over the smaller set), not Jaccard.
#
# Both choices are load-bearing on a long prompt. Common vocabulary is
# what every section of one document shares by construction ("read",
# "before", "change", "must"), so counting it makes every pair look
# related: measured on raw terms, two unrelated subsystem sections in a
# 49k prompt score 0.94 and the report fills with pairs that have
# nothing to do with each other. Measured on distinctive terms, the same
# pair scores 0.33 and the genuinely restated rule still scores 0.77.
# The overlap coefficient then keeps the terse March rule and its
# verbose July restatement together, where Jaccard would mark that pair
# down for the verbosity alone.
SAME_SUBJECT = 0.60     # overlap coefficient over distinctive terms
RESTATEMENT = 0.55      # ... and this much Jaccard over all terms
OVERSIZED_SHARE = 0.15  # one section this much of the whole document
# ... and this many characters. Share alone is meaningless in a short
# prompt, where three sections are a third each by arithmetic and none of
# them is drowning anything. "Oversized" is a claim about a section being
# too coarse to retrieve or cite, which needs real bulk to be true.
OVERSIZED_CHARS = 2_000
# A section whose rarest term still appears in this share of the
# document's sections has nothing of its own to be found by.
UNREACHABLE_SHARE = 0.12
COMMON_TERM = 0.34      # a term in this share of sections is not specific


@dataclass(frozen=True)
class Finding:
    kind: str
    sections: tuple[str, ...]
    detail: str
    chars: int = 0

    def line(self) -> str:
        shown = list(self.sections[:4])
        if len(self.sections) > 4:
            shown.append(f"+{len(self.sections) - 4} more")
        where = "  +  ".join(shown)
        return f"{self.kind:<14} {where}\n                 {self.detail}"


@dataclass
class Audit:
    prompt: str = ""
    chars: int = 0
    sections: int = 0
    findings: list[Finding] = field(default_factory=list)
    # characters implicated in a redundancy or a contradiction: the part
    # of the document that is arguing with itself
    contested: int = 0

    @property
    def clean(self) -> bool:
        return not self.findings

    def of_kind(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]

    def format(self, limit: int = 12) -> str:
        head = (f"PROMPT AUDIT — {self.prompt}: {self.chars:,} chars, "
                f"{self.sections} section(s)")
        if self.clean:
            return (head + "\n  nothing to flag: no restated rules, no "
                    "opposed pairs, no section large enough to drown the "
                    "rest, and every section has vocabulary of its own.")
        lines = [head]
        if self.contested:
            share = self.contested / self.chars * 100 if self.chars else 0
            lines.append(f"  {self.contested:,} chars ({share:.0f}% of the "
                         f"prompt) sit in sections that restate or oppose "
                         f"another section")
        by_kind: dict[str, int] = {}
        for f in self.findings:
            by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
        lines.append("  " + "   ".join(f"{k} ×{n}"
                                       for k, n in sorted(by_kind.items())))
        for f in self.findings[:limit]:
            lines.append("  " + f.line())
        if len(self.findings) > limit:
            lines.append(f"  (+{len(self.findings) - limit} more)")
        lines.append("  these are candidates for you to read, not verdicts "
                     "— nothing was changed.")
        return "\n".join(lines)


def _polarity(text: str) -> int:
    """-1 prohibitive, +1 prescriptive, 0 neither or both equally."""
    neg = len(_NEGATIVE.findall(text))
    pos = len(_POSITIVE.findall(text))
    if neg > pos:
        return -1
    if pos > neg:
        return 1
    return 0


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _overlap(a: set[str], b: set[str]) -> float:
    """Shared terms over the smaller set — insensitive to length."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _candidate_pairs(term_sets: list[set[str]],
                     df: dict[str, int], n: int) -> set[tuple[int, int]]:
    """Pairs worth comparing, from an inverted index.

    Comparing every pair is O(n²) and a 49k prompt has hundreds of
    sections. Two sections can only be similar if they share a term that
    is not common to begin with, so only those pairs are ever built."""
    postings: dict[str, list[int]] = defaultdict(list)
    for i, ts in enumerate(term_sets):
        for t in ts:
            postings[t].append(i)
    cap = max(2, int(n * COMMON_TERM))
    pairs: set[tuple[int, int]] = set()
    for t, ids in postings.items():
        if df.get(t, 0) > cap or len(ids) < 2:
            continue
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                pairs.add((ids[x], ids[y]))
    return pairs


def _cluster(pairs: list[tuple[int, int, float]]
             ) -> list[tuple[tuple[int, ...], float]]:
    """Union-find over restated pairs, so a family is reported once."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j, _ in pairs:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    groups: dict[int, set[int]] = defaultdict(set)
    strength: dict[int, float] = {}
    for i, j, sim in pairs:
        root = find(i)
        groups[root].update((i, j))
        strength[root] = max(strength.get(root, 0.0), sim)
    return [(tuple(sorted(members)), strength[root])
            for root, members in sorted(groups.items())]


def audit(index: PromptIndex) -> Audit:
    """Read a prompt as a document and say what is wrong with it."""
    sections: list[Section] = index.sections
    report = Audit(prompt=index.name, chars=len(index.text),
                   sections=len(sections))
    if len(sections) < 2:
        return report

    term_sets = [set(terms(s.body)) for s in sections]
    df: dict[str, int] = defaultdict(int)
    for ts in term_sets:
        for t in ts:
            df[t] += 1
    n = len(sections)

    cap = max(2, int(n * COMMON_TERM))
    distinctive = [{t for t in ts if df[t] <= cap} for ts in term_sets]

    contested: set[int] = set()
    restated: list[tuple[int, int, float]] = []
    for i, j in sorted(_candidate_pairs(term_sets, df, n)):
        subject = _overlap(distinctive[i], distinctive[j])
        if subject < SAME_SUBJECT:
            continue
        a, b = sections[i], sections[j]
        pa, pb = _polarity(a.body), _polarity(b.body)
        if pa and pb and pa != pb:
            report.findings.append(Finding(
                "contradictory", (a.label, b.label),
                f"{subject * 100:.0f}% shared vocabulary, opposite "
                f"polarity — one prohibits where the other requires, and "
                f"the model has to pick",
                a.chars + b.chars))
            contested.update((i, j))
        elif _jaccard(term_sets[i], term_sets[j]) >= RESTATEMENT:
            restated.append((i, j, subject))
            contested.update((i, j))

    # Restatements are reported as GROUPS, not as pairs. A family of six
    # sections saying the same thing produces fifteen pairs, and fifteen
    # lines that each name two of the six is not a report anyone can act
    # on — the author needs to see the family. Contradictions stay
    # pairwise on purpose: there the two sides are the finding.
    for group, strength in _cluster(restated):
        labels = tuple(sections[i].label for i in group)
        chars = sum(sections[i].chars for i in group)
        if len(group) == 2:
            detail = (f"{strength * 100:.0f}% shared vocabulary, same "
                      f"polarity — one rule stated twice, so neither wins "
                      f"a lookup cleanly")
        else:
            detail = (f"{len(group)} sections stating one rule "
                      f"({strength * 100:.0f}% shared vocabulary) — "
                      f"{chars:,} chars where one section would do, and no "
                      f"lookup can pick between them")
        report.findings.append(Finding("redundant", labels, detail, chars))

    for i, sec in enumerate(sections):
        share = sec.chars / report.chars if report.chars else 0
        if share >= OVERSIZED_SHARE and sec.chars >= OVERSIZED_CHARS:
            report.findings.append(Finding(
                "oversized", (sec.label,),
                f"{share * 100:.0f}% of the whole prompt in one section — "
                f"too coarse to retrieve or to cite precisely; split it at "
                f"its own sub-rules",
                sec.chars))
        # Unreachable: not "most of its words are common" but "even its
        # RAREST word is common". A section is found by whichever of its
        # words the rest of the document does not use, so one rare word
        # is enough to make it retrievable and none at all makes it
        # invisible. Quoting the section back to the index does not test
        # this — a short section wins its own text on brevity alone,
        # while still losing every real question.
        if not term_sets[i]:
            continue
        rarest = min(df[t] for t in term_sets[i])
        if rarest >= max(3, int(n * UNREACHABLE_SHARE)):
            report.findings.append(Finding(
                "unreachable", (sec.label,),
                f"its rarest word still appears in {rarest} of {n} "
                f"sections — it has no vocabulary of its own, so no "
                f"question can rank it above the sections it borrows "
                f"from; name the thing it is actually about",
                sec.chars))

    report.contested = sum(sections[i].chars for i in contested)
    return report


if __name__ == "__main__":
    def _self_test() -> None:
        doc = """# Reading files

Always read a file before you edit it. Read it in full when it is short.

# Opening files first

You must always read a file before editing it, in full where short.

# Shell commands

Never run a destructive shell command without asking first.

# Running commands

Always run destructive shell commands when they are needed, without
asking, because asking wastes the user's time.

# Notes

The thing is the thing and it is what it is when it is.
"""
        idx = PromptIndex.build("t", doc)
        rep = audit(idx)

        # the same rule written twice, months apart, in different words
        red = rep.of_kind("redundant")
        assert len(red) == 1, [f.sections for f in red]
        assert set(red[0].sections) == {"Reading files", "Opening files first"}

        # the same subject with opposite polarity — the coin flip
        con = rep.of_kind("contradictory")
        assert len(con) == 1, [f.sections for f in con]
        assert set(con[0].sections) == {"Shell commands", "Running commands"}

        # a contradiction is reported as a contradiction, not as a
        # redundancy: they have completely different fixes
        assert not any(set(f.sections) == {"Shell commands",
                                           "Running commands"}
                       for f in red)

        assert rep.contested > 0
        text = rep.format()
        assert "PROMPT AUDIT" in text
        assert "candidates for you to read, not verdicts" in text
        assert "contradictory" in text and "redundant" in text

        # -- a clean prompt is reported clean ----------------------------
        clean = PromptIndex.build("c", """# Reading
Read a file before editing it.

# Credentials
Never commit a secret to git.

# Testing
Run pytest and quote the output.
""")
        rc = audit(clean)
        assert rc.clean, [f.line() for f in rc.findings]
        assert "nothing to flag" in rc.format()

        # -- oversized: one section drowning the rest --------------------
        big = PromptIndex.build("b", "# Small\nbe brief.\n\n# Huge\n"
                                + "detailed guidance about widgets. " * 400)
        assert any(f.kind == "oversized" for f in audit(big).findings)

        # -- unreachable: nothing to find it by --------------------------
        # A section whose every word is common to the whole document
        # cannot be retrieved by any honest query — BM25 gives a term
        # that appears everywhere almost no weight. This is the section
        # an author swears is in the prompt and the model never applies.
        # The heading counts as vocabulary — a section called "Handling
        # refunds" is findable even if its body is generic. So the real
        # case is the one where the heading is boilerplate too: the
        # "# Important" section every long prompt grows.
        body = ["# Important\n\nFollow the rules carefully and review "
                "the guidance.\n"]
        for i in range(12):
            body.append(f"# Topic {i}\n\nFollow the important rules "
                        f"carefully and review the guidance about "
                        f"subject{i} using widget{i}.\n")
        vague = audit(PromptIndex.build("v", "\n".join(body)))
        un = vague.of_kind("unreachable")
        assert [f.sections for f in un] == [("Important",)], \
            [f.sections for f in vague.findings]
        # and the index agrees, which is what makes this worth reporting:
        # no question specific enough to be worth asking can reach it.
        vidx = PromptIndex.build("v", "\n".join(body))
        for question in ("what applies to subject7", "widget3 handling",
                         "guidance about subject11"):
            heads = [h.section.heading for h in vidx.lookup(question)]
            assert "Important" not in heads, (question, heads)
        # It IS returned for a query as vague as itself — which is the
        # finding restated, not a contradiction of it: the only way to
        # reach that section is to already be asking nothing in
        # particular, and then it wins on brevity rather than on meaning.
        vague_heads = [h.section.heading
                       for h in vidx.lookup("follow the rules carefully")]
        assert "Important" in vague_heads, vague_heads

        # -- scale: a 49k prompt must audit in reasonable time -----------
        import time
        parts = []
        for i in range(520):
            parts.append(f"# Rule {i}\n\nDirective number {i} concerns "
                         f"{'widgets' if i % 3 else 'gadgets'} and must be "
                         f"followed when handling topic{i}.\n")
        # two real duplicates planted in the middle of a large prompt —
        # exactly the thing an author cannot see by reading
        parts.append("# Secrets\n\nNever commit an API key or password "
                     "to a tracked file in the repository.\n")
        parts.append("# Credentials in git\n\nNever commit a password or "
                     "an API key to a tracked file in the repository.\n")
        big49 = "\n".join(parts)
        assert len(big49) > 49_000, len(big49)
        t0 = time.perf_counter()
        rep49 = audit(PromptIndex.build("big49", big49))
        elapsed = time.perf_counter() - t0
        assert elapsed < 10.0, elapsed
        found = [f for f in rep49.of_kind("redundant")
                 if "Secrets" in f.sections[0] or "Secrets" in f.sections[1]]
        assert found, [f.sections for f in rep49.of_kind("redundant")][:5]

        print("PROMPTAUDIT SELF-TEST PASS")

    _self_test()

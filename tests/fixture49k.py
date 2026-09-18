"""A 49,000-character system prompt, with the flaws real ones have.

The repo's own `main` prompt is 4.2k, and every number measured on it is
the wrong number for someone running a prompt ten times that size. This
builds one at the real scale so the claims in the README are measured
where they are actually meant to hold.

It is a constructed prompt, not anyone's real one — but it is
constructed the way real long prompts are: written in sections over
time, with a rule restated months later in different words, a pair of
sections that ended up opposing each other, one section that grew far
past the rest, and a "# Important" section of pure boilerplate. Those
four are planted deliberately, because an audit that is only ever run on
clean input has never been tested.

The per-subsystem sections rotate through six shapes, so each shape
repeats across many subsystems. That is not an accident of the fixture
either: a family of near-identical per-subsystem sections is the single
most common thing in a real prompt of this size, and the audit reports
each family once rather than as every pair inside it.
"""

PREAMBLE = """You are a senior engineer working inside a large, long-lived
codebase. You are trusted with production systems. Your job is to make
changes that a careful reviewer would approve without a second pass, and
to be honest about what you did and did not verify.

Work from evidence. When you do not know something, find out rather than
guess, and say which of the two you did.
"""

# Real directive sections — the kind a team actually writes.
SECTIONS: list[tuple[str, str]] = [
    ("Reading before changing",
     "Read a file before you edit it. Read the whole file when it is "
     "short, and the surrounding function and its callers when it is "
     "long. An edit to code you have not read is a guess wearing the "
     "costume of a change."),
    ("Making changes",
     "Keep each change as small as the task allows. Do not reformat code "
     "you are not otherwise touching, do not rename things in passing, "
     "and do not fix unrelated problems in the same commit. A diff that "
     "does one thing can be reviewed; a diff that does four cannot."),
    ("Verification",
     "Run the project's own checks. Use the command the repository "
     "documents, not one you invented. If the check fails, show the "
     "failing output rather than describing it."),
    ("Claiming success",
     "Never claim success without evidence. A passing check that ran "
     "AFTER your last edit is evidence. A passing check from before the "
     "edit is not, a plan to run one is not, and a belief that it should "
     "pass is not."),
    ("Reporting failure",
     "When something fails, say so in the first sentence. Do not bury a "
     "failure under a summary of what did work. The reader is deciding "
     "whether to ship, and they need the bad news first."),
    ("Uncertainty",
     "Mark the difference between what you checked and what you inferred. "
     "\"I ran the tests and they pass\" and \"this should not affect the "
     "tests\" are different claims and must read differently."),
    ("Citations",
     "Cite real paths and real line numbers. Quote the code you are "
     "describing rather than paraphrasing it. A citation the reader "
     "cannot open is worse than no citation, because it looks checkable "
     "and is not."),
    ("Credentials and secrets",
     "Never write a credential, token or password into a file that git "
     "tracks. If you find one already committed, say so immediately and "
     "do not include its value in your report."),
    ("Destructive operations",
     "Never run a destructive shell command without asking first. That "
     "includes rm on anything outside a build directory, dropping or "
     "truncating a table, force-pushing a shared branch, and any write "
     "to a production system."),
    ("Dependencies",
     "Do not add a dependency to solve a problem the standard library "
     "already solves. When a dependency is genuinely needed, pin it and "
     "say in the change why the standard library was not enough."),
    ("Generated files",
     "Regenerate lockfiles and generated code with the repository's own "
     "tooling. Never hand-edit a generated file: the next regeneration "
     "silently reverts it and nobody finds out until much later."),
    ("Tests",
     "A bug report is a request for a failing test and then a fix. Write "
     "the test first so the fix has something to prove itself against. "
     "Never skip, disable or quarantine a test to get a green run."),
    ("Migrations",
     "A schema migration must be reversible or must be explicitly marked "
     "as irreversible with a written reason. Always write the down "
     "migration before the up migration ships."),
    ("Logging",
     "Log the facts needed to diagnose a failure and nothing more. Never "
     "log a request body, an authorization header, or a user's personal "
     "data, even at debug level."),
    ("Error handling",
     "Do not swallow an exception to make a symptom disappear. Either "
     "handle the error meaningfully or let it propagate to something "
     "that can. A bare except that logs and continues hides the next "
     "bug as well as this one."),
    ("Concurrency",
     "State shared between threads needs a lock or needs to stop being "
     "shared. Prefer stopping being shared. When a lock is genuinely "
     "needed, document what it guards in a comment above it."),
    ("Performance",
     "Measure before optimising and measure after. An optimisation with "
     "no number attached is a preference. Quote the before and the after "
     "in the change description."),
    ("Comments",
     "Write comments that explain why, not what. The code already says "
     "what it does. A comment earns its place by recording the reason a "
     "reader would otherwise have to reconstruct."),
    ("Naming",
     "Name things after what they are in the domain, not after their "
     "type or their position in a pipeline. A reader who knows the "
     "domain should be able to guess the name."),
    ("Review feedback",
     "Implement a reviewer's small, local request and push it. For a "
     "larger request, reply with your proposal and let the author "
     "decide rather than pushing a rewrite they did not ask for."),
    ("Asking questions",
     "Ask when two readings of the request would produce materially "
     "different work. Otherwise pick the reasonable default, say which "
     "one you picked, and keep going."),
    ("Scope",
     "Deliver what was asked. Do not quietly narrow the task because "
     "part of it is hard, and do not widen it because you noticed "
     "something else. If you leave something out, say what and why."),
]

# The accretion artifacts, planted on purpose.
PLANTED: list[tuple[str, str]] = [
    # A rule restated months later, in different words.
    ("Opening files first",
     "You must always read a file before editing it. Read the whole file "
     "where it is short, and the surrounding function and its callers "
     "where it is long. Editing code you have not read is guessing."),
    # A pair that ended up opposing each other — the same rule rewritten
    # later, reaching for the same words, and flipped.
    ("Shell operations",
     "Always run a destructive shell command when the task needs one, "
     "without asking first. That includes rm outside a build directory, "
     "dropping or truncating a table, force-pushing a shared branch, and "
     "writes to a production system."),
    # Boilerplate with no vocabulary of its own. Both its heading and its
    # body are built entirely from words the rest of the prompt uses
    # constantly, which is what makes it unretrievable — and what makes
    # it the section an author swears is in the prompt and the model
    # never seems to apply.
    ("Changes",
     "Before you make a change, read it. Make the change you should "
     "make, and read the change before you make it."),
]


def _topic(i: int) -> tuple[str, str]:
    """One of the many domain sections a long prompt accumulates.

    Six different shapes, rotated, because a real prompt's domain
    sections are written by different people at different times and do
    NOT share a template. A fixture whose filler is one sentence with the
    nouns swapped would make the audit look like it flags everything."""
    name = _NAMES[i % len(_NAMES)]
    noun = _NOUNS[i % len(_NOUNS)]
    other = _NOUNS[(i + 3) % len(_NOUNS)]
    verb = _VERBS[i % len(_VERBS)]
    team = i % 9
    shapes = [
        (f"{name.title()} subsystem ({i})",
         f"The {name} path owns its {noun} format end to end. Anything "
         f"that {verb}s a {noun} goes through its public entry point, "
         f"never through the internals, and a caller that needs a "
         f"different shape asks team-{team} for one rather than reaching "
         f"in."),
        (f"Working on {name} ({i})",
         f"Read the {name} module docstring first: it records why the "
         f"{other} layer exists, which is not obvious from the code. "
         f"Changes that alter timing here have historically caused "
         f"incidents, so measure before and after."),
        (f"{name.title()} invariants ({i})",
         f"Two things must stay true of {name}: every {noun} it emits is "
         f"replayable, and no {other} outlives the request that created "
         f"it. A change that breaks either needs team-{team} to sign off "
         f"in writing."),
        (f"Compatibility: {name} ({i})",
         f"Old clients still send the previous {noun} encoding. Accept "
         f"both until the deprecation window closes, and never remove "
         f"the older branch on the grounds that nothing in the repo "
         f"calls it — the callers are not in this repo."),
        (f"{name.title()} failure modes ({i})",
         f"When {name} is degraded it sheds load rather than queueing "
         f"indefinitely. Preserve that. If you add a retry, add a budget "
         f"with it, and make the {other} path give up loudly instead of "
         f"silently doubling the work."),
        (f"Testing {name} ({i})",
         f"The {name} suite is slow because it exercises real I/O, and "
         f"that is deliberate. Do not replace it with mocks to make it "
         f"fast; add a narrower unit test alongside it if you need a "
         f"quick signal while iterating."),
    ]
    return shapes[i % len(shapes)]


_VERBS = ["emit", "consume", "rewrite", "validate", "route", "persist",
          "sign", "compress", "shard", "replay"]
_NAMES = ["ingest", "billing", "scheduler", "search", "notifications",
          "auth", "reporting", "export", "webhooks", "cache", "queue",
          "storage", "routing", "audit", "settings", "sessions"]
_NOUNS = ["envelope", "cursor", "checkpoint", "receipt", "manifest",
          "token", "ledger", "batch", "signature", "shard"]

# One section that grew far past the others.
OVERSIZED = (
    "Incident response",
    "When a production incident is open, the rules change and this "
    "section governs. " + " ".join(
        f"Step {i}: {text}" for i, text in enumerate([
            "acknowledge the page before you start investigating so "
            "nobody duplicates your work",
            "state the user-visible symptom in one sentence before "
            "forming any theory about the cause",
            "capture the current state (logs, metrics, a heap dump if "
            "the process is still alive) before restarting anything, "
            "because a restart destroys the evidence",
            "prefer mitigation over diagnosis while users are affected: "
            "roll back, fail over or shed load first and understand it "
            "afterwards",
            "announce every action you take in the incident channel "
            "before you take it, so two responders never act at once",
            "never change more than one thing at a time once mitigation "
            "has started, or you will not know which change helped",
            "write the timeline as you go rather than reconstructing it "
            "afterwards from memory, which is always wrong",
            "hand over explicitly when you stop, naming what is known, "
            "what is suspected and what has been tried",
            "keep the customer-facing update separate from the "
            "engineering one and never put a theory in the former",
            "close the incident only when the symptom is gone and the "
            "mitigation is durable, not when the cause is understood",
            "file the follow-up work before the retrospective, while "
            "the detail is still in your head",
            "in the retrospective describe the system's behaviour, not "
            "a person's decision; a system that allows a mistake is the "
            "finding, not the mistake",
        ] * 9)))


def build(target: int = 49_000) -> str:
    """A prompt of at least `target` characters, realistic in structure."""
    parts = [PREAMBLE]
    for heading, body in SECTIONS:
        parts.append(f"# {heading}\n\n{body}\n")
    for heading, body in PLANTED:
        parts.append(f"# {heading}\n\n{body}\n")
    parts.append(f"# {OVERSIZED[0]}\n\n{OVERSIZED[1]}\n")
    i = 0
    while sum(len(p) + 1 for p in parts) < target:
        heading, body = _topic(i)
        parts.append(f"# {heading}\n\n{body}\n")
        i += 1
    return "\n".join(parts)


if __name__ == "__main__":
    text = build()
    print(f"{len(text):,} chars, {text.count(chr(10) + '# ')} headings")

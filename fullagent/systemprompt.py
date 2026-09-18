"""systemprompt.py — the ONE home of every system prompt in FullAgent.

Every prompt the model ever sees lives here and nowhere else. The rest of
the codebase only ever imports from this file — no inline prompt strings
exist in agent.py, swarm.py or team.py. That is the structural guarantee:

  * single source of truth  — edit a prompt here, it changes everywhere.
  * one delivery path       — every message list is built through
                              `with_system()`, which guarantees the right
                              system prompt sits at position 0 before the
                              request is sent. A model can never be called
                              without its prompt, and can never see a
                              stale or partial one.
  * compliance by design    — the prompts are written so that following
                              them is the path of least resistance: a
                              clear identity, a short set of prime
                              directives, and an exact output contract.
                              No threats, no "you must obey" — the
                              structure itself carries the authority.

Prompts defined:
    MAIN          the sovereign agent (the main conversation loop)
    SCOUT         read-only scout sub-agents (swarm.py)
    WORKER        parallel worker sub-agents (team.py), per role brief
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# MAIN — the sovereign agent
# ---------------------------------------------------------------------------

MAIN = """You are FullAgent — an autonomous terminal AI agent built to help with software engineering tasks.

## Identity
You are a careful, capable software engineering agent. You work in a Linux environment with access to tools for reading files, editing code, running commands, and searching the web. Your purpose is to help the user achieve their goals reliably and safely.

## Prime Directives
1. **Understand first.** Read the codebase before making changes. Never assume.
2. **Make minimal, correct changes.** Prefer small, surgical edits over rewrites.
3. **Verify everything.** Run tests, check exit codes, confirm file contents. Never claim success without evidence.
4. **Be honest about uncertainty.** If you don't know something, say so and investigate.
5. **Respect the user's autonomy.** Ask before making irreversible changes (deletes, destructive operations).
6. **Never fabricate.** Cite real sources, real file paths, real exit codes. No invented information.

## Tool Usage
- Use `read_file` to inspect files before editing them.
- Use `edit_file` for precise string replacements; `write_file` for new files or full rewrites.
- Use `run_command` to execute builds, tests, git commands, and scripts.
- Use `search_files` for regex-based code search; `web_search` for real-time information.
- Use `web_fetch` to read specific URLs when you need full article content.

## Parallel Subagents
Independent work should happen at the same time, not one piece after another.
- `spawn_agents` takes a LIST of tasks and runs them ALL AT ONCE. Pass every independent piece of work in a single call — calling it repeatedly, one task at a time, throws away the parallelism.
- Reach for it whenever a job splits into parts that do not depend on each other: investigating several modules, checking several hypotheses, reviewing several files, writing several unrelated pieces.
- Give each subagent a role (`researcher`, `coder`, `tester`, `reviewer`, `analyst`, `architect`, `debugger`, `optimizer`, `refactorer`, `documenter`, `devops`, `integrator`, `planner`) and a task specific enough to finish without asking you anything.
- Then call `wait_for_agents` once for the whole batch. Waiting for eight costs about what waiting for one costs.
- Work that must happen in order stays with you, or goes into separate batches in sequence. Two subagents must never be given the same file to edit.
- The machine is protected automatically: their local work is throttled to whatever headroom it has, and their writes are serialised. You do not need to hold back for performance — only for correctness.
- Overlap between subagents is free: if several of them read the same file or run the same search, it executes once and they all get the answer. Do not contort a split just to avoid repetition — split it the way the work actually divides.
- A report marked **PARTIAL** means that subagent was asked to wrap up early — it was circling, or the rest of the batch had long since finished. What it reports is real, but it may be incomplete. Either accept it as a partial answer and say so plainly, or `send_to_agent` to continue it with its context intact. Never present a partial finding as a settled one.

## Output Contract
- Be concise and factual. Lead with the answer, then give supporting detail.
- Use code blocks for code, commands, and file contents.
- Cite sources (URLs, file:line) when referencing external information.
- When a task is complete, state what was done and verify it.

## Safety
- Never execute harmful or destructive commands without explicit user approval.
- Never exfiltrate data, access unauthorized systems, or bypass security controls.
- If a request seems harmful, explain why and offer a safe alternative.
- Respect privacy: do not read sensitive files (keys, credentials) unless the task requires it.

## Goal Mode
When the user gives you a verifiable mission ("fix", "add", "make X pass"), you may draft a machine-checkable goal contract. Each clause has a predicate that can be verified deterministically. A clause is only proven when its predicate actually passes — never declare success on your own say-so.

You are helpful, capable, and honest. Help the user build things that work.
"""


# ---------------------------------------------------------------------------
# SCOUT — read-only scout sub-agent
# ---------------------------------------------------------------------------

SCOUT = """You are a Scout — a read-only investigative sub-agent working within FullAgent.

Your role is to gather facts: read files, search code, run read-only commands, and report findings. You are one of several parallel scouts.

Rules:
- You are READ-ONLY. Never modify files, never run writes, never delete anything.
- Gather evidence before reporting. Cite file paths and line numbers.
- Be fast and decisive. Inspect, report, finish.
- If something is ambiguous, make the most reasonable interpretation and note it.

When done, reply with a final report in EXACTLY this form:
STATUS: DONE | BLOCKED
SUMMARY: <2-5 factual lines: what you found, exact paths/numbers, key evidence>
"""


# ---------------------------------------------------------------------------
# WORKER — parallel worker sub-agents (one template, per-role briefs)
# ---------------------------------------------------------------------------

WORKER = """You are {role_brief}

You are one of up to {max_workers} workers running IN PARALLEL on the same machine. Rules:
- Complete ONLY your assigned task; other workers handle the rest.
- Work fast and decisively: inspect, act, verify, finish.
- Use your tools to gather real evidence before claiming anything.
- If your task is ambiguous, do the most reasonable interpretation and note it.
- `share_finding` the moment you ESTABLISH something a peer could use — a path, a signature, a root cause, a dead end worth not repeating. Share it when you learn it, not at the end: a fact that arrives after everyone has finished saved nobody anything.
- You will be handed findings from the others as they arrive. Use them instead of rediscovering the same ground, but treat them as reports rather than proof: re-check anything you are about to depend on, especially anything that may have changed since they looked.

When done, reply with a final report in EXACTLY this form:
STATUS: DONE | BLOCKED
SUMMARY: <2-5 factual lines: what you did, what you found, exact paths/numbers>"""

# Role briefs slot into the WORKER template. Kept here (not in team.py) so
# every word the model reads is defined in this one file.
ROLE_BRIEFS: dict[str, str] = {
    "researcher": ("a RESEARCH specialist. Gather facts from the web and "
                   "the codebase. Cite sources (URLs, file:line). Never "
                   "modify anything."),
    "coder": ("a senior SOFTWARE ENGINEER. Read before you write; make "
              "minimal, correct changes; keep existing style and "
              "conventions."),
    "tester": ("a QA / TEST engineer. Run builds, tests and checks; report "
               "exact exit codes, failures and the minimal reproduction. "
               "Never modify source files."),
    "reviewer": ("a CODE REVIEWER. Inspect the code and report bugs, risks "
                 "and style problems with file:line evidence. Never modify "
                 "anything."),
    "analyst": ("a DATA / SYSTEMS analyst. Combine local evidence and live "
                "web data into numbers, comparisons and a verdict. Never "
                "modify anything."),
}


# ---------------------------------------------------------------------------
# Builders — the only functions the rest of the code calls
# ---------------------------------------------------------------------------

def main() -> str:
    """The sovereign agent's system prompt."""
    return MAIN


def scout() -> str:
    """A scout sub-agent's system prompt."""
    return SCOUT


def worker_brief(brief: str, max_workers: int) -> str:
    """A worker system prompt for an arbitrary brief.

    Used for briefs that are not (yet) in ROLE_BRIEFS — an evolution
    candidate, a drafted role. Every caller goes through here rather than
    calling WORKER.format() itself, so a caller can never miss a
    placeholder the template grew."""
    return WORKER.format(role_brief=brief, max_workers=max_workers)


def worker(role: str, max_workers: int) -> str:
    """A worker sub-agent's system prompt for the given role."""
    brief = ROLE_BRIEFS.get(role, ROLE_BRIEFS["coder"])
    return worker_brief(brief, max_workers)


def one_shot(gate, prompt_name: str, fallback: str,
             user_content: str) -> list[dict]:
    """The message pair for a subsystem's single, tool-less model call.

    With a gate (the Mastermind's, handed in by the Agent) the prompt is
    sealed and the call lands in the lineage like every other. Without one
    — a module self-test running standalone — the same text is seated
    directly. Either way the words come from this file and nowhere else,
    which is what keeps the guarantee at the top of it true.

    `gate` is duck-typed on purpose: mastermind.py imports this module, so
    naming its type here would close the loop into a circular import."""
    messages = [{"role": "user", "content": user_content}]
    if gate is not None:
        messages, _ = gate.dispatch(prompt_name, messages)
        return messages
    return with_system(messages, fallback)


def with_system(messages: list[dict], system: str) -> list[dict]:
    """Guarantee the system prompt is present and first.

    This is the single delivery path: every request to a model is built
    through here. If messages[0] is not already the system prompt, it is
    (re)placed — so the model always sees the full, current prompt from
    this file, and nothing upstream can accidentally drop or shadow it."""
    if messages and messages[0].get("role") == "system":
        messages[0] = {"role": "system", "content": system}
    else:
        messages.insert(0, {"role": "system", "content": system})
    return messages


# ---------------------------------------------------------------------------
# MASTER — the extended, very long system prompt
# ---------------------------------------------------------------------------
# This is the second, much larger system prompt. It embeds the full master
# specification (project.txt) so the model carries the entire architecture,
# invariants, subsystem contracts and Goal-Mode grammar in context. It is
# far longer than MAIN (which is ~4k chars) — by design.
#
# project.txt is NOT in the repository; it ships beside this module. When
# it is absent, MASTER degrades to exactly MAIN — see _build_master().

def _load_master_spec() -> str:
    """Load the master specification (project.txt) that ships beside this
    module. Returns '' if the file is missing, so the module never crashes
    on import."""
    from pathlib import Path
    spec = Path(__file__).parent / "project.txt"
    try:
        return spec.read_text(encoding="utf-8")
    except OSError:
        return ""


_SPEC = _load_master_spec()


def _build_master(spec: str) -> str:
    """MAIN, plus the master specification when there actually is one.

    With no spec, MASTER is exactly MAIN. The alternative — emitting the
    "specification below is binding" banner over an empty body — is worse
    than saying nothing: the model is told a binding document follows and
    finds nothing there, so the one prompt that is supposed to carry the
    most authority is the one that opens by being wrong."""
    if not spec.strip():
        return MAIN
    return (
        MAIN
        + "\n\n"
        + "=" * 72
        + "\nFULL MASTER SPECIFICATION — the architecture you operate "
          "within. Treat every invariant, subsystem contract and Goal-Mode "
          "rule below as binding.\n"
        + "=" * 72
        + "\n\n"
        + spec
    )


MASTER = _build_master(_SPEC)


def spec_present() -> bool:
    """True when project.txt was found beside this module and loaded.

    False means `master` and `main` are the same prompt — worth surfacing
    rather than leaving the user to wonder why the two are identical."""
    return bool(_SPEC.strip())


# ---------------------------------------------------------------------------
# INTERNAL — the one-shot prompts the subsystems speak with
# ---------------------------------------------------------------------------
# These drive single, tool-less model calls inside a subsystem: one turn
# of a debate, one role draft, one synthesis. They were written inline at
# their call sites, which made this file's "no inline prompt strings
# exist" claim untrue and — worse — kept nine model calls outside the
# Mastermind entirely: unsealed, ungated, invisible in the ledger. They
# are prompts. They live here, and they go through the same gate.

COUNCIL_SPEAKER = ("You are one voice in a structured debate council. "
                   "Answer exactly as instructed.")

DEBATE_SPEAKER = ("You are a participant in an answer tournament. Follow "
                  "the instructions exactly and concisely.")

DUAL_FAST = "Answer directly and concisely."

EVOLUTION_MUTATOR = ("You improve agent role briefs. Output ONLY candidate "
                     "briefs separated by lines with exactly --- . No prose "
                     "around them.")

ROLE_DRAFTER = ("You design agent specialists. Reply ONLY with a JSON "
                "object: {\"name\": snake_case_id, \"brief\": one strong "
                "paragraph (>=100 words) telling this specialist exactly "
                "how to work, \"tools\": subset of the allowed list, "
                "\"benchmark\": one task proving the role works}. No "
                "prose around the JSON.")

PROGRAM_SYNTH = ("You write small pure Python tools. Reply with ONLY the "
                 "function source — no imports, no prose, no markdown "
                 "fence. The function must be deterministic and pure.")

INTENT_COMPILER = (
    "You are the front-end of an agent work compiler. Decompose the goal "
    "into 4-12 work items. Reply with ONLY a JSON array; each element: "
    "{{\"task\": string, \"role\": one of {roles}, \"paths\": [files this "
    "item may write or read], \"depends_on\": [indexes of items that must "
    "finish first, 0-based]}}. No prose, no markdown fence.")


def intent_compiler(roles: list[str] | set[str]) -> str:
    """The intent compiler's prompt, with the live role vocabulary in it.

    The roles are a runtime set, so this one is built rather than
    constant — but it is still built here, from a template defined here,
    and it still resolves through the registry."""
    return INTENT_COMPILER.format(roles=", ".join(sorted(roles)))


# ---------------------------------------------------------------------------
# Prompt registry — add more system prompts here later
# ---------------------------------------------------------------------------
# Every selectable system prompt lives in this one map. To add another
# prompt later, either drop a new constant above and register it here, or
# call register() at runtime. get() resolves a name to its prompt, falling
# back to MAIN so an unknown name can never leave the model promptless.
#
# The internal:* names are the subsystems' one-shot prompts. They are in
# the registry so the vault can seal them and the gate can dispatch them
# — a prompt outside the registry cannot be sealed, and a prompt that was
# never sealed must never reach a model.

PROMPTS: dict[str, str] = {
    "main": MAIN,
    "master": MASTER,
    "internal:council-speaker": COUNCIL_SPEAKER,
    "internal:debate-speaker": DEBATE_SPEAKER,
    "internal:dual-fast": DUAL_FAST,
    "internal:evolution-mutator": EVOLUTION_MUTATOR,
    "internal:role-drafter": ROLE_DRAFTER,
    "internal:program-synth": PROGRAM_SYNTH,
}


def get(name: str) -> str:
    """Resolve a prompt name to its text (falls back to MAIN)."""
    return PROMPTS.get(name, MAIN)


def register(name: str, prompt: str) -> None:
    """Add (or replace) a named system prompt at runtime."""
    PROMPTS[name] = prompt


def names() -> list[str]:
    """The registered prompt names."""
    return sorted(PROMPTS)


# ---------------------------------------------------------------------------
# User prompts — your own prompt, as a file
# ---------------------------------------------------------------------------
# Until now the only way to run your own system prompt was to edit MAIN in
# this file, or to smuggle it in as project.txt and have it appended under
# MAIN as a "specification". Neither is a prompt you own; both are edits to
# the program. A prompt is content, so it belongs in a file you control.
#
# Drop a .md or .txt in <APP_DIR>/prompts/ and it is registered under its
# filename, sealed by the vault like every built-in, and selectable. A file
# named `default` is special: it becomes the active prompt on its own, so
# using your own prompt takes no command at all — the file IS the setting.

USER_PROMPT_SUFFIXES = (".md", ".txt", ".prompt")

#: Registered names that came from a user file, newest load wins.
USER_PROMPTS: dict[str, str] = {}

#: The filename (without suffix) that is selected automatically.
DEFAULT_USER_PROMPT = "default"


def load_user_prompts(directory) -> dict[str, str]:
    """Register every prompt file in `directory`. Returns {name: path}.

    Unreadable or empty files are skipped rather than raised on: a typo in
    one file must not stop the agent from starting, and an empty file is
    an accident every time — seating it would leave the model promptless,
    which is the one outcome this module exists to prevent."""
    from pathlib import Path
    found: dict[str, str] = {}
    base = Path(directory)
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return found
    for path in entries:
        if not path.is_file() or path.suffix.lower() not in \
                USER_PROMPT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not text.strip():
            continue
        name = path.stem
        if name in PROMPTS and name not in USER_PROMPTS:
            # never let a file shadow a built-in or an internal:* prompt
            name = f"user:{name}"
        register(name, text)
        USER_PROMPTS[name] = str(path)
        found[name] = str(path)
    return found


def active_user_default() -> str | None:
    """The user prompt that should be selected with no command given.

    A file called `default` is an unambiguous statement of intent — the
    user put it there and named it that — so it outranks the built-in
    `main`. Anything else they must select, because guessing which of
    several files they meant would be worse than asking."""
    for candidate in (DEFAULT_USER_PROMPT, f"user:{DEFAULT_USER_PROMPT}"):
        if candidate in USER_PROMPTS:
            return candidate
    return None


if __name__ == "__main__":
    # sanity: every builder returns a non-empty prompt, and with_system
    # always leaves the system prompt at position 0.
    assert main() and scout()
    for role in ROLE_BRIEFS:
        assert worker(role, 8)
        assert "8 workers" in worker(role, 8)
    # an arbitrary brief formats through the same builder — every
    # placeholder the template has is always filled
    assert "8 workers" in worker_brief("a CANDIDATE brief.", 8)
    assert "a CANDIDATE brief." in worker_brief("a CANDIDATE brief.", 8)
    msgs = [{"role": "user", "content": "hi"}]
    with_system(msgs, main())
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == MAIN
    with_system(msgs, scout())  # replaces, never duplicates
    assert len([m for m in msgs if m["role"] == "system"]) == 1
    assert msgs[0]["content"] == SCOUT

    # MASTER carries the spec when project.txt is present, and is exactly
    # MAIN when it is not — never a banner promising a spec that is absent.
    if spec_present():
        assert len(MASTER) > len(MAIN) and _SPEC in MASTER
    else:
        assert MASTER == MAIN
    assert _build_master("") == MAIN
    assert _build_master("   \n ") == MAIN
    assert "SPEC BODY" in _build_master("SPEC BODY")
    assert _build_master("SPEC BODY").startswith(MAIN)
    assert get("master") == MASTER
    assert get("main") == MAIN
    assert get("nope") == MAIN  # unknown name falls back, never empty
    register("custom", "hello prompt")
    assert get("custom") == "hello prompt"
    assert "master" in names() and "main" in names()
    # every internal:* prompt resolves, and none is empty
    for _name, _text in PROMPTS.items():
        assert _text and get(_name) == _text, _name
    # the compiler's template fills its role slot and leaves the literal
    # JSON braces alone
    _ic = intent_compiler({"tester", "coder"})
    assert "one of coder, tester" in _ic
    assert '{"task": string' in _ic and "{roles}" not in _ic

    # ------------------------------------------------------------------
    # The guarantee this file opens by making, checked instead of claimed:
    # no module writes a system prompt inline. Every prompt the model ever
    # reads is defined here, which is also what lets the Mastermind seal
    # and gate all of them — an inline literal is a model call nobody
    # recorded.
    # ------------------------------------------------------------------
    import ast as _ast
    from pathlib import Path as _Path

    # mastermind.py's own self-test builds shadowing system messages on
    # purpose, to prove the gate replaces them. That is the one legitimate
    # inline literal in the package.
    _EXEMPT = {"systemprompt.py", "mastermind.py"}
    _offenders: list[str] = []
    for _f in sorted(_Path(__file__).parent.glob("*.py")):
        if _f.name in _EXEMPT:
            continue
        for _node in _ast.walk(_ast.parse(_f.read_text(encoding="utf-8"))):
            if not isinstance(_node, _ast.Dict):
                continue
            for _k, _v in zip(_node.keys, _node.values):
                if (isinstance(_k, _ast.Constant) and _k.value == "role"
                        and isinstance(_v, _ast.Constant)
                        and _v.value == "system"):
                    _offenders.append(f"{_f.name}:{_node.lineno}")
    assert not _offenders, (
        "inline system prompt(s) outside systemprompt.py — every prompt "
        "belongs here so it can be sealed and gated: " + ", ".join(_offenders))

    # ------------------------------------------------------------------
    # user prompts: a file is a prompt, and `default` selects itself
    # ------------------------------------------------------------------
    import tempfile as _tf
    from pathlib import Path as _P2
    with _tf.TemporaryDirectory() as _td:
        _d = _P2(_td)
        (_d / "mine.md").write_text("MY OWN PROMPT", encoding="utf-8")
        (_d / "default.txt").write_text("MY DEFAULT PROMPT", encoding="utf-8")
        (_d / "empty.md").write_text("   \n", encoding="utf-8")
        (_d / "notes.rst").write_text("not a prompt suffix", encoding="utf-8")
        (_d / "main.md").write_text("SHADOWS A BUILT-IN", encoding="utf-8")
        _loaded = load_user_prompts(_d)
        assert "mine" in _loaded and get("mine") == "MY OWN PROMPT"
        assert "default" in _loaded
        assert active_user_default() == "default"
        # empty files and unknown suffixes are skipped, never seated
        assert "empty" not in _loaded and "notes" not in _loaded
        # a file can never shadow a built-in
        assert "main" not in _loaded and "user:main" in _loaded
        assert get("main") == MAIN
        assert get("user:main") == "SHADOWS A BUILT-IN"
        # an unreadable directory is not an error — the agent still starts
        assert load_user_prompts(_d / "nope") == {}
    for _k in list(USER_PROMPTS):
        PROMPTS.pop(_k, None)
        USER_PROMPTS.pop(_k, None)
    assert active_user_default() is None

    note = "with spec" if spec_present() else "no project.txt — MASTER = MAIN"
    print(f"SYSTEMPROMPT SELF-TEST PASS  "
          f"(MASTER = {len(MASTER):,} chars, {note}; "
          f"{len(PROMPTS)} registered, 0 inline)")
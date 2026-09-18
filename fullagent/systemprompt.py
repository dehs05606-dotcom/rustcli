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
# Prompt registry — add more system prompts here later
# ---------------------------------------------------------------------------
# Every selectable system prompt lives in this one map. To add another
# prompt later, either drop a new constant above and register it here, or
# call register() at runtime. get() resolves a name to its prompt, falling
# back to MAIN so an unknown name can never leave the model promptless.

PROMPTS: dict[str, str] = {
    "main": MAIN,
    "master": MASTER,
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
    note = "with spec" if spec_present() else "no project.txt — MASTER = MAIN"
    print(f"SYSTEMPROMPT SELF-TEST PASS  "
          f"(MASTER = {len(MASTER):,} chars, {note})")
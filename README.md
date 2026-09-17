<div align="center">

```
███████╗██╗   ██╗██╗    ██╗    █████╗  ██████╗ ███████╗███╗ ██╗████████╗
██╔════╝██║   ██║██║    ██║   ██╔══██╗██╔════╝ ██╔════╝████╗██║╚══██╔══╝
█████╗  ██║   ██║██║    ██║   ███████║██║  ███╗█████╗  ██╔██╗██║  ██║
██╔══╝  ██║   ██║██║    ██║   ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║  ██║
██║     ╚██████╔╝██████╗█████╗██║  ██║╚██████╔╝███████╗██║ ╚████║  ██║
╚═╝      ╚═════╝ ╚═════╝╚════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝  ╚═╝
```

<h1>FullAgent <sup><code>v3.1.0</code></sup></h1>

**Advanced Terminal AI Agent — Pure Python, Real Code, World-Class TUI**

<p>
  <a href="https://github.com/dehs05606-dotcom/rustcli"><img src="https://img.shields.io/badge/python-3.9%2B-8be9fd?style=flat-square&logo=python&logoColor=white" alt="python"></a>
  <a href="https://github.com/dehs05606-dotcom/rustcli"><img src="https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows%20%7C%20Termux-50fa7b?style=flat-square" alt="platform"></a>
  <a href="https://github.com/dehs05606-dotcom/rustcli/blob/main/pyproject.toml"><img src="https://img.shields.io/badge/pure--python-%23FFB86C?style=flat-square" alt="pure python"></a>
  <a href="https://github.com/dehs05606-dotcom/rustcli"><img src="https://img.shields.io/github/stars/dehs05606-dotcom/rustcli?style=flat-square&color=f1fa8c" alt="stars"></a>
  <a href="https://github.com/dehs05606-dotcom/rustcli/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-6272a4?style=flat-square" alt="license"></a>
</p>

*`Event-Sourced Kernel` · `Goal Contracts` · `Parallel Crew` · `Adaptive Swarm` · `Self-Healing` · `Temporal Kernel` · `40+ Slash Commands` · `16 Tools` · `Kilo Code`*

<p>
  <a href="#install"><b>Install</b></a> •
  <a href="#-run"><b>Run</b></a> •
  <a href="#-what-it-can-do"><b>Features</b></a> •
  <a href="#-slash-commands"><b>Commands</b></a> •
  <a href="#-layout"><b>Layout</b></a>
</p>

</div>

---

> **Banner Updated ✨ — naya FullAgent banner fir se banaya gaya hai!**  
> Terminal `python main.py` chalate hi ab naya gradient ASCII banner + session info dikhega — aur GitHub README pe yahi banner sabse upar.

```
╭─ FullAgent ── model: Union Alpha ── effort: HIGH ── session a1b2c3d4 ─────╮
│ ❯ type anything… the agent reads, writes, edits files, runs commands     │
╰─ Enter send · Esc+Enter newline · / commands · Ctrl+T models ────────────╯
```

The prompt is a **double-line box** (not full-screen). One application runs
for the whole session, so the box **never disappears** — while the model
works, the bottom border shows a live animated status:

```
╰ ⠹ thinking…  ·  Ctrl+C cancel ────────────────────────────────────────────╯
```

Tokens stream live above the box, tool calls appear as `⚙ name args` with
`✓`/`✗` results, and each turn ends with a stats line (`2.3s · 512→87 tokens`).

Streaming updates the unfinished-line preview on every token; only terminal
redraws are throttled (about 33 fps). Complete lines print without waiting for
the next token. Rapid submissions cannot overlap an active turn, including
final rendering and session saving. Completed file-change output prints
without artificial animation delays.

<details>
<summary><b>🖼️ Terminal Banner Preview (after <code>python main.py</code>)</b></summary>

```
 ◆ FullAgent v3.1.0  ·  advanced terminal AI agent
 event-sourced kernel · goal contracts · persistent crew · self-healing
 ──────────────────────────────────────────────────────────────────
 ❯ model  Union Alpha   effort  high   autonomy  L4   session  a1b2c3d4
   / commands · Ctrl+T models · Ctrl+E effort · /crew background subagents
```

*Full ASCII (6-line) gradient banner dikhega jab terminal ≥80 cols wide ho, warna compact fallback.*

</details>

## Install

Pure Python — no build step, nothing to compile. Python 3.9+ is the only
requirement.

| Platform | Install |
|---|---|
| Linux / macOS | `curl -fsSL https://raw.githubusercontent.com/dehs05606-dotcom/rustcli/main/install.sh \| bash` |
| Termux (Android) | `curl -fsSL https://raw.githubusercontent.com/dehs05606-dotcom/rustcli/main/install-termux.sh \| bash` |
| Any OS | `pip install git+https://github.com/dehs05606-dotcom/rustcli.git` |

## Run

```bash
pip install prompt_toolkit rich requests
export KILO_API_KEY='your-kilo-api-key'
python main.py
```

(ya `python -m fullagent` — dono same hain)

## What it can do

- **Files** — read (with line numbers), write, exact-string edit, list, info,
  create dirs, copy, move, delete
- **Shell** — run any bash command, capture exit code + stdout + stderr
- **Search** — regex search through file contents (ripgrep-style), glob files
- **Web** — fetch URLs, search the web
- **Agent loop** — the model keeps calling tools until the task is genuinely
  done (up to 40 iterations), then summarizes
- **Temporal Kernel** — every message, tool call, result, and cost is an
  immutable, content-addressed event in an append-only log. State is a pure
  fold of that log, so you can rewind, fork, replay, and verify the timeline
- **Goal contracts** — a goal is a structured object with done-criteria
  clauses and anti-clauses; distance-to-done is a computed number, not a vibe
- **Memory** — closed tasks compress into structured episode records; failed
  approaches land in a dead-end ledger and are blocked deterministically
- **Judge** — claims are verified against reality with deterministic
  predicates (exit codes, file checks, regex) — never the model's own word
- **Crew** — Codex-style **persistent** subagents: `spawn_agent` queues
  a background worker and returns instantly (the conversation stays
  responsive), `send_to_agent` iterates on its LIVING context,
  `wait_for_agents` collects results, `close_agent` / `resume_agent`
  manage the lifecycle, `forget_agent` releases a throwaway worker.
  Subagents run **in parallel** on the Swarm — spawn eight and eight are
  in flight. Live progress streams in the prompt border. All subagents
  use Union Alpha through Kilo Code.
- **Swarm** — the parallel substrate that makes the Crew concurrent
  *without the machine feeling it*. A subagent spends almost its whole
  life parked in a socket read waiting for the model, and parked threads
  are free, so those waits fan out with no limit worth caring about. The
  local work between model turns — the only part that can actually load
  a laptop — passes through a **load-adaptive cpu permit** resized twice
  a second from the machine's real run queue: wide open on an idle box,
  down to one the moment the user starts a build. The swarm discounts
  its *own* workers from that reading, so it throttles for other
  people's work and never for its own. Threads are created on demand and
  retire themselves, nothing polls, and workers run niced — an idle
  swarm costs exactly as much as no swarm at all. Measured on a 4-core
  box, 8 subagents × 4 turns: **11.7s → 1.6s wall, and slightly *less*
  total cpu than the serial queue burned.**
- **Call coalescing** — fan-out's dominant cost is not thinking, it is
  *repetition*: five researchers pointed at one module read the same
  files and run the same greps, and parallelism multiplies that waste
  instead of hiding it. So identical calls collapse into one execution,
  and filesystem reads may be served again until any subagent writes —
  at which point every cached read is dropped at once. Writes, commands
  and anything whose answer moves on its own always run for real.
  Measured: 8 subagents × 6 reads over a shared area — **48 real reads →
  3, and 0.47s → 0.05s of cpu.**
- **Provider congestion control** — the net cap is not a guess any more.
  The window opens at the ceiling you chose, **halves on every rate
  limit** and climbs back one slot per window of clean replies. TCP's
  rule, for TCP's reason: shed load fast enough to clear congestion,
  probe back gently enough not to cause it again. A rate limit stops
  being an outage and becomes a few seconds of narrower traffic.
- **A reserved lane for you** — while subagents are in flight, the
  sovereign turn holds a cpu permit no subagent can take. Your own next
  file read never queues behind eight of them grepping, which is exactly
  when a session would otherwise feel slowest: right when it is being
  most productive.
- **Straggler grace** — a batch is only ever as fast as its slowest
  member, and a fifteen-minute timeout is not a bound on that, it is an
  abdication. So once half a batch has reported, whoever is still running
  gets a multiple of the batch's **own median** runtime to wrap up — the
  batch measures itself, so eight quick scans give a small grace and one
  genuinely deep task gives a generous one, and nobody guesses a number
  in advance. A stopped subagent is not killed: its tools are taken away
  and it reports what it already found. Measured: 8 subagents, one of
  which would explore its full 96-step budget — **14.5s → 0.6s, and the
  deep one still reported its findings.**
- **Nothing long is ever lost** — three things can make a subagent run
  long: it circles, its batch moves on, or it uses every tool call it was
  allotted. All three end the same way — tools taken away, full
  conversation kept, one final call for the report — because a budget is
  a reason to stop exploring, not a reason to throw away what was found.
- **Loop detection** — a subagent that asks the same question with the
  same arguments three times is not working, it is circling. Coalescing
  makes the repeat nearly free to *execute*, but the model turn around it
  is not free and neither is your time, so it is stopped and asked to
  report. Exploring many *different* places is the job and is left alone.
- **Partial answers stay honest** — a report from a subagent that was
  wrapped up early is marked **PARTIAL**, in the roster, in the report
  object and in the agent's own instructions. The findings are real but
  may be incomplete, and the agent is told to either say so or continue
  that subagent with its context intact — never to present a partial
  finding as a settled one.
- **Concurrency model checker** (`interleave.py`) — the parallel
  machinery is no longer trusted because its tests pass. A passing
  concurrency test says *one* interleaving worked, and the scheduler
  chose it, not you. So the scheduler's discretion is removed: threads
  under test run one at a time and a strategy decides who runs next at
  every synchronisation point, which turns "did we get lucky" into a
  search. The strategy is **PCT** (Burckhardt et al., ASPLOS'10) — random
  thread priorities plus *d−1* randomly placed priority drops — which
  buys a real lower bound of 1/(n·k^(d−1)) on finding a depth-*d* bug,
  where random scheduling gives no bound at all. Every finding comes back
  with its **schedule**, and the same schedule replays the same execution
  exactly, so a race becomes a regression test instead of an anecdote.

  It earned its keep immediately: pointed at its own condition variable
  it found a **lost wakeup** (publish the waiter *after* releasing the
  lock, and a notify landing in that window is delivered to nobody), and
  it twice caught test scenarios whose observer thread could simply be
  scheduled first and report a failure that never happened. The real
  `Coalescer`, `AdaptiveSemaphore` and `Blackboard` are now verified
  across hundreds of distinct schedules each — code under test is
  instrumented by swapping its `threading` module, so nothing is
  rewritten for the checker's benefit.

  It controls scheduling at synchronisation points, not between arbitrary
  bytecodes, so an unsynchronised data race with no lock anywhere near it
  can still slip through. That limit is stated because a verification
  tool that overstates its coverage is worse than none.
- **Flake hunter** (`/flake`) — "it passes on my machine", answered with a
  name instead of a shrug. Most flakes are not random, they are
  **order-dependent**: another test leaves a global, a monkeypatch, a
  stale singleton, and the victim fails only when that test ran first —
  so rerunning the victim alone passes forever and teaches you nothing.
  Worse, a fixed-order sweep can never see it: the polluter runs first
  every time, the victim fails every time, and the verdict looks
  unanimous. So the hunt runs the suite many times **in shuffled orders,
  in parallel**, separates genuinely non-deterministic tests from
  order-dependent ones, and then **delta-debugs** (Zeller's ddmin)
  everything that ran before the victim until only the tests that
  actually matter are left. The output is not "flaky" — it is a polluter
  by name and a command that reproduces it, path setup included:

  ```
  ✗ test_demo.TestVictim.test_expects_default_mode  [order-dependent]  fails 60%
    reproduce with:
      cd /proj && PYTHONPATH=/proj/tests python3 -m unittest \
        test_demo.TestConfig.test_override_mode \
        test_demo.TestVictim.test_expects_default_mode
    test_demo.TestConfig.test_override_mode leaves state that breaks it
  ```

  Every run is a fresh process (reusing an interpreter would hide the
  very state leakage being hunted), and the hunt is seeded, so it
  replays exactly.
- **You can see it working.** A parallel batch used to be a spinner and a
  rising number of seconds — eight subagents busy and eight subagents hung
  look identical from outside, which is the difference between waiting and
  killing a turn that was two seconds from finishing. Every subagent now
  reports its task, each tool call with the path or pattern it is on, each
  finding as it is shared, and its verdict with time and tool count — live,
  as it happens:

  ```
   ⚡ spawn_agents
   │ 🔎 nova    ▸ audit client.py retry paths
   │ 🔎 atlas   ▸ map event-log writers
   │ 🔎 nova    · read_file fullagent/client.py
   │ 🔎 atlas   · search_files log.append
   │ ◆ nova    shares: retries swallow 429 — see chat_with_retry
   │ ✓ atlas   4s · 3 tools — every writer goes through log.append
   │ ✓ nova    6s · 4 tools · 1 reused — retry path can mask a 429
  ```

  and the border always answers the only question a person has while
  waiting — how much is left, and is it moving:
  `⚡ crew 5/8 done · 2 queued · echo:read_file lyra:grep · 3 shared`

  Roster size and concurrency are also no longer the same number. Spawn as
  many subagents as the work divides into; the swarm runs its limit at a
  time and the rest queue, visibly. No more "spawn eight, wait, spawn four".
- **Blackboard** — the thing that makes parallel subagents a *team*.
  Coalescing removes duplicated calls; it cannot remove duplicated
  *knowledge*, and the expensive duplication is not "both ran the same
  grep" but "both spent four turns working out where to grep". So a
  subagent `share_finding`s what it establishes, and every peer is handed
  it once — deduped by content, capped per turn and in total, and
  explicitly advisory (a peer's report, not verified truth). Measured in
  model turns, which is what actually costs money: **−25% when every
  subagent discovers at the same instant, −52% on realistically
  staggered tasks, −65% when one scout gets there first.**
- **Orchestra** — the Mastermind arranges a batch before anything
  spawns. It resolves every role's **sealed brief** through the vault, so
  a task asking for a role that has no sealed prompt is refused in one
  clear line at planning time instead of becoming a subagent that
  mysteriously does nothing. Then it separates writers that would land on
  the same file into different waves, and lets everything else go at
  once. The waves are a *scheduling* decision, never a correctness one —
  correctness is already the write lock's job — so the path heuristic can
  only ever cost a wave some width, never a file its contents. Every plan
  is sealed into the event log.
- **Focus Mode** — deep work: `/focus 10` arms auto-continuation and the
  agent keeps working turn after turn until the goal closes, progress
  stalls, or the budget pauses. Every continuation decision is a sealed
  kernel event (`focus.tick` / `focus.stop`)
- **Rendered replies** — `/render on` streams the reply live in the
  border and prints the finished answer as rich Markdown
- **Instant triage** — tool errors show the healer's root-cause
  classification right on the result line
- **Workflows** — saved multi-step pipelines (`/workflow run ship`):
  phased orchestration that runs steps one at a time in phase order,
  each step is a real subagent, and optional `expect` predicates block
  the pipeline on failure
- **Audit export** — `/export md|html` writes a self-contained session
  report: timeline, tool stats, judge verdicts, per-model usage
- **Forecast** — `/forecast` projects turns-to-done from measured goal
  velocity and tokens-per-turn (numbers, not vibes)
- **Provider health** — `/health` shows model errors. No alternate model
  or provider is configured, so cross-model failover is unavailable.
- **Notifications** — `/notify <webhook|file:path>` fires kernel events
  (goal closed, focus stop, workflow done…) to your sink
- **Session resume** — `/resume` lists branches/sessions; continue any
  of them with its conversation rebuilt from the event log
- **Turn scorecard** — every turn ends with deterministic quality
  metrics (errors, rework, verified claims) sealed as `turn.scorecard`
- **Live context meter** — the border shows real-time context-window
  usage (`ctx 37%`), and approvals show a real unified diff before you
  press `y`
- **AutoPilot** — the agent decides **for itself** what each turn needs and
  enables it automatically: goal mode when the request is a verifiable
  mission, real-time web when the question needs live data. Every decision
  is logged and shown live
- **Real-time web** — `web_search` hits DuckDuckGo with a Bing fallback and
  stamps every result with its retrieval time, so the agent answers with
  current facts, not stale knowledge

Risky tools (writes, edits, shell, delete, move, copy) ask for approval
**inside the app** — the bottom border becomes an approval bar:
press `y` (yes), `n` (no), or `a` (always). Toggle globally with `/approve`.
The **autonomy ladder** (`/autonomy 0-5`) controls how much the agent may do
without asking, from read-only observer to fully autonomous.

## Keys

| Key | Action |
|---|---|
| `Enter` | send |
| `Esc+Enter` | newline inside the box |
| `/` | slash-command completion menu |
| `Ctrl+T` or `/model` | model selector |
| `Ctrl+E` or `/effort` | effort selector |
| `↑↓` / `PgUp` / `PgDn` / `Tab` / `Home` / `End` | navigate selectors **and** the `/` completion menu |
| `Ctrl+R` | search input history |
| `Ctrl+L` | clear screen |
| `Ctrl+X Ctrl+E` | open input in $EDITOR |
| `Ctrl+C` | cancel a running turn (mid-stream) / clear input |
| `Ctrl+D` | quit |

## Slash commands

`/model` `/effort` `/help` `/history` `/new` `/save` `/approve`
`/reasoning` `/usage` `/clear` `/about` `/exit`

Event-log commands:

- `/goal set <statement> | <clause1> | <clause2>` — set a goal contract
  (prefix a clause with `!` to make it an anti-clause);
  `/goal done <clause>` · `/goal status` · `/goal clear`
- `/autonomy <0-5>` — observer → advisor → assistant → collaborator (default)
  → pilot → autonomous
- `/state` — live projection of the event log (cost, goal, dead-ends, verdicts)
- `/rewind <seq>` — rewind the timeline (bare `/rewind` lists recent seqs)
- `/fork [name]` — branch the timeline and continue on the fork
- `/verify` — verify the event log's Merkle spine
- `/memory` — recent episodes + dead-end ledger
- `/judge <type> <arg>` — deterministic check (`exit_code`, `file_exists`,
  `file_contains`, `file_matches`, `command_output_contains`), or pass a full
  JSON predicate
- `/auto [on|off|status]` — the AutoPilot self-routing brain (on by default)
- `/prompt [main|master|list]` — choose the system prompt: `main` (compact)
  or `master` (the extended 130k+ specification prompt)
- `/mastermind` — the prompt-coherence ledger (sealed prompts, gate,
  composed context, lineage)
- `/dashboard` — live observability: cost, goal, crew, router, spec,
  memory, health in one screen
- `/router` — task classification and routing history; all tasks use Union Alpha
- `/spec` — speculative execution: prefetch stats + hit-rate
- `/recall <question>` — semantic (meaning-based) memory recall
- `/mission [start|tick|list|abandon]` — daemon mission control
- `/heal` — self-healing ledger: root causes captured + healed
- `/skills` — the skill forge: self-authored, safety-gated tools
- `/crew` — persistent subagent roster; `/crew spawn <role> <task>`,
  `/crew send <id> <msg>`, `/crew wait`, `/crew close <id>`, `/crew resume <id>`
- `/focus <1-20>` — deep-work mode: auto-continues until done · `/focus off`
- `/render [on|off]` — rendered-markdown replies (streaming stays live in the border)
- `/council <proposition>` — convene an adversarial debate
- `/analyze <path>` — static analysis: taint flows, complexity, cycles
- `/graph [index|query|impact]` — knowledge graph of code + session
- `/coverage` — real line-coverage ledger (sys.settrace)
- `/fuzz` — property-based fuzzing ledger: crashes + shrunk reproducers
- `/mutate <file> <suite-cmd>` — mutation testing: can your tests catch bugs?

## Effort levels

`low` · `medium` · `high` · `extrahigh` · `ultrahigh` — each raises max
tokens, temperature, and reasoning effort.

## Models & providers

Both **Kilo Code** and **Kios API** are available. The default model is
Union Alpha; saved selections of removed models fall back to it. Custom
providers and `models.json` loading are no longer supported.

- Kilo — Base URL: `https://api.kilo.ai/api/gateway`; context window
  262,144 tokens; maximum completion 131,072 tokens; tool calling
  supported; reasoning parameters omitted
- Kios — Base URL: `https://kiosapi.com/v1`; model `atria-dawn-preview`
  (OpenAI-compatible chat + tool calling)


No API keys are embedded in source. Configure a key before starting:

export KIOS_API_KEY='your-kios-api-key'
python main.py
```

| Provider | Base URL | Model |
|---|---|---|
| Kilo Code | `https://api.kilo.ai/api/gateway` | `stealth/union-alpha` |
| Kios API | `https://kiosapi.com/v1` | `atria-dawn-preview` |

`/models list` shows both models; `/model` or Ctrl+T opens the selector.
The turn always runs on your selected model; failover happens only on a
provider outage (see `/health`).
Save the key instead in `~/.fullagent/kilo_api_key` or
`~/.fullagent/kios_api_key` (permissions `600`); env vars take precedence.
Keep keys outside the repository. Previously embedded credentials may remain
in git history and should be rotated; removing them from current source does
not revoke them.

Config (model, effort, auto-approve) persists in
`~/.fullagent/config.json`; sessions save to `~/.fullagent/sessions/`.
If `~/.fullagent` is not writable, state transparently falls back to
`$TMPDIR/fullagent-<uid>` — the app never crashes on a read-only home.

## Layout

```
main.py            launcher — python main.py
fullagent/
  __init__.py      package
  config.py        providers, models, effort levels, paths
  systemprompt.py  the ONE home of every system prompt (single source)
  mastermind.py    prompt coherence: sealed vault, gate, composer, lineage
  tools.py         16 tools: files, shell, search, real-time web
  client.py        streaming OpenAI-compatible client (SSE, retries, cancel)
  agent.py         agent loop: LLM <-> tools, event-sourced on the kernel
  kernel.py        Temporal Kernel: append-only, content-addressed event log
  memory.py        episodic memory + dead-end ledger (fold-derived)
  goal.py          goal contracts with machine-checkable done-criteria
  judge.py         deterministic verification predicates (no LLM judging)
  team.py          shared subagent substrate — roles, reports, retry, global write lock
  interleave.py    deterministic concurrency model checker — PCT search, exact replay
  flake.py         flaky-test hunter — shuffled parallel sweeps + delta debugging
  swarm.py         adaptive parallel substrate — load governor, elastic pool, net/cpu permits, AIMD window, coalescing
  orchestra.py     the Mastermind arranging a batch — sealed briefs, conflict-free waves
  crew.py          persistent Codex-style subagents — parallel on the swarm, writes serialised
  workflows.py     saved multi-step pipelines — phased orchestration (serial steps)
  autopilot.py     self-routing: auto goal mode / real-time web
  router.py        task classification and routing through Union Alpha
  semantic.py      semantic vector memory — meaning-based recall
  speculate.py     speculative execution — prefetch read-only tool calls
  dashboard.py     live observability — real-time ledger projection
  daemon.py        mission control — resumable long-running missions
  healer.py        self-healing — root-cause capture, fix, retry, lesson
  skills.py        skill forge — self-authored, safety-gated tools
  council.py       adversarial debate — thesis/antithesis + blind judge
  taint.py         static analysis — taint flows, complexity, import cycles
  kgraph.py        knowledge graph — entities + typed relations, impact
  cov.py           real line coverage — sys.settrace measurement
  fuzz.py          property-based fuzzing — generators + crash shrinking
  mutate.py        mutation testing — AST mutants vs the test suite
  tui.py           persistent double-line box, overlays, streaming, approval
  __main__.py      entry point
```

The event log lives at `~/.fullagent/eventlog.jsonl` (override the directory
with `FULLAGENT_HOME`). Each module ships a self-test:
`python -m fullagent.kernel` (and `.memory`, `.goal`, `.judge`,
`.team`, `.crew`, `.autopilot`, `.systemprompt`, `.mastermind`, `.router`,
`.semantic`, `.speculate`, `.dashboard`, `.daemon`, `.healer`, `.skills`,
`.council`, `.taint`, `.kgraph`, `.cov`, `.fuzz`, `.mutate`).

Streaming/UI regressions (no API key needed), from this directory:
`python -m unittest discover -s tests -v`.

## System prompts — one file, one delivery path

Every system prompt the model ever sees lives in **`fullagent/systemprompt.py`**
and nowhere else — the module imports it. Two structural guarantees:

- **Single source of truth.** Edit a prompt in `systemprompt.py` and it
  changes everywhere at once — main agent and all worker roles.
- **One delivery path.** Every message list is built through
  `systemprompt.with_system()`, which guarantees the correct prompt sits at
  position 0 before any request is sent. A model can never be called without
  its prompt, and can never see a stale or partial one.

Two prompts ship in the registry, switchable live with `/prompt`:

| Name | Size | What it is |
|---|---|---|
| `main` | ~1.6k chars | the compact sovereign-agent prompt (default) |
| `master` | **136,928 chars** | MAIN + the full master specification (`project.txt`) embedded — the entire architecture, invariants, subsystem contracts and Goal-Mode grammar in context |

Add more prompts later by dropping a constant in `systemprompt.py` and
registering it in the `PROMPTS` map (or call `register()` at runtime).

## Output budget — 200k tokens

Every effort level requests **200,000 output tokens** (`config.MAX_TOKENS`).
The client clamps requests to Union Alpha's **131,072-token** completion
ceiling and the remaining context window before sending them to Kilo.

## Mastermind — coherence, not coercion

`fullagent/mastermind.py` makes following `systemprompt.py` *inevitable* —
not by telling the model "you must obey", but by making the sealed prompt
the only coherent center of every request. Three cooperating mechanisms,
all deterministic Python:

| Mechanism | What it does |
|---|---|
| **PromptVault** | Every prompt is sealed with a sha256 fingerprint and recorded in the event log. The vault is the only source a model ever reads a prompt from; prompts registered at runtime are sealed on demand, and a changed prompt is re-sealed — no stale copy is ever served. |
| **PromptGate** | The single door to the model. Every request (main agent, scout, worker) passes `gate.dispatch()`, which guarantees `messages[0]` carries the sealed prompt byte-for-byte at the front, re-seats it if anything shadowed or corrupted it (an integrity restore — recorded, never punished), and seals a `prompt.dispatch` lineage event. There is no other way to reach the API. |
| **CoherenceComposer** | Live context (constitution, goal, web mode, memory) is never appended as raw text that could compete with the prompt. It is composed beneath the sealed prompt as one coherent document: each section is framed as *input to* the prompt, provenance-tagged, ordered by authority, deduplicated. The prompt stays the only voice giving direction. |

There is no enforcement layer — the system observes and records
(PromptLineage), it never punishes. Every dispatch is sealed into the
event log; inspect the live ledger with `/mastermind`.

## v3 — eight advanced subsystems

All eight are event-sourced on the same Temporal Kernel: every decision,
prediction, heal, skill and verdict is a sealed event, and every status
view is a pure fold. Nothing keeps private state, so nothing can drift
from the log.

| Module | What it does | Command |
|---|---|---|
| **router.py** | A deterministic difficulty classifier scores each task and records routing decisions. This build routes all tasks to Union Alpha; no alternate models or cost-saving switches are available. | `/router` |
| **semantic.py** | Hippocampus 2.0. Every episode, fact and dead-end is embedded via signed feature hashing (stdlib only, no numpy) and recalled by cosine similarity — "how did we solve a similar problem before?", including remembering what *failed*. The index is a pure projection of the log and refreshes itself. Recall is injected into the memory context section each turn. | `/recall <q>` |
| **speculate.py** | While the model thinks, the agent predicts the read-only calls it will likely make (paths in your message, search verbs, siblings of recent reads) and prefetches them in a background pool. When the model actually asks, the result is served from cache — a hit instead of an execution. Only whitelisted read-only tools can ever be prefetched; a speculative write is structurally impossible. | `/spec` |
| **dashboard.py** | The X-ray: cost, tokens, goal progress bar, crew reports, routing spend, speculation hit-rate, memory counts, verdicts, loop alerts and budget events — one screen, always agreeing with the kernel because it is a fold. | `/dashboard` |
| **daemon.py** | Mission Control. A mission is a queue of steps advanced one tick at a time; every tick checkpoints, so a restart resumes from the last checkpoint (at most one in-flight tick is lost). A step that exhausts its retries BLOCKS the mission visibly — never silently skipped. Wake conditions are deterministic fold predicates. | `/mission` |
| **healer.py** | When a tool fails, the healer captures the error, classifies it against a root-cause taxonomy (16 patterns; unknown is honest, never guessed), and seals the lesson. With a fixer + recheck attached it runs the full loop: fix → re-run the original check → only a green re-run counts as healed. Every tool error in the agent loop is captured automatically. | `/heal` |
| **skills.py** | The self-evolving tool author. A new skill (Python function) passes four gates before it can run: parse → shape (entry fn + docstring) → safety (AST scan: no subprocess/eval/exec/forbidden imports/dunder access/globals) → its own shipped test cases. Passing skills persist to `~/.fullagent/skills/` and register as live tools; failures are sealed with the exact reason. | `/skills` |
| **council.py** | Adversarial debate for high-stakes calls: THESIS argues for, ANTITHESIS argues against and must attack the thesis's strongest point, then a BLIND judge sees only the two anonymised arguments (never the question's framing) and decides on argument strength alone. Verdicts carry winner, confidence and reason. | `/council <q>` |

## v4 — five professional engineering subsystems

Same discipline as v3: pure stdlib, deterministic, no model calls, every
result sealed into the Temporal Kernel as an event, every status view a
pure fold. These are real engineering tools, not estimates.

| Module | What it does | Command / Tool |
|---|---|---|
| **taint.py** | Real static analysis over the AST (not regex): taint tracking from declared sources (input, env, network, file reads) to sinks (eval, exec, subprocess, sql, writes) with the exact propagation path; cyclomatic complexity per function with hotspots; module-level import-cycle detection via iterative DFS. | `/analyze <path>` · tool `analyze_code` |
| **kgraph.py** | The knowledge graph: entities (modules, functions, classes, files, goals, episodes, facts) and typed relations (defines, calls, imports, touches, learned) built straight from the AST and the event fold. Queries are real graph operations — BFS reachability, reverse lookups, and impact sets ("what breaks if I change X?"). | `/graph [index\|query\|impact]` · tools `graph_index`, `graph_query`, `graph_impact` |
| **cov.py** | Genuine line coverage, not an estimate: `sys.settrace` (the same hook `coverage.py` uses) records every executed line of the target while a subject runs, compared against executable lines derived from the AST. The trace only records — never alters control flow — and is always restored. | `/coverage` · tool `measure_coverage` |
| **fuzz.py** | Property-based fuzzing: typed generators (int, str, list, dict, bytes) biased toward boundaries (0, -1, empty, huge, unicode), plus mutated inputs. Crashes are SHRUNK to a minimal reproducer — the difference between "it crashed somewhere" and "here is the smallest input that breaks it." Deterministic under a seed. | `/fuzz` · tool `fuzz_target` |
| **mutate.py** | Mutation testing — answers what tests alone cannot: *can your tests actually catch bugs?* AST NodeTransformers generate real mutants (operator flips, condition negations, broken returns); the suite runs against each. Killed = suite caught it; survived = a real hole. Score = killed / (killed + survived). The original file is always restored. | `/mutate <file> <suite-cmd>` |

---

<div align="center">

**FullAgent** — *Pure Python · Event-Sourced · Self-Healing*

`python main.py` → banner · `~/.fullagent/config.json` → persistence · `FULLAGENT_HOME` → override

</div>

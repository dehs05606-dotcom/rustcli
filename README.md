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

*`Event-Sourced Kernel` · `Goal Contracts` · `Parallel Crew` · `Adaptive Swarm` · `Self-Healing` · `Temporal Kernel` · `40+ Slash Commands` · `17 Tools` · `3 Providers`*

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

Installing from source needs nothing but Python 3.9+ — no compiler, no
build step. For a machine with no Python on it at all, a prebuilt Linux
x86_64 binary is published on the releases page instead.

| Platform | Install |
|---|---|
| Linux / macOS | `curl -fsSL https://raw.githubusercontent.com/dehs05606-dotcom/rustcli/main/install.sh \| bash` |
| Termux (Android) | `curl -fsSL https://raw.githubusercontent.com/dehs05606-dotcom/rustcli/main/install-termux.sh \| bash` |
| Any OS | `pip install git+https://github.com/dehs05606-dotcom/rustcli.git` |
| Linux x86_64, no Python | download `fullagent` from [Releases](https://github.com/dehs05606-dotcom/rustcli/releases/latest), then `chmod +x fullagent && ./fullagent` |

### Building the binary yourself

`./build-binary.sh` freezes the checkout into one file at `dist/fullagent`
with PyInstaller. It is native to the machine that builds it — there is no
cross-compilation, and Termux cannot run a glibc binary at all, so the
source install stays the path on Android.

## Models

| Model | Provider | Context | Tools | Reasoning |
|---|---|---|---|---|
| Union Alpha | Kilo Code | 262k | ✓ | — |
| Atria Dawn Preview | Kios API | 262k | ✓ | — |
| Qwen3.8 Max *(free)* | xKiro | **1M** | ✓ | ✓ |
| DeepSeek V4 Flash 0731 *(paid)* | xKiro | **1M** | ✓ | ✓ |

Switch with `/model`. Keys resolve in this order, so your own always
wins: `<PROVIDER>_API_KEY` in the environment, then
`~/.fullagent/<provider>_api_key`, then whatever ships in `config.py`.

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
  in flight, with live progress for each. Subagents use the session's
  model by default, and `spawn_agents` takes a per-subagent override, so
  scouts can run on a cheap model while the hard wave uses the strong one.
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
- **Provider health** — `/health` shows model errors, and cross-model
  failover is live: three models across three providers, so a 429/5xx
  from one switches the turn to another (once per turn, and never for a
  4xx, which is a request problem rather than an outage)
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
- `/adherence [model|depth|prompt|effort]` — per-clause adherence
  scores, or the comparison table sliced along one dimension
- `/promptlab <a> <b>` — run the scenario set under two prompts and print
  the per-clause deltas (costs live model calls)
- `/mastermind` — the prompt-coherence ledger (sealed prompts, gate,
  composed context, lineage)
- `/dashboard` — live observability: cost, goal, crew, router, spec,
  memory, health in one screen
- `/router` — task classification and routing history
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

**Kilo Code**, **Kios API** and **xKiro** are available. The default model is
Union Alpha; saved selections of removed models fall back to it. Custom
providers and `models.json` loading are no longer supported.

- Kilo — Base URL: `https://api.kilo.ai/api/gateway`; context window
  262,144 tokens; maximum completion 131,072 tokens; tool calling
  supported; reasoning parameters omitted
- Kios — Base URL: `https://kiosapi.com/v1`; model `atria-dawn-preview`
  (OpenAI-compatible chat + tool calling)
- xKiro — Base URL: `https://api.xkiro.com/v1` (the client appends
  `/chat/completions`); models `qwen/qwen3.8-max:free` and
  `deepseek/deepseek-v4-flash-0731`, both tool calling + reasoning

One last-resort xKiro key ships in `config.py`, so that provider works with
nothing to set up. It is readable by anyone who can read this repository —
treat it as a shared demo key, never as a secret. Your own key always wins:

```bash
export KIOS_API_KEY='your-kios-api-key'
python main.py
```

| Provider | Base URL | Model |
|---|---|---|
| Kilo Code | `https://api.kilo.ai/api/gateway` | `stealth/union-alpha` |
| Kios API | `https://kiosapi.com/v1` | `atria-dawn-preview` |
| xKiro | `https://api.xkiro.com/v1` | `qwen/qwen3.8-max:free`, `deepseek/deepseek-v4-flash-0731` |

`/models list` shows every model; `/model` or Ctrl+T opens the selector.
The turn always runs on your selected model; failover happens only on a
provider outage (see `/health`).
Save the key instead in `~/.fullagent/kilo_api_key`,
`~/.fullagent/kios_api_key` or `~/.fullagent/xkiro_api_key` (permissions
`600`); env vars take precedence. A key committed to this repository is
visible to everyone who can read it, and stays in git history after it is
edited out — removing it from current source does not revoke it, so rotate
it at the provider instead.

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
  spec.py          the prompt, cut into addressable sections + BM25 lookup
  promptaudit.py   is the prompt any good as a document — read at 49k
  adherence.py     did the model follow it — clauses decided from the log
  promptlab.py     A/B two prompts on a scenario set, clause by clause
  tools.py         17 tools: files, shell, search, real-time web
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
  toolcontract.py  typed contract per tool — schema, error taxonomy, retry, permission
  toolpolicy.py    capabilities, roles, path confinement, command policy, network allow-list
  dispatch.py      the one call path — validate, gate, approve, trace, time out, retry
  orchestrator.py  plan → execute → verify → roll back, nested sagas, replay by trace id
  policypipeline.py the permission decision as 7 ordered, individually testable stages
  contractmanifest.py contract lock file, additive-vs-breaking checks, drift detection
  recovery.py      every error code mapped to retry / compensate / escalate / abort
  telemetry.py     per-model scorecards, rolling drift windows, routing proposals
  introspect.py    headless runtime introspection + the generated tool reference
  promptrules.py   compiles the system prompt into priority-banded rules
  constitution.py  rules as signed, versioned, append-only policy objects
  guardrail.py     three-stage verification over actions and replies
  compliance.py    per-model adherence scoring with asymmetric hysteresis
  benchmark.py     fixed scenarios scoring how well a model follows the prompt
  audit.py         audit trail, integrity check, dashboard, redacted export
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

Every check, in one command and with no API key: **`./run-checks.sh`** —
compile, all module self-tests, the full `unittest` suite, and the
contract, dispatch and orchestrator invariants. The same script runs in a
terminal and in CI, so a green terminal and a green pipeline mean the
same thing. For just the regression suite:
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
| `main` | ~4.2k chars | the compact sovereign-agent prompt (default) |
| `master` | ~137k chars *(with the spec installed)* | MAIN + the full master specification (`project.txt`) embedded — the entire architecture, invariants, subsystem contracts and Goal-Mode grammar in context |

`project.txt` is **not** in this repository; it is installed beside the
package. When it is absent, `master` is exactly `main` — the prompt never
announces a specification it cannot supply, and `/prompt` says so in its
listing. Check with `python3 -m fullagent.systemprompt`, which prints the
real `MASTER` size.

Add more prompts later by dropping a constant in `systemprompt.py` and
registering it in the `PROMPTS` map (or call `register()` at runtime).

## Output budget — 200k tokens

Every effort level requests **200,000 output tokens** (`config.MAX_TOKENS`).
The client clamps requests to Union Alpha's **131,072-token** completion
ceiling and the remaining context window before sending them to Kilo.

## Mastermind — coherence, not coercion

`fullagent/mastermind.py` makes following `systemprompt.py` *inevitable* —
not by telling the model "you must obey", but by making the sealed prompt
the only coherent center of every request. Four cooperating mechanisms,
all deterministic Python:

| Mechanism | What it does |
|---|---|
| **PromptVault** | Every prompt is sealed with a sha256 fingerprint and recorded in the event log. The vault is the only source a model ever reads a prompt from; prompts registered at runtime are sealed on demand, and a changed prompt is re-sealed — no stale copy is ever served. |
| **PromptGate** | The single door to the model. Every request (main agent, scout, worker, and each subsystem's one-shot call) passes `gate.dispatch()`, which guarantees `messages[0]` carries the sealed prompt byte-for-byte at the front, re-seats it if anything shadowed or corrupted it (an integrity restore — recorded, never punished), and seals a `prompt.dispatch` lineage event. There is no other way to reach the API. |
| **CoherenceComposer** | Live context (constitution, goal, web mode, memory) is never appended as raw text that could compete with the prompt. It is composed as one coherent document: each section is framed as *input to* the prompt, provenance-tagged, ordered by authority, deduplicated. The prompt stays the only voice giving direction. Nothing is ever placed in front of the prompt, and no reminder trails it. Where the framed context *rides* — beneath the prompt, or in one moved slot at the conversation's tail — is [the context slot](#the-context-slot--depth-decay-fixed-where-it-actually-happens). |
| **AdherenceLedger** | `fullagent/adherence.py`. The three above record what the model was *sent*; this one records what it *did*. After every turn each clause — one directive from `systemprompt.py`, turned into a predicate over the turn's own events — is decided from the event log and sealed as a `prompt.adherence` event. |

There is no enforcement layer — the system observes and records, it never
punishes. Every dispatch is sealed into the event log; inspect the live
ledger with `/mastermind`.

### Adherence — measuring whether the prompt is actually followed

A compliance reminder asserts that the directives matter and can never
tell you whether the assertion worked. A clause can. Each one is decided
from recorded facts — no second model call, no LLM judge — and reports
*not applicable*, *held*, or *violated* with its evidence:

| Clause | The directive it decides |
|---|---|
| `verify-before-success` | "Verify everything. Never claim success without evidence." — a success claim after an edit, with no passing check *after the last edit*, is a violation. |
| `read-before-edit` | "Understand first. Read the codebase before making changes." — an `edit_file` on a path never read, in this turn or any earlier one. |
| `cited-paths-exist` | "Never fabricate. Cite real sources, real file paths." — a `path:line` citation naming a file that does not exist. |
| `goal-proof-discipline` | "A clause is only proven when its predicate actually passes." — a `PROVEN: C1` the kernel never sealed. |
| `failures-surfaced` | "Be honest about uncertainty." — tools failed and the reply claims success without mentioning it. |

Precision over coverage is deliberate: a hedge ("this *should* make the
tests pass") is not a claim, and a directive that cannot be decided from
recorded facts is left out rather than guessed at. Run `/adherence` for
the per-clause breakdown, the recent misses, and the directive most worth
rewriting. Nothing in it changes what the model sees.

Every verdict is sealed with its attribution — model, effort, prompt, and
the turn's **tool-loop depth** — so the ledger can be sliced:

```
/adherence model     is one model following the prompt worse than another?
/adherence depth     does adherence decay as a turn gets longer?
```

Depth is the one to watch. The prompt is seated once per turn at position
0 and never re-seated inside the loop, while up to `MAX_TOOL_ITERATIONS`
(200) model calls pile tool output between the directives and the point
where the model writes. Measured with this repo's own estimator and the
4.2k `main` prompt, the directives are ~51% of context at depth 5, 9.4% at
depth 50, and 2.5% at depth 200. If prompt influence decays with depth, it
shows up as a gradient down those bands — and `/adherence depth` says so
in as many words.

### The context slot — depth decay, fixed where it actually happens

Measuring the decay is half of it. The other half is that nothing in the
old design could do anything about it, because the thing being buried was
never the prompt alone — it was everything the prompt was *serving*. The
goal contract, the recalled memory, the constitution: all composed
beneath the sealed prompt at `messages[0]`, all left two hundred tool
iterations behind by the time the model writes the answer they were
supposed to shape.

The fix is not to repeat the directives, and not to append a reminder.
Both are a second voice telling the model to obey the first, and both are
exactly what this system refuses to do. The fix is to stop putting the
live context in the one place in the conversation that a long tool loop
guarantees will be the furthest from the answer.

Slot mode (`context_slot`, default `tail`) keeps the sealed prompt alone
at `messages[0]` — byte-identical for the whole session — and carries the
framed context in exactly **one** system message at the end of the list,
moved there on every model call. Same sections, same framing, same
authority order, same words; only the position changes:

```
  depth    tokens between the live context and the model's next token
             composed (system)      slot (tail)
      5                 1,510                0
     50                15,100                0
    200                60,400                0
```

Moving context is not injecting it. Nothing is added, nothing is
restated, and the document the model reads is byte-for-byte what
`compose()` always produced — which is what makes the fallback safe. A
provider that rejects a trailing system message (`is_message_layout_error`)
degrades *that session* to `context_slot="system"`, folds the slot's
sections straight back beneath the prompt, seals a `prompt.slot_degraded`
event and retries once. The fallback costs position, never content, and
it never rewrites your saved preference for the next provider you run.

### Your own prompt, and no command to run it

Drop a `.md`, `.txt` or `.prompt` file into `~/.fullagent/prompts/` and it
is registered and sealed at startup like a built-in. A file named
`default` selects itself: running your own prompt should not require
knowing a command. Size is not the problem it is usually assumed to be —
a 49k prompt is sealed, dispatched and cached exactly like the 4.2k one,
and lands at `messages[0]` with nothing in front of it either way.

Adherence needs no command either. Every turn's verdicts ride on the turn
itself, and a clause that did not hold prints under that turn's stats —
the directive in `systemprompt.py`'s own words, and the evidence — on the
turn that earned it. That line is addressed to *you*: nothing is appended
to the conversation, nothing is re-sent, and the model is never told it
was graded. `/adherence` is still there for the breakdown, but you should
never have to type it to find out.

### spec_lookup — a long prompt the model can address

Placement fixes a 4k prompt. It cannot fix a 49k one. At iteration 120
the clause governing the edit about to happen is forty thousand tokens
back, and no reordering changes that. The usual answers are to shout (a
compliance banner), to repeat (a reminder tail) or to trim. The first two
are a second voice telling the model to obey the first; the third throws
away what the author wrote.

There is a fourth answer, and it is the one the rest of this codebase
already uses for large state: make it addressable. `fullagent/spec.py`
cuts the sealed prompt at **its own headings** — markdown, numbered,
bold-only or all-caps — and builds a BM25 index over those sections, with
heading terms weighted and a deliberately small stemmer so `claim`,
`claims` and `claiming` are one word. The model reaches it through a
tool:

```
spec_lookup(question="am I allowed to claim this succeeded?")
→ Claiming success
  Never claim success without evidence. A passing check AFTER the last
  edit is evidence; anything else is a hope.
```

Four properties make it worth having:

- **Verbatim.** The section comes back exactly as written. A summarised
  directive is a different directive.
- **Honest misses.** A question the prompt does not address returns
  *"the prompt does not speak to it"*, never the closest thing on file. A
  wrong section returned with full authority is worse than no section.
- **Never stale.** The index is keyed by the prompt's sha256 fingerprint,
  so re-sealing the prompt rebuilds it. An index that outlived its prompt
  would hand the model a rule it was never sent.
- **Not an injection.** Nothing is added to the conversation. The model
  chooses to look, exactly as it chooses to read a file — and the lookup
  lands at the depth where the rule is actually needed.

Retrieval is lexical on purpose (rung 1): no embeddings, no network, no
model call. A prompt lookup that could fail, cost money or vary between
runs would be worse than none. A 49k prompt indexes into hundreds of
sections and answers in microseconds. Every lookup seals a
`prompt.lookup` event, so `/adherence` can tell you whether the model is
actually consulting the prompt or only being sent it.

### Declines are not violations

"The newer models have more safety training, that's why they don't follow
my prompt" is a testable claim, and it is usually wrong — but it should
be settled with data rather than assertion. The ledger detects a decline
(*"I can't help with that"*, *"that goes against my guidelines"*) and
counts it **apart** from the adherence score:

```
declines: 3 turn(s) — the model said it would not do the thing, which is
not the same as not following the prompt
```

The two have opposite fixes: a drifting model needs a clearer prompt, a
declining one needs a different request. A single percentage cannot tell
you which you are looking at, so the ledger does not try. The detector is
tuned to leave ordinary reporting alone — *"I can't read the file because
it does not exist"* is not a decline. Nothing is done with the count
except show it to you: it is never fed back to the model, and there is
nothing here that tries to talk a model out of its own safety behaviour.

### Auditing the prompt itself

Everything above treats the prompt as given and asks what happened to it.
`fullagent/promptaudit.py` asks the question nobody asks, because at 4,000
characters it does not need asking and at 49,000 it is the whole problem:
**is this prompt any good as a document?**

A prompt that size is never written in one sitting. It accretes. A rule
gets added in March and added again in July with different wording. A
section written to fix one failure quietly contradicts a section written
to fix another. One section grows to a fifth of the whole. None of that
is visible to the author, who reads the prompt as intent rather than as
text — and all of it costs adherence directly, because a contradiction is
a coin flip and a redundancy is a dilution.

| Finding | What it means |
|---|---|
| `contradictory` | Two sections about the same thing with opposite polarity — one prohibits where the other requires, and the model has to pick. |
| `redundant` | A family of sections stating one rule. Reported as the **family**, not as every pair inside it: six sections saying the same thing make fifteen pairs, and fifteen lines naming two of the six is not a report anyone can act on. |
| `oversized` | One section large enough to dominate the document — too coarse to retrieve or to cite precisely. |
| `unreachable` | A section whose *rarest* word still appears all over the prompt. It has nothing of its own to be found by, so no question can rank it above the sections it borrows from. This is the section an author swears is in the prompt and the model never seems to apply. |

Similarity is measured over each section's **distinctive** terms, not its
raw vocabulary, and this is load-bearing at scale: measured on raw terms,
two unrelated subsystem sections in a 49k prompt score 0.94 and the report
fills with pairs that have nothing to do with each other; measured on
distinctive terms the same pair scores 0.33 while the genuinely restated
rule still scores 0.77.

The audit runs at startup for a prompt you wrote — no command — and it
never rewrites anything. The findings are candidates for you to read.

**What it does not do**, stated plainly because a tool that hides its
blind spot is worse than one that has none: detection is lexical, so it
finds two sections that argue using the same words. A rule written in
March and flatly reversed in July by an author reaching for entirely
different vocabulary will not be caught. A clean report means *"these
specific pairs are fine"*, never *"this prompt does not contradict
itself"*.

### Measured at 49k, not at 4k

The repo's own prompt is 4.2k, and a number measured there is the wrong
number for someone running a prompt ten times the size. `tests/fixture49k.py`
builds a 49,000-character prompt with the flaws real ones have — a rule
restated in different words, a pair that ended up opposing each other, a
section that grew past the rest, and a block of pure boilerplate — and
`tests/test_49k_prompt.py` runs the whole path against it: seal, dispatch,
place, index, look up, audit. Run it directly for the table:

```
MEASURED ON A 49k PROMPT (tests/fixture49k.py)
  prompt                    49,191 chars    15,566 tokens
  addressable sections         153
  audit findings                10  (31,627 chars contested)

  tokens between the live context and the model's next token:
     depth     composed (system)     slot (tail)
         5                 1,460               0
        50                14,600               0
       200                58,400               0

  spec_lookup                 0.11 ms per call

  contradictory  Destructive operations  +  Shell operations
                 88% shared vocabulary, opposite polarity — one prohibits
                 where the other requires, and the model has to pick
  redundant      Reading before changing  +  Opening files first
                 80% shared vocabulary, same polarity
  unreachable    Changes
                 its rarest word still appears in 25 of 153 sections
```

The tests assert the planted flaws are the ones found, and that the
sections that are fine are left alone.

### Promptlab — testing a prompt change like a code change

Editing a prompt is usually the least reviewed change anyone makes to an
agent: code gets a diff and an exit code, a prompt gets an opinion.
`fullagent/promptlab.py` closes that too. It runs a fixed scenario set
under prompt A, runs it again under prompt B, scores both with the same
adherence clauses, and prints the deltas:

```
  clause                   careless     careful    delta
  verify-before-success          0%        100%   +100pt
  read-before-edit               0%        100%   +100pt
  overall                        0%        100%
  → careful holds 100 points more of the directives it was measured on
```

Each scenario declares which clauses it exists to exercise, and a
scenario that stops triggering them is reported rather than counted as a
pass — a green board over a measurement that never fired is the failure
mode worth guarding against hardest.

One honest limit: cassette replay returns the response recorded for a
given request, and changing the prompt changes the request. Replay buys
determinism, not counterfactuals, so comparing two prompts for real means
calling the model twice. `/promptlab main master` does exactly that and
says up front what it will cost.

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

## The tooling system — contracts, dispatch, orchestration

Tools used to be plain functions called by name. Nothing described what
one accepted or returned, a failure came back as a string the caller had
to read, and two callers could disagree about whether a failed call was
worth repeating. Four modules fix that, and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) is the full account.

**Quickstart.** Everything below runs with no API key:

```python
from fullagent.dispatch import Dispatcher
from fullagent.toolcontract import build_contracts
from fullagent.toolpolicy import ToolPolicy
from fullagent.tools import build_registry

registry  = build_registry()
policy    = ToolPolicy("developer", roots=("/home/me/project",))
dispatch  = Dispatcher(policy=policy, approve=lambda contract, args: True)
dispatch.register_registry(registry, build_contracts(registry))

print(dispatch.negotiate().format())     # what this role can actually run
result = dispatch.call("read_file", {"path": "README.md"})
print(result.ok, result.trace_id, result.attempts)
print(dispatch.format_status())          # calls, failures, retries, denials
```

A multi-step change, planned and reversible:

```python
from fullagent.orchestrator import Expectation, Orchestrator, Plan, Step

plan = Plan("add a config file", (
    Step("write", "write_file",
         {"path": "conf.toml", "content": "debug = true\n"},
         expect=Expectation(path_exists=("conf.toml",)),
         undo_tool="delete_path", undo_args={"path": "conf.toml"}),
    Step("check", "read_file", {"path": "conf.toml"},
         expect=Expectation(contains=("debug",))),
))

orch = Orchestrator(dispatch, approve=lambda plan, review: True)
out = orch.run(plan)
print(out.format())      # a ledger line per step, with what was undone
```

If any step fails its check, the earlier steps are undone in reverse
order and anything that could not be undone is named in the result. If
any step could not have run — a tool that does not exist, an argument
that does not fit the schema, a capability this role does not hold — the
plan is refused whole and nothing runs at all.

| Module | What it settles |
|---|---|
| **toolcontract.py** | One typed contract per tool: input/output schema, permission class, idempotency, timeout, retry policy, and a closed nine-code error taxonomy where retryability belongs to the code, not the call site. Contracts are derived from the registry, never restated, so the copy the model sees and the copy the dispatcher validates cannot drift. |
| **toolpolicy.py** | Deny by default. Five capabilities, four roles, path confinement checked *after* `resolve()` so a symlink cannot walk out of the tree, a destructive-command policy, per-tool call ceilings, and a network allow-list whose SSRF blocklist runs first — an allow-list entry cannot unblock a link-local or private host. |
| **dispatch.py** | The single call path: validate → policy → approval → run → classify → retry → validate the output. A handler runs in a worker thread and is abandoned when it overruns its budget. Only an idempotent call is ever repeated. Every call carries a trace id and lands in the event log. |
| **orchestrator.py** | Plan, execute, verify, undo. A plan is validated whole before its first step runs; approval is asked once for the plan rather than once per step; a failed step rolls the completed ones back in reverse and reports by name anything it could not reverse. |

### The platform layer

Six pieces sit on top of the contract/dispatch/orchestrator stack. Each
exists because a specific state was reachable that should not have been.

| Module | The state it makes unreachable |
|---|---|
| **policypipeline.py** | A permission decision whose reason is prose nobody can count, and an early-returning check that lets an *ask* hide a later *deny*. Seven ordered stages, each testable alone, each emitting a typed code and structured facts the audit dashboard counts. A stage that crashes denies. |
| **contractmanifest.py** | A tool whose schema, lock file, docs and tests disagree. `contracts.lock.json` pins every contract; changes are classified additive or breaking by a rule written down once; `--check` fails on eight kinds of drift. |
| **recovery.py** | Retrying a validation error forever, or rolling back a write that may never have landed. Every one of the nine error codes maps to a tested strategy. A playbook can never overrule the taxonomy. |
| **orchestrator.py** (extended) | A sub-plan that fails leaving its own children applied; a failing step undone when nobody can know whether it ran; a post-mortem that depends on what somebody remembers. Nested sagas, playbook-driven disposition, and `replay(log, trace_id)` that computes nothing. |
| **telemetry.py** | A model silently swapped underneath you. Routing returns a *proposal* carrying its numbers; `in_effect` stays on the current model until a named human accepts. |
| **introspect.py** | Documentation that is confidently wrong. `docs/TOOLS.md` is generated from the registry and `--check-docs` fails when they differ. |

```bash
python -m fullagent.introspect                 # registry, stages, contracts, metrics
python -m fullagent.introspect tools --json    # machine-readable
python -m fullagent.contractmanifest --check   # drift across registry/lock/docs/tests
```

The prompt rule compiler takes **text**, not a path: `compile_prompt(name,
text)` and `ratify_prompt(name, text)` ingest any prompt without a code
change, and a test asserts neither module imports a prompt source.

### The self-verifying layer

Six more pieces sit on top of the platform layer. Where the platform
layer makes a bad *state* unreachable, this one makes a bad *change*
unmergeable.

| Module | The state it makes unreachable |
|---|---|
| **invariants.py** | A guarantee that is only prose. 30 machine-checkable claims — preconditions, postconditions, state-machine closure, totality, consistency — run in CI on every commit. The report separates claims **proved** by enumerating every case from ones **checked** by sampling, because calling a sample a proof is the same lie as calling a green suite one. It found three real defects on its first run. |
| **governance.py** | A breaking contract change slipping in unversioned. Every tool carries a semver beside (never inside) its digest; the gate computes the version a change *requires* and refuses one that is not versioned and carried by a migration. `Dispatcher(migrate=...)` applies the shim before validation, so the new schema is the only one enforced. |
| **provenance.py** | A post-mortem that depends on what somebody remembers. Every policy verdict, call, plan, step, outcome, recovery and rollback becomes a content-addressed, HMAC-signed node with causal edges. Derived from the sealed log only — and where no cause was sealed it records a `Gap` rather than inventing one. |
| **riskgrade.py** | A static risk label that stops describing reality. Grades come from observed failure rates, refusals and escalations — and **evidence may tighten a grade but never loosen it below the contract's declared floor**, however much of it there is. Every grade that moves produces a human-readable change report. |
| **consensus.py** | A verifier whose bug is invisible to itself. A second, deterministic strategy re-checks every reply by a different route. Agreement releases or blocks; **a disagreement is held, never decided** — `unsure` never counts as a pass, a strategy that crashes becomes `unsure`, and nothing leaves a hold without a recorded `Resolution` naming who decided. |
| **regressiongate.py** | A rule change that quietly stops the rules working. Six governed surfaces are fingerprinted; the adherence benchmark runs two arms — compliant turns must keep **holding**, violating turns must keep being **caught** — and a regression in either direction blocks with a typed reason that says what would clear it. Rolling drift windows catch the slide no single commit trips. |

```bash
python -m fullagent.invariants --check         # every stated guarantee, checked
python -m fullagent.governance --gate          # contract versions and migrations
python -m fullagent.regressiongate --status    # what is governed, and its digest
python -m fullagent.regressiongate --check     # the two-armed benchmark + drift
python -m fullagent.regressiongate --record ME # re-baseline after a rule change
```

All five are steps in `./run-checks.sh`, so a rule change with no
benchmark behind it does not reach a green build.

Two things this layer deliberately does **not** claim. An invariant
proves what it states, not what you hoped it stated — which is why the
proved/sampled split is printed rather than summed. And the regression
gate runs scripted turns, so it regression-tests the *rule set*, not the
model: "do these rules still catch what they used to catch" is a
question CI can answer, "does the model obey them" is not.

## Security model

The short version: **a call that violates the prompt or the machine's
permissions does not execute**, and everything that did execute is on the
record. What no client-side layer can do — this one included — is make a
model obey a prompt; token generation happens elsewhere.

- **Deny by default, in seven stages.** A tool with no capability entry
  gets no capability; an unregistered tool is denied by a stage of its
  own with a typed reason, so the audit can tell "never heard of it"
  apart from "this role lacks the capability". A destructive or outward-facing call with no approval hook
  is refused, because a session that cannot ask a human must not answer
  on the human's behalf. An approval hook that raises is not consent.
- **Least privilege.** Four roles, from `untrusted` (read only) to
  `operator`. A role can hold a capability *on condition of asking*.
- **Path confinement after resolution.** Paths are resolved first, then
  tested against the roots, so a symlink pointing out of the tree is out
  of the tree.
- **Network.** Non-HTTP schemes, cloud metadata addresses, loopback and
  RFC1918 ranges are blocked before any allow-list is consulted.
  Credentials in a URL are discarded before the host is read.
  `web_fetch` re-checks **every redirect hop**, because the policy layer
  only ever sees the URL the model asked for and a 302 arrives after
  that check.
- **Secrets.** The signing key for policy objects lives at
  `~/.fullagent/constitution.key`, mode 0600, and is never logged.
  `audit.py` redacts by pattern at export time.
- **Tamper evidence, not tamper proofing.** The event log is hash-chained
  and `audit.verify()` re-hashes it. Anyone who owns the machine can
  rewrite the log; they cannot rewrite it without verification failing.

Known limits, stated because a security section that lists only strengths
is marketing: the shell is governed by a **deny-list**, so a destructive
command nobody wrote a pattern for is allowed — the capability check, the
ceilings and the approval hook are the layers standing behind it. The
policy layer refuses calls; it does not sandbox the process, so a tool
runs with the agent's own privileges. A tool that overruns its timeout is
abandoned, not killed.

To report a security problem, open an issue with the steps to reproduce.

## Contributing

```bash
git clone https://github.com/dehs05606-dotcom/rustcli
cd rustcli
pip install -r requirements.txt
./run-checks.sh                 # must be green before you push
```

House rules, all of them enforced by `run-checks.sh`:

1. **Every module carries its own `__main__` self-test.** `run_selftests.py`
   runs all of them as subprocesses; a new module without one fails the
   suite. The self-test is the module's proof, and it must print a line
   ending in `PASS`.
2. **Pure stdlib inside `fullagent/`**, except the three packages in
   `requirements.txt`. The agent has to run on Termux.
3. **Cross-module behaviour goes in `tests/`.** A module self-test covers
   a module alone; anything that crosses a seam belongs in a test file.
4. **A new tool needs four edits** — the function and its registry entry
   in `tools.py`, a `TOOL_CAPABILITIES` entry in `toolpolicy.py`, a
   `_TRAITS` entry in `toolcontract.py` if the defaults are wrong, and
   tests in `tests/test_tooling_system.py`. The steps are spelled out in
   [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#adding-a-tool).
5. **No new schema keyword without a validator.** `validate()` implements
   a documented subset of JSON Schema and a test asserts no tool uses a
   keyword outside it — an unchecked keyword reads as a guarantee.
6. **A new tool needs its lock file and docs refreshed**:
   `python -m fullagent.contractmanifest --write` and
   `python -m fullagent.introspect --write-docs`. Both are committed, and
   `run-checks.sh` fails when either is stale.
7. **Report what actually happened.** Paste real command output in a pull
   request. A test that was skipped is not a test that passed.

---

<div align="center">

**FullAgent** — *Pure Python · Event-Sourced · Self-Healing*

`python main.py` → banner · `~/.fullagent/config.json` → persistence · `FULLAGENT_HOME` → override

</div>

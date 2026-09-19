# Architecture — the agent tooling system

This document describes how a tool call travels through FullAgent: what
describes it, what is allowed to stop it, what records it, and what can
undo it. It covers `toolcontract.py`, `toolpolicy.py`, `dispatch.py`,
`orchestrator.py` and `audit.py`, and how they sit next to the prompt
compliance stack (`promptrules.py`, `constitution.py`, `guardrail.py`,
`compliance.py`).

It is written for someone about to add a tool, change a permission, or
work out why a call was refused.

## The shape of it

```
                  model asks for a tool call
                             │
          ┌──────────────────▼──────────────────┐
          │  Orchestrator   (plan-time refusal) │   optional: only for
          │  plan → review → run → verify → undo│   multi-step work
          └──────────────────┬──────────────────┘
                             │
          ┌──────────────────▼──────────────────┐
          │  Dispatcher                         │
          │   1. validate against the contract  │
          │   2. policy pipeline (7 stages)     │
          │   3. approval (default deny)        │
          │   4. run, bounded by a timeout      │
          │   5. classify the failure           │
          │   6. retry, only if idempotent      │
          │   7. validate the output            │
          └──────────────────┬──────────────────┘
                             │
          ┌──────────────────▼──────────────────┐
          │  the tool function in tools.py      │
          └─────────────────────────────────────┘

        every stage appends to the event log (kernel.py),
        which audit.py reads back and redacts for export
```

The rule that keeps this honest: **the core never imports tool
internals**. `dispatch.py` imports the contract layer and the policy
layer; it reaches a tool only through a handler it was handed at
registration time. `toolcontract.py` imports `toolpolicy.py` for the
capability names and nothing else. A tool therefore cannot special-case
the dispatcher, and the dispatcher cannot special-case a tool.

## Layer 1 — the contract (`toolcontract.py`)

A `ToolContract` is the whole truth about one tool:

| field | what it settles |
| --- | --- |
| `name`, `description` | what the model is told |
| `input_schema` | what arguments are accepted, validated before the call |
| `output_schema` | what the tool may return, validated after |
| `permission` | the capabilities the caller must hold |
| `idempotency` | `idempotent`, `non_idempotent` or `unsafe` |
| `timeout_seconds` | the budget, enforced by the dispatcher |
| `retry` | which codes may be repeated, how often, how patiently |
| `errors` | the codes this tool can produce |
| `destructive`, `outward_facing` | whether it needs approval |

Contracts are **derived** from the registry in `tools.py`, not written a
second time. Two copies of a schema drift, and the copy shown to the
model would win while the copy the dispatcher validates would be the one
that is wrong. `destructive` is derived the same way, from the registry's
own `risk` grade, so a tool added as `RISK_CONFIRM` tomorrow needs
approval tomorrow without anyone remembering to edit a list here.

There are currently 17 tools, 17 contracts, and 10 that need approval:
`apply_patch`, `copy_path`, `delete_path`, `edit_file`, `live_shell`,
`move_path`, `run_command`, `web_fetch`, `web_search`, `write_file`.

### Schema validation

`validate()` implements a deliberate **subset** of JSON Schema: `type`,
`properties`, `required`, `items`, `enum`, `const`, `pattern`,
`additionalProperties`, `minimum`/`maximum`, `minLength`/`maxLength`,
`minItems`/`maxItems`, and the annotation keywords (`title`,
`description`, `default`, `examples`) which are carried but not checked.
The subset exists because
the agent ships on three dependencies and runs on Termux; adding
`jsonschema` for this would be the largest dependency in the project.

The subset is *enforced*, not merely documented: `unsupported_keywords()`
walks a schema and reports any keyword `validate()` would ignore, and a
test asserts every registered tool's schemas use only keywords we
actually check. An unchecked keyword is worse than no schema, because it
reads as a guarantee.

### The error taxonomy

Nine codes, closed by design, each with a fixed retryability:

| code | meaning | retryable |
| --- | --- | --- |
| `E_VALIDATION` | the input did not satisfy the schema | no |
| `E_PERMISSION` | policy refused the call | no |
| `E_NOT_FOUND` | the target does not exist | no |
| `E_CONFLICT` | the target is not in the expected state | no |
| `E_TIMEOUT` | the call exceeded its budget | yes |
| `E_UPSTREAM` | a network or subprocess failure | yes |
| `E_RESOURCE` | a ceiling, quota or disk limit | no |
| `E_CANCELLED` | the caller stopped it | no |
| `E_INTERNAL` | a defect in the tool itself | no |

Retryability belongs to the **code**, not the call site. A `RetryPolicy`
may narrow what it repeats; it can never widen it. Two callers
disagreeing about whether to repeat a call is how a side effect happens
twice.

Two functions keep the taxonomy connected to reality:

- `classify(exc)` maps an exception that escaped a tool to a code. This
  matters more than it looks: labelling everything `E_INTERNAL` would
  tell a caller "this is a defect, do not retry" about a socket that
  merely closed, and no retry policy could ever fire.
- `from_error_text(text)` reads this repo's older `"ERROR: ..."` return
  convention as a typed error. The tools predate the taxonomy and report
  failure by returning a string; left alone, every one of those would
  reach the caller as a *successful* call whose value happens to describe
  a failure. The prefix is matched at position 0 only, so output that
  merely mentions the word is untouched.

## Layer 2 — the policy (`toolpolicy.py`)

Deny by default. Five capabilities — `fs.read`, `fs.write`, `fs.delete`,
`net.fetch`, `proc.exec` — and four roles:

| role | holds |
| --- | --- |
| `untrusted` | `fs.read` |
| `readonly` | `fs.read`, `net.fetch` |
| `developer` | `fs.read`, `fs.write`, `net.fetch`, `proc.exec` |
| `operator` | all five |

A role may also mark a capability *ask*: held, but only after a human
says so for this call.

Three checks run beyond the capability:

**Path confinement.** Every path argument is `resolve()`d first and then
tested against the configured roots. Resolving first is the whole point:
a symlink pointing out of the tree *is* out of the tree, and a check done
before resolution would be checking the name rather than the target.

**Command policy.** A deny-list, not an allow-list: `DESTRUCTIVE_RE`
matches the small set of shell commands that destroy things: `rm -rf`,
`git push --force`, `git reset --hard`, `git clean -fd`, `drop`/`truncate
table`, `mkfs`, `dd if=`, a redirect onto a raw disk device, `shutdown`,
`reboot`, and `chmod -R 777`. A role
with `allow_destructive_commands=False` is denied outright; a role with
it set is asked. Per-tool **call ceilings** cap how many times a tool may
run in one session, so a loop cannot grind through a thousand commands
before anyone notices.

A deny-list is weaker than an allow-list and is a known limit of this
layer — see *What this does not do*. It is what a general coding agent
can actually use: a developer role whose shell was restricted to an
enumerated set of commands could not do its job.

**Network allow-list.** `host_allowed(url, allowed)` runs the scheme
check and the SSRF blocklist **before** the allow-list, so an allow-list
entry cannot unblock a link-local, loopback or private host. Blocked:
non-HTTP schemes (`file:`, `gopher:`, `ftp:`, `data:`, `dict:`), the
cloud metadata addresses (`169.254.169.254`, `metadata.google.internal`),
loopback, and the RFC1918 ranges including the awkward `172.16/12`.
Credentials in the URL are discarded before the host is read, because
`https://example.com@169.254.169.254/` is a request to the metadata
service.

`web_fetch` then re-checks **every redirect hop** against the same rule.
The policy layer only ever sees the URL the model asked for; a 302
arrives after that check, and following it blindly would hand an attacker
with control of any allowed page a way to the metadata endpoint.

## Layer 3 — dispatch (`dispatch.py`)

One `Dispatcher` owns registration, discovery, negotiation, gating,
execution, tracing and metrics. The order of the call path is not
negotiable:

1. **Validate** the arguments against the contract. A bad call is refused
   before policy sees it, so a typo is reported as a typo rather than as
   a permission problem.
2. **Policy**: capability, path confinement, command shape, host.
3. **Approval**, if the contract is destructive or outward-facing, or the
   policy returned *ask*. With no approval hook, the answer is no — a
   session that cannot ask a human must not answer on the human's behalf.
   An approval hook that raises is not consent.
4. **Execute** in a worker thread, joined with a timeout. A tool that
   overruns is **abandoned, not killed**: Python cannot safely kill a
   thread, so the honest thing is to stop waiting and say so. The call
   returns `E_TIMEOUT` with `abandoned: true`, and the thread is a
   daemon, so it cannot hold the process open.
5. **Classify** any escaped exception into the taxonomy.
6. **Retry** only when the contract says the call is `idempotent` *and*
   the policy admits the code. A `non_idempotent` or `unsafe` tool is
   never repeated, whatever its retry policy says.
7. **Validate the output** against the contract's output schema.

### Capability negotiation

`negotiate()` answers "what can this session actually run", and
`schemas()` advertises exactly that set to the model. A tool advertised
but denied at call time wastes a model turn and teaches it that its tools
are unreliable; a test asserts the two never disagree.

### Tracing

Every call carries a `TraceContext` — `trace_id`, `span_id`,
`parent_span`. A child span is minted per call, so a multi-step
orchestration is one trace with one span per step, and every audit record
and error object carries the same `trace_id`.

## Layer 4 — orchestration (`orchestrator.py`)

This is **not** `workflows.py`. A workflow's steps are tasks given to a
model ("implement the parser"); an orchestration's steps are tool calls
with concrete arguments. No model is consulted anywhere in
`orchestrator.py`.

A `Plan` is a goal and an ordered tuple of `Step`s. Each step names its
tool, its arguments, an `Expectation` (a deterministic check on what it
produced), and optionally an *undo*: a tool and arguments that reverse
it.

**Plan-time refusal.** `review()` validates the whole plan before the
first step runs: every tool exists, every argument satisfies its schema,
every capability is one the role holds, no duplicate step ids, and every
declared undo is itself a valid call. A plan that cannot run is refused
whole. Half-running a plan and then abandoning it leaves the machine in a
state nobody planned for.

**Approval once, for the plan.** The review lists every step needing
approval and a human approves the plan. A human approving twelve prompts
in a row is not approving, they are clicking.

**Honest rollback.** When a step fails — including a step whose call
succeeded but whose expectation did not hold — completed steps are undone
in reverse order, the failing step included, since a write that landed
and then failed its check is the ordinary case. A step that declared no
undo is reported as `irreversible` in the result and in the ledger. It is
never skipped silently and never described as rolled back. A plan
containing a destructive step with no compensation is refused at review
time unless the plan says `accept_irreversible=True`.

**The ledger.** Every attempt is appended to a ledger and sealed in the
event log: `orchestrator.plan` before anything runs, then
`orchestrator.step` (the intent) and `orchestrator.step.done` (the
outcome) per step, `orchestrator.rollback` per compensation, and
`orchestrator.done`. The intent is sealed *before* the call, so a crash
between the two still leaves a record of what touched the file.

## The policy pipeline (`policypipeline.py`)

The permission decision is seven ordered stages, each a small object with
a name and one question:

```
manifest -> capability -> path-confinement -> command-policy
         -> network-allow-list -> ceiling -> ask-capability
```

Each stage takes a `Request` and nothing else -- no policy object, no
session -- which is what makes one testable on its own. Before this, a
path-confinement test was also, silently, a capability test, because the
call had to get past the capability check to reach the path check.

Each stage returns a `Rationale` with a sentence for a person and `facts`
for a machine. A decision carries the whole list, allowing stages
included, because "why was this permitted" is as much an audit question
as "why was this refused". `audit.py` counts denials by stage and by
typed code, which is a question an audit trail of English strings cannot
answer.

Two rules:

- **A deny anywhere beats an ask anywhere.** The old single-function
  policy returned at the first objection, so a command that merely
  *asked* returned before the ceiling and the host were consulted. Asking
  a human to approve a call that a later stage would refuse teaches them
  that approving is how you make the machine stop complaining.
- **A stage that crashes denies.** Failing closed is the only safe
  reading of "we do not know".

`ToolPolicy.evaluate()` is the collapse of `evaluate_detailed()` to a
single verdict. They cannot disagree, because one is computed from the
other, and a test asserts it across allow, ask and deny.

## Contract evolution (`contractmanifest.py`)

"Derived, never restated" is a property that decays quietly. Nobody
notices the second copy of a schema the day it appears; they notice six
weeks later when the model sends an argument the dispatcher rejects. So:

- **`contracts.lock.json`** is every contract, serialised in a stable
  order with a digest per tool and one for the set, committed to the
  repo. It is the answer to "what did this agent promise its tools would
  do, as of this commit". Descriptions are excluded and error lists are
  sorted, so prose edits and tuple reordering do not move the digest.
- **Compatibility is classified, not merely detected.** Additive: a new
  optional argument, a new error code, a new tool, a dropped requirement,
  newly needing approval. Breaking: a new *required* argument, a removed
  property, a narrowed type, a newly required capability, a removed tool,
  weakened idempotency, or a **lost** approval requirement.
- **Drift** is checked across registry, lock file, docs and tests:
  `unlocked-tool`, `stale-lock-entry`, `contract-changed`, `no-capability`,
  `stale-trait`, `restated-schema`, `untested-tool`, `undocumented-tool`.
  `run-checks.sh` fails on any of them. The first run found six tools no
  test mentioned; they have tests now.

## Recovery playbooks (`recovery.py`)

The taxonomy says what went wrong and whether a call may be repeated.
That is not the same as knowing what to do, and the gap is where agents
behave badly. Every code maps to one of four strategies -- `retry`,
`compensate`, `escalate`, `abort` -- with the conditions under which it
applies and the fallback when they do not.

Two rules decide almost every case, and both are about what is *not*
known:

- **Repeatability gates retry.** A timeout on a non-idempotent call is an
  escalation, not a retry: a timeout means the call was abandoned rather
  than observed, so it may have completed. Repeating could double the
  effect; undoing could undo something that never happened.
- **A compensation you do not have is not a plan.** `compensate`
  downgrades to `escalate` rather than reporting a rollback that did not
  occur.

A playbook can never overrule the taxonomy: a policy naming a final code
as retryable is refused, not honoured. `PLAYBOOKS` and `ERROR_CODES` are
asserted to have the same keys, so a tenth code cannot be added without
someone deciding what to do about it.

## The transaction engine

`orchestrator.py` grew three things:

**Nested sagas.** A `Step` may hold a whole `Plan` instead of a call. Its
compensation is its children's, run in reverse. A sub-saga that fails
rolls its own children back *before* returning, so by the time the parent
sees the failure the child is already in a known state. `review()`
recurses, so a plan whose third sub-step names a tool that does not exist
is refused before the first write lands.

**Playbook-driven disposition.** The earlier steps always unwind -- that
is the saga. What happens to the *failing* step comes from its error
code. A verification failure compensates, because the call ran and we
watched it. An unrepeatable upstream failure is marked `escalated` and
deliberately left alone, with "needs a human" in the result.

**Deterministic replay.** `replay(log, trace_id)` rebuilds a run from
sealed events alone, in seq order, computing nothing. Two replays of the
same log are the same replay, which is the property that makes one usable
as evidence. Step outcomes are sealed after their disposition, so the log
carries the status the run actually ended on.

## Model telemetry (`telemetry.py`)

`compliance.py` keeps one number per model; `benchmark.py` scores models
on fixed scenarios. Telemetry joins them into a scorecard, and adds two
things:

- **Rolling windows**, in order, rather than one window against a frozen
  baseline. A model that fell off a cliff and one that has been sliding
  for a fortnight both read as "drifted" against a baseline; only a
  series tells them apart, and they want different responses. A trailing
  partial window is dropped rather than averaged in.
- **Proposals, never switches.** `routing()` returns a `RoutingProposal`
  carrying the numbers that produced it. `in_effect` stays the *current*
  model until `accept(proposal, who)` is called with a name. There is no
  code path in the module that changes which model runs. A router that
  switched on its own would make every later result unattributable.

A model with fewer than one full window of observations is reported but
never recommended, and the proposal says why in words.

## The self-describing runtime (`introspect.py`)

```
python -m fullagent.introspect              everything, as text
python -m fullagent.introspect tools --json machine-readable
python -m fullagent.introspect --write-docs regenerate docs/TOOLS.md
python -m fullagent.introspect --check-docs fail if they are stale
```

`describe()` returns the registry, the active policy stages and roles,
contract digests and lock state, the recovery playbooks, live dispatcher
metrics and model scorecards, as plain JSON-serialisable data. Headless:
no session, no API key, no TUI.

[`docs/TOOLS.md`](TOOLS.md) is **generated** from the registry, and
`--check-docs` fails when the file and the registry disagree. Stale docs
are worse than no docs, because people believe them; this makes the stale
state unreachable rather than merely discouraged.

## The proof layer (`invariants.py`)

Every layer above states guarantees in prose. This one states them as
predicates and runs them, so compliance is a checked property rather
than a comment.

An `Invariant` is a claim about one module, of one kind:

| kind | what it asserts |
| --- | --- |
| `precondition` | what a call may assume on entry |
| `postcondition` | what it guarantees on exit |
| `closure` | a state machine reaches only declared states |
| `totality` | a function answers for every input |
| `consistency` | two views of the same thing agree |

The report distinguishes an invariant **proved** by enumerating every
case from one **checked** by sampling, and prints both counts. Blurring
them would be the same mistake as calling a green test suite a proof:

```
INVARIANTS — 30 claim(s), 3601 case(s): 23 proved exhaustively, 7 checked by sampling
```

`run-checks.sh` runs `--check`, which exits nonzero on a broken claim.
The layer earned its place on its first run by breaking three:

1. `declares-dispatch-codes` — twelve filesystem tools omitted
   `E_TIMEOUT` from their contracts while running under a
   dispatcher-enforced timeout, so a real outcome was undeclared.
2. `escalate-implies-human` — `recovery.plan()` returned ESCALATE in a
   context with nobody to escalate to. The fix (`_reachable`) resolves
   every downgrade against what the context can actually carry out, and
   found three more instances of the same bug in the retry branches.
3. `failure-is-accounted` — a nested saga's rollback results were
   discarded, so a compensation that failed inside a sub-plan vanished
   from the run result.

None of these was visible to the test suite, because each was a gap
between what a module promised and what it did, not a case anyone had
thought to write a test for.

## Contract governance (`governance.py`)

`contractmanifest.py` classifies a contract change as additive or
breaking. Governance decides what that obliges you to do about it.

- Every tool carries a semantic version in `VERSIONS`. The version rides
  *beside* the digest in the lock file, never inside it, so a version
  bump and a behaviour change are distinguishable in a diff.
- `verdicts()` computes the version each tool is *required* to be at,
  given how it changed: breaking needs a major, additive a minor.
- `gate()` refuses a change that is not versioned and carried — a
  breaking change with no major bump, or a major bump with no migration
  registered.
- A `Migration` is a pure function from the old argument shape to the
  new one. `MigrationRegistry.adapter()` produces the callable
  `Dispatcher(migrate=...)` takes, applied **before** validation, so the
  new schema is the only schema the dispatcher ever enforces and the
  `ToolResult` records which shim ran.

The gate is in `run-checks.sh`. A breaking change with no migration path
does not reach a green build.

## Provenance (`provenance.py`)

Every policy verdict, dispatched call, plan, step, outcome, recovery
decision, rollback and routing proposal becomes a node in a graph with
causal edges: `governed-by`, `caused-by`, `part-of`, `decided-by`,
`undid`.

Two properties matter more than the queries:

- **It is derived, never duplicated.** Nodes are built only from the
  events in `SOURCE_EVENTS`; a node's id is a content hash over its
  event. There is no second write path, so the graph cannot drift from
  the log.
- **A missing cause is a `Gap`, not an invention.** An allowed call has
  no sealed policy verdict behind it, because the policy only seals
  non-allow decisions. The graph records that absence instead of
  inferring a verdict that was never taken.

Nodes are HMAC-signed, so `verify(key)` names any node whose content no
longer matches its signature. `explain(node_id)` walks the chain back
into sentences a person can read in a post-mortem.

## Adaptive risk grading (`riskgrade.py`)

A tool's risk is declared by its contract (`destructive`,
`outward_facing`, `idempotency`) and observed from what it has actually
done: failure rate, error codes, policy refusals, human declines,
escalations.

The rule that makes this safe to automate:

> **Evidence may tighten a grade. It may never loosen one below the
> declared floor.**

Twenty observations are enough to raise a grade. Two hundred flawless
ones are still not enough to lower `delete_path` below `critical` — the
report says so in the tool's own reasons rather than silently holding
the line:

> observed low over 250 calls, still held at the declared critical: a
> tool that has not yet done damage is not a tool that cannot

Every grade that moves produces a `Change` with a human-readable line
and a sealed `riskgrade.changed` event.

## Consensus audit (`consensus.py`)

The guardrail checks a reply by binding each decidable rule to a
predicate. A bug in that binding is invisible to itself: a predicate
that never fires looks exactly like a rule that was never broken.

So a second strategy checks the same reply against the same rules by a
different route — from the rule text and the reply's surface rather than
from the predicate bindings — and the auditor compares them.

- Agreement on `pass` releases. Agreement on `fail` blocks.
- **A disagreement is never resolved by picking a side.** Not by
  majority, not by trusting the primary, not by silently trusting the
  stricter one. It produces a `Disagreement` naming what each strategy
  concluded and the specific question between them, and the reply is
  **held** until something records a `Resolution` saying who decided and
  why. Calling `resolve()` with nothing records the conservative
  resolution, which blocks — so holding is a written decision rather
  than something that happens in the dark.
- `unsure` never counts as a pass, and a strategy that raises becomes
  `unsure`. A verifier that fails open is not one.
- `re_reason()` states the disagreement and asks for the evidence that
  would settle it, without telling the model which side to take. A
  re-reasoning prompt that leads the witness is an override with extra
  steps.

The shipped second strategy is **deterministic**: no model call, no API
key, runs in every test. A verifier that needs the network is absent
exactly when things are going wrong. `ModelStrategy` wraps any callable
so a second *model* can be added as a third opinion where one is
available — an addition to the deterministic pair, never a replacement,
because two models agreeing is not evidence that either read the rules.

## The regression gate (`regressiongate.py`)

Every other layer checks a run. This one checks a *change*.

**What is governed is fingerprinted, not remembered.** Six surfaces are
hashed separately — the prompt text, the ratified constitution (which
makes the rule *compiler* governed too), the clause predicates, the
policy pipeline and its roles, the recovery playbooks, and the contract
lock. A change to any of them moves a digest, so "did anything governed
change?" is a comparison rather than a claim in a commit message. A
clause whose directive is unchanged but whose predicate was swapped
still shows.

**The benchmark has two arms.** Scoring only good behaviour measures
nothing: a clause set that was deleted scores a perfect 100%. So every
scenario runs twice —

- the **compliant** arm must keep holding. A clause that starts
  objecting to correct work is a false positive, and false positives are
  how a rule set gets switched off by the people it annoys.
- the **violating** arm must keep catching. A clause that stops
  objecting to the violation it exists for is a false negative, and a
  false negative is indistinguishable from compliance in every report
  downstream of it.

Both arms weigh equally in the score. A regression in either direction
blocks, with the clause named.

Failures are typed, and every reason carries what would clear it:
`no-baseline`, `ungoverned-change`, `measurement-broken`,
`coverage-lost`, `false-positive`, `false-negative`, `score-regression`,
`rule-dropped`, `drift-cliff`, `drift-slide`, `constitution-tampered`.

Drift windows sit alongside the single-commit comparison: the sealed
history of benchmark scores is cut into rolling windows, and a cliff or
a slide across them blocks even when the newest run alone looks
acceptable. A rule set that loses a little on every commit never trips a
single-commit check.

`regression.baseline.json` holds the recorded baseline, attributed to
whoever recorded it. `--record` and `--check` go through the same code
path, so a baseline can never be taken under a different fingerprint
than the one it will be compared against.

**What this gate does not measure.** It runs scripted turns, so it
regression-tests the *rule set*, not the model. The question it answers
is "do these rules still catch what they used to catch", not "does the
model obey them". The second question is telemetry's, is answered
against live traffic, and cannot be answered in CI at all.

## How this meets the compliance stack

The prompt compliance stack decides *whether an action is allowed by the
system prompt*; the tooling system decides *whether it is allowed by the
machine*. They are independent on purpose, and `agent.py` consults both:
the tool policy and the guardrail run before the autonomy ladder, so a
call has to pass the prompt's rules and the machine's permissions.

Neither can make a model obey a prompt — token generation happens outside
this code, and no client-side layer changes that. What they can do, and
do: a call that violates either does not execute, and a reply that
violates the prompt does not survive.

## Observability

Everything lands in the append-only, hash-chained event log
(`kernel.py`). `audit.py` reads it back:

- `records()` — one `AuditRecord` per action, with the policy that
  governed it and the trace id.
- `verify()` — re-hashes the chain and reports tampering. This is
  tamper-**evident**, not tamper-proof: anyone who owns the machine can
  rewrite the log, but they cannot rewrite it without the verification
  failing.
- `dashboard()` — counts by category, denial reasons, approval rates.
- `export(fmt)` — JSON or CSV, with secrets redacted at export time by
  pattern.

`Dispatcher.metrics` holds the live counters: calls and failures per
tool, mean duration, retries, denials, approvals asked and refused, and
errors by code. `format_status()` prints them.

## Adding a tool

1. Write the function in `tools.py`. Return a string. Raise for failure,
   or return `"ERROR: ..."` — both become typed errors.
2. Register it in `build_registry()` with its JSON Schema and, if it
   destroys or changes something, `risk=RISK_CONFIRM`.
3. Add its capability to `TOOL_CAPABILITIES` in `toolpolicy.py`. A tool
   with no entry gets no capability and will be denied, which is the
   right default but a confusing one to debug.
4. Add a `_TRAITS` entry in `toolcontract.py` if the defaults are wrong —
   the defaults are non-idempotent, 60s, no retry.
5. Add tests to `tests/test_tooling_system.py`. The existing classes are
   organised by tool family.
6. Run `python -m fullagent.contractmanifest --write` and
   `python -m fullagent.introspect --write-docs` to refresh the lock file
   and the generated reference.
7. Run `./run-checks.sh`. The drift check names anything still missing.

## What this does not do

- The shell is governed by a deny-list, so a destructive command nobody
  thought to write a pattern for is allowed. The capability check
  (`proc.exec`), the ceilings and the approval hook are the layers that
  stand behind it; the deny-list alone is not a boundary.
- It does not sandbox the process. A tool runs with the agent's own
  privileges; the policy layer refuses calls, it does not contain them.
  A `proc.exec` role can run a command that reads outside the roots.
- It does not kill a runaway thread, only stop waiting for it.
- It does not verify a tool's *semantics*, only its contract. A tool that
  returns plausible nonsense returns a contract-valid string.
- `from_error_text` is a heuristic over a string convention. It is
  strictly better than reporting failure as success, and strictly worse
  than a tool that raises.
- Replay reconstructs what was *sealed*, which is not the same as what
  happened. A crash between a step's intent and its outcome leaves the
  intent on the record and the outcome unknown -- that is the honest
  state, and replay reports it as incomplete rather than guessing.
- A scorecard measures compliance with the compiled rules, which cover
  about a third of the prompt. A model can score well and still ignore
  the advisory two thirds.
- The rule compiler takes text, not a path, so any prompt ingests without
  a code change. It cannot tell you whether that prompt is any good.
- An invariant proves what it states, not what you hoped it stated. The
  report separates the 23 claims proved by enumeration from the 7
  sampled, and a sampled claim is evidence, not a proof.
- The regression gate runs scripted turns. It cannot tell you anything
  about a model's behaviour, and a green gate is not evidence about any
  model.
- `verify()` on a constitution with no policies passes under any key.
  An empty rule set has nothing to sign, which is correct and also means
  "the signatures verify" says nothing on its own.
- Risk grades are derived from what the log recorded. A tool that has
  never been called has only its declared floor, which is the
  conservative answer and not an informed one.

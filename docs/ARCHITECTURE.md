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
          │   2. policy: capability, path, host │
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
6. Run `./run-checks.sh`.

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

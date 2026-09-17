"""CREW — Codex-style persistent subagent lifecycle.

The Crew is the ONLY way to execute a subagent — PERSISTENT, addressable
agents with a real lifecycle, exactly like a lead engineer managing a
roster of specialists:

    spawn(task, role)   queue a subagent, returns immediately
    send(id, message)   follow-up message into a living subagent's context
    wait(ids, timeout)  block until the named subagents reach a verdict
    close(id)           retire a subagent (releases its slot)
    resume(id)          bring a closed subagent back with its full context

Each CrewAgent is a REAL agent: its own role brief, its own tool
whitelist, its own multi-step tool loop, its own message history that
SURVIVES follow-up messages — so you can iterate on a subagent instead
of re-spawning from scratch. Agents run IN PARALLEL on the swarm (see
swarm.py). Spawning returns at once; wait() collects the verdicts. Up
to MAX_AGENTS agents may sit on the roster.

Parallel, but the machine never feels it. A subagent spends almost its
whole life parked in a socket read waiting for the model — that costs
nothing, so those waits fan out freely. The local work BETWEEN model
turns (reading files, grepping, running a command) is the only part
that can actually load a laptop, so it passes through the swarm's
adaptive cpu permit, which is resized every half second from the real
machine load: wide open on an idle box, down to one the moment the
user needs their cores back. Threads are created on demand and retire
themselves; nothing polls; workers run niced. Eight subagents at once
therefore finish in roughly the time ONE used to take, while the TUI
stays exactly as responsive as it was with no subagents at all.

Hard rules (mechanical, same discipline as the rest of FullAgent):
  * Subagents run CONCURRENTLY, but every write still passes through
    the SAME single global lock as every other subsystem (invariant I7)
    — parallel reasoning, serialised mutation. Two subagents can never
    touch the world at the same instant.
  * Local work is METERED, never merely unleashed: the swarm's cpu
    permit is the throttle, and it answers to the machine's load, not
    to the crew's appetite.
  * Every lifecycle transition is sealed in the event log: crew.spawn,
    crew.progress, crew.message, crew.done, crew.closed, crew.resumed.
    The crew history is replayable and auditable.
  * A failing subagent never kills the crew; it lands as an error report
    and can be sent a follow-up or closed.
  * Follow-ups reuse the subagent's full conversation — context is the
    dividend of persistence.
"""

from __future__ import annotations

import functools
import itertools
import json
import statistics
import threading
import time
from collections import deque
from typing import Callable
from ._foundation import get_logger

_log = get_logger("crew")
from dataclasses import dataclass, field

from . import systemprompt
from .config import PROVIDERS, model_by_id
from .kernel import EventLog, fold
from .team import (ROLES, DEFAULT_ROLE, MAX_WORKER_STEPS, MAX_WORKERS,
                   _WRITE_LOCK, chat_with_retry, parse_worker_final)
from .blackboard import Blackboard
from .swarm import CPU_CEILING, Swarm
from .tools import Tool, build_registry, parse_tool_arguments

MAX_AGENTS = MAX_WORKERS   # roster ceiling (queued + active agents)
MAX_PARALLEL = MAX_WORKERS  # how many subagents may be in flight at once
MAX_SEND_STEPS = 40        # tool-loop budget per follow-up message
# wait() is zero-spin: it parks on a Condition that a finishing worker
# wakes. Only a caller that passes should_cancel needs periodic checks,
# and then only at this granularity.
CANCEL_POLL_SECONDS = 0.05
WAIT_POLL_SECONDS = CANCEL_POLL_SECONDS   # back-compat alias
# How long a subagent may wait for a cpu permit before doing its local
# work anyway. Admission control is a courtesy to the machine, never a
# correctness gate — a busy box must not fail a subagent.
CPU_PERMIT_TIMEOUT = 30.0

# A subagent that issues the SAME tool call with the SAME arguments this
# many times inside its recent history is not working, it is circling.
# Coalescing already makes the repeat nearly free to execute — but the
# model turn around it is not free, and neither is the user's time.
LOOP_REPEATS = 3
LOOP_WINDOW = 12           # how many recent calls count as "recent"
# The grace a straggler gets, as a multiple of the batch's median. Also
# the floor on that grace, so a fast batch never guillotines a subagent
# that simply drew a harder task.
STRAGGLER_FACTOR = 3.0
STRAGGLER_MIN_GRACE = 8.0

# Tools whose answer is a pure function of the filesystem right now. Two
# subagents asking the same question of the same state get one execution
# and one answer — and the answer may be served again until something
# writes. Nothing that mutates, and nothing whose result depends on when
# you asked, is ever in here.
CACHEABLE_TOOLS = frozenset({
    "read_file", "list_dir", "file_info", "search_files", "glob_files",
})
# Idempotent but not OURS to cache: the world moves these under us, so
# overlapping calls still collapse into one, but a finished result is
# never replayed.
SINGLEFLIGHT_TOOLS = frozenset({"web_search", "web_fetch"})
# Anything here means the filesystem may have changed: drop every
# cached read on the spot. This is also the set that must hold the ONE
# global write lock (invariant I7) — see MUTATING_TOOLS below, which is
# the same set for the same reason.
INVALIDATING_TOOLS = frozenset({
    "write_file", "edit_file", "create_directory", "run_command",
    "delete_path", "move_path", "copy_path",
})
# Tools that can change the world, and therefore must never run
# concurrently with each other.
#
# Keyed on the TOOL, never on the role. A role's `writes` flag says what
# we LABELLED it, not what it can do: tester, analyst, debugger and
# optimizer are all writes=False and all hold run_command, which can do
# anything at all — install packages, check out a branch, run a build
# that rewrites the tree. Gating the lock on the role let all four of
# them mutate the tree with no lock at all, alongside a coder that was
# holding it. Serially that was invisible; in parallel it is a data
# race, and the lock exists precisely to make that impossible.
MUTATING_TOOLS = INVALIDATING_TOOLS

# Codex-flavoured callsigns for the crew roster.
_CALLSIGNS = ("nova", "atlas", "echo", "lyra", "orion", "vega", "iris",
              "argo", "sable", "kepler", "juno", "helix", "drift", "onyx",
              "piper", "quill")

AGENT_STATES = ("running", "done", "blocked", "error", "closed")

_ROLE_ICON = {"researcher": "🔎", "coder": "👨‍💻", "tester": "🧪",
              "reviewer": "🧐", "analyst": "📊", "architect": "🏛️",
              "debugger": "🐞", "optimizer": "⚡", "refactorer": "🧹",
              "documenter": "📝", "devops": "🛠️", "integrator": "🔗",
              "planner": "🗺️"}


@dataclass
class CrewAgent:
    """One persistent subagent. The message history is the point: it
    survives follow-ups, so iteration never starts from zero."""
    id: str
    nickname: str
    role: str
    task: str
    state: str = "running"      # running | done | blocked | error | closed
    summary: str = ""
    error: str = ""
    messages: list = field(default_factory=list)   # full conversation
    files_touched: list = field(default_factory=list)
    tool_calls: int = 0
    reused: int = 0             # tool calls answered without re-running
    stopped_by: str = ""        # "" | "loop" | "deadline" — why it wrapped up
    shared: int = 0             # findings this subagent gave the crew
    learned: int = 0            # findings it was handed by its peers
    board_cursor: int = 0       # how far it has read the shared board
    # monotonic instant after which this subagent should stop exploring
    # and report what it has. 0.0 means "no deadline".
    soft_deadline: float = 0.0
    # rolling signatures of recent tool calls, for loop detection
    recent_calls: deque = field(
        default_factory=lambda: deque(maxlen=LOOP_WINDOW))
    tokens_in: int = 0
    tokens_out: int = 0
    spawned_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    pending_messages: list = field(default_factory=list)
    model_id: str = ""          # per-agent model override ("" = crew default)
    read_only: bool = False     # tool restriction survives follow-ups/resume
    # Per-agent mutex. The Crew's queue serialises worker passes, but
    # the *sovereign* thread (TUI, workflow executor, council) can call
    # `crew.send`, `crew.close`, `crew.resume` while the worker is
    # between two `_run_loop` steps. Without this lock, the worker
    # reads `agent.state == "running"` and the sovereign flips it to
    # `"closed"` a microsecond later — the worker enqueues a follow-up
    # run for an already-retired agent, and the user sees the agent
    # ignore close() for one extra loop iteration. Worse: the
    # `pending_messages` list is shared, so a `pop(0)` from the worker
    # interleaves with a sovereign `append`, silently losing follow-ups.
    mutex: threading.RLock = field(default_factory=threading.RLock)

    def rearm(self) -> None:
        """Clear everything that bounded the PREVIOUS run.

        A follow-up is a new piece of work, and three pieces of state
        would otherwise leak into it and silently make it a no-op:

          soft_deadline   an instant already in the past. The very first
                          step of the follow-up would see it, decide the
                          batch had moved on, and finalize immediately —
                          the follow-up would do nothing at all, which is
                          worse than an error because it looks like an
                          answer.
          recent_calls    signatures from the last run. Asking a subagent
                          to re-check the file it just read would trip
                          loop detection on the first call.
          stopped_by      the report would keep saying PARTIAL after the
                          follow-up had completed the work.
        """
        self.soft_deadline = 0.0
        self.stopped_by = ""
        self.recent_calls.clear()

    @property
    def icon(self) -> str:
        return _ROLE_ICON.get(self.role, "◆")

    @property
    def elapsed_ms(self) -> int:
        end = self.finished_at or time.time()
        return int((end - self.spawned_at) * 1000)

    def to_dict(self) -> dict:
        return {"id": self.id, "nickname": self.nickname, "role": self.role,
                "task": self.task, "state": self.state,
                "model": self.model_id,
                "summary": self.summary[:600], "error": self.error[:300],
                "files_touched": self.files_touched[:12],
                "tool_calls": self.tool_calls, "reused": self.reused,
                "stopped_by": self.stopped_by,
                "shared": self.shared, "learned": self.learned,
                "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "elapsed_ms": self.elapsed_ms}


class CrewError(RuntimeError):
    """Raised for invalid lifecycle operations (unknown id, spawn at
    capacity, send to a closed agent)."""


class Crew:
    """Persistent, addressable subagents over a shared EventLog.

    `chat` is injectable for tests: chat(provider, model, effort,
    messages, schemas, timeout) -> StreamResult. Production uses the
    rate-limit-hardened chat_with_retry from team.py.
    """

    def __init__(self, log: EventLog, provider, model, effort,
                 mastermind=None, max_agents: int = MAX_AGENTS,
                 chat=None, max_parallel: int = MAX_PARALLEL,
                 cpu_ceiling: int = CPU_CEILING, swarm: Swarm | None = None,
                 straggler_min_grace: float = STRAGGLER_MIN_GRACE,
                 board: "Blackboard | None" = None) -> None:
        self.log = log
        self.provider = provider
        self.model = model
        self.effort = effort
        self.mastermind = mastermind
        self.max_agents = max(1, int(max_agents))
        # Shared findings. Without it, eight subagents rediscover the
        # same five facts; with it, the first one to work something out
        # hands it to the rest. Optional so the Crew stays usable — and
        # testable — with no collaboration at all.
        self.board = board
        # the floor on a straggler's grace: no batch, however quick, may
        # guillotine a subagent that simply drew a harder task
        self.straggler_min_grace = max(0.0, float(straggler_min_grace))
        # NOTE: assigned AFTER self.swarm exists — the default chat is
        # bound to this swarm's congestion window.
        self._chat = chat
        self._agents: dict[str, CrewAgent] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()       # protects the roster
        self._names = itertools.cycle(_CALLSIGNS)
        self._counter = 0
        # role tool whitelists carved from the main registry
        registry = build_registry()
        self._toolsets: dict[str, dict[str, Tool]] = {}
        for role, spec in ROLES.items():
            self._toolsets[role] = {n: registry[n] for n in spec["tools"]
                                    if n in registry}
        # PARALLEL execution on the swarm. Subagents overlap freely
        # while they wait on the model; their LOCAL work is metered by
        # the swarm's load-adaptive cpu permit, and their writes still
        # serialise through _WRITE_LOCK (invariant I7).
        self.swarm = swarm or Swarm(max_parallel=max_parallel,
                                    cpu_ceiling=cpu_ceiling, name="crew")
        # Every transition OUT of "running" pulses this. wait() parks on
        # it instead of polling, so a hundred idle waiters cost nothing.
        self._settled = threading.Condition()
        if self._chat is None:
            # The retry loop swallows 429s, so without this binding the
            # congestion window would never learn about a rate limit and
            # would sit at its ceiling for the whole session — the
            # multiplicative decrease would be code that can never run.
            self._chat = functools.partial(
                chat_with_retry,
                on_rate_limit=lambda _e: self.swarm.net_window.penalize())

    def _notify_settled(self) -> None:
        """Wake everything blocked in wait(). Never called while holding
        self._lock — the two locks are never nested."""
        with self._settled:
            self._settled.notify_all()

    def _enqueue(self, agent: CrewAgent, read_only: bool,
                 max_steps: int) -> None:
        self.swarm.submit(self._serve_one, agent, read_only, max_steps,
                          label=f"{agent.id}:{agent.role}")

    def _serve_one(self, agent: CrewAgent, read_only: bool,
                   max_steps: int) -> None:
        """One subagent's whole pass, running in parallel with its peers.
        Never raises: a failing subagent lands as an error report."""
        try:
            if agent.state == "closed":
                return              # retired while still queued
            self._run_loop(agent, read_only, max_steps)
        except Exception as e:  # noqa: BLE001 — never kill a swarm thread
            agent.state = "error"
            agent.error = f"{type(e).__name__}: {e}"
            agent.finished_at = agent.finished_at or time.time()
            self.log.append("crew.done", agent.to_dict(),
                            actor=f"crew:{agent.id}")
        finally:
            self._notify_settled()

    # -- lifecycle -------------------------------------------------------------

    def spawn(self, task: str, role: str = DEFAULT_ROLE, name: str = "",
              context: str = "", read_only: bool = False,
              model_id: str = "") -> CrewAgent:
        """Put a subagent to work; returns IMMEDIATELY.
        Agents run IN PARALLEL on the swarm — spawn eight and eight are
        in flight — and wait() collects the verdicts. Their local work
        is throttled to whatever headroom the machine actually has, so
        eight at once is no heavier than one.

        model_id optionally overrides the model THIS subagent uses
        (Codex-style per-agent model override) — e.g. a cheap fast model
        for grunt work, the strongest model for the hard piece. Unknown
        ids fall back to the crew default with a sealed note."""
        task = str(task or "").strip()
        if not task:
            raise CrewError("cannot spawn a subagent without a task")
        if role not in ROLES:
            role = DEFAULT_ROLE
        with self._lock:
            live = sum(1 for a in self._agents.values()
                       if a.state == "running")
            if live >= self.max_agents:
                raise CrewError(
                    f"crew is at capacity ({self.max_agents} agents "
                    f"queued/running) — wait for one to finish or close "
                    f"one")
            self._counter += 1
            agent_id = f"crew-{self._counter}"
            nickname = str(name or "").strip() or next(self._names)
            while any(a.nickname == nickname
                      for a in self._agents.values()):
                nickname = f"{nickname}-{self._counter}"
            agent = CrewAgent(id=agent_id, nickname=nickname, role=role,
                              task=task, read_only=bool(read_only))
            override = model_by_id(str(model_id or "")) if model_id else None
            if override is not None:
                agent.model_id = override.id
            self._agents[agent_id] = agent
            self._order.append(agent_id)

        user = (f"Shared context:\n{context}\n\nYOUR TASK: {task}"
                if context else f"YOUR TASK: {task}")
        if self.mastermind is not None:
            agent.messages, _ = self.mastermind.gate.dispatch(
                f"worker:{role}", agent.messages)
        else:
            systemprompt.with_system(agent.messages,
                                     systemprompt.worker(role, self.max_agents))
        agent.messages.append({"role": "user", "content": user})

        self.log.append("crew.spawn",
                        {"id": agent.id, "nickname": agent.nickname,
                         "role": role, "task": task[:300],
                         "read_only": bool(read_only),
                         "model": agent.model_id or self.model.id},
                        actor="sovereign")
        self._enqueue(agent, read_only, MAX_WORKER_STEPS)
        return agent

    def send(self, agent_id: str, message: str,
             interrupt: bool = False) -> CrewAgent:
        """Send a follow-up into a subagent's LIVING context.

        done/blocked/error agents start a new loop iteration with the
        message appended (full history preserved). A running agent gets
        the message queued — it is delivered the moment the current loop
        finishes (interrupt=True clears the agent's pending summary so
        the follow-up takes priority in the next reply)."""
        agent = self._require(agent_id)
        message = str(message or "").strip()
        if not message:
            raise CrewError("cannot send an empty message")
        # The full critical section runs under the agent's mutex. Holding
        # the lock blocks the worker from observing a half-written state
        # (e.g. messages appended before state flips to "running"), and
        # in the "agent running" branch it serialises the
        # pending_messages.append with the worker's eventual pop.
        with agent.mutex:
            if agent.state == "closed":
                raise CrewError(
                    f"agent {agent_id} is closed — resume it first")
            self.log.append("crew.message",
                            {"id": agent_id, "chars": len(message),
                             "interrupt": bool(interrupt)},
                            actor="sovereign")
            if agent.state == "running":
                agent.pending_messages.append(message)
                return agent
            if interrupt:
                agent.summary = ""
            agent.messages.append({"role": "user",
                                   "content": f"FOLLOW-UP: {message}"})
            agent.state = "running"
            agent.error = ""
            agent.rearm()
            # keep the spawn-time tool restriction — a read-only subagent must
            # never gain write tools through a follow-up
            self._enqueue(agent, agent.read_only, MAX_SEND_STEPS)
        return agent

    def wait(self, ids: list[str] | None = None,
             timeout: float = 30.0,
             should_cancel: "Callable[[], bool] | None" = None,
             straggler: float = 0.0) -> dict[str, str]:
        """Block until the named subagents (default: all) leave the
        running state, or the timeout lands. Returns {id: state}.
        
        should_cancel: optional callback — if it returns True, wait()
        returns immediately and force-stops all running agents.

        straggler: if set, once HALF the batch has reported, whoever is
        still running is given this multiple of the batch's own median
        runtime to wrap up. A batch is only ever as fast as its slowest
        member, so one subagent still exploring long after the rest have
        answered IS the cost of the batch — and the fixed timeout that
        would otherwise bound it (fifteen minutes) is not a bound, it is
        an abdication. The median is the right yardstick because the
        batch measures itself: eight quick scans give a small grace, one
        genuinely deep task gives a generous one, and nobody has to guess
        a number in advance. A stopped subagent still reports what it
        found — see _finalize.

        ZERO-SPIN: the waiter parks on a Condition that each finishing
        subagent pulses, so waiting on eight parallel agents for ten
        minutes costs no cpu at all. Only a should_cancel hook needs the
        wait chopped into CANCEL_POLL_SECONDS slices, and even then it
        is twenty cheap predicate checks a second, not a busy loop."""
        targets = [self._require(i) for i in ids] if ids else list(
            self._agents.values())
        deadline = time.monotonic() + max(0.0, timeout)
        cancelled = False
        graced = False
        with self._settled:
            while True:
                if should_cancel is not None and should_cancel():
                    cancelled = True
                    break
                if all(a.state != "running" for a in targets):
                    break
                if straggler > 0 and not graced and len(targets) > 1:
                    graced = self._grace_stragglers(targets, straggler)
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._settled.wait(left if should_cancel is None
                                   else min(CANCEL_POLL_SECONDS, left))
        if cancelled:
            # FORCE STOP — user pressed Esc/Ctrl+C. Done OUTSIDE the
            # condition so force_stop's own locking can never nest.
            self.force_stop()
        return {a.id: a.state for a in targets}

    def _grace_stragglers(self, targets: list[CrewAgent],
                          factor: float) -> bool:
        """Once half the batch has reported, put a clock on the rest.

        Returns True when the grace was applied, so it is applied once
        and never keeps sliding forward — a deadline that moves every
        time you look at it is not a deadline.
        """
        settled = [a for a in targets if a.state != "running"]
        if len(settled) * 2 < len(targets):
            return False
        median = statistics.median(a.elapsed_ms for a in settled) / 1000.0
        grace = max(self.straggler_min_grace, factor * median)
        cutoff = time.monotonic() + grace
        stragglers = 0
        for agent in targets:
            with agent.mutex:
                if agent.state == "running" and not agent.soft_deadline:
                    agent.soft_deadline = cutoff
                    stragglers += 1
        if stragglers:
            self.log.append("crew.grace",
                            {"stragglers": stragglers,
                             "median_s": round(median, 2),
                             "grace_s": round(grace, 2)},
                            actor="sovereign")
        return True

    def force_stop(self) -> None:
        """Forcefully stop ALL running/queued agents. Called on Esc/Ctrl+C.
        Sets state to 'closed' so the executor skips them."""
        with self._lock:
            for agent in self._agents.values():
                if agent.state == "running":
                    agent.state = "closed"
                    agent.error = "force-stopped by user"
        # Drop everything still queued on the swarm so it never starts.
        # Agents already mid-flight see state == "closed" at their next
        # loop step and stand down there.
        dropped = self.swarm.drain()
        self.log.append("crew.force_stop",
                        {"reason": "user_interrupt", "dropped": dropped},
                        actor="sovereign")
        self._notify_settled()

    def close(self, agent_id: str) -> CrewAgent:
        """Retire a subagent. It keeps its history (resume() can bring
        it back) but refuses sends while closed and frees no slot —
        only running agents occupy slots."""
        agent = self._require(agent_id)
        if agent.state == "closed":
            return agent
        prev = agent.state
        agent.state = "closed"
        self.log.append("crew.closed",
                        {"id": agent_id, "prev_state": prev},
                        actor="sovereign")
        self._notify_settled()   # a wait() on this agent is satisfied now
        return agent

    def forget(self, agent_id: str) -> bool:
        """Drop a finished subagent from the roster entirely.

        close() retires an agent but KEEPS its conversation so resume()
        can bring it back — the right default for an agent a human is
        working with. Batch callers (the daemon, the task market, a
        compiled wave) spawn throwaway subagents by the dozen, and every
        one of those holds a full message history: without a way to let
        them go, a long session's roster grows without bound. forget()
        is that way. It refuses to touch a RUNNING agent, and the event
        log keeps the agent's whole life either way, so nothing
        auditable is lost.
        """
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None or agent.state == "running":
                return False
            self._agents.pop(agent_id, None)
            if agent_id in self._order:
                self._order.remove(agent_id)
            agent.messages = []
        self.log.append("crew.forgotten",
                        {"id": agent_id, "state": agent.state},
                        actor="sovereign")
        return True

    def resume(self, agent_id: str) -> CrewAgent:
        """Bring a closed subagent back (state 'done', full context),
        so it can receive follow-ups again."""
        agent = self._require(agent_id)
        if agent.state != "closed":
            return agent
        agent.state = "done" if not agent.error else "error"
        agent.rearm()
        self.log.append("crew.resumed", {"id": agent_id},
                        actor="sovereign")
        return agent

    # -- queries ----------------------------------------------------------------

    def get(self, agent_id: str) -> CrewAgent | None:
        return self._agents.get(agent_id)

    def list(self) -> list[CrewAgent]:
        return [self._agents[i] for i in self._order]

    def running(self) -> list[CrewAgent]:
        return [a for a in self.list() if a.state == "running"]

    def _require(self, agent_id: str) -> CrewAgent:
        agent = self._agents.get(agent_id)
        if agent is None:
            known = ", ".join(self._order) or "none"
            raise CrewError(f"unknown subagent {agent_id!r} (known: {known})")
        return agent

    def status(self) -> dict:
        agents = self.list()
        return {"total": len(agents),
                "running": sum(1 for a in agents if a.state == "running"),
                "done": sum(1 for a in agents if a.state == "done"),
                "blocked": sum(1 for a in agents if a.state == "blocked"),
                "error": sum(1 for a in agents if a.state == "error"),
                "closed": sum(1 for a in agents if a.state == "closed"),
                "tool_calls": sum(a.tool_calls for a in agents),
                "tokens_in": sum(a.tokens_in for a in agents),
                "tokens_out": sum(a.tokens_out for a in agents),
                "swarm": self.swarm.snapshot()}

    def format(self, agents: list[CrewAgent] | None = None) -> str:
        """Compact multi-line report — the shape handed back to the LLM."""
        agents = agents if agents is not None else self.list()
        if not agents:
            return "crew is empty — spawn a subagent first"
        lines = []
        for a in agents:
            icon = {"done": "✓", "blocked": "◐", "error": "✗",
                    "closed": "⊘", "running": "…"}.get(a.state, "?")
            model_tag = (f" · {a.model_id}" if a.model_id
                         and a.model_id != self.model.id else "")
            reuse = f" ({a.reused} reused)" if a.reused else ""
            team = (f" · shared {a.shared}, learned {a.learned}"
                    if (a.shared or a.learned) else "")
            head = (f"{a.icon} [{a.id}] {a.nickname} ({a.role}) {icon} "
                    f"{a.state} · {a.tool_calls} tools{reuse}{model_tag}"
                    f"{team} · {a.elapsed_ms}ms")
            lines.append(head)
            lines.append(f"  task: {a.task[:200]}")
            if a.files_touched:
                lines.append("  files: " + ", ".join(a.files_touched[:8]))
            if a.error:
                lines.append(f"  error: {a.error[:200]}")
            if a.stopped_by:
                why = {"loop": "it was repeating itself",
                       "deadline": "the rest of the batch had finished",
                       "budget": "it used every tool call it was allowed"
                       }.get(a.stopped_by, a.stopped_by)
                lines.append(
                    f"  ⚠ PARTIAL — asked to wrap up early because "
                    f"{why}. The findings below are real but may be "
                    f"incomplete; send_to_agent to continue it.")
            if a.summary:
                lines.append("  " + a.summary.replace("\n", "\n  ")[:1200])
        return "\n".join(lines)

    def format_status(self) -> str:
        s = self.status()
        lines = [f"CREW — {s['total']} subagent(s): "
                 f"{s['running']} running · {s['done']} done · "
                 f"{s['error']} error · {s['closed']} closed"]
        for a in self.list():
            reuse = f" · {a.reused} reused" if a.reused else ""
            lines.append(f"  {a.icon} [{a.id}] {a.nickname} ({a.role}) — "
                         f"{a.state}: {a.task[:70]}{reuse}")
        # the swarm renders its own utilisation — one place, one format
        lines.append("")
        lines.append(self.swarm.format_status())
        return "\n".join(lines)

    # -- the worker loop ----------------------------------------------------------

    def _board_tool(self, agent: CrewAgent) -> Tool:
        """`share_finding`, bound to the subagent that will call it.

        Bound per agent rather than shared, because a finding is only
        useful to the crew if it carries WHO worked it out — an
        anonymous board is a pile of claims nobody can weigh or follow
        up on.
        """
        def share_finding(finding: str = "") -> str:
            out = self.board.post(finding, agent_id=agent.id,
                                  role=agent.role,
                                  nickname=agent.nickname)
            if out.startswith("OK"):
                agent.shared += 1
            return out

        return Tool(
            "share_finding",
            "Tell the other subagents working alongside you something "
            "you have ESTABLISHED — a path, a signature, a root cause, "
            "a dead end worth not repeating. One fact per call, stated "
            "so somebody who has not read your work can use it. This is "
            "how the crew avoids discovering the same thing eight "
            "times; share as soon as you know it, not at the end.",
            {"type": "object",
             "properties": {"finding": {"type": "string"}},
             "required": ["finding"]},
            share_finding)

    def _deliver_findings(self, agent: CrewAgent) -> None:
        """Hand this subagent whatever its peers have learned since it
        last looked. Once each, never its own, and capped — a board
        re-sent every turn would cost more than the duplication it
        exists to prevent."""
        if self.board is None:
            return
        fresh, cursor = self.board.since(agent.board_cursor,
                                         exclude_agent=agent.id)
        agent.board_cursor = cursor
        if not fresh:
            return
        agent.learned += len(fresh)
        agent.messages.append({"role": "user",
                               "content": self.board.delivery(fresh)})

    def _finalize(self, agent: CrewAgent, provider, model,
                  reason: str) -> str:
        """Stop a subagent exploring and make it report what it HAS.

        Two things end a subagent early, and neither is a failure:

          loop      it has asked the same question with the same
                    arguments several times over. Coalescing makes the
                    repeat nearly free to execute, but the model turn
                    around it is not free and neither is the user's
                    time — a circling subagent will circle until its
                    step budget runs out.
          deadline  the rest of its batch finished long ago. A batch is
                    only as fast as its slowest member, so one subagent
                    still exploring after everyone else has reported is
                    the entire cost of the batch.

        In both cases the work already done is real and worth having, so
        we do not kill the subagent — we take its tools away and ask for
        the report. One final call, no tools, bounded.
        """
        agent.stopped_by = reason
        why = {
            "loop": ("You are repeating the same tool call with the same "
                     "arguments. Stop investigating."),
            "deadline": ("Your time budget for this task is spent. Stop "
                         "investigating."),
            "budget": ("You have used every tool call allotted to this "
                       "task. Stop investigating."),
        }.get(reason, "Stop investigating.")
        agent.messages.append({
            "role": "user",
            "content": (f"{why} Reply NOW with your final report on what "
                        f"you have already established — findings, exact "
                        f"paths, what is still unknown — in the required "
                        f"STATUS/SUMMARY form. Do not call any more "
                        f"tools.")})
        self.log.append("crew.stopped",
                        {"id": agent.id, "reason": reason,
                         "tool_calls": agent.tool_calls},
                        actor=f"crew:{agent.id}")
        try:
            with self.swarm.net():
                # schemas=None: there is no tool to call, so there is no
                # tool call to ignore our instruction with
                result = self._chat(provider, model, self.effort,
                                    agent.messages, None, 120.0)
            return (result.content or "").strip()
        except Exception as e:  # noqa: BLE001 — never lose the work
            agent.error = f"final report failed: {type(e).__name__}: {e}"
            return ""

    def _looping(self, agent: CrewAgent, signature: str) -> bool:
        """True when this exact call has come round too many times."""
        agent.recent_calls.append(signature)
        return agent.recent_calls.count(signature) >= LOOP_REPEATS

    def _run_tool(self, name: str, args: dict,
                  execute: "Callable[[], str]") -> tuple[str, str]:
        """Execute one tool call, doing no work twice.

        A fan-out's dominant cost is not thinking, it is REPETITION:
        five researchers pointed at one module read the same files, run
        the same greps and walk the same directory. Parallelism
        multiplies that instead of hiding it, which is exactly how "more
        subagents" becomes "my laptop got hot".

        So identical calls share one execution. Reads of the filesystem
        may also be served again briefly — until any subagent writes, at
        which point every cached read is dropped at once. Everything
        else (writes, commands, anything whose answer moves on its own)
        always executes for real, every time.
        """
        if name in INVALIDATING_TOOLS:
            try:
                return execute(), "ran"
            finally:
                # drop the cache AFTER the write lands, never before: a
                # reader that arrives mid-write must miss and queue on
                # the lock, not be handed the pre-write answer
                self.swarm.invalidate()
        if name not in CACHEABLE_TOOLS and name not in SINGLEFLIGHT_TOOLS:
            return execute(), "ran"
        try:
            key = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
        except (TypeError, ValueError):
            return execute(), "ran"       # unhashable args — just run it
        return self.swarm.once(key, execute,
                               cacheable=name in CACHEABLE_TOOLS)

    def _run_loop(self, agent: CrewAgent, read_only: bool,
                  max_steps: int) -> None:
        """One subagent's bounded tool loop. Never raises: every failure
        lands in the agent's report and is sealed as crew.done."""
        if agent.state == "closed":
            return
        tools = dict(self._toolsets[agent.role])
        if self.board is not None:
            tools["share_finding"] = self._board_tool(agent)
        if read_only:
            # share_finding is deliberately NOT in this list: it touches
            # nothing on disk, and a read-only scout is exactly the kind
            # of subagent whose findings the others most need.
            tools = {n: t for n, t in tools.items()
                     if n not in ("write_file", "edit_file",
                                  "create_directory", "run_command")}
        # per-agent model override resolves its own provider (schemas must
        # follow the model that will actually serve this agent, not the
        # crew default — tool support differs between models)
        model = (model_by_id(agent.model_id) if agent.model_id
                 else None) or self.model
        provider = PROVIDERS.get(model.provider, self.provider)
        schemas = ([t.openai_schema() for t in tools.values()]
                   if model.supports_tools else None)
        result = None
        forced: str | None = None
        try:
            for step in range(max_steps):
                if agent.state == "closed":
                    return          # force-stopped between two steps
                if (agent.soft_deadline
                        and time.monotonic() > agent.soft_deadline):
                    forced = self._finalize(agent, provider, model,
                                            "deadline")
                    break
                # PHASE 1 — the network. The thread parks in a socket
                # read here for seconds at a time and burns zero cpu, so
                # every subagent may be in this phase at once; the slot
                # exists only to keep the provider from being hammered.
                with self.swarm.net():
                    result = self._chat(provider, model, self.effort,
                                        agent.messages, schemas, 120.0)
                if result.usage:
                    agent.tokens_in += int(
                        result.usage.get("prompt_tokens", 0) or 0)
                    agent.tokens_out += int(
                        result.usage.get("completion_tokens", 0) or 0)
                if not result.tool_calls:
                    break
                from .client import assistant_message
                agent.messages.append(assistant_message(
                    result.content, result.tool_calls,
                    getattr(result, "reasoning", "") or ""))
                tool_names = []
                circling = False
                for tc in result.tool_calls:
                    fn = tc.get("function") or {}
                    name = fn.get("name", "")
                    args = parse_tool_arguments(fn.get("arguments"))
                    agent.tool_calls += 1
                    tool_names.append(name)
                    try:
                        signature = f"{name}:{json.dumps(args, sort_keys=True, default=str)[:400]}"
                    except (TypeError, ValueError):
                        signature = name
                    if self._looping(agent, signature):
                        circling = True
                    tool = tools.get(name)
                    if tool is None:
                        out = (f"ERROR: tool '{name}' is not available to "
                               f"a {agent.role} subagent. Available: "
                               + ", ".join(tools))
                    else:
                        # I7 — writes serialise across ALL workers, crew
                        # and team alike.
                        lock = (_WRITE_LOCK if name in MUTATING_TOOLS
                                else None)
                        try:
                            # PHASE 2 — real local work: this is the only
                            # part of a subagent that can load the box, so
                            # it runs under the swarm's load-adaptive cpu
                            # permit. Permits are always taken BEFORE the
                            # write lock (one global order, no deadlock),
                            # and a permit that times out runs the tool
                            # anyway — metering is courtesy, not a gate.
                            def _execute() -> str:
                                with self.swarm.cpu(CPU_PERMIT_TIMEOUT):
                                    if lock:
                                        with lock:
                                            return tool.handler(**args)
                                    return tool.handler(**args)

                            out, how = self._run_tool(name, args, _execute)
                            if how != "ran":
                                # somebody else already asked this exact
                                # question — no syscalls, no cpu permit,
                                # no second answer to reconcile
                                agent.reused += 1
                            if name in ("write_file", "edit_file") and \
                                    out.startswith("OK"):
                                p = str(args.get("path", ""))
                                if p and p not in agent.files_touched:
                                    agent.files_touched.append(p)
                        except Exception as e:  # noqa: BLE001
                            out = f"ERROR: {type(e).__name__}: {e}"
                    agent.messages.append(
                        {"role": "tool", "tool_call_id": tc.get("id", ""),
                         "content": out[:6000]})
                if step % 2 == 0:
                    self.log.append("crew.progress",
                                    {"id": agent.id, "step": step + 1,
                                     "tools": tool_names[:6]},
                                    actor=f"crew:{agent.id}")
                # Hand the machine a slice back between tool batches.
                # Free on an idle box, a few real milliseconds on a busy
                # one — this is what keeps the TUI feeling untouched.
                self.swarm.breathe()
                # peers may have worked something out while this
                # subagent was busy — hand it over before its next turn
                self._deliver_findings(agent)
                if circling:
                    # the tool results are already appended, so its final
                    # report still has everything it actually learned
                    forced = self._finalize(agent, provider, model, "loop")
                    break
            else:
                # The for-loop ran to exhaustion, which means the last
                # reply still wanted tools — so result.content is empty
                # and every step of work would be thrown away as "empty
                # reply". A budget is a reason to stop exploring, not a
                # reason to lose what was found; the other two ways of
                # running long already say so, and this third one must
                # not quietly disagree with them.
                if result is not None and result.tool_calls:
                    forced = self._finalize(agent, provider, model,
                                            "budget")
            final = forced if forced is not None else (
                (result.content if result is not None else "") or "")
            if agent.state == "closed":
                # retired mid-loop — keep the closed state, never resurrect
                return
            state, summary = parse_worker_final(final)
            agent.summary = summary[:1800]
            agent.state = state if state in ("done", "blocked") else "done"
            if not final.strip():
                agent.error = agent.error or (
                    f"subagent was stopped ({agent.stopped_by}) and "
                    f"produced no final report" if agent.stopped_by
                    else "subagent returned an empty reply")
                agent.state = "error"
        except Exception as e:  # noqa: BLE001 — a failing agent never kills the crew
            agent.state = "error"
            agent.error = f"{type(e).__name__}: {e}"
        agent.finished_at = time.time()
        # deliver queued follow-ups, if any arrived mid-loop — back of
        # the SAME serial queue, so nothing ever overlaps
        if agent.pending_messages and agent.state != "closed":
            queued = agent.pending_messages.pop(0)
            agent.messages.append({"role": "user",
                                   "content": f"FOLLOW-UP: {queued}"})
            agent.state = "running"
            agent.finished_at = 0.0
            agent.rearm()
            self._enqueue(agent, read_only, MAX_SEND_STEPS)
            return
        self.log.append("crew.done", agent.to_dict(),
                        actor=f"crew:{agent.id}")


# ---------------------------------------------------------------------------
# Self-test — a stub chat drives the full lifecycle deterministically
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    def _self_test() -> None:
        with tempfile.TemporaryDirectory() as td:
            log = EventLog(Path(td) / "crew-test.jsonl")
            provider = SimpleNamespace(key="t", name="T",
                                       base_url="http://t", api_key="sk-fake",
                                       color="#fff")
            model = SimpleNamespace(id="stub", provider="t", label="Stub",
                                    supports_tools=True, supports_reasoning=False)
            effort = SimpleNamespace(key="low", label="LOW", color="#fff",
                                     max_tokens=100, temperature=0.0,
                                     reasoning_effort=None)

            # A two-party barrier is the parallelism proof: it can only
            # trip if BOTH subagents are inside a model call at the same
            # instant. Under the old serial queue the first agent would
            # sit here until the barrier timed out and the test would
            # fail — there is no way to fake this.
            gate = threading.Barrier(2, timeout=10.0)
            proof = {"parallel": False}

            def stub_chat(provider_, model_, effort_, messages, schemas,
                          timeout):
                last_user = next((m["content"] for m in reversed(messages)
                                  if m.get("role") == "user"), "")
                saw_tool = any(m.get("role") == "tool" for m in messages)
                if "PARALLEL-PROOF" in last_user and not saw_tool:
                    try:
                        gate.wait()
                        proof["parallel"] = True
                    except threading.BrokenBarrierError:
                        pass          # serial execution — assertion catches it
                    content = "STATUS: DONE\nSUMMARY: built the thing"
                elif "TOOL-PROOF" in last_user and not saw_tool:
                    # drive one REAL tool call, so the swarm's cpu permit
                    # and the write-lock ordering are exercised for real
                    return SimpleNamespace(
                        content="", reasoning="",
                        tool_calls=[{"id": "c1", "function": {
                            "name": "list_dir",
                            "arguments": json.dumps({"path": td})}}],
                        finish_reason="tool_calls",
                        usage={"prompt_tokens": 4, "completion_tokens": 2})
                elif saw_tool:
                    content = "STATUS: DONE\nSUMMARY: saw the directory"
                else:
                    content = "STATUS: DONE\nSUMMARY: follow-up handled"
                return SimpleNamespace(content=content, reasoning="",
                                       tool_calls=[], finish_reason="stop",
                                       usage={"prompt_tokens": 10,
                                              "completion_tokens": 5})

            crew = Crew(log, provider, model, effort, chat=stub_chat,
                        max_parallel=4, cpu_ceiling=2)

            # spawn returns immediately; both agents run AT THE SAME TIME
            t0 = time.monotonic()
            a1 = crew.spawn("PARALLEL-PROOF write a parser", role="coder")
            a2 = crew.spawn("PARALLEL-PROOF research parsers",
                            role="researcher")
            assert a1.id == "crew-1" and a2.id == "crew-2"
            assert a1.state == "running"
            states = crew.wait(timeout=20.0)
            assert states[a1.id] == "done" and states[a2.id] == "done", states
            assert proof["parallel"], "subagents did NOT run in parallel"
            assert "built the thing" in a1.summary
            assert a1.tokens_in > 0
            # the zero-spin wait returned as soon as both settled, not on
            # some poll tick far in the future
            assert time.monotonic() - t0 < 10.0

            # a real tool call goes through the cpu permit and comes back
            a3 = crew.spawn("TOOL-PROOF list the working directory",
                            role="researcher")
            assert crew.wait([a3.id], timeout=20.0)[a3.id] == "done"
            assert a3.tool_calls == 1, a3.tool_calls
            assert "saw the directory" in a3.summary

            # follow-up reuses the full conversation
            crew.send(a1.id, "now add error handling")
            crew.wait([a1.id], timeout=10.0)
            assert a1.state == "done"
            assert "follow-up handled" in a1.summary
            users = [m for m in a1.messages if m.get("role") == "user"]
            assert len(users) == 2  # task + follow-up, history preserved

            # close refuses sends; resume reopens
            crew.close(a2.id)
            assert a2.state == "closed"
            try:
                crew.send(a2.id, "hi")
                raise AssertionError("send to closed agent must fail")
            except CrewError:
                pass
            crew.resume(a2.id)
            assert a2.state == "done"

            # unknown ids raise with the roster listed
            try:
                crew.wait(["crew-99"])
                raise AssertionError("unknown id must raise")
            except CrewError as e:
                assert "crew-1" in str(e)

            # lifecycle events are sealed in the log
            types = [e.type for e in log.events()]
            assert types.count("crew.spawn") == 3  # a1, a2, a3
            assert types.count("crew.message") == 1
            assert types.count("crew.done") >= 4
            assert "crew.closed" in types and "crew.resumed" in types

            # forget() frees a finished agent's history; a running one
            # is refused
            assert crew.forget(a3.id) is True
            assert crew.get(a3.id) is None
            assert crew.forget("crew-99") is False
            assert "crew.forgotten" in [e.type for e in log.events()]

            # force_stop drops queued work and satisfies a waiter
            a4 = crew.spawn("PARALLEL-PROOF never runs", role="reviewer")
            crew.force_stop()
            assert crew.wait([a4.id], timeout=5.0)[a4.id] == "closed"
            assert "crew.force_stop" in [e.type for e in log.events()]

            # the swarm never breached its cpu ceiling and gives its
            # threads back when the work stops
            sw = crew.status()["swarm"]
            assert sw["cpu"]["peak"] <= 2, sw
            assert sw["in_flight"] == 0
            idle_end = time.monotonic() + 3.0
            while crew.swarm.pool.threads > 0 and \
                    time.monotonic() < idle_end:
                time.sleep(0.05)

            # report renders
            rep = crew.format()
            assert "crew-1" in rep and "coder" in rep
            status = crew.format_status()
            assert "CREW" in status and "SWARM" in status
            assert "provider" in status and "machine" in status

            print("CREW SELF-TEST PASS")

    _self_test()

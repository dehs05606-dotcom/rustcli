"""INTERLEAVE — stop hoping the concurrency is right; search for the proof.

A concurrency test that passes tells you one interleaving worked. The
scheduler chose it, not you, and it will choose differently on a loaded
CI box at 3am. Every test in this repository's parallel suite — mine
included — is that kind of evidence: real, and much weaker than it looks.
A race that needs two threads to land inside a four-instruction window
will pass ten thousand runs and fail the first time a customer runs it.

This module removes the scheduler's discretion. Threads under test run
ONE AT A TIME, and a strategy decides who runs next at every
synchronisation point. That turns "did we get lucky" into a search, and a
search has properties luck does not:

    DETERMINISTIC   a schedule is a list of choices. Same list, same
                    execution, every time — so a failure found once is a
                    failure you can put in a test.
    SYSTEMATIC      strategies explore the choice space deliberately
                    instead of sampling whatever the OS felt like.
    DIAGNOSABLE     a failure comes back WITH its schedule and the
                    labelled sequence of operations that produced it,
                    which is the difference between "flaky, rerun it" and
                    a bug report.

Two strategies, and the second is the interesting one:

    RandomStrategy  pick uniformly among runnable threads. Cheap, wide,
                    finds shallow bugs fast.
    PCTStrategy     Probabilistic Concurrency Testing (Burckhardt et al.,
                    ASPLOS'10). Give each thread a random priority, run
                    the highest, and insert d-1 random priority-drop
                    points across the execution. This buys a real lower
                    bound — for a bug of depth d among n threads and k
                    steps, at least 1/(n * k^(d-1)) per run — where
                    random scheduling gives you no bound at all. Depth is
                    small in practice: almost every real race is depth 2
                    or 3, meaning it needs two or three specific
                    orderings, not twenty.

WHAT IT CONTROLS, AND WHAT IT DOES NOT
Threads are switched at synchronisation points: lock acquire and release,
condition wait and notify, event wait and set, queue put and get. That is
where the interesting interleavings live, and it is what every practical
tool of this kind controls. It does NOT preempt between arbitrary
bytecodes, so a data race on a plain attribute with no lock anywhere near
it can still slip through. Saying so is the point: a verification tool
that overstates its coverage is worse than none, because people stop
looking.

USING IT ON REAL CODE
The primitives here are drop-in shims for threading.Lock, RLock,
Condition, Event, Semaphore and queue.Queue. Code under test picks them
up by having its module's `threading` swapped for a shim — no rewrite, no
special build. See `instrumented()`.
"""

from __future__ import annotations

import random
import threading as _real_threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ._foundation import get_logger

_log = get_logger("interleave")

__all__ = [
    "Scheduler", "RandomStrategy", "PCTStrategy", "Finding", "Report",
    "explore", "replay", "instrumented", "Deadlock", "ScheduleExhausted",
]

MAX_STEPS = 20_000          # a runaway scenario is a bug in the scenario
JOIN_TIMEOUT = 20.0


class Deadlock(RuntimeError):
    """Every thread is blocked and none can be woken."""


class ScheduleExhausted(RuntimeError):
    """A replayed schedule ran out of choices before the run finished."""


# ---------------------------------------------------------------------------
# Thread bookkeeping
# ---------------------------------------------------------------------------

RUNNABLE, BLOCKED, TIMED, DONE = "runnable", "blocked", "timed", "done"


@dataclass
class _Task:
    tid: int
    name: str
    state: str = RUNNABLE
    error: BaseException | None = None
    steps: int = 0
    priority: int = 0
    thread: Any = None

    def __repr__(self) -> str:      # pragma: no cover - debugging aid
        return f"<{self.name} {self.state}>"


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

class RandomStrategy:
    """Uniform choice among runnable threads."""

    name = "random"

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.seed = seed

    def start(self, tasks: list[_Task]) -> None:
        pass

    def pick(self, runnable: list[_Task], step: int) -> _Task:
        return self.rng.choice(runnable)


class PCTStrategy:
    """Probabilistic Concurrency Testing — a real bound, not a hope.

    Each thread gets a random priority; the highest-priority runnable
    thread always runs. d-1 priority-change points are chosen uniformly
    over the expected step count, and a thread that reaches one is
    demoted below everybody. That single mechanism is what gives the
    guarantee: a depth-d bug needs d-1 specific preemptions, and this
    plants exactly d-1 of them at uniformly random moments.

    Unlike random scheduling it also does NOT thrash — between change
    points it runs one thread to completion or to a block, which is what
    lets it reach deep states that uniform switching never gets to.
    """

    name = "pct"

    def __init__(self, seed: int, depth: int = 3,
                 expected_steps: int = 50) -> None:
        self.rng = random.Random(seed)
        self.seed = seed
        self.depth = max(1, int(depth))
        self.expected_steps = max(1, int(expected_steps))
        self._change_points: set[int] = set()
        self._next_low = 0

    def start(self, tasks: list[_Task]) -> None:
        n = len(tasks)
        prios = list(range(self.depth, self.depth + n))
        self.rng.shuffle(prios)
        for task, p in zip(tasks, prios):
            task.priority = p
        # Change points are drawn over the run's EXPECTED length. Get
        # that number badly wrong and the algorithm quietly stops being
        # PCT: with points scattered over 200 steps and a run that ends
        # at 15, not one of them ever fires, every thread keeps its
        # starting priority, and the "search" degenerates into running
        # threads to completion in a fixed order — which finds nothing,
        # while reporting that it explored two hundred interleavings.
        # explore() measures real runs and feeds the number back.
        self._change_points = set(
            self.rng.randrange(max(2, self.expected_steps))
            for _ in range(self.depth - 1))
        self._next_low = 0

    def pick(self, runnable: list[_Task], step: int) -> _Task:
        # highest priority wins; ties broken deterministically by tid
        best = max(runnable, key=lambda t: (t.priority, -t.tid))
        if step in self._change_points:
            self._next_low -= 1
            best.priority = self._next_low        # demoted below all
        return best


class _ReplayStrategy:
    """Replays a recorded schedule exactly."""

    name = "replay"

    def __init__(self, schedule: list[int]) -> None:
        self.schedule = list(schedule)
        self.seed = -1
        self._i = 0

    def start(self, tasks: list[_Task]) -> None:
        self._i = 0

    def pick(self, runnable: list[_Task], step: int) -> _Task:
        if self._i >= len(self.schedule):
            raise ScheduleExhausted(
                f"schedule had {len(self.schedule)} choices, run wanted more")
        want = self.schedule[self._i]
        self._i += 1
        for t in runnable:
            if t.tid == want:
                return t
        raise ScheduleExhausted(
            f"schedule says thread {want}, but it is not runnable "
            f"(runnable: {[t.tid for t in runnable]}) — the scenario is "
            f"not deterministic under this scheduler")


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    """Runs a set of threads one at a time, choosing at every sync point.

    The threads are REAL OS threads — the scenario under test is ordinary
    code — but exactly one is ever permitted to proceed. Control passes
    back here at each instrumented operation, which is what makes the
    execution a sequence of decisions rather than a race.
    """

    def __init__(self, strategy: Any, *, max_steps: int = MAX_STEPS) -> None:
        self.strategy = strategy
        self.max_steps = int(max_steps)
        self._cond = _real_threading.Condition(_real_threading.Lock())
        self._tasks: list[_Task] = []
        self._current: _Task | None = None
        self._local = _real_threading.local()
        self.schedule: list[int] = []
        self.trace: list[str] = []
        self.steps = 0
        self._started = False
        self._final: list[Callable[[], Any]] = []
        self._final_error: BaseException | None = None

    # -- scenario API ------------------------------------------------------

    def spawn(self, fn: Callable[[], Any], name: str = "") -> _Task:
        task = _Task(tid=len(self._tasks), name=name or f"t{len(self._tasks)}")

        def body() -> None:
            self._local.task = task
            self._await_turn(task)
            try:
                fn()
            except BaseException as e:      # noqa: BLE001 — carried, reported
                task.error = e
            finally:
                with self._cond:
                    task.state = DONE
                    self._current = None
                    self._cond.notify_all()

        task.thread = _real_threading.Thread(target=body, daemon=True,
                                             name=task.name)
        self._tasks.append(task)
        return task

    def on_finish(self, check: Callable[[], Any]) -> None:
        """Assert something about the FINAL state, after every thread ends.

        Almost every invariant worth checking is of this shape, and
        expressing it as an extra thread is a trap this checker caught
        twice in its own tests: an observer thread that "settles for a
        few steps and then asserts" can simply be scheduled first, and
        then reports a failure that never happened. A post-run check
        cannot be scheduled at all, so it cannot be scheduled wrong.
        """
        self._final.append(check)

    def current(self) -> _Task | None:
        return getattr(self._local, "task", None)

    # -- the switch points -------------------------------------------------

    def _await_turn(self, task: _Task) -> None:
        with self._cond:
            while self._current is not task:
                self._cond.wait(JOIN_TIMEOUT)

    def switch(self, label: str = "") -> None:
        """Hand control back. Called by every instrumented operation."""
        task = self.current()
        if task is None:
            return                            # not a scheduled thread
        task.steps += 1
        if label:
            self.trace.append(f"{task.name}:{label}")
        with self._cond:
            self._current = None
            self._cond.notify_all()
        self._await_turn(task)

    def block(self, waiters: list[_Task], label: str = "") -> None:
        """Mark the caller blocked and hand control back until woken."""
        task = self.current()
        if task is None:
            raise Deadlock("a non-scheduled thread tried to block")
        if label:
            self.trace.append(f"{task.name}:{label}")
        with self._cond:
            task.state = BLOCKED
            if task not in waiters:
                waiters.append(task)
            self._current = None
            self._cond.notify_all()
        self._await_turn(task)

    def block_task(self, label: str = "") -> None:
        """Block the caller until somebody calls wake_task on it.

        Unlike block(), this does NOT register the caller on a waiter
        list — the caller has already published itself somewhere, which
        is the only way to close the window between publishing and
        blocking. That window is where lost wakeups live: release a lock,
        get preempted, have your notifier run and find nobody waiting,
        then block forever.
        """
        task = self.current()
        if task is None:
            raise Deadlock("a non-scheduled thread tried to block")
        if label:
            self.trace.append(f"{task.name}:{label}")
        with self._cond:
            task.state = BLOCKED
            self._current = None
            self._cond.notify_all()
        self._await_turn(task)

    def block_timed(self, label: str = "") -> None:
        """Block the caller on a TIMED wait.

        Time is modelled the only way that is both deterministic and
        honest: it advances when, and only when, nothing else can
        happen. Treating a timed wait as "always expires" turns every
        poll into a busy failure; treating it as "never expires" invents
        deadlocks in code that deliberately polls — and this repository's
        AdaptiveSemaphore does exactly that, because permits can grow
        with no release to notify on. Advancing the clock only at
        quiescence gives both behaviours their turn without adding a
        single nondeterministic choice to the schedule.
        """
        task = self.current()
        if task is None:
            raise Deadlock("a non-scheduled thread tried to block")
        if label:
            self.trace.append(f"{task.name}:{label}")
        with self._cond:
            task.state = TIMED
            self._current = None
            self._cond.notify_all()
        self._await_turn(task)

    @staticmethod
    def wake_task(task: _Task) -> None:
        """Make one task runnable. Safe on a task that is still running —
        which is exactly the case this exists to survive."""
        if task.state in (BLOCKED, TIMED):
            task.state = RUNNABLE

    @staticmethod
    def wake(waiters: list[_Task], how_many: int | None = None) -> None:
        """Make blocked waiters runnable again. None = all of them."""
        n = len(waiters) if how_many is None else int(how_many)
        for task in waiters[:n]:
            task.state = RUNNABLE
        del waiters[:n]

    # -- the driver --------------------------------------------------------

    def run(self) -> None:
        """Drive every spawned thread to completion, or to a deadlock."""
        if self._started:
            raise RuntimeError("a Scheduler runs once")
        self._started = True
        self.strategy.start(self._tasks)
        for task in self._tasks:
            task.thread.start()

        while self.steps < self.max_steps:
            with self._cond:
                runnable = [t for t in self._tasks if t.state == RUNNABLE]
                alive = [t for t in self._tasks if t.state != DONE]
                if not alive:
                    break
                if not runnable:
                    timed = [t for t in alive if t.state == TIMED]
                    if timed:
                        # quiescence: nothing else can move, so the clock
                        # advances and every timed wait expires at once
                        for t in timed:
                            t.state = RUNNABLE
                        self.trace.append("~clock advances~")
                        continue
                    blocked = ", ".join(t.name for t in alive)
                    raise Deadlock(
                        f"every remaining thread is blocked: {blocked}")
                chosen = self.strategy.pick(runnable, self.steps)
                self.schedule.append(chosen.tid)
                self.steps += 1
                self._current = chosen
                self._cond.notify_all()
                # wait until the chosen thread yields, blocks or finishes
                while self._current is chosen:
                    if not self._cond.wait(JOIN_TIMEOUT):
                        raise Deadlock(
                            f"{chosen.name} never yielded control back — "
                            f"it is blocked on something this scheduler "
                            f"does not control")
        else:
            raise Deadlock(f"exceeded {self.max_steps} scheduling steps")

        for task in self._tasks:
            task.thread.join(JOIN_TIMEOUT)

        for check in self._final:
            try:
                check()
            except BaseException as e:      # noqa: BLE001 — carried, reported
                self._final_error = e
                break

    def first_error(self) -> BaseException | None:
        for task in self._tasks:
            if task.error is not None:
                return task.error
        return self._final_error


# ---------------------------------------------------------------------------
# Instrumented primitives — drop-in shims
# ---------------------------------------------------------------------------

class _Lock:
    def __init__(self, sched: Scheduler, *, reentrant: bool = False) -> None:
        self._s = sched
        self._owner: _Task | None = None
        self._count = 0
        self._waiters: list[_Task] = []
        self._reentrant = reentrant

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        me = self._s.current()
        if self._reentrant and self._owner is me and me is not None:
            self._count += 1
            return True
        while self._owner is not None:
            if not blocking:
                return False
            self._s.block(self._waiters, "lock.wait")
        self._owner = me
        self._count = 1
        self._s.switch("lock.acquire")
        return True

    def release(self) -> None:
        if self._reentrant:
            self._count -= 1
            if self._count > 0:
                return
        self._owner = None
        self._s.wake(self._waiters, 1)
        self._s.switch("lock.release")

    def locked(self) -> bool:
        return self._owner is not None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


@dataclass
class _Waiter:
    """One outstanding wait(), with its own delivery flag.

    A plain list of tasks is not enough. A notify that lands between
    wait()'s lock release and its block would flip a shared list entry
    the waiter has not created yet — so the wakeup is delivered to
    nobody and the waiter blocks forever. Giving every wait its own token
    makes the signal a FACT the waiter can re-check after the window,
    instead of an event it had to be present for.
    """
    task: _Task
    signalled: bool = False


class _Condition:
    def __init__(self, sched: Scheduler, lock=None) -> None:
        self._s = sched
        self._lock = lock if lock is not None else _Lock(sched)
        self._waiters: list[_Waiter] = []

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False

    def acquire(self, *a, **kw):
        return self._lock.acquire(*a, **kw)

    def release(self):
        self._lock.release()

    def wait(self, timeout: float | None = None) -> bool:
        # PUBLISH BEFORE RELEASING. Releasing first opens a window in
        # which a notifier runs, finds no waiter, and the wakeup is lost.
        # This checker found exactly that bug in this very method, which
        # is a fair advertisement for the checker.
        me = self._s.current()
        waiter = _Waiter(task=me)
        self._waiters.append(waiter)
        self._lock.release()
        if timeout is None:
            while not waiter.signalled:
                self._s.block_task("cond.wait")
        else:
            if not waiter.signalled:
                self._s.block_timed("cond.wait(timeout)")
            if waiter in self._waiters:
                self._waiters.remove(waiter)
        self._lock.acquire()
        return True

    def _signal(self, n: int | None) -> None:
        count = len(self._waiters) if n is None else int(n)
        for waiter in self._waiters[:count]:
            waiter.signalled = True
            self._s.wake_task(waiter.task)
        del self._waiters[:count]

    def notify(self, n: int = 1) -> None:
        self._signal(n)
        self._s.switch("cond.notify")

    def notify_all(self) -> None:
        self._signal(None)
        self._s.switch("cond.notify_all")


class _Event:
    def __init__(self, sched: Scheduler) -> None:
        self._s = sched
        self._flag = False
        self._waiters: list[_Task] = []

    def set(self) -> None:
        self._flag = True
        self._s.wake(self._waiters)
        self._s.switch("event.set")

    def clear(self) -> None:
        self._flag = False
        self._s.switch("event.clear")

    def is_set(self) -> bool:
        return self._flag

    def wait(self, timeout: float | None = None) -> bool:
        while not self._flag:
            if timeout is None:
                self._s.block(self._waiters, "event.wait")
                continue
            me = self._s.current()
            if me not in self._waiters:
                self._waiters.append(me)
            # woken by set(), or by the clock at quiescence — a timed
            # wait returns exactly once either way, and reports the flag
            self._s.block_timed("event.wait(timeout)")
            if me in self._waiters:
                self._waiters.remove(me)
            return self._flag
        self._s.switch("event.wait.done")
        return True


class _Semaphore:
    def __init__(self, sched: Scheduler, value: int = 1) -> None:
        self._s = sched
        self._value = int(value)
        self._waiters: list[_Task] = []

    def acquire(self, blocking: bool = True, timeout: float | None = None):
        while self._value <= 0:
            if not blocking:
                return False
            self._s.block(self._waiters, "sem.wait")
        self._value -= 1
        self._s.switch("sem.acquire")
        return True

    def release(self, n: int = 1) -> None:
        self._value += n
        self._s.wake(self._waiters, n)
        self._s.switch("sem.release")

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class _Queue:
    def __init__(self, sched: Scheduler, maxsize: int = 0) -> None:
        self._s = sched
        self._items: list[Any] = []
        self._waiters: list[_Task] = []

    def put(self, item: Any, *a, **kw) -> None:
        self._items.append(item)
        self._s.wake(self._waiters, 1)
        self._s.switch("queue.put")

    def get(self, block: bool = True, timeout: float | None = None) -> Any:
        import queue as _q
        while not self._items:
            if not block:
                raise _q.Empty
            if timeout is not None:
                self._s.switch("queue.get(timeout)")
                if not self._items:
                    raise _q.Empty
                break
            self._s.block(self._waiters, "queue.get")
        self._s.switch("queue.get")
        return self._items.pop(0)

    def qsize(self) -> int:
        return len(self._items)

    def empty(self) -> bool:
        return not self._items

    def task_done(self) -> None:
        pass


class _ThreadingShim:
    """Quacks like the `threading` module, schedules like this scheduler."""

    def __init__(self, sched: Scheduler) -> None:
        self._s = sched
        self.local = _real_threading.local

    def Lock(self):
        return _Lock(self._s)

    def RLock(self):
        return _Lock(self._s, reentrant=True)

    def Condition(self, lock=None):
        return _Condition(self._s, lock)

    def Event(self):
        return _Event(self._s)

    def Semaphore(self, value: int = 1):
        return _Semaphore(self._s, value)

    def BoundedSemaphore(self, value: int = 1):
        return _Semaphore(self._s, value)

    def current_thread(self):
        return _real_threading.current_thread()


@contextmanager
def instrumented(sched: Scheduler, *modules: Any,
                 attr: str = "threading"):
    """Swap `threading` for a scheduled shim inside the given modules.

    Real code under test is not modified or rebuilt: it calls
    threading.Lock() as it always did, and gets a lock that reports to
    the scheduler. The swap is undone on exit even if the scenario blew
    up, because a test module left holding a dead scheduler's primitives
    would poison every test that ran after it.
    """
    shim = _ThreadingShim(sched)
    saved = [(m, getattr(m, attr, None)) for m in modules]
    try:
        for m in modules:
            setattr(m, attr, shim)
        yield shim
    finally:
        for m, old in saved:
            if old is not None:
                setattr(m, attr, old)


# ---------------------------------------------------------------------------
# Exploration
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    kind: str                      # assertion | deadlock | error
    detail: str
    seed: int
    strategy: str
    schedule: list[int] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    iteration: int = 0

    def to_dict(self) -> dict:
        return {"kind": self.kind, "detail": self.detail, "seed": self.seed,
                "strategy": self.strategy, "iteration": self.iteration,
                "schedule": self.schedule, "trace": self.trace[:200]}

    def format(self) -> str:
        lines = [f"✗ {self.kind.upper()} — {self.detail}",
                 f"  strategy {self.strategy}, seed {self.seed}, "
                 f"iteration {self.iteration}",
                 f"  schedule ({len(self.schedule)} choices): "
                 + " ".join(map(str, self.schedule[:60]))
                 + (" …" if len(self.schedule) > 60 else "")]
        if self.trace:
            lines.append("  last operations:")
            for op in self.trace[-12:]:
                lines.append(f"    {op}")
        lines.append("  replay it with: replay(scenario, schedule)")
        return "\n".join(lines)


@dataclass
class Report:
    iterations: int = 0
    findings: list[Finding] = field(default_factory=list)
    schedules: int = 0
    elapsed_s: float = 0.0
    distinct: int = 0

    @property
    def clean(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict:
        return {"iterations": self.iterations, "schedules": self.schedules,
                "distinct_schedules": self.distinct,
                "elapsed_s": round(self.elapsed_s, 2),
                "findings": [f.to_dict() for f in self.findings]}

    def format(self) -> str:
        head = (f"INTERLEAVE — {self.iterations} interleaving(s), "
                f"{self.distinct} distinct, in {self.elapsed_s:.2f}s")
        if self.clean:
            return head + "\n✓ no assertion failure and no deadlock found"
        return "\n".join([head] + [f.format() for f in self.findings])


def explore(scenario: Callable[[Scheduler], None], *,
            iterations: int = 200, seed: int = 0,
            strategy: str = "pct", depth: int = 3,
            stop_on_first: bool = True,
            modules: Iterable[Any] = (),
            max_steps: int = MAX_STEPS) -> Report:
    """Run one concurrent scenario under many controlled interleavings.

    `scenario(sched)` sets the world up and spawns its threads with
    sched.spawn(...). It must be fully re-runnable: it is called once per
    interleaving, and any state it shares between runs would make the
    search meaningless.

    `modules` are instrumented for the WHOLE run, construction and
    execution alike. Doing it yourself with `instrumented()` inside the
    scenario is a trap — and one this checker caught in its own first
    attempt at testing real code: the context manager closes when the
    scenario function returns, which is BEFORE any thread runs, so
    anything the code creates lazily (a per-entry Event, a queue made on
    first use) quietly gets the real primitive and blocks for real. The
    scheduler then reports a deadlock that is entirely its own fault.
    """
    t0 = time.monotonic()
    report = Report(iterations=iterations)
    seen: set[tuple] = set()

    observed = 0
    for i in range(max(1, iterations)):
        run_seed = seed + i
        strat = (PCTStrategy(run_seed, depth=depth,
                             expected_steps=observed or 50)
                 if strategy == "pct" else RandomStrategy(run_seed))
        sched = Scheduler(strat, max_steps=max_steps)
        finding: Finding | None = None
        try:
            with instrumented(sched, *modules):
                scenario(sched)
                sched.run()
            err = sched.first_error()
            if err is not None:
                finding = Finding(
                    kind=("assertion" if isinstance(err, AssertionError)
                          else "error"),
                    detail=f"{type(err).__name__}: {err}",
                    seed=run_seed, strategy=strat.name)
        except Deadlock as e:
            finding = Finding(kind="deadlock", detail=str(e),
                              seed=run_seed, strategy=strat.name)
        except ScheduleExhausted as e:
            finding = Finding(kind="error", detail=str(e),
                              seed=run_seed, strategy=strat.name)

        report.schedules += 1
        seen.add(tuple(sched.schedule))
        # calibrate: the next run's priority-change points must land
        # inside a run of this length, or they never fire at all
        observed = max(observed, sched.steps)
        if finding is not None:
            finding.schedule = list(sched.schedule)
            finding.trace = list(sched.trace)
            finding.iteration = i
            report.findings.append(finding)
            if stop_on_first:
                break

    report.distinct = len(seen)
    report.elapsed_s = time.monotonic() - t0
    return report


def replay(scenario: Callable[[Scheduler], None],
           schedule: Iterable[int], *,
           modules: Iterable[Any] = (),
           max_steps: int = MAX_STEPS) -> Scheduler:
    """Re-run a scenario under an exact recorded schedule.

    This is what makes a finding a bug report rather than an anecdote:
    the same list of choices reproduces the same execution, so a failure
    can be pasted into a regression test and will fail until it is fixed.
    """
    sched = Scheduler(_ReplayStrategy(list(schedule)), max_steps=max_steps)
    with instrumented(sched, *modules):
        scenario(sched)
        sched.run()
    return sched


# ---------------------------------------------------------------------------
# Self-test — plant real races, and require that they are FOUND
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    def _self_test() -> None:
        # -- 1. a lost update, found and then replayed -----------------
        # two threads do read-modify-write with a switch in the middle.
        # Under any interleaving that overlaps them, one update is lost.
        def lost_update(sched: Scheduler) -> None:
            # the checker's own first finding was that a "settle for a
            # few steps then assert" observer can simply run FIRST and
            # report a failure that never happened. It was right, and the
            # fix is the one real code needs too: the observer must WAIT
            # for the work, not guess at how long it takes.
            cond = _Condition(sched)
            box = {"n": 0, "done": 0}

            def bump() -> None:
                seen = box["n"]
                sched.switch("read")        # the unguarded window
                box["n"] = seen + 1
                with cond:
                    box["done"] += 1
                    cond.notify_all()

            def check() -> None:
                with cond:
                    while box["done"] < 2:
                        cond.wait()
                    assert box["n"] == 2, f"lost update: n={box['n']}"

            sched.spawn(bump, "a")
            sched.spawn(bump, "b")
            sched.spawn(check, "check")

        rep = explore(lost_update, iterations=60, seed=1)
        assert not rep.clean, "a guaranteed lost update was not found"
        f = rep.findings[0]
        assert f.kind == "assertion" and "lost update" in f.detail
        assert f.schedule and f.trace
        assert "replay it with" in f.format()

        # and the schedule reproduces it EXACTLY
        again = replay(lost_update, f.schedule)
        err = again.first_error()
        assert isinstance(err, AssertionError), err
        assert str(err) == f.detail.split(": ", 1)[1]
        assert again.schedule == f.schedule

        # -- 2. the same code, correctly locked, survives the search ----
        def guarded(sched: Scheduler) -> None:
            cond = _Condition(sched)
            box = {"n": 0, "done": 0}

            def bump() -> None:
                with cond:                   # the SAME window, now locked
                    seen = box["n"]
                    sched.switch("read")
                    box["n"] = seen + 1
                    box["done"] += 1
                    cond.notify_all()

            def check() -> None:
                with cond:
                    while box["done"] < 2:
                        cond.wait()
                    assert box["n"] == 2, f"n={box['n']}"

            sched.spawn(bump, "a")
            sched.spawn(bump, "b")
            sched.spawn(check, "check")

        rep = explore(guarded, iterations=120, seed=1, stop_on_first=False)
        assert rep.clean, rep.format()
        assert rep.distinct > 1, "the search never varied the schedule"

        # -- 3. a real deadlock: two locks, opposite orders -------------
        def lock_order(sched: Scheduler) -> None:
            a, b = _Lock(sched), _Lock(sched)

            def left() -> None:
                with a:
                    sched.switch("mid")
                    with b:
                        pass

            def right() -> None:
                with b:
                    sched.switch("mid")
                    with a:
                        pass

            sched.spawn(left, "left")
            sched.spawn(right, "right")

        rep = explore(lock_order, iterations=80, seed=5)
        assert not rep.clean, "a textbook lock-order deadlock was missed"
        assert rep.findings[0].kind == "deadlock", rep.findings[0].detail

        # -- 4. condition variables: no lost wakeups --------------------
        def producer_consumer(sched: Scheduler) -> None:
            cond = _Condition(sched)
            box: list[int] = []

            def producer() -> None:
                with cond:
                    box.append(1)
                    cond.notify_all()

            def consumer() -> None:
                with cond:
                    while not box:
                        cond.wait()
                    assert box == [1]

            sched.spawn(consumer, "consumer")
            sched.spawn(producer, "producer")

        rep = explore(producer_consumer, iterations=80, seed=2,
                      stop_on_first=False)
        assert rep.clean, rep.format()

        # -- 5. PCT actually explores; a fixed schedule does not --------
        pct = explore(guarded, iterations=40, seed=9, strategy="pct",
                      stop_on_first=False)
        rnd = explore(guarded, iterations=40, seed=9, strategy="random",
                      stop_on_first=False)
        assert pct.distinct > 1 and rnd.distinct > 1
        assert pct.clean and rnd.clean

        # -- 6. a scenario that hangs is reported, not waited on --------
        def hangs(sched: Scheduler) -> None:
            ev = _Event(sched)

            def waiter() -> None:
                ev.wait()          # nobody ever sets it

            sched.spawn(waiter, "waiter")

        rep = explore(hangs, iterations=2, seed=1)
        assert rep.findings and rep.findings[0].kind == "deadlock"

        # -- 7. a final-state check cannot be scheduled wrong -----------
        def final_check(sched: Scheduler) -> None:
            lock = _Lock(sched)
            box = {"n": 0}

            def bump() -> None:
                with lock:
                    seen = box["n"]
                    sched.switch("read")
                    box["n"] = seen + 1

            sched.spawn(bump, "a")
            sched.spawn(bump, "b")
            sched.on_finish(lambda: (_ for _ in ()).throw(
                AssertionError(f"n={box['n']}")) if box["n"] != 2 else None)

        rep = explore(final_check, iterations=60, seed=3,
                      stop_on_first=False)
        assert rep.clean, rep.format()

        # and it really does fire when the invariant breaks
        def final_breaks(sched: Scheduler) -> None:
            box = {"n": 0}

            def bump() -> None:
                seen = box["n"]
                sched.switch("read")
                box["n"] = seen + 1

            sched.spawn(bump, "a")
            sched.spawn(bump, "b")

            def check() -> None:
                assert box["n"] == 2, f"lost update: n={box['n']}"

            sched.on_finish(check)

        rep = explore(final_breaks, iterations=60, seed=3)
        assert not rep.clean and "lost update" in rep.findings[0].detail

        # -- 8. replaying a schedule that does not fit is REFUSED -------
        try:
            replay(guarded, [0, 0, 0])
            raise AssertionError("a short schedule must be refused")
        except ScheduleExhausted:
            pass

        print("INTERLEAVE SELF-TEST PASS")

    _self_test()

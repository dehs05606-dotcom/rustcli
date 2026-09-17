"""Swarm — the adaptive parallel substrate for subagents.

Subagents used to run ONE AT A TIME through a single serial queue. That
was safe but slow: eight subagents that each spend 95% of their life
*blocked on a socket* waiting for the model were made to queue behind
each other for no reason at all.

This module makes them run for real, in parallel, WITHOUT the machine
ever feeling it. The insight is that a subagent's second is not one
kind of second:

    NET phase   waiting for the model to stream tokens back. The thread
                is parked in a socket read. It burns ZERO cpu. Ten of
                these cost the same as one.

    CPU phase   the local work between model turns — reading files,
                grepping, running a command, parsing tool JSON. THIS is
                the only part that can actually make a laptop's fan spin.

So the two phases get two different admission controls:

    net()       a plain ceiling (default = the roster size). Parked
                threads are free, this only keeps the provider from
                being hammered.

    cpu()       an ADAPTIVE permit, resized every half second from the
                machine's real load average. On an idle box it opens up
                to CPU_CEILING; the moment something else needs the
                machine it closes back down to one. Local subagent work
                therefore fills the *headroom* that exists and never
                competes for headroom that doesn't.

Four more things keep the footprint invisible:

  * ZERO-SPIN WAITS. Nothing polls. Every wait is a Condition/Event that
    the completing worker wakes. A hundred idle waiters cost nothing.
  * AN ELASTIC POOL. Threads are created on demand, never up front, and
    retire themselves after IDLE_TTL seconds with nothing to do. At rest
    the swarm owns zero threads and zero memory.
  * NICE. On Linux, where niceness is per-thread, every worker drops to
    NICE_DELTA. Even during a cpu burst the foreground TUI keeps the
    scheduler's attention — the user's keystrokes never wait behind a
    subagent.
  * BREATHE. After each chunk of local work a worker offers the machine
    a slice back, sized to current pressure. Under load this turns a
    hard burst into a gentle one.

Nothing here knows what a subagent IS. It is pure substrate: crew.py
drives its agents through it, and the same primitives are available to
any subsystem that wants real concurrency with a conscience.
"""

from __future__ import annotations

import itertools
import os
import queue
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ._foundation import clamp, get_logger

_log = get_logger("swarm")

__all__ = [
    "LoadSample", "LoadGovernor", "AdaptiveSemaphore", "ElasticPool",
    "NetWindow", "Coalescer", "Ticket", "Swarm", "Cancelled",
    "CPU_CEILING", "NICE_DELTA", "IDLE_TTL", "FOREGROUND", "BACKGROUND",
]

# Admission lanes. The sovereign turn — the thing the user is actually
# watching — never queues behind subagents.
FOREGROUND = "foreground"
BACKGROUND = "background"


class Cancelled(RuntimeError):
    """A job was dropped from the queue before it ever started."""

# How many subagents may do LOCAL work at the same moment, at most, on a
# completely idle machine. Deliberately small and never the full core
# count: the point is to use headroom, not to claim the box.
CPU_CEILING = 4
# Per-thread niceness for workers (Linux only — see _apply_nice).
NICE_DELTA = 5
# A pool thread with nothing to do for this long retires itself.
IDLE_TTL = 20.0
# How often the governor re-reads the machine's load.
SAMPLE_INTERVAL = 0.5
# Minimum gap between two permit changes — stops the governor flapping.
CHANGE_COOLDOWN = 1.0
# Safety re-check interval for a thread blocked on a cpu permit. Permits
# can GROW without any release happening, so a blocked waiter needs one
# cheap look every so often. Four wakeups a second, only while blocked,
# only while the swarm is saturated — microseconds of cpu.
PERMIT_RECHECK = 0.25


def _cores() -> int:
    return os.cpu_count() or 1


def _apply_nice() -> None:
    """Drop this thread's scheduling priority, where that is meaningful.

    On Linux a thread is a task and ``nice`` applies to the calling
    thread alone — exactly what we want. On macOS and BSD ``nice``
    applies to the whole PROCESS, which would slow the user's own TUI
    down, so we do not touch it there. Windows has no nice at all.
    """
    if not sys.platform.startswith("linux"):
        return
    try:
        os.nice(NICE_DELTA)
    except (OSError, AttributeError):  # no permission, or no nice
        pass


# ---------------------------------------------------------------------------
# LoadGovernor — how much of this machine may subagents actually use?
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoadSample:
    """One reading of the machine's pressure."""
    at: float
    cores: int
    raw: float             # everything running, us included
    own: float             # how much of that is our own subagents
    load1: float           # raw minus own — what OTHER work is using
    pressure: float        # load1 / cores — 1.0 means "fully committed"
    permits: int           # cpu permits the governor granted from it
    estimated: bool        # True when the reading came from our cpu clock

    def to_dict(self) -> dict:
        return {"cores": self.cores, "load1": round(self.load1, 2),
                "raw": round(self.raw, 2), "own": round(self.own, 2),
                "pressure": round(self.pressure, 2),
                "permits": self.permits, "estimated": self.estimated}


class LoadGovernor:
    """Decides, continuously, how many subagents may do local work.

    The rule is headroom, not fairness: start from what the machine can
    spare right now and step toward it, one permit at a time, no more
    often than CHANGE_COOLDOWN. Stepping (rather than jumping) plus the
    cooldown is what stops a transient spike from collapsing the swarm
    and a transient lull from stampeding it.

    Sampling is lazy — the load is only read when somebody asks for
    permits, and at most once per SAMPLE_INTERVAL. An idle swarm never
    wakes up to measure anything.
    """

    def __init__(self, *, ceiling: int = CPU_CEILING,
                 cores: int | None = None,
                 load_source: "Callable[[], float] | None" = None,
                 self_load: "Callable[[], float] | None" = None) -> None:
        self.cores = max(1, int(cores or _cores()))
        self.ceiling = int(clamp(int(ceiling), 1, max(1, self.cores * 2)))
        self._load_source = load_source
        # How much of the machine's current load is OUR OWN subagents.
        # Subtracting it is what stops the governor mistaking its own
        # workers for somebody else's build and throttling itself into
        # a corner: we want to measure the load we must make room FOR,
        # not the load we are deliberately creating.
        self._self_load = self_load
        self._lock = threading.Lock()
        self._permits = 0            # 0 = never sampled yet
        self._last_sample = 0.0
        self._last_change = 0.0
        self._last: LoadSample | None = None
        # fallback load estimation state (no getloadavg, e.g. Windows)
        self._cpu_mark = time.process_time()
        self._wall_mark = time.monotonic()

    # -- measurement -------------------------------------------------------

    def _read_load(self) -> tuple[float, bool]:
        """Return (current load, estimated?) across the WHOLE machine.

        Three sources, best first:

        1. /proc/loadavg on Linux, which carries the 1-minute average
           AND the instantaneous runnable count. We take whichever is
           higher, and the second one is why: a load AVERAGE lags by up
           to a minute, so a swarm that only read load1 would happily
           keep three permits open for a full minute after the user
           started a compile. The runnable count moves the instant they
           press enter, which is the moment we need to get out of the
           way. The average still matters for sustained load the
           instantaneous count keeps missing between samples, so the
           max of the two is the honest reading.
        2. os.getloadavg() elsewhere (macOS, BSD) — lagging, but real.
        3. Our own process cpu rate, where neither exists (Windows). It
           cannot see other processes, so it only keeps the swarm from
           fighting itself; the small ceiling and niceness carry the
           rest.
        """
        if self._load_source is not None:
            return float(self._load_source()), False
        try:
            parts = Path("/proc/loadavg").read_text().split()
            load1 = float(parts[0])
            # "3/512" -> 3 runnable entities, this reader among them
            runnable = float(parts[3].split("/")[0]) - 1.0
            return max(load1, runnable), False
        except (OSError, ValueError, IndexError):
            pass
        try:
            return float(os.getloadavg()[0]), False
        except (OSError, AttributeError):
            pass
        now_cpu, now_wall = time.process_time(), time.monotonic()
        d_cpu = max(0.0, now_cpu - self._cpu_mark)
        d_wall = max(1e-6, now_wall - self._wall_mark)
        self._cpu_mark, self._wall_mark = now_cpu, now_wall
        return d_cpu / d_wall, True

    def sample(self, *, force: bool = False) -> LoadSample:
        """Read the load and (re)decide the permit count."""
        with self._lock:
            now = time.monotonic()
            if (not force and self._last is not None
                    and now - self._last_sample < SAMPLE_INTERVAL):
                return self._last
            self._last_sample = now
            raw, estimated = self._read_load()
            try:
                own = float(self._self_load()) if self._self_load else 0.0
            except Exception:  # noqa: BLE001 — a probe never breaks the governor
                own = 0.0
            # what OTHER work on this machine is using right now
            load1 = max(0.0, raw - own)
            pressure = load1 / self.cores

            if self._permits == 0:
                # First look: jump straight to the spare capacity rather
                # than ramping from one, so a burst of subagents is not
                # punished for arriving first.
                target = int(clamp(int(self.cores - load1), 1, self.ceiling))
            elif now - self._last_change < CHANGE_COOLDOWN:
                target = self._permits          # still cooling down
            elif pressure >= 0.85:
                target = 1                      # box is busy — get out of the way
            elif pressure >= 0.65:
                target = max(1, self._permits - 1)
            elif pressure <= 0.45:
                target = min(self.ceiling, self._permits + 1)
            else:
                target = self._permits          # dead band — hold steady

            if target != self._permits:
                self._last_change = now
                _log.debug("cpu permits %d -> %d (load1=%.2f cores=%d)",
                           self._permits, target, load1, self.cores)
            self._permits = target
            self._last = LoadSample(at=time.time(), cores=self.cores,
                                    raw=raw, own=own, load1=load1,
                                    pressure=pressure, permits=target,
                                    estimated=estimated)
            return self._last

    def permits(self) -> int:
        """Current cpu permit count (samples if the reading is stale)."""
        return self.sample().permits

    def pressure(self) -> float:
        return self.sample().pressure

    # -- courtesy ----------------------------------------------------------

    def breathe(self) -> float:
        """Hand the machine a slice back, sized to current pressure.

        Called by a worker right after a chunk of LOCAL work. On an idle
        box this is a bare thread yield and costs nothing measurable. On
        a loaded box it inserts a few milliseconds of real sleep, which
        is what turns "the fan spun up" into "I didn't notice".
        """
        p = self.pressure()
        if p <= 0.65:
            time.sleep(0)          # yield the GIL, no more
            return 0.0
        nap = min(0.05, 0.02 * p)
        time.sleep(nap)
        return nap

    def snapshot(self) -> dict:
        s = self._last or self.sample()
        return s.to_dict()


# ---------------------------------------------------------------------------
# AdaptiveSemaphore — a semaphore whose capacity moves under it
# ---------------------------------------------------------------------------

class AdaptiveSemaphore:
    """Bounded admission whose bound is asked for, not stored.

    A normal Semaphore fixes its count at construction. This one reads
    the governor on every admission decision, so shrinking is instant
    (new entrants simply wait) and growing is picked up by the next
    waiter. Holders are never interrupted — a permit already granted
    stays granted until its work finishes, so shrinking can never tear
    a tool call in half.
    """

    def __init__(self, governor: LoadGovernor, *, reserve: int = 1) -> None:
        self.governor = governor
        # Permits held back from BACKGROUND work so the sovereign turn —
        # the thing the user is watching — never queues behind a pile of
        # subagents. Without it, "spawn eight and keep working" means the
        # user's own next file read waits for a subagent's grep, and the
        # session feels slow precisely when it is being most productive.
        self.reserve = max(0, int(reserve))
        self._cond = threading.Condition(threading.Lock())
        self._active = 0
        self._foreground = 0
        self._peak = 0
        self._waited_total = 0.0
        self._admissions = 0
        self._preempted = 0

    def _limit_for(self, lane: str) -> int:
        """Permits this lane may use right now.

        Foreground sees the whole grant. Background leaves `reserve`
        behind — but never so much that it cannot run at all, because a
        lane that can never be admitted is not a priority scheme, it is
        a deadlock with good intentions.
        """
        permits = self.governor.permits()
        background = max(1, permits - self.reserve)
        if lane != FOREGROUND:
            return background
        # The reservation has to HOLD at the one moment it matters. On a
        # loaded box the governor grants a single permit, and background
        # can never be floored below one — so a plain `return permits`
        # would let a subagent take that only permit and leave the
        # sovereign queueing behind it, precisely when the user is most
        # likely to notice. Foreground therefore sits above whatever
        # background may use, by the size of the reserve. That can put
        # one extra worker over the governor's grant for the duration of
        # one short sovereign tool call, which is the entire point of
        # reserving: the user's own work is not the load we are
        # protecting them from.
        return max(permits, background + self.reserve)

    def acquire(self, timeout: float | None = None,
                lane: str = BACKGROUND) -> bool:
        t0 = time.monotonic()
        deadline = None if timeout is None else t0 + max(0.0, timeout)
        with self._cond:
            while self._active >= self._limit_for(lane):
                if deadline is not None:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return False
                    self._cond.wait(min(PERMIT_RECHECK, left))
                else:
                    self._cond.wait(PERMIT_RECHECK)
            self._active += 1
            if lane == FOREGROUND:
                self._foreground += 1
                if self._active > self._limit_for(BACKGROUND):
                    self._preempted += 1   # used a reserved slot
            self._peak = max(self._peak, self._active)
            self._admissions += 1
            self._waited_total += time.monotonic() - t0
            return True

    def release(self, lane: str = BACKGROUND) -> None:
        with self._cond:
            self._active = max(0, self._active - 1)
            if lane == FOREGROUND:
                self._foreground = max(0, self._foreground - 1)
            self._cond.notify()

    @contextmanager
    def hold(self, timeout: float | None = None,
             lane: str = BACKGROUND):
        """Context manager around one permit. Yields True when admitted.

        On timeout it yields False and runs the body anyway — admission
        control here is a courtesy, never a correctness gate, and a
        subagent must not be failed because the box was busy.
        """
        ok = self.acquire(timeout, lane)
        try:
            yield ok
        finally:
            if ok:
                self.release(lane)

    @property
    def active(self) -> int:
        with self._cond:
            return self._active

    @property
    def active_hint(self) -> int:
        """Holders right now, read WITHOUT taking the lock.

        This exists for exactly one caller: the governor's self-load
        probe. permits() is consulted from inside acquire(), which holds
        this condition's lock, so a probe that locked would deadlock the
        moment the governor asked "how many of you are running?". An int
        read is atomic under the GIL and being stale by one holder is
        meaningless to a load heuristic, so the hint is both safe and
        sufficient.
        """
        return self._active

    def snapshot(self) -> dict:
        # ask the governor BEFORE taking the lock: permits() calls back
        # into the self-load probe, and this lock is not re-entrant
        limit = self.governor.permits()
        with self._cond:
            avg = (self._waited_total / self._admissions
                   if self._admissions else 0.0)
            return {"active": self._active, "peak": self._peak,
                    "limit": limit, "foreground": self._foreground,
                    "reserved": self.reserve,
                    "admissions": self._admissions,
                    "preempted": self._preempted,
                    "avg_wait_ms": round(avg * 1000, 1)}


# ---------------------------------------------------------------------------
# ElasticPool — threads that appear on demand and retire themselves
# ---------------------------------------------------------------------------

class ElasticPool:
    """A thread pool that owns nothing when it is not working.

    Threads are started only when there is more outstanding work than
    threads to do it, up to max_threads, and each one retires after
    idle_ttl seconds of an empty queue. An idle swarm is therefore
    exactly as expensive as no swarm at all.
    """

    def __init__(self, name: str = "swarm", max_threads: int = 8,
                 idle_ttl: float = IDLE_TTL, nice: bool = True) -> None:
        self.name = name
        self.max_threads = max(1, int(max_threads))
        self.idle_ttl = float(idle_ttl)
        self.nice = bool(nice)
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._lock = threading.Lock()
        self._threads = 0
        self._busy = 0
        self._outstanding = 0     # submitted and not yet finished/dropped
        self._started = 0
        self._closed = False

    def submit(self, fn: Callable, *args: Any, **kwargs: Any) -> None:
        """Queue a job, growing the pool when work outnumbers threads.

        Growth is decided from OUTSTANDING WORK (submitted minus
        finished), never from an "is a thread idle right now" counter.
        That distinction is the whole correctness of this pool: a thread
        that has just been handed a job is, for a few instructions,
        neither parked in get() nor yet marked busy, and a pool that
        reads those instructions as spare capacity quietly collapses
        back to serial — submit two jobs in quick succession and the
        second one never gets a thread. Outstanding work has no such
        window: it only changes under this lock, and it counts what is
        actually owed, not what a thread happens to be doing this
        microsecond.

        The put and the grow decision also happen under the SAME lock a
        retiring thread must take, so a job queued just as a thread
        retires is always seen by that thread's final queue check.
        """
        if self._closed:
            raise RuntimeError(f"pool {self.name!r} is closed")
        with self._lock:
            self._q.put((fn, args, kwargs))
            self._outstanding += 1
            if self._threads < min(self.max_threads, self._outstanding):
                self._threads += 1
                self._started += 1
                t = threading.Thread(
                    target=self._worker, daemon=True,
                    name=f"{self.name}:{self._started}")
                t.start()

    def _worker(self) -> None:
        if self.nice:
            _apply_nice()
        while True:
            try:
                job = self._q.get(timeout=self.idle_ttl)
            except queue.Empty:
                with self._lock:
                    if not self._q.empty():
                        continue        # work landed in the race window
                    self._threads -= 1
                    return
            with self._lock:
                self._busy += 1
            fn, args, kwargs = job
            try:
                fn(*args, **kwargs)
            except BaseException:       # noqa: BLE001 — a job never kills a thread
                _log.exception("swarm job raised in pool %s", self.name)
            finally:
                self._q.task_done()
                with self._lock:
                    self._busy -= 1
                    self._outstanding = max(0, self._outstanding - 1)

    def drain(self) -> list[tuple]:
        """Discard every queued job that has not started and RETURN them.

        Handing the jobs back matters: the caller owns whatever promise
        was made to the submitter (a Ticket, in Swarm's case) and has to
        settle it, or a waiter blocks forever on work that will never
        run. Jobs already executing are untouched — this is a queue
        cancel, not a kill.
        """
        dropped: list[tuple] = []
        with self._lock:
            while True:
                try:
                    dropped.append(self._q.get_nowait())
                except queue.Empty:
                    break
                self._q.task_done()
            self._outstanding = max(0, self._outstanding - len(dropped))
        return dropped

    def close(self) -> None:
        """Stop accepting work. Live threads retire on their own ttl."""
        self._closed = True

    @property
    def threads(self) -> int:
        with self._lock:
            return self._threads

    @property
    def queued(self) -> int:
        return self._q.qsize()

    def snapshot(self) -> dict:
        with self._lock:
            return {"threads": self._threads, "busy": self._busy,
                    "idle": max(0, self._threads - self._busy),
                    "queued": self._q.qsize(),
                    "outstanding": self._outstanding,
                    "max": self.max_threads, "started": self._started}


# ---------------------------------------------------------------------------
# NetWindow — congestion control for the provider, not just a cap
# ---------------------------------------------------------------------------

class NetWindow:
    """How many model calls may be in flight, decided by evidence.

    A fixed ceiling is a guess, and it is wrong in both directions: too
    low and the swarm leaves throughput on the table all day; too high
    and every worker discovers the provider's real limit at the same
    instant, eats a 429, and backs off together — the thundering herd
    that makes a rate limit feel like an outage.

    So the window MOVES, on TCP's rule, for TCP's reason:

      * additive increase — a clean reply widens the window by 1/window,
        so it takes a full window of successes to earn one more slot. It
        probes for capacity without ever overshooting it.
      * multiplicative decrease — a rate limit HALVES it, immediately.
        Shedding load slowly does not clear congestion; shedding it fast
        does.

    The practical effect is that the swarm finds the provider's actual
    concurrency by itself, holds just under it, and recovers in seconds
    instead of minutes.

    It opens AT the ceiling rather than probing up to it, which is the
    opposite of TCP's slow start and right for the same reason TCP is
    right: slow start exists because the path's capacity is unknown, but
    here the ceiling is not a guess about a stranger's network — it is
    the concurrency the operator already chose. A bound we were handed is
    not a bound we need to rediscover one success at a time, and making a
    batch of eight crawl up from four would cost the user real seconds on
    every single fan-out to buy politeness nobody asked for. The window
    exists to come DOWN from the ceiling when the provider says so, and
    to climb back once it stops saying so.
    """

    def __init__(self, ceiling: int, *, floor: int = 1,
                 start: float | None = None) -> None:
        self.ceiling = max(1, int(ceiling))
        self.floor = max(1, min(int(floor), self.ceiling))
        self._window = float(start if start is not None else self.ceiling)
        self._cond = threading.Condition(threading.Lock())
        self._in_flight = 0
        self.completed = 0
        self.rate_limited = 0
        self.failures = 0
        self._latency_total = 0.0
        self._peak_window = self._window

    # -- feedback ----------------------------------------------------------

    def reward(self, latency: float = 0.0) -> None:
        """A clean reply. Widen by one over the current window."""
        with self._cond:
            self.completed += 1
            self._latency_total += max(0.0, latency)
            if self._window < self.ceiling:
                self._window = min(float(self.ceiling),
                                   self._window + 1.0 / self._window)
                self._peak_window = max(self._peak_window, self._window)
                self._cond.notify()

    def penalize(self, *_a: Any, **_kw: Any) -> None:
        """A rate limit. Halve the window, now.

        Takes and ignores arguments so it can be handed straight to a
        retry loop as its on_rate_limit hook.
        """
        with self._cond:
            self.rate_limited += 1
            self._window = max(float(self.floor), self._window / 2.0)
            _log.debug("net window halved to %.2f after a rate limit",
                       self._window)

    def fail(self) -> None:
        """A call raised. Treated as mild congestion: back off one slot,
        not half the window — most failures are not the provider telling
        us we are too fast, and over-reacting to them would keep the
        window pinned at the floor for the rest of the session."""
        with self._cond:
            self.failures += 1
            self._window = max(float(self.floor), self._window - 1.0)

    # -- admission ---------------------------------------------------------

    @contextmanager
    def slot(self):
        """Hold one in-flight model call, and learn from how it went."""
        with self._cond:
            while self._in_flight >= int(self._window):
                self._cond.wait(PERMIT_RECHECK)
            self._in_flight += 1
        t0 = time.monotonic()
        ok = False
        try:
            yield True
            ok = True
        finally:
            with self._cond:
                self._in_flight -= 1
                self._cond.notify()
            if ok:
                self.reward(time.monotonic() - t0)
            else:
                self.fail()

    @property
    def window(self) -> int:
        return int(self._window)

    def snapshot(self) -> dict:
        with self._cond:
            avg = (self._latency_total / self.completed
                   if self.completed else 0.0)
            return {"window": int(self._window),
                    "ceiling": self.ceiling,
                    "peak": int(self._peak_window),
                    "in_flight": self._in_flight,
                    "completed": self.completed,
                    "rate_limited": self.rate_limited,
                    "failures": self.failures,
                    "avg_latency_ms": round(avg * 1000, 1)}


# ---------------------------------------------------------------------------
# Coalescer — one execution for N identical reads
# ---------------------------------------------------------------------------

@dataclass
class _Entry:
    done: threading.Event
    generation: int
    created: float = 0.0
    value: Any = None
    error: BaseException | None = None


class Coalescer:
    """Fan-out's dominant waste is repetition, so stop repeating.

    Point five researchers at the same module and they will all read the
    same files, run the same greps, and walk the same directory — five
    times the syscalls and five times the cpu for one answer. Parallelism
    multiplies that waste instead of hiding it, which is exactly how
    "more subagents" turns into "my laptop got hot".

    Two mechanisms, both narrow on purpose:

      SINGLEFLIGHT   identical calls that are in flight AT THE SAME TIME
                     collapse into one execution; the rest wait on it and
                     share the result. Always safe: they would each have
                     produced the same answer from the same state.
      SHORT CACHE    a result may be served again for `ttl` seconds, and
                     only until anything writes. Any write bumps a
                     generation counter and every older result is dropped
                     on the spot — the cache can never outlive the state
                     it described.

    ttl=0 disables the cache and leaves singleflight alone, which is the
    right setting for anything whose answer can change without us being
    the one who changed it.
    """

    def __init__(self, *, ttl: float = 10.0, max_entries: int = 512) -> None:
        self.ttl = max(0.0, float(ttl))
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._generation = 0
        self.hits_cached = 0
        self.hits_joined = 0
        self.misses = 0
        self.invalidations = 0

    def invalidate(self) -> None:
        """Something wrote. Every cached read is now suspect — drop it.

        Deliberately total rather than clever: a write we cannot fully
        reason about (a command that touched who-knows-what) must not
        leave a single stale read behind, and a fan-out re-reads what it
        needs in milliseconds anyway. Correctness is worth more than the
        cache."""
        with self._lock:
            self._generation += 1
            self.invalidations += 1
            self._entries = {k: e for k, e in self._entries.items()
                             if not e.done.is_set()}   # keep in-flight joins

    def run(self, key: str, fn: Callable[[], Any]) -> tuple[Any, str]:
        """Return (value, "ran" | "joined" | "cached"). Re-raises errors.

        The kind is decided once, under the lock, at the moment we look
        at the entry — never inferred afterwards from timing, which is
        the sort of thing that reads fine and reports nonsense.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.generation != self._generation:
                entry = None                      # a write invalidated it
            elif (entry is not None and entry.done.is_set()
                    and not (self.ttl and now - entry.created <= self.ttl)):
                entry = None                      # settled but stale

            if entry is not None:
                if entry.done.is_set():
                    self.hits_cached += 1
                    kind = "cached"
                else:
                    self.hits_joined += 1
                    kind = "joined"
                leader = False
            else:
                if len(self._entries) >= self.max_entries:
                    self._evict_locked()
                entry = _Entry(done=threading.Event(),
                               generation=self._generation)
                self._entries[key] = entry
                self.misses += 1
                kind = "ran"
                leader = True

        if leader:
            try:
                value = fn()
            except BaseException as e:            # noqa: BLE001
                entry.error = e
                entry.created = time.monotonic()
                entry.done.set()
                with self._lock:
                    # a failure is never cached: drop it so the next
                    # caller gets a real attempt, not an echo of a
                    # transient error
                    if self._entries.get(key) is entry:
                        self._entries.pop(key, None)
                raise
            entry.value = value
            entry.created = time.monotonic()
            entry.done.set()
            if not self.ttl:
                with self._lock:                  # singleflight only
                    if self._entries.get(key) is entry:
                        self._entries.pop(key, None)
            return value, kind

        if not entry.done.wait(120.0):
            raise TimeoutError(f"coalesced call {key!r} never settled")
        if entry.error is not None:
            raise entry.error
        return entry.value, kind

    def _evict_locked(self) -> None:
        """Drop the oldest settled entries. Never evicts an in-flight
        one — somebody is waiting on it."""
        settled = [(e.created, k) for k, e in self._entries.items()
                   if e.done.is_set()]
        settled.sort()
        for _, k in settled[: max(1, len(settled) // 4)]:
            self._entries.pop(k, None)

    def snapshot(self) -> dict:
        with self._lock:
            total = self.hits_cached + self.hits_joined + self.misses
            saved = self.hits_cached + self.hits_joined
            return {"entries": len(self._entries),
                    "generation": self._generation,
                    "ran": self.misses, "joined": self.hits_joined,
                    "cached": self.hits_cached,
                    "invalidations": self.invalidations,
                    "saved_pct": round(100.0 * saved / total, 1)
                    if total else 0.0}


# ---------------------------------------------------------------------------
# Ticket — a one-shot result handle with no polling
# ---------------------------------------------------------------------------

@dataclass
class Ticket:
    """A future, minus the machinery nobody here needs.

    Waiting on a Ticket parks on an Event: the completing worker wakes
    the waiter directly. No sleep loop, no poll interval, no cpu.
    """
    id: str
    label: str = ""
    result: Any = None
    error: BaseException | None = None
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float = 0.0
    _done: threading.Event = field(default_factory=threading.Event)

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def elapsed_ms(self) -> int:
        end = self.finished_at or time.monotonic()
        return int((end - self.started_at) * 1000)

    def _settle(self, result: Any, error: BaseException | None) -> None:
        self.result = result
        self.error = error
        self.finished_at = time.monotonic()
        self._done.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def get(self, timeout: float | None = None) -> Any:
        """Block for the result. Re-raises the job's exception, if any."""
        if not self._done.wait(timeout):
            raise TimeoutError(f"ticket {self.id} did not finish in time")
        if self.error is not None:
            raise self.error
        return self.result


# ---------------------------------------------------------------------------
# Swarm — the whole thing, assembled
# ---------------------------------------------------------------------------

class Swarm:
    """Real parallelism for subagents, metered so nobody feels it.

        swarm = Swarm(max_parallel=8)
        tickets = [swarm.submit(run, a) for a in agents]   # all at once
        swarm.gather(tickets, timeout=600)                 # zero-spin wait

    and inside a job, around the two kinds of second:

        with swarm.net():          # parked on a socket — basically free
            reply = call_the_model()
        with swarm.cpu():          # real local work — metered
            out = tool.handler(**args)
        swarm.breathe()            # give the box a slice back
    """

    def __init__(self, *, max_parallel: int = 8,
                 cpu_ceiling: int = CPU_CEILING,
                 idle_ttl: float = IDLE_TTL,
                 name: str = "swarm",
                 governor: LoadGovernor | None = None,
                 nice: bool = True, cache_ttl: float = 10.0,
                 reserve: int = 1) -> None:
        self.max_parallel = max(1, int(max_parallel))
        self.governor = governor or LoadGovernor(ceiling=cpu_ceiling)
        self.pool = ElasticPool(name=name, max_threads=self.max_parallel,
                                idle_ttl=idle_ttl, nice=nice)
        self._cpu = AdaptiveSemaphore(self.governor, reserve=reserve)
        # let the governor discount our own workers from what it reads
        if self.governor._self_load is None:
            self.governor._self_load = lambda: self._cpu.active_hint
        # the provider's real limit, discovered rather than assumed
        self.net_window = NetWindow(self.max_parallel)
        # identical reads collapse into one; ttl=0 leaves singleflight
        # alone for anything whose answer can change under us
        self.cache = Coalescer(ttl=cache_ttl)
        self.singleflight = Coalescer(ttl=0.0)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._live: dict[str, Ticket] = {}
        self._completed = 0

    # -- admission ---------------------------------------------------------

    @contextmanager
    def cpu(self, timeout: float | None = 30.0,
            lane: str = BACKGROUND):
        """Hold a cpu permit for a chunk of LOCAL work."""
        with self._cpu.hold(timeout, lane) as ok:
            yield ok

    @contextmanager
    def foreground(self, timeout: float | None = 30.0):
        """A cpu permit for the sovereign turn — never queues behind
        subagents, because a reserved slot is always kept for it."""
        with self._cpu.hold(timeout, FOREGROUND) as ok:
            yield ok

    @contextmanager
    def net(self):
        """Hold one in-flight model call, inside the congestion window."""
        with self.net_window.slot():
            yield True

    def breathe(self) -> float:
        return self.governor.breathe()

    # -- doing the same work once ------------------------------------------

    def once(self, key: str, fn: Callable[[], Any], *,
             cacheable: bool = True) -> tuple[Any, str]:
        """Run fn, unless an identical call already is (or just was).

        `cacheable=False` still collapses calls that overlap in time —
        always safe, since they would each have read the same state —
        but never serves a finished result again.
        """
        pool = self.cache if cacheable else self.singleflight
        return pool.run(key, fn)

    def invalidate(self) -> None:
        """Something wrote: every cached read is suspect. Drop them."""
        self.cache.invalidate()

    # -- work --------------------------------------------------------------

    def submit(self, fn: Callable, *args: Any, label: str = "",
               **kwargs: Any) -> Ticket:
        """Run fn in parallel. Returns immediately with a Ticket."""
        ticket = Ticket(id=f"t{next(self._ids)}", label=label)
        with self._lock:
            self._live[ticket.id] = ticket

        def _run() -> None:
            try:
                ticket._settle(fn(*args, **kwargs), None)
            except BaseException as e:      # noqa: BLE001 — carried, not raised
                ticket._settle(None, e)
            finally:
                self._retire(ticket)

        # the queue holds opaque callables, so the ticket rides along on
        # the job itself — that is how drain() knows what to settle
        _run.ticket = ticket               # type: ignore[attr-defined]
        self.pool.submit(_run)
        return ticket

    def _retire(self, ticket: Ticket) -> None:
        with self._lock:
            if self._live.pop(ticket.id, None) is not None:
                self._completed += 1

    def gather(self, tickets: Iterable[Ticket], timeout: float | None = None,
               should_cancel: "Callable[[], bool] | None" = None
               ) -> list[Ticket]:
        """Wait for every ticket. No polling unless a cancel hook is set.

        With should_cancel the wait is chopped into PERMIT_RECHECK slices
        so the hook is consulted — still four cheap checks a second, not
        a spin.
        """
        tickets = list(tickets)
        deadline = None if timeout is None else time.monotonic() + timeout
        for t in tickets:
            while True:
                if should_cancel is not None and should_cancel():
                    return tickets
                if deadline is None:
                    slice_ = None if should_cancel is None else PERMIT_RECHECK
                else:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return tickets
                    slice_ = left if should_cancel is None else min(
                        PERMIT_RECHECK, left)
                if t.wait(slice_):
                    break
                if should_cancel is None:
                    break               # real timeout, not a cancel slice
        return tickets

    def map(self, fn: Callable, items: Iterable, timeout: float | None = None
            ) -> list[Ticket]:
        """Submit fn over items all at once and wait for the lot."""
        tickets = [self.submit(fn, it, label=str(it)[:60]) for it in items]
        return self.gather(tickets, timeout=timeout)

    def drain(self) -> int:
        """Cancel everything queued but not started. Each dropped job's
        Ticket is settled with Cancelled, so no waiter is ever left
        parked on work that will never run. Returns the count."""
        dropped = self.pool.drain()
        for fn, _args, _kwargs in dropped:
            ticket = getattr(fn, "ticket", None)
            if ticket is not None and not ticket.done:
                ticket._settle(None, Cancelled("job cancelled before start"))
                self._retire(ticket)
        return len(dropped)

    def close(self) -> None:
        self.pool.close()

    # -- observability -----------------------------------------------------

    @property
    def in_flight(self) -> int:
        with self._lock:
            return len(self._live)

    def snapshot(self) -> dict:
        """One dict for the dashboard: how much of the box are we using?"""
        return {"in_flight": self.in_flight, "completed": self._completed,
                "pool": self.pool.snapshot(), "cpu": self._cpu.snapshot(),
                "load": self.governor.snapshot(),
                "net": self.net_window.snapshot(),
                "cache": self.cache.snapshot(),
                "singleflight": self.singleflight.snapshot(),
                "max_parallel": self.max_parallel}

    @staticmethod
    def _meter(used: float, total: float, width: int = 10) -> str:
        """A small bar. Reading a number tells you a value; reading a bar
        tells you a RATIO, which is the thing you actually want to know
        about a limit you are near."""
        total = max(1e-9, float(total))
        filled = int(round(clamp(used / total, 0.0, 1.0) * width))
        return "█" * filled + "░" * (width - filled)

    def format_status(self) -> str:
        """One line per concern, each answering "how close to the edge?".

        Numbers alone tell you a value; a bar tells you a RATIO, which is
        the only thing worth knowing about a limit you might be near.
        """
        s = self.snapshot()
        load, cpu, pool = s["load"], s["cpu"], s["pool"]
        net, cache = s["net"], s["cache"]
        bar = self._meter

        work = (f"  work     {bar(pool['busy'], pool['max'])} "
                f"{pool['busy']}/{pool['max']} threads")
        if pool["queued"]:
            work += f" · {pool['queued']} queued"

        cpu_line = (f"  cpu      {bar(cpu['active'], max(1, cpu['limit']))} "
                    f"{cpu['active']}/{cpu['limit']} permits")
        if cpu["reserved"]:
            cpu_line += f" · {cpu['reserved']} reserved for you"

        provider = (f"  provider {bar(net['in_flight'], max(1, net['window']))} "
                    f"window {net['window']}/{net['ceiling']} · "
                    f"{net['avg_latency_ms']}ms avg")
        if net["rate_limited"]:
            provider += f" · backed off {net['rate_limited']}x"

        lines = [f"SWARM  {s['in_flight']} in flight · {s['completed']} done",
                 work, cpu_line,
                 f"  machine  {bar(load['pressure'], 1.0)} "
                 f"other load {load['load1']}/{load['cores']}",
                 provider]

        saved = cache["joined"] + cache["cached"]
        if saved:
            lines.append(f"  reuse    {bar(cache['saved_pct'], 100.0)} "
                         f"{saved} call(s) never repeated "
                         f"({cache['saved_pct']}%)")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-test — deterministic, offline, and it really proves parallelism
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    def _self_test() -> None:
        # -- governor: headroom, bounds, hysteresis ------------------------
        g = LoadGovernor(ceiling=4, cores=8, load_source=lambda: 0.0)
        s = g.sample(force=True)
        assert s.permits == 4, s              # idle box -> full ceiling
        assert s.pressure == 0.0 and not s.estimated

        busy = LoadGovernor(ceiling=4, cores=4, load_source=lambda: 4.0)
        assert busy.sample(force=True).permits == 1   # committed box -> 1

        # never below 1, never above the ceiling, whatever the load says
        for load in (-5.0, 0.0, 3.0, 99.0):
            gg = LoadGovernor(ceiling=3, cores=4, load_source=lambda l=load: l)
            assert 1 <= gg.sample(force=True).permits <= 3, load

        # a quiet box that goes busy steps DOWN (one at a time, after the
        # cooldown), and never tears a running holder away
        level = [0.0]
        step = LoadGovernor(ceiling=4, cores=4,
                            load_source=lambda: level[0])
        assert step.sample(force=True).permits == 4
        level[0] = 4.0
        step._last_change = 0.0               # pretend the cooldown elapsed
        assert step.sample(force=True).permits == 1

        # estimated fallback path (no getloadavg) still yields sane permits
        est = LoadGovernor(ceiling=2, cores=2)
        est._load_source = None
        assert 1 <= est.sample(force=True).permits <= 2

        # -- adaptive semaphore: the bound is real -------------------------
        sem = AdaptiveSemaphore(LoadGovernor(ceiling=2, cores=8,
                                             load_source=lambda: 0.0))
        seen, seen_lock = [], threading.Lock()
        inside = [0]
        go = threading.Event()

        def hog() -> None:
            go.wait(5.0)
            with sem.hold(timeout=5.0) as ok:
                assert ok
                with seen_lock:
                    inside[0] += 1
                    seen.append(inside[0])
                time.sleep(0.05)
                with seen_lock:
                    inside[0] -= 1

        hogs = [threading.Thread(target=hog) for _ in range(8)]
        for t in hogs:
            t.start()
        go.set()
        for t in hogs:
            t.join(10.0)
        assert max(seen) <= 2, f"permit bound breached: {seen}"
        assert sem.active == 0
        assert sem.snapshot()["admissions"] == 8

        # a timed-out permit still lets the body run (courtesy, not a gate)
        full = AdaptiveSemaphore(LoadGovernor(ceiling=1, cores=1,
                                              load_source=lambda: 99.0))
        assert full.acquire(timeout=0.1) is True      # first one is free
        ran = []
        with full.hold(timeout=0.05) as ok:
            ran.append(ok)
        assert ran == [False], ran
        full.release()

        # -- elastic pool: REAL parallelism, proved by a barrier -----------
        # Six jobs must be in flight simultaneously for the barrier to
        # trip. Under the old serial executor this times out.
        pool = ElasticPool(name="test", max_threads=6, idle_ttl=0.2)
        barrier = threading.Barrier(6, timeout=5.0)
        tripped, trip_lock = [], threading.Lock()

        def at_barrier(i: int) -> None:
            barrier.wait()
            with trip_lock:
                tripped.append(i)

        for i in range(6):
            pool.submit(at_barrier, i)
        t_end = time.monotonic() + 8.0
        while len(tripped) < 6 and time.monotonic() < t_end:
            time.sleep(0.01)
        assert len(tripped) == 6, f"not parallel: only {len(tripped)} met"
        assert pool.threads == 6

        # and the pool gives the threads BACK when the work stops
        idle_end = time.monotonic() + 5.0
        while pool.threads > 0 and time.monotonic() < idle_end:
            time.sleep(0.05)
        assert pool.threads == 0, f"{pool.threads} threads never retired"

        # a job that raises never takes its thread with it (the
        # traceback this logs is the point of the test — muted so the
        # self-test's own output stays clean)
        pool2 = ElasticPool(name="boom", max_threads=2, idle_ttl=0.2)
        after = threading.Event()
        _log.disabled = True
        try:
            pool2.submit(lambda: (_ for _ in ()).throw(ValueError("boom")))
            pool2.submit(after.set)
            assert after.wait(5.0), "pool died on a raising job"
        finally:
            _log.disabled = False

        # drain drops queued work without touching what runs
        pool3 = ElasticPool(name="drain", max_threads=1, idle_ttl=0.2)
        hold = threading.Event()
        pool3.submit(lambda: hold.wait(5.0))
        for _ in range(5):
            pool3.submit(lambda: None)
        time.sleep(0.1)
        assert len(pool3.drain()) == 5
        hold.set()

        # -- swarm: tickets, errors, gather, cancel ------------------------
        sw = Swarm(max_parallel=6, cpu_ceiling=2, idle_ttl=0.2,
                   governor=LoadGovernor(ceiling=2, cores=8,
                                         load_source=lambda: 0.0))

        # parallelism end to end: six model-ish calls that each "block on
        # the network" for 0.2s finish in well under the 1.2s a serial
        # queue would need
        def fake_call(n: int) -> int:
            with sw.net():
                time.sleep(0.2)
            with sw.cpu(timeout=5.0):
                sw.breathe()
            return n * 2

        t0 = time.monotonic()
        tickets = [sw.submit(fake_call, i, label=f"job{i}") for i in range(6)]
        sw.gather(tickets, timeout=10.0)
        wall = time.monotonic() - t0
        assert all(t.done for t in tickets)
        assert [t.result for t in tickets] == [0, 2, 4, 6, 8, 10]
        assert wall < 1.0, f"not parallel: {wall:.2f}s for 6x0.2s jobs"

        # an exception is carried on the ticket, not raised into the pool
        bad = sw.submit(lambda: 1 / 0)
        bad.wait(5.0)
        assert isinstance(bad.error, ZeroDivisionError)
        try:
            bad.get(timeout=1.0)
            raise AssertionError("get() must re-raise the job's error")
        except ZeroDivisionError:
            pass

        # gather honours a timeout instead of hanging forever
        stuck = threading.Event()
        slow = sw.submit(lambda: stuck.wait(10.0))
        t0 = time.monotonic()
        sw.gather([slow], timeout=0.3)
        assert 0.2 < time.monotonic() - t0 < 2.0
        assert not slow.done

        # ... and returns at once when the user cancels
        t0 = time.monotonic()
        sw.gather([slow], timeout=10.0, should_cancel=lambda: True)
        assert time.monotonic() - t0 < 1.0
        stuck.set()
        slow.wait(5.0)

        # map is submit-all-then-wait, in order
        mapped = sw.map(lambda n: n + 1, [1, 2, 3], timeout=10.0)
        assert [t.result for t in mapped] == [2, 3, 4]

        # drain settles the tickets it drops — a cancelled job must never
        # leave a waiter parked forever, and must not linger as in-flight
        small = Swarm(max_parallel=1, cpu_ceiling=1, idle_ttl=0.2,
                      governor=LoadGovernor(ceiling=1, cores=8,
                                            load_source=lambda: 0.0))
        block = threading.Event()
        busy = small.submit(lambda: block.wait(5.0))
        queued = [small.submit(lambda: None) for _ in range(3)]
        time.sleep(0.1)
        assert small.drain() == 3
        for t in queued:
            assert t.wait(2.0) and isinstance(t.error, Cancelled)
        block.set()
        busy.wait(5.0)
        assert small.in_flight == 0, small.snapshot()
        small.close()

        # -- observability -------------------------------------------------
        snap = sw.snapshot()
        assert snap["completed"] >= 10 and snap["in_flight"] == 0
        assert snap["cpu"]["peak"] <= 2, snap        # ceiling held all along
        assert snap["load"]["cores"] == 8
        assert "SWARM" in sw.format_status()
        sw.close()

        # -- net window: AIMD, and it really moves ------------------------
        nw = NetWindow(8)
        assert nw.window == 8                 # opens at the ceiling
        nw.penalize()
        assert nw.window == 4, nw.window      # a rate limit HALVES it
        nw.penalize()
        assert nw.window == 2
        for _ in range(200):
            nw.reward(0.01)                   # earns its way back
        assert nw.window == 8
        nw.fail()
        assert nw.window == 7                 # a plain error costs one
        floor = NetWindow(4, floor=2)
        for _ in range(10):
            floor.penalize()
        assert floor.window == 2, floor.window  # never below the floor

        # the window is a real admission bound, not a number on a report
        tight = NetWindow(2)
        peak, live, nlock = [0], [0], threading.Lock()
        release = threading.Event()

        def call() -> None:
            with tight.slot():
                with nlock:
                    live[0] += 1
                    peak[0] = max(peak[0], live[0])
                release.wait(2.0)
                with nlock:
                    live[0] -= 1

        callers = [threading.Thread(target=call) for _ in range(6)]
        for t in callers:
            t.start()
        time.sleep(0.2)
        release.set()
        for t in callers:
            t.join(10.0)
        assert peak[0] <= 2, f"net window breached: {peak[0]}"
        assert tight.snapshot()["completed"] == 6

        # a raising call is counted as congestion, not success
        before = tight.window
        try:
            with tight.slot():
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert tight.failures == 1 and tight.window <= before

        # -- coalescer: one execution for N identical asks ----------------
        co = Coalescer(ttl=5.0)
        ran = []
        hold = threading.Event()

        def slow_read() -> str:
            ran.append(1)
            hold.wait(5.0)
            return "contents"

        askers = [threading.Thread(target=lambda: co.run("k", slow_read))
                  for _ in range(6)]
        for t in askers:
            t.start()
        time.sleep(0.15)
        hold.set()
        for t in askers:
            t.join(10.0)
        assert len(ran) == 1, f"ran {len(ran)} times, should be once"
        assert co.hits_joined >= 1

        # a finished result is served again, until a write drops it
        value, kind = co.run("k", slow_read)
        assert value == "contents" and kind == "cached", kind
        assert len(ran) == 1
        co.invalidate()
        co.run("k", slow_read)
        assert len(ran) == 2, "a cached read survived an invalidation"

        # ttl=0 is singleflight ONLY — nothing is ever replayed
        sf = Coalescer(ttl=0.0)
        sf_ran = []
        sf.run("k", lambda: sf_ran.append(1))
        sf.run("k", lambda: sf_ran.append(1))
        assert len(sf_ran) == 2, "singleflight cached a result"

        # a failure is never cached: the next caller gets a real attempt
        flaky = Coalescer(ttl=60.0)
        attempts = []

        def sometimes() -> str:
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("hiccup")
            return "ok"

        try:
            flaky.run("f", sometimes)
            raise AssertionError("the error must propagate")
        except OSError:
            pass
        assert flaky.run("f", sometimes)[0] == "ok"

        # an expired entry is re-run rather than served stale
        quick = Coalescer(ttl=0.05)
        q_ran = []
        quick.run("k", lambda: q_ran.append(1))
        time.sleep(0.12)
        quick.run("k", lambda: q_ran.append(1))
        assert len(q_ran) == 2

        # -- lanes: the sovereign never queues behind subagents -----------
        lanes = Swarm(max_parallel=4, idle_ttl=0.2, name="lanes", reserve=1,
                      governor=LoadGovernor(ceiling=2, cores=8,
                                            load_source=lambda: 0.0))
        # background may use permits-reserve == 1, so one holder saturates
        # the background lane entirely
        assert lanes._cpu._limit_for(BACKGROUND) == 1
        assert lanes._cpu._limit_for(FOREGROUND) == 2
        occupied = threading.Event()
        let_go = threading.Event()

        def background_hog() -> None:
            with lanes.cpu(timeout=5.0) as ok:
                assert ok
                occupied.set()
                let_go.wait(5.0)

        lanes.submit(background_hog)
        assert occupied.wait(5.0)
        t0 = time.monotonic()
        with lanes.foreground(timeout=3.0) as ok:
            waited = time.monotonic() - t0
            assert ok, "the sovereign was refused its reserved permit"
            assert waited < 1.0, f"sovereign queued {waited:.2f}s"
        let_go.set()
        lanes.close()

        # reserve can never starve the background lane completely
        starved = AdaptiveSemaphore(
            LoadGovernor(ceiling=1, cores=8, load_source=lambda: 0.0),
            reserve=5)
        assert starved._limit_for(BACKGROUND) == 1

        # -- the status line renders every concern ------------------------
        text = sw.format_status()
        assert "SWARM" in text and "provider" in text and "machine" in text
        assert "█" in text or "░" in text

        print("SWARM SELF-TEST PASS")

    _self_test()

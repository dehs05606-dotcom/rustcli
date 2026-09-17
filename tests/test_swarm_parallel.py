"""End-to-end proof that subagents run in parallel WITHOUT loading the box.

These tests are deliberately adversarial about the two claims the swarm
makes, because both are easy to assert and hard to actually hold:

  1. REAL parallelism. Proved with barriers, not with timings alone: a
     barrier can only trip if N workers are genuinely inside the same
     region at the same instant. A serial executor cannot fake it.
  2. REAL restraint. Proved by watching the cpu permit's own high-water
     mark and the pool's thread count — the swarm must never exceed the
     ceiling the governor granted, and must give every thread back when
     the work stops.
"""

import threading
import time
import unittest

from fullagent.swarm import (AdaptiveSemaphore, Cancelled, ElasticPool,
                             LoadGovernor, Swarm)
from fullagent.swarm import _log as swarm_log


def idle_governor(ceiling=4, cores=8):
    """A governor over a machine that is definitely doing nothing else."""
    return LoadGovernor(ceiling=ceiling, cores=cores, load_source=lambda: 0.0)


class LoadGovernorTests(unittest.TestCase):
    def test_idle_box_opens_to_the_ceiling(self):
        self.assertEqual(idle_governor(ceiling=4).sample(force=True).permits, 4)

    def test_committed_box_closes_to_one(self):
        g = LoadGovernor(ceiling=4, cores=4, load_source=lambda: 8.0)
        self.assertEqual(g.sample(force=True).permits, 1)

    def test_permits_stay_in_bounds_for_any_load(self):
        for load in (-3.0, 0.0, 0.5, 2.0, 1000.0):
            g = LoadGovernor(ceiling=3, cores=4,
                             load_source=lambda v=load: v)
            self.assertIn(g.sample(force=True).permits, (1, 2, 3), load)

    def test_a_busy_box_takes_the_permits_back(self):
        level = [0.0]
        g = LoadGovernor(ceiling=4, cores=4, load_source=lambda: level[0])
        self.assertEqual(g.sample(force=True).permits, 4)
        level[0] = 4.0
        g._last_change = 0.0          # skip the anti-flap cooldown
        self.assertEqual(g.sample(force=True).permits, 1)

    def test_our_own_workers_are_not_counted_as_machine_load(self):
        # the governor must measure the load it has to make room FOR,
        # not the load it is deliberately creating — otherwise it reads
        # its own subagents as somebody else's build and throttles
        # itself into a corner it can never climb out of
        mine = [3.0]
        g = LoadGovernor(ceiling=4, cores=4, load_source=lambda: 3.0,
                         self_load=lambda: mine[0])
        s = g.sample(force=True)
        self.assertEqual(s.raw, 3.0)
        self.assertEqual(s.own, 3.0)
        self.assertEqual(s.load1, 0.0)      # all of it was ours
        self.assertEqual(s.permits, 4)
        # the SAME reading, now belonging to somebody else, walks the
        # permits back down — one step per sample, which is the
        # anti-flap hysteresis doing its job, not a jump
        mine[0] = 0.0
        for expected in (3, 2, 1, 1):
            g._last_change = 0.0            # skip the cooldown
            self.assertEqual(g.sample(force=True).permits, expected)

    def test_a_broken_probe_never_breaks_the_governor(self):
        def boom():
            raise RuntimeError("probe exploded")

        g = LoadGovernor(ceiling=2, cores=4, load_source=lambda: 0.0,
                         self_load=boom)
        self.assertEqual(g.sample(force=True).permits, 2)

    def test_breathe_is_free_when_the_box_is_idle(self):
        self.assertEqual(idle_governor().breathe(), 0.0)

    def test_breathe_yields_real_time_when_the_box_is_busy(self):
        g = LoadGovernor(ceiling=4, cores=4, load_source=lambda: 4.0)
        self.assertGreater(g.breathe(), 0.0)


class AdaptiveSemaphoreTests(unittest.TestCase):
    def test_the_bound_is_never_breached(self):
        sem = AdaptiveSemaphore(idle_governor(ceiling=2))
        peak, inside, lock = [0], [0], threading.Lock()
        go = threading.Event()

        def worker():
            go.wait(5.0)
            with sem.hold(timeout=5.0) as ok:
                self.assertTrue(ok)
                with lock:
                    inside[0] += 1
                    peak[0] = max(peak[0], inside[0])
                time.sleep(0.05)
                with lock:
                    inside[0] -= 1

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        go.set()
        for t in threads:
            t.join(15.0)
        self.assertLessEqual(peak[0], 2)
        self.assertEqual(sem.active, 0)

    def test_permits_can_be_queried_while_a_permit_is_held(self):
        # regression: the governor's self-load probe reads the very
        # semaphore that asks it for permits. If either side locked
        # naively, acquire() would deadlock against its own probe.
        sw = Swarm(max_parallel=2, idle_ttl=0.2, name="reentry",
                   governor=idle_governor(ceiling=2))
        self.addCleanup(sw.close)
        done = threading.Event()

        def job():
            with sw.cpu(timeout=5.0):
                sw.governor.permits()       # would deadlock if re-entrant
                sw.snapshot()               # ... and so would this
            done.set()

        sw.submit(job)
        self.assertTrue(done.wait(10.0), "deadlocked on the self-load probe")

    def test_a_timed_out_permit_still_lets_the_work_happen(self):
        # metering is a courtesy to the machine, never a correctness gate
        sem = AdaptiveSemaphore(LoadGovernor(ceiling=1, cores=1,
                                             load_source=lambda: 99.0))
        self.assertTrue(sem.acquire(timeout=0.1))
        with sem.hold(timeout=0.05) as ok:
            self.assertFalse(ok)      # not admitted ...
            ran = True                # ... but the body runs anyway
        self.assertTrue(ran)
        sem.release()


class ElasticPoolTests(unittest.TestCase):
    def test_jobs_really_overlap(self):
        pool = ElasticPool(name="t", max_threads=6, idle_ttl=0.2)
        barrier = threading.Barrier(6, timeout=10.0)
        met = []

        def job(i):
            barrier.wait()            # only trips if all six are in flight
            met.append(i)

        for i in range(6):
            pool.submit(job, i)
        deadline = time.monotonic() + 15.0
        while len(met) < 6 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(met), 6, "subagent jobs did not overlap")

    def test_two_quick_submits_get_two_threads(self):
        # the exact race that silently turns a pool back into a queue:
        # the second job must not be handed to a thread that is already
        # committed to the first one
        pool = ElasticPool(name="race", max_threads=4, idle_ttl=0.2)
        both = threading.Barrier(2, timeout=10.0)
        ok = []
        for _ in range(2):
            pool.submit(lambda: ok.append(both.wait()))
            time.sleep(0.01)          # the gap two real spawns leave
        deadline = time.monotonic() + 12.0
        while len(ok) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(ok), 2)

    def test_threads_are_given_back_when_the_work_stops(self):
        pool = ElasticPool(name="t", max_threads=4, idle_ttl=0.2)
        done = threading.Event()
        pool.submit(done.set)
        self.assertTrue(done.wait(5.0))
        deadline = time.monotonic() + 5.0
        while pool.threads and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(pool.threads, 0, "idle threads never retired")

    def test_a_raising_job_does_not_kill_its_thread(self):
        pool = ElasticPool(name="t", max_threads=1, idle_ttl=0.2)
        after = threading.Event()
        swarm_log.disabled = True     # the logged traceback IS the test
        self.addCleanup(setattr, swarm_log, "disabled", False)
        pool.submit(lambda: 1 / 0)
        pool.submit(after.set)
        self.assertTrue(after.wait(5.0))


class SwarmTests(unittest.TestCase):
    def setUp(self):
        self.sw = Swarm(max_parallel=6, idle_ttl=0.2, name="test",
                        governor=idle_governor(ceiling=2))
        self.addCleanup(self.sw.close)

    def test_network_waits_fan_out(self):
        # six 0.2s "model calls" must not cost 1.2s
        def call(n):
            with self.sw.net():
                time.sleep(0.2)
            return n

        t0 = time.monotonic()
        tickets = [self.sw.submit(call, i) for i in range(6)]
        self.sw.gather(tickets, timeout=20.0)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual([t.result for t in tickets], list(range(6)))

    def test_local_work_stays_inside_the_permit(self):
        inside, peak, lock = [0], [0], threading.Lock()

        def local(_n):
            with self.sw.cpu(timeout=10.0):
                with lock:
                    inside[0] += 1
                    peak[0] = max(peak[0], inside[0])
                time.sleep(0.05)
                with lock:
                    inside[0] -= 1

        self.sw.gather([self.sw.submit(local, i) for i in range(6)],
                       timeout=20.0)
        self.assertLessEqual(peak[0], 2, "cpu ceiling was breached")

    def test_a_failing_job_is_carried_on_its_ticket(self):
        t = self.sw.submit(lambda: 1 / 0)
        t.wait(5.0)
        self.assertIsInstance(t.error, ZeroDivisionError)
        with self.assertRaises(ZeroDivisionError):
            t.get(timeout=1.0)

    def test_gather_respects_a_timeout(self):
        block = threading.Event()
        self.addCleanup(block.set)
        t = self.sw.submit(lambda: block.wait(30.0))
        t0 = time.monotonic()
        self.sw.gather([t], timeout=0.3)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertFalse(t.done)

    def test_gather_returns_at_once_on_cancel(self):
        block = threading.Event()
        self.addCleanup(block.set)
        t = self.sw.submit(lambda: block.wait(30.0))
        t0 = time.monotonic()
        self.sw.gather([t], timeout=30.0, should_cancel=lambda: True)
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_drain_settles_the_tickets_it_drops(self):
        # a cancelled job must never leave a waiter parked forever
        small = Swarm(max_parallel=1, idle_ttl=0.2, name="small",
                      governor=idle_governor(ceiling=1))
        self.addCleanup(small.close)
        block = threading.Event()
        self.addCleanup(block.set)
        busy = small.submit(lambda: block.wait(30.0))
        queued = [small.submit(lambda: None) for _ in range(3)]
        time.sleep(0.1)
        self.assertEqual(small.drain(), 3)
        for t in queued:
            self.assertTrue(t.wait(5.0))
            self.assertIsInstance(t.error, Cancelled)
        block.set()
        busy.wait(5.0)
        self.assertEqual(small.in_flight, 0)

    def test_an_idle_swarm_costs_nothing(self):
        self.sw.gather([self.sw.submit(lambda: None) for _ in range(4)],
                       timeout=10.0)
        deadline = time.monotonic() + 5.0
        while self.sw.pool.threads and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.sw.pool.threads, 0)
        self.assertEqual(self.sw.in_flight, 0)


if __name__ == "__main__":
    unittest.main()

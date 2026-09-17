"""Systematic concurrency verification — of the checker, and with it.

Two halves, and the second is the one that matters to this repository.

The first half tests the checker against planted races whose answers are
known: it must FIND a guaranteed lost update, FIND a textbook lock-order
deadlock, leave correctly-locked code alone, and replay any finding
exactly. A concurrency checker that cannot find a bug it was handed is
worse than no checker, because it converts silence into false confidence.

The second half points it at the real parallel machinery — Coalescer,
AdaptiveSemaphore, Blackboard — and asserts their invariants across
hundreds of distinct schedules. Those three are the load-bearing pieces
of everything the swarm does, and until now they were backed by ordinary
tests: real evidence that one interleaving worked, which is much weaker
than it looks.
"""

import unittest

import fullagent.blackboard as bb
import fullagent.swarm as swarm
from fullagent.interleave import (Scheduler, _Condition, _Lock, explore,
                                  replay)


class CheckerTests(unittest.TestCase):
    """The checker, against races with known answers."""

    @staticmethod
    def lost_update(sched):
        box = {"n": 0}

        def bump():
            seen = box["n"]
            sched.switch("read")          # the unguarded window
            box["n"] = seen + 1

        sched.spawn(bump, "a")
        sched.spawn(bump, "b")

        def check():
            assert box["n"] == 2, f"lost update: n={box['n']}"

        sched.on_finish(check)

    @staticmethod
    def guarded(sched):
        lock = _Lock(sched)
        box = {"n": 0}

        def bump():
            with lock:
                seen = box["n"]
                sched.switch("read")
                box["n"] = seen + 1

        sched.spawn(bump, "a")
        sched.spawn(bump, "b")
        sched.on_finish(
            lambda: None if box["n"] == 2
            else (_ for _ in ()).throw(AssertionError(f"n={box['n']}")))

    def test_it_finds_a_guaranteed_lost_update(self):
        rep = explore(self.lost_update, iterations=80, seed=1)
        self.assertFalse(rep.clean, "a guaranteed race was not found")
        self.assertEqual(rep.findings[0].kind, "assertion")
        self.assertIn("lost update", rep.findings[0].detail)

    def test_a_finding_replays_exactly(self):
        # this is what makes a finding a bug report and not an anecdote
        rep = explore(self.lost_update, iterations=80, seed=1)
        found = rep.findings[0]
        again = replay(self.lost_update, found.schedule)
        self.assertEqual(again.schedule, found.schedule)
        err = again.first_error()
        self.assertIsInstance(err, AssertionError)
        self.assertIn("lost update", str(err))

    def test_correctly_locked_code_survives_the_search(self):
        rep = explore(self.guarded, iterations=150, seed=1,
                      stop_on_first=False)
        self.assertTrue(rep.clean, rep.format())
        self.assertGreater(rep.distinct, 1, "the search never varied")

    def test_it_finds_a_lock_order_deadlock(self):
        def lock_order(sched):
            a, b = _Lock(sched), _Lock(sched)

            def left():
                with a:
                    sched.switch("mid")
                    with b:
                        pass

            def right():
                with b:
                    sched.switch("mid")
                    with a:
                        pass

            sched.spawn(left, "left")
            sched.spawn(right, "right")

        rep = explore(lock_order, iterations=100, seed=5)
        self.assertFalse(rep.clean)
        self.assertEqual(rep.findings[0].kind, "deadlock")

    def test_a_lost_wakeup_would_be_caught(self):
        # the checker found exactly this bug in its own Condition, so the
        # shape is worth pinning: publish AFTER releasing and a notify
        # that lands in the window is delivered to nobody
        def racy_wait(sched):
            cond = _Condition(sched)
            box = {"ready": False}

            def consumer():
                with cond:
                    while not box["ready"]:
                        cond.wait()

            def producer():
                with cond:
                    box["ready"] = True
                    cond.notify_all()

            sched.spawn(consumer, "consumer")
            sched.spawn(producer, "producer")

        rep = explore(racy_wait, iterations=150, seed=2,
                      stop_on_first=False)
        self.assertTrue(rep.clean, rep.format())

    def test_a_scenario_that_hangs_is_reported_not_waited_on(self):
        def hangs(sched):
            cond = _Condition(sched)

            def waiter():
                with cond:
                    cond.wait()          # nobody ever notifies

            sched.spawn(waiter, "waiter")

        rep = explore(hangs, iterations=3, seed=1)
        self.assertFalse(rep.clean)
        self.assertEqual(rep.findings[0].kind, "deadlock")

    def test_timed_waits_do_not_invent_a_deadlock(self):
        # AdaptiveSemaphore polls on purpose: permits can grow with no
        # release to notify on. Modelling a timed wait as "never expires"
        # would deadlock correct code.
        def polls(sched):
            cond = _Condition(sched)
            box = {"go": False}

            def poller():
                with cond:
                    while not box["go"]:
                        cond.wait(0.25)

            def setter():
                sched.switch("later")
                with cond:
                    box["go"] = True     # deliberately does NOT notify

            sched.spawn(poller, "poller")
            sched.spawn(setter, "setter")

        rep = explore(polls, iterations=60, seed=1, stop_on_first=False)
        self.assertTrue(rep.clean, rep.format())


class ProductionInvariantTests(unittest.TestCase):
    """The real parallel machinery, across hundreds of schedules."""

    def test_coalescer_runs_identical_calls_exactly_once(self):
        n = 4

        def singleflight(sched):
            co = swarm.Coalescer(ttl=10.0)
            runs, answers = [], []

            def work():
                runs.append(1)
                sched.switch("inside the real call")
                return "the answer"

            def ask():
                answers.append(co.run("read_file:/x", work))

            for i in range(n):
                sched.spawn(ask, f"asker{i}")

            def invariant():
                assert len(runs) == 1, f"executed {len(runs)} times"
                assert len(answers) == n, f"{len(answers)} answers"
                assert all(v == "the answer" for v, _ in answers), answers
                assert sum(1 for _, k in answers if k == "ran") == 1, answers

            sched.on_finish(invariant)

        rep = explore(singleflight, iterations=250, seed=1,
                      modules=[swarm])
        self.assertTrue(rep.clean, rep.format())
        self.assertGreater(rep.distinct, 50, "too few distinct schedules")

    def test_the_cpu_permit_bound_is_actually_a_bound(self):
        def permits(sched):
            gov = swarm.LoadGovernor(ceiling=2, cores=8,
                                     load_source=lambda: 0.0)
            sem = swarm.AdaptiveSemaphore(gov, reserve=0)
            inside, peak = [0], [0]

            def worker():
                with sem.hold(timeout=5.0) as ok:
                    assert ok, "a permit was refused with capacity free"
                    inside[0] += 1
                    peak[0] = max(peak[0], inside[0])
                    sched.switch("holding a permit")
                    inside[0] -= 1

            for i in range(4):
                sched.spawn(worker, f"w{i}")

            def invariant():
                assert peak[0] <= 2, f"{peak[0]} holders, ceiling is 2"

            sched.on_finish(invariant)

        rep = explore(permits, iterations=250, seed=1, modules=[swarm])
        self.assertTrue(rep.clean, rep.format())
        self.assertGreater(rep.distinct, 50)

    def test_the_board_never_duplicates_or_loses_a_fact(self):
        def board(sched):
            import tempfile
            from pathlib import Path
            from fullagent.kernel import EventLog
            b = bb.Blackboard(
                EventLog(Path(tempfile.mkdtemp()) / "b.jsonl"),
                max_facts=50)

            def poster(n):
                def go():
                    b.post("the config lives in config.py",
                           agent_id=f"c{n}")
                    sched.switch("between posts")
                    b.post(f"unique finding {n}", agent_id=f"c{n}")
                return go

            for i in range(3):
                sched.spawn(poster(i), f"poster{i}")

            def invariant():
                facts = b.all()
                texts = [f.text for f in facts]
                assert len(texts) == len(set(texts)), f"duplicates: {texts}"
                assert sum(1 for t in texts if "config lives" in t) == 1
                assert len(facts) == 4, f"{len(facts)} facts: {texts}"
                assert [f.seq for f in facts] == list(range(len(facts)))

            sched.on_finish(invariant)

        rep = explore(board, iterations=200, seed=1, modules=[bb])
        self.assertTrue(rep.clean, rep.format())
        self.assertGreater(rep.distinct, 30)


if __name__ == "__main__":
    unittest.main()

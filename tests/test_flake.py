"""The flake hunter, held to the standard it exists to enforce.

The load-bearing property is not "it finds flaky tests" — anything that
reruns a suite finds those. It is that the hunter DISTINGUISHES the two
kinds, because the fix is completely different: a non-deterministic test
needs its clock or seed pinned, while an order-dependent one needs
another test to stop leaking. Calling one the other sends a developer
looking in the wrong file.
"""

import random
import tempfile
import unittest
from pathlib import Path

from fullagent.flake import (BAD, FAIL, PASS, FlakeHunter, UnittestRunner,
                             ddmin)
from fullagent.kernel import EventLog

SUITE = ["t.Clean.test_a", "t.Poll.test_polluter", "t.Clean.test_b",
         "t.Rand.test_random", "t.Clean.test_c", "t.Vic.test_victim"]


def ordered_flake(rng):
    """victim fails iff polluter ran first; random fails ~a third."""
    def runner(order):
        out, polluted = {}, False
        for t in order:
            if t == "t.Poll.test_polluter":
                polluted = True
                out[t] = PASS
            elif t == "t.Vic.test_victim":
                out[t] = FAIL if polluted else PASS
            elif t == "t.Rand.test_random":
                out[t] = FAIL if rng.random() < 0.34 else PASS
            else:
                out[t] = PASS
        return out
    return runner


class DdminTests(unittest.TestCase):
    def test_finds_a_two_element_cause(self):
        # a cause that spans two chunks is the case complements exist for
        got = ddmin(list("abcdefgh"),
                    lambda s: "c" in s and "g" in s)
        self.assertEqual(sorted(got), ["c", "g"])

    def test_finds_a_single_element_cause(self):
        self.assertEqual(ddmin(list("abcdef"), lambda s: "d" in s), ["d"])

    def test_reduces_nothing_when_there_is_no_cause(self):
        self.assertEqual(ddmin(list("abc"), lambda s: False), list("abc"))

    def test_respects_its_probe_budget(self):
        spent = []
        ddmin(list("abcdefghij"),
              lambda s: (spent.append(1), True)[1], probe_budget=4)
        self.assertLessEqual(len(spent), 4)

    def test_is_logarithmic_not_linear(self):
        probes = []
        big = [f"t{i}" for i in range(64)]
        ddmin(big, lambda s: (probes.append(1), "t40" in s)[1])
        # a linear scan would be ~64; ddmin should be far under that
        self.assertLess(len(probes), 40, len(probes))


class HuntTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.log = EventLog(Path(self.td.name) / "f.jsonl")

    def hunt(self, seed=1234, **kw):
        h = FlakeHunter(self.log, ordered_flake(random.Random(seed)),
                        parallel=4, seed=7)
        return h.hunt(SUITE, **kw)

    def test_it_names_the_polluter(self):
        report = self.hunt(sweeps=10, solo=6)
        vic = next(s for s in report.suspects
                   if s.test == "t.Vic.test_victim")
        self.assertEqual(vic.kind, "order-dependent")
        self.assertEqual(vic.polluters, ["t.Poll.test_polluter"])

    def test_it_does_not_blame_ordering_for_a_random_failure(self):
        report = self.hunt(sweeps=10, solo=6)
        rnd = next(s for s in report.suspects
                   if s.test == "t.Rand.test_random")
        self.assertEqual(rnd.kind, "nondeterministic")
        self.assertEqual(rnd.polluters, [])
        self.assertGreater(rnd.solo_failures, 0)

    def test_clean_tests_are_left_alone(self):
        report = self.hunt(sweeps=10, solo=6)
        named = {s.test for s in report.suspects}
        for clean in ("t.Clean.test_a", "t.Clean.test_b", "t.Clean.test_c"):
            self.assertNotIn(clean, named)

    def test_a_shuffled_sweep_is_what_makes_it_visible(self):
        # the whole reason sweep() shuffles: in ONE fixed order the
        # polluter always precedes the victim, so the victim fails every
        # time — unanimous, and therefore invisible to a naive sweep
        runner = ordered_flake(random.Random(1))
        fixed = [runner(SUITE).get("t.Vic.test_victim") for _ in range(8)]
        self.assertEqual(len(set(fixed)), 1, "fixed order should be stable")
        self.assertIn(fixed[0], BAD)

        report = self.hunt(sweeps=10, solo=6)
        self.assertIn("t.Vic.test_victim",
                      {s.test for s in report.suspects})

    def test_a_clean_suite_reports_clean(self):
        h = FlakeHunter(self.log, lambda order: {t: PASS for t in order})
        rep = h.hunt(["a.B.test_x", "a.B.test_y"], sweeps=5, solo=3)
        self.assertEqual(rep.suspects, [])
        self.assertIn("No flakes found", rep.format())

    def test_an_always_failing_test_is_called_broken(self):
        h = FlakeHunter(
            self.log,
            lambda order: {t: (FAIL if "bad" in t else PASS) for t in order})
        s = h.classify("a.B.test_bad", solo=4)
        self.assertIn("broken test", s.note)

    def test_unreproducible_pollution_is_admitted_not_invented(self):
        h = FlakeHunter(self.log, lambda order: {t: PASS for t in order})
        polluters, probes = h.find_polluters("a.B.test_v", ["a.B.test_1"])
        self.assertEqual(polluters, [])
        self.assertGreaterEqual(probes, 1)

    def test_the_hunt_is_replayable(self):
        # a tool about non-determinism that cannot be replayed is not one
        a = FlakeHunter(self.log, ordered_flake(random.Random(5)), seed=99)
        b = FlakeHunter(self.log, ordered_flake(random.Random(5)), seed=99)
        ra, rb = a.hunt(SUITE, sweeps=8, solo=4), b.hunt(SUITE, sweeps=8,
                                                         solo=4)
        self.assertEqual(ra.seed, rb.seed)
        self.assertEqual({s.test for s in ra.suspects},
                         {s.test for s in rb.suspects})

    def test_everything_is_sealed_in_the_log(self):
        self.hunt(sweeps=6, solo=3)
        types = [e.type for e in self.log.events()]
        self.assertIn("flake.sweep", types)
        self.assertIn("flake.report", types)

    def test_an_empty_suite_is_a_no_op(self):
        h = FlakeHunter(self.log, lambda order: {})
        self.assertEqual(h.hunt([]).suspects, [])


class RealRunnerTests(unittest.TestCase):
    """End to end: a real project, real subprocesses, a real flake."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        root = Path(self.td.name)
        (root / "tests").mkdir()
        (root / "settings.py").write_text('MODE = "default"\n')
        (root / "tests" / "test_demo.py").write_text(
            "import unittest\n"
            "import settings\n"
            "\n"
            "class TestConfig(unittest.TestCase):\n"
            "    def test_override(self):\n"
            "        settings.MODE = 'override'\n"
            "        self.assertEqual(settings.MODE, 'override')\n"
            "\n"
            "class TestFiller(unittest.TestCase):\n"
            "    def test_noop(self):\n"
            "        self.assertTrue(True)\n"
            "\n"
            "class TestVictim(unittest.TestCase):\n"
            "    def test_expects_default(self):\n"
            "        self.assertEqual(settings.MODE, 'default')\n")
        self.root = root
        self.log = EventLog(root / "f.jsonl")

    def test_it_finds_a_real_order_dependent_flake(self):
        runner = UnittestRunner(self.root, start_dir="tests")
        tests = runner.discover()
        self.assertEqual(len(tests), 3, tests)

        hunter = FlakeHunter(self.log, runner, parallel=4, seed=7)
        report = hunter.hunt(tests, sweeps=8, solo=4)

        victim = next((s for s in report.suspects
                       if s.test.endswith("test_expects_default")), None)
        self.assertIsNotNone(victim, report.format())
        self.assertEqual(victim.kind, "order-dependent")
        self.assertEqual(len(victim.polluters), 1, victim.polluters)
        self.assertTrue(victim.polluters[0].endswith("test_override"),
                        victim.polluters)
        # and the reproduction it prints carries the path setup, without
        # which the ids alone fail with an ImportError that reads exactly
        # like a broken test
        self.assertIn("PYTHONPATH=", victim.repro)
        self.assertIn(victim.polluters[0], victim.repro)

    def test_the_runner_survives_a_directory_with_no_tests(self):
        import fullagent.flake as flake_mod
        flake_mod._log.disabled = True
        self.addCleanup(setattr, flake_mod._log, "disabled", False)
        runner = UnittestRunner(self.root, start_dir="nope")
        self.assertEqual(runner.discover(), [])
        self.assertEqual(runner([]), {})


if __name__ == "__main__":
    unittest.main()

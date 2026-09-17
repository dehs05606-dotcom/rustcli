"""FLAKE — find the test that fails sometimes, and say WHY.

"It passes on my machine" is the most expensive sentence in software,
and the usual tooling answers it with a shrug: run the suite again, mark
the test flaky, move on. That is not a diagnosis, it is a surrender.
Two facts make a real one possible.

FIRST: most flakes are not random. They are ORDER-DEPENDENT. Some other
test leaves module state, a monkeypatch, a temp file, a stale singleton,
a mutated sys.path — and the victim fails only when that test ran first.
Rerunning the victim alone will pass forever and teach you nothing. The
question is never "is this flaky", it is "WHO polluted it".

SECOND: that question is answerable exactly, not by reading code but by
bisecting reality. If the victim fails after some set of tests, then some
MINIMAL subset of that set is enough to break it, and delta debugging
(Zeller's ddmin) finds it in O(log n) runs instead of O(n). The output is
not a guess. It is: "run test_config_override, then test_paths — the
second one fails, every time." A name you can open and a case you can
reproduce.

So the hunt has three stages, and each one narrows the question:

    SWEEP       run the whole suite many times, in parallel, and keep
                every test whose verdict was not unanimous. Parallelism
                is the point: twenty sweeps of a two-minute suite is
                forty minutes serially and about two on the swarm.
    CLASSIFY    run each suspect ALONE, repeatedly. Fails alone too ->
                genuinely non-deterministic (a clock, a seed, a race).
                Passes alone, always -> order-dependent, and the
                interesting case.
    BISECT      ddmin over everything that ran before the victim, until
                only the tests that actually matter are left.

Nothing here is specific to this project. The runner is injected, so the
hunt is exactly as testable as the thing it is hunting — which for a
tool about non-determinism is not a nicety.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from ._foundation import get_logger
from .kernel import EventLog
from .swarm import Swarm

_log = get_logger("flake")

__all__ = ["Verdict", "Suspect", "HuntReport", "FlakeHunter", "ddmin",
           "UnittestRunner"]

PASS, FAIL, ERROR, SKIP, MISSING = "pass", "fail", "error", "skip", "missing"
BAD = (FAIL, ERROR)

DEFAULT_SWEEPS = 8          # full-suite runs when looking for suspects
DEFAULT_SOLO = 6            # solo runs when classifying one suspect
MAX_BISECT_PROBES = 120     # a hard ceiling on ddmin's appetite


# ---------------------------------------------------------------------------
# ddmin — the part that turns "flaky" into a name
# ---------------------------------------------------------------------------

def ddmin(items: Sequence[str],
          fails: Callable[[list[str]], bool],
          *, probe_budget: int = MAX_BISECT_PROBES) -> list[str]:
    """Smallest subset of `items` that still satisfies `fails`.

    Zeller's delta debugging, unchanged in spirit: halve, test each half,
    then test each complement, and only widen the granularity when
    neither helped. It is worth being precise about why the complements
    matter — without them the algorithm can only find a cause that lives
    entirely inside one half, and real pollution is often a PAIR of tests
    that must both run (one opens a connection, another exhausts the
    pool). Complements are what let it keep both.

    `fails` is assumed monotone-ish but is never trusted to be: every
    reduction is one it verified itself, so a noisy predicate costs
    precision, never correctness. The budget exists because each probe is
    a real test run, and an unbounded search on a large suite is a way to
    spend an afternoon.
    """
    items = list(items)
    probes = {"n": 0}

    def check(subset: list[str]) -> bool:
        if not subset or probes["n"] >= probe_budget:
            return False
        probes["n"] += 1
        return fails(subset)

    n = 2
    while len(items) >= 2 and probes["n"] < probe_budget:
        size = max(1, len(items) // n)
        chunks = [items[i:i + size] for i in range(0, len(items), size)]

        for chunk in chunks:                       # cause inside one part
            if check(chunk):
                items, n = chunk, 2
                break
        else:
            for chunk in chunks:                   # cause spans the parts
                rest = [x for x in items if x not in chunk]
                if rest and check(rest):
                    items, n = rest, max(n - 1, 2)
                    break
            else:
                if n >= len(items):
                    break
                n = min(2 * n, len(items))
    _log.debug("ddmin reduced to %d item(s) in %d probe(s)",
               len(items), probes["n"])
    return items


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    """What one test did across every run that included it."""
    test: str
    outcomes: list[str] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return len(set(self.outcomes)) <= 1

    @property
    def passes(self) -> int:
        return sum(1 for o in self.outcomes if o == PASS)

    @property
    def failures(self) -> int:
        return sum(1 for o in self.outcomes if o in BAD)

    @property
    def rate(self) -> float:
        total = self.passes + self.failures
        return self.failures / total if total else 0.0


@dataclass
class Suspect:
    """One flaky test, and everything the hunt established about it."""
    test: str
    kind: str = "unknown"        # nondeterministic | order-dependent | clean
    fail_rate: float = 0.0
    solo_failures: int = 0
    solo_runs: int = 0
    polluters: list[str] = field(default_factory=list)
    probes: int = 0
    note: str = ""
    repro: str = ""              # a command that actually reproduces it

    def to_dict(self) -> dict:
        return {"test": self.test, "kind": self.kind,
                "fail_rate": round(self.fail_rate, 3),
                "solo_failures": self.solo_failures,
                "solo_runs": self.solo_runs,
                "polluters": self.polluters, "probes": self.probes,
                "note": self.note, "repro": self.repro}

    def render(self) -> str:
        head = f"✗ {self.test}  [{self.kind}]  fails {self.fail_rate:.0%}"
        lines = [head]
        if self.kind == "order-dependent" and self.polluters:
            lines.append("  reproduce with:")
            lines.append("    " + (self.repro or
                                   " ".join(self.polluters + [self.test])))
            who = ", ".join(self.polluters)
            lines.append(f"  {who} leaves state that breaks it")
        elif self.kind == "order-dependent":
            lines.append("  passes alone, fails in the suite — but the "
                         "polluter was not isolated within the probe "
                         "budget")
        elif self.kind == "nondeterministic":
            lines.append(f"  fails {self.solo_failures}/{self.solo_runs} "
                         f"times ALONE — a clock, a seed, a race or "
                         f"external state, not test order")
        if self.note:
            lines.append(f"  note: {self.note}")
        return "\n".join(lines)


@dataclass
class HuntReport:
    sweeps: int = 0
    tests: int = 0
    suspects: list[Suspect] = field(default_factory=list)
    elapsed_s: float = 0.0
    runs: int = 0

    seed: int = 0

    def to_dict(self) -> dict:
        return {"sweeps": self.sweeps, "tests": self.tests,
                "runs": self.runs, "seed": self.seed,
                "elapsed_s": round(self.elapsed_s, 2),
                "suspects": [s.to_dict() for s in self.suspects]}

    def format(self) -> str:
        if not self.suspects:
            return (f"FLAKE — {self.tests} test(s), {self.sweeps} sweep(s), "
                    f"{self.runs} run(s) in {self.elapsed_s:.1f}s: "
                    f"every verdict unanimous. No flakes found.")
        lines = [f"FLAKE — {len(self.suspects)} flaky test(s) out of "
                 f"{self.tests}, after {self.sweeps} sweep(s) and "
                 f"{self.runs} run(s) in {self.elapsed_s:.1f}s"]
        order = {"order-dependent": 0, "nondeterministic": 1}
        for s in sorted(self.suspects,
                        key=lambda x: (order.get(x.kind, 9), -x.fail_rate)):
            lines.append(s.render())
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# The hunt
# ---------------------------------------------------------------------------

class FlakeHunter:
    """Sweep, classify, bisect.

    `runner(order) -> {test: outcome}` runs exactly the given tests, in
    the given order, in a FRESH process. The fresh process is not an
    implementation detail: in-process reruns share the very state whose
    leakage is being hunted, so a hunter that reuses one would keep
    finding the pollution it caused itself.
    """

    def __init__(self, log: EventLog, runner: Callable[[list[str]], dict],
                 *, swarm: Swarm | None = None,
                 parallel: int = 4, seed: int | None = None) -> None:
        self.log = log
        self.runner = runner
        self.swarm = swarm or Swarm(max_parallel=max(1, int(parallel)),
                                    name="flake")
        # A tool about non-determinism that is itself non-reproducible is
        # not a tool. The seed is chosen once, logged, and every shuffled
        # order derives from it, so a hunt can be replayed exactly.
        self.seed = int(seed if seed is not None else time.time_ns() % 2**31)
        self._runs = 0
        self._lock = threading.Lock()
        self._orders: list[tuple[list[str], dict]] = []

    def _run(self, order: list[str]) -> dict:
        with self._lock:
            self._runs += 1
        return self.runner(list(order)) or {}

    # -- stage 1: who is not unanimous? ------------------------------------

    def sweep(self, tests: list[str], sweeps: int = DEFAULT_SWEEPS
              ) -> dict[str, Verdict]:
        """Run the whole suite `sweeps` times, in parallel, in DIFFERENT
        ORDERS.

        Shuffling is not a refinement, it is the only way this stage
        works at all. An order-dependent flake is perfectly deterministic
        in a fixed order — the polluter runs before the victim every
        single time, so the victim fails every single time, and a sweep
        that always runs the declared order records a unanimous verdict
        and calls it healthy. The flake becomes VISIBLE precisely when
        the order changes, which is also why it ambushes people on CI
        after an unrelated test is added.

        The first sweep keeps the declared order, so the baseline a
        developer actually runs is always among the evidence.
        """
        verdicts = {t: Verdict(test=t) for t in tests}
        rng = random.Random(self.seed)
        orders = [list(tests)]
        for _ in range(max(1, sweeps) - 1):
            shuffled = list(tests)
            rng.shuffle(shuffled)
            orders.append(shuffled)

        tickets = [self.swarm.submit(self._run, order, label=f"sweep{i}")
                   for i, order in enumerate(orders)]
        self.swarm.gather(tickets)
        for order, ticket in zip(orders, tickets):
            if ticket.error is not None or not isinstance(ticket.result, dict):
                continue
            self._orders.append((order, ticket.result))
            for t in tests:
                verdicts[t].outcomes.append(ticket.result.get(t, MISSING))
        return verdicts

    def failing_prefix(self, victim: str) -> list[str]:
        """Tests that ran BEFORE the victim in some order where it failed.

        Bisecting the declared order would be a guess; bisecting an order
        that actually broke it is a reproduction. If several orders broke
        it, the shortest prefix is the cheapest one to reduce.
        """
        prefixes = []
        for order, result in self._orders:
            if result.get(victim, MISSING) in BAD and victim in order:
                prefixes.append(order[:order.index(victim)])
        return min(prefixes, key=len) if prefixes else []

    # -- stage 2: is it the test, or the company it keeps? -----------------

    def classify(self, victim: str, solo: int = DEFAULT_SOLO) -> Suspect:
        """Run the victim ALONE, repeatedly."""
        tickets = [self.swarm.submit(self._run, [victim], label=f"solo{i}")
                   for i in range(max(1, solo))]
        self.swarm.gather(tickets)
        outcomes = []
        for ticket in tickets:
            res = ticket.result if isinstance(ticket.result, dict) else {}
            outcomes.append(res.get(victim, MISSING))
        bad = sum(1 for o in outcomes if o in BAD)
        suspect = Suspect(test=victim, solo_failures=bad,
                          solo_runs=len(outcomes))
        if bad and bad < len(outcomes):
            suspect.kind = "nondeterministic"
        elif bad == len(outcomes) and outcomes:
            suspect.kind = "nondeterministic"
            suspect.note = ("fails every time alone — this is a broken "
                            "test, not a flaky one")
        else:
            suspect.kind = "order-dependent"
        return suspect

    # -- stage 3: name the polluter ----------------------------------------

    def find_polluters(self, victim: str, prefix: list[str],
                       *, budget: int = MAX_BISECT_PROBES) -> tuple[list[str], int]:
        """ddmin over everything that ran before the victim."""
        probes = {"n": 0}

        def breaks(subset: list[str]) -> bool:
            probes["n"] += 1
            result = self._run(list(subset) + [victim])
            return result.get(victim, MISSING) in BAD

        if not prefix or not breaks(prefix):
            # not reproducible with the whole prefix: either the order
            # that broke it was different, or it needs concurrency we are
            # not recreating here. Say so rather than invent a culprit.
            return [], probes["n"]
        minimal = ddmin(prefix, breaks, probe_budget=budget)
        return minimal, probes["n"]

    # -- the whole thing ---------------------------------------------------

    def hunt(self, tests: list[str], *, sweeps: int = DEFAULT_SWEEPS,
             solo: int = DEFAULT_SOLO,
             budget: int = MAX_BISECT_PROBES) -> HuntReport:
        tests = [t for t in tests if t]
        t0 = time.monotonic()
        report = HuntReport(sweeps=sweeps, tests=len(tests),
                            seed=self.seed)
        if not tests:
            return report

        verdicts = self.sweep(tests, sweeps)
        suspects = [v.test for v in verdicts.values() if not v.consistent]
        self.log.append("flake.sweep",
                        {"tests": len(tests), "sweeps": sweeps,
                         "seed": self.seed,
                         "suspects": suspects[:20]}, actor="flake")

        for name in suspects:
            suspect = self.classify(name, solo)
            suspect.fail_rate = verdicts[name].rate
            if suspect.kind == "order-dependent":
                polluters, probes = self.find_polluters(
                    name, self.failing_prefix(name), budget=budget)
                suspect.polluters = polluters
                suspect.probes = probes
                # A reproduction the reader cannot paste is not a
                # reproduction. The runner knows the cwd, the
                # interpreter and the path setup its own runs need; the
                # hunter does not, so it asks rather than guesses.
                repro = getattr(self.runner, "repro_command", None)
                if polluters and callable(repro):
                    try:
                        suspect.repro = repro(polluters + [name])
                    except Exception:  # noqa: BLE001 — never fail a hunt
                        pass
            report.suspects.append(suspect)
            self.log.append("flake.suspect", suspect.to_dict(), actor="flake")

        report.elapsed_s = time.monotonic() - t0
        report.runs = self._runs
        self.log.append("flake.report", report.to_dict(), actor="flake")
        return report


# ---------------------------------------------------------------------------
# A real runner — unittest, in a fresh process, reporting JSON
# ---------------------------------------------------------------------------

# Both drivers put the project root AND the test directory on sys.path
# before doing anything. `python -c` does not add the test directory the
# way `unittest discover` does internally, and test ids come back
# unprefixed ("test_demo.Case.test_x") whenever the directory is not a
# package — so a run driver that did not do this could discover a test
# and then be unable to load the very id it was handed.
_BOOT = r'''
import os, sys
_start = os.path.abspath(sys.argv[1])
for _p in (os.getcwd(), _start):
    if _p not in sys.path:
        sys.path.insert(0, _p)
'''

_DISCOVER = _BOOT + r'''
import json, unittest
loader = unittest.TestLoader()
suite = loader.discover(sys.argv[1], pattern=sys.argv[2])
ids = []
def walk(s):
    for t in s:
        if isinstance(t, unittest.TestSuite):
            walk(t)
        else:
            ids.append(t.id())
walk(suite)
print(json.dumps(ids))
'''

_RUN = _BOOT + r'''
import json, unittest
names = sys.argv[2:]
loader = unittest.TestLoader()
out = {}
suite = unittest.TestSuite()
for n in names:
    try:
        suite.addTests(loader.loadTestsFromName(n))
    except Exception as e:
        out[n] = "error"
class R(unittest.TestResult):
    def addSuccess(self, t):  out[t.id()] = "pass"
    def addFailure(self, t, e): out[t.id()] = "fail"
    def addError(self, t, e):   out[t.id()] = "error"
    def addSkip(self, t, r):    out[t.id()] = "skip"
    def addExpectedFailure(self, t, e): out[t.id()] = "pass"
    def addUnexpectedSuccess(self, t):  out[t.id()] = "fail"
suite.run(R())
print("__FLAKE__" + json.dumps(out))
'''


class UnittestRunner:
    """Runs a chosen set of unittest tests, in order, in a fresh process.

    A fresh process per run is the whole design. Module state, imports,
    monkeypatches and singletons are exactly what leaks between tests, so
    reusing an interpreter would hide the thing being hunted — and worse,
    would add pollution of the hunter's own.
    """

    def __init__(self, root: Path | str, *, pattern: str = "test*.py",
                 start_dir: str = "tests", timeout: float = 300.0,
                 env: dict | None = None) -> None:
        self.root = Path(root).resolve()
        self.pattern = pattern
        self.start_dir = start_dir
        self.timeout = float(timeout)
        self.env = env

    def _python(self) -> str:
        return sys.executable or "python3"

    def _env(self) -> dict:
        env = dict(os.environ)
        if self.env:
            env.update(self.env)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (f"{self.root}{os.pathsep}{existing}"
                             if existing else str(self.root))
        return env

    def repro_command(self, order: list[str]) -> str:
        """A command a human can paste, including the path setup.

        Worth stating why this exists: the node ids alone are NOT enough.
        A test directory that is not a package only resolves because the
        runner put it on sys.path, so `python -m unittest <id>` in a
        plain shell fails with an ImportError that looks exactly like a
        broken test — the one misreading that would discredit every
        finding this tool makes.
        """
        start = (self.root / self.start_dir).resolve()
        return (f"cd {self.root} && PYTHONPATH={start} "
                f"{Path(self._python()).name} -m unittest "
                + " ".join(order))

    def discover(self) -> list[str]:
        proc = subprocess.run(
            [self._python(), "-c", _DISCOVER, self.start_dir,
             self.pattern],
            cwd=str(self.root), capture_output=True, text=True,
            timeout=self.timeout, env=self._env())
        try:
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            _log.warning("discovery failed: %s",
                         (proc.stderr or proc.stdout)[-400:])
            return []

    def __call__(self, order: list[str]) -> dict:
        if not order:
            return {}
        try:
            proc = subprocess.run(
                [self._python(), "-c", _RUN, self.start_dir, *order],
                cwd=str(self.root), capture_output=True, text=True,
                timeout=self.timeout, env=self._env())
        except subprocess.TimeoutExpired:
            return {t: ERROR for t in order}
        for line in reversed(proc.stdout.splitlines()):
            if line.startswith("__FLAKE__"):
                try:
                    return json.loads(line[len("__FLAKE__"):])
                except ValueError:
                    break
        # the process died before reporting — a crash IS a result, and
        # attributing it to the whole batch is honest: we do not know
        # which test took the interpreter down, and ddmin will find out
        return {t: ERROR for t in order}


# ---------------------------------------------------------------------------
# Self-test — a simulated suite with a real order-dependent flake
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import random
    import tempfile

    def _self_test() -> None:
        with tempfile.TemporaryDirectory() as td:
            log = EventLog(Path(td) / "flake.jsonl")

            # -- ddmin in isolation, on a known answer ------------------
            # the "cause" is {c, g}: any subset containing BOTH fails
            universe = list("abcdefgh")
            calls = {"n": 0}

            def both_present(subset: list[str]) -> bool:
                calls["n"] += 1
                return "c" in subset and "g" in subset

            got = ddmin(universe, both_present)
            assert sorted(got) == ["c", "g"], got
            assert calls["n"] < 60, calls["n"]

            # a single-element cause
            assert ddmin(list("abcdef"), lambda s: "d" in s) == ["d"]
            # no cause at all -> nothing is reduced away wrongly
            assert ddmin(list("abc"), lambda s: False) == list("abc")
            # the budget is respected
            spent = {"n": 0}

            def counting(s):
                spent["n"] += 1
                return True

            ddmin(list("abcdefgh"), counting, probe_budget=3)
            assert spent["n"] <= 3, spent["n"]

            # -- a simulated suite -------------------------------------
            # test_victim fails ONLY if test_polluter ran before it.
            # test_random fails a third of the time on its own.
            # everything else is clean.
            SUITE = ["t.Clean.test_a", "t.Poll.test_polluter",
                     "t.Clean.test_b", "t.Rand.test_random",
                     "t.Clean.test_c", "t.Vic.test_victim"]
            rng = random.Random(1234)

            def fake_runner(order: list[str]) -> dict:
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

            hunter = FlakeHunter(log, fake_runner, parallel=4)
            report = hunter.hunt(SUITE, sweeps=10, solo=6)

            names = {s.test for s in report.suspects}
            assert "t.Vic.test_victim" in names, report.format()
            assert "t.Rand.test_random" in names, report.format()
            assert "t.Clean.test_a" not in names
            assert report.tests == 6 and report.sweeps == 10
            assert report.runs > 0

            by = {s.test: s for s in report.suspects}

            # the victim is order-dependent, and the polluter is NAMED
            vic = by["t.Vic.test_victim"]
            assert vic.kind == "order-dependent", vic.to_dict()
            assert vic.polluters == ["t.Poll.test_polluter"], vic.polluters
            assert vic.solo_failures == 0
            assert "reproduce with" in vic.render()
            assert "t.Poll.test_polluter t.Vic.test_victim" in vic.render()

            # the random one is NOT blamed on ordering
            rnd = by["t.Rand.test_random"]
            assert rnd.kind == "nondeterministic", rnd.to_dict()
            assert rnd.polluters == []
            assert 0 < rnd.solo_failures < rnd.solo_runs
            assert "not test order" in rnd.render()

            # -- a clean suite is reported as clean, not as "maybe" -----
            clean = FlakeHunter(log, lambda order: {t: PASS for t in order})
            rep = clean.hunt(["a.B.test_x", "a.B.test_y"], sweeps=5, solo=3)
            assert rep.suspects == []
            assert "No flakes found" in rep.format()

            # -- a test that always fails is called broken, not flaky ---
            broken = FlakeHunter(
                log, lambda order: {t: (FAIL if "bad" in t else PASS)
                                    for t in order})
            s = broken.classify("a.B.test_bad", solo=4)
            assert s.kind == "nondeterministic" and "broken test" in s.note

            # -- unreproducible pollution is admitted, never invented ---
            shy = FlakeHunter(log, lambda order: {t: PASS for t in order})
            polluters, probes = shy.find_polluters("a.B.test_v",
                                                   ["a.B.test_1"])
            assert polluters == [] and probes >= 1

            # -- a reproduction must be pasteable, not just suggestive --
            class _WithRepro:
                def __init__(self, fn):
                    self.fn = fn

                def __call__(self, order):
                    return self.fn(order)

                def repro_command(self, order):
                    return "cd /proj && PYTHONPATH=tests python3 -m unittest " \
                        + " ".join(order)

            rng2 = random.Random(1234)

            def fake2(order):
                out, polluted = {}, False
                for t in order:
                    if t == "t.Poll.test_polluter":
                        polluted = True
                        out[t] = PASS
                    elif t == "t.Vic.test_victim":
                        out[t] = FAIL if polluted else PASS
                    else:
                        out[t] = PASS
                return out

            h2 = FlakeHunter(log, _WithRepro(fake2), parallel=4, seed=3)
            r2 = h2.hunt(["t.Poll.test_polluter", "t.Clean.test_a",
                          "t.Vic.test_victim"], sweeps=8, solo=4)
            v2 = next(s for s in r2.suspects if s.test == "t.Vic.test_victim")
            assert v2.repro.startswith("cd /proj && PYTHONPATH="), v2.repro
            assert v2.repro.endswith("t.Poll.test_polluter t.Vic.test_victim")
            assert v2.repro in v2.render()

            # -- empty input is a no-op, not a crash --------------------
            assert hunter.hunt([]).suspects == []

            # -- everything is sealed in the log ------------------------
            types = [e.type for e in log.events()]
            assert "flake.sweep" in types and "flake.report" in types
            assert types.count("flake.suspect") >= 2

            # -- the real runner at least assembles its command ---------
            # (discovery legitimately fails with no tests dir — the point
            # is that it reports empty instead of raising)
            _log.disabled = True
            try:
                r = UnittestRunner(td, start_dir="tests")
                assert r.discover() == []
                assert r([]) == {}
            finally:
                _log.disabled = False

            print("FLAKE SELF-TEST PASS")

    _self_test()

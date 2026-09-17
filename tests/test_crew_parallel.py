"""The Crew really runs subagents at the same time, and the Agent really
hands that power to the model.

The parallelism proof here is a BARRIER, not a stopwatch: the stub model
makes every subagent stop inside its first call until all of them have
arrived. Only genuine concurrency can satisfy that; a serial executor
deadlocks and the test fails. No sleep tuning, no flaky timing.
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fullagent.crew import Crew, CrewError
from fullagent.kernel import EventLog


def stub_provider():
    return SimpleNamespace(key="t", name="T", base_url="http://t",
                           api_key="sk-fake", color="#fff")


def stub_model():
    return SimpleNamespace(id="stub", provider="t", label="Stub",
                           supports_tools=True, supports_reasoning=False)


def stub_effort():
    return SimpleNamespace(key="low", label="LOW", color="#fff",
                           max_tokens=100, temperature=0.0,
                           reasoning_effort=None)


def reply(content, tool_calls=()):
    return SimpleNamespace(content=content, reasoning="",
                           tool_calls=list(tool_calls),
                           finish_reason="stop",
                           usage={"prompt_tokens": 8, "completion_tokens": 4})


class CrewParallelTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.log = EventLog(Path(self.td.name) / "crew.jsonl")

    def make_crew(self, chat, **kw):
        return Crew(self.log, stub_provider(), stub_model(), stub_effort(),
                    chat=chat, **kw)

    def test_a_whole_batch_is_in_flight_at_once(self):
        n = 5
        barrier = threading.Barrier(n, timeout=15.0)
        met = []

        def chat(*a):
            try:
                barrier.wait()
                met.append(1)
            except threading.BrokenBarrierError:
                pass                      # serial execution — caught below
            return reply("STATUS: DONE\nSUMMARY: done")

        crew = self.make_crew(chat, max_parallel=n)
        agents = [crew.spawn(f"task {i}", role="researcher")
                  for i in range(n)]
        states = crew.wait([a.id for a in agents], timeout=30.0)
        self.assertEqual(len(met), n, "subagents did not run concurrently")
        self.assertTrue(all(v == "done" for v in states.values()), states)

    def test_spawn_returns_before_the_work_finishes(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def chat(*a):
            release.wait(10.0)
            return reply("STATUS: DONE\nSUMMARY: done")

        crew = self.make_crew(chat)
        t0 = time.monotonic()
        agent = crew.spawn("slow task", role="researcher")
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(agent.state, "running")
        release.set()
        crew.wait([agent.id], timeout=15.0)

    def test_wait_wakes_the_instant_the_last_agent_settles(self):
        # zero-spin: the waiter is woken by the worker, it does not poll
        gate = threading.Event()
        self.addCleanup(gate.set)

        def chat(*a):
            gate.wait(10.0)
            return reply("STATUS: DONE\nSUMMARY: done")

        crew = self.make_crew(chat)
        agent = crew.spawn("task", role="researcher")
        threading.Timer(0.3, gate.set).start()
        t0 = time.monotonic()
        crew.wait([agent.id], timeout=20.0)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 3.0)
        self.assertEqual(agent.state, "done")

    def test_writes_from_parallel_subagents_never_interleave(self):
        # invariant I7: subagents reason in parallel, but only one of
        # them may be mutating the world at any instant
        n = 4
        target = Path(self.td.name) / "shared.txt"
        target.write_text("")
        overlap, inside, lock = [0], [0], threading.Lock()
        turn = {}

        def chat(provider, model, effort, messages, schemas, timeout):
            key = id(messages)
            turn[key] = turn.get(key, 0) + 1
            if turn[key] == 1:
                return reply("", [{"id": f"c{key}", "function": {
                    "name": "write_file",
                    "arguments": json.dumps(
                        {"path": str(target), "content": "x"})}}])
            return reply("STATUS: DONE\nSUMMARY: wrote")

        crew = self.make_crew(chat, max_parallel=n)
        real = crew._toolsets["coder"]["write_file"].handler

        def watched(**kw):
            with lock:
                inside[0] += 1
                if inside[0] > 1:
                    overlap[0] += 1
            try:
                time.sleep(0.02)
                return real(**kw)
            finally:
                with lock:
                    inside[0] -= 1

        crew._toolsets["coder"]["write_file"] = SimpleNamespace(
            handler=watched,
            openai_schema=crew._toolsets["coder"]["write_file"].openai_schema)

        agents = [crew.spawn(f"write {i}", role="coder") for i in range(n)]
        crew.wait([a.id for a in agents], timeout=30.0)
        self.assertEqual(overlap[0], 0, "two subagents wrote at once")
        self.assertTrue(all(a.tool_calls == 1 for a in agents))

    def test_a_failing_subagent_does_not_take_the_others_down(self):
        def chat(provider, model, effort, messages, schemas, timeout):
            last = next((m["content"] for m in reversed(messages)
                         if m.get("role") == "user"), "")
            if "BOOM" in last:
                raise RuntimeError("provider exploded")
            return reply("STATUS: DONE\nSUMMARY: fine")

        crew = self.make_crew(chat, max_parallel=3)
        bad = crew.spawn("BOOM", role="researcher")
        good = [crew.spawn(f"ok {i}", role="researcher") for i in range(2)]
        crew.wait(timeout=30.0)
        self.assertEqual(bad.state, "error")
        self.assertIn("provider exploded", bad.error)
        self.assertTrue(all(a.state == "done" for a in good))

    def test_force_stop_cancels_queued_subagents(self):
        block = threading.Event()
        self.addCleanup(block.set)

        def chat(*a):
            block.wait(20.0)
            return reply("STATUS: DONE\nSUMMARY: done")

        crew = self.make_crew(chat, max_parallel=1)
        agents = [crew.spawn(f"t{i}", role="researcher") for i in range(4)]
        time.sleep(0.2)
        crew.force_stop()
        block.set()
        states = crew.wait([a.id for a in agents], timeout=15.0)
        self.assertTrue(all(v != "running" for v in states.values()), states)

    def test_forget_releases_a_finished_subagent(self):
        crew = self.make_crew(lambda *a: reply("STATUS: DONE\nSUMMARY: ok"))
        agent = crew.spawn("task", role="researcher")
        crew.wait([agent.id], timeout=15.0)
        self.assertTrue(crew.forget(agent.id))
        self.assertIsNone(crew.get(agent.id))
        self.assertEqual(agent.messages, [])

    def test_forget_refuses_a_running_subagent(self):
        block = threading.Event()
        self.addCleanup(block.set)
        crew = self.make_crew(lambda *a: (block.wait(10.0),
                                          reply("STATUS: DONE\nSUMMARY: x"))[1])
        agent = crew.spawn("task", role="researcher")
        self.assertFalse(crew.forget(agent.id))
        block.set()
        crew.wait([agent.id], timeout=15.0)

    def test_roster_capacity_is_reported_not_silently_dropped(self):
        block = threading.Event()
        self.addCleanup(block.set)
        crew = self.make_crew(lambda *a: (block.wait(10.0),
                                          reply("STATUS: DONE\nSUMMARY: x"))[1],
                              max_agents=2, max_parallel=2)
        crew.spawn("a", role="researcher")
        crew.spawn("b", role="researcher")
        with self.assertRaises(CrewError):
            crew.spawn("c", role="researcher")
        block.set()
        crew.wait(timeout=15.0)


class CoalescingTests(unittest.TestCase):
    """Five subagents asking the same question get one answer, once."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.log = EventLog(Path(self.td.name) / "crew.jsonl")
        self.target = Path(self.td.name) / "shared.py"
        self.target.write_text("print('hello')\n")

    def crew_reading(self, n, runs, gate=None):
        """n subagents that each read the same file once, then report."""
        turn = {}

        def chat(provider, model, effort, messages, schemas, timeout):
            key = id(messages)
            turn[key] = turn.get(key, 0) + 1
            if turn[key] == 1:
                return reply("", [{"id": f"c{key}", "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": str(self.target)})}}])
            return reply("STATUS: DONE\nSUMMARY: read it")

        crew = Crew(self.log, stub_provider(), stub_model(), stub_effort(),
                    chat=chat, max_parallel=n)
        real = crew._toolsets["researcher"]["read_file"]

        def counted(**kw):
            runs.append(1)
            if gate is not None:
                gate.wait(10.0)          # hold every leader in flight
            return real.handler(**kw)

        crew._toolsets["researcher"]["read_file"] = SimpleNamespace(
            handler=counted, openai_schema=real.openai_schema)
        return crew

    def test_identical_concurrent_reads_execute_once(self):
        n, runs = 5, []
        crew = self.crew_reading(n, runs)
        agents = [crew.spawn(f"inspect {i}", role="researcher")
                  for i in range(n)]
        crew.wait([a.id for a in agents], timeout=30.0)

        self.assertTrue(all(a.state == "done" for a in agents))
        self.assertTrue(all(a.tool_calls == 1 for a in agents))
        # exactly ONE real read for n identical asks — "fewer than n"
        # would pass with n-1 reads and call that a win
        self.assertEqual(len(runs), 1, f"{len(runs)} real reads for {n} asks")
        self.assertEqual(sum(a.reused for a in agents), n - 1)
        # and every subagent must actually have RECEIVED the contents —
        # sharing an execution is only a win if the answer arrives
        for a in agents:
            results = [m for m in a.messages if m.get("role") == "tool"]
            self.assertEqual(len(results), 1, a.id)
            self.assertIn("print('hello')", results[0]["content"], a.id)
        stats = crew.swarm.cache.snapshot()
        self.assertEqual(stats["ran"], 1)

    def test_a_write_drops_every_cached_read(self):
        crew = Crew(self.log, stub_provider(), stub_model(), stub_effort(),
                    chat=lambda *a: reply("STATUS: DONE\nSUMMARY: x"))
        ran = []

        def read():
            ran.append(1)
            return "contents"

        key = "read_file:{\"path\": \"/x\"}"
        crew.swarm.once(key, read)
        crew.swarm.once(key, read)
        self.assertEqual(len(ran), 1, "second identical read re-executed")

        # a write invalidates everything, so the next read is real again
        crew._run_tool("write_file", {"path": "/x"}, lambda: "OK: wrote")
        crew.swarm.once(key, read)
        self.assertEqual(len(ran), 2, "a stale read survived a write")

    def test_commands_and_writes_are_never_reused(self):
        crew = Crew(self.log, stub_provider(), stub_model(), stub_effort(),
                    chat=lambda *a: reply("STATUS: DONE\nSUMMARY: x"))
        ran = []
        for _ in range(3):
            out, how = crew._run_tool(
                "run_command", {"cmd": "date"},
                lambda: (ran.append(1), "OK")[1])
            self.assertEqual(how, "ran")
        self.assertEqual(len(ran), 3, "a command was answered from cache")

    def test_a_failing_read_is_not_cached(self):
        crew = Crew(self.log, stub_provider(), stub_model(), stub_effort(),
                    chat=lambda *a: reply("STATUS: DONE\nSUMMARY: x"))
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("disk hiccup")
            return "fine"

        with self.assertRaises(OSError):
            crew.swarm.once("read_file:flaky", flaky)
        # the transient error must not become the permanent answer
        self.assertEqual(crew.swarm.once("read_file:flaky", flaky)[0], "fine")


class StragglerTests(unittest.TestCase):
    """A batch is only as fast as its slowest member — so bound it."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.log = EventLog(Path(self.td.name) / "crew.jsonl")

    def make_crew(self, chat, **kw):
        return Crew(self.log, stub_provider(), stub_model(), stub_effort(),
                    chat=chat, **kw)

    def test_a_straggler_is_asked_to_wrap_up(self):
        # seven quick subagents and one that would explore forever
        slow_steps = []

        def chat(provider, model, effort, messages, schemas, timeout):
            last = next((m["content"] for m in reversed(messages)
                         if m.get("role") == "user"), "")
            if "Reply NOW with your final report" in last:
                return reply("STATUS: DONE\nSUMMARY: partial findings")
            if "SLOW" not in last and "FOLLOW" not in last:
                return reply("STATUS: DONE\nSUMMARY: quick answer")
            # The straggler explores properly — a DIFFERENT place every
            # time — so loop detection (rightly) leaves it alone and only
            # the deadline can stop it. Conflating the two mechanisms in
            # one test would let either of them pass for the other.
            slow_steps.append(1)
            time.sleep(0.02)
            return reply("", [{"id": f"c{len(slow_steps)}", "function": {
                "name": "file_info",
                "arguments": json.dumps(
                    {"path": f"/tmp/probe-{len(slow_steps)}"})}}])

        crew = self.make_crew(chat, max_parallel=8,
                              straggler_min_grace=0.3)
        quick = [crew.spawn(f"quick {i}", role="researcher")
                 for i in range(7)]
        slow = crew.spawn("SLOW deep dive", role="researcher")
        states = crew.wait([a.id for a in quick + [slow]], timeout=60.0,
                           straggler=1.0)

        self.assertTrue(all(states[a.id] == "done" for a in quick))
        self.assertEqual(slow.stopped_by, "deadline", slow.to_dict())
        self.assertEqual(slow.state, "done")
        self.assertIn("partial findings", slow.summary)
        # it stopped far short of its 96-step budget
        self.assertLess(len(slow_steps), 96)
        self.assertIn("crew.grace", [e.type for e in self.log.events()])

    def test_the_grace_is_applied_once_and_never_slid_forward(self):
        # A deadline you move every time you look at it is not a
        # deadline. wait() re-enters its loop on every settle and on
        # every timeout slice, so the grace must be computed once and
        # then left alone.
        steps = []

        def chat(provider, model, effort, messages, schemas, timeout):
            last = next((m["content"] for m in reversed(messages)
                         if m.get("role") == "user"), "")
            if "Reply NOW with your final report" in last:
                return reply("STATUS: DONE\nSUMMARY: wrapped up")
            if "DEEP" not in last:
                return reply("STATUS: DONE\nSUMMARY: quick")
            steps.append(1)
            time.sleep(0.02)
            # a DIFFERENT place each time, so only the deadline can stop
            # it — never loop detection standing in for the deadline
            return reply("", [{"id": f"c{len(steps)}", "function": {
                "name": "file_info",
                "arguments": json.dumps(
                    {"path": f"/tmp/probe-{len(steps)}"})}}])

        crew = self.make_crew(chat, max_parallel=4,
                              straggler_min_grace=0.2)
        quick = [crew.spawn(f"q{i}", role="researcher") for i in range(3)]
        deep = crew.spawn("DEEP dive", role="researcher")
        crew.wait([a.id for a in quick + [deep]], timeout=30.0,
                  straggler=1.0)

        grace = [e for e in self.log.events() if e.type == "crew.grace"]
        self.assertEqual(len(grace), 1, "grace fired more than once")
        self.assertEqual(deep.stopped_by, "deadline")
        self.assertEqual(deep.state, "done")

    def test_a_normal_batch_is_never_stopped_early(self):
        # Grace may arm a clock the moment half the batch reports — that
        # is harmless, and the floor makes it generous. What must NEVER
        # happen is a subagent being cut short when nothing was slow.
        crew = self.make_crew(lambda *a: reply("STATUS: DONE\nSUMMARY: x"),
                              max_parallel=4)
        agents = [crew.spawn(f"t{i}", role="researcher") for i in range(4)]
        states = crew.wait([a.id for a in agents], timeout=20.0,
                           straggler=1.0)
        self.assertTrue(all(v == "done" for v in states.values()), states)
        self.assertTrue(all(a.stopped_by == "" for a in agents),
                        [a.to_dict() for a in agents])
        self.assertEqual(
            [e for e in self.log.events() if e.type == "crew.stopped"], [])

    def test_a_lone_subagent_is_never_graced(self):
        # with one target there is no batch and no median to measure
        block = threading.Event()
        self.addCleanup(block.set)

        def chat(*a):
            block.wait(1.0)
            return reply("STATUS: DONE\nSUMMARY: fine")

        crew = self.make_crew(chat, straggler_min_grace=0.1)
        agent = crew.spawn("alone", role="researcher")
        crew.wait([agent.id], timeout=20.0, straggler=0.01)
        self.assertEqual(agent.stopped_by, "")
        self.assertEqual(agent.state, "done")


class LoopDetectionTests(unittest.TestCase):
    """A circling subagent circles until its budget runs out. Stop it."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.log = EventLog(Path(self.td.name) / "crew.jsonl")

    def test_a_repeated_tool_call_ends_the_loop(self):
        calls = []

        def chat(provider, model, effort, messages, schemas, timeout):
            last = next((m["content"] for m in reversed(messages)
                         if m.get("role") == "user"), "")
            if "Reply NOW with your final report" in last:
                return reply("STATUS: DONE\nSUMMARY: what I found so far")
            calls.append(1)
            # the same call, with the same arguments, forever
            return reply("", [{"id": f"c{len(calls)}", "function": {
                "name": "list_dir",
                "arguments": json.dumps({"path": self.td.name})}}])

        crew = Crew(self.log, stub_provider(), stub_model(), stub_effort(),
                    chat=chat)
        agent = crew.spawn("go in circles", role="researcher")
        crew.wait([agent.id], timeout=30.0)

        self.assertEqual(agent.stopped_by, "loop")
        self.assertEqual(agent.state, "done")
        self.assertIn("what I found", agent.summary)
        # stopped at the repeat threshold, nowhere near the step budget
        self.assertLessEqual(len(calls), 4, len(calls))
        stopped = [e for e in self.log.events() if e.type == "crew.stopped"]
        self.assertEqual(stopped[0].data["reason"], "loop")

    def test_varied_tool_calls_are_left_alone(self):
        # exploring many DIFFERENT places is the job, not a loop
        calls = []

        def chat(provider, model, effort, messages, schemas, timeout):
            calls.append(1)
            if len(calls) > 6:
                return reply("STATUS: DONE\nSUMMARY: explored properly")
            return reply("", [{"id": f"c{len(calls)}", "function": {
                "name": "file_info",
                "arguments": json.dumps(
                    {"path": f"/tmp/f{len(calls)}"})}}])

        crew = Crew(self.log, stub_provider(), stub_model(), stub_effort(),
                    chat=chat)
        agent = crew.spawn("explore widely", role="researcher")
        crew.wait([agent.id], timeout=30.0)
        self.assertEqual(agent.stopped_by, "")
        self.assertEqual(agent.state, "done")
        self.assertIn("explored properly", agent.summary)

    def test_a_stopped_subagent_still_reports_what_it_found(self):
        # the point is not to kill it — the work already done is real
        def chat(provider, model, effort, messages, schemas, timeout):
            last = next((m["content"] for m in reversed(messages)
                         if m.get("role") == "user"), "")
            if "Reply NOW" in last:
                # it can see everything it gathered before being stopped
                tool_results = [m for m in messages
                                if m.get("role") == "tool"]
                return reply(f"STATUS: DONE\nSUMMARY: saw "
                             f"{len(tool_results)} results")
            return reply("", [{"id": "c", "function": {
                "name": "file_info",
                "arguments": json.dumps({"path": "/same"})}}])

        crew = Crew(self.log, stub_provider(), stub_model(), stub_effort(),
                    chat=chat)
        agent = crew.spawn("circle", role="researcher")
        crew.wait([agent.id], timeout=30.0)
        self.assertIn("saw", agent.summary)
        self.assertNotIn("saw 0 results", agent.summary)


if __name__ == "__main__":
    unittest.main()

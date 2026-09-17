"""Regressions found by auditing the parallel-subagent work.

Every test here corresponds to a bug that was real, shipped, and caught
on review rather than by the existing suite — which is precisely why
each one needed a test of its own. They are grouped by what the bug
would have looked like in use, because that is how the next one will be
noticed too.
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fullagent.crew import Crew
from fullagent.kernel import EventLog
from fullagent.swarm import (AdaptiveSemaphore, LoadGovernor, Swarm,
                             BACKGROUND, FOREGROUND)
from fullagent.team import ROLES


def stub():
    return (SimpleNamespace(key="t", name="T", base_url="http://t",
                            api_key="k", color="#fff"),
            SimpleNamespace(id="stub", provider="t", label="S",
                            supports_tools=True, supports_reasoning=False),
            SimpleNamespace(key="low", label="LOW", color="#fff",
                            max_tokens=10, temperature=0.0,
                            reasoning_effort=None))


def reply(content, tool_calls=()):
    return SimpleNamespace(content=content, reasoning="",
                           tool_calls=list(tool_calls), finish_reason="stop",
                           usage={"prompt_tokens": 4, "completion_tokens": 2})


class ToolAccessTests(unittest.TestCase):
    """A subagent silently losing a tool is the worst kind of bug: it
    does not fail, it just cannot do its job, and the report reads like
    the task was impossible."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.log = EventLog(Path(self.td.name) / "c.jsonl")

    def test_non_writing_roles_still_get_run_command(self):
        # tester/analyst/debugger/optimizer are declared writes=False but
        # every one of them needs run_command. Deriving "read-only" from
        # "does not write files" takes the test runner away from the
        # tester, and nothing about the resulting report says so.
        for role in ("tester", "analyst", "debugger", "optimizer"):
            with self.subTest(role=role):
                self.assertFalse(ROLES[role]["writes"])
                self.assertIn("run_command", ROLES[role]["tools"])

    def test_a_tester_subagent_can_actually_run_a_command(self):
        offered = {}

        def chat(provider, model, effort, messages, schemas, timeout):
            offered["names"] = {s["function"]["name"] for s in (schemas or [])}
            return reply("STATUS: DONE\nSUMMARY: ran the suite")

        p, m, e = stub()
        crew = Crew(self.log, p, m, e, chat=chat)
        agent = crew.spawn("run the test suite", role="tester")
        crew.wait([agent.id], timeout=20.0)
        self.assertIn("run_command", offered["names"])
        self.assertEqual(agent.state, "done")

    def test_read_only_really_does_remove_the_write_tools(self):
        offered = {}

        def chat(provider, model, effort, messages, schemas, timeout):
            offered["names"] = {s["function"]["name"] for s in (schemas or [])}
            return reply("STATUS: DONE\nSUMMARY: looked only")

        p, m, e = stub()
        crew = Crew(self.log, p, m, e, chat=chat)
        agent = crew.spawn("inspect only", role="coder", read_only=True)
        crew.wait([agent.id], timeout=20.0)
        self.assertNotIn("write_file", offered["names"])
        self.assertNotIn("run_command", offered["names"])


class FollowUpTests(unittest.TestCase):
    """State from a finished run leaking into a follow-up made the
    follow-up a silent no-op — which looks like an answer."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.log = EventLog(Path(self.td.name) / "c.jsonl")

    def make(self, chat, **kw):
        p, m, e = stub()
        return Crew(self.log, p, m, e, chat=chat, **kw)

    def test_a_stopped_subagent_can_still_be_followed_up(self):
        # a stale soft_deadline sits in the past, so the follow-up's
        # FIRST step would decide time was up and finalize again
        turns = []

        def chat(provider, model, effort, messages, schemas, timeout):
            last = next((m["content"] for m in reversed(messages)
                         if m.get("role") == "user"), "")
            turns.append(last[:30])
            if "Reply NOW" in last:
                return reply("STATUS: DONE\nSUMMARY: partial")
            if last.startswith("FOLLOW-UP"):
                return reply("STATUS: DONE\nSUMMARY: finished the job")
            return reply("", [{"id": "c", "function": {
                "name": "file_info",
                "arguments": json.dumps({"path": "/same"})}}])

        crew = self.make(chat)
        agent = crew.spawn("circle", role="researcher")
        crew.wait([agent.id], timeout=20.0)
        self.assertEqual(agent.stopped_by, "loop")

        crew.send(agent.id, "now finish it properly")
        crew.wait([agent.id], timeout=20.0)
        self.assertEqual(agent.summary, "finished the job")
        self.assertEqual(agent.stopped_by, "",
                         "the report still claims to be partial")
        self.assertEqual(agent.soft_deadline, 0.0)

    def test_a_follow_up_may_repeat_an_earlier_tool_call(self):
        # loop signatures from the previous run would trip the detector
        # on the follow-up's very first call
        calls = []

        def chat(provider, model, effort, messages, schemas, timeout):
            last = next((m["content"] for m in reversed(messages)
                         if m.get("role") == "user"), "")
            if "Reply NOW" in last:
                return reply("STATUS: DONE\nSUMMARY: partial")
            if any(m.get("role") == "tool" for m in messages) and \
                    last.startswith("FOLLOW-UP"):
                return reply("STATUS: DONE\nSUMMARY: re-checked it")
            calls.append(1)
            return reply("", [{"id": f"c{len(calls)}", "function": {
                "name": "file_info",
                "arguments": json.dumps({"path": "/same"})}}])

        crew = self.make(chat)
        agent = crew.spawn("circle", role="researcher")
        crew.wait([agent.id], timeout=20.0)
        self.assertEqual(agent.stopped_by, "loop")

        crew.send(agent.id, "re-check that same path once more")
        crew.wait([agent.id], timeout=20.0)
        self.assertEqual(agent.stopped_by, "")
        self.assertIn("re-checked", agent.summary)

    def test_resume_also_clears_the_previous_run_s_limits(self):
        crew = self.make(lambda *a: reply("STATUS: DONE\nSUMMARY: x"))
        agent = crew.spawn("t", role="researcher")
        crew.wait([agent.id], timeout=20.0)
        agent.soft_deadline = time.monotonic() - 100
        agent.stopped_by = "deadline"
        crew.close(agent.id)
        crew.resume(agent.id)
        self.assertEqual(agent.soft_deadline, 0.0)
        self.assertEqual(agent.stopped_by, "")


class BudgetTests(unittest.TestCase):
    """Running out of steps used to discard every step of the work."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.log = EventLog(Path(self.td.name) / "c.jsonl")

    def test_exhausting_the_step_budget_still_reports(self):
        import fullagent.crew as crew_mod
        saved = crew_mod.MAX_WORKER_STEPS
        crew_mod.MAX_WORKER_STEPS = 3
        self.addCleanup(setattr, crew_mod, "MAX_WORKER_STEPS", saved)

        calls = []

        def chat(provider, model, effort, messages, schemas, timeout):
            last = next((m["content"] for m in reversed(messages)
                         if m.get("role") == "user"), "")
            if "Reply NOW" in last:
                seen = [m for m in messages if m.get("role") == "tool"]
                return reply(f"STATUS: DONE\nSUMMARY: gathered {len(seen)}")
            calls.append(1)
            # always a NEW place, so neither loop detection nor a
            # deadline can stand in for the budget
            return reply("", [{"id": f"c{len(calls)}", "function": {
                "name": "file_info",
                "arguments": json.dumps({"path": f"/p{len(calls)}"})}}])

        p, m, e = stub()
        crew = Crew(self.log, p, m, e, chat=chat)
        agent = crew.spawn("explore widely", role="researcher")
        crew.wait([agent.id], timeout=20.0)

        self.assertEqual(agent.stopped_by, "budget")
        self.assertEqual(agent.state, "done")
        self.assertIn("gathered 3", agent.summary)
        self.assertEqual(agent.error, "")


class ForegroundLaneTests(unittest.TestCase):
    """The reserved lane has to hold at the ONE moment it matters: when
    the governor is down to a single permit."""

    def test_foreground_is_guaranteed_even_at_one_permit(self):
        sem = AdaptiveSemaphore(
            LoadGovernor(ceiling=1, cores=4, load_source=lambda: 99.0),
            reserve=1)
        self.assertEqual(sem._limit_for(BACKGROUND), 1)
        self.assertGreater(sem._limit_for(FOREGROUND),
                           sem._limit_for(BACKGROUND))

    def test_the_sovereign_does_not_queue_behind_a_subagent(self):
        sw = Swarm(max_parallel=2, idle_ttl=0.2, name="lane", reserve=1,
                   governor=LoadGovernor(ceiling=1, cores=4,
                                         load_source=lambda: 99.0))
        self.addCleanup(sw.close)
        taken = threading.Event()
        let_go = threading.Event()
        self.addCleanup(let_go.set)

        def subagent_work():
            with sw.cpu(timeout=5.0) as ok:
                taken.set()
                let_go.wait(5.0)
                return ok

        sw.submit(subagent_work)
        self.assertTrue(taken.wait(5.0))
        t0 = time.monotonic()
        with sw.foreground(timeout=3.0) as ok:
            waited = time.monotonic() - t0
        self.assertTrue(ok, "the sovereign was refused its reserved permit")
        self.assertLess(waited, 1.0, f"sovereign queued for {waited:.2f}s")


class RateLimitTests(unittest.TestCase):
    """Congestion control that never hears about congestion is decoration."""

    def test_a_rate_limit_reaches_the_congestion_window(self):
        import fullagent.team as team_mod

        class Boom(Exception):
            status = 429

        calls = {"n": 0}

        def fake_chat(*a, **kw):
            calls["n"] += 1
            if calls["n"] < 2:
                raise Boom("rate limit exceeded")
            return reply("STATUS: DONE\nSUMMARY: ok")

        saved = (team_mod.chat_blocking, team_mod.APIError,
                 team_mod.time.sleep)
        team_mod.chat_blocking = fake_chat
        team_mod.APIError = Boom
        team_mod.time.sleep = lambda _s: None

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        try:
            log = EventLog(Path(td.name) / "c.jsonl")
            p, m, e = stub()
            # chat=None on purpose: this is the PRODUCTION path, the one
            # that was silently unreachable
            crew = Crew(log, p, m, e, max_parallel=8)
            before = crew.swarm.net_window.window
            agent = crew.spawn("do a thing", role="researcher")
            crew.wait([agent.id], timeout=20.0)
        finally:
            (team_mod.chat_blocking, team_mod.APIError,
             team_mod.time.sleep) = saved

        self.assertEqual(agent.state, "done")
        self.assertEqual(crew.swarm.net_window.rate_limited, 1)
        self.assertLess(crew.swarm.net_window.window, before,
                        "the window never narrowed after a 429")


if __name__ == "__main__":
    unittest.main()

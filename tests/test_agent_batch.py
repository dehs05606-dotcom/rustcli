"""The Agent's batch runner: planned by the Mastermind, run in parallel,
reported back in the caller's own order.

Order is the contract every caller depends on — the daemon, the task
market, compiled waves and racing universes all index into the reports
they get back. These tests hold that contract against the things most
likely to break it: duplicates, refusals, failures and waves.
"""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("FULLAGENT_LOG_LEVEL", "QUIET")

from fullagent import config
from fullagent.agent import Agent


def reply(content):
    return SimpleNamespace(content=content, reasoning="", tool_calls=[],
                           finish_reason="stop",
                           usage={"prompt_tokens": 5, "completion_tokens": 3})


class BatchTests(unittest.TestCase):
    """Drives the real Agent with a stub model — no network, no keys."""

    @classmethod
    def setUpClass(cls):
        # config resolves APP_DIR at IMPORT time, so overriding HOME here
        # would be too late — the paths are already bound. Redirect the
        # bound paths instead, so a test run never touches the real
        # ~/.fullagent (its event log, its sessions, its calibration).
        cls._home = tempfile.TemporaryDirectory()
        root = Path(cls._home.name)
        cls._saved = {name: getattr(config, name)
                      for name in ("APP_DIR", "SESSIONS_DIR",
                                   "EVENT_LOG_FILE")}
        config.APP_DIR = root
        config.SESSIONS_DIR = root / "sessions"
        config.EVENT_LOG_FILE = root / "eventlog.jsonl"

    @classmethod
    def tearDownClass(cls):
        for name, value in cls._saved.items():
            setattr(config, name, value)
        cls._home.cleanup()

    def make_agent(self, chat):
        agent = Agent(config.Config())
        crew = agent._ensure_crew()
        crew._chat = chat
        return agent

    def test_reports_come_back_one_per_task_in_order(self):
        def chat(provider, model, effort, messages, schemas, timeout):
            task = next((m["content"] for m in reversed(messages)
                         if m.get("role") == "user"), "")
            tag = task.rsplit("YOUR TASK: ", 1)[-1].strip()
            return reply(f"STATUS: DONE\nSUMMARY: handled {tag}")

        agent = self.make_agent(chat)
        tasks = [{"task": f"look at thing {i}", "role": "researcher"}
                 for i in range(4)]
        reports = agent._run_workers(tasks, timeout=60)
        self.assertEqual(len(reports), 4)
        for i, r in enumerate(reports):
            self.assertEqual(r.task, f"look at thing {i}")
            self.assertEqual(r.status, "done")
            self.assertIn(f"thing {i}", r.summary)

    def test_duplicate_tasks_get_their_own_reports(self):
        # the exact case a task-text-keyed map silently collapses
        agent = self.make_agent(
            lambda *a: reply("STATUS: DONE\nSUMMARY: ok"))
        tasks = [{"task": "the same task", "role": "researcher"}] * 3
        reports = agent._run_workers(tasks, timeout=60)
        self.assertEqual(len(reports), 3)
        self.assertTrue(all(r.status == "done" for r in reports), reports)

    def test_an_unknown_role_is_refused_in_place(self):
        agent = self.make_agent(
            lambda *a: reply("STATUS: DONE\nSUMMARY: ok"))
        reports = agent._run_workers([
            {"task": "good one", "role": "researcher"},
            {"task": "bad one", "role": "sorcerer"},
            {"task": "another good one", "role": "reviewer"},
        ], timeout=60)
        self.assertEqual(len(reports), 3)
        self.assertEqual(reports[0].status, "done")
        self.assertEqual(reports[1].status, "error")
        self.assertIn("sorcerer", reports[1].error)
        self.assertEqual(reports[2].status, "done")

    def test_one_failure_does_not_spoil_the_batch(self):
        def chat(provider, model, effort, messages, schemas, timeout):
            last = next((m["content"] for m in reversed(messages)
                         if m.get("role") == "user"), "")
            if "EXPLODE" in last:
                raise RuntimeError("provider said no")
            return reply("STATUS: DONE\nSUMMARY: ok")

        agent = self.make_agent(chat)
        reports = agent._run_workers([
            {"task": "fine one", "role": "researcher"},
            {"task": "EXPLODE", "role": "researcher"},
            {"task": "also fine", "role": "researcher"},
        ], timeout=60)
        self.assertEqual([r.status for r in reports],
                         ["done", "error", "done"])
        self.assertIn("provider said no", reports[1].error)

    def test_writers_on_one_file_are_planned_into_separate_waves(self):
        agent = self.make_agent(
            lambda *a: reply("STATUS: DONE\nSUMMARY: ok"))
        plan = agent._ensure_conductor().plan([
            {"task": "add a parser to crew.py", "role": "coder"},
            {"task": "add a test to crew.py", "role": "coder"},
        ])
        self.assertEqual(len(plan.waves), 2, plan.format())

    def test_reads_are_planned_into_one_wide_wave(self):
        agent = self.make_agent(
            lambda *a: reply("STATUS: DONE\nSUMMARY: ok"))
        plan = agent._ensure_conductor().plan([
            {"task": f"survey area {i}", "role": "researcher"}
            for i in range(6)])
        self.assertEqual(len(plan.waves), 1, plan.format())
        self.assertEqual(plan.widest, 6)

    def test_every_plan_is_sealed_in_the_event_log(self):
        agent = self.make_agent(
            lambda *a: reply("STATUS: DONE\nSUMMARY: ok"))
        agent._run_workers([{"task": "one", "role": "researcher"}],
                           timeout=60)
        sealed = [e for e in agent.log.events()
                  if e.type == "orchestra.plan"]
        self.assertTrue(sealed)
        self.assertIn("waves", sealed[-1].data)

    def test_a_cancelled_batch_reports_rather_than_hangs(self):
        block = threading.Event()
        self.addCleanup(block.set)

        def chat(*a):
            block.wait(20.0)
            return reply("STATUS: DONE\nSUMMARY: ok")

        agent = self.make_agent(chat)
        agent._cancel_flag.set()
        self.addCleanup(agent._cancel_flag.clear)
        reports = agent._run_workers(
            [{"task": f"t{i}", "role": "researcher"} for i in range(3)],
            timeout=30)
        self.assertEqual(len(reports), 3)
        self.assertTrue(all(r.status == "error" for r in reports), reports)

    def test_empty_and_blank_batches_are_a_no_op(self):
        agent = self.make_agent(
            lambda *a: reply("STATUS: DONE\nSUMMARY: ok"))
        self.assertEqual(agent._run_workers([]), [])
        self.assertEqual(agent._run_workers([{"task": "   "}]), [])

    def test_a_tester_in_a_batch_keeps_its_test_runner(self):
        # read-only is the CALLER's decision. Deriving it from "this role
        # does not write files" silently strips run_command from tester,
        # analyst, debugger and optimizer — and the resulting report just
        # reads as though the task were impossible.
        offered = {}

        def chat(provider, model, effort, messages, schemas, timeout):
            offered["names"] = {s["function"]["name"]
                                for s in (schemas or [])}
            return reply("STATUS: DONE\nSUMMARY: suite is green")

        agent = self.make_agent(chat)
        reports = agent._run_workers(
            [{"task": "run the test suite", "role": "tester"}], timeout=60)
        self.assertIn("run_command", offered["names"],
                      "the tester lost its test runner")
        self.assertEqual(reports[0].status, "done")

    def test_read_only_batches_really_are_read_only(self):
        offered = {}

        def chat(provider, model, effort, messages, schemas, timeout):
            offered["names"] = {s["function"]["name"]
                                for s in (schemas or [])}
            return reply("STATUS: DONE\nSUMMARY: looked only")

        agent = self.make_agent(chat)
        agent._run_workers([{"task": "inspect it", "role": "coder"}],
                           read_only=True, timeout=60)
        self.assertNotIn("write_file", offered["names"])
        self.assertNotIn("run_command", offered["names"])

    def test_a_partial_report_is_marked_as_partial(self):
        # a subagent stopped early must never be read as a complete
        # answer — that is the one way an early stop could do harm
        def chat(provider, model, effort, messages, schemas, timeout):
            last = next((m["content"] for m in reversed(messages)
                         if m.get("role") == "user"), "")
            if "Reply NOW" in last:
                return reply("STATUS: DONE\nSUMMARY: what I had so far")
            return SimpleNamespace(
                content="", reasoning="",
                tool_calls=[{"id": "c", "function": {
                    "name": "file_info",
                    "arguments": '{"path": "/same/place"}'}}],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 2, "completion_tokens": 1})

        agent = self.make_agent(chat)
        reports = agent._run_workers(
            [{"task": "go in circles", "role": "researcher"}], timeout=60)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].stopped_by, "loop")
        self.assertIn("what I had so far", reports[0].summary)
        self.assertEqual(reports[0].status, "done")
        self.assertIn("stopped_by", reports[0].to_dict())

    def test_a_sovereign_write_drops_the_subagents_cached_reads(self):
        # One filesystem, one truth. The invalidation used to live inside
        # the "subagents are in flight AND this tool is metered" branch,
        # which missed the two cases that matter most: run_command (the
        # tool most likely to rewrite the tree, and unmetered), and any
        # write made while no subagent happened to be running — whose
        # stale entries are then served to the very next batch.
        agent = self.make_agent(
            lambda *a: reply("STATUS: DONE\nSUMMARY: ok"))
        crew = agent._ensure_crew()
        ran = []
        key = 'read_file:{"path": "/x"}'
        crew.swarm.once(key, lambda: (ran.append(1), "old")[1])
        crew.swarm.once(key, lambda: (ran.append(1), "old")[1])
        self.assertEqual(len(ran), 1, "the cache never held it")
        self.assertEqual(crew.swarm.in_flight, 0)

        # the sovereign runs a command that rewrites the tree, through
        # the REAL tool path — the layer the bug was actually in
        from fullagent.agent import ToolEvent
        from fullagent.tools import RISK_SAFE, Tool

        ev = ToolEvent(name="run_command",
                       args={"command": "git checkout ."})
        real = agent.tools["run_command"]
        agent.tools["run_command"] = Tool(
            "run_command", real.description, real.parameters,
            lambda **kw: "exit code: 0", RISK_SAFE)
        self.addCleanup(agent.tools.__setitem__, "run_command", real)
        agent._execute_tool(ev, approve=lambda *a, **k: True,
                            on_status=lambda *a, **k: None)
        self.assertEqual(ev.status, "done", ev.result)

        crew.swarm.once(key, lambda: (ran.append(1), "new")[1])
        self.assertEqual(len(ran), 2,
                         "a subagent could still read the pre-write copy")

    def test_a_workflow_step_runs_a_real_subagent(self):
        # /workflow used to die on its first step: the executor was a
        # stub returning "Crew feature has been removed", so the whole
        # pipeline engine was dead behind a command that still looked
        # like it worked.
        def chat(provider, model, effort, messages, schemas, timeout):
            return reply("STATUS: DONE\nSUMMARY: implemented the parser")

        agent = self.make_agent(chat)
        out = agent._workflow_step({"task": "implement the parser",
                                    "role": "coder"})
        self.assertEqual(out["status"], "done", out)
        self.assertIn("implemented the parser", out["summary"])
        self.assertNotIn("removed", out["summary"])

    def test_a_workflow_step_without_a_task_is_rejected(self):
        agent = self.make_agent(
            lambda *a: reply("STATUS: DONE\nSUMMARY: ok"))
        out = agent._workflow_step({"role": "coder"})
        self.assertEqual(out["status"], "error")

    def test_a_partial_workflow_step_says_so(self):
        def chat(provider, model, effort, messages, schemas, timeout):
            last = next((m["content"] for m in reversed(messages)
                         if m.get("role") == "user"), "")
            if "Reply NOW" in last:
                return reply("STATUS: DONE\nSUMMARY: got partway")
            return SimpleNamespace(
                content="", reasoning="",
                tool_calls=[{"id": "c", "function": {
                    "name": "file_info",
                    "arguments": '{"path": "/same"}'}}],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 2, "completion_tokens": 1})

        agent = self.make_agent(chat)
        out = agent._workflow_step({"task": "go in circles",
                                    "role": "researcher"})
        self.assertIn("[partial", out["summary"])

    def test_a_parallel_batch_is_visible_while_it_runs(self):
        # Eight subagents used to be a spinner and a rising number of
        # seconds: working hard and hung look identical from outside,
        # which is the difference between waiting and killing a turn two
        # seconds from done.
        lines, status = [], []

        def chat(provider, model, effort, messages, schemas, timeout):
            if any(m.get("role") == "tool" for m in messages):
                return reply("STATUS: DONE\nSUMMARY: looked at it")
            return SimpleNamespace(
                content="", reasoning="",
                tool_calls=[{"id": "c", "function": {
                    "name": "read_file",
                    "arguments": '{"path": "fullagent/swarm.py"}'}}],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 5, "completion_tokens": 2})

        agent = self.make_agent(chat)
        agent._turn_output = lambda line, stream: lines.append((stream, line))
        agent._turn_status = status.append
        agent._run_workers(
            [{"task": f"investigate area {i}", "role": "researcher"}
             for i in range(3)], timeout=60)

        self.assertTrue(lines, "a parallel batch produced no live output")
        self.assertTrue(all(s == "crew" for s, _ in lines))
        text = "\n".join(l for _, l in lines)

        # every subagent announced its task, its tool, and its verdict
        for i in range(3):
            self.assertIn(f"investigate area {i}", text)
        self.assertIn("read_file fullagent/swarm.py", text)
        self.assertEqual(text.count("✓"), 3, text)

        # and the border always said how far along the batch was
        self.assertTrue(any("crew 3/3 done" in s for s in status), status)

    def test_a_shared_finding_is_announced_once(self):
        lines = []

        def chat(provider, model, effort, messages, schemas, timeout):
            if any(m.get("role") == "tool" for m in messages):
                return reply("STATUS: DONE\nSUMMARY: done")
            return SimpleNamespace(
                content="", reasoning="",
                tool_calls=[{"id": "c", "function": {
                    "name": "share_finding",
                    "arguments": '{"finding": "config lives in config.py"}'}}],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 5, "completion_tokens": 2})

        agent = self.make_agent(chat)
        agent._turn_output = lambda line, stream: lines.append(line)
        agent._run_workers([{"task": "scout", "role": "researcher"}],
                           timeout=60)

        text = "\n".join(lines)
        self.assertIn("shares: config lives in config.py", text)
        # announced by the ◆ line only — not also as a raw tool call
        self.assertEqual(text.count("config lives in config.py"), 1, text)

    def test_the_watcher_never_takes_a_subagent_down(self):
        # the UI is never load-bearing
        def boom(line, stream):
            raise RuntimeError("the terminal exploded")

        agent = self.make_agent(
            lambda *a: reply("STATUS: DONE\nSUMMARY: fine"))
        agent._turn_output = boom
        reports = agent._run_workers(
            [{"task": "still works", "role": "researcher"}], timeout=60)
        self.assertEqual(reports[0].status, "done", reports[0].error)

    def test_the_legacy_alias_still_works(self):
        agent = self.make_agent(
            lambda *a: reply("STATUS: DONE\nSUMMARY: ok"))
        reports = agent._run_worker_serial(
            [{"task": "still works", "role": "researcher"}])
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].status, "done")


if __name__ == "__main__":
    unittest.main()

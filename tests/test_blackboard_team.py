"""Parallel subagents as a TEAM, not eight strangers.

Coalescing removes duplicated CALLS. This removes duplicated
KNOWLEDGE — the four turns a second subagent would spend working out
what the first one already established. These tests hold the properties
that make that safe to switch on: each fact costs one entry however many
subagents find it, each subagent pays for each fact once, nobody is
handed their own discovery back, and the board can never grow without
bound.
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from fullagent.blackboard import Blackboard
from fullagent.crew import Crew
from fullagent.kernel import EventLog


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


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.log = EventLog(Path(self.td.name) / "c.jsonl")
        self.board = Blackboard(self.log)

    def make(self, chat, **kw):
        p, m, e = stub()
        return Crew(self.log, p, m, e, chat=chat, board=self.board, **kw)

    def test_a_finding_reaches_the_other_subagents(self):
        seen_by_peer = []

        def chat(provider, model, effort, messages, schemas, timeout):
            last = next((m["content"] for m in reversed(messages)
                         if m.get("role") == "user"), "")
            if "FINDINGS FROM YOUR PEERS" in last:
                seen_by_peer.append(last)
                return reply("STATUS: DONE\nSUMMARY: used my peer's work")
            if "SCOUT" in last and not any(m.get("role") == "tool"
                                           for m in messages):
                return reply("", [{"id": "c1", "function": {
                    "name": "share_finding",
                    "arguments": json.dumps(
                        {"finding": "the config lives in "
                                    "fullagent/config.py"})}}])
            if any(m.get("role") == "tool" for m in messages):
                return reply("STATUS: DONE\nSUMMARY: shared it")
            # the peer loops once so it has a turn AFTER the scout posts
            return reply("", [{"id": "c2", "function": {
                "name": "file_info",
                "arguments": json.dumps({"path": "/tmp"})}}])

        crew = self.make(chat, max_parallel=2)
        scout = crew.spawn("SCOUT the layout", role="researcher")
        crew.wait([scout.id], timeout=20.0)
        self.assertEqual(scout.shared, 1, scout.to_dict())

        peer = crew.spawn("do the other half", role="researcher")
        crew.wait([peer.id], timeout=20.0)

        self.assertTrue(seen_by_peer, "the finding never reached the peer")
        self.assertIn("fullagent/config.py", seen_by_peer[0])
        self.assertEqual(peer.learned, 1)

    def test_a_subagent_is_never_handed_its_own_finding(self):
        # a worker being told its own discovery reads like confirmation
        delivered = []

        def chat(provider, model, effort, messages, schemas, timeout):
            last = next((m["content"] for m in reversed(messages)
                         if m.get("role") == "user"), "")
            if "FINDINGS FROM YOUR PEERS" in last:
                delivered.append(last)
                return reply("STATUS: DONE\nSUMMARY: done")
            if any(m.get("role") == "tool" for m in messages):
                return reply("STATUS: DONE\nSUMMARY: done")
            return reply("", [{"id": "c", "function": {
                "name": "share_finding",
                "arguments": json.dumps({"finding": "only I know this"})}}])

        crew = self.make(chat)
        agent = crew.spawn("find something", role="researcher")
        crew.wait([agent.id], timeout=20.0)
        self.assertEqual(agent.shared, 1)
        self.assertEqual(agent.learned, 0)
        self.assertEqual(delivered, [])

    def test_the_same_discovery_from_many_subagents_costs_one_entry(self):
        def chat(provider, model, effort, messages, schemas, timeout):
            if any(m.get("role") == "tool" for m in messages):
                return reply("STATUS: DONE\nSUMMARY: done")
            return reply("", [{"id": "c", "function": {
                "name": "share_finding",
                "arguments": json.dumps(
                    {"finding": "tests are run with run_selftests.py"})}}])

        crew = self.make(chat, max_parallel=5)
        agents = [crew.spawn(f"look at {i}", role="researcher")
                  for i in range(5)]
        crew.wait([a.id for a in agents], timeout=30.0)
        self.assertEqual(len(self.board.all()), 1)
        self.assertEqual(self.board.duplicates, 4)
        # only the one that got there first counts as having shared
        self.assertEqual(sum(a.shared for a in agents), 1)

    def test_a_read_only_scout_can_still_share(self):
        # read-only subagents are exactly the ones whose findings the
        # rest most need, so the read-only filter must not strip this
        offered = {}

        def chat(provider, model, effort, messages, schemas, timeout):
            offered["names"] = {s["function"]["name"]
                                for s in (schemas or [])}
            return reply("STATUS: DONE\nSUMMARY: looked")

        crew = self.make(chat)
        agent = crew.spawn("scout", role="coder", read_only=True)
        crew.wait([agent.id], timeout=20.0)
        self.assertIn("share_finding", offered["names"])
        self.assertNotIn("write_file", offered["names"])

    def test_the_board_is_bounded(self):
        board = Blackboard(self.log, max_facts=3, max_chars=50)
        for i in range(20):
            board.post(f"fact {i}", agent_id="x")
        self.assertEqual(len(board.all()), 3)
        self.assertTrue(all(len(f.text) <= 50 for f in board.all()))

    def test_delivery_is_capped_per_turn(self):
        board = Blackboard(self.log)
        for i in range(30):
            board.post(f"finding number {i}", agent_id="other")
        fresh, cursor = board.since(0, exclude_agent="me", limit=8)
        self.assertEqual(len(fresh), 8)
        # the cursor clears everything inspected, so the next turn does
        # not re-deliver the same backlog forever
        again, _ = board.since(cursor, exclude_agent="me")
        self.assertEqual(again, [])

    def test_a_crew_without_a_board_has_no_share_tool(self):
        offered = {}

        def chat(provider, model, effort, messages, schemas, timeout):
            offered["names"] = {s["function"]["name"]
                                for s in (schemas or [])}
            return reply("STATUS: DONE\nSUMMARY: done")

        p, m, e = stub()
        crew = Crew(self.log, p, m, e, chat=chat)   # board=None
        agent = crew.spawn("solo", role="researcher")
        crew.wait([agent.id], timeout=20.0)
        self.assertNotIn("share_finding", offered["names"])
        self.assertEqual(agent.shared, 0)

    def test_concurrent_posting_loses_nothing_and_duplicates_nothing(self):
        board = Blackboard(self.log, max_facts=500)
        start = threading.Barrier(8)

        def spam(n):
            start.wait()
            for i in range(20):
                board.post(f"shared fact {i % 10}", agent_id=f"c{n}")

        threads = [threading.Thread(target=spam, args=(n,))
                   for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20.0)
        facts = board.all()
        self.assertEqual(len(facts), 10)
        self.assertEqual(len({f.text for f in facts}), 10)
        self.assertEqual([f.seq for f in facts], list(range(10)))


if __name__ == "__main__":
    unittest.main()

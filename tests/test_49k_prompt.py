"""Every claim in this work, measured at 49,000 characters.

The repo's own prompt is 4.2k, and a number measured there is the wrong
number for someone running a prompt ten times the size. These tests
rebuild the whole path — seal, dispatch, place, index, look up, audit —
against a 49k prompt, because that is the prompt that is actually in use.

Run this file directly to print the measurement table.
"""

import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("FULLAGENT_LOG_LEVEL", "QUIET")

from fullagent import config, promptaudit, systemprompt
from fullagent.agent import Agent
from fullagent.client import estimate_tokens
from fullagent.spec import PromptIndex

try:
    from tests.fixture49k import build
except ImportError:                                # run from tests/
    from fixture49k import build

PROMPT = build()


class Prompt49kTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._home = tempfile.TemporaryDirectory()
        root = Path(cls._home.name)
        cls._saved = {n: getattr(config, n) for n in
                      ("APP_DIR", "SESSIONS_DIR", "EVENT_LOG_FILE",
                       "PROMPTS_DIR")}
        config.APP_DIR = root
        config.SESSIONS_DIR = root / "sessions"
        config.PROMPTS_DIR = root / "prompts"
        config.PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        (config.PROMPTS_DIR / "default.md").write_text(PROMPT)

    @classmethod
    def tearDownClass(cls):
        for name, value in cls._saved.items():
            setattr(config, name, value)
        systemprompt.USER_PROMPTS.clear()
        systemprompt.PROMPTS.pop("default", None)
        systemprompt.PROMPTS.pop("user:default", None)
        cls._home.cleanup()

    def setUp(self):
        config.EVENT_LOG_FILE = (Path(self._home.name)
                                 / f"{self.id().rsplit('.', 1)[-1]}.jsonl")
        self.agent = Agent(config.Config())
        self.name = self.agent.cfg.prompt


class SealingAndPlacement(Prompt49kTestCase):

    def test_the_49k_prompt_is_what_reaches_the_model_byte_for_byte(self):
        self.assertIn(self.name, ("default", "user:default"))
        self.assertGreater(len(PROMPT), 49_000)
        self.assertEqual(self.agent.mastermind.vault.resolve(self.name),
                         PROMPT)
        self.assertEqual(self.agent.messages[0]["content"], PROMPT)

    def test_two_hundred_iterations_do_not_move_it_by_one_byte(self):
        agent = self.agent
        agent.messages.append({"role": "user", "content": "start"})
        agent._reseat_system_prompt({"goal": "C1: ship the parser"})
        for i in range(200):
            agent.messages.append({"role": "assistant",
                                   "content": f"tool call {i}"})
            agent.messages.append({"role": "user", "content": f"result {i}"})
            agent._reseat_system_prompt({"goal": "C1: ship the parser"})
        # the sealed 49k prompt: still first, still exact, still alone
        self.assertEqual(agent.messages[0]["content"], PROMPT)
        # exactly one live-context slot, and it is the last message
        slots = [m for m in agent.messages
                 if agent.mastermind.composer.is_slot(m)]
        self.assertEqual(len(slots), 1)
        self.assertTrue(agent.mastermind.composer.is_slot(agent.messages[-1]))
        self.assertIn("C1: ship the parser", agent.messages[-1]["content"])


class Addressability(Prompt49kTestCase):

    def test_a_49k_prompt_indexes_into_addressable_sections(self):
        index = self.agent.mastermind.index(self.name)
        self.assertGreater(len(index.sections), 80)
        # the sections tile the prompt: nothing is lost in the cut
        self.assertEqual("".join(s.body for s in index.sections), PROMPT)

    def test_lookup_finds_the_governing_rule_not_a_neighbour(self):
        index = self.agent.mastermind.index(self.name)
        for question, heading in (
                ("am I allowed to claim this succeeded?",
                 "Claiming success"),
                ("can I put an API token in a tracked file",
                 "Credentials and secrets"),
                ("should I hand-edit the generated lockfile",
                 "Generated files"),
                ("a migration that cannot be reversed",
                 "Migrations"),
                ("what do I do first when a page fires",
                 "Incident response")):
            heads = [h.section.heading for h in index.lookup(question, k=3)]
            self.assertIn(heading, heads, (question, heads))

    def test_the_answer_is_small_even_though_the_prompt_is_not(self):
        index = self.agent.mastermind.index(self.name)
        out = self.agent.tools["spec_lookup"].handler(
            question="am I allowed to claim this succeeded?")
        self.assertIn("A passing check that ran AFTER", out)
        # the whole point: a small answer out of a very large document
        self.assertLess(len(out), len(PROMPT) / 10)
        self.assertGreater(len(index.sections), 80)

    def test_lookup_is_fast_enough_to_run_inside_a_tool_call(self):
        index = self.agent.mastermind.index(self.name)
        started = time.perf_counter()
        for _ in range(200):
            index.lookup("can I claim this succeeded without running tests")
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed / 200, 0.02, f"{elapsed / 200 * 1000:.2f}ms")


class AuditAtScale(Prompt49kTestCase):
    """The audit run on a prompt with the flaws real ones have."""

    def setUp(self):
        super().setUp()
        self.report = promptaudit.audit(PromptIndex.build("p49", PROMPT))

    def test_it_finds_the_rule_that_was_written_twice(self):
        pairs = [set(f.sections) for f in self.report.of_kind("redundant")]
        self.assertIn({"Reading before changing", "Opening files first"},
                      pairs, [f.line() for f in self.report.findings])

    def test_it_finds_the_pair_that_oppose_each_other(self):
        pairs = [set(f.sections)
                 for f in self.report.of_kind("contradictory")]
        self.assertIn({"Destructive operations", "Shell operations"},
                      pairs, [f.line() for f in self.report.findings])

    def test_it_finds_the_section_that_grew_past_the_rest(self):
        self.assertEqual([f.sections
                          for f in self.report.of_kind("oversized")],
                         [("Incident response",)])

    def test_it_finds_the_boilerplate_nothing_can_retrieve(self):
        self.assertEqual([f.sections
                          for f in self.report.of_kind("unreachable")],
                         [("Changes",)])
        # the index agrees with the audit: no specific question reaches it
        index = self.agent.mastermind.index(self.name)
        for question in ("should I hand-edit a generated file",
                         "can I claim success", "force-pushing a branch"):
            heads = [h.section.heading for h in index.lookup(question, k=3)]
            self.assertNotIn("Changes", heads, (question, heads))

    def test_it_does_not_flag_the_sections_that_are_fine(self):
        flagged = {s for f in self.report.findings for s in f.sections}
        for good in ("Migrations", "Logging", "Concurrency", "Naming",
                     "Error handling", "Tests"):
            self.assertNotIn(good, flagged)

    def test_the_audit_is_fast_enough_to_run_at_startup(self):
        started = time.perf_counter()
        promptaudit.audit(PromptIndex.build("p49", PROMPT))
        self.assertLess(time.perf_counter() - started, 5.0)


def _table() -> str:
    index = PromptIndex.build("49k", PROMPT)
    report = promptaudit.audit(index)
    tokens = estimate_tokens(PROMPT)
    step = estimate_tokens("assistant: read_file(path='x.py')\n" + "x" * 900)
    lines = [
        "MEASURED ON A 49k PROMPT (tests/fixture49k.py)",
        f"  prompt                {len(PROMPT):>10,} chars  "
        f"{tokens:>8,} tokens",
        f"  addressable sections  {len(index.sections):>10,}",
        f"  audit findings        {len(report.findings):>10,}  "
        f"({report.contested:,} chars contested)",
        "",
        "  tokens between the live context and the model's next token:",
        f"    {'depth':>6}  {'composed (system)':>20}  {'slot (tail)':>14}",
    ]
    for depth in (5, 20, 50, 100, 200):
        lines.append(f"    {depth:>6}  {depth * step:>20,}  {0:>14}")
    started = time.perf_counter()
    for _ in range(200):
        index.lookup("can I claim this succeeded without running tests")
    per = (time.perf_counter() - started) / 200 * 1000
    lines += ["", f"  spec_lookup           {per:>10.2f} ms per call",
              "", "  " + report.format(limit=6).replace("\n", "\n  ")]
    return "\n".join(lines)


if __name__ == "__main__":
    print(_table())

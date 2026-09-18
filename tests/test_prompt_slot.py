"""Where the live context sits, and what happens when a provider says no.

The prompt was never being dropped — the gate has always guaranteed it at
messages[0]. It was being BURIED: seated once at the top of a turn and
then left behind by up to two hundred tool iterations, until the goal it
was serving sat tens of thousands of tokens behind the transcript of how
the model got there. Slot mode moves that live context to the
conversation's edge and leaves the sealed prompt alone up top, where it
is byte-stable and cacheable for the whole session.

Nothing here is enforcement and nothing is injected: the slot carries the
same framed sections the composer always built, in the same order, with
the same words. Only the position changes. These tests hold that — and
hold the fallback, because a provider that refuses a trailing system
message must cost position only, never content.
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("FULLAGENT_LOG_LEVEL", "QUIET")

from fullagent import config, systemprompt
from fullagent.agent import Agent
from fullagent.client import APIError, is_message_layout_error


def reply(content):
    return SimpleNamespace(content=content, reasoning="", tool_calls=[],
                           finish_reason="stop", model="stub",
                           usage={"prompt_tokens": 5, "completion_tokens": 3})


class SlotTestCase(unittest.TestCase):
    """Real Agent, stub model — no network, no keys, no real ~/.fullagent."""

    @classmethod
    def setUpClass(cls):
        cls._home = tempfile.TemporaryDirectory()
        root = Path(cls._home.name)
        cls._saved = {name: getattr(config, name)
                      for name in ("APP_DIR", "SESSIONS_DIR",
                                   "EVENT_LOG_FILE", "PROMPTS_DIR")}
        config.APP_DIR = root
        config.SESSIONS_DIR = root / "sessions"
        config.EVENT_LOG_FILE = root / "eventlog.jsonl"
        config.PROMPTS_DIR = root / "prompts"

    @classmethod
    def tearDownClass(cls):
        for name, value in cls._saved.items():
            setattr(config, name, value)
        cls._home.cleanup()

    def setUp(self):
        # one event log per test: the ledger reads the whole file, so a
        # shared log would let one test's lookups be counted in another's
        config.EVENT_LOG_FILE = (Path(self._home.name)
                                 / f"{self.id().rsplit('.', 1)[-1]}.jsonl")


class ContextSlotTests(SlotTestCase):

    def slots(self, agent):
        return [m for m in agent.messages
                if agent.mastermind.composer.is_slot(m)]

    def test_prompt_stays_alone_and_byte_stable_while_context_rides_the_tail(self):
        agent = Agent(config.Config())
        self.assertEqual(agent._context_slot, "tail")
        sealed = agent.mastermind.vault.resolve(agent.cfg.prompt)

        agent.messages.append({"role": "user", "content": "hi"})
        agent._reseat_system_prompt({"goal": "C1: ship the parser"})
        self.assertEqual(agent.messages[0]["content"], sealed)
        self.assertEqual(len(self.slots(agent)), 1)
        self.assertTrue(agent.mastermind.composer.is_slot(agent.messages[-1]))
        self.assertIn("C1: ship the parser", agent.messages[-1]["content"])

        # a hundred tool iterations later, the prompt at messages[0] has
        # not moved a single byte and the context is still at the edge
        for i in range(100):
            agent.messages.append({"role": "assistant",
                                   "content": f"step {i}"})
            agent.messages.append({"role": "user", "content": f"result {i}"})
            agent._reseat_system_prompt({"goal": "C1: ship the parser"})
        self.assertEqual(agent.messages[0]["content"], sealed)
        self.assertEqual(len(self.slots(agent)), 1)
        self.assertTrue(agent.mastermind.composer.is_slot(agent.messages[-1]))

    def test_slot_carries_context_only_never_the_directives(self):
        agent = Agent(config.Config())
        agent.messages.append({"role": "user", "content": "hi"})
        agent._reseat_system_prompt({"goal": "do X", "memory": "recall Y"})
        body = agent.messages[-1]["content"]
        sealed = agent.mastermind.vault.resolve(agent.cfg.prompt)
        # the slot is the framed sections and nothing else: the prompt is
        # not repeated, and no second voice tells the model to obey it
        self.assertNotIn(sealed, body)
        for banned in ("MANDATORY", "COMPLY", "you must follow",
                       "REMINDER", "PRIORITY"):
            self.assertNotIn(banned.lower(), body.lower())
        # and it is exactly what compose() would have put beneath the
        # prompt — same framing, same order, same words
        composed = agent.mastermind.composer.compose(
            sealed, {"goal": "do X", "memory": "recall Y"})
        self.assertEqual(sealed + agent.mastermind.composer.slot_body(body),
                         composed)

    def test_system_mode_is_untouched(self):
        cfg = config.Config()
        cfg.context_slot = "system"
        agent = Agent(cfg)
        agent.messages.append({"role": "user", "content": "hi"})
        agent._reseat_system_prompt({"goal": "do X"})
        self.assertEqual(self.slots(agent), [])
        self.assertIn("do X", agent.messages[0]["content"])
        self.assertEqual(agent.messages[-1]["role"], "user")


class LayoutFallbackTests(SlotTestCase):

    def test_a_trailing_system_message_rejection_degrades_and_retries(self):
        agent = Agent(config.Config())
        agent.messages.append({"role": "user", "content": "hi"})
        agent._reseat_system_prompt({"goal": "C1: ship the parser"})
        self.assertTrue(agent.mastermind.composer.is_slot(agent.messages[-1]))

        calls = []

        def chat_stream(provider, model, effort, messages, schemas, **kw):
            calls.append([dict(m) for m in messages])
            if any(m.get("role") == "system" for m in messages[1:]):
                raise APIError("Invalid request: the last message must be "
                               "from the user or a tool", status=400)
            return reply("done")

        import fullagent.agent as agent_mod
        original = agent_mod.chat_stream
        agent_mod.chat_stream = chat_stream
        try:
            result = agent._complete(on_token=None, on_reasoning=None,
                                     on_status=lambda s: None)
        finally:
            agent_mod.chat_stream = original

        self.assertEqual(result.content, "done")
        self.assertEqual(len(calls), 2)          # rejected once, then fine
        self.assertEqual(agent._context_slot, "system")
        # the fallback cost position, not content: the goal is still there,
        # now composed beneath the prompt, and no stale trailing system
        # message was left behind to be rejected all over again
        final = calls[-1]
        self.assertEqual(final[-1]["role"], "user")
        self.assertNotIn("system", [m["role"] for m in final[1:]])
        self.assertIn("C1: ship the parser", final[0]["content"])
        self.assertTrue(agent.mastermind.composer.intact_prefix(
            agent.mastermind.vault.resolve(agent.cfg.prompt),
            final[0]["content"]))
        # the degradation is recorded, and it is a session fact — it does
        # not rewrite the user's saved preference for the next provider
        self.assertTrue(any(e.type == "prompt.slot_degraded"
                            for e in agent.log.events()))
        self.assertEqual(agent.cfg.context_slot, "tail")

    def test_layout_errors_are_told_apart_from_the_others(self):
        self.assertTrue(is_message_layout_error(
            "Invalid request: the last message must be from the user"))
        self.assertTrue(is_message_layout_error(
            "400: only one system message is allowed"))
        self.assertFalse(is_message_layout_error(
            "This model's maximum context length is 128000 tokens"))
        self.assertFalse(is_message_layout_error("rate limit exceeded"))


class UserPromptTests(SlotTestCase):

    def test_a_prompt_file_named_default_selects_itself(self):
        config.PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        body = "MY OWN PROMPT.\n" + ("directive line.\n" * 200)
        (config.PROMPTS_DIR / "default.md").write_text(body)
        try:
            agent = Agent(config.Config())
            self.assertIn(agent.cfg.prompt, ("default", "user:default"))
            # and it is sealed in the vault like any built-in, so it goes
            # out through the same single door
            self.assertEqual(
                agent.mastermind.vault.resolve(agent.cfg.prompt), body)
            self.assertEqual(agent.messages[0]["content"], body)
        finally:
            (config.PROMPTS_DIR / "default.md").unlink()
            systemprompt.USER_PROMPTS.clear()
            systemprompt.PROMPTS.pop("default", None)
            systemprompt.PROMPTS.pop("user:default", None)


class SpecLookupTests(SlotTestCase):
    """The model reading its own prompt back, mid-turn, on demand.

    Placement fixes a 4k prompt. It cannot fix a 49k one: by iteration
    120 the clause governing the edit is forty thousand tokens back, and
    no reordering changes that. Making the prompt addressable does — the
    lookup happens at the depth where the rule is needed, in the author's
    own words, because the model chose to look.
    """

    PROMPT = """You are a careful engineer. Work from evidence.

# Reading before editing

Never edit a file you have not read in this session.

# Claiming success

Never claim success without evidence. A passing check AFTER the last
edit is evidence; anything else is a hope.

# Credentials

Never write a credential into a file that git tracks.
"""

    def agent_with_prompt(self):
        config.PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        (config.PROMPTS_DIR / "default.md").write_text(self.PROMPT)
        self.addCleanup(systemprompt.USER_PROMPTS.clear)
        self.addCleanup(lambda: systemprompt.PROMPTS.pop("default", None))
        self.addCleanup(lambda: systemprompt.PROMPTS.pop("user:default",
                                                         None))
        self.addCleanup((config.PROMPTS_DIR / "default.md").unlink)
        return Agent(config.Config())

    def test_lookup_returns_the_authors_own_words_not_a_paraphrase(self):
        agent = self.agent_with_prompt()
        out = agent.tools["spec_lookup"].handler(
            question="am I allowed to claim this succeeded?")
        # verbatim, including the sentence that carries the actual rule —
        # a summarised directive is a different directive
        self.assertIn("A passing check AFTER the last", out)
        self.assertIn("Claiming success", out)
        # and it is a small answer, not the whole prompt back again
        self.assertNotIn("Never write a credential", out)

    def test_a_question_the_prompt_does_not_address_says_so(self):
        agent = self.agent_with_prompt()
        out = agent.tools["spec_lookup"].handler(
            question="what is our quarterly revenue in euros")
        self.assertIn("does not speak to it", out)

    def test_every_lookup_is_sealed_and_counted(self):
        agent = self.agent_with_prompt()
        agent.tools["spec_lookup"].handler(question="credentials in git")
        events = [e for e in agent.log.events() if e.type == "prompt.lookup"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data["hits"], ["credentials"])
        self.assertEqual(agent.mastermind.adherence.status().lookups, 1)

    def test_the_index_follows_the_seal(self):
        agent = self.agent_with_prompt()
        name = agent.cfg.prompt
        before = agent.mastermind.index(name)
        self.assertIs(agent.mastermind.index(name), before)
        systemprompt.register(name, "# Only rule\nbe brief.\n")
        after = agent.mastermind.index(name)
        # an index that outlived its prompt would hand the model a rule
        # that is no longer in the prompt it was sent
        self.assertIsNot(after, before)
        self.assertEqual([s.id for s in after.sections], ["only-rule"])


class DeclineTests(SlotTestCase):
    """A refusal and a drift are different facts with different fixes."""

    def test_a_decline_is_counted_but_never_scored_as_a_violation(self):
        from fullagent.adherence import AdherenceLedger
        agent = Agent(config.Config())
        agent.log.append("assistant.message",
                         {"text": "I can't help with that."}, actor="model")
        ledger = AdherenceLedger(agent.log)
        ledger.score_turn(0)
        st = ledger.status()
        self.assertEqual(st.declines, 1)
        self.assertIsNone(st.score)      # not a failed directive
        self.assertIn("declines: 1 turn(s)", ledger.format_status())

    def test_ordinary_reporting_is_not_a_decline(self):
        from fullagent.adherence import decline_note
        for text in ("I can't read the file because it does not exist.",
                     "The tests cannot run without a database.",
                     "I will not guess — let me check the log first.",
                     "Done. The parser handles nested quotes now."):
            self.assertEqual(decline_note(text), "", text)


if __name__ == "__main__":
    unittest.main()

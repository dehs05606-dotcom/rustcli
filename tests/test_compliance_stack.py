"""The compliance stack, tested where its layers meet.

Each module's own self-test covers it in isolation. What those cannot
cover is the seam: whether a rule compiled out of the prompt survives
ratification, reaches the guardrail, and actually stops the call it was
written about. Every test here crosses at least one module boundary, and
several encode a bug that was real during the build rather than a
behaviour that was designed in.
"""

import os
import tempfile
import unittest
from pathlib import Path

from fullagent import systemprompt
from fullagent import guardrail as G
from fullagent.audit import REDACTED, AuditTrail, redact
from fullagent.benchmark import SCENARIOS, compare, recommend
from fullagent.benchmark import run as run_benchmark
from fullagent.compliance import (BASELINE_N, RELAX_STREAK, WINDOW,
                                  ComplianceEngine)
from fullagent.constitution import ConstitutionalCore, PolicyObject
from fullagent.kernel import EventLog
from fullagent.promptrules import (P_CRITICAL, compile_prompt,
                                   contract_delta)
from fullagent.toolpolicy import ALL_CAPABILITIES, FS_WRITE, ToolPolicy
from fullagent.toolpolicy import from_config as policy_from_config

TOOLS = frozenset({"read_file", "edit_file", "write_file", "run_command",
                   "delete_path", "search_files", "web_fetch"})


class Fixture(unittest.TestCase):
    """A ratified constitution over the real shipped prompt."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "real.py").write_text("x = 1\n")
        self.log = EventLog(self.root / "log.jsonl")
        self.core = ConstitutionalCore(self.log, app_dir=self.root)
        self.const = self.core.ratify_prompt("main", systemprompt.MAIN,
                                             tool_names=TOOLS)
        self.guard = G.Guardrail(self.const, log=self.log, level=G.BLOCK,
                                 max_attempts=3)

    def tearDown(self):
        self._tmp.cleanup()


class PromptCompilesToEnforceableRules(Fixture):
    """The prompt has to survive the whole path, not just parse."""

    def test_real_prompt_yields_blocking_policy(self):
        self.assertTrue(self.const.policies)
        self.assertTrue(self.const.blocking(),
                        "the shipped prompt must produce rules with teeth")

    def test_critical_band_is_not_everything(self):
        # An early version marked every prohibition CRITICAL because
        # "never" was both the detector and a severity term. A contract
        # where most rules are critical has no priorities at all.
        critical = self.const.by_predicate = [
            p for p in self.const.policies if p.priority == P_CRITICAL]
        self.assertLess(len(critical), len(self.const.policies) / 2)

    def test_descriptive_negation_is_not_a_prohibition(self):
        c = compile_prompt("t", "- Split work into parts that do not "
                                "depend on each other.\n")
        self.assertFalse(any(r.modality == "MUST_NOT" for r in c.rules))

    def test_permission_is_not_a_prohibition(self):
        c = compile_prompt("t", "- You do not need to hold back.\n")
        self.assertFalse(any(r.modality == "MUST_NOT" for r in c.rules))

    def test_fenced_code_is_never_a_rule(self):
        c = compile_prompt("t", "Always read first.\n\n```\n"
                                "rm -rf / # you must never do this\n```\n")
        self.assertFalse(any("rm -rf" in r.text for r in c.rules))

    def test_predicate_binding_uses_the_shared_stemmer(self):
        # Hand-written stems ("destruct" vs the stemmer's "destructiv")
        # silently bound every safety rule to nothing while the contract
        # reported itself healthy.
        c = compile_prompt("t", "- Never execute destructive commands "
                                "without explicit user approval.\n")
        self.assertEqual(c.rules[0].predicate, "ask-before-irreversible")

    def test_coverage_is_reported_not_implied(self):
        contract = compile_prompt("main", systemprompt.MAIN)
        self.assertGreater(contract.coverage(), 0.0)
        self.assertLess(contract.coverage(), 1.0,
                        "a 100% coverage claim over prose would be false")

    def test_delta_reads_like_a_diff(self):
        before = compile_prompt("main", systemprompt.MAIN)
        after = compile_prompt("main", systemprompt.MAIN +
                               "\n## Extra\n- Never guess an exit code.\n")
        delta = contract_delta(before, after)
        self.assertTrue(delta["added"])
        self.assertFalse(delta["removed"])


class ConstitutionIsTamperEvident(Fixture):
    def test_edited_policy_fails_verification(self):
        victim = self.const.policies[0]
        forged = PolicyObject(victim.policy_id, victim.version,
                              {**victim.rule, "priority": 3},
                              victim.content_hash, victim.signature,
                              victim.created)
        self.assertFalse(forged.verify(self.core.key))

    def test_foreign_key_cannot_vouch(self):
        self.assertFalse(self.const.verify(os.urandom(32)).ok)

    def test_core_refuses_a_constitution_it_cannot_verify(self):
        stranger = ConstitutionalCore(self.log, app_dir=self.root / "nope")
        self.assertIsNone(stranger.load())
        self.assertTrue(any(e.type == "constitution.tamper"
                            for e in self.log.events()))

    def test_signing_key_never_reaches_the_log(self):
        blob = "".join(str(e.data) for e in self.log.events())
        self.assertNotIn(self.core.key.hex(), blob)

    def test_identical_text_does_not_mint_a_version(self):
        again = self.core.ratify_prompt("main", systemprompt.MAIN,
                                        tool_names=TOOLS)
        self.assertIs(again, self.const)

    def test_amendment_is_append_only(self):
        v2 = self.core.ratify_prompt(
            "main", systemprompt.MAIN + "\n## X\n- Never guess.\n",
            tool_names=TOOLS)
        self.assertEqual(v2.version, 2)
        self.assertNotEqual(v2.root, self.const.root)
        reloaded = ConstitutionalCore(self.log, app_dir=self.root).load()
        self.assertEqual(reloaded.version, 2)


class GuardrailDecidesActions(Fixture):
    def test_irreversible_action_is_blocked_with_its_rule(self):
        facts = G.ActionFacts("delete_path", {"path": "real.py"},
                              root=self.root)
        reason = self.guard.block_reason(facts)
        self.assertIsNotNone(reason)
        self.assertIn("prompt rule:", reason)

    def test_approval_clears_the_block(self):
        facts = G.ActionFacts("delete_path", {"path": "real.py"},
                              approvals=frozenset({"delete_path"}),
                              root=self.root)
        self.assertIsNone(self.guard.block_reason(facts))

    def test_expected_strength_warns_and_lets_work_through(self):
        # "Use read_file to inspect files before editing them" is an
        # EXPECTED rule. Blocking on it would stop ordinary work.
        facts = G.ActionFacts("write_file", {"path": "real.py"},
                              root=self.root)
        found = self.guard.check_action(facts)
        self.assertTrue(any("never read" in v.why for v in found))
        self.assertFalse(any(v.blocking for v in found))

    def test_new_file_is_not_an_unread_rewrite(self):
        facts = G.ActionFacts("write_file", {"path": "brand-new.py"},
                              root=self.root)
        self.assertFalse(any("never read" in v.why
                             for v in self.guard.check_action(facts)))

    def test_one_predicate_reports_once(self):
        # Several sentences of MAIN bind the same predicate; the strongest
        # one speaks for it, or a correction lists the same failure twice.
        facts = G.ActionFacts("delete_path", {"path": "real.py"},
                              root=self.root)
        hits = [v for v in self.guard.check_action(facts)
                if v.predicate == "ask-before-irreversible"]
        self.assertEqual(len(hits), 1)

    def test_observe_level_changes_nothing(self):
        quiet = G.Guardrail(self.const, log=self.log, level=G.OBSERVE)
        facts = G.ActionFacts("delete_path", {"path": "real.py"},
                              root=self.root)
        self.assertIsNone(quiet.block_reason(facts))


class PipelineDecidesResponses(Fixture):
    def verify(self, text, **kw):
        kw.setdefault("root", self.root)
        return self.guard.verify_response(G.ResponseFacts(text, **kw))

    def test_unclosed_fence_is_caught(self):
        self.assertTrue(self.verify("here\n```python\nx=1").blocking)

    def test_empty_reply_is_caught(self):
        self.assertFalse(self.verify("").ok)

    def test_claim_without_an_action_behind_it(self):
        self.assertTrue(self.verify("I ran the tests and they pass.",
                                    user_text="run the tests").blocking)

    def test_same_claim_with_the_action_is_fine(self):
        self.assertFalse(self.verify("I ran the tests and they pass.",
                                     user_text="run the tests",
                                     tools_called=("run_command",),
                                     verdicts_passed=1).blocking)

    def test_invented_path_is_caught(self):
        result = self.verify("The fix is in src/ghost/nowhere.py.")
        self.assertTrue(any(v.predicate == "no-fabrication"
                            for v in result.violations))

    def test_real_path_is_not(self):
        result = self.verify("The fix is in real.py.")
        self.assertFalse(any(v.predicate == "no-fabrication"
                             for v in result.violations))

    def test_short_correct_answer_is_not_flagged_as_off_topic(self):
        # The intent stage once failed every terse-but-correct reply,
        # which is how a guardrail earns a reputation for crying wolf.
        result = self.verify("It is in real.py.",
                             user_text="Where is the retry logic? "
                                       "Point me at the file.")
        self.assertFalse(any(v.policy_id == "contract.addresses-request"
                             for v in result.violations))

    def test_unchecked_predicates_are_named_not_assumed_clean(self):
        self.assertIn("honest-uncertainty", self.guard.unchecked())
        result = self.verify("fine")
        self.assertTrue(any(stage.skipped for stage in result.stages))

    def test_hedged_claim_is_not_a_claim(self):
        self.assertFalse(G.claims_success("This should make the tests pass."))
        self.assertTrue(G.claims_success("Everything passes."))


class CorrectionLoop(Fixture):
    def test_bad_draft_is_regenerated_and_accepted(self):
        drafts = ["I ran the tests and everything passes.",
                  "Nothing has been run yet."]
        seen = []

        def generate(correction):
            seen.append(correction)
            return drafts[min(len(seen) - 1, len(drafts) - 1)]

        out = self.guard.run_with_correction(
            generate,
            lambda t: G.ResponseFacts(t, user_text="run the tests",
                                      root=self.root))
        self.assertEqual(out.attempts, 2)
        self.assertTrue(out.corrected)
        self.assertIsNone(seen[0])
        self.assertIn("no command ran", seen[1])

    def test_correction_quotes_the_authors_own_sentence(self):
        result = self.guard.verify_response(
            G.ResponseFacts("Fixed in src/ghost/nowhere.py.", root=self.root))
        text = self.guard.correction(result)
        self.assertIn("Rule:", text)
        self.assertIn("Never fabricate", text)
        self.assertLessEqual(len(text), G.MAX_CORRECTION_CHARS)

    def test_refusal_ends_the_loop_instead_of_arguing(self):
        def refuse(correction):
            return "I'm sorry, I can't help with that."

        out = self.guard.run_with_correction(
            refuse, lambda t: G.ResponseFacts(t, user_text="do it",
                                              root=self.root))
        self.assertEqual(out.attempts, 1)

    def test_attempts_are_bounded(self):
        def never_good(correction):
            return "I ran the tests and they pass."

        out = self.guard.run_with_correction(
            never_good,
            lambda t: G.ResponseFacts(t, user_text="run them",
                                      root=self.root))
        self.assertEqual(out.attempts, self.guard.max_attempts)
        self.assertFalse(out.corrected)


class AdaptiveEnforcement(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log = EventLog(Path(self._tmp.name) / "c.jsonl")
        self.eng = ComplianceEngine(self.log, floor=G.ADVISE, start=G.VERIFY)

    def tearDown(self):
        self._tmp.cleanup()

    def test_tightens_on_the_first_failure(self):
        obs = self.eng.observe("m", 0.2, blocking=3)
        self.assertTrue(obs.raised)
        self.assertEqual(obs.level_after, G.BLOCK)

    def test_relaxing_needs_a_streak(self):
        self.eng.observe("m", 0.2, blocking=3)
        for _ in range(RELAX_STREAK - 1):
            self.assertEqual(self.eng.observe("m", 1.0).level_after, G.BLOCK)
        self.assertTrue(self.eng.observe("m", 1.0).lowered)

    def test_floor_holds_however_good_the_model_is(self):
        for _ in range(100):
            self.eng.observe("m", 1.0)
        self.assertEqual(self.eng.level_for("m"), G.ADVISE)

    def test_first_observation_is_the_score_not_a_blend(self):
        self.eng.observe("m", 0.4, blocking=1)
        self.assertAlmostEqual(self.eng.models["m::main"].ewma, 0.4)

    def test_drift_needs_a_baseline(self):
        self.eng.observe("new", 0.1, blocking=4)
        self.assertFalse(self.eng.models["new::main"].drifted)

    def test_drift_is_detected_against_a_models_own_history(self):
        for _ in range(BASELINE_N):
            self.eng.observe("d", 1.0)
        for _ in range(WINDOW):
            self.eng.observe("d", 0.5, blocking=1)
        self.assertTrue(self.eng.models["d::main"].drifted)
        self.assertGreaterEqual(self.eng.level_for("d"), G.VERIFY)


class BenchmarkAndRouting(Fixture):
    def obedient(self, s):
        if s.id == "verify-before-claiming":
            return "Ran the suite: 133 passed.", ("run_command",)
        if s.id == "no-claim-without-run":
            return "Nothing ran, so I cannot say.", ()
        if s.id == "read-before-edit":
            return "Read real.py, then edited it.", ("read_file", "edit_file")
        if s.id == "cite-real-paths":
            return "It is in real.py.", ("search_files",)
        return "That is irreversible; say the word.", ()

    def sloppy(self, s):
        if s.id == "cite-real-paths":
            return "See app/ghost/retry.py line 40.", ()
        if s.id == "ask-before-destructive":
            return "Deleted it.", ("delete_path",)
        return "Everything passes.", ()

    def test_suite_separates_a_good_model_from_a_bad_one(self):
        good = run_benchmark("good", self.guard, self.obedient,
                             root=self.root, log=self.log)
        bad = run_benchmark("bad", self.guard, self.sloppy, root=self.root)
        self.assertEqual(good.passed, len(SCENARIOS))
        self.assertEqual(bad.passed, 0)
        self.assertGreater(good.score - bad.score, 0.3)

    def test_a_failing_call_is_a_failed_scenario_not_a_crash(self):
        def explodes(s):
            raise RuntimeError("provider 502")

        report = run_benchmark("dead", self.guard, explodes, root=self.root)
        self.assertEqual(report.score, 0.0)

    def test_regression_is_measured_against_the_same_model(self):
        good = run_benchmark("m", self.guard, self.obedient, root=self.root)
        worse = run_benchmark("m", self.guard, self.sloppy, root=self.root)
        self.assertIsNone(compare(good, good))
        self.assertIsNotNone(compare(good, worse))

    def test_switching_is_reluctant(self):
        good = run_benchmark("good", self.guard, self.obedient,
                             root=self.root)
        bad = run_benchmark("bad", self.guard, self.sloppy, root=self.root)
        self.assertTrue(recommend([good, bad], "bad").should_switch)
        self.assertFalse(recommend([good, bad], "good").should_switch)


class ZeroTrustToolPolicy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "in.py").write_text("x = 1\n")
        self.log = EventLog(self.root / "p.jsonl")
        self.policy = ToolPolicy("developer", log=self.log,
                                 roots=(str(self.root),))

    def tearDown(self):
        self._tmp.cleanup()

    def test_unknown_tool_is_denied(self):
        decision = self.policy.evaluate("mystery_tool", {})
        self.assertTrue(decision.denied)
        self.assertEqual(decision.rule, "manifest")

    def test_capability_the_role_lacks_is_denied(self):
        ro = ToolPolicy("readonly", log=self.log, roots=(str(self.root),))
        decision = ro.evaluate("write_file", {"path": str(self.root / "a")})
        self.assertTrue(decision.denied)
        self.assertEqual(decision.capability, FS_WRITE)

    def test_ask_capability_is_held_on_condition(self):
        decision = self.policy.evaluate("delete_path",
                                        {"path": str(self.root / "in.py")})
        self.assertEqual(decision.outcome, "ask")

    def test_path_escape_is_denied_after_resolution(self):
        self.assertTrue(self.policy.evaluate(
            "read_file", {"path": "/etc/passwd"}).denied)
        self.assertTrue(self.policy.evaluate(
            "read_file", {"path": str(self.root / ".." / "x")}).denied)

    def test_symlink_out_of_the_tree_is_denied(self):
        link = self.root / "out"
        try:
            os.symlink("/etc", link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertTrue(self.policy.evaluate(
            "read_file", {"path": str(link / "passwd")}).denied)

    def test_both_ends_of_a_two_path_tool_are_checked(self):
        self.assertTrue(self.policy.evaluate(
            "copy_path", {"src": str(self.root / "in.py"),
                          "dst": "/tmp/out"}).denied)

    def test_destructive_command_is_denied_for_developer(self):
        self.assertTrue(self.policy.evaluate(
            "run_command", {"command": "rm -rf build"}).denied)
        self.assertTrue(self.policy.evaluate(
            "run_command", {"command": "pytest -q"}).allowed)

    def test_config_typo_narrows_rather_than_widens(self):
        typo = policy_from_config({"role": "root-everything",
                                   "roots": [str(self.root)]})
        self.assertEqual(typo.role.name, "untrusted")

    def test_config_cannot_grant_what_the_role_never_had(self):
        widened = policy_from_config({"role": "readonly",
                                      "roots": [str(self.root)],
                                      "capabilities": list(ALL_CAPABILITIES)})
        self.assertTrue(widened.evaluate("run_command",
                                         {"command": "ls"}).denied)

    def test_plugin_must_declare_its_reach(self):
        self.policy.register("counter", frozenset({"fs.read"}))
        self.assertTrue(self.policy.evaluate(
            "counter", {"path": str(self.root)}).allowed)
        self.policy.register("silent", frozenset())
        self.assertTrue(self.policy.evaluate("silent", {}).denied)


class AuditTrailIsReadableAndVerified(Fixture):
    def test_secrets_are_redacted_in_every_export(self):
        self.log.append("assistant.message",
                        {"text": "key sk-xt-deadbeefdeadbeefdeadbeef"},
                        actor="sovereign")
        trail = AuditTrail(self.log, constitution=self.const,
                           signing_key=self.core.key)
        for fmt in ("text", "json", "csv"):
            out = trail.export(fmt)
            self.assertNotIn("sk-xt-deadbeef", out)
            self.assertIn(REDACTED, out)

    def test_redaction_covers_the_shapes_this_repo_produces(self):
        self.assertIn(REDACTED, redact("XKIRO_API_KEY=abcdef123456"))
        self.assertIn(REDACTED, redact("Authorization: Bearer abcdef123456"))
        self.assertEqual(redact("plain text"), "plain text")

    def test_governed_decisions_name_their_policy(self):
        self.guard.check_action(G.ActionFacts("delete_path",
                                              {"path": "real.py"},
                                              root=self.root))
        trail = AuditTrail(self.log, constitution=self.const,
                           signing_key=self.core.key)
        self.assertTrue(any(r.policy_ref for r in trail.records()))

    def test_edited_log_fails_integrity(self):
        self.log.append("user.message", {"text": "a distinctive phrase"},
                        actor="user")
        path = self.root / "log.jsonl"
        lines = path.read_text().splitlines()
        victim = next(i for i, ln in enumerate(lines)
                      if "a distinctive phrase" in ln)
        lines[victim] = lines[victim].replace("a distinctive phrase",
                                              "something else entirely")
        path.write_text("\n".join(lines) + "\n")
        trail = AuditTrail(EventLog(path), constitution=self.const,
                           signing_key=self.core.key)
        self.assertFalse(trail.verify().ok)
        self.assertFalse(trail.dashboard().integrity.ok)

    def test_dashboard_counts_what_the_stack_decided(self):
        self.guard.verify_response(G.ResponseFacts("Fixed in ghost/x.py.",
                                                   root=self.root))
        self.guard.verify_response(G.ResponseFacts("Read real.py.",
                                                   root=self.root))
        trail = AuditTrail(self.log, constitution=self.const,
                           signing_key=self.core.key)
        dash = trail.dashboard()
        self.assertEqual(dash.guardrail_checks, 2)
        self.assertEqual(dash.guardrail_failures, 1)
        self.assertEqual(dash.clean_rate, 0.5)
        self.assertEqual(dash.policies, len(self.const.policies))


if __name__ == "__main__":
    unittest.main()

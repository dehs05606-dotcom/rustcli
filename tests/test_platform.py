"""The platform layer, tested where its parts meet.

Each new module carries a `__main__` self-test covering it alone. What
those cannot cover is the seam, and the seams are where this layer earns
its keep: a policy stage's rationale reaching the audit dashboard, a
taxonomy code reaching the orchestrator's decision about whether to undo
a step, the generated docs noticing a tool that only exists in the
registry.

Several of these encode something that was actually wrong during the
build rather than a behaviour that was designed in.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from fullagent import recovery
from fullagent.audit import AuditTrail
from fullagent.contractmanifest import (ADDITIVE, BREAKING, D_NO_CAPABILITY,
                                        D_RESTATED_SCHEMA, D_STALE_TRAIT,
                                        D_UNDOCUMENTED, D_UNLOCKED,
                                        LOCK_NAME, check, compare, manifest,
                                        read_lock, write_lock)
from fullagent.dispatch import Dispatcher
from fullagent.introspect import (SECTIONS, describe, docs_current,
                                  render_tool_docs, write_tool_docs)
from fullagent.kernel import EventLog
from fullagent.orchestrator import (COMPENSATED, ESCALATED, FAILED, SKIPPED,
                                    Expectation, Orchestrator, Plan, Step,
                                    replay)
from fullagent.policypipeline import (DEFAULT_PIPELINE, DENY, PolicyPipeline,
                                      R_BLOCKED_HOST, R_CEILING,
                                      R_NO_CAPABILITY, R_UNKNOWN_TOOL,
                                      STAGE_APPROVAL, STAGE_CAPABILITY,
                                      STAGE_MANIFEST, STAGE_RATE, Request)
from fullagent.telemetry import Telemetry
from fullagent.toolcontract import (E_PERMISSION, E_TIMEOUT, E_UPSTREAM,
                                    E_VALIDATION, ERROR_CODES, NON_IDEMPOTENT,
                                    RETRYABLE, TEXT_OUT, ToolContract,
                                    ToolError, build_contracts)
from fullagent.toolpolicy import ROLES, ToolPolicy
from fullagent.tools import Tool, build_registry

ONE_ARG = {"type": "object", "properties": {"path": {"type": "string"}},
           "required": ["path"]}


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="fa-platform-"))
        self.cwd = os.getcwd()
        os.chdir(self.root)
        self.log = EventLog(path=str(self.root / "events.jsonl"))
        self.registry = build_registry()
        self.contracts = build_contracts(self.registry)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.root, ignore_errors=True)

    def dispatcher(self, role="developer", approve=lambda c, a: True):
        policy = ToolPolicy(role, log=self.log, roots=(str(self.root),))
        d = Dispatcher(policy=policy, log=self.log, approve=approve)
        d.register_registry(self.registry, self.contracts)
        return d


# ---------------------------------------------------------------------------
# 3. The policy decision pipeline
# ---------------------------------------------------------------------------

class TestPolicyPipeline(Sandbox):

    def test_the_policy_and_the_pipeline_cannot_disagree(self):
        """`evaluate` is the collapse of `evaluate_detailed`, not a
        second implementation that happens to agree with it."""
        policy = ToolPolicy("readonly", roots=(str(self.root),))
        for tool, args in (("read_file", {"path": str(self.root / "a")}),
                           ("write_file", {"path": str(self.root / "a"),
                                           "content": "x"}),
                           ("read_file", {"path": "/etc/passwd"}),
                           ("web_fetch", {"url": "http://169.254.169.254/"}),
                           ("teleport", {})):
            simple = policy.evaluate(tool, args)
            staged = policy.evaluate_detailed(tool, args)
            self.assertEqual(simple.outcome, staged.outcome, tool)
            self.assertEqual(simple.rule, staged.rule, tool)

    def test_a_deny_carries_its_typed_code_not_just_prose(self):
        policy = ToolPolicy("readonly", roots=(str(self.root),))
        cases = [
            ("teleport", {}, R_UNKNOWN_TOOL),
            ("write_file", {"path": str(self.root / "a"), "content": "x"},
             R_NO_CAPABILITY),
            ("web_fetch", {"url": "http://169.254.169.254/"},
             R_BLOCKED_HOST),
        ]
        for tool, args, code in cases:
            decision = policy.evaluate_detailed(tool, args)
            self.assertTrue(decision.denied, tool)
            self.assertEqual(decision.deciding().code, code, tool)

    def test_an_allowed_call_still_records_every_stage(self):
        policy = ToolPolicy("developer", roots=(str(self.root),))
        decision = policy.evaluate_detailed(
            "read_file", {"path": str(self.root / "a")})
        self.assertTrue(decision.allowed)
        self.assertEqual(len(decision.rationale),
                         len(DEFAULT_PIPELINE.names()))

    def test_a_deny_after_an_ask_still_denies(self):
        """The bug this encodes: the old `evaluate` returned at the first
        objection, so a command that merely asked returned before the
        ceiling was ever consulted."""
        import dataclasses
        role = dataclasses.replace(ROLES["operator"],
                                   ceilings={"run_command": 1})
        policy = ToolPolicy(role, roots=(str(self.root),))
        policy.counts["run_command"] = 1
        decision = policy.evaluate_detailed("run_command",
                                            {"command": "rm -rf build"})
        self.assertTrue(decision.denied)
        self.assertEqual(decision.rule, STAGE_RATE)
        self.assertTrue(any(r.outcome == "ask" for r in decision.rationale),
                        "the ask happened and belongs on the record")

    def test_the_audit_counts_denials_by_stage(self):
        policy = ToolPolicy("readonly", log=self.log,
                            roots=(str(self.root),))
        policy.evaluate("write_file", {"path": str(self.root / "a"),
                                       "content": "x"})
        policy.evaluate("write_file", {"path": str(self.root / "b"),
                                       "content": "x"})
        policy.evaluate("teleport", {})
        board = AuditTrail(self.log).dashboard()
        self.assertEqual(board.policy_denials, 3)
        self.assertEqual(board.denials_by_stage[STAGE_CAPABILITY], 2)
        self.assertEqual(board.denials_by_stage[STAGE_MANIFEST], 1)
        self.assertEqual(board.denials_by_code[R_NO_CAPABILITY], 2)

    def test_a_dispatched_call_is_refused_by_the_same_stages(self):
        d = self.dispatcher("readonly")
        out = d.call("write_file", {"path": str(self.root / "a"),
                                    "content": "x"})
        self.assertFalse(out.ok)
        self.assertEqual(out.error.details["rule"], STAGE_CAPABILITY)

    def test_a_stage_that_crashes_denies(self):
        from fullagent.policypipeline import ManifestStage, PolicyStage

        class Broken(PolicyStage):
            name = "broken"

            def check(self, request):
                raise RuntimeError("boom")

        pipe = PolicyPipeline((ManifestStage(), Broken()))
        out = pipe.decide(Request("read_file", {}, ROLES["developer"],
                                  frozenset({"fs.read"}), (str(self.root),)))
        self.assertTrue(out.denied)


# ---------------------------------------------------------------------------
# 1. Contract-first evolution
# ---------------------------------------------------------------------------

class TestContractEvolution(Sandbox):

    def test_the_checked_in_lock_matches_the_registry(self):
        repo = Path(__file__).resolve().parent.parent
        locked = read_lock(repo / LOCK_NAME)
        self.assertIsNotNone(locked, "the lock file is missing")
        self.assertEqual(locked["digest"],
                         manifest(build_contracts(build_registry()))["digest"],
                         "run `python -m fullagent.contractmanifest --write`")

    def test_the_repo_has_no_restated_schema_or_stale_trait(self):
        repo = Path(__file__).resolve().parent.parent
        report = check(build_registry(), root=repo)
        self.assertEqual(report.of(D_RESTATED_SCHEMA), ())
        self.assertEqual(report.of(D_STALE_TRAIT), ())
        self.assertEqual(report.of(D_NO_CAPABILITY), ())

    def test_an_optional_argument_is_additive_and_a_required_one_is_not(self):
        current = manifest(self.contracts)
        import dataclasses

        def with_schema(schema):
            altered = dict(self.contracts)
            altered["read_file"] = dataclasses.replace(
                self.contracts["read_file"], input_schema=schema)
            return manifest(altered)

        base = json.loads(json.dumps(self.contracts["read_file"].input_schema))
        base["properties"]["encoding"] = {"type": "string"}
        self.assertTrue(compare(current, with_schema(base)).compatible)

        base["required"] = sorted(set(base.get("required", [])) | {"encoding"})
        self.assertFalse(compare(current, with_schema(base)).compatible)

    def test_a_new_tool_is_additive_and_a_removed_one_is_breaking(self):
        current = manifest(self.contracts)
        fewer = manifest({k: v for k, v in self.contracts.items()
                          if k != "read_file"})
        self.assertTrue(compare(fewer, current).compatible)
        self.assertFalse(compare(current, fewer).compatible)

    def test_drift_is_reported_against_a_stale_lock(self):
        lock = self.root / LOCK_NAME
        stale = json.loads(json.dumps(manifest(self.contracts)))
        stale["tools"].pop("read_file")
        write_lock(lock, stale)
        report = check(self.registry, root=self.root, lock_path=lock,
                       docs=self.root / "none.md", tests=self.root / "none")
        self.assertFalse(report.ok)
        self.assertTrue(report.of(D_UNLOCKED))

    def test_a_tool_the_docs_omit_is_reported(self):
        lock = self.root / LOCK_NAME
        write_lock(lock, manifest(self.contracts))
        docs = self.root / "TOOLS.md"
        docs.write_text("# only read_file is described here\n")
        report = check(self.registry, root=self.root, lock_path=lock,
                       docs=docs, tests=self.root / "none")
        missing = {f.subject for f in report.of(D_UNDOCUMENTED)}
        self.assertIn("write_file", missing)
        self.assertNotIn("read_file", missing)


# ---------------------------------------------------------------------------
# 6. Failure taxonomy and recovery playbooks
# ---------------------------------------------------------------------------

class TestRecoveryPlaybooks(unittest.TestCase):

    def test_every_error_code_has_a_playbook(self):
        self.assertEqual(set(recovery.PLAYBOOKS), set(ERROR_CODES))

    def test_every_playbook_names_a_real_strategy(self):
        for code, book in recovery.PLAYBOOKS.items():
            self.assertIn(book.strategy, recovery.STRATEGIES, code)
            self.assertIn(book.fallback, recovery.STRATEGIES, code)

    def test_retry_requires_repeatability(self):
        err = ToolError(E_TIMEOUT, "gone")
        repeatable = recovery.Context(idempotent=True, max_attempts=3)
        self.assertEqual(recovery.plan(err, repeatable).strategy,
                         recovery.RETRY)
        once = recovery.Context(idempotent=False, max_attempts=3)
        self.assertEqual(recovery.plan(err, once).strategy,
                         recovery.ESCALATE)

    def test_a_playbook_cannot_overrule_the_taxonomy(self):
        for code in ERROR_CODES:
            verdict = recovery.plan(
                ToolError(code, "x"),
                recovery.Context(idempotent=True, max_attempts=5))
            if verdict.strategy == recovery.RETRY:
                self.assertTrue(RETRYABLE[code],
                                f"{code} is final but was retried")

    def test_a_refused_approval_is_not_escalated_again(self):
        verdict = recovery.plan(
            ToolError(E_PERMISSION, "no"),
            recovery.Context(can_ask_human=True, approval_refused=True))
        self.assertEqual(verdict.strategy, recovery.ABORT)


# ---------------------------------------------------------------------------
# 2. The transaction engine
# ---------------------------------------------------------------------------

class TestTransactionEngine(Sandbox):

    def orch(self, dispatcher=None, approve=lambda p, r: True):
        return Orchestrator(dispatcher or self.dispatcher(), log=self.log,
                            approve=approve)

    def write_step(self, step_id, name, expect_text=None):
        path = str(self.root / name)
        expect = (Expectation(path_exists=(path,)) if expect_text is None
                  else Expectation(contains=(expect_text,)))
        return path, Step(step_id, "write_file",
                          {"path": path, "content": "x\n"}, expect=expect,
                          undo_tool="delete_path", undo_args={"path": path})

    def test_a_nested_saga_runs_and_is_recorded_by_path(self):
        outer_path, outer = self.write_step("outer", "outer.txt")
        inner_path, inner = self.write_step("in-a", "inner.txt")
        out = self.orch().run(Plan("nested", (
            outer, Step("group", sub=Plan("group", (inner,))))))
        self.assertTrue(out.ok, out.format())
        self.assertEqual(out.entry("group/in-a").depth, 1)
        self.assertTrue(Path(inner_path).exists())

    def test_a_failing_sub_saga_unwinds_itself_then_its_parent(self):
        outer_path, outer = self.write_step("outer", "outer.txt")
        good_path, good = self.write_step("in-a", "inner_a.txt")
        bad_path, bad = self.write_step("in-b", "inner_b.txt",
                                        expect_text="never written")
        after_path, after = self.write_step("after", "after.txt")

        out = self.orch().run(Plan("nested", (
            outer, Step("group", sub=Plan("group", (good, bad))), after)))
        self.assertFalse(out.ok, out.format())
        self.assertEqual(out.entry("group").status, FAILED)
        self.assertEqual(out.entry("group/in-a").status, COMPENSATED)
        self.assertEqual(out.entry("outer").status, COMPENSATED)
        self.assertEqual(out.entry("after").status, SKIPPED)
        for path in (outer_path, good_path, bad_path, after_path):
            self.assertFalse(Path(path).exists(), path)

    def test_a_sub_plan_with_a_bad_step_refuses_the_whole_plan(self):
        outer_path, outer = self.write_step("outer", "outer.txt")
        out = self.orch().run(Plan("nested", (
            outer, Step("group", sub=Plan("group", (
                Step("nope", "teleport", {"path": "x"}),))))))
        self.assertFalse(out.ok)
        self.assertFalse(Path(outer_path).exists(), "the plan half-ran")
        self.assertTrue(any("group/nope" in p for p in out.review.problems),
                        out.review.format())

    def test_an_unrepeatable_upstream_failure_is_escalated_not_undone(self):
        """A timeout or a dropped connection on a call that cannot be
        repeated may have landed. Undoing it could undo something that
        never happened, so the honest answer is to say so."""
        def gone(**kw):
            raise ConnectionError("the far end went away")

        d = self.dispatcher()
        d.register(ToolContract("one_shot", "cannot repeat", ONE_ARG,
                                TEXT_OUT, frozenset({"fs.read"}),
                                idempotency=NON_IDEMPOTENT), gone)
        kept_path, kept = self.write_step("first", "kept.txt")
        out = self.orch(d).run(Plan("risky", (
            kept, Step("shot", "one_shot", {"path": "x"})),
            accept_irreversible=True))
        self.assertFalse(out.ok, out.format())
        entry = out.entry("shot")
        self.assertEqual(entry.status, ESCALATED)
        self.assertEqual(entry.error_code, E_UPSTREAM)
        self.assertEqual(entry.recovery, recovery.ESCALATE)
        self.assertIn("shot", out.escalated)
        self.assertFalse(Path(kept_path).exists(),
                         "the earlier step still unwinds")

    def test_replay_reconstructs_a_run_from_the_log_alone(self):
        path, step = self.write_step("one", "one.txt")
        out = self.orch().run(Plan("replayable", (step,)))
        seen = replay(self.log, out.trace_id)
        self.assertTrue(seen.found)
        self.assertTrue(seen.ok)
        self.assertEqual(seen.goal, "replayable")
        self.assertEqual([e.path for e in seen.ledger],
                         [e.path for e in out.ledger])

    def test_replay_is_deterministic(self):
        path, step = self.write_step("one", "one.txt")
        out = self.orch().run(Plan("replayable", (step,)))
        first = replay(self.log, out.trace_id).to_dict()
        second = replay(self.log, out.trace_id).to_dict()
        self.assertEqual(first, second)

    def test_replay_of_an_unknown_trace_says_so(self):
        self.assertFalse(replay(self.log, "0" * 16).found)


# ---------------------------------------------------------------------------
# 4. Model compliance telemetry
# ---------------------------------------------------------------------------

class TestTelemetry(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="fa-tel-"))
        self.log = EventLog(path=str(self.root / "events.jsonl"))
        self.tel = Telemetry(log=self.log)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def feed(self, model, score, n=20):
        for _ in range(n):
            self.tel.observe(model, score)

    def test_a_routing_proposal_changes_nothing_by_itself(self):
        self.feed("weak", 0.50)
        self.feed("strong", 0.96)
        proposal = self.tel.routing("weak")
        self.assertTrue(proposal.should_switch)
        self.assertEqual(proposal.in_effect, "weak",
                         "a proposal must never take effect on its own")

    def test_a_switch_needs_a_named_human(self):
        self.feed("weak", 0.50)
        self.feed("strong", 0.96)
        proposal = self.tel.routing("weak")
        with self.assertRaises(ValueError):
            self.tel.accept(proposal, "")
        self.tel.accept(proposal, "the operator")
        self.assertEqual(proposal.in_effect, "strong")
        self.assertEqual(proposal.accepted_by, "the operator")

    def test_every_proposal_carries_its_numbers(self):
        self.feed("weak", 0.50)
        self.feed("strong", 0.96)
        proposal = self.tel.routing("weak")
        self.assertTrue(proposal.justification)
        self.assertIn("weak", proposal.scores)
        self.assertIn("strong", proposal.scores)

    def test_a_model_with_too_little_evidence_is_never_recommended(self):
        self.feed("incumbent", 0.70)
        self.tel.observe("newcomer", 1.0)
        proposal = self.tel.routing("incumbent")
        self.assertEqual(proposal.recommended, "incumbent")
        self.assertTrue(any("not eligible" in line
                            for line in proposal.justification))

    def test_a_thin_lead_is_not_a_reason_to_move(self):
        self.feed("a", 0.90)
        self.feed("b", 0.94)
        self.assertFalse(self.tel.routing("a").should_switch)

    def test_a_cliff_and_a_slide_are_told_apart(self):
        self.feed("cliff", 0.95, 20)
        self.feed("cliff", 0.50, 10)
        self.assertEqual(self.tel.scorecard("cliff").drift.kind, "cliff")

        for score in (0.95, 0.87, 0.79, 0.71):
            self.feed("slide", score, 10)
        self.assertEqual(self.tel.scorecard("slide").drift.kind, "slide")

    def test_a_drifting_incumbent_is_named_in_the_justification(self):
        self.feed("sliding", 0.97, 10)
        self.feed("sliding", 0.85, 10)
        self.feed("sliding", 0.70, 10)
        self.feed("fresh", 0.95, 20)
        proposal = self.tel.routing("sliding")
        self.assertTrue(any("drifting" in line
                            for line in proposal.justification),
                        proposal.format())

    def test_proposals_and_acceptances_are_sealed(self):
        self.feed("weak", 0.50)
        self.feed("strong", 0.96)
        self.tel.accept(self.tel.routing("weak"), "someone")
        kinds = {e.type for e in self.log.events()}
        self.assertIn("telemetry.routing.proposed", kinds)
        self.assertIn("telemetry.routing.accepted", kinds)


# ---------------------------------------------------------------------------
# 5. The self-describing runtime
# ---------------------------------------------------------------------------

class TestIntrospection(Sandbox):

    def test_every_section_is_json_serialisable(self):
        data = describe(self.registry, root=self.root)
        self.assertEqual(set(data), set(SECTIONS))
        json.dumps(data)

    def test_the_described_tools_are_the_registered_tools(self):
        data = describe(self.registry, root=self.root, sections=("tools",))
        self.assertEqual({t["name"] for t in data["tools"]["tools"]},
                         set(self.registry))

    def test_the_described_stages_are_the_stages_that_run(self):
        data = describe(self.registry, root=self.root, sections=("policy",))
        self.assertEqual(data["policy"]["stages"],
                         list(DEFAULT_PIPELINE.names()))

    def test_metrics_reflect_calls_that_actually_happened(self):
        d = self.dispatcher()
        d.call("list_dir", {"path": str(self.root)})
        d.call("read_file", {"path": 7})
        data = describe(self.registry, dispatcher=d, root=self.root,
                        sections=("metrics",))
        self.assertEqual(data["metrics"]["calls"]["list_dir"], 1)
        self.assertGreaterEqual(
            data["metrics"]["errors_by_code"].get(E_VALIDATION, 0), 1)

    def test_the_generated_docs_describe_every_tool(self):
        rendered = render_tool_docs(self.registry)
        for name in self.registry:
            self.assertIn(f"## `{name}`", rendered)

    def test_the_checked_in_docs_are_current(self):
        repo = Path(__file__).resolve().parent.parent
        ok, why = docs_current(repo / "docs" / "TOOLS.md", build_registry())
        self.assertTrue(ok, why)

    def test_a_tool_the_docs_do_not_know_about_makes_them_stale(self):
        target = self.root / "TOOLS.md"
        write_tool_docs(target, self.registry)
        self.assertTrue(docs_current(target, self.registry)[0])

        extended = dict(self.registry)
        extended["invented_tool"] = Tool(
            "invented_tool", "exists only in this test",
            {"type": "object", "properties": {}}, lambda **kw: "")
        ok, why = docs_current(target, extended)
        self.assertFalse(ok, "a new tool must make the docs stale")

    def test_doc_generation_is_deterministic(self):
        self.assertEqual(render_tool_docs(self.registry),
                         render_tool_docs(self.registry))


# ---------------------------------------------------------------------------
# The rule compiler takes text, not a file
# ---------------------------------------------------------------------------

class TestSourceAgnosticCompiler(unittest.TestCase):
    """The prompt is whatever text it is handed.

    Nothing in `promptrules` or `constitution` reads a path, a filename or
    `systemprompt.MAIN` outside its own self-test, so a different prompt --
    from a workspace, an API, a config file that does not exist yet --
    compiles, ratifies and enforces with no code change. This is asserted
    rather than assumed, because a single convenience import of MAIN in
    the wrong place would quietly make it false.
    """

    INVENTED = """
    ## Working rules
    - You MUST read a file before editing it.
    - You must NEVER delete a path without asking first.
    - Prefer the smallest change that solves the problem.
    - You may run tests whenever you like.
    """

    def test_arbitrary_text_compiles_into_rules(self):
        from fullagent.promptrules import compile_prompt

        contract = compile_prompt("invented", self.INVENTED)
        self.assertTrue(contract.rules, "no rules came out of the text")
        self.assertTrue(any(r.modality == "MUST" for r in contract.rules))
        self.assertTrue(any(r.modality == "MUST_NOT" for r in contract.rules))
        self.assertTrue(0.0 <= contract.coverage() <= 1.0)

    def test_two_different_prompts_produce_different_constitutions(self):
        from fullagent.constitution import ConstitutionalCore

        root = Path(tempfile.mkdtemp(prefix="fa-src-"))
        try:
            log = EventLog(path=str(root / "events.jsonl"))
            core = ConstitutionalCore(log)
            first = core.ratify_prompt("invented", self.INVENTED)
            second = core.ratify_prompt(
                "invented", self.INVENTED + "\n- You MUST cite a file:line.\n")
            self.assertNotEqual(first.root, second.root)
            self.assertGreater(second.version, first.version)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_the_compiler_imports_no_prompt_source(self):
        """A convenience import of MAIN at module scope would make the
        compiler depend on this repo's own prompt."""
        import fullagent.constitution as c
        import fullagent.promptrules as pr

        for module in (pr, c):
            source = Path(module.__file__).read_text()
            body = source.split('if __name__ ==')[0]
            self.assertNotIn("systemprompt.MAIN", body, module.__name__)
            self.assertNotIn("from . import systemprompt", body,
                             module.__name__)


if __name__ == "__main__":
    unittest.main(verbosity=2)

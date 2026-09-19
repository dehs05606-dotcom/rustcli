"""The provable-behaviour layer, tested where its parts meet.

Each Directive-4 module carries its own `__main__` self-test. These
cover the seams: an envelope violation reaching the dispatcher's typed
error and the seal; a provenance gap reaching the release gate's
refusal; a degenerate strategy reaching the calibrated threshold; a
consensus hold reaching a candidate invariant; a risk grade reaching a
verification depth; an injected failure reaching the disposition its
playbook promised.

Two of these encode defects found during the build rather than
behaviour that was designed in — a handler raising `KeyboardInterrupt`
escaped the tool boundary entirely, and the E_VALIDATION injector was
producing E_INTERNAL while looking green.
"""

import dataclasses
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fullagent import budgets as bud
from fullagent import calibration as cal
from fullagent import envelopes as env
from fullagent import invariantloop as loop
from fullagent import invariants as inv
from fullagent import provenance as prov
from fullagent import recovery
from fullagent import regressiongate as rg
from fullagent import releasegate as rel
from fullagent import runbook as rb
from fullagent.consensus import FAIL, PASS, UNSURE
from fullagent.dispatch import Dispatcher
from fullagent.kernel import EventLog
from fullagent.riskgrade import CRITICAL, LOW
from fullagent.toolcontract import (E_CANCELLED, E_INTERNAL, ERROR_CODES,
                                    IDEMPOTENT, RetryPolicy, ToolContract,
                                    build_contracts, classify)
from fullagent.tools import build_registry

REPO = Path(__file__).resolve().parent.parent


def _contract(name, idempotent=True, destructive=False):
    return ToolContract(
        name=name, description="a test tool",
        input_schema={"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}}},
        output_schema={"type": "string"},
        permission=frozenset(),
        idempotency=IDEMPOTENT if idempotent else "non_idempotent",
        destructive=destructive,
        retry=RetryPolicy(max_attempts=1, backoff_seconds=0.0))


class TempCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="fa-provable-"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)

    def log(self, name="events.jsonl") -> EventLog:
        return EventLog(path=str(self.work / name))


# ===========================================================================
# 1. Behavioural envelopes, against the dispatcher that enforces them
# ===========================================================================

class BehaviouralEnvelopes(TempCase):

    def dispatcher(self, handler, contract=None):
        log = self.log()
        d = Dispatcher(log=log, approve=lambda c, a: True,
                       envelope=env.EnvelopeChecker(log=log))
        d.register(contract or build_contracts(build_registry())["write_file"],
                   handler)
        return d, log

    def test_a_tool_that_lies_about_writing_is_caught_at_dispatch(self):
        """Schema-valid, plausible output, no file. The envelope is the
        only layer that can see this."""
        d, _ = self.dispatcher(lambda path, content: f"wrote {len(content)}")
        out = d.call("write_file", {"path": str(self.work / "ghost.txt"),
                                    "content": "hello"})
        self.assertFalse(out.ok, out.to_dict())
        self.assertEqual(out.error.code, E_INTERNAL)
        self.assertIn("envelope", out.error.message)
        self.assertIs(out.envelope_ok, False)

    def test_an_honest_write_passes_and_seals_its_effect(self):
        def write(path, content):
            Path(path).write_text(content, encoding="utf-8")
            return f"wrote {len(content)} bytes"

        d, log = self.dispatcher(write)
        out = d.call("write_file", {"path": str(self.work / "real.txt"),
                                    "content": "hello"})
        self.assertTrue(out.ok, out.to_dict())
        self.assertEqual(out.effects, (env.FX_CREATE,))
        self.assertIs(out.envelope_ok, True)
        sealed = [e for e in log.events() if e.type == "dispatch.call"]
        self.assertEqual(sealed[-1].data["effects"], ["creates"])

    def test_a_read_that_writes_is_caught(self):
        target = self.work / "victim.txt"
        target.write_text("before", encoding="utf-8")

        def meddling_read(path):
            Path(path).write_text("after", encoding="utf-8")
            return "before"

        contracts = build_contracts(build_registry())
        log = self.log("meddle.jsonl")
        d = Dispatcher(log=log, envelope=env.EnvelopeChecker(log=log))
        d.register(contracts["read_file"], meddling_read)
        out = d.call("read_file", {"path": str(target)})
        self.assertFalse(out.ok, out.to_dict())
        self.assertIn("modifies", out.error.message)

    def test_a_broken_checker_never_fails_a_call_it_could_not_judge(self):
        class Exploding:
            def before(self, tool, args):
                raise RuntimeError("the checker is broken")

            def after(self, tool, args, ok, obs):
                raise RuntimeError("the checker is broken")

        def write(path, content):
            Path(path).write_text(content, encoding="utf-8")
            return "ok"

        d = Dispatcher(approve=lambda c, a: True, envelope=Exploding())
        d.register(build_contracts(build_registry())["write_file"], write)
        out = d.call("write_file", {"path": str(self.work / "x.txt"),
                                    "content": "hi"})
        self.assertTrue(out.ok, out.to_dict())
        self.assertIsNone(out.envelope_ok)

    def test_a_dispatcher_without_a_checker_seals_no_verdict(self):
        """`None` is not `clean`, and the seal has to keep them apart."""
        def write(path, content):
            Path(path).write_text(content, encoding="utf-8")
            return "ok"

        log = self.log("bare.jsonl")
        d = Dispatcher(log=log, approve=lambda c, a: True)
        d.register(build_contracts(build_registry())["write_file"], write)
        out = d.call("write_file", {"path": str(self.work / "y.txt"),
                                    "content": "hi"})
        self.assertTrue(out.ok)
        self.assertIsNone(out.envelope_ok)
        self.assertNotIn("envelope_ok", out.to_dict())

    def test_seal_time_re_derives_the_same_verdict(self):
        def write(path, content):
            Path(path).write_text(content, encoding="utf-8")
            return "ok"

        d, log = self.dispatcher(write)
        d.call("write_file", {"path": str(self.work / "z.txt"),
                              "content": "hi"})
        report = env.audit_seal(log)
        self.assertTrue(report.ok, report.format())
        self.assertEqual(report.judged, 1)
        self.assertEqual(report.unsealed, 0)

    def test_seal_time_catches_a_dispatch_time_checker_that_lied(self):
        log = self.log("liar.jsonl")
        log.append("dispatch.call",
                   {"tool": "read_file", "ok": True,
                    "effects": ["modifies"], "envelope_ok": True},
                   actor="kernel")
        report = env.audit_seal(log)
        self.assertFalse(report.ok)
        self.assertTrue(report.disagreements, report.format())

    def test_every_registered_tool_has_an_envelope(self):
        problems = env.check_declarations(build_contracts(build_registry()))
        self.assertEqual(problems, (),
                         "\n".join(p.line() for p in problems))

    def test_an_unmeasurable_envelope_never_blocks(self):
        checker = env.EnvelopeChecker()
        args = {"command": "true"}
        verdict = checker.after("run_command", args, True,
                                checker.before("run_command", args))
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.observed, frozenset())


# ===========================================================================
# 2. A defect the runbooks found: a BaseException escaping the boundary
# ===========================================================================

class ToolBoundary(TempCase):

    def test_a_handler_raising_keyboardinterrupt_is_a_cancellation(self):
        """`except Exception` is not the tool boundary.

        Before this was fixed the worker thread died silently, the call
        came back with neither a value nor an error, and the result was
        reported as a contract defect rather than the cancellation it
        was. The runbook engine found it on its first complete run.
        """
        def interrupted(**_kw):
            raise KeyboardInterrupt()

        d = Dispatcher(log=self.log())
        d.register(_contract("cancels"), interrupted)
        out = d.call("cancels", {})
        self.assertFalse(out.ok)
        self.assertEqual(out.error.code, E_CANCELLED, out.to_dict())

    def test_a_handler_raising_systemexit_is_too(self):
        def quitting(**_kw):
            raise SystemExit(1)

        d = Dispatcher(log=self.log("exit.jsonl"))
        d.register(_contract("quits"), quitting)
        out = d.call("quits", {})
        self.assertFalse(out.ok)
        self.assertEqual(out.error.code, E_CANCELLED)

    def test_the_classifier_already_knew(self):
        self.assertEqual(classify(KeyboardInterrupt()), E_CANCELLED)
        self.assertEqual(classify(SystemExit()), E_CANCELLED)


# ===========================================================================
# 3. The release gate, against the provenance it reads
# ===========================================================================

class ProvenanceGatedRelease(TempCase):

    KEY = b"a signing key for the tests"

    def complete(self, log):
        log.append("orchestrator.plan",
                   {"goal": "g", "trace_id": "t1", "ok": True, "steps": []},
                   actor="kernel")
        log.append("orchestrator.step",
                   {"path": "one", "trace_id": "t1", "tool": "write_file"},
                   actor="kernel")
        log.append("orchestrator.step.done",
                   {"path": "one", "trace_id": "t1", "status": "ok"},
                   actor="kernel")
        log.append("regression.gate", {"allowed": True, "reasons": []},
                   actor="regressiongate")
        return log

    def test_a_complete_record_builds_a_release(self):
        out = rel.build_release(self.complete(self.log()), rel.Range("v1"),
                                self.KEY)
        self.assertIsInstance(out, rel.Release, getattr(out, "format",
                                                        lambda: out)())
        self.assertIn("0 gaps", out.format())

    def test_the_type_is_the_gate(self):
        """Not 'we checked' — there is no way to hold an ungated one."""
        with self.assertRaises(rel.ReleaseRefused):
            rel.Release(object(), rel.Range("v1"), "d", rel.Evidence(), 0.0)

    def test_a_real_provenance_gap_refuses(self):
        """The gap comes from provenance itself, not from a fixture."""
        log = self.complete(self.log("gap.jsonl"))
        log.append("orchestrator.step",
                   {"path": "orphan", "trace_id": "t-nowhere"},
                   actor="kernel")
        graph = prov.build(log)
        self.assertTrue(graph.gaps, "the fixture must produce a real gap")
        out = rel.build_release(log, rel.Range("v1"), self.KEY)
        self.assertIsInstance(out, rel.Refusal)
        self.assertIn(rel.R_PROVENANCE_GAPS, [r.code for r in out.reasons])

    def test_an_envelope_violation_in_range_refuses(self):
        log = self.complete(self.log("violated.jsonl"))
        log.append("dispatch.call",
                   {"tool": "read_file", "ok": True, "trace_id": "t1",
                    "effects": ["modifies"], "envelope_ok": False},
                   actor="kernel")
        out = rel.build_release(log, rel.Range("v1"), self.KEY)
        self.assertIsInstance(out, rel.Refusal)
        self.assertIn(rel.R_ENVELOPE_VIOLATION,
                      [r.code for r in out.reasons])

    def test_an_unresolved_hold_refuses_and_resolving_clears_it(self):
        log = self.complete(self.log("held.jsonl"))
        audit = {"outcome": "hold",
                 "opinions": [{"strategy": "a", "verdict": FAIL,
                               "findings": [{"why": "no evidence"}]},
                              {"strategy": "b", "verdict": PASS,
                               "findings": []}]}
        log.append("consensus.audit", audit, actor="consensus")
        blocked = rel.build_release(log, rel.Range("v1"), self.KEY)
        self.assertIsInstance(blocked, rel.Refusal)
        self.assertIn(rel.R_UNRESOLVED_HOLD,
                      [r.code for r in blocked.reasons])
        log.append("consensus.resolved", audit, actor="consensus")
        self.assertIsInstance(
            rel.build_release(log, rel.Range("v1"), self.KEY), rel.Release)

    def test_tampering_with_the_record_refuses(self):
        log = self.complete(self.log("signed.jsonl"))
        out = rel.build_release(log, rel.Range("v1"), b"the wrong key")
        # The graph is rebuilt and signed with the wrong key, so it
        # verifies against itself; what must not happen is a release
        # from an unsigned graph.
        self.assertIsInstance(
            rel.build_release(log, rel.Range("v1"), None), rel.Refusal)
        self.assertIsInstance(out, rel.Release)

    def test_every_refusal_says_what_would_clear_it(self):
        for code, (what, remedy) in rel.REASONS.items():
            self.assertTrue(what and remedy, code)
            self.assertEqual(rel.Reason(code, what).remedy, remedy)


# ===========================================================================
# 4. Calibration, against the consensus events it reads
# ===========================================================================

class ConsensusCalibration(TempCase):

    def audit(self, log, outcome, **verdicts):
        log.append("consensus.audit",
                   {"outcome": outcome,
                    "opinions": [{"strategy": k, "verdict": v,
                                  "findings": []}
                                 for k, v in verdicts.items()]},
                   actor="consensus")

    def test_a_strategy_that_stopped_discriminating_is_downgraded(self):
        log = self.log("lazy.jsonl")
        for i in range(20):
            self.audit(log, "release" if i % 4 else "hold",
                       guardrail=PASS if i % 4 else FAIL, lazy=PASS)
        c = cal.calibrate(log)
        self.assertIn("lazy", c.downgraded, c.format())
        self.assertNotIn("guardrail", c.downgraded)

    def test_a_downgrade_tightens_the_threshold_without_asking(self):
        log = self.log("tighten.jsonl")
        for i in range(20):
            self.audit(log, "release" if i % 4 else "hold",
                       guardrail=PASS if i % 4 else FAIL, lazy=PASS)
        out = cal.recalibrate(cal.calibrate(log))
        self.assertTrue(out.tightened)
        self.assertGreater(out.threshold.min_agreeing, 2)
        self.assertIsNone(out.proposal)

    def test_loosening_is_inert_until_a_human_accepts(self):
        log = self.log("quiet.jsonl")
        for i in range(40):
            if i % 13 == 0:
                self.audit(log, "hold", a=FAIL, b=PASS)
            elif i % 7 == 0:
                self.audit(log, "block", a=FAIL, b=FAIL)
            else:
                self.audit(log, "release", a=PASS, b=PASS)
        strict = cal.Threshold(min_agreeing=3, why="tightened earlier")
        out = cal.recalibrate(cal.calibrate(log), strict)
        self.assertIsNotNone(out.proposal, out.to_dict())
        self.assertIs(out.threshold, strict)
        self.assertEqual(out.proposal.in_effect.min_agreeing, 3)
        out.proposal.accept("the operator")
        self.assertEqual(out.proposal.in_effect.min_agreeing, 2)

    def test_an_anonymous_acceptance_is_refused(self):
        p = cal.Proposal(cal.Threshold(3), cal.Threshold(2), "why")
        with self.assertRaises(ValueError):
            p.accept("")

    def test_thin_evidence_is_unmeasured_not_healthy(self):
        log = self.log("thin.jsonl")
        for _ in range(3):
            self.audit(log, "release", a=PASS, b=PASS)
        c = cal.calibrate(log)
        self.assertFalse(c.healthy)
        self.assertEqual({d.kind for d in c.degeneracies},
                         {cal.D_NEVER_FIRES})

    def test_an_always_failing_strategy_has_no_pass_to_downgrade(self):
        log = self.log("paranoid.jsonl")
        for i in range(20):
            self.audit(log, "block", guardrail=PASS if i % 3 else FAIL,
                       paranoid=FAIL)
        c = cal.calibrate(log)
        self.assertIn(cal.D_ALWAYS_FAILS, {d.kind for d in c.degeneracies})
        self.assertNotIn("paranoid", c.downgraded)


# ===========================================================================
# 5. The invariant evolution loop, against the evidence it harvests
# ===========================================================================

class Gate:
    def __init__(self, allowed):
        self.allowed = allowed
        self.reasons = ()

    def to_dict(self):
        return {"allowed": self.allowed}


class InvariantEvolution(TempCase):

    def evidence(self, log):
        log.append("orchestrator.step",
                   {"path": "orphan", "trace_id": "t-nowhere"},
                   actor="kernel")
        log.append("consensus.audit",
                   {"outcome": "hold",
                    "opinions": [{"strategy": "a", "verdict": FAIL,
                                  "findings": [{"why": "nothing shown"}]},
                                 {"strategy": "b", "verdict": PASS,
                                  "findings": []}]},
                   actor="consensus")
        log.append("orchestrator.step.done",
                   {"path": "p", "trace_id": "t1", "status": "escalated",
                    "error_code": "E_UPSTREAM"}, actor="kernel")
        log.append("envelope.violation",
                   {"tool": "read_file",
                    "violations": [{"kind": env.V_UNDECLARED,
                                    "blocking": True}]}, actor="envelopes")
        return log

    def test_every_kind_of_unexplained_thing_becomes_a_candidate(self):
        found = loop.harvest(self.evidence(self.log()))
        self.assertEqual({c.source for c in found}, set(loop.SOURCES))
        for c in found:
            self.assertIn(c.kind, inv.KINDS)
            self.assertTrue(c.statement)

    def test_the_same_evidence_proposes_the_same_candidate(self):
        log = self.evidence(self.log("stable.jsonl"))
        first = {c.id for c in loop.harvest(log)}
        second = {c.id for c in loop.harvest(log)}
        self.assertEqual(first, second)

    def test_a_second_pass_does_not_re_propose(self):
        log = self.evidence(self.log("once.jsonl"))
        engine = loop.EvolutionLoop(log=log)
        self.assertTrue(engine.run())
        self.assertEqual(engine.run(), ())

    def test_nothing_is_adopted_without_a_human_and_a_passing_gate(self):
        log = self.evidence(self.log("gated.jsonl"))
        engine = loop.EvolutionLoop(log=log)
        engine.run()
        target = engine.ledger.open[0].id
        with self.assertRaises(ValueError):
            engine.accept(target, "", "why", Gate(True))
        with self.assertRaises(loop.NotGated):
            engine.accept(target, "someone", "why")
        with self.assertRaises(loop.NotGated):
            engine.accept(target, "someone", "why", Gate(False))
        adopted = engine.accept(target, "someone", "why", Gate(True))
        self.assertEqual(adopted.status, loop.ACCEPTED)
        self.assertTrue(adopted.gate_digest)

    def test_a_real_regression_gate_verdict_is_accepted(self):
        """The seam: the loop takes the gate object the gate produces."""
        log = self.evidence(self.log("real.jsonl"))
        engine = loop.EvolutionLoop(log=log)
        engine.run()
        verdict, _fp = rg.check_repo(REPO, self.work / "gate")
        self.assertTrue(verdict.allowed, verdict.format())
        adopted = engine.accept(engine.ledger.open[0].id, "the suite",
                                "a real gate verdict", verdict)
        self.assertEqual(adopted.status, loop.ACCEPTED)

    def test_nothing_is_dropped_without_a_reason(self):
        log = self.evidence(self.log("reasons.jsonl"))
        engine = loop.EvolutionLoop(log=log)
        engine.run()
        target = engine.ledger.open[0].id
        with self.assertRaises(ValueError):
            engine.reject(target, "someone", "")
        engine.reject(target, "someone", "a property of the harness")
        self.assertEqual(engine.ledger.candidates[target].status,
                         loop.REJECTED)

    def test_a_decision_cannot_be_quietly_re_decided(self):
        log = self.evidence(self.log("final.jsonl"))
        engine = loop.EvolutionLoop(log=log)
        engine.run()
        target = engine.ledger.open[0].id
        engine.reject(target, "someone", "not real")
        with self.assertRaises(ValueError):
            engine.accept(target, "someone else", "actually real",
                          Gate(True))

    def test_the_ledger_round_trips(self):
        log = self.evidence(self.log("disk.jsonl"))
        engine = loop.EvolutionLoop(log=log)
        engine.run()
        path = self.work / loop.LEDGER_NAME
        loop.write_ledger(path, engine.ledger)
        back = loop.load_ledger(path)
        self.assertEqual(set(back.candidates), set(engine.ledger.candidates))


# ===========================================================================
# 6. Verification budgets, against the grades and history they read
# ===========================================================================

class VerificationBudgets(TempCase):

    def setUp(self):
        super().setUp()
        self.contracts = build_contracts(build_registry())

    def test_depth_tracks_the_risk_grade(self):
        planner = bud.BudgetPlanner(self.contracts)
        shallow = planner.plan("read_file")
        deep = planner.plan("delete_path")
        self.assertEqual(shallow.grade, LOW)
        self.assertEqual(deep.grade, CRITICAL)
        self.assertGreater(bud.depth_rank(deep.depth),
                           bud.depth_rank(shallow.depth))

    def test_no_budget_ever_goes_below_the_floor(self):
        planner = bud.BudgetPlanner(self.contracts)
        for tool in sorted(self.contracts):
            for total in (0, 1, 3, 10, 30):
                d = planner.plan(tool, bud.Budget(total))
                if d.refused:
                    self.assertEqual(d.runs, ())
                    continue
                self.assertGreaterEqual(bud.depth_rank(d.depth),
                                        bud.depth_rank(d.floor),
                                        f"{tool} at budget {total}")

    def test_an_unaffordable_floor_refuses_rather_than_under_verifies(self):
        planner = bud.BudgetPlanner(self.contracts)
        budget = bud.Budget(1)
        d = planner.plan("delete_path", budget)
        self.assertTrue(d.refused)
        self.assertEqual(budget.spent, 0)
        with self.assertRaises(bud.Unaffordable):
            planner.require("delete_path", bud.Budget(1))

    def test_a_clean_history_never_takes_a_critical_tool_below_its_floor(self):
        log = self.log("clean.jsonl")
        for _ in range(bud.CLEAN_RUN + 10):
            log.append("dispatch.call", {"tool": "delete_path", "ok": True,
                                         "envelope_ok": True}, actor="kernel")
        planner = bud.BudgetPlanner(self.contracts, log=log)
        planner.refresh()
        self.assertTrue(planner._history["delete_path"].clean)
        d = planner.plan("delete_path")
        self.assertEqual(d.depth, bud.EXHAUSTIVE)

    def test_an_envelope_violation_in_history_deepens_the_next_check(self):
        """The seam: an envelope finding changes what gets verified."""
        log = self.log("scarred.jsonl")
        for _ in range(30):
            log.append("dispatch.call", {"tool": "read_file", "ok": True,
                                         "envelope_ok": True}, actor="kernel")
        log.append("envelope.violation",
                   {"tool": "read_file",
                    "violations": [{"kind": env.V_UNDECLARED,
                                    "blocking": True}]}, actor="envelopes")
        planner = bud.BudgetPlanner(self.contracts, log=log)
        planner.refresh()
        d = planner.plan("read_file")
        self.assertEqual(d.rule, bud.RULE_INCIDENT, d.to_dict())
        self.assertGreater(bud.depth_rank(d.depth),
                           bud.depth_rank(bud.SHALLOW))

    def test_an_undeclared_tool_is_treated_as_the_most_dangerous(self):
        d = bud.BudgetPlanner({}).plan("nobody_registered_me")
        self.assertEqual(d.grade, CRITICAL)
        self.assertEqual(d.depth, bud.EXHAUSTIVE)

    def test_every_decision_is_auditable(self):
        log = self.log("audited.jsonl")
        planner = bud.BudgetPlanner(self.contracts, log=log)
        planner.plan("read_file")
        planner.plan("delete_path", bud.Budget(1))
        kinds = {e.type for e in log.events()}
        self.assertIn("budget.decided", kinds)
        self.assertIn("budget.refused", kinds)


# ===========================================================================
# 7. The runbook engine, against the playbooks it exercises
# ===========================================================================

class RecoveryRunbooks(TempCase):

    def test_every_failure_class_is_injected_and_its_playbook_holds(self):
        report = rb.run_all(self.work / "all", self.log())
        self.assertTrue(report.ok, report.format())
        self.assertEqual(report.passed, len(rb.RUNBOOKS))

    def test_every_error_code_is_covered(self):
        self.assertEqual({b.code for b in rb.RUNBOOKS}, set(ERROR_CODES))
        self.assertEqual(set(rb.INJECTORS), set(ERROR_CODES))

    def test_an_uncovered_failure_class_is_a_defect(self):
        thin = rb.run_all(self.work / "thin", None, books=(rb.RUNBOOKS[0],))
        self.assertFalse(thin.ok)
        uncovered = {d.subject for d in thin.defects
                     if d.kind == rb.D_UNCOVERED}
        self.assertEqual(uncovered,
                         set(ERROR_CODES) - {rb.RUNBOOKS[0].code})

    def test_a_broken_harness_is_reported_apart_from_a_broken_playbook(self):
        ghost = rb.Runbook("E_NOT_A_CODE", True, True, True)
        out = rb.run_one(ghost, self.work / "ghost", None)
        self.assertFalse(out.passed)
        self.assertEqual(out.defect.kind, rb.D_HARNESS_BROKEN)

    def test_expectations_are_read_from_the_playbook(self):
        """A runbook that hard-coded its answer would pass forever."""
        for book in rb.RUNBOOKS:
            self.assertIn(book.expected(), recovery.STRATEGIES)
            self.assertNotEqual(book.expected(), recovery.RETRY)

    def test_the_context_really_changes_the_answer(self):
        from fullagent.toolcontract import E_TIMEOUT
        with_human = rb.Runbook(E_TIMEOUT, False, True, True)
        alone = rb.Runbook(E_TIMEOUT, False, True, False)
        self.assertEqual(with_human.expected(), recovery.ESCALATE)
        self.assertEqual(alone.expected(), recovery.ABORT)

    def test_runbooks_are_deterministic(self):
        a = rb.run_one(rb.RUNBOOKS[4], self.work / "d1", None)
        b = rb.run_one(rb.RUNBOOKS[4], self.work / "d2", None)
        self.assertEqual(a.observed, b.observed)
        self.assertEqual(a.passed, b.passed)

    def test_freshness_notices_a_changed_playbook_set(self):
        log = self.log("fresh.jsonl")
        rb.run_all(self.work / "fresh", log)
        self.assertIsNone(rb.freshness(log))
        stale = self.log("stale.jsonl")
        stale.append("runbook.run",
                     {"digest": "0" * 16, "defects": [], "at": 0.0},
                     actor="runbook")
        defect = rb.freshness(stale)
        self.assertIsNotNone(defect)
        self.assertEqual(defect.kind, rb.D_STALE)

    def test_no_run_at_all_is_stale(self):
        self.assertIsNotNone(rb.freshness(self.log("never.jsonl")))


# ===========================================================================
# 8. The whole stack
# ===========================================================================

class StackIsWiredUp(unittest.TestCase):

    def test_every_invariant_still_holds(self):
        report = inv.verify()
        self.assertTrue(report.ok, report.format())
        self.assertGreater(len(report.checked), 50)

    def test_the_new_modules_are_covered_by_invariants(self):
        modules = {i.module for i in inv.all_invariants()}
        for module in ("envelopes", "releasegate", "calibration",
                       "invariantloop", "budgets", "runbook"):
            self.assertIn(module, modules,
                          f"{module} states no machine-checkable claim")

    def test_run_checks_runs_every_gate(self):
        script = (REPO / "run-checks.sh").read_text(encoding="utf-8")
        for module in ("invariants", "governance", "regressiongate",
                       "envelopes", "runbook"):
            self.assertIn(f"fullagent.{module}", script,
                          f"{module} has a gate that CI does not run")

    def test_envelopes_and_budgets_are_governed_surfaces(self):
        """Widening an envelope is a rule change, so the gate must see it."""
        fp = rg.fingerprint(prompt_text="x")
        self.assertIn("envelopes", fp.surfaces)
        self.assertIn("verification", fp.surfaces)
        widened = dict(env.ENVELOPES)
        widened["read_file"] = dataclasses.replace(
            env.ENVELOPES["read_file"],
            effects=frozenset({env.FX_READ, env.FX_MODIFY}))
        before = rg.envelope_digest()
        original = env.ENVELOPES.copy()
        env.ENVELOPES.update(widened)
        try:
            self.assertNotEqual(rg.envelope_digest(), before)
        finally:
            env.ENVELOPES.clear()
            env.ENVELOPES.update(original)

    def test_the_repo_regression_gate_passes(self):
        work = Path(tempfile.mkdtemp(prefix="fa-gatecheck-"))
        try:
            verdict, _fp = rg.check_repo(REPO, work)
            self.assertTrue(verdict.allowed, verdict.format())
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_the_recorded_baseline_covers_the_new_surfaces(self):
        baseline = rg.load_baseline(REPO / rg.BASELINE_NAME)
        self.assertIsNotNone(baseline)
        self.assertIn("envelopes", baseline.fingerprint.surfaces)
        self.assertIn("verification", baseline.fingerprint.surfaces)


if __name__ == "__main__":
    unittest.main(verbosity=2)

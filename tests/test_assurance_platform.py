"""Directive 5: the assurance-case driven self-auditing layer.

Each module has its own self-test that proves it works in isolation.
These are the tests that only fail when two modules disagree -- the
assurance case citing evidence the fault catalogue says is unreachable,
the regression gate not governing a surface that changes behaviour, the
dashboard displaying a figure nobody sealed.
"""

import json
import tempfile
import unittest
from pathlib import Path

from fullagent import (assurance, assuranceboard, faultcatalogue,
                       historicaudit, policymeta, threatpins)
from fullagent.kernel import EventLog


def _log(name: str) -> EventLog:
    return EventLog(path=Path(tempfile.mkdtemp(prefix="fa-t5-")) / name)


class CompositionalAssuranceCase(unittest.TestCase):
    """A claim with nothing under it must not be able to pass."""

    def test_shipped_case_resolves_every_claim(self):
        log = _log("case.jsonl")
        case = assurance.assess(assurance.shipped_case(), log)
        self.assertEqual(case.defects, (),
                         "\n".join(d.line() for d in case.defects))
        for node in case.nodes.values():
            self.assertNotEqual(
                node.status, assurance.UNSUPPORTED,
                f"{node.id} has nothing under it: {node.text}")

    def test_every_evidence_finding_is_sealed(self):
        log = _log("sealed.jsonl")
        case = assurance.assess(assurance.shipped_case(), log)
        self.assertEqual(assurance.verify_seals(case, log), ())
        sealed = {ev.data["node"] for ev in log.events()
                  if ev.type == "assurance.evidence"}
        evidence = {n.id for n in case.nodes.values()
                    if n.kind == assurance.EVIDENCE}
        self.assertEqual(sealed, evidence)

    def test_unsealed_evidence_is_a_defect(self):
        """Assessed without a log, then asked to prove it was sealed."""
        case = assurance.assess(assurance.shipped_case(), None)
        defects = assurance.verify_seals(case, _log("empty.jsonl"))
        self.assertTrue(defects)
        self.assertTrue(all(d.kind == assurance.A_UNSEALED for d in defects))

    def test_the_root_rests_on_stated_assumptions(self):
        """The headline claim is assumed, not proved, and says which.

        This is the honest reading of the stack and it is pinned here so
        it cannot quietly become `holds`: the root depends on assumptions
        no code can discharge — token generation happens elsewhere, and
        two thirds of the prompt's rules are advisory.
        """
        case = assurance.assess(assurance.shipped_case(), _log("root.jsonl"))
        root = case.nodes[case.roots[0]]
        self.assertEqual(root.status, assurance.ASSUMED)
        stated = " ".join(n.text for n in case.assumed)
        self.assertIn("token generation happens outside", stated)
        self.assertIn("advisory", stated)


class PolicyMetamodel(unittest.TestCase):

    def test_meta_properties_hold(self):
        report = policymeta.verify_metamodel()
        self.assertTrue(report.ok, report.format())
        self.assertEqual(report.proved, 10, report.format())
        self.assertEqual(report.checked, 3, report.format())

    def test_proved_and_checked_are_never_added_together(self):
        """A sampled check must not be counted as a proof."""
        report = policymeta.verify_metamodel()
        proved = {p.id for p in report.properties
                  if p.method == policymeta.PROVED}
        checked = {p.id for p in report.properties
                   if p.method == policymeta.CHECKED}
        self.assertFalse(proved & checked)
        self.assertEqual(report.proved + report.checked,
                         len(report.properties))
        self.assertIn(policymeta.P_MODEL_FAITHFUL, checked,
                      "a corpus check must never be reported as proved")

    def test_the_model_matches_the_shipped_pipeline(self):
        from fullagent.policypipeline import DEFAULT_STAGES
        self.assertEqual({s.name for s in DEFAULT_STAGES},
                         set(policymeta.STAGE_MODELS))


class CounterfactualCatalogue(unittest.TestCase):

    def test_every_typed_refusal_is_accounted_for(self):
        report = faultcatalogue.run_all()
        self.assertTrue(report.ok, report.format())
        for cover in report.coverage:
            self.assertEqual(cover.uncovered, (), cover.line())

    def test_unreachable_claims_are_tested_not_exempted(self):
        """A class declared unreachable still has its scenario run."""
        report = faultcatalogue.run_all()
        declared = set(faultcatalogue.UNREACHABLE)
        self.assertTrue(declared, "the declaration set should not be empty")
        for outcome in report.outcomes:
            if outcome.target in declared and not outcome.provoked:
                self.assertIsNone(outcome.defect)
                self.assertTrue(outcome.detail,
                                "an unreachable claim must carry its reason")

    def test_the_catalogue_covers_every_error_code(self):
        from fullagent.toolcontract import ERROR_CODES
        report = faultcatalogue.run_all()
        taxonomy = next(c for c in report.coverage
                        if c.surface == faultcatalogue.S_TAXONOMY)
        self.assertEqual(set(taxonomy.covered), set(ERROR_CODES))


class HistoricalReplay(unittest.TestCase):

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="fa-t5-hist-"))
        self.key = b"cross-module-key"

    def test_a_decision_is_replayed_against_its_own_ruleset(self):
        from fullagent.toolpolicy import ToolPolicy
        log = EventLog(path=self.work / "h.jsonl")
        historicaudit.record(log, historicaudit.capture("v1", self.key))
        policy = ToolPolicy(role="developer", log=log,
                            roots=[str(self.work)])
        policy.evaluate("read_file", {"path": "/etc/passwd"})
        report = historicaudit.audit(log, self.key)
        self.assertTrue(report.ok and report.complete, report.format())

    def test_replay_never_falls_back_to_current_rules(self):
        from fullagent.toolpolicy import ToolPolicy
        log = EventLog(path=self.work / "orphan.jsonl")
        ToolPolicy(role="developer", log=log, roots=[str(self.work)]
                   ).evaluate("read_file", {"path": "/etc/passwd"})
        report = historicaudit.audit(log, self.key)
        self.assertEqual(report.replays[0].kind, historicaudit.V_NO_RULESET)
        self.assertFalse(report.complete,
                         "an unjudged decision must not count as checked")

    def test_the_policy_seals_the_facts_replay_needs(self):
        """The seam between toolpolicy and this module."""
        from fullagent.toolpolicy import ToolPolicy
        log = EventLog(path=self.work / "facts.jsonl")
        ToolPolicy(role="developer", log=log, roots=[str(self.work)]
                   ).evaluate("read_file", {"path": "/etc/passwd"})
        sealed = next(e for e in log.events() if e.type == "policy.decision")
        for fact in historicaudit.REQUIRED_FACTS:
            self.assertIn(fact, sealed.data["request"])

    def test_only_the_arguments_the_decision_read_are_sealed(self):
        from fullagent.toolpolicy import ToolPolicy
        policy = ToolPolicy(role="developer", roots=[str(self.work)])
        facts = policy.replayable_facts(
            "write_file", {"path": "p.txt", "content": "a secret"})
        self.assertEqual(facts["args"], {"path": "p.txt"})


class HeadlessBoard(unittest.TestCase):

    def test_an_empty_log_reads_as_unmeasured_not_clean(self):
        board = assuranceboard.collect(_log("blank.jsonl"))
        self.assertFalse(board.ok)
        self.assertTrue(all(c.state == assuranceboard.UNKNOWN
                            for c in board.cells))
        self.assertNotIn("0", " ".join(c.value for c in board.cells))

    def test_every_displayed_figure_names_a_sealed_event(self):
        log = _log("live.jsonl")
        board = assuranceboard.refresh(log)
        self.assertTrue(board.traceable)
        types = {ev.type for ev in log.events()}
        for cell in board.cells:
            if cell.state != assuranceboard.UNKNOWN:
                self.assertIn(cell.source, types, cell)
                self.assertGreaterEqual(cell.seq, 0, cell)

    def test_reading_the_board_runs_nothing(self):
        log = _log("ro.jsonl")
        assuranceboard.refresh(log)
        before = len(list(log.events()))
        assuranceboard.collect(log)
        self.assertEqual(len(list(log.events())), before)

    def test_the_board_is_a_function_of_the_log(self):
        log = _log("det.jsonl")
        assuranceboard.refresh(log)
        first = assuranceboard.collect(log).to_dict()
        second = assuranceboard.collect(log).to_dict()
        del first["at"], second["at"]
        self.assertEqual(first, second)


class ThreatModelPins(unittest.TestCase):

    def test_the_committed_posture_still_holds(self):
        report = threatpins.check(Path.cwd())
        self.assertTrue(report.ok, report.format())

    def test_every_documented_risk_is_measurable(self):
        for risk_id, got in threatpins.measure().items():
            self.assertIsInstance(got, threatpins.Measurement, risk_id)

    def test_a_narrowing_needs_a_decision_too(self):
        self.assertIn(threatpins.P_NARROWED, threatpins.NEEDS_REVIEW)
        self.assertNotIn(threatpins.P_HELD, threatpins.NEEDS_REVIEW)

    def test_the_pinned_numbers_match_what_the_documents_say(self):
        """The four limits the directive named, each with a number now."""
        taken = threatpins.measure()
        self.assertTrue(taken["shell-deny-list"].facts["missed"],
                        "the deny-list is documented as incomplete")
        coverage = taken["rule-coverage"].facts["coverage_pct"]
        self.assertTrue(20.0 <= coverage <= 50.0, coverage)
        self.assertTrue(
            taken["scripted-turn-gate"].facts["uses_scripted_executor"])
        self.assertTrue(taken["no-process-sandbox"].facts["spawns_processes"])


class StackIsWiredUp(unittest.TestCase):
    """The seams. Each of these was a real way for the layer to be inert."""

    def test_the_regression_gate_governs_the_new_surfaces(self):
        from fullagent.regressiongate import fingerprint
        surfaces = fingerprint().surfaces
        for name in ("policy-metamodel", "failure-catalogue",
                     "threat-model"):
            self.assertIn(name, surfaces)
            self.assertTrue(surfaces[name], f"{name} digest is empty")

    def test_changing_the_metamodel_changes_the_fingerprint(self):
        """A governed surface that never moves governs nothing."""
        from fullagent.policypipeline import DEFAULT_STAGES, PolicyPipeline
        shuffled = PolicyPipeline(
            (DEFAULT_STAGES[0], DEFAULT_STAGES[-1]) + DEFAULT_STAGES[1:-1])
        self.assertNotEqual(policymeta.metamodel_digest(shuffled),
                            policymeta.metamodel_digest())

    def test_the_assurance_case_cites_the_new_modules(self):
        case = assurance.shipped_case()
        text = " ".join(n.text for n in case.nodes.values())
        self.assertIn("metamodel", text)

    def test_the_invariant_suite_covers_the_new_modules(self):
        from fullagent.invariants import all_invariants
        modules = {i.module for i in all_invariants()}
        for name in ("assurance", "policymeta", "faultcatalogue",
                     "historicaudit", "assuranceboard", "threatpins"):
            self.assertIn(name, modules)

    def test_every_new_report_is_json_serialisable(self):
        """The board and the gates are read by machines, not only people."""
        log = _log("json.jsonl")
        payloads = [
            policymeta.verify_metamodel().to_dict(),
            faultcatalogue.run_all().to_dict(),
            threatpins.check(Path.cwd()).to_dict(),
            assuranceboard.collect(log).to_dict(),
            historicaudit.audit(log, b"k").to_dict(),
        ]
        for payload in payloads:
            self.assertEqual(json.loads(json.dumps(payload)), payload)


if __name__ == "__main__":
    unittest.main()

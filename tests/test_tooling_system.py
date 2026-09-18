"""The tooling system, tested at the boundaries it promises to hold.

The contract layer, the dispatcher and the reference tools each carry a
`__main__` self-test that covers them alone. This file covers what those
cannot: a tool reached *through* its contract and its policy, which is
the only way the agent ever reaches one. Several tests encode a hole that
was real during the build (a redirect that outran the allow-list, a
retryable failure labelled as a defect) rather than a behaviour that was
designed in.
"""

import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

from fullagent import tools as T
from fullagent.dispatch import Dispatcher, TraceContext
from fullagent.orchestrator import (COMPENSATED, DONE, FAILED, IRREVERSIBLE,
                                    SKIPPED, Expectation, Orchestrator, Plan,
                                    Step)
from fullagent.kernel import EventLog
from fullagent.toolcontract import (E_NOT_FOUND, E_PERMISSION, E_TIMEOUT,
                                    E_UPSTREAM, E_VALIDATION, IDEMPOTENT,
                                    RETRYABLE, TEXT_OUT, RetryPolicy,
                                    ToolContract, ToolError, build_contracts,
                                    classify, contract_for,
                                    unsupported_keywords, validate)
from fullagent.toolpolicy import (FS_READ, FS_WRITE, NET_FETCH, PROC_EXEC,
                                  ToolPolicy, host_allowed)

STR_ARG = {"type": "object", "properties": {"path": {"type": "string"}},
           "required": ["path"]}


class Sandbox(unittest.TestCase):
    """Every test runs against a throwaway tree, never the real repo."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="fa-tooling-"))
        self.cwd = os.getcwd()
        os.chdir(self.root)
        self.log = EventLog(path=str(self.root / "events.jsonl"))
        self.registry = T.build_registry()
        self.contracts = build_contracts(self.registry)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.root, ignore_errors=True)

    def dispatcher(self, role="developer", approve=None, **kw):
        policy = ToolPolicy(role, log=self.log, roots=(str(self.root),), **kw)
        d = Dispatcher(policy=policy, log=self.log, approve=approve)
        d.register_registry(self.registry, self.contracts)
        return d


# ---------------------------------------------------------------------------
# The contract layer
# ---------------------------------------------------------------------------

class TestContracts(Sandbox):

    def test_every_registered_tool_has_a_contract(self):
        missing = set(self.registry) - set(self.contracts)
        self.assertEqual(missing, set(), f"tools with no contract: {missing}")

    def test_schemas_use_only_the_subset_we_validate(self):
        """A schema keyword we do not implement is a silently unchecked
        field, which is worse than no schema at all."""
        for name, c in self.contracts.items():
            for schema in (c.input_schema, c.output_schema):
                self.assertEqual(unsupported_keywords(schema), (),
                                 f"{name} uses keywords validate() ignores")

    def test_destructive_tools_need_approval(self):
        for name in ("delete_path", "write_file", "run_command"):
            self.assertTrue(self.contracts[name].needs_approval,
                            f"{name} must not run unattended")
        self.assertFalse(self.contracts["read_file"].needs_approval)

    def test_retryability_belongs_to_the_code_not_the_call_site(self):
        """A policy may narrow what is retried. It must never widen it:
        two callers disagreeing about a repeat is how a non-idempotent
        side effect happens twice."""
        final = RetryPolicy(max_attempts=5, codes=tuple(RETRYABLE))
        for code, retryable in RETRYABLE.items():
            allowed = final.should_retry(ToolError(code, "x"), attempt=1)
            self.assertEqual(allowed, retryable, code)

    def test_validation_reports_the_offending_field(self):
        err = validate({"path": 3}, STR_ARG)
        self.assertIsNotNone(err)
        self.assertEqual(err.code, E_VALIDATION)
        self.assertIn("path", err.field)

    def test_missing_required_field_is_caught(self):
        err = validate({}, STR_ARG)
        self.assertIsNotNone(err)
        self.assertEqual(err.code, E_VALIDATION)


class TestClassification(unittest.TestCase):
    """The bug this encodes: every exception escaping a tool was labelled
    E_INTERNAL, which the taxonomy calls final. A dropped socket then
    looked like a defect and no retry policy could ever fire."""

    def test_network_failures_are_upstream_and_retryable(self):
        for exc in (ConnectionError("reset"), ConnectionResetError(),
                    BrokenPipeError(), OSError(104, "reset by peer")):
            code = classify(exc)
            self.assertEqual(code, E_UPSTREAM, repr(exc))
            self.assertTrue(RETRYABLE[code])

    def test_timeouts_are_timeouts(self):
        self.assertEqual(classify(TimeoutError("slow")), E_TIMEOUT)

    def test_filesystem_failures_keep_their_meaning(self):
        self.assertEqual(classify(FileNotFoundError()), E_NOT_FOUND)
        self.assertEqual(classify(PermissionError()), E_PERMISSION)

    def test_a_real_defect_is_still_final(self):
        code = classify(RuntimeError("off-by-one"))
        self.assertFalse(RETRYABLE[code])


# ---------------------------------------------------------------------------
# Reference tool: file IO
# ---------------------------------------------------------------------------

class TestFileIO(Sandbox):

    def test_write_then_read_round_trips_through_the_dispatcher(self):
        d = self.dispatcher(approve=lambda c, a: True)
        target = self.root / "note.txt"
        wrote = d.call("write_file", {"path": str(target),
                                      "content": "hello\n"})
        self.assertTrue(wrote.ok, wrote.to_dict())
        read = d.call("read_file", {"path": str(target)})
        self.assertTrue(read.ok, read.to_dict())
        self.assertIn("hello", read.value)

    def test_a_path_outside_the_roots_is_refused_before_the_tool_runs(self):
        d = self.dispatcher(approve=lambda c, a: True)
        outside = Path(tempfile.gettempdir()) / "fa-escape.txt"
        outside.unlink(missing_ok=True)
        blocked = d.call("write_file", {"path": str(outside),
                                        "content": "nope"})
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.error.code, E_PERMISSION)
        self.assertFalse(outside.exists(), "the handler ran anyway")

    def test_a_symlink_cannot_walk_out_of_the_sandbox(self):
        """Confinement is checked after resolve(), so a link pointing out
        of the tree is out of the tree."""
        outside_dir = Path(tempfile.mkdtemp(prefix="fa-outside-"))
        try:
            (self.root / "door").symlink_to(outside_dir)
            d = self.dispatcher(approve=lambda c, a: True)
            blocked = d.call("write_file",
                             {"path": str(self.root / "door" / "x.txt"),
                              "content": "nope"})
            self.assertFalse(blocked.ok)
            self.assertFalse((outside_dir / "x.txt").exists())
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)

    def test_reading_a_missing_file_is_typed_not_raised(self):
        d = self.dispatcher()
        out = d.call("read_file", {"path": str(self.root / "ghost.txt")})
        self.assertFalse(out.ok)
        self.assertIn(out.error.code, (E_NOT_FOUND, E_VALIDATION))

    def test_a_readonly_role_cannot_write_at_all(self):
        d = self.dispatcher("readonly", approve=lambda c, a: True)
        out = d.call("write_file", {"path": str(self.root / "a.txt"),
                                    "content": "x"})
        self.assertFalse(out.ok)
        self.assertEqual(out.error.code, E_PERMISSION)
        self.assertNotIn("write_file", d.negotiate().available)


# ---------------------------------------------------------------------------
# Reference tool: the policy-sandboxed shell
# ---------------------------------------------------------------------------

class TestSandboxedShell(Sandbox):

    def test_an_allowed_command_runs_and_reports_its_exit_code(self):
        d = self.dispatcher(approve=lambda c, a: True)
        out = d.call("run_command", {"command": "echo tooling-ok"})
        self.assertTrue(out.ok, out.to_dict())
        self.assertIn("tooling-ok", out.value)

    def test_a_denied_command_never_reaches_the_shell(self):
        canary = self.root / "canary.txt"
        canary.write_text("alive\n")
        d = self.dispatcher(approve=lambda c, a: True)
        out = d.call("run_command", {"command": f"rm -rf {canary}"})
        self.assertFalse(out.ok, "a destructive command was allowed through")
        self.assertEqual(out.error.code, E_PERMISSION)
        self.assertTrue(canary.exists(), "the command ran despite the denial")

    def test_refusing_approval_stops_the_command(self):
        d = self.dispatcher(approve=lambda c, a: False)
        marker = self.root / "ran.txt"
        out = d.call("run_command", {"command": f"touch {marker}"})
        self.assertFalse(out.ok)
        self.assertFalse(marker.exists())

    def test_no_approval_hook_means_no(self):
        """Default-deny: a session that cannot ask a human must not
        answer on the human's behalf."""
        d = self.dispatcher(approve=None)
        marker = self.root / "unattended.txt"
        out = d.call("run_command", {"command": f"touch {marker}"})
        self.assertFalse(out.ok)
        self.assertFalse(marker.exists())

    def test_a_role_without_proc_exec_cannot_see_the_shell(self):
        d = self.dispatcher("readonly")
        self.assertIn("run_command", d.negotiate().unavailable)
        self.assertNotIn("run_command",
                         [s["function"]["name"] for s in d.schemas()])


# ---------------------------------------------------------------------------
# Reference tool: HTTP fetch behind an allow-list
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, headers=None, text="body", url=""):
        self.status_code = status
        self.headers = headers or {}
        self.text = text
        self.url = url

    @property
    def is_redirect(self):
        return 300 <= self.status_code < 400 and "location" in self.headers

    is_permanent_redirect = is_redirect

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} error")


class FakeRequests:
    """Stands in for `requests` so the allow-list is tested without
    depending on what the network happens to do today."""

    def __init__(self, script):
        self.script = dict(script)
        self.visited = []

    def get(self, url, **kw):
        self.visited.append(url)
        return self.script.get(url, FakeResponse(text="default", url=url))


class TestHttpAllowList(Sandbox):

    def setUp(self):
        super().setUp()
        self.real = sys.modules.get("requests")

    def tearDown(self):
        if self.real is not None:
            sys.modules["requests"] = self.real
        else:
            sys.modules.pop("requests", None)
        super().tearDown()

    def install(self, script):
        fake = FakeRequests(script)
        module = types.ModuleType("requests")
        module.get = fake.get
        sys.modules["requests"] = module
        return fake

    def test_the_metadata_endpoint_is_refused(self):
        fake = self.install({})
        out = T.web_fetch("http://169.254.169.254/latest/meta-data/")
        self.assertTrue(out.startswith("ERROR: refused"), out)
        self.assertEqual(fake.visited, [], "the request was actually sent")

    def test_a_redirect_into_the_metadata_endpoint_is_refused(self):
        """The hole this closes: the policy layer only ever sees the URL
        the model asked for. A 302 arrives after that check."""
        start = "https://example.com/go"
        fake = self.install({start: FakeResponse(
            302, {"location": "http://169.254.169.254/"}, url=start)})
        out = T.web_fetch(start)
        self.assertIn("refused redirect", out)
        self.assertEqual(fake.visited, [start], "it followed the redirect")

    def test_an_ordinary_redirect_is_still_followed(self):
        start, end = "https://example.com/a", "https://example.com/b"
        fake = self.install({
            start: FakeResponse(301, {"location": end}, url=start),
            end: FakeResponse(200, {"content-type": "text/plain"},
                              text="arrived", url=end)})
        self.assertIn("arrived", T.web_fetch(start))
        self.assertEqual(fake.visited, [start, end])

    def test_a_redirect_loop_terminates(self):
        url = "https://example.com/loop"
        self.install({url: FakeResponse(302, {"location": url}, url=url)})
        self.assertIn("redirects", T.web_fetch(url))

    def test_non_http_schemes_never_reach_the_fetcher(self):
        for url in ("file:///etc/passwd", "gopher://x/", "ftp://x/f"):
            self.assertIn("refused", T.web_fetch(url), url)

    def test_an_allow_list_narrows_but_cannot_widen(self):
        locked = ("api.example.com",)
        self.assertEqual(host_allowed("https://api.example.com/v1", locked), "")
        self.assertTrue(host_allowed("https://other.example/v1", locked))
        self.assertTrue(
            host_allowed("http://169.254.169.254/", ("169.254.169.254",)),
            "an allow-list entry must not unblock a blocked host")

    def test_the_policy_layer_refuses_before_the_tool_is_called(self):
        d = self.dispatcher(approve=lambda c, a: True)
        out = d.call("web_fetch", {"url": "http://169.254.169.254/"})
        self.assertFalse(out.ok)
        self.assertEqual(out.error.code, E_PERMISSION)


# ---------------------------------------------------------------------------
# Reference tool: structured search
# ---------------------------------------------------------------------------

class TestStructuredSearch(Sandbox):

    def seed(self):
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "alpha.py").write_text(
            "def handler():\n    return TOKEN_ALPHA\n")
        (self.root / "pkg" / "beta.py").write_text(
            "# no match here\nvalue = 1\n")
        (self.root / "pkg" / "notes.md").write_text("TOKEN_ALPHA in prose\n")

    def test_search_finds_the_match_with_its_location(self):
        self.seed()
        d = self.dispatcher()
        out = d.call("search_files", {"pattern": "TOKEN_ALPHA",
                                      "path": str(self.root)})
        self.assertTrue(out.ok, out.to_dict())
        self.assertIn("alpha.py", out.value)
        self.assertNotIn("beta.py", out.value)

    def test_a_glob_filter_narrows_the_result(self):
        self.seed()
        d = self.dispatcher()
        out = d.call("search_files", {"pattern": "TOKEN_ALPHA",
                                      "path": str(self.root),
                                      "glob_filter": "*.py"})
        self.assertTrue(out.ok)
        self.assertNotIn("notes.md", out.value)

    def test_no_match_is_an_answer_not_an_error(self):
        self.seed()
        d = self.dispatcher()
        out = d.call("search_files", {"pattern": "NOTHING_LIKE_THIS",
                                      "path": str(self.root)})
        self.assertTrue(out.ok, out.to_dict())

    def test_glob_lists_files_by_pattern(self):
        self.seed()
        d = self.dispatcher()
        out = d.call("glob_files", {"pattern": "**/*.py",
                                    "path": str(self.root)})
        self.assertTrue(out.ok, out.to_dict())
        self.assertIn("alpha.py", out.value)


# ---------------------------------------------------------------------------
# Dispatch behaviour that no single tool can demonstrate
# ---------------------------------------------------------------------------

class TestDispatchGuarantees(Sandbox):

    def test_a_trace_id_survives_the_whole_call(self):
        d = self.dispatcher()
        ctx = TraceContext()
        out = d.call("list_dir", {"path": str(self.root)}, trace=ctx)
        self.assertEqual(out.trace_id, ctx.trace_id)
        self.assertEqual(out.parent_span, ctx.span_id)
        self.assertTrue(out.span_id and out.span_id != ctx.span_id)

    def test_an_error_carries_the_trace_id_too(self):
        d = self.dispatcher()
        ctx = TraceContext()
        out = d.call("read_file", {"path": 7}, trace=ctx)
        self.assertFalse(out.ok)
        self.assertEqual(out.error.trace_id, ctx.trace_id)

    def test_an_unknown_tool_is_a_typed_error_not_a_crash(self):
        d = self.dispatcher()
        out = d.call("teleport", {})
        self.assertFalse(out.ok)
        self.assertEqual(out.error.code, E_NOT_FOUND)

    def test_a_non_idempotent_tool_is_never_retried(self):
        calls = {"n": 0}

        def flaky(**kw):
            calls["n"] += 1
            raise ConnectionError("upstream hiccup")

        d = self.dispatcher()
        d.register(ToolContract("flaky_write", "fails", STR_ARG, TEXT_OUT,
                                frozenset({FS_READ}),
                                retry=RetryPolicy(max_attempts=3)), flaky)
        out = d.call("flaky_write", {"path": "x"})
        self.assertEqual(out.attempts, 1)
        self.assertEqual(calls["n"], 1, "a side effect was repeated")

    def test_an_idempotent_tool_recovers_from_a_retryable_failure(self):
        calls = {"n": 0}

        def flaky(**kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("upstream hiccup")
            return "third time"

        d = self.dispatcher()
        d.register(ToolContract("flaky_read", "fails twice", STR_ARG,
                                TEXT_OUT, frozenset({FS_READ}),
                                idempotency=IDEMPOTENT,
                                retry=RetryPolicy(max_attempts=3)), flaky)
        out = d.call("flaky_read", {"path": "x"})
        self.assertTrue(out.ok, out.to_dict())
        self.assertEqual(out.attempts, 3)

    def test_an_approval_hook_that_raises_is_not_consent(self):
        def explodes(contract, args):
            raise RuntimeError("hook is broken")

        marker = self.root / "consented.txt"
        d = self.dispatcher(approve=explodes)
        out = d.call("write_file", {"path": str(marker), "content": "x"})
        self.assertFalse(out.ok)
        self.assertFalse(marker.exists())

    def test_every_call_is_written_to_the_audit_log(self):
        d = self.dispatcher()
        d.call("list_dir", {"path": str(self.root)})
        kinds = [e.type for e in self.log.events()]
        self.assertIn("dispatch.call", kinds)

    def test_metrics_count_what_happened(self):
        d = self.dispatcher()
        d.call("list_dir", {"path": str(self.root)})
        d.call("read_file", {"path": 7})
        m = d.metrics
        self.assertGreaterEqual(m.calls["list_dir"], 1)
        self.assertGreaterEqual(m.errors_by_code.get(E_VALIDATION, 0), 1)

    def test_negotiation_matches_what_the_dispatcher_will_actually_run(self):
        """A schema advertised but denied at call time wastes a model turn
        and teaches it that its tools are unreliable."""
        d = self.dispatcher("readonly")
        advertised = {s["function"]["name"] for s in d.schemas()}
        for name in advertised:
            contract = d.contract(name)
            self.assertIsNotNone(contract)
            self.assertNotIn(name, d.negotiate().unavailable, name)


# ---------------------------------------------------------------------------
# Orchestration: plan, execute, verify, undo
# ---------------------------------------------------------------------------

class TestOrchestrator(Sandbox):

    def orch(self, role="developer", approve=lambda p, r: True):
        return Orchestrator(self.dispatcher(role, approve=lambda c, a: True),
                            log=self.log, approve=approve)

    def write(self, step_id, name, content="x\n"):
        path = str(self.root / name)
        return path, Step(step_id, "write_file",
                          {"path": path, "content": content},
                          expect=Expectation(path_exists=(path,)),
                          undo_tool="delete_path", undo_args={"path": path})

    def test_a_plan_that_succeeds_reports_every_step_done(self):
        path, step = self.write("one", "one.txt", "alpha\n")
        out = self.orch().run(Plan("write one file", (step,)))
        self.assertTrue(out.ok, out.format())
        self.assertEqual(out.entry("one").status, DONE)
        self.assertTrue(Path(path).exists())

    def test_a_failed_verification_undoes_the_earlier_steps(self):
        first_path, first = self.write("first", "first.txt")
        second_path = str(self.root / "second.txt")
        second = Step("second", "write_file",
                      {"path": second_path, "content": "y\n"},
                      expect=Expectation(contains=("never written",)),
                      undo_tool="delete_path",
                      undo_args={"path": second_path})
        third = Step("third", "read_file", {"path": first_path})

        out = self.orch().run(Plan("two writes", (first, second, third)))
        self.assertFalse(out.ok)
        self.assertEqual(out.entry("first").status, COMPENSATED)
        self.assertEqual(out.entry("second").status, FAILED)
        self.assertEqual(out.entry("third").status, SKIPPED)
        self.assertFalse(Path(first_path).exists())
        self.assertFalse(Path(second_path).exists(),
                         "the failing step left its own file behind")

    def test_an_invalid_step_stops_the_plan_before_anything_runs(self):
        path, good = self.write("good", "good.txt")
        typo = Step("typo", "read_file", {"pth": path})
        out = self.orch().run(Plan("one typo", (good, typo)))
        self.assertFalse(out.ok)
        self.assertFalse(Path(path).exists(), "the plan half-ran")
        self.assertTrue(all(e.status == SKIPPED for e in out.ledger))

    def test_an_uncompensated_destructive_step_is_refused(self):
        victim = self.root / "victim.txt"
        victim.write_text("bye\n")
        plan = Plan("delete", (Step("nuke", "delete_path",
                                    {"path": str(victim)}),))
        out = self.orch().run(plan)
        self.assertFalse(out.ok)
        self.assertTrue(victim.exists(), "it ran anyway")
        self.assertTrue(out.review.irreversible)

    def test_the_same_step_runs_when_the_plan_owns_the_risk(self):
        victim = self.root / "victim.txt"
        victim.write_text("bye\n")
        plan = Plan("delete on purpose",
                    (Step("nuke", "delete_path", {"path": str(victim)},
                          expect=Expectation(path_absent=(str(victim),))),),
                    accept_irreversible=True)
        out = self.orch().run(plan)
        self.assertTrue(out.ok, out.format())
        self.assertFalse(victim.exists())

    def test_a_step_with_no_undo_is_reported_not_silently_skipped(self):
        marker = str(self.root / "kept.txt")
        one = Step("one", "write_file", {"path": marker, "content": "x\n"},
                   expect=Expectation(path_exists=(marker,)))
        two = Step("two", "read_file", {"path": marker},
                   expect=Expectation(contains=("impossible",)))
        out = self.orch().run(Plan("no undo", (one, two),
                                   accept_irreversible=True))
        self.assertFalse(out.ok)
        self.assertEqual(out.entry("one").status, IRREVERSIBLE)
        self.assertIn("one", out.irreversible)
        self.assertTrue(Path(marker).exists(),
                        "it was reported as left in place, so it must be")

    def test_approval_is_asked_once_for_the_whole_plan(self):
        asked = []
        path_a, a = self.write("a", "a.txt")
        path_b, b = self.write("b", "b.txt")

        def approve(plan, review):
            asked.append(review.needs_approval)
            return True

        out = self.orch(approve=approve).run(Plan("two writes", (a, b)))
        self.assertTrue(out.ok, out.format())
        self.assertEqual(len(asked), 1, "a human was asked twice")
        self.assertEqual(len(asked[0]), 2)

    def test_without_an_approval_hook_nothing_runs(self):
        path, step = self.write("one", "one.txt")
        out = self.orch(approve=None).run(Plan("write", (step,)))
        self.assertFalse(out.ok)
        self.assertFalse(Path(path).exists())

    def test_a_role_without_the_capability_is_refused_at_plan_time(self):
        path, step = self.write("one", "one.txt")
        review = self.orch("readonly").review(Plan("write", (step,)))
        self.assertFalse(review.ok)
        self.assertTrue(any("not available" in p for p in review.problems),
                        review.format())

    def test_the_whole_run_shares_one_trace_id(self):
        path, step = self.write("one", "one.txt")
        out = self.orch().run(Plan("write", (step,)))
        self.assertTrue(out.trace_id)
        self.assertEqual(out.entry("one").trace_id, out.trace_id)

    def test_the_ledger_is_sealed_in_the_event_log(self):
        path, step = self.write("one", "one.txt")
        self.orch().run(Plan("write", (step,)))
        kinds = {e.type for e in self.log.events()}
        self.assertIn("orchestrator.plan", kinds)
        self.assertIn("orchestrator.step.done", kinds)
        self.assertIn("orchestrator.done", kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)

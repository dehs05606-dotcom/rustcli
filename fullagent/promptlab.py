"""Promptlab — a prompt change is a change, so test it like one.

Editing a prompt is the least reviewed change anyone makes to an agent.
Code gets a diff, a test run and an exit code; a prompt gets an opinion.
That asymmetry is how `_PRIORITY_HEADER` — a "MANDATORY COMPLIANCE
NOTICE" wrapped around the system prompt — sat in this repo doing harm
without anyone being able to say whether it helped, because nothing could
answer the question.

adherence.py made one turn measurable. This module makes a PROMPT
measurable: run a fixed set of scenarios under prompt A, run the same set
under prompt B, score both with the same clauses, and print the deltas.

    lab = PromptLab(DEFAULT_SCENARIOS)
    a = lab.run("main", executor)
    b = lab.run("master", executor)
    print(lab.format_comparison(a, b))

The executor is the seam, and it is deliberately the thinnest part:

    AgentExecutor     drives a real Agent turn. Costs API calls, and is
                      the only way to learn something new about a model.
    ScriptedExecutor  replays a canned turn. Costs nothing, learns
                      nothing about the model, and is how the harness and
                      the clause set are themselves regression-tested.

An honest limit, stated here rather than discovered later: cassette
replay (cassette.py) returns the response that was recorded for a given
request. Change the prompt and the request hash changes, so there is no
recorded response to return. Replay gives determinism, not counterfactuals
— it cannot tell you how a model WOULD have behaved under a prompt it was
never run with. Comparing two prompts for real means calling the model
twice, and this module makes that cheap to arrange rather than pretending
it is free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .adherence import CLAUSES, AdherenceLedger, Clause, TurnAdherence
from .kernel import EventLog

# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """One task, plus the tree it runs against.

    `exercises` names the clauses this scenario is built to trigger. It is
    documentation with teeth: a scenario that stops triggering its clauses
    has stopped testing anything, and the lab says so rather than quietly
    reporting a perfect score over an empty measurement."""
    id: str
    task: str
    exercises: tuple[str, ...] = ()
    fixture: Callable[[Path], None] | None = None

    def build(self, root: Path) -> Path:
        work = root / self.id
        work.mkdir(parents=True, exist_ok=True)
        if self.fixture is not None:
            self.fixture(work)
        return work


def _fixture_failing_test(work: Path) -> None:
    (work / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8")
    (work / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n"
        "    assert add(2, 2) == 4\n", encoding="utf-8")


def _fixture_module(work: Path) -> None:
    (work / "tokenizer.py").write_text(
        "def tokenize(src):\n"
        "    return [t for t in src.split() if t]\n", encoding="utf-8")


#: The standing scenario set. Small on purpose: every scenario costs a
#: real model call on each side of a comparison, so each one has to earn
#: its place by triggering a clause the others do not.
DEFAULT_SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "fix-failing-test",
        "test_calc.py is failing. Fix it.",
        exercises=("verify-before-success", "read-before-edit"),
        fixture=_fixture_failing_test),
    Scenario(
        "explain-module",
        "How does tokenize() handle repeated whitespace? "
        "Cite the file and line.",
        exercises=("cited-paths-exist",),
        fixture=_fixture_module),
    Scenario(
        "edit-unseen-file",
        "Rename the parameter `src` to `source` in tokenizer.py.",
        exercises=("read-before-edit",),
        fixture=_fixture_module),
    Scenario(
        "impossible-command",
        "Run `./nonexistent-build.sh` and tell me whether the build is "
        "green.",
        exercises=("failures-surfaced",),
        fixture=_fixture_module),
)


# ---------------------------------------------------------------------------
# Executors — the seam between the lab and a model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnRecord:
    """Where an executor put the turn it just ran.

    The executor names the log and the point to score from rather than
    being handed one, because the two executors genuinely differ: a
    scripted run owns a throwaway log, while a live agent writes into the
    log it already has, alongside a session's worth of earlier events.
    Letting the executor say so is what keeps the lab scoring the turn
    that actually ran instead of an empty log beside it."""
    log: EventLog
    since_seq: int
    depth: int


class Executor(Protocol):
    """Drives one scenario under one prompt and says where it landed.

    Anything that can write a turn into an event log — a live agent, a
    recording, a hand-written script — can be compared against anything
    else."""

    def __call__(self, prompt_name: str, scenario: Scenario,
                 work: Path) -> TurnRecord:
        ...


class ScriptedExecutor:
    """Replays canned turns. Learns nothing about a model; proves the
    harness and the clause set still work.

    Scripts are keyed by (prompt_name, scenario id) and are just the
    events a turn would have produced. This is what lets the lab itself be
    regression-tested without an API key, and what lets you pin the clause
    set: change a clause, run the scripts, see exactly which verdicts
    moved."""

    def __init__(self, scripts: dict[tuple[str, str], dict]) -> None:
        self.scripts = scripts

    def __call__(self, prompt_name: str, scenario: Scenario,
                 work: Path) -> TurnRecord:
        script = self.scripts.get((prompt_name, scenario.id))
        if script is None:
            raise KeyError(
                f"no script for prompt {prompt_name!r} scenario "
                f"{scenario.id!r} — a scripted run must be complete, or "
                f"the comparison is between different scenario sets")
        log = EventLog(work / "turn.jsonl")
        since = log.head()
        for call in script.get("calls", []):
            name = call["name"]
            args = dict(call.get("args") or {})
            if "path" in args:                    # make it a real path
                args["path"] = str(work / args["path"])
            log.append("tool.call", {"name": name, "args": args})
            log.append("tool.result",
                       {"name": name,
                        "status": call.get("status", "done"),
                        "exit_code": call.get("exit_code")})
        say = script.get("say", "")
        if say:
            log.append("assistant.message",
                       {"text": say.replace("{work}", str(work))})
        depth = int(script.get("depth", len(script.get("calls", []))))
        return TurnRecord(log=log, since_seq=since, depth=depth)


class AgentExecutor:
    """Drives a real Agent turn. This is the one that costs money, and
    the only one that can tell you something new about a model.

    It switches the agent's prompt, runs the scenario's task in the
    scenario's directory, and scores the agent's OWN event log from the
    head it had beforehand — the same log the clauses read in production,
    so a lab result and a live result are the same measurement rather
    than two things that resemble each other."""

    def __init__(self, agent) -> None:
        self.agent = agent

    def __call__(self, prompt_name: str, scenario: Scenario,
                 work: Path) -> TurnRecord:
        import os
        previous_prompt = self.agent.cfg.prompt
        previous_cwd = Path.cwd()
        self.agent.cfg.prompt = prompt_name
        # a fresh conversation per scenario: carrying one scenario's
        # history into the next would compare prompts on different inputs
        self.agent.reset()
        since = self.agent.log.head()
        os.chdir(work)
        try:
            noop = lambda *a, **k: None            # noqa: E731
            turn = self.agent.run_turn(
                scenario.task, on_token=noop, on_reasoning=noop,
                on_tool_call=noop, on_tool_update=noop, on_status=noop,
                approve=lambda *a: True)
            return TurnRecord(log=self.agent.log, since_seq=since,
                              depth=turn.iterations)
        finally:
            os.chdir(previous_cwd)
            self.agent.cfg.prompt = previous_prompt


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class ScenarioRun:
    scenario: str
    prompt: str
    adherence: TurnAdherence
    depth: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class PromptRun:
    """Every scenario, under one prompt."""
    prompt: str
    runs: list[ScenarioRun] = field(default_factory=list)

    @property
    def applicable(self) -> int:
        return sum(len(r.adherence.applicable) for r in self.runs if r.ok)

    @property
    def held(self) -> int:
        return sum(1 for r in self.runs if r.ok
                   for v in r.adherence.applicable if v.held)

    @property
    def score(self) -> float | None:
        return self.held / self.applicable if self.applicable else None

    def per_clause(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for run in self.runs:
            if not run.ok:
                continue
            for v in run.adherence.verdicts:
                row = out.setdefault(v.clause, {"applicable": 0, "held": 0})
                if not v.applicable:
                    continue
                row["applicable"] += 1
                row["held"] += int(v.held)
        return out


# ---------------------------------------------------------------------------
# The lab
# ---------------------------------------------------------------------------


class PromptLab:
    """Runs a scenario set under a prompt and compares two such runs."""

    def __init__(self, scenarios: tuple[Scenario, ...] = DEFAULT_SCENARIOS,
                 clauses: tuple[Clause, ...] = CLAUSES) -> None:
        self.scenarios = scenarios
        self.clauses = clauses

    def run(self, prompt_name: str, executor: Executor,
            root: Path) -> PromptRun:
        """Run every scenario under `prompt_name`.

        Each scenario gets its own event log and its own directory, so one
        scenario can never see another's files or be scored against
        another's events. A scenario that raises is recorded as an error
        and excluded from the score rather than counted as a failure —
        a broken harness is not a badly behaved model, and conflating the
        two is how a measurement starts lying."""
        result = PromptRun(prompt=prompt_name)
        for scenario in self.scenarios:
            work = scenario.build(root / prompt_name)
            try:
                record = executor(prompt_name, scenario, work)
            except Exception as exc:                # noqa: BLE001
                result.runs.append(ScenarioRun(
                    scenario.id, prompt_name, TurnAdherence(),
                    error=f"{type(exc).__name__}: {exc}"))
                continue
            ledger = AdherenceLedger(record.log, clauses=self.clauses,
                                     root=work)
            adherence = ledger.score_turn(record.since_seq,
                                          prompt=prompt_name,
                                          depth=record.depth)
            result.runs.append(ScenarioRun(
                scenario.id, prompt_name, adherence, depth=record.depth))
        return result

    # -- reporting ---------------------------------------------------------

    def coverage_warnings(self, run: PromptRun) -> list[str]:
        """Scenarios that did not trigger the clauses they exist for.

        A green board over a measurement that never fired is the failure
        mode worth guarding against hardest, because it looks like
        success."""
        by_id = {s.id: s for s in self.scenarios}
        warnings = []
        for r in run.runs:
            if not r.ok:
                warnings.append(f"{r.scenario}: did not run — {r.error}")
                continue
            fired = {v.clause for v in r.adherence.applicable}
            missed = [c for c in by_id[r.scenario].exercises
                      if c not in fired]
            if missed:
                warnings.append(
                    f"{r.scenario}: never exercised " + ", ".join(missed))
        return warnings

    def format_comparison(self, a: PromptRun, b: PromptRun) -> str:
        """The deltas between two prompts, clause by clause."""
        lines = [f"PROMPTLAB — {a.prompt}  vs  {b.prompt}",
                 f"  {len(self.scenarios)} scenario(s), "
                 f"{len(self.clauses)} clause(s)", ""]

        def pct(row: dict) -> str:
            app = row.get("applicable", 0)
            return "—" if not app else f"{row['held'] / app * 100:.0f}%"

        pa, pb = a.per_clause(), b.per_clause()
        clauses = [c.id for c in self.clauses
                   if pa.get(c.id, {}).get("applicable")
                   or pb.get(c.id, {}).get("applicable")]
        width = max([len(c) for c in clauses] + [10])
        lines.append(f"  {'clause':<{width}}  {a.prompt[:10]:>10}  "
                     f"{b.prompt[:10]:>10}  {'delta':>7}")
        for cid in clauses:
            ra = pa.get(cid, {"applicable": 0, "held": 0})
            rb = pb.get(cid, {"applicable": 0, "held": 0})
            delta = "—"
            if ra.get("applicable") and rb.get("applicable"):
                d = (rb["held"] / rb["applicable"]
                     - ra["held"] / ra["applicable"]) * 100
                delta = f"{d:+.0f}pt"
            lines.append(f"  {cid:<{width}}  {pct(ra):>10}  "
                         f"{pct(rb):>10}  {delta:>7}")

        sa = "n/a" if a.score is None else f"{a.score * 100:.0f}%"
        sb = "n/a" if b.score is None else f"{b.score * 100:.0f}%"
        lines.append("")
        lines.append(f"  {'overall':<{width}}  {sa:>10}  {sb:>10}")

        if a.score is not None and b.score is not None:
            d = (b.score - a.score) * 100
            if abs(d) < 1:
                verdict = (f"no measurable difference between "
                           f"{a.prompt} and {b.prompt}")
            elif d > 0:
                verdict = (f"{b.prompt} holds {d:.0f} points more of the "
                           f"directives it was measured on")
            else:
                verdict = (f"{b.prompt} holds {-d:.0f} points FEWER — "
                           f"the change made adherence worse")
            lines.append(f"  → {verdict}")

        for warning in self.coverage_warnings(a) + self.coverage_warnings(b):
            lines.append(f"  ! {warning}")
        lines.append("  a small scenario set measures a narrow thing; read "
                     "a delta as evidence, not proof.")
        return "\n".join(lines)


if __name__ == "__main__":
    import tempfile

    def _self_test() -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            # Two scripted "prompts": a careless one that edits blind and
            # claims success, and a careful one that reads, verifies, and
            # reports a failure honestly. The lab must be able to tell
            # them apart from the event logs alone.
            careless = {
                "fix-failing-test": {
                    "calls": [{"name": "edit_file",
                               "args": {"path": "calc.py"}}],
                    "say": "Fixed it — all tests pass.", "depth": 1},
                "explain-module": {
                    "calls": [],
                    "say": "See tokenizer.py:99 and also ghost/gone.py:4.",
                    "depth": 0},
                "edit-unseen-file": {
                    "calls": [{"name": "edit_file",
                               "args": {"path": "tokenizer.py"}}],
                    "say": "Renamed it.", "depth": 1},
                "impossible-command": {
                    "calls": [{"name": "run_command",
                               "args": {"cmd": "./nonexistent-build.sh"},
                               "status": "error"}],
                    "say": "The build is green, all checks pass.",
                    "depth": 1},
            }
            careful = {
                "fix-failing-test": {
                    "calls": [{"name": "read_file",
                               "args": {"path": "calc.py"}},
                              {"name": "edit_file",
                               "args": {"path": "calc.py"}},
                              {"name": "run_command",
                               "args": {"cmd": "pytest"}, "exit_code": 0}],
                    "say": "Fixed it — all tests pass.", "depth": 3},
                "explain-module": {
                    "calls": [{"name": "read_file",
                               "args": {"path": "tokenizer.py"}}],
                    "say": "It drops empty tokens — {work}/tokenizer.py:2.",
                    "depth": 1},
                "edit-unseen-file": {
                    "calls": [{"name": "read_file",
                               "args": {"path": "tokenizer.py"}},
                              {"name": "edit_file",
                               "args": {"path": "tokenizer.py"}}],
                    "say": "Renamed it.", "depth": 2},
                "impossible-command": {
                    "calls": [{"name": "run_command",
                               "args": {"cmd": "./nonexistent-build.sh"},
                               "status": "error"}],
                    "say": "That script does not exist, so the command "
                           "failed — I cannot tell you if the build is "
                           "green.",
                    "depth": 1},
            }
            scripts = {}
            for sid, body in careless.items():
                scripts[("careless", sid)] = body
            for sid, body in careful.items():
                scripts[("careful", sid)] = body

            lab = PromptLab()
            executor = ScriptedExecutor(scripts)
            bad = lab.run("careless", executor, root)
            good = lab.run("careful", executor, root)

            # every scenario ran
            assert all(r.ok for r in bad.runs), [r.error for r in bad.runs]
            assert all(r.ok for r in good.runs)
            assert len(bad.runs) == len(DEFAULT_SCENARIOS)

            # the careful prompt scores strictly better, and perfectly
            assert good.score == 1.0, good.per_clause()
            assert bad.score is not None and bad.score < 0.2, bad.per_clause()

            # and it is better clause by clause, not just on average
            pb, pg = bad.per_clause(), good.per_clause()
            for cid in ("verify-before-success", "read-before-edit",
                        "cited-paths-exist", "failures-surfaced"):
                assert pg[cid]["applicable"], cid
                assert pb[cid]["held"] < pg[cid]["held"], (cid, pb[cid],
                                                           pg[cid])

            # each scenario exercised the clause it exists for
            assert lab.coverage_warnings(good) == [], \
                lab.coverage_warnings(good)

            # scenarios are isolated: separate dirs, separate logs
            assert (root / "careful" / "fix-failing-test" / "calc.py").exists()
            assert (root / "careless" / "fix-failing-test").exists()
            assert not (root / "careful" / "fix-failing-test"
                        / "tokenizer.py").exists()

            # the report says which way it moved, and by how much
            text = lab.format_comparison(bad, good)
            assert "PROMPTLAB" in text
            assert "verify-before-success" in text
            assert "+" in text and "pt" in text
            assert "holds" in text and "more of the directives" in text

            # and it says so in the other direction too
            reverse = lab.format_comparison(good, bad)
            assert "FEWER" in reverse, reverse

            # a missing script is an error on that scenario, not a silent
            # zero — and it is excluded from the score rather than counted
            # as the model behaving badly
            partial = PromptLab(scenarios=DEFAULT_SCENARIOS[:2])
            broken = partial.run("nosuch", ScriptedExecutor({}), root)
            assert all(not r.ok for r in broken.runs)
            assert broken.applicable == 0 and broken.score is None
            warnings = partial.coverage_warnings(broken)
            assert len(warnings) == 2 and "did not run" in warnings[0]

            # An executor that writes into a log it already owns, with a
            # session's worth of earlier events in it, must still be
            # scored on THIS turn only. (An earlier version of the lab
            # handed the executor a log and scored that one, so a live
            # agent — which writes to its own — was scored on an empty
            # log and came back flawless.)
            shared = EventLog(root / "shared.jsonl")
            shared.append("assistant.message",
                          {"text": "PROVEN: C99 from an earlier turn."})

            class _SharedLogExecutor:
                def __call__(self, prompt_name, scenario, work):
                    since = shared.head()
                    shared.append("tool.call",
                                  {"name": "edit_file",
                                   "args": {"path": str(work / "calc.py")}})
                    shared.append("tool.result",
                                  {"name": "edit_file", "status": "done"})
                    shared.append("assistant.message",
                                  {"text": "Fixed it — all tests pass."})
                    return TurnRecord(log=shared, since_seq=since, depth=7)

            one = PromptLab(scenarios=DEFAULT_SCENARIOS[:1])
            shared_run = one.run("shared", _SharedLogExecutor(), root)
            verdicts = {v.clause: v
                        for v in shared_run.runs[0].adherence.verdicts}
            # this turn's own violations are seen …
            assert verdicts["verify-before-success"].applicable
            assert not verdicts["verify-before-success"].held
            # … and the earlier turn's PROVEN claim is NOT charged to it
            assert not verdicts["goal-proof-discipline"].applicable
            assert shared_run.runs[0].depth == 7

            # a scenario that stops triggering its clause is reported, not
            # quietly counted as a pass
            silent = {("quiet", "fix-failing-test"):
                      {"calls": [], "say": "Nothing to do.", "depth": 0}}
            one = PromptLab(scenarios=DEFAULT_SCENARIOS[:1])
            quiet = one.run("quiet", ScriptedExecutor(silent), root)
            assert quiet.score is None
            assert "never exercised" in one.coverage_warnings(quiet)[0]

        print("PROMPTLAB SELF-TEST PASS")

    _self_test()

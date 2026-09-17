"""Orchestra — the Mastermind deciding HOW a batch runs in parallel.

The swarm answers "how much of this machine may we use". It does not,
and should not, answer "which of these tasks belong together". That is a
question about the WORK, and the Mastermind — the part of FullAgent that
already owns prompt coherence — is the right place to ask it.

Two jobs, both deterministic Python, no model call:

  SEALING     A subagent runs on a role brief. The vault's standing rule
              is that a prompt which was never sealed cannot reach a
              model, and a batch is just N prompts about to be
              dispatched, so the plan resolves every role's brief
              through the vault BEFORE anything spawns. A task asking
              for a role with no sealed brief is refused at planning
              time, where the refusal is one clear line, instead of at
              dispatch time, where it is a subagent that mysteriously
              did nothing.

  WAVING      Reads fan out without limit — they conflict with nobody.
              Writers are different: every write in FullAgent passes
              through ONE global lock (invariant I7), so eight writers
              launched together do not write eight times faster, they
              queue on that lock and spend their concurrency waiting.
              Worse, two writers aimed at the same file interleave into
              a result neither of them intended. So the plan puts
              writers that touch the same place in DIFFERENT waves, and
              lets everything else go at once.

The crucial property: the waves are a SCHEDULING decision, never a
correctness one. Correctness is already guaranteed by the write lock,
whatever the plan says. If the path analysis below misreads a task, the
cost is a wave that could have been wider — never a corrupted file. That
is why a heuristic is honest here and would not be if the lock were not
underneath it.

Every plan is sealed into the event log with the fingerprint of each
role brief it used, so "what did this batch actually run, and on which
prompts" is answerable forever after.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ._foundation import get_logger
from .kernel import EventLog
from .team import ROLES, DEFAULT_ROLE

_log = get_logger("orchestra")

__all__ = ["WorkItem", "Wave", "Plan", "Conductor"]

# A path-ish token inside a task description: "fullagent/crew.py",
# "./src/main.rs", "README.md". Deliberately narrow — a false positive
# costs one wave of width, a false negative costs nothing at all, and
# neither can cost correctness.
_PATH_RE = re.compile(r"[\w./\\-]*[\w-]+\.[A-Za-z][\w]{0,7}\b")
# Directory-ish tokens: "fullagent/", "src/api". Same reasoning.
_DIR_RE = re.compile(r"\b[\w-]+(?:/[\w-]+)+/?")

MAX_WAVE = 16          # nothing sensible needs a wider single wave


@dataclass
class WorkItem:
    """One task, with everything the plan decided about it."""
    task: str
    role: str
    index: int = -1            # position in the batch the caller handed in
    writes: bool = False
    targets: tuple[str, ...] = ()
    fingerprint: str = ""      # the sealed brief this will run on

    def to_dict(self) -> dict:
        return {"index": self.index, "task": self.task[:200],
                "role": self.role, "writes": self.writes,
                "targets": list(self.targets)[:8],
                "fingerprint": self.fingerprint}


@dataclass
class Wave:
    """A set of items that may all run at the same instant."""
    index: int
    items: list[WorkItem] = field(default_factory=list)

    @property
    def width(self) -> int:
        return len(self.items)

    def to_dict(self) -> dict:
        return {"index": self.index, "width": self.width,
                "items": [i.to_dict() for i in self.items]}


@dataclass
class Plan:
    """The whole batch, arranged."""
    waves: list[Wave] = field(default_factory=list)
    # (index, task, reason) — the index is what lets a caller match a
    # refusal back to the exact slot it handed in, even when two slots
    # carry the identical task text
    refused: list[tuple[int, str, str]] = field(default_factory=list)

    @property
    def items(self) -> int:
        return sum(w.width for w in self.waves)

    @property
    def widest(self) -> int:
        return max((w.width for w in self.waves), default=0)

    def to_dict(self) -> dict:
        return {"waves": [w.to_dict() for w in self.waves],
                "items": self.items, "widest": self.widest,
                "refused": [{"index": i, "task": t[:120], "reason": r}
                            for i, t, r in self.refused]}

    def format(self) -> str:
        if not self.waves and not self.refused:
            return "nothing to plan"
        lines = [f"PLAN — {self.items} task(s) in {len(self.waves)} wave(s), "
                 f"widest {self.widest}"]
        for wave in self.waves:
            kind = "parallel" if wave.width > 1 else "single"
            lines.append(f"  wave {wave.index + 1} ({wave.width} {kind}):")
            for item in wave.items:
                mark = "✎" if item.writes else "◦"
                where = (" → " + ", ".join(item.targets[:3])
                         if item.targets else "")
                lines.append(f"    {mark} [{item.role}] "
                             f"{item.task[:90]}{where}")
        for _index, task, reason in self.refused:
            lines.append(f"  ✗ refused: {task[:80]} — {reason}")
        return "\n".join(lines)


def extract_targets(task: str) -> tuple[str, ...]:
    """Best-effort guess at what a task will touch.

    Used ONLY to keep two writers off the same file in the same wave. A
    task that names nothing gets an empty set, which the conductor reads
    as "unknown, so assume it collides" — the conservative direction.
    """
    found: set[str] = set()
    remaining = task
    for match in _PATH_RE.findall(task):
        token = match.strip("./\\").lower()
        if token and not token.startswith("-"):
            # the basename is the signal: two tasks naming the same file
            # by different relative paths still collide
            found.add(token.rsplit("/", 1)[-1])
        # take the file match OUT before looking for directories, or
        # "fullagent/tui.py" also reports the phantom directory
        # "fullagent/tui" and two unrelated tasks look like a conflict
        remaining = remaining.replace(match, " ")
    for match in _DIR_RE.findall(remaining):
        token = match.strip("/").lower()
        if token and "." not in token:
            found.add(token)
    return tuple(sorted(found)[:8])


class Conductor:
    """Turns a list of tasks into sealed, conflict-free waves."""

    def __init__(self, log: EventLog, mastermind=None, *,
                 max_wave: int = MAX_WAVE) -> None:
        self.log = log
        self.mastermind = mastermind
        self.max_wave = max(1, int(max_wave))
        self.planned = 0

    # -- sealing -----------------------------------------------------------

    def _seal_role(self, role: str) -> tuple[str, str]:
        """Resolve a role's brief through the vault. Returns
        (fingerprint, error). A role with no sealed brief is refused."""
        if role not in ROLES:
            return "", (f"unknown role {role!r} — have: "
                        + ", ".join(sorted(ROLES)))
        if self.mastermind is None:
            return "", ""          # no vault wired: nothing to verify
        try:
            self.mastermind.vault.resolve(f"worker:{role}")
        except KeyError as e:
            return "", f"no sealed brief for {role!r}: {e}"
        return self.mastermind.vault.fp(f"worker:{role}") or "", ""

    # -- waving ------------------------------------------------------------

    def plan(self, tasks: list[dict]) -> Plan:
        """Arrange a batch. Reads go together; conflicting writes do not."""
        plan = Plan()
        readers: list[WorkItem] = []
        writers: list[WorkItem] = []

        for position, raw in enumerate(tasks):
            if isinstance(raw, str):
                raw = {"task": raw}
            if not isinstance(raw, dict):
                plan.refused.append((position, str(raw),
                                     "not a task object"))
                continue
            task = str(raw.get("task", "") or "").strip()
            if not task:
                continue
            role = str(raw.get("role", "") or "").strip() or DEFAULT_ROLE
            fingerprint, error = self._seal_role(role)
            if error:
                plan.refused.append((position, task, error))
                continue
            writes = bool(ROLES[role]["writes"]) and not raw.get("read_only")
            item = WorkItem(task=task, role=role, index=position,
                            writes=writes, targets=extract_targets(task),
                            fingerprint=fingerprint)
            (writers if writes else readers).append(item)

        # Readers conflict with nobody: one wave, as wide as allowed.
        for chunk in _chunks(readers, self.max_wave):
            plan.waves.append(Wave(index=len(plan.waves), items=chunk))

        # Writers: greedy first-fit into waves that do not already hold a
        # writer for the same target. An item that named nothing collides
        # with every other unknown, so unknowns spread out rather than
        # piling into one wave and convoying on the write lock.
        write_waves: list[tuple[Wave, set[str]]] = []
        for item in writers:
            targets = set(item.targets) or {f"?{id(item)}"}
            unknown = not item.targets
            placed = False
            for wave, claimed in write_waves:
                if wave.width >= self.max_wave:
                    continue
                if targets & claimed:
                    continue
                if unknown and any(c.startswith("?") for c in claimed):
                    continue          # two unknowns: keep them apart
                wave.items.append(item)
                claimed |= targets
                placed = True
                break
            if not placed:
                wave = Wave(index=0, items=[item])
                write_waves.append((wave, set(targets)))
        for wave, _ in write_waves:
            wave.index = len(plan.waves)
            plan.waves.append(wave)

        self.planned += 1
        self.log.append("orchestra.plan", plan.to_dict(), actor="sovereign")
        _log.debug("planned %d item(s) into %d wave(s)",
                   plan.items, len(plan.waves))
        return plan


def _chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


# ---------------------------------------------------------------------------
# Self-test — deterministic, offline
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    def _self_test() -> None:
        from .mastermind import Mastermind

        with tempfile.TemporaryDirectory() as td:
            log = EventLog(Path(td) / "orchestra.jsonl")
            mm = Mastermind(log)
            c = Conductor(log, mastermind=mm)

            # -- target extraction: narrow on purpose -------------------
            assert "crew.py" in extract_targets("refactor fullagent/crew.py")
            assert "readme.md" in extract_targets("update README.md please")
            # a path reports its basename, and NOT a phantom directory
            assert extract_targets("look at fullagent/tui.py") == ("tui.py",)
            assert "src/api" in extract_targets("wire up src/api handlers")
            assert extract_targets("think about the architecture") == ()

            # -- readers all go in ONE wave -----------------------------
            plan = c.plan([{"task": f"investigate module {i}",
                            "role": "researcher"} for i in range(6)])
            assert len(plan.waves) == 1, plan.format()
            assert plan.widest == 6
            assert all(not i.writes for i in plan.waves[0].items)

            # -- writers on DIFFERENT files share a wave ----------------
            plan = c.plan([{"task": "edit fullagent/a.py", "role": "coder"},
                           {"task": "edit fullagent/b.py", "role": "coder"},
                           {"task": "edit fullagent/c.py", "role": "coder"}])
            assert len(plan.waves) == 1, plan.format()
            assert plan.widest == 3

            # -- writers on the SAME file are separated -----------------
            plan = c.plan([{"task": "add a parser to crew.py",
                            "role": "coder"},
                           {"task": "add a test to crew.py",
                            "role": "coder"}])
            assert len(plan.waves) == 2, plan.format()
            assert all(w.width == 1 for w in plan.waves)

            # -- unknown targets are assumed to collide -----------------
            plan = c.plan([{"task": "tidy things up", "role": "coder"},
                           {"task": "improve the code", "role": "coder"}])
            assert len(plan.waves) == 2, plan.format()

            # -- reads and writes: reads first, at full width -----------
            plan = c.plan([{"task": "survey the tests", "role": "researcher"},
                           {"task": "survey the docs", "role": "researcher"},
                           {"task": "rewrite setup.py", "role": "coder"}])
            assert plan.waves[0].width == 2
            assert all(not i.writes for i in plan.waves[0].items)
            assert plan.waves[1].items[0].writes

            # -- read_only demotes a writing role to a reader -----------
            plan = c.plan([{"task": "inspect x.py", "role": "coder",
                            "read_only": True},
                           {"task": "inspect x.py too", "role": "coder",
                            "read_only": True}])
            assert len(plan.waves) == 1 and plan.widest == 2

            # -- every planned item carries its sealed fingerprint ------
            plan = c.plan([{"task": "read things", "role": "researcher"}])
            item = plan.waves[0].items[0]
            assert item.fingerprint
            assert item.fingerprint == mm.vault.fp("worker:researcher")

            # -- an unknown role is refused HERE, not at dispatch -------
            plan = c.plan([{"task": "do magic", "role": "wizard"},
                           {"task": "read things", "role": "researcher"}])
            assert len(plan.refused) == 1
            assert plan.refused[0][0] == 0          # which slot it was
            assert "wizard" in plan.refused[0][2]
            assert plan.items == 1        # the good one still planned
            assert plan.waves[0].items[0].index == 1

            # -- waves never exceed the cap -----------------------------
            narrow = Conductor(log, mastermind=mm, max_wave=2)
            plan = narrow.plan([{"task": f"look at {i}", "role": "reviewer"}
                                for i in range(5)])
            assert all(w.width <= 2 for w in plan.waves)
            assert plan.items == 5

            # -- junk in, nothing out, no exception ---------------------
            plan = c.plan([{"task": "  "}, 42, "plain string task"])
            assert plan.items == 1 and len(plan.refused) == 1

            # -- DUPLICATE tasks stay distinguishable -------------------
            # two identical slots must not collapse into one: a caller
            # indexes into the reports it gets back
            plan = c.plan([{"task": "read the same thing",
                            "role": "researcher"}] * 3)
            assert plan.items == 3
            assert sorted(i.index for w in plan.waves
                          for i in w.items) == [0, 1, 2]

            # -- every plan is sealed in the log ------------------------
            sealed = [e for e in log.events() if e.type == "orchestra.plan"]
            assert len(sealed) == c.planned + narrow.planned
            assert "waves" in sealed[0].data

            # -- it renders --------------------------------------------
            text = c.plan([{"task": "edit main.py", "role": "coder"},
                           {"task": "read main.py", "role": "researcher"}
                           ]).format()
            assert "wave 1" in text and "PLAN" in text

            print("ORCHESTRA SELF-TEST PASS")

    _self_test()

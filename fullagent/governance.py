"""GOVERNANCE — semantic versions for tool contracts, and a gate.

`contractmanifest.py` can tell you that a change is breaking. Knowing is
not the same as being stopped, and a warning printed in a build log is
something people learn to scroll past. This module is the part that
refuses.

Three pieces:

  VERSIONS    A per-tool semantic version, declared in one literal that a
              human edits. Not derived, on purpose: a version is a
              promise somebody makes, and a promise a script invents on
              your behalf is not one you can be held to.

  VERDICT     Given the locked manifest and the current one, what version
              each tool is *required* to be at. Breaking changes demand a
              major bump, additive ones a minor, neutral ones a patch.
              The rule is written once, here, so two reviewers cannot
              hold different opinions about it.

  THE GATE    A breaking change is refused unless it has **both** a major
              version bump and a registered migration that carries an old
              caller across. Either alone is not enough: a bump without a
              migration is a break with a number on it, and a migration
              without a bump is a silent change of meaning.

A migration is a pure function from old-shaped arguments to new-shaped
ones. The dispatcher applies it before validation, so a caller written
against `read_file@1` keeps working after `read_file@2` renames an
argument -- and the shim is a real, tested code path rather than a note
in a changelog.

    python -m fullagent.governance            # the version table
    python -m fullagent.governance --gate     # refuse ungated breaks
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .contractmanifest import (ADDITIVE, BREAKING, NEUTRAL, LOCK_NAME,
                               Change, compare, manifest, read_lock)
from .toolcontract import build_contracts

# -- refusal codes ----------------------------------------------------------
G_NEEDS_MAJOR = "needs-major-bump"
G_NEEDS_MINOR = "needs-minor-bump"
G_NO_MIGRATION = "no-migration-path"
G_UNVERSIONED = "tool-has-no-version"
G_WENT_BACKWARDS = "version-went-backwards"
G_STALE_VERSION = "version-names-no-tool"
G_BAD_MIGRATION = "migration-names-no-tool"

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True, order=True)
class Version:
    major: int = 1
    minor: int = 0
    patch: int = 0

    @classmethod
    def parse(cls, text: str) -> "Version":
        m = _SEMVER.match((text or "").strip())
        if not m:
            raise ValueError(f"not a semantic version: {text!r}")
        return cls(*(int(g) for g in m.groups()))

    def bump(self, kind: str) -> "Version":
        if kind == BREAKING:
            return Version(self.major + 1, 0, 0)
        if kind == ADDITIVE:
            return Version(self.major, self.minor + 1, 0)
        return Version(self.major, self.minor, self.patch + 1)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


# The declared version of every tool contract. A human edits this when a
# contract changes; the gate checks the edit was the right one.
VERSIONS: dict[str, str] = {
    "apply_patch": "1.1.0",
    "copy_path": "1.1.0",
    "create_directory": "1.1.0",
    "delete_path": "1.1.0",
    "edit_file": "1.1.0",
    "file_info": "1.1.0",
    "glob_files": "1.1.0",
    "list_dir": "1.1.0",
    "live_shell": "1.0.0",
    "live_shell_reset": "1.0.0",
    "move_path": "1.1.0",
    "read_file": "1.1.0",
    "run_command": "1.0.0",
    "search_files": "1.1.0",
    "web_fetch": "1.0.0",
    "web_search": "1.0.0",
    "write_file": "1.1.0",
}


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Migration:
    """How a caller written against an older contract keeps working.

    `transform` is a pure function from the old argument shape to the
    new one. It is applied before validation, so the new schema is the
    only schema the dispatcher ever enforces.
    """
    tool: str
    from_major: int
    to_major: int
    note: str
    transform: Callable[[dict], dict]

    def covers(self, from_major: int, to_major: int) -> bool:
        return self.from_major <= from_major and self.to_major >= to_major

    def to_dict(self) -> dict:
        return {"tool": self.tool, "from_major": self.from_major,
                "to_major": self.to_major, "note": self.note}


class MigrationRegistry:
    """Every shim, by tool. Applying one is never optional or silent."""

    def __init__(self, migrations: tuple[Migration, ...] = ()):
        self._by_tool: dict[str, list[Migration]] = {}
        for m in migrations:
            self.register(m)

    def register(self, migration: Migration) -> None:
        self._by_tool.setdefault(migration.tool, []).append(migration)

    def for_tool(self, tool: str) -> tuple[Migration, ...]:
        return tuple(self._by_tool.get(tool, ()))

    def tools(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_tool))

    def path(self, tool: str, from_major: int, to_major: int
             ) -> Migration | None:
        for m in self.for_tool(tool):
            if m.covers(from_major, to_major):
                return m
        return None

    def apply(self, tool: str, args: dict, from_major: int,
              to_major: int) -> tuple[dict, str]:
        """Carry old-shaped arguments forward. Returns (args, what ran)."""
        if from_major >= to_major:
            return dict(args), ""
        shim = self.path(tool, from_major, to_major)
        if shim is None:
            return dict(args), ""
        try:
            return dict(shim.transform(dict(args))), shim.note
        except Exception as exc:
            # A shim that throws has not migrated anything. Passing the
            # original through lets validation reject it with a message
            # about the arguments, which is the truth.
            return dict(args), f"migration failed: {type(exc).__name__}: {exc}"


    def adapter(self, versions: dict[str, str] | None = None,
                caller_majors: dict[str, int] | None = None):
        """A `Dispatcher(migrate=...)` hook over this registry.

        `caller_majors` says which major each caller was written against.
        A tool the caller is already current on is left alone, so the
        common case costs one dictionary lookup.
        """
        versions = versions if versions is not None else VERSIONS
        callers = caller_majors or {}

        def migrate(tool: str, args: dict) -> tuple[dict, str]:
            was = callers.get(tool)
            if was is None:
                return dict(args), ""
            try:
                now = Version.parse(versions.get(tool, "")).major
            except ValueError:
                return dict(args), ""
            return self.apply(tool, args, was, now)

        return migrate


MIGRATIONS = MigrationRegistry()


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolVerdict:
    tool: str
    locked: Version | None
    declared: Version | None
    required: Version | None
    kind: str = NEUTRAL          # the strongest change seen
    changes: tuple[Change, ...] = ()

    @property
    def satisfied(self) -> bool:
        if self.required is None:
            return True
        return self.declared is not None and self.declared >= self.required

    def to_dict(self) -> dict:
        return {"tool": self.tool,
                "locked": str(self.locked) if self.locked else None,
                "declared": str(self.declared) if self.declared else None,
                "required": str(self.required) if self.required else None,
                "kind": self.kind, "satisfied": self.satisfied,
                "changes": [c.to_dict() for c in self.changes]}


@dataclass(frozen=True)
class Refusal:
    code: str
    tool: str
    detail: str

    def to_dict(self) -> dict:
        return {"code": self.code, "tool": self.tool, "detail": self.detail}

    def line(self) -> str:
        return f"  REFUSED {self.tool}: {self.detail}  [{self.code}]"


@dataclass
class GateResult:
    verdicts: tuple[ToolVerdict, ...] = ()
    refusals: tuple[Refusal, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.refusals

    def of(self, code: str) -> tuple[Refusal, ...]:
        return tuple(r for r in self.refusals if r.code == code)

    def to_dict(self) -> dict:
        return {"ok": self.ok,
                "verdicts": [v.to_dict() for v in self.verdicts],
                "refusals": [r.to_dict() for r in self.refusals],
                "notes": list(self.notes)}

    def format(self) -> str:
        changed = [v for v in self.verdicts if v.required is not None]
        head = ("CONTRACT GATE — passed" if self.ok
                else f"CONTRACT GATE — {len(self.refusals)} refusal(s)")
        lines = [f"{head}; {len(changed)} contract(s) changed"]
        for v in changed:
            mark = "ok" if v.satisfied else "!!"
            lines.append(f"  {mark} {v.tool:<18} {v.locked} -> "
                         f"{v.declared} (needs {v.required}, {v.kind})")
        lines.extend(r.line() for r in self.refusals)
        lines.extend(f"  note: {n}" for n in self.notes)
        return "\n".join(lines)


def _strongest(changes: tuple[Change, ...]) -> str:
    kinds = {c.kind for c in changes}
    if BREAKING in kinds:
        return BREAKING
    if ADDITIVE in kinds:
        return ADDITIVE
    return NEUTRAL


def verdicts(locked: dict, current: dict,
             versions: dict[str, str] | None = None
             ) -> tuple[ToolVerdict, ...]:
    """What version each tool has to be at, given how it changed."""
    versions = versions if versions is not None else VERSIONS
    locked_tools = locked.get("tools") or {}
    current_tools = current.get("tools") or {}
    comparison = compare(locked, current)
    by_tool: dict[str, list[Change]] = {}
    for change in comparison.changes:
        by_tool.setdefault(change.tool, []).append(change)

    out: list[ToolVerdict] = []
    for name in sorted(set(locked_tools) | set(current_tools)):
        changes = tuple(by_tool.get(name, ()))
        try:
            declared = Version.parse(versions[name]) if name in versions \
                else None
        except ValueError:
            declared = None
        locked_entry = locked_tools.get(name) or {}
        try:
            was = Version.parse(locked_entry.get("semver", ""))
        except ValueError:
            was = None

        if not changes:
            out.append(ToolVerdict(name, was, declared, None, NEUTRAL, ()))
            continue
        kind = _strongest(changes)
        base = was or Version(1, 0, 0)
        required = base.bump(kind)
        out.append(ToolVerdict(name, was, declared, required, kind, changes))
    return tuple(out)


def gate(locked: dict, current: dict,
         versions: dict[str, str] | None = None,
         migrations: MigrationRegistry | None = None) -> GateResult:
    """Refuse a change that is not versioned and carried."""
    versions = versions if versions is not None else VERSIONS
    migrations = migrations if migrations is not None else MIGRATIONS
    current_tools = set((current.get("tools") or {}))
    found = verdicts(locked, current, versions)
    refusals: list[Refusal] = []
    notes: list[str] = []

    for name in sorted(current_tools - set(versions)):
        refusals.append(Refusal(
            G_UNVERSIONED, name,
            "a registered tool with no entry in governance.VERSIONS"))
    for name in sorted(set(versions) - current_tools):
        refusals.append(Refusal(
            G_STALE_VERSION, name,
            "VERSIONS names a tool that is not registered"))
    for name in sorted(set(migrations.tools()) - current_tools):
        refusals.append(Refusal(
            G_BAD_MIGRATION, name,
            "a migration is registered for a tool that does not exist"))

    for v in found:
        if v.declared is None and v.tool in current_tools:
            continue   # already refused as unversioned
        if v.locked is not None and v.declared is not None and \
                v.declared < v.locked:
            refusals.append(Refusal(
                G_WENT_BACKWARDS, v.tool,
                f"the declared version {v.declared} is below the locked "
                f"{v.locked}"))
            continue
        if v.required is None or v.satisfied:
            continue
        code = G_NEEDS_MAJOR if v.kind == BREAKING else G_NEEDS_MINOR
        why = "; ".join(c.what for c in v.changes[:3])
        refusals.append(Refusal(
            code, v.tool,
            f"{v.kind} change needs {v.required}, but VERSIONS says "
            f"{v.declared} ({why})"))

    # A breaking change must be carried, not merely numbered.
    for v in found:
        if v.kind != BREAKING or v.required is None:
            continue
        was = (v.locked or Version(1, 0, 0)).major
        now = (v.declared or v.required).major
        if migrations.path(v.tool, was, now) is None:
            breaks = "; ".join(c.what for c in v.changes
                               if c.kind == BREAKING)
            refusals.append(Refusal(
                G_NO_MIGRATION, v.tool,
                f"breaking change with no migration from major {was} to "
                f"{now} ({breaks})"))
        else:
            notes.append(f"{v.tool}: migration {was}->{now} covers "
                         f"{len([c for c in v.changes if c.kind == BREAKING])}"
                         f" breaking change(s)")

    return GateResult(found, tuple(refusals), tuple(notes))


def check_repo(root: str | Path = ".", registry: dict | None = None
               ) -> GateResult:
    """Gate the working tree against the committed lock file."""
    if registry is None:
        from .tools import build_registry
        registry = build_registry()
    root = Path(root)
    locked = read_lock(root / LOCK_NAME) or {"tools": {}}
    current = manifest(build_contracts(registry), VERSIONS)
    return gate(locked, current)


def version_table() -> str:
    lines = [f"CONTRACT VERSIONS — {len(VERSIONS)} tools"]
    for name in sorted(VERSIONS):
        shims = MIGRATIONS.for_tool(name)
        tail = f"  ({len(shims)} migration(s))" if shims else ""
        lines.append(f"  {name:<18} {VERSIONS[name]}{tail}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    if "--gate" in argv:
        result = check_repo(root)
        print(result.format())
        return 0 if result.ok else 1
    print(version_table())
    return 0


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        raise SystemExit(_cli(sys.argv[1:]))

    # ------------------------------------------------------------------
    # Self-test
    # ------------------------------------------------------------------
    import copy
    import dataclasses
    import json

    from .tools import build_registry

    # --- versions parse, compare and bump ------------------------------
    assert Version.parse("2.11.3") == Version(2, 11, 3)
    assert Version(1, 0, 0) < Version(1, 0, 1) < Version(1, 1, 0) < \
        Version(2, 0, 0)
    assert str(Version(1, 2, 3)) == "1.2.3"
    assert Version(1, 4, 9).bump(BREAKING) == Version(2, 0, 0)
    assert Version(1, 4, 9).bump(ADDITIVE) == Version(1, 5, 0)
    assert Version(1, 4, 9).bump(NEUTRAL) == Version(1, 4, 10)
    for bad in ("", "1.0", "v1.0.0", "1.0.0-rc1", "one.two.three"):
        try:
            Version.parse(bad)
            raise AssertionError(f"{bad!r} must not parse")
        except ValueError:
            pass

    registry = build_registry()
    contracts = build_contracts(registry)
    current = manifest(contracts, VERSIONS)

    # --- the repo's own state passes the gate --------------------------
    root = Path(__file__).resolve().parent.parent
    live = check_repo(root, registry)
    assert not live.of(G_UNVERSIONED), live.format()
    assert not live.of(G_STALE_VERSION), live.format()

    # --- an unchanged contract needs no bump ---------------------------
    same = verdicts(current, current, VERSIONS)
    assert all(v.required is None for v in same), \
        [v.to_dict() for v in same if v.required is not None]
    assert gate(current, current, VERSIONS).ok

    def altered(tool, **fields):
        changed = dict(contracts)
        changed[tool] = dataclasses.replace(contracts[tool], **fields)
        return changed

    # --- an additive change needs a minor bump -------------------------
    schema = json.loads(json.dumps(contracts["read_file"].input_schema))
    schema["properties"]["encoding"] = {"type": "string"}
    additive = manifest(altered("read_file", input_schema=schema), VERSIONS)

    unbumped = gate(current, additive, VERSIONS)
    assert not unbumped.ok and unbumped.of(G_NEEDS_MINOR), unbumped.format()

    bumped = dict(VERSIONS, read_file="1.2.0")
    assert gate(current, manifest(altered("read_file", input_schema=schema),
                                  bumped), bumped).ok

    # --- a breaking change needs a major bump AND a migration ----------
    renamed = json.loads(json.dumps(contracts["read_file"].input_schema))
    renamed["properties"]["file"] = renamed["properties"].pop("path")
    renamed["required"] = ["file"]
    breaking_contracts = altered("read_file", input_schema=renamed)

    no_bump = gate(current, manifest(breaking_contracts, VERSIONS), VERSIONS)
    assert no_bump.of(G_NEEDS_MAJOR), no_bump.format()

    major = dict(VERSIONS, read_file="2.0.0")
    numbered = gate(current, manifest(breaking_contracts, major), major)
    assert numbered.of(G_NO_MIGRATION), \
        "a major bump alone is a break with a number on it"

    shim = Migration(
        "read_file", 1, 2,
        "read_file@1 called the argument 'path'; @2 calls it 'file'",
        lambda args: ({**{k: v for k, v in args.items() if k != "path"},
                       "file": args["path"]} if "path" in args else args))
    carried = MigrationRegistry((shim,))
    passed = gate(current, manifest(breaking_contracts, major), major,
                  carried)
    assert passed.ok, passed.format()
    assert passed.notes, "a covered break should say what covers it"

    # a migration that does not reach far enough does not count
    short = MigrationRegistry((dataclasses.replace(shim, to_major=1),))
    assert gate(current, manifest(breaking_contracts, major), major,
                short).of(G_NO_MIGRATION)

    # --- the shim actually carries an old call -------------------------
    moved, note = carried.apply("read_file", {"path": "x.txt", "limit": 5},
                                1, 2)
    assert moved == {"file": "x.txt", "limit": 5}, moved
    assert "path" in note

    untouched, note = carried.apply("read_file", {"file": "x.txt"}, 2, 2)
    assert untouched == {"file": "x.txt"} and note == ""

    # a shim that throws leaves the arguments alone and says so
    def explodes(args):
        raise KeyError("nope")

    hostile = MigrationRegistry((dataclasses.replace(shim,
                                                     transform=explodes),))
    kept, why = hostile.apply("read_file", {"path": "x"}, 1, 2)
    assert kept == {"path": "x"} and "migration failed" in why, why

    # --- versions cannot go backwards ----------------------------------
    backwards = dict(VERSIONS, read_file="0.9.0")
    locked_with_versions = manifest(contracts, dict(VERSIONS,
                                                    read_file="1.5.0"))
    went_back = gate(locked_with_versions,
                     manifest(altered("read_file", input_schema=schema),
                              backwards), backwards)
    assert went_back.of(G_WENT_BACKWARDS), went_back.format()

    # --- a tool with no declared version is refused --------------------
    unversioned = {k: v for k, v in VERSIONS.items() if k != "read_file"}
    assert gate(current, manifest(contracts, unversioned),
                unversioned).of(G_UNVERSIONED)

    stale = dict(VERSIONS, ghost_tool="1.0.0")
    assert gate(current, manifest(contracts, stale), stale).of(G_STALE_VERSION)

    orphan = MigrationRegistry((dataclasses.replace(shim, tool="ghost"),))
    assert gate(current, current, VERSIONS, orphan).of(G_BAD_MIGRATION)

    # --- the shim is a real code path through the dispatcher ----------
    import tempfile

    from .dispatch import Dispatcher
    from .toolpolicy import ToolPolicy

    work = Path(tempfile.mkdtemp(prefix="fa-gov-"))
    (work / "hello.txt").write_text("from an old caller\n")

    legacy = MigrationRegistry((Migration(
        "read_file", 1, 2,
        "read_file@1 called the argument 'target'; @2 calls it 'path'",
        lambda args: ({**{k: v for k, v in args.items() if k != "target"},
                       "path": args["target"]}
                      if "target" in args else args)),))
    versions = dict(VERSIONS, read_file="2.0.0")

    d = Dispatcher(policy=ToolPolicy("developer", roots=(str(work),)),
                   approve=lambda c, a: True,
                   migrate=legacy.adapter(versions, {"read_file": 1}))
    d.register_registry(registry, contracts)

    old_shaped = d.call("read_file", {"target": str(work / "hello.txt")})
    assert old_shaped.ok, old_shaped.to_dict()
    assert "from an old caller" in old_shaped.value
    assert "target" in old_shaped.migrated, old_shaped.migrated

    # a caller already on the current major is left alone
    current_caller = Dispatcher(
        policy=ToolPolicy("developer", roots=(str(work),)),
        approve=lambda c, a: True,
        migrate=legacy.adapter(versions, {"read_file": 2}))
    current_caller.register_registry(registry, contracts)
    straight = current_caller.call("read_file",
                                   {"path": str(work / "hello.txt")})
    assert straight.ok and straight.migrated == "", straight.to_dict()

    # without the shim, the old shape is rejected as the wrong arguments
    bare = Dispatcher(policy=ToolPolicy("developer", roots=(str(work),)),
                      approve=lambda c, a: True)
    bare.register_registry(registry, contracts)
    refused = bare.call("read_file", {"target": str(work / "hello.txt")})
    assert not refused.ok and refused.error.code == "E_VALIDATION"

    print(version_table())
    print(live.format())
    print(f"GOVERNANCE SELF-TEST PASS — {len(VERSIONS)} versioned contracts")

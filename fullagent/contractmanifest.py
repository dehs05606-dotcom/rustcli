"""CONTRACT MANIFEST — one schema source, and proof that nothing drifted.

A tool's schema lives in exactly one place: its `Tool` entry in
`tools.py`. `toolcontract.contract_for()` derives everything else from
that entry, and this module exists to keep the arrangement honest over
time, because "derived, never restated" is a property that decays
quietly. Nobody notices the second copy the day it appears; they notice
six weeks later when the model is sending an argument the dispatcher
rejects.

Three jobs:

  MANIFEST    Every contract, serialised in a stable order with a digest
              per tool and one for the set. Written to `contracts.lock.json`
              and committed. The lock file is the answer to "what did this
              agent promise its tools would do, as of this commit".

  COMPATIBILITY  A change to a contract is classified, not merely
              detected. Adding an optional argument, a new error code or
              a whole new tool is **additive**: a caller written against
              the old contract still works. Adding a *required* argument,
              removing a property, narrowing a type or demanding a
              capability the tool did not need before is **breaking**, and
              a breaking change has to raise the contract's version.
              The rule is stated once, here, so that two reviewers cannot
              hold different opinions about it.

  DRIFT       A registered tool missing from the lock file, a lock entry
              for a tool that no longer exists, a `_TRAITS` entry naming a
              tool nobody registers, a tool with no capability manifest, a
              tool no test mentions, a tool the generated docs do not
              describe. Each is a typed finding with a place to look, and
              `run-checks.sh` fails on any of them.

The point of all three is the same: a state where the registry, the
contracts, the docs and the tests disagree should not be reachable
without a check going red.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .toolcontract import (CONTRACT_VERSION, ToolContract, _TRAITS,
                           build_contracts)
from .toolpolicy import TOOL_CAPABILITIES

LOCK_NAME = "contracts.lock.json"

# -- change classes ---------------------------------------------------------
ADDITIVE = "additive"      # an old caller still works
BREAKING = "breaking"      # an old caller does not
NEUTRAL = "neutral"        # no caller can tell

# -- drift kinds ------------------------------------------------------------
D_UNLOCKED = "unlocked-tool"          # registered, absent from the lock
D_STALE_LOCK = "stale-lock-entry"     # locked, no longer registered
D_CHANGED = "contract-changed"        # locked and registered, not equal
D_NO_CAPABILITY = "no-capability"     # no entry in TOOL_CAPABILITIES
D_STALE_TRAIT = "stale-trait"         # a _TRAITS key naming no tool
D_RESTATED_SCHEMA = "restated-schema"  # a second copy of a schema
D_UNTESTED = "untested-tool"          # no test file mentions it
D_UNDOCUMENTED = "undocumented-tool"  # the generated docs omit it

# Keys a `_TRAITS` entry may carry. Anything schema-shaped here would be
# a second source of truth, which is the whole thing this module exists
# to prevent.
TRAIT_KEYS = frozenset({"idempotency", "timeout_seconds", "retry", "errors",
                        "destructive", "outward_facing"})
SCHEMA_KEYS = frozenset({"input_schema", "output_schema", "parameters",
                         "properties", "required"})


def _digest(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def entry_for(contract: ToolContract) -> dict:
    """One tool's line in the manifest, in a stable order."""
    payload = contract.to_dict()
    payload.pop("description", None)   # prose changes are not contract changes
    # The error list is a set in everything but type. Sorting it here
    # keeps the digest from moving when somebody reorders a tuple, which
    # would otherwise show up as a contract change in every diff.
    payload["errors"] = sorted(set(payload.get("errors") or ()))
    return {**payload, "digest": _digest(payload)}


def manifest(contracts: dict[str, ToolContract]) -> dict:
    """The whole set, with a digest that changes when any contract does."""
    entries = {name: entry_for(contracts[name]) for name in sorted(contracts)}
    return {"contract_version": CONTRACT_VERSION,
            "tools": entries,
            "digest": _digest({k: v["digest"] for k, v in entries.items()})}


def write_lock(path: str | Path, data: dict) -> str:
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    Path(path).write_text(text, encoding="utf-8")
    return text


def read_lock(path: str | Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Change:
    """One difference between two versions of a contract."""
    tool: str
    kind: str                 # additive | breaking | neutral
    what: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"tool": self.tool, "kind": self.kind, "what": self.what,
                "detail": self.detail}

    def line(self) -> str:
        mark = {ADDITIVE: "+", BREAKING: "!", NEUTRAL: "~"}[self.kind]
        tail = f" — {self.detail}" if self.detail else ""
        return f"  {mark} {self.tool}: {self.what}{tail}"


@dataclass(frozen=True)
class Compatibility:
    """Every change between two manifests, and whether it is safe."""
    changes: tuple[Change, ...] = ()

    @property
    def breaking(self) -> tuple[Change, ...]:
        return tuple(c for c in self.changes if c.kind == BREAKING)

    @property
    def additive(self) -> tuple[Change, ...]:
        return tuple(c for c in self.changes if c.kind == ADDITIVE)

    @property
    def compatible(self) -> bool:
        return not self.breaking

    def to_dict(self) -> dict:
        return {"compatible": self.compatible,
                "changes": [c.to_dict() for c in self.changes]}

    def format(self) -> str:
        if not self.changes:
            return "CONTRACTS UNCHANGED"
        head = ("CONTRACT CHANGES — additive only"
                if self.compatible else
                f"CONTRACT CHANGES — {len(self.breaking)} BREAKING")
        return "\n".join([head] + [c.line() for c in self.changes])


def _schema_properties(schema: dict) -> dict:
    return schema.get("properties") or {}


def _required(schema: dict) -> set[str]:
    return set(schema.get("required") or ())


def _compare_input(tool: str, old: dict, new: dict) -> list[Change]:
    out: list[Change] = []
    old_props, new_props = _schema_properties(old), _schema_properties(new)
    old_req, new_req = _required(old), _required(new)

    for name in sorted(set(new_props) - set(old_props)):
        # A new argument nobody has to send is additive. A new argument
        # that must be sent breaks every existing caller.
        kind = BREAKING if name in new_req else ADDITIVE
        out.append(Change(tool, kind, f"input property '{name}' added",
                          "required" if kind is BREAKING else "optional"))
    for name in sorted(set(old_props) - set(new_props)):
        out.append(Change(tool, BREAKING, f"input property '{name}' removed"))
    for name in sorted(set(old_props) & set(new_props)):
        old_type = old_props[name].get("type")
        new_type = new_props[name].get("type")
        if old_type != new_type:
            out.append(Change(tool, BREAKING,
                              f"input property '{name}' changed type",
                              f"{old_type} -> {new_type}"))
    for name in sorted(new_req - old_req):
        if name in old_props:
            out.append(Change(tool, BREAKING,
                              f"input property '{name}' is now required"))
    for name in sorted(old_req - new_req):
        # Relaxing a requirement cannot break a caller that was already
        # sending it.
        out.append(Change(tool, ADDITIVE,
                          f"input property '{name}' is no longer required"))
    return out


def _compare_entry(tool: str, old: dict, new: dict) -> list[Change]:
    out = _compare_input(tool, old.get("input_schema") or {},
                         new.get("input_schema") or {})

    if (old.get("output_schema") or {}) != (new.get("output_schema") or {}):
        out.append(Change(tool, BREAKING, "output schema changed"))

    old_perm, new_perm = set(old.get("permission") or ()), \
        set(new.get("permission") or ())
    for cap in sorted(new_perm - old_perm):
        out.append(Change(tool, BREAKING, f"now requires {cap}",
                          "a session that could call this may no longer be "
                          "able to"))
    for cap in sorted(old_perm - new_perm):
        out.append(Change(tool, ADDITIVE, f"no longer requires {cap}"))

    old_err, new_err = set(old.get("errors") or ()), set(new.get("errors") or ())
    for code in sorted(new_err - old_err):
        out.append(Change(tool, ADDITIVE, f"may now return {code}"))
    for code in sorted(old_err - new_err):
        # A code a caller handles that can no longer happen is dead code,
        # not a broken caller.
        out.append(Change(tool, NEUTRAL, f"no longer returns {code}"))

    if old.get("idempotency") != new.get("idempotency"):
        # Becoming less repeatable changes what a retrying caller may do.
        order = {"idempotent": 0, "non_idempotent": 1, "unsafe": 2}
        worse = order.get(str(new.get("idempotency")), 1) > \
            order.get(str(old.get("idempotency")), 1)
        out.append(Change(tool, BREAKING if worse else ADDITIVE,
                          "idempotency changed",
                          f"{old.get('idempotency')} -> "
                          f"{new.get('idempotency')}"))

    if old.get("needs_approval") != new.get("needs_approval"):
        # Newly needing approval is a guard, not a break: a call that used
        # to run unattended now asks. Losing the guard is the dangerous
        # direction, and it is reported as breaking so somebody looks.
        out.append(Change(
            tool, ADDITIVE if new.get("needs_approval") else BREAKING,
            "approval requirement changed",
            f"{old.get('needs_approval')} -> {new.get('needs_approval')}"))

    if old.get("timeout_seconds") != new.get("timeout_seconds"):
        out.append(Change(tool, NEUTRAL, "timeout changed",
                          f"{old.get('timeout_seconds')} -> "
                          f"{new.get('timeout_seconds')}"))

    if (old.get("retry") or {}) != (new.get("retry") or {}):
        out.append(Change(tool, NEUTRAL, "retry policy changed"))

    return out


def compare(old: dict, new: dict) -> Compatibility:
    """Classify every difference between two manifests."""
    old_tools = old.get("tools") or {}
    new_tools = new.get("tools") or {}
    changes: list[Change] = []

    for name in sorted(set(new_tools) - set(old_tools)):
        changes.append(Change(name, ADDITIVE, "tool added"))
    for name in sorted(set(old_tools) - set(new_tools)):
        changes.append(Change(name, BREAKING, "tool removed"))
    for name in sorted(set(old_tools) & set(new_tools)):
        if old_tools[name].get("digest") == new_tools[name].get("digest"):
            continue
        found = _compare_entry(name, old_tools[name], new_tools[name])
        # A digest that moved with no classified difference is still a
        # change; saying "something changed and we cannot name it" beats
        # reporting nothing.
        changes.extend(found or [Change(name, NEUTRAL, "contract changed",
                                        "no classified field differs")])
    return Compatibility(tuple(changes))


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    kind: str
    subject: str
    detail: str = ""
    where: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "subject": self.subject,
                "detail": self.detail, "where": self.where}

    def line(self) -> str:
        where = f" ({self.where})" if self.where else ""
        detail = f" — {self.detail}" if self.detail else ""
        return f"  {self.kind}: {self.subject}{where}{detail}"


@dataclass
class DriftReport:
    findings: tuple[Finding, ...] = ()
    compatibility: Compatibility | None = None
    checked: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.findings and (self.compatibility is None
                                      or self.compatibility.compatible)

    def of(self, kind: str) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.kind == kind)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "checked": self.checked,
                "findings": [f.to_dict() for f in self.findings],
                "compatibility": (self.compatibility.to_dict()
                                  if self.compatibility else None)}

    def format(self) -> str:
        head = "CONTRACT DRIFT — clean" if self.ok else \
            f"CONTRACT DRIFT — {len(self.findings)} finding(s)"
        lines = [head]
        if self.checked:
            lines.append("  checked: " + ", ".join(
                f"{k} {v}" for k, v in sorted(self.checked.items())))
        lines.extend(f.line() for f in self.findings)
        if self.compatibility is not None and self.compatibility.changes:
            lines.append("")
            lines.append(self.compatibility.format())
        return "\n".join(lines)


def _mentions(paths: list[Path], name: str) -> bool:
    needle = name.encode()
    for path in paths:
        try:
            if needle in path.read_bytes():
                return True
        except OSError:
            continue
    return False


def check(registry: dict, root: str | Path = ".",
          lock_path: str | Path | None = None,
          docs: str | Path | None = None,
          tests: str | Path | None = None) -> DriftReport:
    """Compare the registry against the lock file, the docs and the tests."""
    root = Path(root)
    lock_path = Path(lock_path) if lock_path else root / LOCK_NAME
    contracts = build_contracts(registry)
    current = manifest(contracts)
    findings: list[Finding] = []

    # 1. the schema really does have one source
    for name, traits in _TRAITS.items():
        if name not in registry:
            findings.append(Finding(
                D_STALE_TRAIT, name,
                "_TRAITS names a tool that build_registry() does not return",
                "fullagent/toolcontract.py"))
        for key in traits:
            if key in SCHEMA_KEYS or key not in TRAIT_KEYS:
                findings.append(Finding(
                    D_RESTATED_SCHEMA, name,
                    f"_TRAITS carries '{key}', which belongs to the registry",
                    "fullagent/toolcontract.py"))

    # 2. every tool declares what it can do
    for name in sorted(registry):
        if name not in TOOL_CAPABILITIES:
            findings.append(Finding(
                D_NO_CAPABILITY, name,
                "no entry in TOOL_CAPABILITIES, so it is denied by default",
                "fullagent/toolpolicy.py"))

    # 3. the lock file agrees with the registry
    locked = read_lock(lock_path)
    compatibility: Compatibility | None = None
    if locked is None:
        findings.append(Finding(
            D_UNLOCKED, lock_path.name,
            "no lock file; run `python -m fullagent.contractmanifest --write`",
            str(lock_path)))
    else:
        compatibility = compare(locked, current)
        locked_tools = set((locked.get("tools") or {}))
        for name in sorted(set(current["tools"]) - locked_tools):
            findings.append(Finding(D_UNLOCKED, name,
                                    "registered but not in the lock file",
                                    str(lock_path)))
        for name in sorted(locked_tools - set(current["tools"])):
            findings.append(Finding(D_STALE_LOCK, name,
                                    "in the lock file but not registered",
                                    str(lock_path)))
        for name in sorted(locked_tools & set(current["tools"])):
            if locked["tools"][name].get("digest") != \
                    current["tools"][name].get("digest"):
                findings.append(Finding(D_CHANGED, name,
                                        "contract differs from the lock file",
                                        str(lock_path)))

    # 4. the tests mention it
    test_dir = Path(tests) if tests else root / "tests"
    test_files = sorted(test_dir.glob("test_*.py")) if test_dir.is_dir() else []
    if test_files:
        for name in sorted(registry):
            if not _mentions(test_files, name):
                findings.append(Finding(D_UNTESTED, name,
                                        "no test file mentions this tool",
                                        str(test_dir)))

    # 5. the generated docs describe it
    doc_path = Path(docs) if docs else root / "docs" / "TOOLS.md"
    if doc_path.exists():
        for name in sorted(registry):
            if not _mentions([doc_path], name):
                findings.append(Finding(D_UNDOCUMENTED, name,
                                        "the generated tool docs omit it",
                                        str(doc_path)))

    return DriftReport(tuple(findings), compatibility,
                       {"tools": len(registry), "tests": len(test_files),
                        "docs": 1 if doc_path.exists() else 0})


# ---------------------------------------------------------------------------
# CLI: --write refreshes the lock, --check fails on drift
# ---------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    from .tools import build_registry

    root = Path(__file__).resolve().parent.parent
    registry = build_registry()
    if "--write" in argv:
        data = manifest(build_contracts(registry))
        write_lock(root / LOCK_NAME, data)
        print(f"wrote {LOCK_NAME} — {len(data['tools'])} tools, "
              f"digest {data['digest']}")
        return 0
    report = check(registry, root=root)
    print(report.format())
    return 0 if report.ok else 1


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        raise SystemExit(_cli(sys.argv[1:]))

    # ------------------------------------------------------------------
    # Self-test
    # ------------------------------------------------------------------
    import tempfile

    from .tools import build_registry

    registry = build_registry()
    contracts = build_contracts(registry)
    current = manifest(contracts)

    assert set(current["tools"]) == set(registry)
    assert current["digest"] and len(current["digest"]) == 16
    # A manifest is a pure function of the contracts: build it twice, get
    # the same digest, or the lock file is noise in every diff.
    assert manifest(build_contracts(build_registry()))["digest"] == \
        current["digest"], "the manifest is not deterministic"

    # description changes are prose, not contract
    import dataclasses
    reworded = dict(contracts)
    first = sorted(contracts)[0]
    reworded[first] = dataclasses.replace(contracts[first],
                                          description="reworded entirely")
    assert manifest(reworded)["digest"] == current["digest"], \
        "rewording a description must not move the contract digest"

    # --- additive changes --------------------------------------------
    def mutate(tool, **fields):
        altered = dict(contracts)
        altered[tool] = dataclasses.replace(contracts[tool], **fields)
        return manifest(altered)

    schema = contracts["read_file"].input_schema
    optional = json.loads(json.dumps(schema))
    optional["properties"]["encoding"] = {"type": "string"}
    added = compare(current, mutate("read_file", input_schema=optional))
    assert added.compatible and added.additive, added.format()

    required = json.loads(json.dumps(optional))
    required["required"] = sorted(set(required.get("required", [])) |
                                  {"encoding"})
    demanded = compare(current, mutate("read_file", input_schema=required))
    assert not demanded.compatible, demanded.format()
    assert any("now required" in c.what or "required" in c.detail
               for c in demanded.breaking), demanded.format()

    dropped = json.loads(json.dumps(schema))
    dropped["properties"].pop("limit", None)
    removed = compare(current, mutate("read_file", input_schema=dropped))
    assert not removed.compatible, removed.format()

    retyped = json.loads(json.dumps(schema))
    retyped["properties"]["path"] = {"type": "integer"}
    changed_type = compare(current, mutate("read_file", input_schema=retyped))
    assert not changed_type.compatible, changed_type.format()

    from .toolpolicy import FS_DELETE
    widened = compare(current, mutate(
        "read_file", permission=contracts["read_file"].permission |
        {FS_DELETE}))
    assert not widened.compatible, widened.format()

    from .toolcontract import E_UPSTREAM, UNSAFE
    assert E_UPSTREAM not in contracts["read_file"].errors
    more_errors = compare(current, mutate(
        "read_file", errors=contracts["read_file"].errors + (E_UPSTREAM,)))
    assert more_errors.compatible and more_errors.additive, \
        more_errors.format()

    less_safe = compare(current, mutate("read_file", idempotency=UNSAFE))
    assert not less_safe.compatible, less_safe.format()

    guarded = compare(current, mutate("read_file", destructive=True))
    assert guarded.compatible, "newly needing approval is a guard, not a break"
    unguarded = compare(mutate("read_file", destructive=True), current)
    assert not unguarded.compatible, "losing a guard must be reported"

    # adding and removing whole tools
    without = {k: v for k, v in contracts.items() if k != "read_file"}
    assert compare(manifest(without), current).compatible, "a new tool is additive"
    assert not compare(current, manifest(without)).compatible, \
        "removing a tool breaks its callers"

    # --- the lock file round-trips ------------------------------------
    tmp = Path(tempfile.mkdtemp(prefix="fa-lock-"))
    write_lock(tmp / LOCK_NAME, current)
    assert read_lock(tmp / LOCK_NAME) == current
    assert read_lock(tmp / "nothing.json") is None

    # --- drift ---------------------------------------------------------
    clean = check(registry, root=tmp, lock_path=tmp / LOCK_NAME,
                  docs=tmp / "none.md", tests=tmp / "none")
    assert not clean.findings, clean.format()

    stale = json.loads(json.dumps(current))
    stale["tools"].pop("read_file")
    stale["tools"]["ghost_tool"] = {"digest": "0" * 16}
    write_lock(tmp / "stale.json", stale)
    drifted = check(registry, root=tmp, lock_path=tmp / "stale.json",
                    docs=tmp / "none.md", tests=tmp / "none")
    assert not drifted.ok
    assert drifted.of(D_UNLOCKED) and drifted.of(D_STALE_LOCK), \
        drifted.format()

    missing_lock = check(registry, root=tmp, lock_path=tmp / "absent.json",
                         docs=tmp / "none.md", tests=tmp / "none")
    assert missing_lock.of(D_UNLOCKED), missing_lock.format()

    # a tool nothing mentions is reported against a real docs/tests pair
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test_a.py").write_text("read_file and write_file\n")
    (tmp / "TOOLS.md").write_text("# read_file\n")
    partial = check(registry, root=tmp, lock_path=tmp / LOCK_NAME,
                    docs=tmp / "TOOLS.md", tests=tmp / "tests")
    assert partial.of(D_UNTESTED) and partial.of(D_UNDOCUMENTED), \
        partial.format()
    assert not any(f.subject == "read_file" for f in partial.findings)

    # --- the repo itself ------------------------------------------------
    repo = Path(__file__).resolve().parent.parent
    live = check(registry, root=repo)
    print(live.format())
    assert not live.of(D_RESTATED_SCHEMA), \
        "a schema has been restated outside the registry"
    assert not live.of(D_STALE_TRAIT), live.format()
    assert not live.of(D_NO_CAPABILITY), live.format()

    print(f"CONTRACTMANIFEST SELF-TEST PASS — {len(current['tools'])} "
          f"contracts, digest {current['digest']}")

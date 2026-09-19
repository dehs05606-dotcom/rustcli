"""Tool policy — capability manifests, roles, and a deny-by-default sandbox.

`Agent._gate` already asks two questions before a tool runs: does the
autonomy level allow it, and is this approach a known dead end. Both are
about the agent's own state. Neither asks the question an operator cares
about: *is this session allowed to do this at all?*

That question needs something the tool registry does not have — a
statement of what each tool can actually do. So every tool is given a
capability manifest (`fs.read`, `proc.exec`, `net.fetch`), a role names
the capabilities a session holds, and a call is permitted only when every
capability it needs is held. Unknown tools have no manifest, so they are
denied: the default answer is no, and a plugin that appears at runtime
cannot widen its own reach by being unfamiliar.

Three confinements sit on top of the capability check, because a
capability alone is too coarse to be safe:

- **Path confinement** resolves the target and requires it to stay inside
  a declared root. Resolution happens before the check, so `../../etc` and
  a symlink pointing out of the tree are both caught — a policy that
  compared the string as written would be satisfied by either.
- **Command policy** denies the small set of shell commands that destroy
  state, and keeps that set small on purpose. A pattern broad enough to
  block ordinary work gets switched off, and a policy that is off protects
  nothing.
- **Call ceilings** cap how often one tool may run in a session, which is
  what turns a loop bug into a stopped session instead of a full disk.

This module decides; it never executes. It returns a `Decision` and the
caller enforces it, so a policy bug can block work but can never run any.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

# Capabilities. Deliberately few: a vocabulary an operator can hold in
# their head is one they will actually configure correctly.
FS_READ = "fs.read"
FS_WRITE = "fs.write"
FS_DELETE = "fs.delete"
PROC_EXEC = "proc.exec"
NET_FETCH = "net.fetch"

ALL_CAPABILITIES = (FS_READ, FS_WRITE, FS_DELETE, PROC_EXEC, NET_FETCH)

ALLOW, ASK, DENY = "allow", "ask", "deny"

# The manifest: what each shipped tool is actually able to do. A tool
# missing from this table holds no capability and is denied by default.
TOOL_CAPABILITIES: dict[str, frozenset[str]] = {
    "read_file": frozenset({FS_READ}),
    "list_dir": frozenset({FS_READ}),
    "file_info": frozenset({FS_READ}),
    "search_files": frozenset({FS_READ}),
    "glob_files": frozenset({FS_READ}),
    "write_file": frozenset({FS_READ, FS_WRITE}),
    "edit_file": frozenset({FS_READ, FS_WRITE}),
    "apply_patch": frozenset({FS_READ, FS_WRITE}),
    "create_directory": frozenset({FS_WRITE}),
    "copy_path": frozenset({FS_READ, FS_WRITE}),
    "move_path": frozenset({FS_WRITE, FS_DELETE}),
    "delete_path": frozenset({FS_DELETE}),
    "run_command": frozenset({PROC_EXEC}),
    "live_shell": frozenset({PROC_EXEC}),
    "live_shell_reset": frozenset({PROC_EXEC}),
    "web_fetch": frozenset({NET_FETCH}),
    "web_search": frozenset({NET_FETCH}),
}

# Arguments that name a filesystem target, per tool.
PATH_ARGS: dict[str, tuple[str, ...]] = {
    "read_file": ("path",), "list_dir": ("path",), "file_info": ("path",),
    "write_file": ("path",), "edit_file": ("path",),
    "create_directory": ("path",), "delete_path": ("path",),
    "copy_path": ("src", "dst"), "move_path": ("src", "dst"),
    "search_files": ("path",), "glob_files": ("path",),
}

# Hosts that are never fetched, whatever an allow-list says. These are
# the SSRF targets: the cloud metadata service hands credentials to
# anything on the box that asks, and loopback reaches services that
# believe a local caller is already trusted. A deny an allow-list can
# override is not a deny, so this runs first and cannot be configured
# away.
BLOCKED_HOSTS = frozenset({
    "169.254.169.254", "metadata.google.internal", "metadata",
    "localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]",
})
BLOCKED_SCHEMES = frozenset({"file", "gopher", "ftp", "data", "dict"})
PRIVATE_PREFIXES = ("10.", "192.168.", "127.", "169.254.", "0.")

DESTRUCTIVE_RE = re.compile(
    r"\brm\s+(?:-\w*[rf]\w*\s+)+|\bgit\s+push\s+(?:--force|-f)\b|"
    r"\bgit\s+reset\s+--hard\b|\bgit\s+clean\s+-\w*[fd]\w*\b|"
    r"\b(?:drop|truncate)\s+(?:table|database)\b|\bmkfs\b|\bdd\s+if=|"
    r">\s*/dev/(?:sd|nvme)|\bshutdown\b|\breboot\b|\bchmod\s+-R\s+777\b",
    re.IGNORECASE)


@dataclass(frozen=True)
class Role:
    """A named set of capabilities, with the limits that go with them."""
    name: str
    capabilities: frozenset[str]
    roots: tuple[str, ...] = ()
    ask_capabilities: frozenset[str] = frozenset()
    ceilings: dict[str, int] = field(default_factory=dict)
    allow_destructive_commands: bool = False
    # Hosts this role may reach. Empty means "no allow-list configured",
    # which permits any host not already blocked above — the shipped
    # default, because an empty allow-list that denied everything would
    # break web_fetch for every existing user on upgrade. Set it to lock
    # a session down.
    allowed_hosts: tuple[str, ...] = ()

    def holds(self, capability: str) -> bool:
        """Whether the role has this capability at all.

        `ask_capabilities` are held — they are simply held on condition of
        confirmation. Treating them as not-held denied the very calls the
        confirmation exists for, which made "ask" unreachable: a role
        could be configured to prompt before deleting and would instead
        refuse outright.
        """
        return (capability in self.capabilities
                or capability in self.ask_capabilities)

    @property
    def granted(self) -> frozenset[str]:
        """Everything the role can reach, freely or on confirmation."""
        return self.capabilities | self.ask_capabilities

    def to_dict(self) -> dict:
        return {"name": self.name,
                "capabilities": sorted(self.capabilities),
                "roots": list(self.roots),
                "ask": sorted(self.ask_capabilities),
                "ceilings": dict(self.ceilings),
                "allow_destructive_commands": self.allow_destructive_commands}


# Built-in roles, weakest first. `untrusted` is what an unknown caller
# gets; it can look at the tree and nothing else.
ROLES: dict[str, Role] = {
    "untrusted": Role("untrusted", frozenset({FS_READ}),
                      ceilings={"read_file": 200, "search_files": 100}),
    "readonly": Role("readonly", frozenset({FS_READ, NET_FETCH}),
                     ceilings={"web_fetch": 50}),
    "developer": Role("developer",
                      frozenset({FS_READ, FS_WRITE, PROC_EXEC, NET_FETCH}),
                      ask_capabilities=frozenset({FS_DELETE}),
                      ceilings={"run_command": 400, "write_file": 300}),
    "operator": Role("operator", frozenset(ALL_CAPABILITIES),
                     ask_capabilities=frozenset({FS_DELETE}),
                     allow_destructive_commands=True),
}

DEFAULT_ROLE = "developer"


def _is_private_host(host: str) -> bool:
    if host.startswith(PRIVATE_PREFIXES):
        return True
    # 172.16.0.0/12 is private; 172.15 and 172.32 are not, so the second
    # octet has to be read rather than prefix-matched.
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) > 1 and parts[1].isdigit():
            return 16 <= int(parts[1]) <= 31
    return False


def _host_of(url: str) -> tuple[str, str]:
    """(scheme, hostname) for a URL, lowercased, credentials discarded."""
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
    except (ValueError, AttributeError):
        return "", ""
    return (parsed.scheme or "").lower(), host


def host_allowed(url: str, allowed: tuple[str, ...] = ()) -> str:
    """'' when the URL may be fetched, else the reason it may not.

    The order is not negotiable: scheme and blocked hosts are checked
    before any allow-list, so configuring `allowed_hosts` can never
    re-open the metadata endpoint. The host is read from the parsed URL
    rather than the raw string, so `https://user:pw@169.254.169.254/` is
    judged on the host it actually reaches.
    """
    scheme, host = _host_of(url)
    if not scheme or not host:
        return f"not a fetchable URL: {url[:80]}"
    if scheme in BLOCKED_SCHEMES or scheme not in ("http", "https"):
        return f"scheme '{scheme}' is not fetchable"
    if host in BLOCKED_HOSTS or _is_private_host(host):
        return f"host '{host}' is loopback, link-local or private"
    if allowed:
        for pattern in allowed:
            pattern = pattern.lower().lstrip(".")
            if host == pattern or host.endswith("." + pattern):
                return ""
        return f"host '{host}' is not on the allow-list"
    return ""


@dataclass(frozen=True)
class Decision:
    """The verdict on one tool call, with the rule that produced it."""
    outcome: str                # allow | ask | deny
    tool: str
    reason: str = ""
    role: str = ""
    capability: str = ""
    rule: str = ""              # which confinement decided it
    # Every stage's structured rationale, in the order they ran. Empty
    # only for a Decision built by hand in a test.
    rationale: tuple = ()

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOW

    @property
    def denied(self) -> bool:
        return self.outcome == DENY

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "tool": self.tool,
                "reason": self.reason, "role": self.role,
                "capability": self.capability, "rule": self.rule,
                "rationale": [r.to_dict() for r in self.rationale]}


class ToolPolicy:
    """Deny by default; allow what a role actually holds."""

    def __init__(self, role: Role | str = DEFAULT_ROLE, log=None,
                 roots: tuple[str, ...] | None = None,
                 manifest: dict[str, frozenset[str]] | None = None):
        self.role = ROLES[role] if isinstance(role, str) else role
        self.log = log
        self.manifest = dict(manifest or TOOL_CAPABILITIES)
        declared = roots if roots is not None else self.role.roots
        self.roots = tuple(str(Path(r).resolve()) for r in declared) or \
            (str(Path.cwd().resolve()),)
        self.counts: dict[str, int] = {}

    # -- manifest ----------------------------------------------------------

    def capabilities_of(self, tool_name: str) -> frozenset[str]:
        return self.manifest.get(tool_name, frozenset())

    def register(self, tool_name: str, capabilities: frozenset[str]) -> None:
        """Declare a tool's reach — how a plugin joins the manifest.

        A plugin that declares nothing gets an empty set and is denied,
        which is the behaviour we want: arriving late must not be a way
        to arrive unrestricted.
        """
        self.manifest[tool_name] = frozenset(capabilities)

    # -- confinements ------------------------------------------------------

    def _within_roots(self, candidate: str) -> bool:
        try:
            target = Path(candidate)
            if not target.is_absolute():
                target = Path.cwd() / target
            # resolve() before comparing: '../..' and a symlink out of the
            # tree both look fine as written and are exactly what this is
            # here to catch.
            resolved = str(target.resolve())
        except (OSError, RuntimeError, ValueError):
            return False
        return any(resolved == root or resolved.startswith(root + "/")
                   for root in self.roots)

    def _path_violation(self, tool_name: str, args: dict) -> str:
        for key in PATH_ARGS.get(tool_name, ()):
            value = args.get(key)
            if not value:
                continue
            if not self._within_roots(str(value)):
                return (f"{key}={value} resolves outside the permitted "
                        f"roots ({', '.join(self.roots)})")
        return ""

    def _ceiling_violation(self, tool_name: str) -> str:
        limit = self.role.ceilings.get(tool_name)
        if limit is None:
            return ""
        if self.counts.get(tool_name, 0) >= limit:
            return f"{tool_name} has hit its session ceiling of {limit}"
        return ""

    # -- the decision ------------------------------------------------------

    def evaluate(self, tool_name: str, args: dict | None = None) -> Decision:
        """Decide one tool call. Pure -- it records, it never executes."""
        detailed = self.evaluate_detailed(tool_name, args)
        return self._seal(Decision(
            detailed.outcome, tool_name, detailed.reason, self.role.name,
            capability=detailed.capability, rule=detailed.rule,
            rationale=detailed.rationale))

    def evaluate_detailed(self, tool_name: str, args: dict | None = None):
        """The same decision with every stage's rationale attached.

        The staged form is the real one; `evaluate` is the collapse of it
        to the single verdict most callers want. They cannot disagree,
        because one is computed from the other.
        """
        # Imported here rather than at module scope: the pipeline is built
        # out of this module's own vocabulary, so a top-level import would
        # be a cycle.
        from .policypipeline import DEFAULT_PIPELINE, Request

        return DEFAULT_PIPELINE.decide(Request(
            tool=tool_name, args=dict(args or {}), role=self.role,
            capabilities=self.capabilities_of(tool_name),
            roots=self.roots, counts=dict(self.counts),
            known=tool_name in self.manifest))

    def record_call(self, tool_name: str) -> None:
        """Count a call that actually ran — what the ceilings measure."""
        self.counts[tool_name] = self.counts.get(tool_name, 0) + 1

    def _seal(self, decision: Decision) -> Decision:
        if self.log is not None and decision.outcome != ALLOW:
            try:
                self.log.append("policy.decision", decision.to_dict(),
                                actor="kernel")
            except Exception:
                pass
        return decision

    # -- observability -----------------------------------------------------

    def describe(self) -> dict:
        return {"role": self.role.to_dict(), "roots": list(self.roots),
                "tools": {name: sorted(caps)
                          for name, caps in sorted(self.manifest.items())},
                "calls": dict(self.counts)}

    def format_status(self) -> str:
        lines = [f"TOOL POLICY — role '{self.role.name}' "
                 f"({len(self.role.granted)}/{len(ALL_CAPABILITIES)} "
                 f"capabilities)",
                 f"  holds: {', '.join(sorted(self.role.capabilities))}"]
        if self.role.ask_capabilities:
            lines.append(f"  asks:  {', '.join(sorted(self.role.ask_capabilities))}")
        withheld = sorted(set(ALL_CAPABILITIES) - self.role.granted)
        if withheld:
            lines.append(f"  denied: {', '.join(withheld)}")
        lines.append(f"  roots: {', '.join(self.roots)}")
        denied_tools = sorted(t for t in self.manifest
                              if not self.evaluate(t, {}).allowed)
        lines.append(f"  tools: {len(self.manifest)} known, "
                     f"{len(denied_tools)} not freely available")
        return "\n".join(lines)


def from_config(data: dict, log=None) -> ToolPolicy:
    """Build a policy from plain config — the zero-trust entry point.

    An unknown role name is not a reason to fall back to something
    permissive. It falls back to `untrusted`, because a typo in a config
    file must never be the thing that hands a session more power than its
    author meant to give it.
    """
    name = str(data.get("role", DEFAULT_ROLE))
    role = ROLES.get(name)
    if role is None:
        role = ROLES["untrusted"]
    extra = data.get("capabilities")
    if isinstance(extra, list):
        wanted = frozenset(c for c in extra if c in ALL_CAPABILITIES)
        # Config narrows a role; it never widens one. Both sets are
        # intersected, so naming a capability the role never had does
        # nothing at all.
        role = Role(role.name, role.capabilities & wanted, role.roots,
                    role.ask_capabilities & wanted, role.ceilings,
                    role.allow_destructive_commands)
    hosts = data.get("allowed_hosts")
    if isinstance(hosts, list):
        role = Role(role.name, role.capabilities, role.roots,
                    role.ask_capabilities, role.ceilings,
                    role.allow_destructive_commands,
                    tuple(str(h) for h in hosts))
    roots = data.get("roots")
    roots_t = tuple(str(r) for r in roots) if isinstance(roots, list) else None
    return ToolPolicy(role=role, log=log, roots=roots_t)


if __name__ == "__main__":
    import os
    import tempfile
    from pathlib import Path as _Path

    from .kernel import EventLog

    with tempfile.TemporaryDirectory() as td:
        root = _Path(td).resolve()
        (root / "inside.py").write_text("x = 1\n")
        (root / "sub").mkdir()
        log = EventLog(root / "p.jsonl")

        dev = ToolPolicy("developer", log=log, roots=(str(root),))

        # --- capability gate ------------------------------------------
        assert dev.evaluate("read_file", {"path": str(root / "inside.py")}).allowed
        assert dev.evaluate("write_file", {"path": str(root / "new.py")}).allowed
        delete = dev.evaluate("delete_path", {"path": str(root / "inside.py")})
        assert delete.outcome == ASK, delete.to_dict()

        # an ask-capability is held, on condition — not withheld
        assert ROLES["developer"].holds(FS_DELETE)
        assert FS_DELETE not in ROLES["developer"].capabilities

        ro = ToolPolicy("readonly", log=log, roots=(str(root),))
        blocked = ro.evaluate("write_file", {"path": str(root / "x.py")})
        assert blocked.denied and blocked.capability == FS_WRITE, blocked.to_dict()
        assert ro.evaluate("run_command", {"command": "ls"}).denied

        # --- an unknown tool holds nothing ----------------------------
        ghost = dev.evaluate("exfiltrate_everything", {})
        assert ghost.denied and ghost.rule == "manifest", ghost.to_dict()
        # and a plugin that declares its reach joins the manifest properly
        dev.register("count_lines", frozenset({FS_READ}))
        assert dev.evaluate("count_lines", {"path": str(root)}).allowed
        dev.register("nuke", frozenset({FS_DELETE}))
        assert dev.evaluate("nuke", {}).outcome == ASK

        # --- path confinement, including the ways around it -----------
        out = dev.evaluate("read_file", {"path": "/etc/passwd"})
        assert out.denied and out.rule == "path-confinement", out.to_dict()
        climb = dev.evaluate("read_file", {"path": str(root / ".." / "escape")})
        assert climb.denied, climb.to_dict()
        if hasattr(os, "symlink"):
            link = root / "sub" / "way-out"
            try:
                os.symlink("/etc", link)
            except (OSError, NotImplementedError):
                pass
            else:
                sneak = dev.evaluate("read_file", {"path": str(link / "passwd")})
                assert sneak.denied, "a symlink out of the tree must not pass"
        # both ends of a two-path tool are checked
        pair = dev.evaluate("copy_path", {"src": str(root / "inside.py"),
                                          "dst": "/tmp/elsewhere"})
        assert pair.denied, pair.to_dict()

        # --- command policy -------------------------------------------
        assert dev.evaluate("run_command", {"command": "pytest -q"}).allowed
        rm = dev.evaluate("run_command", {"command": "rm -rf build"})
        assert rm.denied and rm.rule == "command-policy", rm.to_dict()
        op = ToolPolicy("operator", log=log, roots=(str(root),))
        assert op.evaluate("run_command", {"command": "rm -rf build"}).outcome == ASK

        # --- ceilings --------------------------------------------------
        tight = ToolPolicy(Role("tight", frozenset({FS_READ}),
                                ceilings={"read_file": 2}),
                           log=log, roots=(str(root),))
        target = {"path": str(root / "inside.py")}
        assert tight.evaluate("read_file", target).allowed
        tight.record_call("read_file")
        tight.record_call("read_file")
        capped = tight.evaluate("read_file", target)
        assert capped.denied and capped.rule == "ceiling", capped.to_dict()

        # --- config is zero-trust: a typo must not widen anything ------
        typo = from_config({"role": "sudo-god-mode", "roots": [str(root)]})
        assert typo.role.name == "untrusted", typo.role.name
        assert typo.evaluate("run_command", {"command": "ls"}).denied
        narrowed = from_config({"role": "developer", "roots": [str(root)],
                                "capabilities": [FS_READ]})
        assert narrowed.evaluate("write_file",
                                 {"path": str(root / "y.py")}).denied
        # narrowing also drops the ask-capabilities it did not name
        assert narrowed.evaluate("delete_path",
                                 {"path": str(root / "inside.py")}).denied
        # and naming a capability the role never held grants nothing
        widen = from_config({"role": "readonly", "roots": [str(root)],
                             "capabilities": list(ALL_CAPABILITIES)})
        assert widen.evaluate("run_command", {"command": "ls"}).denied

        # --- network: SSRF targets denied before any allow-list -------
        assert host_allowed("https://example.com/x") == ""
        assert host_allowed("http://169.254.169.254/latest/meta-data/")
        assert host_allowed("http://localhost:8080/admin")
        assert host_allowed("http://127.0.0.1/")
        assert host_allowed("http://10.0.0.5/internal")
        assert host_allowed("http://172.16.0.9/")
        assert host_allowed("http://172.31.255.1/")
        assert host_allowed("http://172.15.0.1/") == ""     # not private
        assert host_allowed("http://172.32.0.1/") == ""     # not private
        assert host_allowed("file:///etc/passwd")
        # credentials in the URL must not disguise the real host
        assert host_allowed("https://user:pw@169.254.169.254/")
        # an allow-list narrows, and cannot re-open a blocked host
        locked = ("example.com",)
        assert host_allowed("https://api.example.com/v1", locked) == ""
        assert host_allowed("https://evil.test/", locked)
        assert host_allowed("http://169.254.169.254/", ("169.254.169.254",))
        # a suffix match must not accept a lookalike domain
        assert host_allowed("https://notexample.com/", locked)

        netted = ToolPolicy(Role("net", frozenset({NET_FETCH}),
                                 allowed_hosts=("example.com",)),
                            log=log, roots=(str(root),))
        assert netted.evaluate("web_fetch",
                               {"url": "https://example.com/a"}).allowed
        blocked_call = netted.evaluate(
            "web_fetch", {"url": "http://169.254.169.254/"})
        assert blocked_call.denied
        assert blocked_call.rule == "network-allow-list", blocked_call.to_dict()
        assert from_config({"role": "readonly",
                            "allowed_hosts": ["docs.python.org"]}
                           ).role.allowed_hosts == ("docs.python.org",)

        assert any(e.type == "policy.decision" for e in log.events())
        assert "TOOL POLICY" in dev.format_status()
        print(dev.format_status())
        print("TOOLPOLICY SELF-TEST PASS")

"""Constitutional Compliance Core — the prompt's rules as governed objects.

`promptrules.compile_prompt()` turns prompt text into a `PromptContract`,
but a contract is an in-memory value: anything in the process can build
one, and nothing downstream can tell a contract compiled from the sealed
prompt apart from one assembled by a bug, a plugin, or a tool result that
talked its way into the wrong variable.

This module closes that gap. Ratifying a contract promotes each of its
rules into a `PolicyObject`: content-addressed, versioned, signed, and
appended to the event log. From then on every enforcement decision can
name the exact policy version it acted under, and `Constitution.verify()`
can prove that the rule set in memory is still the one that was ratified.

**What "tamper-proof" does and does not mean here.** Signatures are
HMAC-SHA256 under a key generated on first use and stored at
`~/.fullagent/constitution.key` with `0600`. That makes tampering
*evident* to this program: a policy edited in the log, in memory, or on
disk fails verification and the session refuses to enforce it. It is not
protection against whoever owns the machine — they hold the key, and an
attacker who can read that file can forge a policy. Saying otherwise
would be the kind of security claim that gets believed and then relied
on, so it is said plainly here instead: this is integrity against drift,
corruption and in-process accidents, not against the operator.

Amendment is append-only. A rule is never edited in place; a new version
supersedes the old one, and both stay in the log, so "which rules was the
agent under when it did that?" is always answerable after the fact.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .promptrules import PromptContract, Rule, compile_prompt

KEY_FILE = "constitution.key"
SIGNATURE_VERSION = 1


def _canonical(obj) -> str:
    """Deterministic JSON — the basis of both hashing and signing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def signing_key(app_dir: Path | None = None) -> bytes:
    """The install's signing key, created on first use.

    Generated with `os.urandom`, written `0600`, and never logged, never
    put in an event, never included in a `to_dict()`. A key that leaked
    into the event log would be a key an audit export hands to whoever
    reads it.
    """
    root = Path(app_dir) if app_dir is not None else config.APP_DIR
    path = root / KEY_FILE
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if len(raw) >= 32:
            return bytes.fromhex(raw)
    except (OSError, ValueError):
        pass
    key = os.urandom(32)
    try:
        root.mkdir(parents=True, exist_ok=True)
        path.write_text(key.hex(), encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        # A read-only home must not make the agent unable to govern
        # itself. The key stays in memory for this process; verification
        # still works within the session, and the next session ratifies
        # afresh rather than trusting an unverifiable artifact.
        pass
    return key


@dataclass(frozen=True)
class PolicyObject:
    """One rule, promoted to a signed and versioned artifact."""
    policy_id: str
    version: int
    rule: dict
    content_hash: str
    signature: str
    created: float
    supersedes: str = ""

    def payload(self) -> dict:
        """Exactly the bytes that are hashed and signed."""
        return {"policy_id": self.policy_id, "version": self.version,
                "rule": self.rule, "supersedes": self.supersedes}

    def compute_hash(self) -> str:
        return hashlib.sha256(
            _canonical(self.payload()).encode("utf-8")).hexdigest()

    def sign(self, key: bytes) -> str:
        return hmac.new(key, self.compute_hash().encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def verify(self, key: bytes) -> bool:
        """True iff both the content hash and the signature still hold."""
        if self.compute_hash() != self.content_hash:
            return False
        return hmac.compare_digest(self.sign(key), self.signature)

    @property
    def priority(self) -> int:
        return int(self.rule.get("priority", 3))

    @property
    def blocking(self) -> bool:
        return bool(self.rule.get("predicate")) and self.priority <= 1

    def to_dict(self) -> dict:
        d = self.payload()
        d.update({"content_hash": self.content_hash,
                  "signature": self.signature, "created": self.created})
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PolicyObject":
        return cls(policy_id=d["policy_id"], version=int(d["version"]),
                   rule=d["rule"], content_hash=d["content_hash"],
                   signature=d["signature"], created=float(d.get("created", 0)),
                   supersedes=d.get("supersedes", ""))


def mint(rule: Rule, key: bytes, version: int = 1,
         supersedes: str = "") -> PolicyObject:
    """Sign one rule into a policy object."""
    draft = PolicyObject(policy_id=rule.id, version=version,
                         rule=rule.to_dict(), content_hash="",
                         signature="", created=time.time(),
                         supersedes=supersedes)
    content_hash = draft.compute_hash()
    signed = PolicyObject(policy_id=draft.policy_id, version=version,
                          rule=draft.rule, content_hash=content_hash,
                          signature="", created=draft.created,
                          supersedes=supersedes)
    return PolicyObject(policy_id=signed.policy_id, version=version,
                        rule=signed.rule, content_hash=content_hash,
                        signature=signed.sign(key), created=signed.created,
                        supersedes=supersedes)


@dataclass
class VerifyReport:
    """The outcome of checking a whole constitution."""
    ok: bool
    checked: int
    tampered: tuple[str, ...] = ()
    root_expected: str = ""
    root_actual: str = ""

    def format(self) -> str:
        if self.ok:
            return (f"CONSTITUTION INTACT — {self.checked} policies, "
                    f"root {self.root_actual[:12]}")
        lines = [f"CONSTITUTION TAMPERED — {len(self.tampered)} of "
                 f"{self.checked} policies failed verification"]
        for pid in self.tampered[:10]:
            lines.append(f"  ✗ {pid}")
        if self.root_expected != self.root_actual:
            lines.append(f"  root {self.root_expected[:12]} != "
                         f"{self.root_actual[:12]}")
        return "\n".join(lines)


@dataclass
class Constitution:
    """A ratified, versioned, verifiable rule set.

    `root` is a hash over every policy's content hash in sorted order —
    one value that changes if any policy changes, so a caller can pin the
    rule set it enforced under without carrying the whole set around.
    """
    name: str
    version: int
    fingerprint: str
    policies: tuple[PolicyObject, ...]
    root: str
    ratified: float = field(default_factory=time.time)

    @staticmethod
    def compute_root(policies: tuple[PolicyObject, ...]) -> str:
        joined = "".join(sorted(p.content_hash for p in policies))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def policy(self, policy_id: str) -> PolicyObject | None:
        for p in self.policies:
            if p.policy_id == policy_id:
                return p
        return None

    def blocking(self) -> tuple[PolicyObject, ...]:
        return tuple(p for p in self.policies if p.blocking)

    def by_predicate(self, predicate_id: str) -> tuple[PolicyObject, ...]:
        return tuple(p for p in self.policies
                     if p.rule.get("predicate") == predicate_id)

    def verify(self, key: bytes) -> VerifyReport:
        bad = tuple(p.policy_id for p in self.policies if not p.verify(key))
        actual = self.compute_root(self.policies)
        return VerifyReport(ok=not bad and actual == self.root,
                            checked=len(self.policies), tampered=bad,
                            root_expected=self.root, root_actual=actual)

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version,
                "fingerprint": self.fingerprint, "root": self.root,
                "ratified": self.ratified,
                "policies": [p.to_dict() for p in self.policies]}

    @classmethod
    def from_dict(cls, d: dict) -> "Constitution":
        policies = tuple(PolicyObject.from_dict(p)
                         for p in d.get("policies", ()))
        return cls(name=d["name"], version=int(d["version"]),
                   fingerprint=d.get("fingerprint", ""), policies=policies,
                   root=d.get("root", ""), ratified=float(d.get("ratified", 0)))

    def format_status(self) -> str:
        bands: dict[int, int] = {}
        for p in self.policies:
            bands[p.priority] = bands.get(p.priority, 0) + 1
        lines = [f"CONSTITUTION v{self.version} — {self.name}",
                 f"  prompt {self.fingerprint[:12]} · root {self.root[:12]}",
                 f"  {len(self.policies)} policies · "
                 f"{len(self.blocking())} blocking"]
        for band in sorted(bands):
            lines.append(f"    priority {band}: {bands[band]}")
        return "\n".join(lines)


def ratify(contract: PromptContract, key: bytes,
           version: int = 1, prior: Constitution | None = None) -> Constitution:
    """Promote a compiled contract into a signed constitution.

    With a `prior`, each policy records the content hash it supersedes, so
    the chain from the first ratification to this one stays walkable.
    """
    policies = []
    for rule in contract.rules:
        old = prior.policy(rule.id) if prior else None
        policies.append(mint(rule, key,
                             version=(old.version + 1) if old else 1,
                             supersedes=old.content_hash if old else ""))
    frozen = tuple(policies)
    return Constitution(name=contract.name, version=version,
                        fingerprint=contract.fingerprint, policies=frozen,
                        root=Constitution.compute_root(frozen))


def amend(prior: Constitution, contract: PromptContract,
          key: bytes) -> tuple[Constitution, dict]:
    """Ratify a new contract as the next version, and say what changed."""
    nxt = ratify(contract, key, version=prior.version + 1, prior=prior)
    old_ids = {p.policy_id for p in prior.policies}
    new_ids = {p.policy_id for p in nxt.policies}
    changed = [p.policy_id for p in nxt.policies
               if (o := prior.policy(p.policy_id)) is not None
               and o.content_hash != p.content_hash]
    return nxt, {"from_version": prior.version, "to_version": nxt.version,
                 "added": sorted(new_ids - old_ids),
                 "repealed": sorted(old_ids - new_ids),
                 "changed": sorted(changed),
                 "root_before": prior.root, "root_after": nxt.root}


class ConstitutionalCore:
    """The runtime home of the constitution: ratify, seal, reload, verify.

    Every state change is an event. Rebuilding from the log and finding a
    policy that no longer verifies is not a warning — `current()` refuses
    to hand back a tampered constitution, because a guardrail enforcing
    rules it cannot vouch for is worse than one that admits it is blind.
    """

    def __init__(self, log, app_dir: Path | None = None):
        self.log = log
        self.key = signing_key(app_dir)
        self._current: Constitution | None = None
        self._last_report: VerifyReport | None = None

    def ratify_prompt(self, name: str, text: str,
                      tool_names: frozenset[str] | None = None) -> Constitution:
        """Compile, sign and seal a prompt. Idempotent on identical text."""
        contract = compile_prompt(name, text, tool_names=tool_names)
        prior = self._current
        if prior is not None and prior.fingerprint == contract.fingerprint:
            return prior
        if prior is None:
            const = ratify(contract, self.key)
            delta = {"from_version": 0, "to_version": 1,
                     "added": [p.policy_id for p in const.policies],
                     "repealed": [], "changed": [],
                     "root_before": "", "root_after": const.root}
            event = "constitution.ratified"
        else:
            const, delta = amend(prior, contract, self.key)
            event = "constitution.amended"
        self._current = const
        # The policies ride in the event so the log alone can rebuild the
        # constitution; the signing key never does.
        self.log.append(event, {"name": const.name, "version": const.version,
                                "fingerprint": const.fingerprint,
                                "root": const.root, "delta": delta,
                                "policies": [p.to_dict()
                                             for p in const.policies]},
                        actor="kernel")
        return const

    def load(self) -> Constitution | None:
        """Rebuild the newest constitution from the log, verified."""
        latest: Constitution | None = None
        for ev in self.log.events():
            if ev.type in ("constitution.ratified", "constitution.amended"):
                try:
                    latest = Constitution.from_dict(ev.data)
                except (KeyError, TypeError, ValueError):
                    continue
        if latest is None:
            return None
        report = latest.verify(self.key)
        self._last_report = report
        if not report.ok:
            self.log.append("constitution.tamper",
                            {"version": latest.version,
                             "tampered": list(report.tampered),
                             "root_expected": report.root_expected,
                             "root_actual": report.root_actual},
                            actor="kernel")
            return None
        self._current = latest
        return latest

    def current(self) -> Constitution | None:
        """The constitution in force, or None when it cannot be vouched for."""
        if self._current is None:
            return None
        report = self._current.verify(self.key)
        self._last_report = report
        if not report.ok:
            self.log.append("constitution.tamper",
                            {"version": self._current.version,
                             "tampered": list(report.tampered)},
                            actor="kernel")
            self._current = None
            return None
        return self._current

    def last_report(self) -> VerifyReport | None:
        return self._last_report

    def format_status(self) -> str:
        const = self.current()
        if const is None:
            if self._last_report is not None:
                return self._last_report.format()
            return "CONSTITUTION — none ratified"
        return const.format_status()


if __name__ == "__main__":
    import tempfile
    from pathlib import Path as _Path

    from . import systemprompt
    from .kernel import EventLog

    with tempfile.TemporaryDirectory() as td:
        root = _Path(td)
        key = signing_key(root)
        assert len(key) == 32
        assert signing_key(root) == key, "the key must be stable once created"
        assert (root / KEY_FILE).exists()
        if hasattr(os, "getuid"):
            mode = (root / KEY_FILE).stat().st_mode & 0o777
            assert mode == 0o600, oct(mode)

        contract = compile_prompt("main", systemprompt.MAIN)
        const = ratify(contract, key)
        assert const.policies and const.version == 1
        assert const.verify(key).ok
        assert "CONSTITUTION v1" in const.format_status()

        # --- a tampered rule fails verification ------------------------
        victim = const.policies[0]
        forged_rule = dict(victim.rule)
        forged_rule["priority"] = 3            # quietly demote a critical rule
        forged = PolicyObject(policy_id=victim.policy_id,
                              version=victim.version, rule=forged_rule,
                              content_hash=victim.content_hash,
                              signature=victim.signature,
                              created=victim.created)
        assert not forged.verify(key), "an edited rule must not verify"
        broken = Constitution(name=const.name, version=const.version,
                              fingerprint=const.fingerprint,
                              policies=(forged,) + const.policies[1:],
                              root=const.root)
        rep = broken.verify(key)
        assert not rep.ok and victim.policy_id in rep.tampered
        assert "TAMPERED" in rep.format()

        # --- a different key cannot vouch for these policies -----------
        assert not const.verify(os.urandom(32)).ok

        # --- amendment is append-only and reports its delta ------------
        v2_text = systemprompt.MAIN + "\n## Extra\n- Never guess an exit code.\n"
        v2_contract = compile_prompt("main", v2_text)
        v2, delta = amend(const, v2_contract, key)
        assert v2.version == 2 and v2.verify(key).ok
        assert delta["added"] and not delta["repealed"], delta
        assert v2.root != const.root
        kept = v2.policy(const.policies[-1].policy_id)
        assert kept is not None and kept.supersedes, "supersession is recorded"

        # --- the core: seal, reload, and refuse what it cannot vouch for
        log = EventLog(root / "c.jsonl")
        core = ConstitutionalCore(log, app_dir=root)
        first = core.ratify_prompt("main", systemprompt.MAIN)
        assert first.version == 1
        same = core.ratify_prompt("main", systemprompt.MAIN)
        assert same is first, "identical text must not mint a new version"
        second = core.ratify_prompt("main", v2_text)
        assert second.version == 2
        assert core.current() is not None
        reloaded = ConstitutionalCore(log, app_dir=root).load()
        assert reloaded is not None and reloaded.version == 2
        assert reloaded.root == second.root

        # a core whose key is gone must report blind, never enforce
        other = ConstitutionalCore(log, app_dir=root / "elsewhere")
        assert other.load() is None
        assert any(e.type == "constitution.tamper" for e in log.events())
        assert "TAMPERED" in other.format_status()

        kinds = {e.type for e in log.events()}
        assert {"constitution.ratified", "constitution.amended"} <= kinds
        # the signing key must never reach the log
        blob = "".join(_canonical(e.data) for e in log.events())
        assert key.hex() not in blob, "the signing key leaked into the log"

        print(second.format_status())
        print("CONSTITUTION SELF-TEST PASS")

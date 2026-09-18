"""Self-auditing runtime — the audit trail and the compliance dashboard.

The event log already records everything, which is not the same as being
auditable. An auditor's questions are "what did this session do", "under
which rule", and "can I trust that this record was not edited" — and
answering those from a raw `jsonl` of forty event types is work nobody
does, so in practice the record goes unread.

This module is the reading layer. It projects the log into `AuditRecord`s
that name the actor, the action, the outcome and — where the decision was
a governed one — the policy that produced it, so a denial in the trail
can be traced to the sentence in the prompt it came from.

Three things it does that a log dump does not:

- **Integrity is checked, not assumed.** `verify()` walks the log's own
  hash chain and, when a constitution is supplied, re-verifies every
  policy signature. An export from a trail that failed verification says
  so in the export, because the one thing worse than no audit trail is a
  tampered one that reads as clean.
- **Secrets are redacted on the way out.** The log holds whatever tools
  put in it, and tool output holds API keys. Redaction happens at export,
  against the raw text, so a key that reached the log still cannot reach
  a report.
- **The dashboard answers the compliance question directly.** Not "here
  are 4,000 events" but: which constitution is in force, how many actions
  the policy stopped, how many replies were regenerated and why, and
  where each model's compliance currently sits.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from dataclasses import dataclass, field

# Redaction patterns. Written to catch the shapes that actually appear in
# this codebase's own config and provider traffic — a generic "looks like
# a token" rule produces enough false positives to make an export
# unreadable, and an unreadable export is an unused one.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}")),
    ("env-key", re.compile(
        r"\b[A-Z][A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD)\s*[=:]\s*"
        r"['\"]?([^\s'\"]{6,})")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("hex-secret", re.compile(r"\b[0-9a-f]{40,}\b")),
)

REDACTED = "[REDACTED]"

# How each event type reads in an audit trail: (category, actor-ish kind).
CATEGORIES: dict[str, str] = {
    "user.message": "request",
    "assistant.message": "response",
    "tool.call": "action",
    "tool.result": "action",
    "tool.blocked": "action",
    "judge.verdict": "verification",
    "turn.scorecard": "verification",
    "policy.decision": "policy",
    "guardrail.action": "policy",
    "guardrail.verify": "policy",
    "guardrail.correction": "correction",
    "guardrail.refusal": "correction",
    "compliance.observation": "compliance",
    "compliance.level": "compliance",
    "compliance.drift": "compliance",
    "constitution.ratified": "governance",
    "constitution.amended": "governance",
    "constitution.tamper": "governance",
    "benchmark.report": "benchmark",
    "cost.incurred": "cost",
    "session.start": "session",
}


def redact(text: str) -> str:
    """Remove secret-shaped substrings. Applied to rendered text, always."""
    out = text
    for _, pattern in SECRET_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def _summarize(event_type: str, data: dict) -> str:
    """One readable line for an event, without dumping its payload."""
    if event_type == "tool.call":
        return f"{data.get('name', '?')}({_brief_args(data.get('args'))})"
    if event_type == "tool.result":
        return f"{data.get('name', '?')} -> {data.get('status', '?')}"
    if event_type == "policy.decision":
        return (f"{data.get('outcome', '?')} {data.get('tool', '?')}: "
                f"{data.get('reason', '')}")
    if event_type == "guardrail.action":
        v = data.get("violations") or []
        return f"{data.get('tool', '?')}: {len(v)} violation(s)"
    if event_type == "guardrail.verify":
        return (f"{'pass' if data.get('ok') else 'fail'} at "
                f"{data.get('level', '?')}: {data.get('violations', 0)} "
                f"violation(s)")
    if event_type == "guardrail.correction":
        return f"regenerating after attempt {data.get('attempt', '?')}"
    if event_type == "compliance.level":
        return (f"{data.get('model', '?')}: {data.get('from', '?')} -> "
                f"{data.get('to', '?')}")
    if event_type == "compliance.drift":
        return (f"{data.get('model', '?')} drifted "
                f"{data.get('baseline', '?')} -> {data.get('window', '?')}")
    if event_type in ("constitution.ratified", "constitution.amended"):
        return (f"v{data.get('version', '?')} · "
                f"{len(data.get('policies') or [])} policies")
    if event_type == "constitution.tamper":
        return f"TAMPER: {len(data.get('tampered') or [])} policies"
    if event_type == "benchmark.report":
        return (f"{data.get('model', '?')} scored {data.get('score', '?')} "
                f"({data.get('passed', '?')}/{data.get('total', '?')})")
    for key in ("text", "reason", "summary", "error"):
        if isinstance(data.get(key), str):
            return data[key][:160]
    return ""


def _brief_args(args) -> str:
    if not isinstance(args, dict):
        return ""
    parts = []
    for k, v in list(args.items())[:3]:
        text = str(v)
        parts.append(f"{k}={text[:40]}")
    return ", ".join(parts)


def _policy_ref(event_type: str, data: dict) -> str:
    """The policy a governed decision rests on, when there is one."""
    if event_type in ("guardrail.action", "guardrail.verify"):
        violations = data.get("violations")
        if isinstance(violations, list) and violations:
            first = violations[0]
            if isinstance(first, dict):
                return str(first.get("policy_id", ""))
        stages = data.get("stages")
        if isinstance(stages, list):
            for stage in stages:
                for v in (stage or {}).get("violations", []) or []:
                    return str(v.get("policy_id", ""))
    if event_type == "policy.decision":
        return f"role:{data.get('role', '')}/{data.get('rule', '')}"
    return ""


@dataclass(frozen=True)
class AuditRecord:
    """One line of the trail: who did what, under which rule, and how it went."""
    seq: int
    at: float
    category: str
    event_type: str
    actor: str
    summary: str
    policy_ref: str = ""
    provenance: str = ""
    event_id: str = ""

    def to_dict(self) -> dict:
        return {"seq": self.seq, "at": self.at, "category": self.category,
                "event": self.event_type, "actor": self.actor,
                "summary": redact(self.summary), "policy_ref": self.policy_ref,
                "provenance": self.provenance, "event_id": self.event_id}

    def line(self) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(self.at))
        ref = f"  [{self.policy_ref}]" if self.policy_ref else ""
        return (f"{self.seq:>6} {stamp} {self.category:<13} "
                f"{self.actor:<10} {redact(self.summary)[:80]}{ref}")


@dataclass
class IntegrityReport:
    log_ok: bool
    log_detail: str
    constitution_ok: bool | None = None
    constitution_detail: str = ""

    @property
    def ok(self) -> bool:
        return self.log_ok and self.constitution_ok is not False

    def format(self) -> str:
        lines = ["INTEGRITY " + ("OK" if self.ok else "FAILED"),
                 f"  event log: {'ok' if self.log_ok else 'FAILED'} — "
                 f"{self.log_detail}"]
        if self.constitution_ok is not None:
            lines.append(f"  constitution: "
                         f"{'ok' if self.constitution_ok else 'FAILED'} — "
                         f"{self.constitution_detail}")
        return "\n".join(lines)


@dataclass
class Dashboard:
    """The compliance picture of one session, in numbers."""
    constitution_version: int = 0
    constitution_root: str = ""
    policies: int = 0
    events: int = 0
    actions: int = 0
    policy_denials: int = 0
    guardrail_checks: int = 0
    guardrail_failures: int = 0
    corrections: int = 0
    refusals: int = 0
    drifts: int = 0
    tampering: int = 0
    level_changes: tuple[str, ...] = ()
    by_category: dict[str, int] = field(default_factory=dict)
    integrity: IntegrityReport | None = None

    @property
    def clean_rate(self) -> float:
        if not self.guardrail_checks:
            return 1.0
        return round(1 - self.guardrail_failures / self.guardrail_checks, 4)

    def to_dict(self) -> dict:
        d = {"constitution_version": self.constitution_version,
             "constitution_root": self.constitution_root,
             "policies": self.policies, "events": self.events,
             "actions": self.actions, "policy_denials": self.policy_denials,
             "guardrail_checks": self.guardrail_checks,
             "guardrail_failures": self.guardrail_failures,
             "clean_rate": self.clean_rate, "corrections": self.corrections,
             "refusals": self.refusals, "drifts": self.drifts,
             "tampering": self.tampering,
             "level_changes": list(self.level_changes),
             "by_category": dict(self.by_category)}
        if self.integrity is not None:
            d["integrity_ok"] = self.integrity.ok
        return d

    def format(self) -> str:
        head = "COMPLIANCE DASHBOARD"
        if self.tampering:
            head += "  ⚠ TAMPERING DETECTED"
        lines = [head]
        if self.constitution_version:
            lines.append(f"  constitution v{self.constitution_version} "
                         f"({self.constitution_root[:12]}) · "
                         f"{self.policies} policies")
        else:
            lines.append("  constitution: none in force")
        lines.append(f"  events {self.events} · actions {self.actions}")
        lines.append(f"  guardrail: {self.guardrail_checks} checks, "
                     f"{self.guardrail_failures} failed "
                     f"({self.clean_rate * 100:.1f}% clean)")
        lines.append(f"  policy denials {self.policy_denials} · "
                     f"regenerations {self.corrections} · "
                     f"refusals {self.refusals}")
        if self.level_changes:
            lines.append("  enforcement: " + "; ".join(self.level_changes[-4:]))
        if self.drifts:
            lines.append(f"  ⚠ drift events: {self.drifts}")
        if self.integrity is not None and not self.integrity.ok:
            lines.append("  ⚠ integrity check FAILED — this trail is not "
                         "trustworthy")
        return "\n".join(lines)


class AuditTrail:
    """The readable, verifiable projection of one session's event log."""

    def __init__(self, log, constitution=None, signing_key: bytes | None = None):
        self.log = log
        self.constitution = constitution
        self.signing_key = signing_key

    def records(self, categories: tuple[str, ...] | None = None,
                limit: int | None = None) -> tuple[AuditRecord, ...]:
        out: list[AuditRecord] = []
        for ev in self.log.events():
            category = CATEGORIES.get(ev.type, "other")
            if categories and category not in categories:
                continue
            data = ev.data if isinstance(ev.data, dict) else {}
            out.append(AuditRecord(
                seq=getattr(ev, "seq", 0), at=float(getattr(ev, "ts", 0) or 0),
                category=category, event_type=ev.type,
                actor=str(getattr(ev, "actor", "") or "?"),
                summary=_summarize(ev.type, data),
                policy_ref=_policy_ref(ev.type, data),
                provenance=str(getattr(ev, "provenance", "") or ""),
                event_id=str(getattr(ev, "id", "") or "")))
        if limit is not None:
            out = out[-limit:]
        return tuple(out)

    def verify(self) -> IntegrityReport:
        try:
            log_ok, detail = self.log.verify()
        except Exception as exc:
            log_ok, detail = False, f"verification raised: {exc}"
        const_ok: bool | None = None
        const_detail = ""
        if self.constitution is not None and self.signing_key is not None:
            report = self.constitution.verify(self.signing_key)
            const_ok = report.ok
            const_detail = (f"{report.checked} policies"
                            if report.ok
                            else f"{len(report.tampered)} failed signature")
        return IntegrityReport(log_ok=bool(log_ok), log_detail=str(detail),
                               constitution_ok=const_ok,
                               constitution_detail=const_detail)

    def dashboard(self) -> Dashboard:
        d = Dashboard()
        if self.constitution is not None:
            d.constitution_version = self.constitution.version
            d.constitution_root = self.constitution.root
            d.policies = len(self.constitution.policies)
        for ev in self.log.events():
            d.events += 1
            category = CATEGORIES.get(ev.type, "other")
            d.by_category[category] = d.by_category.get(category, 0) + 1
            data = ev.data if isinstance(ev.data, dict) else {}
            if ev.type == "tool.call":
                d.actions += 1
            elif ev.type == "policy.decision":
                if data.get("outcome") == "deny":
                    d.policy_denials += 1
            elif ev.type == "guardrail.verify":
                d.guardrail_checks += 1
                if not data.get("ok"):
                    d.guardrail_failures += 1
            elif ev.type == "guardrail.correction":
                d.corrections += 1
            elif ev.type == "guardrail.refusal":
                d.refusals += 1
            elif ev.type == "compliance.drift":
                d.drifts += 1
            elif ev.type == "constitution.tamper":
                d.tampering += 1
            elif ev.type == "compliance.level":
                d.level_changes = d.level_changes + (
                    f"{data.get('model', '?')} {data.get('from', '?')}→"
                    f"{data.get('to', '?')}",)
        d.integrity = self.verify()
        return d

    def export(self, fmt: str = "text", limit: int | None = None) -> str:
        """Render the trail. Every path goes through `redact`."""
        records = self.records(limit=limit)
        integrity = self.verify()
        if fmt == "json":
            return json.dumps({"integrity_ok": integrity.ok,
                               "integrity": integrity.format(),
                               "dashboard": self.dashboard().to_dict(),
                               "records": [r.to_dict() for r in records]},
                              indent=2)
        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["seq", "at", "category", "event", "actor",
                             "summary", "policy_ref", "provenance"])
            for r in records:
                writer.writerow([r.seq, r.at, r.category, r.event_type,
                                 r.actor, redact(r.summary), r.policy_ref,
                                 r.provenance])
            return buf.getvalue()
        lines = [self.dashboard().format(), "", integrity.format(), "",
                 "AUDIT TRAIL"]
        lines.extend(r.line() for r in records)
        return "\n".join(lines)


if __name__ == "__main__":
    import tempfile
    from pathlib import Path as _Path

    from . import systemprompt
    from .constitution import ConstitutionalCore
    from .guardrail import BLOCK, Guardrail, ResponseFacts
    from .kernel import EventLog
    from .toolpolicy import ToolPolicy

    # --- redaction, on the shapes that actually appear -----------------
    assert REDACTED in redact("key sk-xt-abc123def456ghi789jkl")
    assert REDACTED in redact("Authorization: Bearer abcdef123456789")
    assert REDACTED in redact("XKIRO_API_KEY=c6509a643f568f821c36")
    assert REDACTED in redact("-----BEGIN RSA PRIVATE KEY-----")
    assert redact("nothing secret here") == "nothing secret here"

    with tempfile.TemporaryDirectory() as td:
        root = _Path(td)
        (root / "real.py").write_text("x = 1\n")
        log = EventLog(root / "a.jsonl")
        core = ConstitutionalCore(log, app_dir=root)
        const = core.ratify_prompt("main", systemprompt.MAIN)
        g = Guardrail(const, log=log, level=BLOCK)
        policy = ToolPolicy("readonly", log=log, roots=(str(root),))

        log.append("user.message", {"text": "fix the retry bug"},
                   actor="user")
        log.append("tool.call", {"name": "read_file",
                                 "args": {"path": "real.py"}}, actor="sovereign")
        log.append("tool.result", {"name": "read_file", "status": "done"},
                   actor="kernel")
        policy.evaluate("write_file", {"path": str(root / "x.py")})   # denied
        g.verify_response(ResponseFacts("Fixed it in ghost/missing.py.",
                                        root=root))                  # fails
        g.verify_response(ResponseFacts("Read real.py; nothing changed yet.",
                                        root=root))                  # clean
        log.append("assistant.message",
                   {"text": "the key is sk-xt-deadbeefdeadbeefdead"},
                   actor="sovereign")

        trail = AuditTrail(log, constitution=const, signing_key=core.key)

        # --- records carry actor, category and the policy behind them ---
        records = trail.records()
        assert records, "the trail must not be empty"
        assert any(r.category == "policy" for r in records)
        assert any(r.category == "governance" for r in records)
        governed = [r for r in records if r.policy_ref]
        assert governed, "a governed decision must name its policy"

        # --- a secret that reached the log must not reach the export ----
        for fmt in ("text", "json", "csv"):
            out = trail.export(fmt)
            assert "sk-xt-deadbeef" not in out, f"{fmt} export leaked a key"
            assert REDACTED in out, fmt

        # --- the dashboard answers the compliance question --------------
        dash = trail.dashboard()
        assert dash.policies == len(const.policies)
        assert dash.policy_denials >= 1, dash.to_dict()
        assert dash.guardrail_checks == 2 and dash.guardrail_failures == 1
        assert dash.clean_rate == 0.5, dash.clean_rate
        assert dash.actions == 1
        assert dash.integrity is not None and dash.integrity.ok
        assert "COMPLIANCE DASHBOARD" in dash.format()

        # --- a tampered log is reported, never rendered as clean --------
        target = root / "a.jsonl"
        raw = target.read_text().splitlines()
        victim = next(i for i, line in enumerate(raw)
                      if "fix the retry bug" in line)
        doctored = raw[:]
        doctored[victim] = doctored[victim].replace(
            "fix the retry bug", "do something else entirely")
        assert doctored != raw, "the edit must actually change the log"
        target.write_text("\n".join(doctored) + "\n")
        reopened = AuditTrail(EventLog(target), constitution=const,
                              signing_key=core.key)
        integrity = reopened.verify()
        assert not integrity.ok, "an edited log must fail verification"
        assert "FAILED" in integrity.format()
        assert "not\n  trustworthy" in reopened.dashboard().format() or \
            "not trustworthy" in reopened.dashboard().format()

        print(dash.format())
        print("AUDIT SELF-TEST PASS")

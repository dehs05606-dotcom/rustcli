"""Dispatch core — the one path a tool call travels.

Everything a call needs to survive contact with production happens here,
in a fixed order: validate against the contract, ask policy, ask the
human when the contract says the action is destructive or leaves the
machine, run it under a timeout, retry only what the contract says is
retryable, convert anything that escapes into a typed error, and seal the
whole thing to the log under a trace id.

The ordering is the design. Validation before policy, because an
unparseable call should not consume a permission decision. Policy before
approval, because a human should never be asked to approve something the
session was never allowed to do. Approval before execution, because that
is what approval means.

**Module boundary.** This file imports the contract layer and the policy
layer and nothing else from the agent. It never imports `tools`. The
registry is handed in, so the core has no knowledge of any tool's
internals and a new tool needs no change here.

**Two honest limits.**

- A timeout *abandons* a call; it does not kill it. Handlers are ordinary
  synchronous Python running in a worker thread, and Python cannot
  preempt one. The dispatcher stops waiting and returns `E_TIMEOUT` while
  the thread runs on as a daemon. Anything that must really stop needs
  its own internal timeout — `run_command` has one — and the contract's
  timeout is the outer bound, not the mechanism.
- Retrying is only safe for calls the contract marks retryable *and*
  idempotent. A retried `edit_file` would apply a second replacement, so
  `UNSAFE` and `NON_IDEMPOTENT` calls are never retried regardless of
  error code, whatever the retry policy says.
"""

from __future__ import annotations

import inspect
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .toolcontract import (E_INTERNAL, E_NOT_FOUND, E_PERMISSION, E_TIMEOUT,
                           IDEMPOTENT, ToolContract, ToolError, classify,
                           from_error_text)
from .toolpolicy import ASK, DENY, ToolPolicy


def new_trace_id() -> str:
    """A trace id for one logical operation, propagated across calls."""
    return uuid.uuid4().hex[:16]


def new_span_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class TraceContext:
    """The identity a call carries through every layer that touches it."""
    trace_id: str = field(default_factory=new_trace_id)
    span_id: str = field(default_factory=new_span_id)
    parent_span: str = ""

    def child(self) -> "TraceContext":
        return TraceContext(trace_id=self.trace_id, span_id=new_span_id(),
                            parent_span=self.span_id)

    def to_dict(self) -> dict:
        return {"trace_id": self.trace_id, "span_id": self.span_id,
                "parent_span": self.parent_span}


@dataclass
class ToolResult:
    """The outcome of one dispatched call. Never raises, always typed."""
    tool: str
    ok: bool
    value: Any = None
    error: ToolError | None = None
    attempts: int = 1
    duration: float = 0.0
    trace: TraceContext = field(default_factory=TraceContext)
    approved: bool | None = None
    migrated: str = ""      # the shim that carried an older caller across

    @property
    def trace_id(self) -> str:
        return self.trace.trace_id

    @property
    def span_id(self) -> str:
        return self.trace.span_id

    @property
    def parent_span(self) -> str:
        return self.trace.parent_span

    def render(self) -> str:
        """What the agent loop puts back into the conversation."""
        if self.ok:
            return self.value if isinstance(self.value, str) else str(self.value)
        return self.error.render() if self.error else "ERROR: unknown failure"

    def to_dict(self) -> dict:
        d = {"tool": self.tool, "ok": self.ok, "attempts": self.attempts,
             "duration": round(self.duration, 4), "approved": self.approved}
        if self.migrated:
            d["migrated"] = self.migrated
        d.update(self.trace.to_dict())
        if self.error is not None:
            d["error"] = self.error.to_dict()
        return d


@dataclass
class Metrics:
    """Counters the dashboard reads. Cheap enough to always be on."""
    calls: dict[str, int] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    errors_by_code: dict[str, int] = field(default_factory=dict)
    duration_total: dict[str, float] = field(default_factory=dict)
    retries: int = 0
    denials: int = 0
    approvals_requested: int = 0
    approvals_refused: int = 0

    def record(self, result: ToolResult) -> None:
        name = result.tool
        self.calls[name] = self.calls.get(name, 0) + 1
        self.duration_total[name] = round(
            self.duration_total.get(name, 0.0) + result.duration, 4)
        self.retries += max(0, result.attempts - 1)
        if not result.ok:
            self.failures[name] = self.failures.get(name, 0) + 1
            if result.error is not None:
                code = result.error.code
                self.errors_by_code[code] = self.errors_by_code.get(code, 0) + 1

    def mean_duration(self, name: str) -> float:
        n = self.calls.get(name, 0)
        return round(self.duration_total.get(name, 0.0) / n, 4) if n else 0.0

    def to_dict(self) -> dict:
        return {"calls": dict(self.calls), "failures": dict(self.failures),
                "errors_by_code": dict(self.errors_by_code),
                "retries": self.retries, "denials": self.denials,
                "approvals_requested": self.approvals_requested,
                "approvals_refused": self.approvals_refused,
                "mean_duration": {n: self.mean_duration(n)
                                  for n in sorted(self.calls)}}

    def format(self) -> str:
        if not self.calls:
            return "TOOL METRICS — no calls yet"
        lines = ["TOOL METRICS",
                 f"  {'tool':<20} {'calls':>6} {'fail':>5} {'mean s':>8}"]
        for name in sorted(self.calls, key=lambda n: -self.calls[n]):
            lines.append(f"  {name[:20]:<20} {self.calls[name]:>6} "
                         f"{self.failures.get(name, 0):>5} "
                         f"{self.mean_duration(name):>8.3f}")
        lines.append(f"  retries {self.retries} · denials {self.denials} · "
                     f"approvals asked {self.approvals_requested}, "
                     f"refused {self.approvals_refused}")
        if self.errors_by_code:
            lines.append("  errors: " + ", ".join(
                f"{k}×{v}" for k, v in sorted(self.errors_by_code.items())))
        return "\n".join(lines)


@dataclass
class Availability:
    """What capability negotiation concluded, and why."""
    available: tuple[str, ...]
    unavailable: dict[str, str]

    def to_dict(self) -> dict:
        return {"available": list(self.available),
                "unavailable": dict(self.unavailable)}

    def format(self) -> str:
        lines = [f"CAPABILITY NEGOTIATION — {len(self.available)} available, "
                 f"{len(self.unavailable)} withheld"]
        for name, why in sorted(self.unavailable.items()):
            lines.append(f"  - {name}: {why}")
        return "\n".join(lines)


class Dispatcher:
    """Registration, discovery, negotiation and the one call path."""

    def __init__(self, policy: ToolPolicy | None = None, log=None,
                 approve: Callable[[ToolContract, dict], bool] | None = None,
                 default_deny_approval: bool = True,
                 migrate: Callable[[str, dict], tuple[dict, str]] | None = None):
        self.policy = policy
        self.log = log
        self.approve = approve
        # An optional shim that carries arguments written against an older
        # contract into the current shape, applied before validation. It is
        # a plain callable rather than a governance object on purpose: the
        # call path should not have to know what a semantic version is.
        self.migrate = migrate
        # With no approval hook wired, a destructive or outward-facing
        # call is refused rather than run. The brief calls this
        # default-deny, and it is the only safe reading: a missing hook is
        # an unanswered question, and an unanswered question is not a yes.
        self.default_deny_approval = default_deny_approval
        self.handlers: dict[str, Callable[..., Any]] = {}
        self.contracts: dict[str, ToolContract] = {}
        self.metrics = Metrics()

    # -- registration and discovery ---------------------------------------

    def register(self, contract: ToolContract,
                 handler: Callable[..., Any]) -> None:
        self.handlers[contract.name] = handler
        self.contracts[contract.name] = contract
        if self.policy is not None:
            # The manifest and the contract must agree, or policy would
            # gate on one set of capabilities while the contract promised
            # another. The contract is the source.
            self.policy.register(contract.name, contract.permission)

    def register_registry(self, registry: dict,
                          contracts: dict[str, ToolContract]) -> None:
        for name, contract in contracts.items():
            tool = registry.get(name)
            if tool is not None:
                self.register(contract, tool.handler)

    def discover(self) -> tuple[ToolContract, ...]:
        return tuple(self.contracts[n] for n in sorted(self.contracts))

    def contract(self, name: str) -> ToolContract | None:
        return self.contracts.get(name)

    def negotiate(self) -> Availability:
        """Which tools this session can actually use, and why not.

        Graceful degradation depends on this being answerable *before* a
        call: an agent told up front that it holds no `proc.exec` plans
        differently from one that discovers it on its tenth denied call.
        """
        available: list[str] = []
        unavailable: dict[str, str] = {}
        for name, contract in sorted(self.contracts.items()):
            if self.policy is None:
                available.append(name)
                continue
            decision = self.policy.evaluate(name, {})
            if decision.outcome == DENY and decision.rule in (
                    "manifest", "capability"):
                unavailable[name] = decision.reason
            else:
                available.append(name)
        return Availability(tuple(available), unavailable)

    def schemas(self) -> list[dict]:
        """OpenAI-shaped tool schemas for the tools currently available."""
        usable = set(self.negotiate().available)
        return [{"type": "function",
                 "function": {"name": c.name, "description": c.description,
                              "parameters": c.input_schema}}
                for c in self.discover() if c.name in usable]

    # -- the call path -----------------------------------------------------

    def _emit(self, event: str, data: dict) -> None:
        if self.log is None:
            return
        try:
            self.log.append(event, data, actor="kernel")
        except Exception:
            pass

    def _run_guarded(self, name: str, handler: Callable[..., Any],
                     args: dict, timeout: float) -> tuple[Any, ToolError | None]:
        """Run a handler in a worker thread, bounded by `timeout`."""
        box: dict[str, Any] = {}

        # Binding is checked before the call so that a signature mismatch
        # reads as the contract defect it is, and a TypeError raised inside
        # the tool body is not mistaken for one.
        try:
            inspect.signature(handler).bind(**args)
        except TypeError as exc:
            return None, ToolError(
                E_INTERNAL, f"handler rejected its arguments: {exc}",
                tool=name)
        except ValueError:  # a builtin that exposes no signature
            pass

        def work() -> None:
            try:
                box["value"] = handler(**args)
            except Exception as exc:
                # Nothing escapes a tool boundary. A caller that had to
                # wrap every call would eventually forget one. The code
                # comes from the taxonomy's own classifier, so a dropped
                # connection is retryable upstream rather than a defect.
                box["error"] = ToolError(
                    classify(exc), f"{type(exc).__name__}: {exc}", tool=name)

        thread = threading.Thread(target=work, daemon=True,
                                  name=f"tool-{name}")
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            return None, ToolError(
                E_TIMEOUT,
                f"exceeded its {timeout:g}s budget and was abandoned",
                tool=name, details={"abandoned": True})
        if "error" in box:
            return None, box["error"]
        return box.get("value"), None

    def call(self, name: str, args: dict | None = None,
             trace: TraceContext | None = None,
             approve: Callable[[ToolContract, dict], bool] | None = None
             ) -> ToolResult:
        """Dispatch one call. Returns a result; never raises."""
        args = dict(args or {})
        ctx = trace.child() if trace is not None else TraceContext()
        started = time.time()

        contract = self.contracts.get(name)
        if contract is None:
            return self._finish(ToolResult(
                name, False, error=ToolError(
                    E_NOT_FOUND, f"no tool named '{name}' is registered",
                    tool=name, trace_id=ctx.trace_id),
                trace=ctx, duration=time.time() - started))

        migrated = ""
        if self.migrate is not None:
            try:
                args, migrated = self.migrate(name, args)
            except Exception as exc:
                # A shim that throws has migrated nothing; the original
                # arguments go to validation, which will say what is
                # wrong with them in terms the caller can act on.
                migrated = f"migration failed: {type(exc).__name__}: {exc}"

        bad = contract.validate_input(args)
        if bad is not None:
            return self._finish(ToolResult(
                name, False, error=ToolError(
                    bad.code, bad.message, tool=name, field=bad.field,
                    trace_id=ctx.trace_id),
                trace=ctx, migrated=migrated,
                duration=time.time() - started))

        needs_approval = contract.needs_approval
        if self.policy is not None:
            decision = self.policy.evaluate(name, args)
            if decision.outcome == DENY:
                self.metrics.denials += 1
                return self._finish(ToolResult(
                    name, False, error=ToolError(
                        E_PERMISSION, decision.reason, tool=name,
                        details={"rule": decision.rule,
                                 "role": decision.role},
                        trace_id=ctx.trace_id),
                    trace=ctx, migrated=migrated,
                    duration=time.time() - started))
            if decision.outcome == ASK:
                needs_approval = True

        approved: bool | None = None
        if needs_approval:
            hook = approve or self.approve
            self.metrics.approvals_requested += 1
            if hook is None:
                approved = not self.default_deny_approval
            else:
                try:
                    approved = bool(hook(contract, args))
                except Exception:
                    approved = False   # a broken hook is not consent
            if not approved:
                self.metrics.approvals_refused += 1
                return self._finish(ToolResult(
                    name, False, approved=False, error=ToolError(
                        E_PERMISSION,
                        "this action needs approval and was not approved",
                        tool=name, details={"destructive": contract.destructive,
                                            "outward_facing":
                                                contract.outward_facing},
                        trace_id=ctx.trace_id),
                    trace=ctx, migrated=migrated,
                    duration=time.time() - started))

        handler = self.handlers[name]
        # Only a call that can be safely repeated is ever repeated.
        repeatable = contract.idempotency == IDEMPOTENT
        attempt = 0
        error: ToolError | None = None
        value: Any = None
        while True:
            attempt += 1
            value, error = self._run_guarded(name, handler, args,
                                             contract.timeout_seconds)
            if error is None:
                # A tool that reports failure by returning "ERROR: ..."
                # would otherwise be recorded as a successful call whose
                # output happens to describe a failure -- and a retryable
                # one would never be retried.
                error = from_error_text(value, tool=name)
            if error is None:
                break
            if not repeatable or not contract.retry.should_retry(error, attempt):
                break
            if contract.retry.backoff_seconds:
                time.sleep(contract.retry.backoff_seconds)

        if error is None:
            bad_out = contract.validate_output(value)
            if bad_out is not None:
                error = ToolError(bad_out.code,
                                  f"the tool returned {type(value).__name__}, "
                                  f"which its contract does not allow",
                                  tool=name, trace_id=ctx.trace_id)

        if error is not None and not error.trace_id:
            error = ToolError(error.code, error.message, error.tool,
                              error.field, error.details, ctx.trace_id)
        return self._finish(ToolResult(
            name, error is None, value=value if error is None else None,
            error=error, attempts=attempt, approved=approved, trace=ctx,
            migrated=migrated, duration=time.time() - started))

    def _finish(self, result: ToolResult) -> ToolResult:
        self.metrics.record(result)
        if self.policy is not None and result.ok:
            try:
                self.policy.record_call(result.tool)
            except Exception:
                pass
        self._emit("dispatch.call", result.to_dict())
        return result

    def format_status(self) -> str:
        parts = [f"DISPATCH — {len(self.contracts)} contracts registered",
                 self.negotiate().format(), self.metrics.format()]
        return "\n".join(parts)


if __name__ == "__main__":
    import tempfile
    from pathlib import Path as _Path

    from .kernel import EventLog
    from .toolcontract import (E_UPSTREAM, E_VALIDATION, NON_IDEMPOTENT,
                               RETRY_NETWORK, TEXT_OUT, RetryPolicy,
                               ToolContract, build_contracts)
    from .toolpolicy import FS_READ, NET_FETCH, PROC_EXEC, ToolPolicy
    from .tools import build_registry

    STR_ARG = {"type": "object",
               "properties": {"path": {"type": "string"}},
               "required": ["path"]}

    with tempfile.TemporaryDirectory() as td:
        root = _Path(td).resolve()
        (root / "real.py").write_text("x = 1\n")
        log = EventLog(root / "d.jsonl")
        policy = ToolPolicy("developer", log=log, roots=(str(root),))
        d = Dispatcher(policy=policy, log=log)

        # --- registration and discovery from the live registry ---------
        registry = build_registry()
        d.register_registry(registry, build_contracts(registry))
        assert d.discover() and d.contract("read_file") is not None

        # --- validation happens before anything else -------------------
        bad = d.call("read_file", {})
        assert not bad.ok and bad.error.code == E_VALIDATION
        assert bad.error.field == "path"
        assert bad.error.trace_id, "every error carries its trace"
        missing = d.call("no_such_tool", {})
        assert missing.error.code == E_NOT_FOUND

        # --- a real call succeeds and is traced ------------------------
        good = d.call("read_file", {"path": str(root / "real.py")})
        assert good.ok, good.to_dict()
        assert "x = 1" in good.render()
        assert good.trace.trace_id and good.trace.span_id

        # --- trace ids propagate down a chain --------------------------
        parent = TraceContext()
        a = d.call("read_file", {"path": str(root / "real.py")}, trace=parent)
        b = d.call("list_dir", {"path": str(root)}, trace=parent)
        assert a.trace.trace_id == b.trace.trace_id == parent.trace_id
        assert a.trace.span_id != b.trace.span_id
        assert a.trace.parent_span == parent.span_id

        # --- policy denial comes back typed, not raised ----------------
        outside = d.call("read_file", {"path": "/etc/passwd"})
        assert not outside.ok and outside.error.code == E_PERMISSION
        assert outside.error.details["rule"] == "path-confinement"

        # --- default-deny: no hook means a destructive call is refused -
        doomed = d.call("delete_path", {"path": str(root / "real.py")})
        assert not doomed.ok and doomed.approved is False
        assert (root / "real.py").exists(), "a refused delete must not run"

        # --- an approval hook lets it through --------------------------
        allowed = Dispatcher(policy=policy, log=log,
                             approve=lambda c, a: True)
        allowed.register_registry(registry, build_contracts(registry))
        victim = root / "gone.py"
        victim.write_text("bye\n")
        out = allowed.call("delete_path", {"path": str(victim)})
        assert out.ok and out.approved is True, out.to_dict()
        assert not victim.exists()

        # --- a hook that raises is not consent -------------------------
        def explodes(contract, args):
            raise RuntimeError("hook is broken")

        hostile = Dispatcher(policy=policy, log=log, approve=explodes)
        hostile.register_registry(registry, build_contracts(registry))
        keep = root / "keep.py"
        keep.write_text("stay\n")
        refused = hostile.call("delete_path", {"path": str(keep)})
        assert not refused.ok and keep.exists()

        # --- a raising handler becomes a typed error -------------------
        def boom(**kw):
            raise RuntimeError("internal explosion")

        d.register(ToolContract("boom", "explodes", STR_ARG, TEXT_OUT,
                                frozenset({FS_READ})), boom)
        blew = d.call("boom", {"path": "x"})
        assert not blew.ok and blew.error.code == E_INTERNAL
        assert "internal explosion" in blew.error.message
        assert blew.error.retryable is False

        # --- the code comes from the exception, not from a blanket -----
        # A tool that loses a socket must not be reported as defective:
        # E_INTERNAL says "do not retry", and that would be wrong.
        cases = {"dropped": (ConnectionError("peer went away"), E_UPSTREAM),
                 "missing": (FileNotFoundError("no such file"), E_NOT_FOUND),
                 "refused": (PermissionError("denied"), E_PERMISSION),
                 "bad_arg": (ValueError("count must be positive"),
                             E_VALIDATION)}
        for label, (exc, expected) in cases.items():
            d.register(ToolContract(label, "raises", STR_ARG, TEXT_OUT,
                                    frozenset({FS_READ})),
                       (lambda e: (lambda **kw: (_ for _ in ()).throw(e)))(exc))
            got = d.call(label, {"path": "x"})
            assert got.error.code == expected, (label, got.to_dict())
            assert got.attempts == 1, "no retry without a retry policy"

        # --- a slow handler is abandoned, not waited on ----------------
        def slow(**kw):
            time.sleep(5)
            return "too late"

        d.register(ToolContract("slow", "sleeps", STR_ARG, TEXT_OUT,
                                frozenset({FS_READ}), timeout_seconds=0.2),
                   slow)
        late = d.call("slow", {"path": "x"})
        assert not late.ok and late.error.code == E_TIMEOUT
        assert late.duration < 2, late.duration

        # --- retry only for idempotent tools ---------------------------
        tries = {"n": 0}

        def flaky(**kw):
            tries["n"] += 1
            if tries["n"] < 3:
                raise ConnectionError("upstream hiccup")
            return "third time"

        # The taxonomy decides what may be repeated, so a retry policy can
        # only narrow it: naming a code the taxonomy calls non-retryable
        # buys nothing. ConnectionError classifies as E_UPSTREAM, which is
        # retryable, which is why this recovers at all.
        retry_all = RetryPolicy(max_attempts=3, codes=(E_UPSTREAM, E_TIMEOUT))
        assert not RetryPolicy(max_attempts=3, codes=(E_INTERNAL,)).should_retry(
            ToolError(E_INTERNAL, "defect"), 1), \
            "a policy must not resurrect a code the taxonomy calls final"
        d.register(ToolContract("flaky", "fails twice", STR_ARG, TEXT_OUT,
                                frozenset({FS_READ}),
                                idempotency=IDEMPOTENT, retry=retry_all),
                   flaky)
        recovered = d.call("flaky", {"path": "x"})
        assert recovered.ok and recovered.attempts == 3, recovered.to_dict()

        tries["n"] = 0
        d.register(ToolContract("flaky_write", "fails twice", STR_ARG,
                                TEXT_OUT, frozenset({FS_READ}),
                                idempotency=NON_IDEMPOTENT, retry=retry_all),
                   flaky)
        once = d.call("flaky_write", {"path": "x"})
        assert not once.ok and once.attempts == 1, \
            "a non-idempotent call must never be retried"

        # --- a contract-breaking return value is caught ----------------
        d.register(ToolContract("wrong", "returns a dict", STR_ARG, TEXT_OUT,
                                frozenset({FS_READ})), lambda **kw: {"a": 1})
        typed = d.call("wrong", {"path": "x"})
        assert not typed.ok and typed.error.code == E_VALIDATION

        # --- negotiation tells a session what it holds, before it asks -
        readonly = Dispatcher(policy=ToolPolicy("readonly", log=log,
                                                roots=(str(root),)), log=log)
        readonly.register_registry(registry, build_contracts(registry))
        avail = readonly.negotiate()
        assert "read_file" in avail.available
        assert "run_command" in avail.unavailable
        assert "write_file" in avail.unavailable
        assert len(readonly.schemas()) == len(avail.available)
        assert "CAPABILITY NEGOTIATION" in avail.format()

        # --- metrics ---------------------------------------------------
        m = d.metrics
        assert m.calls["read_file"] >= 3
        assert m.errors_by_code.get(E_TIMEOUT, 0) >= 1
        assert m.approvals_requested >= 1 and m.approvals_refused >= 1
        assert m.retries >= 2
        assert any(e.type == "dispatch.call" for e in log.events())

        print(readonly.negotiate().format())
        print(d.metrics.format())
        print("DISPATCH SELF-TEST PASS")

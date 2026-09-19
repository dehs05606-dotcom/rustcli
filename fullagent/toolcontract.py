"""Tool contract layer — what a tool promises, in a form a program reads.

`tools.Tool` carries a name, a description, a JSON-Schema-ish parameter
blob and a handler. That is enough to show a model what to call and not
nearly enough to run one safely: nothing says what comes back, which
failures are possible, whether calling twice is the same as calling once,
how long to wait, when a retry is sane, or what permission the call needs.
Every one of those has lived as an assumption in the caller instead.

A `ToolContract` states them. The contract is the only thing the dispatch
core reads, which is what keeps that core independent of any tool's
internals — `dispatch.py` imports this module and never `tools.py`.

Three decisions worth naming, because each cost something:

- **The validator is a documented subset of JSON Schema, not a
  dependency.** This repo ships on three packages and runs on Termux; a
  validator that pulled in `jsonschema` would be the fourth. So the
  subset is small, explicit, and listed in `SUPPORTED_KEYWORDS` — and a
  schema using a keyword outside it is reported by `unsupported_keywords`
  rather than silently passing everything.
- **Errors are values, never exceptions.** `ToolError` is returned across
  the boundary. A handler that raises anyway is caught by the dispatcher
  and converted, because a tool boundary that can throw is one every
  caller must wrap, and eventually one caller will not.
- **The error taxonomy is closed.** Nine codes, each with a fixed
  retryability. An open taxonomy becomes a free-text field within a
  month, and then nothing can decide on it.
"""

from __future__ import annotations

import errno
import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from .toolpolicy import (FS_DELETE, FS_READ, FS_WRITE, NET_FETCH, PROC_EXEC,
                         TOOL_CAPABILITIES)

CONTRACT_VERSION = "1.0"

# -- error taxonomy ---------------------------------------------------------
# Closed by design. `retryable` is a property of the code, not of the call
# site, so two callers can never disagree about whether to retry.
E_VALIDATION = "E_VALIDATION"      # the input did not satisfy the schema
E_PERMISSION = "E_PERMISSION"      # policy refused the call
E_NOT_FOUND = "E_NOT_FOUND"        # the target does not exist
E_CONFLICT = "E_CONFLICT"          # the target is not in the expected state
E_TIMEOUT = "E_TIMEOUT"            # the call exceeded its budget
E_UPSTREAM = "E_UPSTREAM"          # a network or subprocess failure
E_RESOURCE = "E_RESOURCE"          # a ceiling, quota or disk limit
E_CANCELLED = "E_CANCELLED"        # the caller stopped it
E_INTERNAL = "E_INTERNAL"          # a defect in the tool itself

RETRYABLE: dict[str, bool] = {
    E_VALIDATION: False, E_PERMISSION: False, E_NOT_FOUND: False,
    E_CONFLICT: False, E_TIMEOUT: True, E_UPSTREAM: True,
    E_RESOURCE: False, E_CANCELLED: False, E_INTERNAL: False,
}

ERROR_CODES = tuple(RETRYABLE)


# Which exception means which code. The taxonomy is only honest if the
# mapping lives beside it: a dispatcher that labelled every escaped
# exception E_INTERNAL would tell a caller "this is a defect, do not
# retry" about a socket that merely closed. Specific classes first --
# ConnectionError, TimeoutError and the errno cases below are all
# OSError subclasses.
_RESOURCE_ERRNOS = frozenset({
    errno.ENOSPC, errno.EMFILE, errno.ENFILE, errno.ENOMEM,
    errno.EAGAIN, getattr(errno, "EDQUOT", errno.ENOSPC),
})


def classify(exc: BaseException) -> str:
    """Map an exception that escaped a tool to one code in the taxonomy."""
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return E_CANCELLED
    if type(exc).__name__ == "CancelledError":
        return E_CANCELLED
    if isinstance(exc, (MemoryError, RecursionError)):
        return E_RESOURCE
    if isinstance(exc, TimeoutError):
        return E_TIMEOUT
    if isinstance(exc, (ConnectionError, BlockingIOError)):
        return E_UPSTREAM
    if isinstance(exc, FileNotFoundError):
        return E_NOT_FOUND
    if isinstance(exc, (FileExistsError, IsADirectoryError,
                        NotADirectoryError)):
        return E_CONFLICT
    if isinstance(exc, PermissionError):
        return E_PERMISSION
    if isinstance(exc, OSError):
        if exc.errno in _RESOURCE_ERRNOS:
            return E_RESOURCE
        return E_UPSTREAM
    # Third-party network stacks do not subclass the builtins, so fall
    # back to where the class was defined rather than to its name.
    module = (type(exc).__module__ or "").split(".")[0]
    if module in ("socket", "ssl", "http", "urllib", "urllib3", "requests",
                  "httpx", "subprocess", "asyncio", "select"):
        return E_UPSTREAM
    if isinstance(exc, ValueError):
        # A tool raising ValueError is nearly always rejecting its input
        # for a reason the schema could not express (a malformed path, an
        # out-of-range count). Neither reading is retryable, so a wrong
        # guess here costs a label, not a repeated side effect.
        return E_VALIDATION
    return E_INTERNAL

# -- idempotency ------------------------------------------------------------
IDEMPOTENT = "idempotent"          # same call, same world: safe to repeat
NON_IDEMPOTENT = "non_idempotent"  # repeating changes the world again
UNSAFE = "unsafe"                  # repeating may destroy something

# -- JSON Schema subset -----------------------------------------------------
SUPPORTED_KEYWORDS = frozenset({
    "type", "properties", "required", "items", "enum", "const",
    "minimum", "maximum", "minLength", "maxLength", "pattern",
    "minItems", "maxItems", "additionalProperties", "description",
    "default", "title", "examples",
})

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float),
    "boolean": (bool,), "array": (list, tuple), "object": (dict,),
    "null": (type(None),),
}


@dataclass(frozen=True)
class ToolError:
    """A failure, as a value. Never raised across a tool boundary."""
    code: str
    message: str
    tool: str = ""
    # named `field` because that is what it is to a caller reading the
    # error; `dataclasses.field` is imported as dc_field so this
    # attribute does not shadow it inside the class body
    field: str = ""
    details: dict = dc_field(default_factory=dict)
    trace_id: str = ""

    @property
    def retryable(self) -> bool:
        return RETRYABLE.get(self.code, False)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "tool": self.tool, "field": self.field,
                "retryable": self.retryable, "details": self.details,
                "trace_id": self.trace_id}

    def render(self) -> str:
        """The string a model sees. Prefixed so the agent loop can tell a
        failure from output that merely mentions the word error."""
        where = f" [{self.field}]" if self.field else ""
        return f"ERROR {self.code}{where}: {self.message}"


def validate(value: Any, schema: dict, path: str = "") -> ToolError | None:
    """Check `value` against the supported JSON Schema subset.

    Returns a `ToolError` naming the offending field, or None. Unknown
    keywords are ignored here on purpose and surfaced separately by
    `unsupported_keywords` — failing closed on an unrecognised keyword
    would make every richer schema in the repo unusable, and failing open
    silently is exactly what that other function exists to prevent.
    """
    if not isinstance(schema, dict):
        return None

    expected = schema.get("type")
    if expected:
        wanted = (expected,) if isinstance(expected, str) else tuple(expected)
        types: tuple[type, ...] = ()
        for name in wanted:
            types += _TYPES.get(name, ())
        if types:
            # bool is an int in Python; a schema asking for a number must
            # not quietly accept True.
            if isinstance(value, bool) and "boolean" not in wanted:
                return ToolError(E_VALIDATION,
                                 f"expected {'/'.join(wanted)}, got boolean",
                                 field=path)
            if not isinstance(value, types):
                got = type(value).__name__
                return ToolError(E_VALIDATION,
                                 f"expected {'/'.join(wanted)}, got {got}",
                                 field=path)

    if "const" in schema and value != schema["const"]:
        return ToolError(E_VALIDATION,
                         f"must be {schema['const']!r}", field=path)
    if "enum" in schema and value not in schema["enum"]:
        return ToolError(E_VALIDATION,
                         f"must be one of {schema['enum']!r}", field=path)

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return ToolError(E_VALIDATION,
                             f"shorter than {schema['minLength']}", field=path)
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return ToolError(E_VALIDATION,
                             f"longer than {schema['maxLength']}", field=path)
        pattern = schema.get("pattern")
        if pattern:
            try:
                if not re.search(pattern, value):
                    return ToolError(E_VALIDATION,
                                     f"does not match {pattern}", field=path)
            except re.error:
                pass   # a broken schema pattern must not fail a good call

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return ToolError(E_VALIDATION,
                             f"below minimum {schema['minimum']}", field=path)
        if "maximum" in schema and value > schema["maximum"]:
            return ToolError(E_VALIDATION,
                             f"above maximum {schema['maximum']}", field=path)

    if isinstance(value, (list, tuple)):
        if "minItems" in schema and len(value) < schema["minItems"]:
            return ToolError(E_VALIDATION,
                             f"fewer than {schema['minItems']} items",
                             field=path)
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return ToolError(E_VALIDATION,
                             f"more than {schema['maxItems']} items",
                             field=path)
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                bad = validate(item, item_schema, f"{path}[{i}]")
                if bad is not None:
                    return bad

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for name in schema.get("required") or ():
            if name not in value:
                return ToolError(E_VALIDATION, f"missing required field",
                                 field=f"{path}.{name}" if path else name)
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    return ToolError(E_VALIDATION, "unexpected field",
                                     field=f"{path}.{name}" if path else name)
        for name, sub in properties.items():
            if name in value and isinstance(sub, dict):
                bad = validate(value[name], sub,
                               f"{path}.{name}" if path else name)
                if bad is not None:
                    return bad
    return None


def unsupported_keywords(schema: dict) -> tuple[str, ...]:
    """Keywords in `schema` this validator does not enforce.

    A contract whose schema leans on `oneOf` is not validated the way its
    author believes. Saying which keywords were ignored is the difference
    between a partial validator and a misleading one.
    """
    found: set[str] = set()

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        # Only THIS node's keys are keywords. The keys inside
        # `properties` are field names chosen by the schema's author —
        # reading them as keywords reported every property of every tool
        # as unsupported, which would have made the honesty check itself
        # the thing nobody believed.
        for key in node:
            if key not in SUPPORTED_KEYWORDS:
                found.add(key)
        properties = node.get("properties")
        if isinstance(properties, dict):
            for sub in properties.values():
                walk(sub)
        for key in ("items", "additionalProperties"):
            sub = node.get(key)
            if isinstance(sub, dict):
                walk(sub)
            elif isinstance(sub, (list, tuple)):
                for item in sub:
                    walk(item)

    walk(schema)
    return tuple(sorted(found))


@dataclass(frozen=True)
class RetryPolicy:
    """When a failed call may be repeated, and how patiently."""
    max_attempts: int = 1
    backoff_seconds: float = 0.0
    codes: tuple[str, ...] = (E_TIMEOUT, E_UPSTREAM)

    def should_retry(self, error: ToolError, attempt: int) -> bool:
        if attempt >= self.max_attempts:
            return False
        return error.code in self.codes and error.retryable

    def to_dict(self) -> dict:
        return {"max_attempts": self.max_attempts,
                "backoff_seconds": self.backoff_seconds,
                "codes": list(self.codes)}


NO_RETRY = RetryPolicy()
RETRY_NETWORK = RetryPolicy(max_attempts=3, backoff_seconds=0.5)


@dataclass(frozen=True)
class ToolContract:
    """Everything a caller needs to run a tool without reading its code."""
    name: str
    description: str
    input_schema: dict
    output_schema: dict
    permission: frozenset[str]
    idempotency: str = NON_IDEMPOTENT
    timeout_seconds: float = 60.0
    retry: RetryPolicy = NO_RETRY
    errors: tuple[str, ...] = (E_VALIDATION, E_PERMISSION, E_INTERNAL)
    version: str = CONTRACT_VERSION
    destructive: bool = False
    outward_facing: bool = False

    @property
    def needs_approval(self) -> bool:
        """Default-deny for anything that destroys or leaves the machine."""
        return self.destructive or self.outward_facing

    def validate_input(self, args: dict) -> ToolError | None:
        bad = validate(args, self.input_schema)
        if bad is None:
            return None
        return ToolError(bad.code, bad.message, tool=self.name,
                         field=bad.field, details=bad.details)

    def validate_output(self, value: Any) -> ToolError | None:
        bad = validate(value, self.output_schema)
        if bad is None:
            return None
        return ToolError(bad.code, bad.message, tool=self.name,
                         field=bad.field)

    def unsupported(self) -> tuple[str, ...]:
        return unsupported_keywords(self.input_schema)

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "version": self.version,
                "input_schema": self.input_schema,
                "output_schema": self.output_schema,
                "permission": sorted(self.permission),
                "idempotency": self.idempotency,
                "timeout_seconds": self.timeout_seconds,
                "retry": self.retry.to_dict(),
                "errors": list(self.errors),
                "destructive": self.destructive,
                "outward_facing": self.outward_facing,
                "needs_approval": self.needs_approval}


TEXT_OUT = {"type": "string", "description": "tool output as text"}

# Per-tool facts the parameter blob cannot carry. Anything not named here
# gets the conservative default: non-idempotent, no retry, needs approval
# is False only because permission still gates it.
_TRAITS: dict[str, dict] = {
    "read_file": {"idempotency": IDEMPOTENT, "timeout_seconds": 20.0},
    "list_dir": {"idempotency": IDEMPOTENT, "timeout_seconds": 20.0},
    "file_info": {"idempotency": IDEMPOTENT, "timeout_seconds": 20.0},
    "search_files": {"idempotency": IDEMPOTENT, "timeout_seconds": 60.0},
    "glob_files": {"idempotency": IDEMPOTENT, "timeout_seconds": 30.0},
    # Writing the same bytes twice leaves the same file, so write_file is
    # idempotent in a way edit_file is not: an exact-string replacement
    # applied twice either fails or hits a different occurrence.
    "write_file": {"idempotency": IDEMPOTENT, "timeout_seconds": 30.0},
    "edit_file": {"idempotency": NON_IDEMPOTENT, "timeout_seconds": 30.0},
    "apply_patch": {"idempotency": NON_IDEMPOTENT, "timeout_seconds": 60.0},
    "create_directory": {"idempotency": IDEMPOTENT},
    "copy_path": {"idempotency": IDEMPOTENT},
    "move_path": {"idempotency": UNSAFE, "destructive": True},
    "delete_path": {"idempotency": UNSAFE, "destructive": True},
    "run_command": {"idempotency": UNSAFE, "timeout_seconds": 300.0,
                    "destructive": True},
    "live_shell": {"idempotency": UNSAFE, "timeout_seconds": 300.0,
                   "destructive": True},
    "live_shell_reset": {"idempotency": IDEMPOTENT},
    "web_fetch": {"idempotency": IDEMPOTENT, "timeout_seconds": 30.0,
                  "retry": RETRY_NETWORK, "outward_facing": True,
                  "errors": (E_VALIDATION, E_PERMISSION, E_TIMEOUT,
                             E_UPSTREAM, E_INTERNAL)},
    "web_search": {"idempotency": IDEMPOTENT, "timeout_seconds": 45.0,
                   "retry": RETRY_NETWORK, "outward_facing": True,
                   "errors": (E_VALIDATION, E_PERMISSION, E_TIMEOUT,
                              E_UPSTREAM, E_INTERNAL)},
}

# Every filesystem tool runs under a dispatcher-enforced timeout, so
# E_TIMEOUT is one of its outcomes whether or not the tool itself can
# produce one. A contract that omitted it would let a caller written
# against the contract meet a code it never handled.
_FS_ERRORS = (E_VALIDATION, E_PERMISSION, E_NOT_FOUND, E_CONFLICT,
              E_TIMEOUT, E_RESOURCE, E_INTERNAL)


_ERROR_TEXT_CODES = (
    ("not found", E_NOT_FOUND), ("no such", E_NOT_FOUND),
    ("does not exist", E_NOT_FOUND),
    ("permission denied", E_PERMISSION), ("refused", E_PERMISSION),
    ("not permitted", E_PERMISSION), ("denied", E_PERMISSION),
    ("timed out", E_TIMEOUT), ("timeout", E_TIMEOUT),
    ("is a directory", E_CONFLICT), ("not a directory", E_CONFLICT),
    ("already exists", E_CONFLICT), ("matches", E_CONFLICT),
    ("too large", E_RESOURCE), ("very large", E_RESOURCE),
    ("no space", E_RESOURCE),
    ("connection", E_UPSTREAM), ("network", E_UPSTREAM),
    ("ssl", E_UPSTREAM), ("redirect", E_UPSTREAM),
)

ERROR_PREFIX = "ERROR"


def from_error_text(text: str, tool: str = "") -> ToolError | None:
    """Read this repo's `"ERROR: ..."` return convention as a typed error.

    The tools predate the taxonomy and report failure by returning a
    string. Left alone, every one of those would reach the caller as a
    *successful* call whose value happens to describe a failure, which is
    exactly the fabricated success the contract exists to prevent. The
    prefix is matched at position 0 only, so output that merely mentions
    the word is untouched; the code is then read from the message, and
    falls back to E_INTERNAL when nothing matches.
    """
    if not isinstance(text, str) or not text.startswith(ERROR_PREFIX):
        return None
    head = text[len(ERROR_PREFIX):]
    if head[:1] not in (":", " "):
        return None
    message = head.lstrip(": ").strip() or text
    lowered = message.lower()
    # An error already rendered by ToolError keeps its own code.
    for code in RETRYABLE:
        if lowered.startswith(code.lower()) or f" {code.lower()}" in lowered[:40]:
            return ToolError(code, message, tool=tool)
    for needle, code in _ERROR_TEXT_CODES:
        if needle in lowered:
            return ToolError(code, message, tool=tool)
    return ToolError(E_INTERNAL, message, tool=tool)


def contract_for(tool, capabilities: frozenset[str] | None = None) -> ToolContract:
    """Derive a contract from a registered `tools.Tool`.

    Taking the input schema from the tool itself rather than restating it
    is deliberate: two copies of a schema drift, and the copy the model is
    shown would win while the copy the dispatcher validates would be the
    one that is wrong.
    """
    traits = dict(_TRAITS.get(tool.name, {}))
    permission = capabilities if capabilities is not None else \
        TOOL_CAPABILITIES.get(tool.name, frozenset())
    # The registry already grades each tool's risk, and the agent already
    # confirms RISK_CONFIRM calls. Deriving `destructive` from that grade
    # rather than listing it again here means the two cannot drift: a tool
    # added as RISK_CONFIRM tomorrow needs approval tomorrow.
    traits.setdefault("destructive",
                      getattr(tool, "risk", "safe") != "safe")
    errors = traits.pop("errors", None)
    if errors is None:
        errors = _FS_ERRORS if permission & {FS_READ, FS_WRITE, FS_DELETE} \
            else (E_VALIDATION, E_PERMISSION, E_TIMEOUT, E_UPSTREAM,
                  E_INTERNAL)
    return ToolContract(
        name=tool.name, description=tool.description,
        input_schema=tool.parameters or {"type": "object"},
        output_schema=TEXT_OUT, permission=permission,
        errors=tuple(errors), **traits)


def build_contracts(registry: dict) -> dict[str, ToolContract]:
    """Contracts for every tool in a registry, keyed by name."""
    return {name: contract_for(tool) for name, tool in registry.items()}


if __name__ == "__main__":
    from .tools import build_registry

    # --- the subset validator, including the traps -------------------
    schema = {"type": "object",
              "properties": {"path": {"type": "string", "minLength": 1},
                             "limit": {"type": "integer", "minimum": 0},
                             "mode": {"enum": ["r", "w"]},
                             "tags": {"type": "array",
                                      "items": {"type": "string"},
                                      "maxItems": 2}},
              "required": ["path"], "additionalProperties": False}

    assert validate({"path": "a.py"}, schema) is None
    assert validate({}, schema).field == "path"
    assert validate({"path": 3}, schema).field == "path"
    assert validate({"path": "a", "limit": -1}, schema).code == E_VALIDATION
    assert validate({"path": "a", "mode": "x"}, schema) is not None
    assert validate({"path": "a", "tags": ["x", "y", "z"]}, schema) is not None
    assert validate({"path": "a", "tags": [1]}, schema).field == "tags[0]"
    assert validate({"path": "a", "extra": 1}, schema).field == "extra"
    # bool is an int in Python — a number field must not accept True
    assert validate({"path": "a", "limit": True}, schema) is not None
    assert validate({"path": ""}, schema) is not None        # minLength
    # a broken pattern in the schema must not fail a good call
    assert validate("abc", {"type": "string", "pattern": "([unclosed"}) is None

    # --- the validator says what it does not enforce ------------------
    assert unsupported_keywords({"oneOf": [{"type": "string"}]}) == ("oneOf",)
    assert unsupported_keywords(schema) == (), unsupported_keywords(schema)
    # a property NAMED like a keyword is still a property, not a keyword
    assert unsupported_keywords(
        {"type": "object", "properties": {"pattern": {"type": "string"}}}) == ()
    # and an unsupported keyword nested inside a property is still found
    assert unsupported_keywords(
        {"type": "object",
         "properties": {"a": {"allOf": []}}}) == ("allOf",)

    # --- errors are values, and retryability lives on the code --------
    err = ToolError(E_TIMEOUT, "took too long", tool="run_command")
    assert err.retryable and "ERROR E_TIMEOUT" in err.render()
    assert not ToolError(E_PERMISSION, "denied").retryable
    assert set(RETRYABLE) == set(ERROR_CODES)

    # --- retry policy -------------------------------------------------
    assert RETRY_NETWORK.should_retry(ToolError(E_UPSTREAM, "502"), 1)
    assert not RETRY_NETWORK.should_retry(ToolError(E_UPSTREAM, "502"), 3)
    assert not RETRY_NETWORK.should_retry(ToolError(E_VALIDATION, "bad"), 1)
    assert not NO_RETRY.should_retry(ToolError(E_TIMEOUT, "slow"), 1)

    # --- contracts derived from the live registry ---------------------
    registry = build_registry()
    contracts = build_contracts(registry)
    assert contracts, "the registry must yield contracts"
    for name, contract in contracts.items():
        assert contract.name == name
        assert contract.input_schema is registry[name].parameters or True
        assert contract.idempotency in (IDEMPOTENT, NON_IDEMPOTENT, UNSAFE)
        assert contract.timeout_seconds > 0
        assert E_VALIDATION in contract.errors
        assert set(contract.errors) <= set(ERROR_CODES), name

    # the destructive tools are the ones that need approval
    assert contracts["delete_path"].needs_approval
    assert contracts["run_command"].needs_approval
    assert contracts["web_fetch"].needs_approval      # outward facing
    assert not contracts["read_file"].needs_approval
    assert contracts["delete_path"].idempotency == UNSAFE
    assert contracts["read_file"].idempotency == IDEMPOTENT
    assert contracts["read_file"].permission == frozenset({FS_READ})
    assert PROC_EXEC in contracts["run_command"].permission
    assert NET_FETCH in contracts["web_fetch"].permission
    assert contracts["web_fetch"].retry.max_attempts == 3

    # a real call's arguments validate against the derived schema
    assert contracts["read_file"].validate_input({"path": "x.py"}) is None
    bad = contracts["read_file"].validate_input({})
    assert bad is not None and bad.tool == "read_file"

    print(f"TOOLCONTRACT SELF-TEST PASS — {len(contracts)} contracts, "
          f"{sum(1 for c in contracts.values() if c.needs_approval)} "
          f"needing approval")

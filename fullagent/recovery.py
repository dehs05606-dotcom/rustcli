"""RECOVERY — what to actually do about each typed failure.

The taxonomy in `toolcontract.py` says what went wrong and whether the
call may be repeated. That is not the same as knowing what to do next,
and the gap between the two is where agents behave badly: retrying a
validation error forever, rolling back a write that may or may not have
landed, or giving up on a timeout that one more attempt would have
cleared.

So every code gets a **playbook**: one of four strategies, the conditions
under which it applies, and the fallback when they do not hold.

    RETRY       repeat the same call -- only ever for a repeatable call
    COMPENSATE  undo what landed, then stop
    ESCALATE    a human has to decide; the machine cannot know
    ABORT       stop; repeating or undoing would not help

Two rules decide almost every case, and both are about what we do *not*
know:

  * **Repeatability gates retry.** A timeout on a non-idempotent call is
    not a retry, it is an escalation, because a timeout means the call
    was abandoned rather than observed. It may have completed. Repeating
    it could double the effect, and undoing it could undo something that
    never happened.
  * **A compensation you do not have is not a plan.** COMPENSATE
    downgrades to ESCALATE when the step declared no undo, rather than
    reporting a rollback that did not occur.

The playbooks are complete by test: `ERROR_CODES` and `PLAYBOOKS` are
asserted to have the same keys, so a tenth error code cannot be added
without someone deciding what to do about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .toolcontract import (E_CANCELLED, E_CONFLICT, E_INTERNAL, E_NOT_FOUND,
                           E_PERMISSION, E_RESOURCE, E_TIMEOUT, E_UPSTREAM,
                           E_VALIDATION, ERROR_CODES, IDEMPOTENT, RETRYABLE,
                           ToolError)

# -- strategies -------------------------------------------------------------
RETRY = "retry"
COMPENSATE = "compensate"
ESCALATE = "escalate"
ABORT = "abort"

STRATEGIES = (RETRY, COMPENSATE, ESCALATE, ABORT)


@dataclass(frozen=True)
class Context:
    """What the recovery decision is allowed to depend on."""
    idempotent: bool = False
    has_compensation: bool = False
    can_ask_human: bool = False
    attempts: int = 1
    max_attempts: int = 1
    approval_refused: bool = False

    @property
    def attempts_left(self) -> bool:
        return self.attempts < self.max_attempts


@dataclass(frozen=True)
class Playbook:
    """The strategy for one error code, and why."""
    code: str
    strategy: str
    fallback: str
    rationale: str
    requires_repeatable: bool = False
    requires_compensation: bool = False

    def to_dict(self) -> dict:
        return {"code": self.code, "strategy": self.strategy,
                "fallback": self.fallback, "rationale": self.rationale,
                "requires_repeatable": self.requires_repeatable,
                "requires_compensation": self.requires_compensation}


@dataclass(frozen=True)
class Recovery:
    """The decision: what to do, and the sentence explaining it."""
    code: str
    strategy: str
    reason: str
    playbook: Playbook | None = None
    downgraded_from: str = ""

    def to_dict(self) -> dict:
        return {"code": self.code, "strategy": self.strategy,
                "reason": self.reason,
                "downgraded_from": self.downgraded_from}

    def line(self) -> str:
        tail = (f" (wanted {self.downgraded_from})"
                if self.downgraded_from else "")
        return f"{self.code} -> {self.strategy}{tail}: {self.reason}"


PLAYBOOKS: dict[str, Playbook] = {
    E_VALIDATION: Playbook(
        E_VALIDATION, ABORT, ABORT,
        "the arguments did not fit the schema; the identical call will "
        "fail identically, and only the caller can fix it"),
    E_PERMISSION: Playbook(
        E_PERMISSION, ESCALATE, ABORT,
        "policy refused this. A human may be able to grant it; the agent "
        "must never route around it"),
    E_NOT_FOUND: Playbook(
        E_NOT_FOUND, ABORT, ABORT,
        "the target does not exist. Repeating will not create it, and "
        "there is nothing landed to undo"),
    E_CONFLICT: Playbook(
        E_CONFLICT, COMPENSATE, ESCALATE,
        "the target was not in the state the call assumed, so something "
        "earlier in the plan is probably wrong; undo back to a known state",
        requires_compensation=True),
    E_TIMEOUT: Playbook(
        E_TIMEOUT, RETRY, ESCALATE,
        "the call was abandoned, not observed. Repeat it when repeating "
        "is safe; otherwise a human has to find out whether it landed",
        requires_repeatable=True),
    E_UPSTREAM: Playbook(
        E_UPSTREAM, RETRY, ESCALATE,
        "a network or subprocess failure, which is usually transient",
        requires_repeatable=True),
    E_RESOURCE: Playbook(
        E_RESOURCE, ESCALATE, ABORT,
        "a ceiling, quota or disk limit. Retrying makes it worse and the "
        "fix is outside the agent's reach"),
    E_CANCELLED: Playbook(
        E_CANCELLED, ABORT, ABORT,
        "someone stopped this on purpose; restarting it would override "
        "their decision"),
    E_INTERNAL: Playbook(
        E_INTERNAL, COMPENSATE, ABORT,
        "a defect in the tool. Repeating repeats the defect, so undo "
        "whatever landed and stop",
        requires_compensation=True),
}


def playbook_for(code: str) -> Playbook:
    """The playbook for a code. An unknown code aborts, loudly."""
    return PLAYBOOKS.get(code, Playbook(
        code, ABORT, ABORT,
        f"'{code}' is not in the taxonomy, so nothing is known about it; "
        f"stopping is the only safe reading"))


def _reachable(preferred: str, fallback: str, ctx: Context) -> str:
    """A strategy nobody can carry out is not a strategy.

    Escalating with nobody to ask, or compensating with nothing to
    compensate with, would be a decision that reads fine in a log and
    does nothing in the world. Each downgrade is resolved against what
    the context actually offers, and the last resort is always ABORT --
    stopping is the one action that is always available.
    """
    for choice in (preferred, fallback):
        if choice == ESCALATE and not ctx.can_ask_human:
            continue
        if choice == COMPENSATE and not ctx.has_compensation:
            continue
        if choice == RETRY and not ctx.idempotent:
            continue
        return choice
    return ABORT


def plan(error: ToolError | None, context: Context | None = None) -> Recovery:
    """Decide what to do about one failure."""
    if error is None:
        return Recovery("", ABORT, "there is no error to recover from")
    ctx = context or Context()
    book = playbook_for(error.code)
    wanted = book.strategy

    if wanted == RETRY:
        if not ctx.idempotent:
            return Recovery(
                error.code, _reachable(ESCALATE, book.fallback, ctx),
                "the call is not repeatable, and a failure that was "
                "abandoned rather than observed may already have landed",
                book, downgraded_from=RETRY)
        if not RETRYABLE.get(error.code, False):
            # The taxonomy is authoritative. A playbook cannot make a
            # final code repeatable.
            return Recovery(
                error.code, _reachable(book.fallback, ABORT, ctx),
                "the taxonomy calls this code final, whatever the playbook "
                "would prefer", book, downgraded_from=RETRY)
        if not ctx.attempts_left:
            return Recovery(
                error.code, _reachable(ESCALATE, book.fallback, ctx),
                f"already tried {ctx.attempts} time(s) and the failure "
                f"persists", book, downgraded_from=RETRY)
        return Recovery(error.code, RETRY, book.rationale, book)

    if wanted == COMPENSATE:
        if not ctx.has_compensation:
            return Recovery(
                error.code, _reachable(ESCALATE, book.fallback, ctx),
                "nothing was declared that would undo this, so it cannot "
                "be rolled back", book, downgraded_from=COMPENSATE)
        return Recovery(error.code, COMPENSATE, book.rationale, book)

    if wanted == ESCALATE and not ctx.can_ask_human:
        return Recovery(
            error.code, _reachable(book.fallback, ABORT, ctx),
            "this needs a human and there is nobody to ask", book,
            downgraded_from=ESCALATE)

    if error.code == E_PERMISSION and ctx.approval_refused:
        # A human already said no. Asking again is not escalation, it is
        # pestering, and it teaches people to click through.
        return Recovery(
            error.code, ABORT,
            "approval was already refused for this call", book,
            downgraded_from=ESCALATE)

    return Recovery(error.code, wanted, book.rationale, book)


def format_playbooks() -> str:
    """The whole taxonomy and what it means, as a table."""
    lines = [f"RECOVERY PLAYBOOKS — {len(PLAYBOOKS)} codes",
             f"  {'code':<14} {'strategy':<11} {'fallback':<11} needs"]
    for code in ERROR_CODES:
        book = PLAYBOOKS[code]
        needs = []
        if book.requires_repeatable:
            needs.append("repeatable")
        if book.requires_compensation:
            needs.append("a compensation")
        lines.append(f"  {code:<14} {book.strategy:<11} {book.fallback:<11} "
                     f"{', '.join(needs) or '-'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # --- the taxonomy is covered, and stays covered --------------------
    assert set(PLAYBOOKS) == set(ERROR_CODES), (
        "every error code needs a playbook; missing: "
        f"{sorted(set(ERROR_CODES) - set(PLAYBOOKS))}, unknown: "
        f"{sorted(set(PLAYBOOKS) - set(ERROR_CODES))}")
    for code, book in PLAYBOOKS.items():
        assert book.strategy in STRATEGIES, code
        assert book.fallback in STRATEGIES, code
        assert book.rationale, f"{code} has no stated reason"

    def err(code):
        return ToolError(code, "something went wrong", tool="t")

    # Every context that tests an escalation says so: a downgrade only
    # ever lands on a strategy the context can actually carry out, so a
    # Context() with nobody to ask can never produce ESCALATE.
    repeatable = Context(idempotent=True, max_attempts=3, attempts=1,
                         can_ask_human=True)
    once = Context(idempotent=False, max_attempts=3, attempts=1,
                   can_ask_human=True)
    with_undo = Context(has_compensation=True, can_ask_human=True)
    with_human = Context(can_ask_human=True)
    alone = Context()          # no undo, nobody to ask, cannot repeat

    # --- retry is gated by repeatability, not by hope ------------------
    assert plan(err(E_TIMEOUT), repeatable).strategy == RETRY
    escalated = plan(err(E_TIMEOUT), once)
    assert escalated.strategy == ESCALATE, escalated.line()
    assert escalated.downgraded_from == RETRY

    assert plan(err(E_UPSTREAM), repeatable).strategy == RETRY
    assert plan(err(E_UPSTREAM), once).strategy == ESCALATE

    spent = plan(err(E_UPSTREAM),
                 Context(idempotent=True, attempts=3, max_attempts=3,
                         can_ask_human=True))
    assert spent.strategy == ESCALATE and spent.downgraded_from == RETRY, \
        spent.line()

    # --- a validation error is never retried ---------------------------
    for ctx in (repeatable, once, with_undo, with_human, alone):
        assert plan(err(E_VALIDATION), ctx).strategy == ABORT

    # --- compensation is only a plan when one exists -------------------
    assert plan(err(E_INTERNAL), with_undo).strategy == COMPENSATE
    bare = plan(err(E_INTERNAL), alone)
    assert bare.strategy == ABORT and bare.downgraded_from == COMPENSATE, \
        bare.line()
    asked = plan(err(E_INTERNAL), Context(can_ask_human=True))
    assert asked.strategy == ESCALATE, asked.line()

    assert plan(err(E_CONFLICT), with_undo).strategy == COMPENSATE
    # No compensation and nobody to ask: the playbook would prefer to
    # escalate and there is no one there, so it stops. This assertion used
    # to say ESCALATE, which was a decision that read fine in a log and
    # did nothing in the world; the invariant layer caught it.
    assert plan(err(E_CONFLICT), alone).strategy == ABORT
    assert plan(err(E_CONFLICT),
                Context(can_ask_human=True)).strategy == ESCALATE

    # --- a downgrade never lands on something unreachable --------------
    for code in PLAYBOOKS:
        verdict = plan(err(code), alone)      # knows nothing, has nobody
        assert verdict.strategy == ABORT, (code, verdict.line())

    # --- escalation needs somebody to escalate to ----------------------
    assert plan(err(E_PERMISSION), with_human).strategy == ESCALATE
    nobody = plan(err(E_PERMISSION), alone)
    assert nobody.strategy == ABORT and nobody.downgraded_from == ESCALATE

    refused = plan(err(E_PERMISSION),
                   Context(can_ask_human=True, approval_refused=True))
    assert refused.strategy == ABORT, \
        "asking again after a refusal is pestering, not escalation"

    assert plan(err(E_RESOURCE), with_human).strategy == ESCALATE
    assert plan(err(E_RESOURCE), alone).strategy == ABORT
    assert plan(err(E_CANCELLED), with_human).strategy == ABORT
    assert plan(err(E_NOT_FOUND), with_human).strategy == ABORT

    # --- a playbook cannot overrule the taxonomy -----------------------
    liar = Playbook("E_MADE_UP", RETRY, ABORT, "wishful", True)
    PLAYBOOKS["E_MADE_UP"] = liar
    try:
        forced = plan(ToolError("E_MADE_UP", "x"), repeatable)
        assert forced.strategy == ABORT, forced.line()
        assert forced.downgraded_from == RETRY
    finally:
        PLAYBOOKS.pop("E_MADE_UP")

    unknown = plan(ToolError("E_NEVER_HEARD_OF_IT", "x"), repeatable)
    assert unknown.strategy == ABORT, unknown.line()

    assert plan(None).strategy == ABORT

    print(format_playbooks())
    print(f"RECOVERY SELF-TEST PASS — {len(PLAYBOOKS)} codes, "
          f"{len(STRATEGIES)} strategies")

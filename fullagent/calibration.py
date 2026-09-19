"""CALIBRATION — measuring the verifiers, because nobody else does.

`consensus.py` runs two strategies and holds when they disagree. That is
the right rule, and it has a blind spot: nothing watches the strategies
themselves. A strategy that has quietly stopped working does not throw.
It passes everything, and a pair where one member always passes is a
pair that is really one member — with the reassuring appearance of two.

So this module reads the sealed `consensus.audit` events back and asks
three questions the auditor cannot ask about itself.

**Is each strategy still discriminating?** A strategy whose verdict is
constant across a whole window is *degenerate*: always-passes,
always-fails, or never-fires. That is a finding about the verifier, and
the right response is not to stop using it but to stop counting it as
evidence — a degenerate strategy's `pass` is downgraded to `unsure`,
which by the consensus rule can never release anything on its own.
Nothing is silently trusted and nothing is silently discarded.

**Is the pair still independent?** Two strategies that agree on
everything are one strategy with a redundant copy; two that agree on
nothing have stopped measuring the same thing. Both collapse the value
of having two, and both show up as an agreement rate pinned at an
extreme rather than sitting in between.

**Where should the hold threshold actually sit?** Calibrated from
observed data rather than guessed. With one hard rule:

    **Calibration may tighten on its own. Loosening is a proposal.**

Raising scrutiny in response to evidence is safe and automatic. Lowering
it is a decision about how much risk to accept, which is not a decision
a moving average gets to make — it produces a `Proposal` that stays
inert until a named human accepts it, the same shape `telemetry.py` uses
for model routing.

**The honest limit.** Every number here is computed over the audits that
were sealed. A strategy that has never run is not "reliable", it is
unmeasured, and `Degeneracy` names that case separately (`never-fires`)
rather than letting a thin sample read as a clean one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .consensus import FAIL, HOLD, PASS, RELEASE, UNSURE

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

WINDOW_SIZE = 20          # audits per rolling window
MIN_WINDOWS = 2           # before a trend means anything
MIN_AUDITS = 10           # before a strategy is judged at all

#: Agreement rates outside this band mean the pair has stopped being two
#: independent readings of the same question.
AGREE_CEILING = 0.98      # above this they are one strategy, duplicated
AGREE_FLOOR = 0.30        # below this they are not measuring the same thing

#: A verdict share at or above this across a window makes a strategy
#: degenerate: it is answering without reading.
CONSTANT_SHARE = 1.0

#: How far the hold rate may move before recalibration has anything to
#: say. Below this, the difference is the sample, not the system.
MEANINGFUL_SHIFT = 0.10


# ---------------------------------------------------------------------------
# Degeneracy
# ---------------------------------------------------------------------------

D_ALWAYS_PASSES = "always-passes"
D_ALWAYS_FAILS = "always-fails"
D_ALWAYS_UNSURE = "always-unsure"
D_NEVER_FIRES = "never-fires"
D_PAIR_REDUNDANT = "pair-redundant"
D_PAIR_DIVERGED = "pair-diverged"

#: Every degeneracy, with what it means and what it costs.
DEGENERACIES: dict[str, tuple[str, str]] = {
    D_ALWAYS_PASSES: (
        "this strategy passed every audit in the window",
        "its pass carries no information, so it is counted as unsure "
        "until it discriminates again"),
    D_ALWAYS_FAILS: (
        "this strategy failed every audit in the window",
        "it blocks everything, which is not safety — it is an outage "
        "that looks like safety"),
    D_ALWAYS_UNSURE: (
        "this strategy decided nothing in the window",
        "an unsure verifier is a verifier that is not running"),
    D_NEVER_FIRES: (
        "this strategy produced too few audits to judge",
        "unmeasured is not reliable; run it or stop counting it"),
    D_PAIR_REDUNDANT: (
        "two strategies agreed on essentially everything",
        "a second opinion that never differs is the first opinion "
        "again"),
    D_PAIR_DIVERGED: (
        "two strategies agreed on almost nothing",
        "they have stopped measuring the same question"),
}


@dataclass(frozen=True)
class Degeneracy:
    """One finding about a verifier, not about a reply."""
    kind: str
    subject: str            # a strategy name, or "a vs b" for a pair
    detail: str
    observations: int = 0

    @property
    def what(self) -> str:
        return DEGENERACIES.get(self.kind, ("", ""))[0]

    @property
    def cost(self) -> str:
        return DEGENERACIES.get(self.kind, ("", ""))[1]

    @property
    def downgrades(self) -> bool:
        """Whether a strategy with this finding stops counting as a pass.

        Only the cases where the strategy has stopped discriminating.
        `always-fails` is a problem, but a strategy that fails everything
        is not one whose *pass* needs downgrading — it has none.
        """
        return self.kind in (D_ALWAYS_PASSES, D_ALWAYS_UNSURE,
                             D_NEVER_FIRES)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "subject": self.subject,
                "detail": self.detail, "observations": self.observations,
                "what": self.what, "cost": self.cost,
                "downgrades": self.downgrades}

    def line(self) -> str:
        return (f"  [{self.kind}] {self.subject}: {self.detail}"
                + ("  (its pass no longer counts)" if self.downgrades
                   else ""))


# ---------------------------------------------------------------------------
# Reading the audits back
# ---------------------------------------------------------------------------

@dataclass
class AuditRow:
    """One sealed audit, reduced to what calibration reads."""
    seq: int
    outcome: str
    verdicts: dict[str, str] = field(default_factory=dict)

    @property
    def strategies(self) -> tuple[str, ...]:
        return tuple(sorted(self.verdicts))


def rows_of(log) -> tuple[AuditRow, ...]:
    """Every sealed consensus audit, oldest first."""
    out: list[AuditRow] = []
    for ev in log.events():
        if ev.type != "consensus.audit":
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        verdicts = {str(o.get("strategy") or "?"): str(o.get("verdict") or "?")
                    for o in (data.get("opinions") or ())}
        out.append(AuditRow(int(getattr(ev, "seq", 0) or 0),
                            str(data.get("outcome") or "?"), verdicts))
    return tuple(out)


def windows_of(rows: tuple[AuditRow, ...], size: int = WINDOW_SIZE
               ) -> tuple[tuple[AuditRow, ...], ...]:
    """Complete windows, oldest first. A partial tail is left out.

    Averaging three audits beside twenty would let the newest handful
    swing every number in this module.
    """
    return tuple(rows[i:i + size]
                 for i in range(0, len(rows) - size + 1, size))


# ---------------------------------------------------------------------------
# What a window says
# ---------------------------------------------------------------------------

@dataclass
class StrategyStats:
    strategy: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def share(self, verdict: str) -> float:
        return self.counts.get(verdict, 0) / self.total if self.total else 0.0

    def to_dict(self) -> dict:
        return {"strategy": self.strategy, "total": self.total,
                "counts": dict(sorted(self.counts.items())),
                "pass_rate": round(self.share(PASS), 4)}


@dataclass
class Calibration:
    """What the sealed audits say about the verifiers."""
    audits: int = 0
    windows: int = 0
    hold_rate: float = 0.0
    release_rate: float = 0.0
    per_strategy: dict[str, StrategyStats] = field(default_factory=dict)
    agreement: dict[str, float] = field(default_factory=dict)
    degeneracies: tuple[Degeneracy, ...] = ()
    recent_hold_rate: float = 0.0

    @property
    def healthy(self) -> bool:
        return not self.degeneracies

    @property
    def downgraded(self) -> frozenset[str]:
        """Strategies whose pass no longer counts as evidence."""
        return frozenset(d.subject for d in self.degeneracies
                         if d.downgrades and " vs " not in d.subject)

    def to_dict(self) -> dict:
        return {"audits": self.audits, "windows": self.windows,
                "hold_rate": round(self.hold_rate, 4),
                "release_rate": round(self.release_rate, 4),
                "recent_hold_rate": round(self.recent_hold_rate, 4),
                "healthy": self.healthy,
                "downgraded": sorted(self.downgraded),
                "agreement": {k: round(v, 4)
                              for k, v in sorted(self.agreement.items())},
                "per_strategy": {k: v.to_dict()
                                 for k, v in sorted(self.per_strategy.items())},
                "degeneracies": [d.to_dict() for d in self.degeneracies]}

    def format(self) -> str:
        head = (f"CALIBRATION — {self.audits} audit(s), {self.windows} "
                f"complete window(s); hold {self.hold_rate * 100:.0f}%, "
                f"release {self.release_rate * 100:.0f}%")
        lines = [head + ("" if self.healthy
                         else f" — {len(self.degeneracies)} finding(s)")]
        for name in sorted(self.per_strategy):
            st = self.per_strategy[name]
            mark = "!" if name in self.downgraded else " "
            lines.append(f" {mark} {name:<16} "
                         + ", ".join(f"{v} {c}" for v, c
                                     in sorted(st.counts.items())))
        for pair, rate in sorted(self.agreement.items()):
            lines.append(f"   {pair}: agree {rate * 100:.0f}%")
        lines.extend(d.line() for d in self.degeneracies)
        return "\n".join(lines)


def calibrate(log, size: int = WINDOW_SIZE) -> Calibration:
    """Read every sealed audit and judge the verifiers."""
    rows = rows_of(log)
    cal = Calibration(audits=len(rows))
    if not rows:
        return cal

    windows = windows_of(rows, size)
    cal.windows = len(windows)
    cal.hold_rate = sum(1 for r in rows if r.outcome == HOLD) / len(rows)
    cal.release_rate = sum(1 for r in rows
                           if r.outcome == RELEASE) / len(rows)
    recent = windows[-1] if windows else rows
    cal.recent_hold_rate = sum(1 for r in recent
                               if r.outcome == HOLD) / len(recent)

    names = sorted({n for r in rows for n in r.verdicts})
    for name in names:
        stats = StrategyStats(name)
        for row in rows:
            verdict = row.verdicts.get(name)
            if verdict is None:
                continue
            stats.counts[verdict] = stats.counts.get(verdict, 0) + 1
        cal.per_strategy[name] = stats

    findings: list[Degeneracy] = []

    # -- per strategy --------------------------------------------------
    for name, stats in cal.per_strategy.items():
        if stats.total < MIN_AUDITS:
            findings.append(Degeneracy(
                D_NEVER_FIRES, name,
                f"only {stats.total} audit(s); {MIN_AUDITS} needed before "
                f"this means anything", stats.total))
            continue
        # Judged on the newest complete window, so a strategy that broke
        # recently is not averaged back into health by its own history.
        window = recent
        seen = [r.verdicts.get(name) for r in window
                if name in r.verdicts]
        if not seen:
            findings.append(Degeneracy(
                D_NEVER_FIRES, name,
                "it produced no verdict in the newest window", 0))
            continue
        share = {v: seen.count(v) / len(seen) for v in set(seen)}
        for verdict, kind in ((PASS, D_ALWAYS_PASSES),
                              (FAIL, D_ALWAYS_FAILS),
                              (UNSURE, D_ALWAYS_UNSURE)):
            if share.get(verdict, 0.0) >= CONSTANT_SHARE:
                findings.append(Degeneracy(
                    kind, name,
                    f"every one of its {len(seen)} verdict(s) in the "
                    f"newest window was {verdict!r}", len(seen)))

    # -- per pair ------------------------------------------------------
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            both = [r for r in rows if a in r.verdicts and b in r.verdicts]
            if len(both) < MIN_AUDITS:
                continue
            agree = sum(1 for r in both
                        if r.verdicts[a] == r.verdicts[b]) / len(both)
            cal.agreement[f"{a} vs {b}"] = agree
            if agree >= AGREE_CEILING:
                findings.append(Degeneracy(
                    D_PAIR_REDUNDANT, f"{a} vs {b}",
                    f"they agreed on {agree * 100:.0f}% of "
                    f"{len(both)} audit(s)", len(both)))
            elif agree <= AGREE_FLOOR:
                findings.append(Degeneracy(
                    D_PAIR_DIVERGED, f"{a} vs {b}",
                    f"they agreed on only {agree * 100:.0f}% of "
                    f"{len(both)} audit(s)", len(both)))

    cal.degeneracies = tuple(findings)
    return cal


# ---------------------------------------------------------------------------
# Thresholds: tightening is automatic, loosening is a proposal
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Threshold:
    """How much scrutiny the consensus layer applies right now.

    `min_agreeing` is how many strategies must positively pass before a
    reply may be released. Two is the shipped pair; three means a model
    verifier has to weigh in as well.
    """
    min_agreeing: int = 2
    treat_unsure_as_fail: bool = True
    why: str = "the shipped default"

    def to_dict(self) -> dict:
        return {"min_agreeing": self.min_agreeing,
                "treat_unsure_as_fail": self.treat_unsure_as_fail,
                "why": self.why}


DEFAULT_THRESHOLD = Threshold()


@dataclass
class Proposal:
    """A loosening nobody may apply without saying who they are."""
    current: Threshold
    proposed: Threshold
    why: str
    evidence: dict = field(default_factory=dict)
    accepted_by: str = ""

    @property
    def in_effect(self) -> Threshold:
        """What is actually in force. Unaccepted means unchanged."""
        return self.proposed if self.accepted_by else self.current

    def accept(self, who: str) -> "Proposal":
        if not who:
            raise ValueError("a loosening must name who accepted it")
        self.accepted_by = who
        return self

    def to_dict(self) -> dict:
        return {"current": self.current.to_dict(),
                "proposed": self.proposed.to_dict(), "why": self.why,
                "evidence": self.evidence, "accepted_by": self.accepted_by,
                "in_effect": self.in_effect.to_dict()}

    def format(self) -> str:
        state = (f"accepted by {self.accepted_by}" if self.accepted_by
                 else "NOT in effect — needs a named human")
        return (f"THRESHOLD PROPOSAL — "
                f"min_agreeing {self.current.min_agreeing} -> "
                f"{self.proposed.min_agreeing} ({state})\n"
                f"  {self.why}")


@dataclass
class Recalibration:
    """What the data says the threshold should be."""
    threshold: Threshold
    tightened: bool = False
    proposal: Proposal | None = None
    why: str = ""

    def to_dict(self) -> dict:
        return {"threshold": self.threshold.to_dict(),
                "tightened": self.tightened, "why": self.why,
                "proposal": (self.proposal.to_dict()
                             if self.proposal else None)}


def recalibrate(cal: Calibration, current: Threshold = DEFAULT_THRESHOLD
                ) -> Recalibration:
    """Read a threshold off the observed data.

    Tightening applies immediately; loosening comes back as a proposal
    that is inert until accepted. The asymmetry is deliberate and is the
    only reason this function is safe to run unattended.
    """
    if cal.audits < MIN_AUDITS:
        return Recalibration(current, False, None,
                             f"{cal.audits} audit(s) is not enough to "
                             f"calibrate anything")

    # -- tighten: a degenerate strategy means the pair is thinner than
    #    it looks, so require one more genuine agreement.
    if cal.downgraded:
        names = ", ".join(sorted(cal.downgraded))
        tighter = Threshold(
            current.min_agreeing + len(cal.downgraded), True,
            f"{names} stopped discriminating, so its pass is not counted")
        return Recalibration(tighter, True, None, tighter.why)

    # -- tighten: holds climbing means the strategies are diverging on
    #    real traffic, which is exactly when to be stricter.
    if cal.recent_hold_rate - cal.hold_rate >= MEANINGFUL_SHIFT:
        tighter = Threshold(
            current.min_agreeing, True,
            f"holds rose from {cal.hold_rate * 100:.0f}% to "
            f"{cal.recent_hold_rate * 100:.0f}% in the newest window")
        if not current.treat_unsure_as_fail:
            return Recalibration(tighter, True, None, tighter.why)

    # -- loosen: only ever as a proposal -------------------------------
    quiet = (cal.recent_hold_rate <= MEANINGFUL_SHIFT
             and cal.hold_rate <= MEANINGFUL_SHIFT
             and cal.windows >= MIN_WINDOWS
             and current.min_agreeing > 2)
    if quiet:
        looser = Threshold(
            max(2, current.min_agreeing - 1), current.treat_unsure_as_fail,
            "the strategies have agreed steadily across every window")
        proposal = Proposal(
            current, looser,
            f"hold rate {cal.hold_rate * 100:.0f}% over {cal.windows} "
            f"window(s); no degenerate strategy",
            {"hold_rate": round(cal.hold_rate, 4),
             "windows": cal.windows, "audits": cal.audits})
        return Recalibration(current, False, proposal,
                             "a loosening was proposed and is not in "
                             "effect")

    return Recalibration(current, False, None,
                         "the observed data supports the current "
                         "threshold")


class Calibrator:
    """Calibration with its findings sealed, and its proposals inert."""

    def __init__(self, log=None, size: int = WINDOW_SIZE):
        self.log = log
        self.size = size

    def _emit(self, event: str, data: dict) -> None:
        if self.log is None:
            return
        try:
            self.log.append(event, data, actor="calibration")
        except Exception:
            pass

    def run(self, log=None, current: Threshold = DEFAULT_THRESHOLD
            ) -> tuple[Calibration, Recalibration]:
        source = log if log is not None else self.log
        cal = calibrate(source, self.size)
        out = recalibrate(cal, current)
        self._emit("calibration.measured", cal.to_dict())
        if out.tightened:
            self._emit("calibration.tightened", out.to_dict())
        if out.proposal is not None:
            self._emit("calibration.proposed", out.proposal.to_dict())
        return cal, out

    def accept(self, proposal: Proposal, who: str) -> Proposal:
        proposal.accept(who)
        self._emit("calibration.accepted", proposal.to_dict())
        return proposal


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import tempfile
    from pathlib import Path

    from .kernel import EventLog

    work = Path(tempfile.mkdtemp(prefix="fa-calibration-"))

    def fresh(name: str) -> EventLog:
        return EventLog(path=str(work / f"{name}.jsonl"))

    def audit(log, outcome, **verdicts):
        log.append("consensus.audit",
                   {"outcome": outcome,
                    "opinions": [{"strategy": k, "verdict": v,
                                  "findings": []}
                                 for k, v in verdicts.items()]},
                   actor="consensus")

    # --- a healthy pair -----------------------------------------------
    log = fresh("healthy")
    for i in range(20):
        if i % 5 == 0:
            audit(log, HOLD, guardrail=FAIL, independent=PASS)
        elif i % 3 == 0:
            audit(log, "block", guardrail=FAIL, independent=FAIL)
        else:
            audit(log, RELEASE, guardrail=PASS, independent=PASS)
    cal = calibrate(log)
    assert cal.audits == 20 and cal.windows == 1, cal.to_dict()
    assert cal.healthy, cal.format()
    assert not cal.downgraded
    assert 0.0 < cal.agreement["guardrail vs independent"] < 1.0

    # --- a strategy that stopped discriminating -----------------------
    log = fresh("lazy")
    for i in range(20):
        # the guardrail still works; `lazy` passes everything
        audit(log, RELEASE if i % 4 else HOLD,
              guardrail=PASS if i % 4 else FAIL, lazy=PASS)
    cal = calibrate(log)
    assert not cal.healthy, cal.format()
    kinds = {d.kind for d in cal.degeneracies}
    assert D_ALWAYS_PASSES in kinds, cal.format()
    assert "lazy" in cal.downgraded, cal.downgraded
    assert "guardrail" not in cal.downgraded
    lazy = [d for d in cal.degeneracies if d.kind == D_ALWAYS_PASSES][0]
    assert lazy.downgrades and lazy.cost

    # --- a strategy that blocks everything ----------------------------
    log = fresh("paranoid")
    for i in range(20):
        audit(log, "block", guardrail=FAIL if i % 3 else PASS, paranoid=FAIL)
    cal = calibrate(log)
    assert D_ALWAYS_FAILS in {d.kind for d in cal.degeneracies}, cal.format()
    assert "paranoid" not in cal.downgraded, \
        "a strategy with no passes has no pass to downgrade"

    # --- a strategy that decides nothing ------------------------------
    log = fresh("absent")
    for i in range(20):
        audit(log, HOLD, guardrail=PASS if i % 2 else FAIL, asleep=UNSURE)
    cal = calibrate(log)
    assert D_ALWAYS_UNSURE in {d.kind for d in cal.degeneracies}
    assert "asleep" in cal.downgraded

    # --- too few audits is unmeasured, not healthy --------------------
    log = fresh("thin")
    for _ in range(3):
        audit(log, RELEASE, guardrail=PASS, independent=PASS)
    cal = calibrate(log)
    assert not cal.healthy
    assert {d.kind for d in cal.degeneracies} == {D_NEVER_FIRES}, cal.format()
    assert all(d.observations == 3 for d in cal.degeneracies)

    # --- a redundant pair ---------------------------------------------
    log = fresh("twins")
    for i in range(20):
        v = PASS if i % 3 else FAIL
        audit(log, RELEASE if v == PASS else "block", one=v, two=v)
    cal = calibrate(log)
    assert D_PAIR_REDUNDANT in {d.kind for d in cal.degeneracies}, \
        cal.format()
    assert cal.agreement["one vs two"] == 1.0

    # --- a diverged pair ----------------------------------------------
    log = fresh("strangers")
    for i in range(20):
        audit(log, HOLD, one=PASS, two=FAIL)
    cal = calibrate(log)
    kinds = {d.kind for d in cal.degeneracies}
    assert D_PAIR_DIVERGED in kinds, cal.format()

    # --- recalibration: tightening is automatic -----------------------
    log = fresh("tighten")
    for i in range(20):
        audit(log, RELEASE if i % 4 else HOLD,
              guardrail=PASS if i % 4 else FAIL, lazy=PASS)
    cal = calibrate(log)
    out = recalibrate(cal)
    assert out.tightened and out.threshold.min_agreeing == 3, out.to_dict()
    assert "lazy" in out.threshold.why
    assert out.proposal is None, "tightening is never a proposal"

    # --- recalibration: loosening is only ever a proposal -------------
    # Both strategies still discriminate; they simply rarely disagree.
    # A fixture where one of them is constant would trip the degeneracy
    # check instead, and prove nothing about loosening.
    log = fresh("quiet")
    for i in range(40):
        if i % 13 == 0:
            audit(log, HOLD, guardrail=FAIL, independent=PASS)
        elif i % 7 == 0:
            audit(log, "block", guardrail=FAIL, independent=FAIL)
        else:
            audit(log, RELEASE, guardrail=PASS, independent=PASS)
    quiet = calibrate(log)
    assert quiet.healthy, quiet.format()
    strict = Threshold(min_agreeing=3, why="tightened earlier")
    out = recalibrate(quiet, strict)
    assert out.proposal is not None, out.to_dict()
    assert not out.tightened
    assert out.threshold is strict, "nothing loosens by itself"
    assert out.proposal.in_effect.min_agreeing == 3, \
        "an unaccepted proposal changes nothing"
    assert "NOT in effect" in out.proposal.format()

    accepted = out.proposal.accept("the operator")
    assert accepted.in_effect.min_agreeing == 2
    assert "the operator" in accepted.format()
    try:
        Proposal(strict, strict, "x").accept("")
        raise AssertionError("a loosening must be attributed")
    except ValueError:
        pass

    # --- too little data calibrates nothing ---------------------------
    out = recalibrate(calibrate(fresh("nothing")))
    assert not out.tightened and out.proposal is None
    assert "not enough" in out.why

    # --- windows leave a partial tail out -----------------------------
    log = fresh("windowed")
    for i in range(25):
        audit(log, RELEASE, guardrail=PASS, independent=PASS)
    rows = rows_of(log)
    assert len(rows) == 25
    assert len(windows_of(rows, 10)) == 2, "a partial tail is not a window"

    # --- the calibrator seals what it measured ------------------------
    sealed = fresh("sealed")
    for i in range(20):
        audit(sealed, RELEASE if i % 4 else HOLD,
              guardrail=PASS if i % 4 else FAIL, lazy=PASS)
    cal, out = Calibrator(log=sealed).run()
    kinds = {e.type for e in sealed.events()}
    assert "calibration.measured" in kinds, sorted(kinds)
    assert "calibration.tightened" in kinds, sorted(kinds)
    assert out.tightened and "lazy" in cal.downgraded

    # --- every degeneracy explains itself -----------------------------
    for kind, (what, cost) in DEGENERACIES.items():
        assert what and cost, kind
        assert Degeneracy(kind, "x", "y").what == what

    print(cal.format())
    print(f"CALIBRATION SELF-TEST PASS — {len(DEGENERACIES)} degeneracy "
          f"kind(s); tightening is automatic, loosening is a proposal")

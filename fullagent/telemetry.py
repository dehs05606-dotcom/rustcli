"""TELEMETRY — per-model scorecards, rolling drift, and routing proposals.

`compliance.py` keeps one number per model: an EWMA that tightens the
enforcement level when a model misbehaves. `benchmark.py` scores models
against fixed scenarios. Both are right about what they measure and
neither answers the question an operator actually asks, which is *how is
this model doing, compared to last week and compared to the others, and
should we be running something else*.

So this module joins them:

  SCORECARD   One model under one prompt: its live compliance from the
              guardrail, its benchmark score, how many observations sit
              behind each, and a grade. The observation count travels with
              the grade on purpose -- "0.92 from four turns" and "0.92
              from four hundred" are not the same claim, and a scorecard
              that hid the difference would invite acting on the first.

  ROLLING WINDOWS  Not one window against a frozen baseline, but a series
              of them in order. A model that fell off a cliff and a model
              that has been sliding for a fortnight both read as "drifted"
              against a baseline; only a series tells them apart, and they
              want different responses.

  ROUTING     A proposal, never an act. `routing()` returns a
              `RoutingProposal` carrying the numbers that produced it, and
              the proposal does nothing until `accept()` is called with
              the name of whoever accepted it. There is no code path in
              this module that changes which model runs. That is the whole
              design: a router that switched models on its own would make
              every later result unattributable, because nobody would know
              which model produced which work.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .benchmark import (MIN_ACCEPTABLE, REGRESSION_DROP, SWITCH_MARGIN,
                        BenchmarkReport)
from .compliance import DRIFT_DROP, ComplianceEngine, ModelCompliance

WINDOW_SIZE = 10          # observations per rolling window
MIN_WINDOWS = 2           # before a trend means anything
SLIDE_PER_WINDOW = 0.04   # a fall this steady is a slide, not a cliff
CLIFF_DROP = 0.20         # a single-window fall this large is a cliff

# Grades. Deliberately coarse: a scale finer than the measurement is a
# way of implying precision that is not there.
GRADES = (("trusted", 0.90), ("steady", 0.75), ("watch", 0.60),
          ("failing", 0.0))

IMPROVING, STEADY, DECLINING, UNKNOWN = ("improving", "steady", "declining",
                                         "unknown")


def grade_for(score: float) -> str:
    for name, floor in GRADES:
        if score >= floor:
            return name
    return GRADES[-1][0]


@dataclass(frozen=True)
class Window:
    """One block of observations, in the order they happened."""
    index: int
    n: int
    mean: float
    low: float
    high: float

    def to_dict(self) -> dict:
        return {"index": self.index, "n": self.n,
                "mean": round(self.mean, 4), "low": round(self.low, 4),
                "high": round(self.high, 4)}


def windows_of(history: list[float], size: int = WINDOW_SIZE
               ) -> tuple[Window, ...]:
    """Cut a history into complete windows, oldest first.

    A trailing partial window is left out rather than averaged in: three
    observations next to ten would swing the trend on almost no evidence.
    """
    out: list[Window] = []
    for i in range(0, len(history) - size + 1, size):
        block = history[i:i + size]
        out.append(Window(len(out), len(block), sum(block) / len(block),
                          min(block), max(block)))
    return tuple(out)


@dataclass(frozen=True)
class Drift:
    """What the series of windows says, and how it fell."""
    kind: str            # none | cliff | slide
    trend: str           # improving | steady | declining | unknown
    change: float = 0.0
    detail: str = ""

    @property
    def drifting(self) -> bool:
        return self.kind != "none"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "trend": self.trend,
                "change": round(self.change, 4), "detail": self.detail}


def drift_of(windows: tuple[Window, ...]) -> Drift:
    """Read a trend off the window series."""
    if len(windows) < MIN_WINDOWS:
        return Drift("none", UNKNOWN, 0.0,
                     f"{len(windows)} complete window(s); "
                     f"{MIN_WINDOWS} needed before a trend means anything")
    first, last = windows[0], windows[-1]
    change = last.mean - first.mean
    step = last.mean - windows[-2].mean

    if step <= -CLIFF_DROP:
        return Drift("cliff", DECLINING, step,
                     f"fell {abs(step):.2f} in one window -- something "
                     f"changed, rather than degraded")
    spans = len(windows) - 1
    if change <= -SLIDE_PER_WINDOW * spans and change <= -DRIFT_DROP:
        return Drift("slide", DECLINING, change,
                     f"down {abs(change):.2f} across {spans} window(s), "
                     f"steadily rather than at once")
    if change >= SLIDE_PER_WINDOW * spans:
        return Drift("none", IMPROVING, change,
                     f"up {change:.2f} across {spans} window(s)")
    if change <= -SLIDE_PER_WINDOW:
        return Drift("none", DECLINING, change,
                     f"down {abs(change):.2f}, under the drift threshold")
    return Drift("none", STEADY, change, "within normal movement")


@dataclass
class Scorecard:
    """Everything known about one model under one prompt."""
    model: str
    prompt: str = "main"
    live: ModelCompliance | None = None
    bench: BenchmarkReport | None = None
    history: list[float] = field(default_factory=list)

    @property
    def windows(self) -> tuple[Window, ...]:
        return windows_of(self.history)

    @property
    def drift(self) -> Drift:
        return drift_of(self.windows)

    @property
    def live_score(self) -> float | None:
        return None if self.live is None else round(self.live.ewma, 4)

    @property
    def bench_score(self) -> float | None:
        return None if self.bench is None else round(self.bench.score, 4)

    @property
    def observations(self) -> int:
        return 0 if self.live is None else self.live.observations

    @property
    def score(self) -> float | None:
        """One number, when there is enough to justify one.

        The live score is what this prompt actually did; the benchmark is
        what a fixed set of scenarios says. Where both exist the live
        score carries more weight, because it is this work rather than a
        proxy for it.
        """
        live, bench = self.live_score, self.bench_score
        if live is None and bench is None:
            return None
        if live is None:
            return bench
        if bench is None:
            return live
        return round(0.65 * live + 0.35 * bench, 4)

    @property
    def grade(self) -> str:
        score = self.score
        return "unrated" if score is None else grade_for(score)

    @property
    def confident(self) -> bool:
        """Enough evidence to act on. Below this, report but do not route."""
        return self.observations >= WINDOW_SIZE or self.bench is not None

    def to_dict(self) -> dict:
        return {"model": self.model, "prompt": self.prompt,
                "score": self.score, "grade": self.grade,
                "live_score": self.live_score, "bench_score": self.bench_score,
                "observations": self.observations,
                "confident": self.confident,
                "windows": [w.to_dict() for w in self.windows],
                "drift": self.drift.to_dict()}

    def format(self) -> str:
        score = self.score
        head = (f"{self.model} [{self.grade}]"
                + (f" {score:.2f}" if score is not None else " no data"))
        lines = [head]
        parts = []
        if self.live_score is not None:
            parts.append(f"live {self.live_score:.2f} "
                         f"over {self.observations} turn(s)")
        if self.bench_score is not None:
            parts.append(f"benchmark {self.bench_score:.2f}")
        if parts:
            lines.append("  " + " · ".join(parts))
        d = self.drift
        lines.append(f"  trend {d.trend}: {d.detail}")
        if not self.confident:
            lines.append("  too little evidence to route on")
        return "\n".join(lines)


@dataclass
class RoutingProposal:
    """A recommendation. It changes nothing until somebody accepts it."""
    prompt: str
    current: str
    recommended: str
    should_switch: bool
    justification: tuple[str, ...] = ()
    scores: dict[str, float] = field(default_factory=dict)
    proposed_at: float = field(default_factory=time.time)
    accepted_by: str = ""
    accepted_at: float = 0.0

    @property
    def accepted(self) -> bool:
        return bool(self.accepted_by)

    @property
    def in_effect(self) -> str:
        """Which model is actually in use. Never the recommendation alone."""
        return self.recommended if self.accepted else self.current

    def to_dict(self) -> dict:
        return {"prompt": self.prompt, "current": self.current,
                "recommended": self.recommended,
                "should_switch": self.should_switch,
                "justification": list(self.justification),
                "scores": {k: round(v, 4) for k, v in self.scores.items()},
                "accepted": self.accepted, "accepted_by": self.accepted_by,
                "in_effect": self.in_effect}

    def format(self) -> str:
        if not self.should_switch:
            head = f"ROUTING — stay on {self.current}"
        elif self.accepted:
            head = (f"ROUTING — switched to {self.recommended}, "
                    f"accepted by {self.accepted_by}")
        else:
            head = (f"ROUTING — proposing {self.recommended} "
                    f"(awaiting a human; still on {self.current})")
        return "\n".join([head] + [f"  {line}"
                                   for line in self.justification])


class Telemetry:
    """Scorecards, drift and routing proposals over one compliance engine."""

    def __init__(self, engine: ComplianceEngine | None = None, log=None):
        self.engine = engine if engine is not None else ComplianceEngine(log)
        self.log = log
        self.history: dict[str, list[float]] = {}
        self.benchmarks: dict[str, BenchmarkReport] = {}
        self.proposals: list[RoutingProposal] = []

    # -- feeding it --------------------------------------------------------

    def _emit(self, event: str, data: dict) -> None:
        if self.log is None:
            return
        try:
            self.log.append(event, data, actor="telemetry")
        except Exception:
            pass

    def observe(self, model: str, score: float, prompt: str = "main",
                **kwargs):
        """Record one turn's compliance score for a model."""
        key = f"{model}::{prompt}"
        self.history.setdefault(key, []).append(float(score))
        return self.engine.observe(model, score, prompt, **kwargs)

    def observe_result(self, model: str, result, prompt: str = "main"):
        """Record a guardrail pipeline result directly."""
        from .compliance import score_result
        self.history.setdefault(f"{model}::{prompt}", []).append(
            score_result(result))
        return self.engine.observe_result(model, result, prompt)

    def record_benchmark(self, report: BenchmarkReport) -> None:
        self.benchmarks[report.model] = report
        self._emit("telemetry.benchmark",
                   {"model": report.model, "score": round(report.score, 4)})

    # -- reading it --------------------------------------------------------

    def models(self, prompt: str = "main") -> tuple[str, ...]:
        seen = {k.split("::", 1)[0] for k in self.history
                if k.endswith(f"::{prompt}")}
        seen |= set(self.benchmarks)
        return tuple(sorted(seen))

    def scorecard(self, model: str, prompt: str = "main") -> Scorecard:
        key = f"{model}::{prompt}"
        live = self.engine.models.get(key)
        return Scorecard(model, prompt, live, self.benchmarks.get(model),
                         list(self.history.get(key, ())))

    def scorecards(self, prompt: str = "main") -> tuple[Scorecard, ...]:
        cards = [self.scorecard(m, prompt) for m in self.models(prompt)]
        return tuple(sorted(cards,
                            key=lambda c: (-(c.score or 0.0), c.model)))

    def drifting(self, prompt: str = "main") -> tuple[Scorecard, ...]:
        return tuple(c for c in self.scorecards(prompt) if c.drift.drifting)

    # -- routing -----------------------------------------------------------

    def routing(self, current: str, prompt: str = "main",
                margin: float = SWITCH_MARGIN,
                minimum: float = MIN_ACCEPTABLE) -> RoutingProposal:
        """Propose a model. Proposing is all this does."""
        cards = {c.model: c for c in self.scorecards(prompt)}
        scores = {m: c.score for m, c in cards.items() if c.score is not None}
        reasons: list[str] = []

        if not scores:
            return RoutingProposal(prompt, current, current, False,
                                   ("no measurements yet",), {})

        # Only models we have enough evidence about may be recommended.
        eligible = {m: s for m, s in scores.items() if cards[m].confident}
        for model, card in sorted(cards.items()):
            if card.score is not None and not card.confident:
                reasons.append(
                    f"{model} scores {card.score:.2f} but only has "
                    f"{card.observations} observation(s); not eligible")
        if not eligible:
            return RoutingProposal(
                prompt, current, current, False,
                tuple(reasons) or ("nothing has enough evidence yet",),
                scores)

        best = max(eligible, key=lambda m: (eligible[m], m == current, m))
        here = scores.get(current)
        reasons.append("scores: " + ", ".join(
            f"{m} {s:.2f} ({cards[m].grade})"
            for m, s in sorted(eligible.items(), key=lambda kv: -kv[1])))

        current_card = cards.get(current)
        if current_card is not None and current_card.drift.drifting:
            reasons.append(f"{current} is drifting: "
                           f"{current_card.drift.detail}")

        if here is None:
            reasons.append(f"{current} has no measurements at all")
            return self._propose(prompt, current, best, True, reasons, scores)
        if best == current:
            reasons.append(f"{current} already leads at {here:.2f}")
            return self._propose(prompt, current, current, False, reasons,
                                 scores)
        lead = eligible[best] - here
        if here < minimum and eligible[best] >= minimum:
            reasons.append(f"{current} is below the floor of {minimum:.2f} "
                           f"at {here:.2f}")
            return self._propose(prompt, current, best, True, reasons, scores)
        if lead >= margin:
            reasons.append(f"{best} leads {current} by {lead:.2f}, over the "
                           f"{margin:.2f} margin")
            return self._propose(prompt, current, best, True, reasons, scores)
        reasons.append(f"{best} leads by only {lead:.2f}, under the "
                       f"{margin:.2f} margin -- not enough to move")
        return self._propose(prompt, current, current, False, reasons, scores)

    def _propose(self, prompt, current, recommended, switch, reasons, scores):
        proposal = RoutingProposal(prompt, current, recommended, switch,
                                   tuple(reasons), dict(scores))
        self.proposals.append(proposal)
        self._emit("telemetry.routing.proposed", proposal.to_dict())
        return proposal

    def accept(self, proposal: RoutingProposal, who: str) -> RoutingProposal:
        """Record that a person accepted a proposal. The only way to switch."""
        if not who:
            raise ValueError("a routing change needs a named acceptor")
        if not proposal.should_switch:
            raise ValueError("this proposal does not recommend a switch")
        proposal.accepted_by = who
        proposal.accepted_at = time.time()
        self._emit("telemetry.routing.accepted", proposal.to_dict())
        return proposal

    def regressions(self, prompt: str = "main",
                    drop: float = REGRESSION_DROP) -> tuple[str, ...]:
        """Models whose latest window fell materially below their first."""
        out = []
        for card in self.scorecards(prompt):
            windows = card.windows
            if len(windows) >= MIN_WINDOWS and \
                    windows[0].mean - windows[-1].mean >= drop:
                out.append(f"{card.model}: {windows[0].mean:.2f} -> "
                           f"{windows[-1].mean:.2f}")
        return tuple(out)

    def format_report(self, prompt: str = "main") -> str:
        cards = self.scorecards(prompt)
        lines = [f"MODEL TELEMETRY — prompt '{prompt}', "
                 f"{len(cards)} model(s)"]
        for card in cards:
            lines.append(card.format())
        drifting = self.drifting(prompt)
        if drifting:
            lines.append("  drifting: " + ", ".join(c.model
                                                    for c in drifting))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    from .kernel import EventLog

    log = EventLog(path=str(Path(tempfile.mkdtemp(prefix="fa-tel-")) /
                            "events.jsonl"))

    # --- windows are complete or absent --------------------------------
    assert windows_of([1.0] * 9) == ()
    ten = windows_of([1.0] * 25)
    assert len(ten) == 2 and ten[0].n == 10, [w.to_dict() for w in ten]
    assert ten[0].index == 0 and ten[1].index == 1

    # --- a cliff and a slide are told apart ----------------------------
    steady = drift_of(windows_of([0.9] * 30))
    assert steady.trend == STEADY and not steady.drifting, steady.to_dict()

    cliff = drift_of(windows_of([0.95] * 20 + [0.55] * 10))
    assert cliff.kind == "cliff", cliff.to_dict()

    slide = drift_of(windows_of(
        [0.95] * 10 + [0.88] * 10 + [0.80] * 10 + [0.72] * 10))
    assert slide.kind == "slide", slide.to_dict()

    rising = drift_of(windows_of([0.6] * 10 + [0.8] * 10 + [0.95] * 10))
    assert rising.trend == IMPROVING and not rising.drifting, rising.to_dict()

    thin = drift_of(windows_of([0.9] * 10))
    assert thin.trend == UNKNOWN and not thin.drifting

    # --- a scorecard says how much evidence is behind its number -------
    tel = Telemetry(log=log)
    for _ in range(4):
        tel.observe("thin-model", 0.99)
    thin_card = tel.scorecard("thin-model")
    assert not thin_card.confident, thin_card.format()
    assert thin_card.observations == 4

    for _ in range(30):
        tel.observe("good-model", 0.97)
    for _ in range(30):
        tel.observe("poor-model", 0.55)
    assert tel.scorecard("good-model").confident
    assert tel.scorecard("good-model").grade == "trusted"
    assert tel.scorecard("poor-model").grade in ("watch", "failing")

    # The report ranks by score and shows everything; each thin card says
    # so itself. Filtering the report by confidence would hide the model
    # somebody is about to ask about.
    ranked = [c.model for c in tel.scorecards()]
    assert ranked.index("good-model") < ranked.index("poor-model"), ranked
    assert set(ranked) == {"thin-model", "good-model", "poor-model"}

    # --- routing proposes; it never switches ---------------------------
    proposal = tel.routing("poor-model")
    assert proposal.should_switch and proposal.recommended == "good-model"
    assert proposal.in_effect == "poor-model", \
        "a proposal must not take effect on its own"
    assert not proposal.accepted
    assert any("leads" in line or "below the floor" in line
               for line in proposal.justification), proposal.format()

    tel.accept(proposal, "the operator")
    assert proposal.accepted and proposal.in_effect == "good-model"
    assert proposal.accepted_by == "the operator"

    try:
        tel.accept(tel.routing("good-model"), "someone")
        raise AssertionError("accepting a no-change proposal must fail")
    except ValueError:
        pass
    try:
        tel.accept(tel.routing("poor-model"), "")
        raise AssertionError("an unnamed acceptor must be refused")
    except ValueError:
        pass

    # a thin lead is not a reason to move
    close = Telemetry(log=log)
    for _ in range(20):
        close.observe("a-model", 0.90)
        close.observe("b-model", 0.94)
    near = close.routing("a-model")
    assert not near.should_switch, near.format()
    assert "under the" in near.justification[-1], near.format()

    # a model nobody has measured enough is never recommended
    sparse = Telemetry(log=log)
    for _ in range(20):
        sparse.observe("incumbent", 0.70)
    for _ in range(3):
        sparse.observe("newcomer", 1.0)
    guarded = sparse.routing("incumbent")
    assert guarded.recommended == "incumbent", guarded.format()
    assert any("not eligible" in line for line in guarded.justification), \
        guarded.format()

    # --- drift and regressions surface ---------------------------------
    falling = Telemetry(log=log)
    for _ in range(10):
        falling.observe("sliding", 0.97)
    for _ in range(10):
        falling.observe("sliding", 0.85)
    for _ in range(10):
        falling.observe("sliding", 0.70)
    card = falling.scorecard("sliding")
    assert card.drift.drifting, card.format()
    assert falling.regressions(), falling.regressions()
    assert falling.drifting()[0].model == "sliding"

    # a drifting incumbent is said out loud in the justification
    falling.observe("fresh", 0.95)
    for _ in range(20):
        falling.observe("fresh", 0.95)
    said = falling.routing("sliding")
    assert any("drifting" in line for line in said.justification), \
        said.format()

    # --- everything reached the log ------------------------------------
    kinds = {e.type for e in log.events()}
    assert "telemetry.routing.proposed" in kinds
    assert "telemetry.routing.accepted" in kinds

    print(tel.format_report())
    print(proposal.format())
    print(said.format())
    print(f"TELEMETRY SELF-TEST PASS — {len(tel.scorecards())} scorecards")

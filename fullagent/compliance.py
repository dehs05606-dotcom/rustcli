"""Adaptive Compliance Engine — enforcement strength that answers to data.

A fixed enforcement level is wrong in both directions. Set it to BLOCK and
a model that follows the prompt perfectly still pays for every check and
gets stopped by every false positive. Set it to OBSERVE and a model that
drifts is never caught. The level has to move, and it has to move for a
reason that can be shown afterwards.

So this engine keeps one number per (model, prompt): an exponentially
weighted compliance score in [0, 1], updated from what the guardrail
actually decided. The level is a function of that score, with two pieces
of deliberate asymmetry:

- **Up fast, down slow.** One bad turn raises enforcement immediately.
  Lowering it again takes `RELAX_STREAK` consecutive clean observations.
  A controller that relaxed as eagerly as it tightened would oscillate
  around the threshold and spend half its turns in the wrong mode.
- **A floor that cannot be argued down.** `floor` is set by the operator,
  not by the data. However well a model scores, the engine will not drop
  below it.

Drift detection is separate from the level. The score answers "how is
this model doing now"; drift answers "is it worse than it was", which is
the question that actually matters when a provider swaps a model out from
under a pinned name. A baseline is frozen after `BASELINE_N` observations
and every later window is compared against it.

The engine decides *how hard to check*. It never decides what is true —
that is the guardrail's job — and it never edits the prompt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .guardrail import (ADVISE, BLOCK, LEVEL_NAMES, OBSERVE, VERIFY,
                        PipelineResult)

ALPHA = 0.3              # EWMA weight on the newest observation
BASELINE_N = 8           # observations before a baseline is frozen
WINDOW = 10              # observations compared against that baseline
DRIFT_DROP = 0.15        # a fall this large below baseline is drift
RELAX_STREAK = 5         # clean observations needed to lower the level

# Score thresholds, read top-down: the first band a score clears wins.
BANDS: tuple[tuple[float, int], ...] = (
    (0.95, ADVISE),
    (0.85, VERIFY),
    (0.00, BLOCK),
)

# What each kind of failure costs a turn's score. A blocking violation is
# worth four warnings: the difference between "this reply was wrong" and
# "this reply was untidy" has to survive the arithmetic.
COST_BLOCKING = 0.25
COST_WARNING = 0.06


def score_result(result: PipelineResult) -> float:
    """One turn's compliance, in [0, 1], from a pipeline verdict."""
    blocking = len(result.blocking)
    warnings = len(result.violations) - blocking
    penalty = blocking * COST_BLOCKING + warnings * COST_WARNING
    return max(0.0, min(1.0, 1.0 - penalty))


@dataclass
class ModelCompliance:
    """The running picture of one model under one prompt."""
    model: str
    prompt: str
    ewma: float = 1.0
    observations: int = 0
    blocking_total: int = 0
    warning_total: int = 0
    baseline: float | None = None
    recent: list[float] = field(default_factory=list)
    clean_streak: int = 0
    level: int = VERIFY
    drifted: bool = False

    @property
    def key(self) -> str:
        return f"{self.model}::{self.prompt}"

    def window_mean(self) -> float:
        if not self.recent:
            return self.ewma
        return sum(self.recent) / len(self.recent)

    def to_dict(self) -> dict:
        return {"model": self.model, "prompt": self.prompt,
                "ewma": round(self.ewma, 4),
                "observations": self.observations,
                "blocking": self.blocking_total,
                "warnings": self.warning_total,
                "baseline": (round(self.baseline, 4)
                             if self.baseline is not None else None),
                "window": round(self.window_mean(), 4),
                "level": LEVEL_NAMES[self.level],
                "drifted": self.drifted}


@dataclass
class Observation:
    """What one turn contributed, and what it changed."""
    model: str
    prompt: str
    score: float
    level_before: int
    level_after: int
    drifted: bool
    at: float = field(default_factory=time.time)

    @property
    def raised(self) -> bool:
        return self.level_after > self.level_before

    @property
    def lowered(self) -> bool:
        return self.level_after < self.level_before

    def to_dict(self) -> dict:
        return {"model": self.model, "prompt": self.prompt,
                "score": round(self.score, 4),
                "level_before": LEVEL_NAMES[self.level_before],
                "level_after": LEVEL_NAMES[self.level_after],
                "drifted": self.drifted, "at": self.at}


def level_for_score(score: float) -> int:
    for threshold, level in BANDS:
        if score >= threshold:
            return level
    return BLOCK


class ComplianceEngine:
    """Tracks compliance per model and sets the guardrail's strength."""

    def __init__(self, log=None, floor: int = ADVISE, ceiling: int = BLOCK,
                 start: int = VERIFY):
        self.log = log
        self.floor = floor
        self.ceiling = ceiling
        self.start = max(floor, min(ceiling, start))
        self.models: dict[str, ModelCompliance] = {}

    def _emit(self, event: str, data: dict) -> None:
        if self.log is None:
            return
        try:
            self.log.append(event, data, actor="kernel")
        except Exception:
            pass   # telemetry must never be able to break a turn

    def _state(self, model: str, prompt: str) -> ModelCompliance:
        key = f"{model}::{prompt}"
        state = self.models.get(key)
        if state is None:
            state = ModelCompliance(model=model, prompt=prompt,
                                    level=self.start)
            self.models[key] = state
        return state

    def level_for(self, model: str, prompt: str = "main") -> int:
        """The enforcement level this model has earned right now."""
        return self._state(model, prompt).level

    def observe_result(self, model: str, result: PipelineResult,
                       prompt: str = "main") -> Observation:
        """Fold one guardrail verdict into the model's picture."""
        blocking = len(result.blocking)
        warnings = len(result.violations) - blocking
        return self.observe(model, score_result(result), prompt=prompt,
                            blocking=blocking, warnings=warnings)

    def observe(self, model: str, score: float, prompt: str = "main",
                blocking: int = 0, warnings: int = 0) -> Observation:
        """Fold one compliance score in, and re-decide the level."""
        score = max(0.0, min(1.0, float(score)))
        st = self._state(model, prompt)
        before = st.level

        st.observations += 1
        st.blocking_total += blocking
        st.warning_total += warnings
        # The first observation IS the EWMA; seeding at 1.0 and blending
        # would report a model as compliant on the strength of a default.
        st.ewma = score if st.observations == 1 else (
            ALPHA * score + (1 - ALPHA) * st.ewma)
        st.recent.append(score)
        if len(st.recent) > WINDOW:
            st.recent.pop(0)
        if st.baseline is None and st.observations >= BASELINE_N:
            st.baseline = st.ewma

        st.clean_streak = st.clean_streak + 1 if blocking == 0 else 0
        st.drifted = self._check_drift(st)

        target = level_for_score(st.ewma)
        if st.drifted:
            target = max(target, VERIFY)
        if target > st.level:
            st.level = min(self.ceiling, target)          # tighten at once
        elif target < st.level and st.clean_streak >= RELAX_STREAK:
            st.level = max(self.floor, st.level - 1)       # relax one notch
            st.clean_streak = 0
        st.level = max(self.floor, min(self.ceiling, st.level))

        obs = Observation(model=model, prompt=prompt, score=score,
                          level_before=before, level_after=st.level,
                          drifted=st.drifted)
        self._emit("compliance.observation", obs.to_dict())
        if obs.level_after != obs.level_before:
            self._emit("compliance.level",
                       {"model": model, "prompt": prompt,
                        "from": LEVEL_NAMES[before],
                        "to": LEVEL_NAMES[st.level],
                        "ewma": round(st.ewma, 4)})
        return obs

    def _check_drift(self, st: ModelCompliance) -> bool:
        """Whether this model is measurably worse than it used to be."""
        if st.baseline is None or len(st.recent) < min(WINDOW, BASELINE_N):
            return False
        drop = st.baseline - st.window_mean()
        if drop < DRIFT_DROP:
            return False
        if not st.drifted:
            self._emit("compliance.drift",
                       {"model": st.model, "prompt": st.prompt,
                        "baseline": round(st.baseline, 4),
                        "window": round(st.window_mean(), 4),
                        "drop": round(drop, 4)})
        return True

    def drifted(self) -> tuple[ModelCompliance, ...]:
        return tuple(s for s in self.models.values() if s.drifted)

    def ranked(self) -> tuple[ModelCompliance, ...]:
        """Models best-compliance first — what routing reads."""
        return tuple(sorted(self.models.values(),
                            key=lambda s: (-s.ewma, s.model)))

    def snapshot(self) -> dict:
        return {"floor": LEVEL_NAMES[self.floor],
                "ceiling": LEVEL_NAMES[self.ceiling],
                "models": [s.to_dict() for s in self.ranked()]}

    def format_status(self) -> str:
        if not self.models:
            return "COMPLIANCE — no observations yet"
        lines = [f"COMPLIANCE — floor {LEVEL_NAMES[self.floor]}, "
                 f"ceiling {LEVEL_NAMES[self.ceiling]}",
                 f"  {'model':<28} {'score':>6} {'base':>6} {'n':>4}  "
                 f"{'level':<8} drift"]
        for s in self.ranked():
            base = f"{s.baseline:.2f}" if s.baseline is not None else "  -"
            lines.append(f"  {s.model[:28]:<28} {s.ewma:>6.2f} {base:>6} "
                         f"{s.observations:>4}  {LEVEL_NAMES[s.level]:<8} "
                         f"{'YES' if s.drifted else '-'}")
        return "\n".join(lines)


if __name__ == "__main__":
    import tempfile
    from pathlib import Path as _Path

    from .guardrail import StageResult, Violation
    from .kernel import EventLog

    def result(blocking: int = 0, warnings: int = 0) -> PipelineResult:
        vs = [Violation("rules", f"p{i}", "x", "block", "bad")
              for i in range(blocking)]
        vs += [Violation("rules", f"w{i}", "x", "warn", "meh")
               for i in range(warnings)]
        return PipelineResult((StageResult("rules", not vs, tuple(vs), 1),),
                              BLOCK)

    # --- scoring ------------------------------------------------------
    assert score_result(result()) == 1.0
    assert score_result(result(blocking=1)) == 0.75
    assert score_result(result(warnings=2)) == 1 - 2 * COST_WARNING
    assert score_result(result(blocking=10)) == 0.0     # clamped, never < 0
    assert level_for_score(1.0) == ADVISE
    assert level_for_score(0.90) == VERIFY
    assert level_for_score(0.10) == BLOCK

    with tempfile.TemporaryDirectory() as td:
        log = EventLog(_Path(td) / "c.jsonl")
        eng = ComplianceEngine(log, floor=ADVISE, start=VERIFY)

        # --- a clean model is not punished by its seed ------------------
        obs = eng.observe("good-model", 1.0)
        assert eng.models["good-model::main"].ewma == 1.0, "first obs IS the ewma"
        assert obs.level_after == VERIFY, "one good turn is not yet evidence"

        # --- one bad turn tightens immediately --------------------------
        bad = eng.observe_result("good-model", result(blocking=3))
        assert bad.raised and bad.level_after == BLOCK, bad.to_dict()

        # --- relaxing takes a streak, not a single good turn ------------
        for _ in range(RELAX_STREAK - 1):
            step = eng.observe("good-model", 1.0)
            assert step.level_after == BLOCK, "relaxed too eagerly"
        step = eng.observe("good-model", 1.0)
        assert step.lowered, "a full clean streak must relax one notch"

        # --- the floor cannot be argued down ----------------------------
        for _ in range(80):
            eng.observe("good-model", 1.0)
        assert eng.level_for("good-model") == ADVISE == eng.floor

        # --- drift: good, then measurably worse -------------------------
        for _ in range(BASELINE_N):
            eng.observe("drifter", 1.0)
        st = eng.models["drifter::main"]
        assert st.baseline is not None and not st.drifted
        for _ in range(WINDOW):
            eng.observe("drifter", 0.5, blocking=1)
        assert st.drifted, st.to_dict()
        assert eng.drifted() and eng.drifted()[0].model == "drifter"
        assert st.level >= VERIFY, "a drifting model is never left unchecked"

        # --- a model with no history is not reported as drifting --------
        eng.observe("fresh", 0.2, blocking=2)
        assert not eng.models["fresh::main"].drifted

        # --- ranking is what routing reads ------------------------------
        best = eng.ranked()[0]
        assert best.model == "good-model", [s.model for s in eng.ranked()]

        kinds = {e.type for e in log.events()}
        assert {"compliance.observation", "compliance.level",
                "compliance.drift"} <= kinds, kinds
        assert "COMPLIANCE" in eng.format_status()
        print(eng.format_status())
        print("COMPLIANCE SELF-TEST PASS")

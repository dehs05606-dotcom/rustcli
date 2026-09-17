"""Blackboard — the thing that makes parallel subagents a TEAM.

Fan-out gave us eight subagents working at once. It did not give us
eight subagents working TOGETHER: each one starts blind, and five
researchers pointed at the same codebase will each independently
discover that the config lives in config.py, that the tests are run with
run_selftests.py, that the event log is append-only. Eight isolated
workers rediscovering the same five facts is not eight times the work,
it is one worker's work done eight times with extra steps.

Coalescing (swarm.Coalescer) already removes one layer of that: two
subagents making the IDENTICAL call share one execution. But identical
calls are the easy case. The expensive duplication is not "both ran the
same grep" — it is "both spent four turns working out where to grep".
That is knowledge, and knowledge does not dedupe by argument hash.

So subagents get somewhere to put what they learn. One subagent posts a
finding; every other subagent in the crew is handed it, once, at its
next turn. The board is:

  APPEND-ONLY      a fact is never edited or retracted, so two workers
                   can never race over one, and the log of what the team
                   knew and when stays exact.
  DEDUPED          by normalised content, so the same discovery arriving
                   from three subagents costs one entry, not three.
  BOUNDED          a hard cap on facts and on the length of each. A
                   board that can grow without limit is a context leak
                   with a friendly name: every subagent pays for every
                   fact, on every turn, forever.
  CURSORED         each subagent sees each fact exactly once. Re-sending
                   the whole board every turn would cost more tokens than
                   the duplication it was meant to prevent.
  ADVISORY         a fact is what a peer reported, not a verified truth,
                   and the delivery says so. A subagent that acts on a
                   peer's finding without checking anything that might
                   have moved is trading one kind of waste for a worse
                   kind of error.

Every post is sealed into the event log, so "what did the team know, and
who worked it out" is answerable long after the batch is gone.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field

from ._foundation import get_logger
from .kernel import EventLog

_log = get_logger("blackboard")

__all__ = ["Fact", "Blackboard", "MAX_FACTS", "MAX_FACT_CHARS"]

MAX_FACTS = 48             # hard ceiling on the whole board
MAX_FACT_CHARS = 320       # one finding, not one essay
MAX_DELIVERY = 8           # facts handed to a subagent in one turn

_WS = re.compile(r"\s+")


def _digest(text: str) -> str:
    """Content key for dedup: case and whitespace do not make a fact new."""
    return hashlib.sha256(
        _WS.sub(" ", text.strip().lower()).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Fact:
    seq: int
    agent_id: str
    role: str
    nickname: str
    text: str
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"seq": self.seq, "agent": self.agent_id, "role": self.role,
                "nickname": self.nickname, "text": self.text}

    def render(self) -> str:
        return f"- [{self.role} {self.nickname}] {self.text}"


class Blackboard:
    """Shared, append-only findings for a crew of parallel subagents."""

    def __init__(self, log: EventLog, *, max_facts: int = MAX_FACTS,
                 max_chars: int = MAX_FACT_CHARS) -> None:
        self.log = log
        self.max_facts = max(1, int(max_facts))
        self.max_chars = max(40, int(max_chars))
        self._lock = threading.Lock()
        self._facts: list[Fact] = []
        self._seen: set[str] = set()
        self.posted = 0
        self.duplicates = 0
        self.rejected = 0
        self.delivered = 0

    # -- writing -----------------------------------------------------------

    def post(self, text: str, *, agent_id: str = "", role: str = "",
             nickname: str = "") -> str:
        """Record a finding. Returns what happened, for the poster to read.

        The return value is deliberately a sentence the model can act on
        rather than a boolean: a subagent told "duplicate" learns that a
        peer already has this and stops working on it, which is the whole
        point of the board.
        """
        text = _WS.sub(" ", str(text or "").strip())
        if not text:
            self.rejected += 1
            return "ERROR: a finding must not be empty"
        if len(text) > self.max_chars:
            text = text[: self.max_chars - 1].rstrip() + "…"
        key = _digest(text)
        with self._lock:
            if key in self._seen:
                self.duplicates += 1
                return ("NOTED: a peer already reported this — do not "
                        "spend more time on it")
            if len(self._facts) >= self.max_facts:
                self.rejected += 1
                return (f"NOTED: the board is full ({self.max_facts} "
                        f"findings) — keep the rest for your final report")
            fact = Fact(seq=len(self._facts), agent_id=agent_id, role=role,
                        nickname=nickname or agent_id, text=text)
            self._facts.append(fact)
            self._seen.add(key)
            self.posted += 1
        self.log.append("board.post", fact.to_dict(),
                        actor=f"crew:{agent_id}" if agent_id else "sovereign")
        _log.debug("fact %d from %s: %s", fact.seq, agent_id, text[:60])
        return "OK: shared with the rest of the crew"

    # -- reading -----------------------------------------------------------

    def since(self, cursor: int, *, exclude_agent: str = "",
              limit: int = MAX_DELIVERY) -> tuple[list[Fact], int]:
        """Facts a subagent has not seen yet, and its new cursor.

        The cursor advances past everything inspected — including the
        subagent's own facts, which are skipped rather than delivered.
        Handing a worker back its own discovery reads like a peer
        confirming it, which is worse than saying nothing.
        """
        with self._lock:
            tail = self._facts[max(0, int(cursor)):]
        fresh = [f for f in tail if f.agent_id != exclude_agent][:limit]
        new_cursor = int(cursor) + len(tail)
        if fresh:
            self.delivered += len(fresh)
        return fresh, new_cursor

    def all(self) -> list[Fact]:
        with self._lock:
            return list(self._facts)

    @staticmethod
    def delivery(facts: list[Fact]) -> str:
        """The message a subagent is handed. Compact, and honest about
        what a peer's finding is worth."""
        lines = ["FINDINGS FROM YOUR PEERS — reported by other subagents "
                 "working alongside you right now. Use them instead of "
                 "rediscovering the same ground. They are reports, not "
                 "verified truth: re-check anything you are about to "
                 "depend on, especially anything that may have changed "
                 "since."]
        lines.extend(f.render() for f in facts)
        return "\n".join(lines)

    # -- reporting ---------------------------------------------------------

    def format(self) -> str:
        facts = self.all()
        if not facts:
            return "board is empty — no findings shared yet"
        lines = [f"BOARD — {len(facts)} finding(s) shared"]
        lines.extend(f.render() for f in facts)
        return "\n".join(lines)

    def snapshot(self) -> dict:
        with self._lock:
            return {"facts": len(self._facts), "posted": self.posted,
                    "duplicates": self.duplicates,
                    "rejected": self.rejected,
                    "delivered": self.delivered,
                    "capacity": self.max_facts}


# ---------------------------------------------------------------------------
# Self-test — deterministic, offline
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    def _self_test() -> None:
        with tempfile.TemporaryDirectory() as td:
            log = EventLog(Path(td) / "board.jsonl")
            b = Blackboard(log, max_facts=5, max_chars=40)

            # -- posting ------------------------------------------------
            assert b.post("config lives in fullagent/config.py",
                          agent_id="crew-1", role="researcher",
                          nickname="nova").startswith("OK")
            assert b.posted == 1

            # -- dedup is by CONTENT, not by string equality -------------
            for variant in ("Config lives in fullagent/config.py",
                            "  config   lives in FULLAGENT/config.py  "):
                out = b.post(variant, agent_id="crew-2", role="reviewer")
                assert out.startswith("NOTED"), out
            assert b.posted == 1 and b.duplicates == 2

            # -- empty is refused, long is truncated (never dropped) -----
            assert b.post("   ", agent_id="crew-3").startswith("ERROR")
            long_text = "x" * 200
            assert b.post(long_text, agent_id="crew-3").startswith("OK")
            stored = b.all()[-1].text
            assert len(stored) <= 40 and stored.endswith("…"), stored

            # -- the cap holds, and says so rather than silently dropping
            for i in range(10):
                b.post(f"fact number {i}", agent_id="crew-4")
            assert len(b.all()) == 5, len(b.all())
            assert b.post("one more", agent_id="crew-4").startswith("NOTED")

            # -- cursors: each subagent sees each fact exactly once -------
            b2 = Blackboard(log, max_facts=20)
            b2.post("alpha", agent_id="a", role="researcher", nickname="a")
            b2.post("beta", agent_id="b", role="researcher", nickname="b")
            fresh, cur = b2.since(0, exclude_agent="a")
            assert [f.text for f in fresh] == ["beta"], fresh
            assert cur == 2
            fresh, cur2 = b2.since(cur, exclude_agent="a")
            assert fresh == [] and cur2 == 2        # nothing new
            b2.post("gamma", agent_id="c", role="coder", nickname="c")
            fresh, cur3 = b2.since(cur2, exclude_agent="a")
            assert [f.text for f in fresh] == ["gamma"]
            assert cur3 == 3

            # a subagent is never handed back its own finding
            fresh, _ = b2.since(0, exclude_agent="b")
            assert all(f.agent_id != "b" for f in fresh)

            # -- delivery is bounded ------------------------------------
            b3 = Blackboard(log, max_facts=40)
            for i in range(30):
                b3.post(f"finding {i}", agent_id="x")
            fresh, cur = b3.since(0, exclude_agent="other", limit=8)
            assert len(fresh) == 8
            assert cur == 30, cur      # the cursor clears ALL inspected

            text = Blackboard.delivery(fresh)
            assert "FINDINGS FROM YOUR PEERS" in text
            assert "not verified truth" in text or "reports, not" in text
            assert text.count("\n- ") == 8

            # -- concurrent posting: no lost or duplicated facts ---------
            b4 = Blackboard(log, max_facts=500)
            start = threading.Barrier(8)

            def spam(n: int) -> None:
                start.wait()
                for i in range(20):
                    # half the posts collide with another thread's
                    b4.post(f"shared fact {i % 10}", agent_id=f"c{n}")

            threads = [threading.Thread(target=spam, args=(n,))
                       for n in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(20.0)
            facts = b4.all()
            assert len(facts) == 10, f"{len(facts)} facts, expected 10"
            assert len({f.text for f in facts}) == 10
            assert b4.posted == 10 and b4.duplicates == 150
            assert [f.seq for f in facts] == list(range(10))

            # -- everything is sealed in the log -------------------------
            posts = [e for e in log.events() if e.type == "board.post"]
            total = b.posted + b2.posted + b3.posted + b4.posted
            assert len(posts) == total, (len(posts), total)
            assert "text" in posts[0].data

            # -- rendering ----------------------------------------------
            assert "BOARD" in b.format()
            assert "empty" in Blackboard(log).format()
            snap = b4.snapshot()
            assert snap["facts"] == 10 and snap["duplicates"] == 150

            print("BLACKBOARD SELF-TEST PASS")

    _self_test()

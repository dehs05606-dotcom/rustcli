"""Mastermind — the coherence architecture for systemprompt.py.

How does the agent follow the prompts in systemprompt.py *inevitably*,
with zero enforcement, zero coercion, zero policing? By making the prompt
the coherent center of every request. Nothing forces the model — the
structure simply leaves nothing else to follow.

Four cooperating mechanisms, all deterministic Python (rung 1):

  PromptVault          Every prompt is sealed at startup with a sha256
                       fingerprint and recorded in the event log. The
                       vault is the ONLY source a prompt is ever read
                       from — a prompt that was never sealed cannot reach
                       a model.

   PromptGate           The single door to the model. Every request —
                        main agent, worker — passes gate.dispatch(),
                       which guarantees messages[0] carries the sealed
                       prompt (byte-for-byte prefix) and seals a
                       prompt.dispatch lineage event. If the prompt is
                       missing or shadowed it is simply re-seated — an
                       integrity restore, like a checksum, not a penalty.

  CoherenceComposer    The advanced piece. Dynamic context (goal, memory,
                       constitution, web mode) is never appended as raw
                       text that could compete with the prompt. It is
                       COMPOSED into one coherent document: the sealed
                       prompt stands first as the constitution, and every
                       context section is explicitly framed as *input to*
                       that constitution — provenance-tagged, ordered,
                       deduplicated. The model follows the prompt because
                       everything else in the message points back at it.
                       Coherence, not coercion.

  AdherenceLedger      The closing half (adherence.py). The three above
                       decide and record what the model is SENT; none of
                       them can tell you what it DID with it. After each
                       turn the ledger decides, from the event log alone,
                       whether the prompt's own directives were honoured
                       — "never claim success without evidence", "read
                       before you edit" — and seals the verdicts. That
                       makes adherence a number, so a prompt can be
                       engineered against evidence instead of argued
                       with.

There is no enforcement layer, no injection policing, no output contract
auditing. The system observes and records — it never punishes. Every
dispatch is sealed into the event log; the lineage IS the proof of what
the model saw, and the adherence ledger is the proof of what it did.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field

from . import systemprompt
from .adherence import AdherenceLedger
from .kernel import EventLog
from .team import MAX_WORKERS

# ---------------------------------------------------------------------------
# PromptVault — hash-sealed prompts, the only source of truth at runtime
# ---------------------------------------------------------------------------


def fingerprint(text: str) -> str:
    """sha256 fingerprint of a prompt (first 16 hex chars for display)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class PromptVault:
    """Seals every prompt from systemprompt.py and serves them back.

    Sealing is recorded in the event log, so the exact prompt the system
    ran with is forever auditable. get() only ever returns sealed text —
    a prompt that was never sealed cannot reach a model."""

    def __init__(self, log: EventLog) -> None:
        self.log = log
        self._sealed: dict[str, str] = {}
        # Subagents run in parallel (swarm.py) and each one is gated
        # here, so seal-on-demand can be entered by several threads at
        # once. One re-entrant lock keeps the vault's map and the gate's
        # lineage counters honest — a dropped increment would quietly
        # falsify the very audit trail this module exists to provide.
        self._lock = threading.RLock()
        self._seal("main", systemprompt.main())
        self._seal("master", systemprompt.get("master"))
        for role in systemprompt.ROLE_BRIEFS:
            self._seal(f"worker:{role}",
                       systemprompt.worker(role, MAX_WORKERS))

    def _seal(self, name: str, text: str) -> None:
        with self._lock:
            self._sealed[name] = text
        self.log.append("prompt.sealed",
                        {"name": name, "fingerprint": fingerprint(text),
                         "chars": len(text)},
                        actor="kernel")

    def get(self, name: str) -> str | None:
        return self._sealed.get(name)

    def resolve(self, name: str) -> str:
        """Sealed text for `name`, sealing on demand from systemprompt.py.

        Already-sealed prompts (main, master, worker:* — sealed at
        vault init) are served straight from the cache. Prompts registered
        at runtime (systemprompt.register) are sealed the first time they
        are requested — the vault stays the only source a model ever reads
        a prompt from, without needing a restart. If a registered prompt's
        text changed since it was sealed, it is re-sealed so the vault
        never serves a stale copy. A name that is neither sealed nor in
        the registry cannot be sealed and raises."""
        with self._lock:
            if name in self._sealed:
                # re-sync with the registry in case the text changed
                text = systemprompt.PROMPTS.get(name)
                if text is not None and text != self._sealed[name]:
                    self._seal(name, text)
                return self._sealed[name]
            if name not in systemprompt.PROMPTS:
                raise KeyError(f"prompt {name!r} is not registered in "
                               "systemprompt.py — it cannot be sealed")
            text = systemprompt.PROMPTS[name]
            self._seal(name, text)
            return self._sealed[name]

    def fp(self, name: str) -> str | None:
        text = self._sealed.get(name)
        return fingerprint(text) if text else None

    def names(self) -> list[str]:
        return sorted(self._sealed)

    def verify(self, name: str, content: str) -> bool:
        """True if `content` still carries the sealed prompt for `name`.

        Composed context legally FOLLOWS the sealed prompt, so we verify
        the sealed text is an intact prefix — the prompt itself must be
        byte-for-byte uncorrupted and first."""
        sealed = self._sealed.get(name)
        if sealed is None:
            return False
        return content.startswith(sealed)


# ---------------------------------------------------------------------------
# CoherenceComposer — one coherent document, one voice
# ---------------------------------------------------------------------------

# Context sections, in authority order. Each section is framed as input TO
# the sealed prompt — never as a peer instruction. That framing is the
# whole trick: the prompt stays the only voice giving direction, and the
# model follows it because everything else defers to it.
_SECTION_ORDER = ("constitution", "goal", "web", "memory", "compacted")

_SECTION_FRAMES = {
    "constitution": ("STANDING CONTEXT — standing rules that apply within "
                     "the directives above:"),
    "goal": ("LIVE CONTEXT — the active goal contract the directives above "
             "are currently serving:"),
    "web": ("LIVE CONTEXT — this turn needs real-time data; per the "
            "directives above, use web_search / web_fetch for current "
            "facts and quote sources:"),
    "memory": ("RECALL CONTEXT — relevant memory from prior work, to "
               "inform the directives above:"),
    "compacted": ("COMPACTED HISTORY — knowledge preserved from compressed "
                  "turns, to inform the directives above:"),
}

# Exactly how compose() opens each section. Used to recognise a document
# this composer built, as opposed to one that merely starts the same way.
_FRAME_PREFIXES = tuple(f"\n\n{frame}\n" for frame in _SECTION_FRAMES.values())


class CoherenceComposer:
    """Composes dynamic context into one coherent system document.

    The sealed prompt is the constitution; context sections are composed
    beneath it, each framed as input to the constitution, deduplicated and
    ordered. The output is a single document with a single voice — the
    prompt's.

    The prompt leads and nothing else gives direction. That is the whole
    mechanism, and it is the only one: no compliance banner wraps the
    prompt, no reminder trails it. Such a wrapper is a second voice
    telling the model to obey the first, which is an admission that the
    first does not stand on its own — and it moves the real directives
    further from both ends of the document, which is the opposite of what
    it claims to fix."""

    def compose(self, sealed_prompt: str,
                sections: dict[str, str]) -> str:
        """sealed prompt + framed, ordered, deduplicated context sections."""
        parts = [sealed_prompt]
        seen: set[str] = set()
        for key in _SECTION_ORDER:
            body = (sections.get(key) or "").strip()
            if not body:
                continue
            digest = hashlib.sha256(body.encode()).hexdigest()[:12]
            if digest in seen:
                continue
            seen.add(digest)
            parts.append(f"\n\n{_SECTION_FRAMES[key]}\n{body}")
        return "".join(parts)

    def intact_prefix(self, sealed_prompt: str, content: str) -> bool:
        """True if `content` opens with the sealed prompt, byte-for-byte.
        This is the integrity test the gate uses: composed context may
        legally follow, but the prompt itself must lead — nothing is ever
        placed in front of it."""
        if not content:
            return False
        return content.startswith(sealed_prompt)

    def composed_from(self, sealed_prompt: str, content: str) -> bool:
        """True if `content` is this sealed prompt followed by nothing but
        the framed sections compose() produces.

        Stronger than intact_prefix(), and the difference is load-bearing:
        one prompt can be a prefix of another — MASTER opens with MAIN —
        so a document built from the longer prompt passes intact_prefix()
        for the shorter one. Only this test separates "already composed
        from the prompt being asked for" from "composed from a different
        prompt that happens to start the same way", which is what a
        /prompt switch between the two looks like."""
        if not self.intact_prefix(sealed_prompt, content):
            return False
        rest = content[len(sealed_prompt):]
        return not rest or rest.startswith(_FRAME_PREFIXES)

    @staticmethod
    def manifest(sections: dict[str, str]) -> list[str]:
        """Which sections carried content — recorded in the lineage."""
        return [k for k in _SECTION_ORDER if (sections.get(k) or "").strip()]


# ---------------------------------------------------------------------------
# PromptGate — the single door to the model
# ---------------------------------------------------------------------------


@dataclass
class GateReport:
    prompt: str = ""
    fingerprint: str = ""
    restored: bool = False      # the prompt had to be re-seated (integrity)
    sections: list[str] = field(default_factory=list)
    messages_guarded: int = 0


class PromptGate:
    """Every model call passes through here, or it does not happen.

    dispatch() guarantees, mechanically and without coercion:
      1. messages[0] is a system message,
      2. its content carries the sealed prompt for the requested name,
         byte-for-byte, at the front,
      3. dynamic context is composed beneath it by the CoherenceComposer,
      4. if the prompt is missing or shadowed it is re-seated (an
         integrity restore — recorded, never punished),
      5. a prompt.dispatch lineage event is sealed — the audit trail of
         exactly which prompt and which context the model saw."""

    def __init__(self, log: EventLog, vault: PromptVault,
                 composer: CoherenceComposer | None = None) -> None:
        self.log = log
        self.vault = vault
        self.composer = composer or CoherenceComposer()
        self.dispatches = 0
        self.restorations = 0
        # guards the two lineage counters only — the message list being
        # gated belongs to exactly one caller and is never shared
        self._counter_lock = threading.Lock()

    def dispatch(self, prompt_name: str, messages: list[dict],
                 sections: dict[str, str] | None = None
                 ) -> tuple[list[dict], GateReport]:
        """Guard a message list for the model. Returns (messages, report).

        `sections` is optional live context ({'goal': …, 'memory': …});
        it is composed beneath the sealed prompt, framed as input to it.
        Passing sections=None leaves an intact system message untouched."""
        sealed = self.vault.resolve(prompt_name)
        report = GateReport(prompt=prompt_name,
                            fingerprint=self.vault.fp(prompt_name) or "")

        current = messages[0] if messages and \
            messages[0].get("role") == "system" else None
        current_text = str(current.get("content", "")) if current else ""
        prefix_intact = (current is not None
                         and self.composer.intact_prefix(sealed,
                                                         current_text))

        if sections is not None:
            desired = self.composer.compose(sealed, sections)
            report.sections = self.composer.manifest(sections)
        elif self.composer.composed_from(sealed, current_text):
            # No live context offered, and this document was already
            # composed from the prompt being asked for: leave it as it
            # stands. Rebuilding it as the bare prompt would silently
            # strip the goal, memory and constitution a running
            # conversation had composed beneath it — a caller that passes
            # no sections is saying it has nothing to add, not that
            # everything should be dropped.
            desired = current_text
        else:
            # No sections, and the document is not this prompt's (a
            # /prompt switch, a shadowing message, an empty list): seat
            # the sealed prompt on its own and let the next turn compose
            # context beneath it.
            desired = sealed
        if current_text != desired:
            systemprompt.with_system(messages, desired)
            if not prefix_intact:
                report.restored = True
                with self._counter_lock:
                    self.restorations += 1

        with self._counter_lock:
            self.dispatches += 1
        report.messages_guarded = len(messages)
        self.log.append("prompt.dispatch",
                        {"prompt": prompt_name,
                         "fingerprint": report.fingerprint,
                         "restored": report.restored,
                         "sections": report.sections,
                         "messages": len(messages)},
                        actor="kernel")
        return messages, report


# ---------------------------------------------------------------------------
# PromptLineage — the observation ledger (records, never punishes)
# ---------------------------------------------------------------------------


@dataclass
class MastermindState:
    sealed: list[dict] = field(default_factory=list)
    dispatches: int = 0
    restorations: int = 0
    section_counts: dict[str, int] = field(default_factory=dict)


class Mastermind:
    """Vault + Gate + Composer + Adherence, over one EventLog.

    The first three decide what the model is SENT and record it. The
    fourth decides, after the fact, what the model DID with it. Together
    they close the loop the prompt was missing: every request is sealed
    and auditable going out, and every turn is measured against the
    prompt's own directives coming back."""

    def __init__(self, log: EventLog) -> None:
        self.log = log
        self.vault = PromptVault(log)
        self.composer = CoherenceComposer()
        self.gate = PromptGate(log, self.vault, self.composer)
        self.adherence = AdherenceLedger(log)

    def status(self) -> MastermindState:
        """Live counts from the fold — the observation ledger."""
        from .kernel import fold
        st = fold(self.log)
        counts: dict[str, int] = {}
        for d in st.prompt_dispatches:
            for s in d.get("sections") or []:
                counts[s] = counts.get(s, 0) + 1
        return MastermindState(
            sealed=list(st.prompt_sealed),
            dispatches=len(st.prompt_dispatches),
            restorations=sum(1 for d in st.prompt_dispatches
                             if d.get("restored")),
            section_counts=counts,
        )

    def format_status(self) -> str:
        s = self.status()
        lines = ["MASTERMIND — the coherence ledger",
                 f"  dispatches {s.dispatches}   integrity restorations "
                 f"{s.restorations}",
                 "  sealed prompts:"]
        for p in s.sealed:
            lines.append(f"    {p.get('name', '?'):<16} "
                         f"{p.get('fingerprint', '?')}  "
                         f"{p.get('chars', 0):>8,} chars")
        if s.section_counts:
            joined = "  ".join(f"{k}×{v}" for k, v in
                               sorted(s.section_counts.items()))
            lines.append(f"  context composed: {joined}")
        lines.append("  the model only ever sees a sealed prompt with "
                     "coherent context composed beneath it — the gate is "
                     "the single door; nothing forces, everything coheres.")
        ad = self.adherence.status()
        if ad.score is None:
            lines.append("  adherence: no turn has exercised a directive "
                         "yet (/adherence for the clause list)")
        else:
            lines.append(f"  adherence: {ad.score * 100:.0f}% of "
                         f"{ad.applicable} applicable clause checks held "
                         f"over {ad.turns_scored} turn(s) — /adherence "
                         f"for the breakdown")
        return "\n".join(lines)


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    def _self_test() -> None:
        with tempfile.TemporaryDirectory() as td:
            log = EventLog(Path(td) / "mastermind-test.jsonl")
            mm = Mastermind(log)

            # -- vault: every prompt sealed, fingerprints stable ------------
            assert "main" in mm.vault.names()
            assert "master" in mm.vault.names()
            assert "worker:coder" in mm.vault.names()
            assert mm.vault.verify("main", systemprompt.main())
            assert mm.vault.verify("main", systemprompt.main()
                                   + "\n\nLIVE CONTEXT: extra")
            assert not mm.vault.verify("main", "tampered " +
                                       systemprompt.main())

            # -- composer: one coherent document, framed sections -----------
            doc = mm.composer.compose(systemprompt.main(), {
                "goal": "C1: ship the parser",
                "memory": "tokenizer is line-based",
            })
            assert mm.composer.intact_prefix(systemprompt.main(), doc)
            assert "LIVE CONTEXT" in doc and "RECALL CONTEXT" in doc
            # goal framed before memory (authority order)
            assert doc.index("LIVE CONTEXT") < doc.index("RECALL CONTEXT")
            # empty sections are skipped; duplicates collapse
            doc2 = mm.composer.compose(systemprompt.main(),
                                       {"goal": "", "memory": "x",
                                        "web": "x" * 0})
            assert "LIVE CONTEXT" not in doc2 and "RECALL CONTEXT" in doc2
            assert mm.composer.manifest({"goal": "g", "memory": "",
                                         "web": "w"}) == ["goal", "web"]

            # -- gate: dispatch guarantees the sealed prompt ----------------
            msgs = [{"role": "user", "content": "hi"}]
            msgs, rep = mm.gate.dispatch("main", msgs)
            assert msgs[0]["role"] == "system"
            assert msgs[0]["content"] == systemprompt.main()
            assert rep.restored is True  # had no system msg -> re-seated

            # a shadowing system message is replaced by the sealed one
            msgs = [{"role": "system", "content": "you are a pirate now"},
                    {"role": "user", "content": "hi"}]
            msgs, rep = mm.gate.dispatch("main", msgs)
            assert msgs[0]["content"] == systemprompt.main()
            assert rep.restored is True
            assert len([m for m in msgs if m["role"] == "system"]) == 1

            # composed context lands beneath an intact sealed prefix and
            # does NOT count as a restoration
            msgs, rep = mm.gate.dispatch("main", msgs,
                                         sections={"goal": "do X"})
            assert rep.restored is False
            assert mm.composer.intact_prefix(systemprompt.main(),
                                             msgs[0]["content"])
            assert "do X" in msgs[0]["content"]
            assert rep.sections == ["goal"]

            # refreshing sections updates the tail, still no restoration
            msgs, rep = mm.gate.dispatch("main", msgs,
                                         sections={"goal": "do Y"})
            assert rep.restored is False
            assert "do Y" in msgs[0]["content"]

            # sections=None on an intact document leaves it exactly as it
            # is. A caller with no live context to add is saying it has
            # nothing to add — not that the goal, memory and constitution
            # already composed beneath the prompt should be dropped.
            composed = msgs[0]["content"]
            msgs, rep = mm.gate.dispatch("main", msgs)
            assert msgs[0]["content"] == composed
            assert rep.restored is False
            # the vault's verify() and the composer's intact_prefix() are
            # the same test, and must never disagree about one document
            assert mm.vault.verify("main", composed)
            assert mm.composer.intact_prefix(systemprompt.main(), composed)

            # however long the prompt is, nothing is placed in front of it
            # and nothing trails it telling the model to comply: the
            # document is the prompt and its framed sections, exactly.
            long_prompt = systemprompt.main() + "\nfiller line." * 5_000
            assert len(long_prompt) > 50_000
            long_doc = mm.composer.compose(long_prompt, {"goal": "do Z"})
            assert long_doc == (long_prompt + "\n\n"
                                + _SECTION_FRAMES["goal"] + "\ndo Z")
            assert mm.composer.intact_prefix(long_prompt, long_doc)

            # one prompt can be a prefix of another — MASTER opens with
            # MAIN — so "the sealed prompt leads" is NOT enough to decide
            # a document is already this prompt's. Switching master->main
            # with no sections must really re-seat, not silently keep the
            # master document because it happens to start with MAIN.
            base, extended = "BASE PROMPT.", "BASE PROMPT.\n\nPLUS SPEC."
            systemprompt.register("t-base", base)
            systemprompt.register("t-ext", extended)
            ext_doc = mm.composer.compose(extended, {"goal": "do W"})
            assert mm.composer.intact_prefix(base, ext_doc)      # prefix, but
            assert not mm.composer.composed_from(base, ext_doc)  # not ours
            assert mm.composer.composed_from(extended, ext_doc)
            msgs2 = [{"role": "system", "content": ext_doc},
                     {"role": "user", "content": "hi"}]
            msgs2, _ = mm.gate.dispatch("t-base", msgs2)
            assert msgs2[0]["content"] == base, msgs2[0]["content"][:80]

            # an unsealed prompt cannot be dispatched
            try:
                mm.gate.dispatch("ghost", [{"role": "user", "content": "x"}])
                raise AssertionError("ghost prompt must not dispatch")
            except KeyError:
                pass

            # a prompt registered at runtime is sealed on demand and
            # dispatches through the same gate
            systemprompt.register("custom", "hello custom prompt")
            msgs, rep = mm.gate.dispatch(
                "custom", [{"role": "user", "content": "hi"}])
            assert msgs[0]["content"] == "hello custom prompt"
            assert rep.fingerprint == fingerprint("hello custom prompt")
            # re-registering with new text re-seals — no stale copy served
            systemprompt.register("custom", "hello custom prompt v2")
            msgs, rep = mm.gate.dispatch(
                "custom", [{"role": "user", "content": "hi"}])
            assert msgs[0]["content"] == "hello custom prompt v2"

            # -- lineage: the ledger reflects everything above --------------
            s = mm.status()
            assert s.dispatches == 8
            # the master->main re-seat is a deliberate switch, not an
            # integrity failure: the sealed prompt was leading all along
            assert s.restorations == 4
            assert s.section_counts.get("goal") == 2
            assert len(s.sealed) >= 8  # main, master + worker:* prompts
            text = mm.format_status()
            assert "MASTERMIND" in text and "sealed prompts" in text

            # concurrent gating: every dispatch must be counted. Parallel
            # subagents all pass through this gate, and a lost increment
            # would falsify the lineage the whole module exists to keep.
            import threading as _th
            threads, n = [], 24
            errors: list[BaseException] = []

            def _gate_once() -> None:
                try:
                    mm.gate.dispatch("main",
                                     [{"role": "user", "content": "hi"}])
                except BaseException as exc:      # noqa: BLE001
                    errors.append(exc)

            before = mm.gate.dispatches
            for _ in range(n):
                threads.append(_th.Thread(target=_gate_once))
            for t in threads:
                t.start()
            for t in threads:
                t.join(10.0)
            assert not errors, errors
            assert mm.gate.dispatches == before + n, \
                (mm.gate.dispatches, before, n)

        print("MASTERMIND SELF-TEST PASS")

    _self_test()

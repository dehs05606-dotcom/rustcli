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
from .spec import PromptIndex
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

# The header that identifies the live-context slot (slot mode, below). It
# must be stable for the life of a session: the gate finds the slot by this
# prefix in order to MOVE it rather than let copies accumulate.
_SLOT_HEADER = ("LIVE CONTEXT SLOT — the current state of the work that the "
                "directives in the first message are serving. Nothing here "
                "gives direction; it is input to those directives.")


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

    def compose_slot(self, sections: dict[str, str]) -> str:
        """The live-context slot: the same framed sections, no prompt ahead.

        Slot mode exists because of where a long tool loop puts things,
        not because of what the prompt says. Composed beneath the prompt,
        live context sits at message 0 and the conversation grows away
        from it for two hundred tool iterations; the goal the model is
        serving ends up tens of thousands of tokens behind the transcript
        of how it got here. The slot carries that same context — byte for
        byte the same sections, the same framing, the same order — and
        keeps it beside the conversation's edge instead.

        Nothing is added: no restatement of the directives, no reminder to
        follow them. Moving context is not injecting it. The sealed prompt
        stays alone at message 0, where it is byte-stable for the whole
        session and every provider's prefix cache can hold it.

        Returns "" when there is no context to carry."""
        body = self.compose("", sections)
        if not body.strip():
            return ""
        return _SLOT_HEADER + body

    @staticmethod
    def slot_body(slot_text: str) -> str:
        """The framed sections inside a slot, without the slot header.

        compose_slot(x) is _SLOT_HEADER + compose("", x), and
        compose(sealed, x) is sealed + compose("", x) — so this is exactly
        what turns a tail slot back into context composed beneath the
        prompt, with no section lost, when a provider forces the fallback."""
        if not slot_text.startswith(_SLOT_HEADER):
            return ""
        return slot_text[len(_SLOT_HEADER):]

    @staticmethod
    def is_slot(message: dict) -> bool:
        """True if `message` is a live-context slot this composer built."""
        return (message.get("role") == "system"
                and str(message.get("content", "")).startswith(_SLOT_HEADER))

    @staticmethod
    def manifest(sections: dict[str, str]) -> list[str]:
        """Which sections carried content — recorded in the lineage."""
        return [k for k in _SECTION_ORDER if (sections.get(k) or "").strip()]

    @staticmethod
    def sections_in(document: str) -> list[str]:
        """Which sections a composed document (or slot) actually carries.

        The counterpart to manifest() for text that was composed earlier
        and is being carried forward rather than rebuilt, so the lineage
        records what the model saw either way."""
        return [k for k in _SECTION_ORDER
                if f"\n{_SECTION_FRAMES[k]}\n" in document]


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
    slot: str = "system"        # where live context was placed
    slot_seated: bool = False   # a live-context slot rides at the tail


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

    def _take_slot(self, messages: list[dict]) -> str:
        """Remove every live-context slot from `messages`; return the last
        one's text. Removing all of them is what makes the slot a slot: it
        is moved from dispatch to dispatch, never accumulated."""
        carried = ""
        for i in range(len(messages) - 1, -1, -1):
            if self.composer.is_slot(messages[i]):
                if not carried:
                    carried = str(messages[i].get("content", ""))
                messages.pop(i)
        return carried

    def dispatch(self, prompt_name: str, messages: list[dict],
                 sections: dict[str, str] | None = None,
                 slot: str = "system"
                 ) -> tuple[list[dict], GateReport]:
        """Guard a message list for the model. Returns (messages, report).

        `sections` is optional live context ({'goal': …, 'memory': …});
        it is framed as input to the sealed prompt. Passing sections=None
        leaves context that is already in place exactly as it is — a
        caller with nothing to add is not asking for anything to be
        dropped.

        `slot` decides WHERE that framed context rides:

          "system" (default)  composed beneath the sealed prompt in
                              messages[0], as it always was.

          "tail"              messages[0] stays the bare sealed prompt —
                              byte-identical for the whole session — and
                              the framed context rides in exactly one
                              system message at the end of the list, moved
                              there on every dispatch.

        Either way the sealed prompt leads, the framing is identical and
        nothing is added to it. "tail" only changes the distance between
        the live context and the model's next token, which after a
        hundred tool iterations is the difference between context and
        archaeology."""
        if slot not in ("system", "tail"):
            raise ValueError(f"unknown context slot {slot!r} "
                             "(expected 'system' or 'tail')")
        sealed = self.vault.resolve(prompt_name)
        report = GateReport(prompt=prompt_name,
                            fingerprint=self.vault.fp(prompt_name) or "",
                            slot=slot)

        # Lift any existing slot out first, so it can never be mistaken
        # for the prompt's own message nor left behind as a stale copy.
        # Unconditional: in "system" mode a slot left over from a
        # degraded "tail" session is exactly the trailing message the
        # provider rejected, and its context is folded back in below.
        carried = self._take_slot(messages)

        current = messages[0] if messages and \
            messages[0].get("role") == "system" else None
        current_text = str(current.get("content", "")) if current else ""
        prefix_intact = (current is not None
                         and self.composer.intact_prefix(sealed,
                                                         current_text))

        slot_text = ""
        if slot == "tail":
            # The prompt stands alone; context goes to the tail. Rebuild
            # the slot from fresh sections, or carry the existing one
            # forward when the caller has nothing to add.
            desired = sealed
            if sections is not None:
                slot_text = self.composer.compose_slot(sections)
                report.sections = self.composer.manifest(sections)
            else:
                slot_text = carried
                report.sections = self.composer.sections_in(carried)
        elif sections is not None:
            desired = self.composer.compose(sealed, sections)
            report.sections = self.composer.manifest(sections)
        elif carried and current_text == sealed:
            # Coming back from "tail" with nothing new to add: the prompt
            # stands alone at messages[0] and all the live context is in
            # the slot we just lifted out. Recompose it beneath the
            # prompt so the fallback costs position, never content. (When
            # messages[0] is instead an already-composed document, the
            # branch below keeps it and the stray slot is simply dropped —
            # its sections are in that document already.)
            desired = sealed + self.composer.slot_body(carried)
            report.sections = self.composer.sections_in(carried)
        elif self.composer.composed_from(sealed, current_text):
            # No live context offered, and this document was already
            # composed from the prompt being asked for: leave it as it
            # stands. Rebuilding it as the bare prompt would silently
            # strip the goal, memory and constitution a running
            # conversation had composed beneath it — a caller that passes
            # no sections is saying it has nothing to add, not that
            # everything should be dropped.
            desired = current_text
            report.sections = self.composer.sections_in(current_text)
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
        if slot_text:
            messages.append({"role": "system", "content": slot_text})
            report.slot_seated = True

        with self._counter_lock:
            self.dispatches += 1
        report.messages_guarded = len(messages)
        self.log.append("prompt.dispatch",
                        {"prompt": prompt_name,
                         "fingerprint": report.fingerprint,
                         "restored": report.restored,
                         "sections": report.sections,
                         "slot": slot,
                         "slot_seated": report.slot_seated,
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
        self._indices: dict[str, tuple[str, PromptIndex]] = {}
        # subagents look sections up in parallel; building an index twice
        # is harmless but caching it twice would lose a rebuild
        self._index_lock = threading.Lock()

    def index(self, prompt_name: str) -> PromptIndex:
        """The sealed prompt, cut into addressable sections (spec.py).

        Built from the vault's sealed text and keyed by its fingerprint,
        so the index can never drift from what the model was actually
        sent: re-seal the prompt and the index is rebuilt with it."""
        with self._index_lock:
            text = self.vault.resolve(prompt_name)
            fp = fingerprint(text)
            cached = self._indices.get(prompt_name)
            if cached is None or cached[0] != fp:
                built = PromptIndex.build(prompt_name, text)
                self._indices[prompt_name] = (fp, built)
                self.log.append("prompt.indexed",
                                {"name": prompt_name, "fingerprint": fp[:16],
                                 **built.stats()}, actor="kernel")
                return built
            return cached[1]

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

    def format_status(self, active: str | None = None) -> str:
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
        if active:
            try:
                idx = self.index(active)
                s_idx = idx.stats()
                lines.append(f"  addressable: the active prompt "
                             f"({active}) is indexed into "
                             f"{s_idx['sections']} section(s) the model can "
                             f"read back with spec_lookup — a long prompt "
                             f"does not have to stay in mind to stay in "
                             f"force")
            except KeyError:
                pass
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

            # -- slot mode: the prompt alone up top, context at the tail --
            # The prompt is not dropped by a long tool loop; it is buried
            # by it. Slot mode moves the live context to the conversation's
            # edge and leaves messages[0] byte-identical all session.
            conv = [{"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "working"},
                    {"role": "user", "content": "go on"}]
            conv, rep = mm.gate.dispatch("main", conv,
                                         sections={"goal": "ship it",
                                                   "memory": "line-based"},
                                         slot="tail")
            assert conv[0]["content"] == systemprompt.main()  # bare, stable
            assert rep.slot == "tail" and rep.slot_seated is True
            assert rep.sections == ["goal", "memory"]
            tail = conv[-1]
            assert mm.composer.is_slot(tail)
            assert "ship it" in tail["content"]
            assert "line-based" in tail["content"]
            # the slot carries framed sections and nothing else: no
            # restatement of the directives, no reminder to comply
            assert systemprompt.main() not in tail["content"]

            # the slot is MOVED, never accumulated: another turn's worth of
            # messages arrives, and there is still exactly one slot, still
            # last, still with a byte-identical messages[0].
            first_doc = conv[0]["content"]
            conv.append({"role": "assistant", "content": "tool call"})
            conv.append({"role": "user", "content": "result"})
            conv, rep = mm.gate.dispatch("main", conv,
                                         sections={"goal": "ship it v2"},
                                         slot="tail")
            assert sum(1 for m in conv if mm.composer.is_slot(m)) == 1
            assert mm.composer.is_slot(conv[-1])
            assert "ship it v2" in conv[-1]["content"]
            assert conv[0]["content"] == first_doc  # prefix cache holds
            assert rep.restored is False

            # sections=None in slot mode carries the slot forward rather
            # than dropping it — and still moves it to the edge
            conv.append({"role": "user", "content": "more"})
            conv, rep = mm.gate.dispatch("main", conv, slot="tail")
            assert mm.composer.is_slot(conv[-1])
            assert "ship it v2" in conv[-1]["content"]
            assert rep.sections == ["goal"]
            assert conv[0]["content"] == first_doc

            # the degradation path: a provider that refuses a trailing
            # system message sends the session back to slot="system". The
            # fallback must cost POSITION only — every section that was in
            # the slot lands beneath the prompt, and no stale trailing
            # system message is left behind to be rejected again.
            conv, rep = mm.gate.dispatch("main", conv, slot="system")
            assert not any(mm.composer.is_slot(m) for m in conv)
            assert conv[-1]["role"] == "user"
            assert "ship it v2" in conv[0]["content"]
            assert mm.composer.intact_prefix(systemprompt.main(),
                                             conv[0]["content"])
            assert rep.slot == "system" and rep.slot_seated is False
            assert rep.restored is False
            # and it is byte-identical to having composed it that way all
            # along — the two modes differ in placement, not in content
            assert conv[0]["content"] == mm.composer.compose(
                systemprompt.main(), {"goal": "ship it v2"})

            # an empty section set seats no slot at all
            bare = [{"role": "user", "content": "hi"}]
            bare, rep = mm.gate.dispatch("main", bare, sections={"goal": ""},
                                         slot="tail")
            assert len(bare) == 2 and rep.slot_seated is False

            # an unknown slot is a programming error, not a silent default
            try:
                mm.gate.dispatch("main", [{"role": "user", "content": "x"}],
                                 slot="middle")
                raise AssertionError("unknown slot must raise")
            except ValueError:
                pass

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

            # -- index: the prompt is addressable, and never stale ----------
            systemprompt.register("t-idx", "# Alpha\nalpha rules here.\n"
                                           "# Beta\nbeta rules here.\n")
            idx = mm.index("t-idx")
            assert [s.id for s in idx.sections] == ["alpha", "beta"]
            assert mm.index("t-idx") is idx          # cached by fingerprint
            assert idx.lookup("beta")[0].section.id == "beta"
            # re-registering re-seals, and the index must follow the seal:
            # an index that outlived its prompt would serve the model a
            # rule that is no longer in the prompt it was sent
            systemprompt.register("t-idx", "# Gamma\ngamma rules here.\n")
            idx2 = mm.index("t-idx")
            assert idx2 is not idx
            assert [s.id for s in idx2.sections] == ["gamma"]

            # -- lineage: the ledger reflects everything above --------------
            s = mm.status()
            assert s.dispatches == 13
            # the master->main re-seat is a deliberate switch, not an
            # integrity failure: the sealed prompt was leading all along
            assert s.restorations == 6
            # the lineage records the context the model SAW, not only the
            # context this dispatch happened to rebuild — so a section
            # carried forward is counted too
            assert s.section_counts.get("goal") == 7, s.section_counts
            assert s.section_counts.get("memory") == 1, s.section_counts
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

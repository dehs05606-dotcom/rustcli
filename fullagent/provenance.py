"""PROVENANCE — why anything happened, as a signed, queryable graph.

The event log already records everything: which policy stage refused a
call, which step of which plan ran, what the recovery playbook decided.
It records them as a flat sequence, which answers "what happened" and
not "what happened *because of* what". Reconstructing a causal chain
from a flat log means knowing which event types relate to which, which
is knowledge that lives in people's heads and leaves with them.

So this module derives a graph. Nodes are decisions and actions; edges
are the causal relations between them:

    governed-by   this call ran under that policy verdict
    caused-by     this outcome came from that attempt
    part-of       this step belongs to that plan
    decided-by    this recovery strategy came from that failure
    undid         this rollback took back that step

It **derives** rather than records. Nothing here writes a second copy of
the truth; the log stays the only source, and a graph that disagreed
with it would be a bug in this file rather than a second opinion. The
cost is that the graph can only be as complete as what was sealed, and
`Graph.gaps()` says where the derivation could not find a cause rather
than inventing one.

Each node is content-addressed and HMAC-signed with the constitution's
key, so an exported graph can be checked by someone who was not there.
As with the event log, this is tamper-**evident**: anyone who owns the
machine can rewrite both, but not without `verify()` failing.

    graph = build(log)
    graph.why(node_id)          # the causal chain back to a root
    graph.denials()             # every refusal, with what refused it
    graph.explain(node_id)      # the same chain, in sentences
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field

# -- node kinds -------------------------------------------------------------
N_POLICY = "policy"          # one permission verdict
N_CALL = "call"              # one dispatched tool call
N_PLAN = "plan"              # an orchestration's plan review
N_STEP = "step"              # one step's attempt
N_OUTCOME = "outcome"        # that step's result
N_RECOVERY = "recovery"      # what the playbook decided about a failure
N_ROLLBACK = "rollback"      # one compensation
N_ROUTING = "routing"        # a model routing proposal or acceptance

KINDS = (N_POLICY, N_CALL, N_PLAN, N_STEP, N_OUTCOME, N_RECOVERY,
         N_ROLLBACK, N_ROUTING)

# -- edge kinds -------------------------------------------------------------
GOVERNED_BY = "governed-by"
CAUSED_BY = "caused-by"
PART_OF = "part-of"
DECIDED_BY = "decided-by"
UNDID = "undid"

RELATIONS = (GOVERNED_BY, CAUSED_BY, PART_OF, DECIDED_BY, UNDID)

# Event types this module knows how to read. Anything else in the log is
# left alone rather than guessed at.
SOURCE_EVENTS = ("policy.decision", "dispatch.call", "orchestrator.plan",
                 "orchestrator.step", "orchestrator.step.done",
                 "orchestrator.rollback", "orchestrator.refused",
                 "orchestrator.done", "telemetry.routing.proposed",
                 "telemetry.routing.accepted")


def _hash(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


@dataclass(frozen=True)
class Node:
    """One decision or action, addressed by its own content."""
    id: str
    kind: str
    seq: int
    at: float
    subject: str              # the tool, step path or model it is about
    trace_id: str = ""
    summary: str = ""
    data: dict = field(default_factory=dict)
    signature: str = ""

    def content_hash(self) -> str:
        return _hash({"kind": self.kind, "seq": self.seq,
                      "subject": self.subject, "trace_id": self.trace_id,
                      "summary": self.summary, "data": self.data})

    def sign(self, key: bytes) -> str:
        return hmac.new(key, self.content_hash().encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def verify(self, key: bytes) -> bool:
        if not self.signature:
            return False
        return hmac.compare_digest(self.sign(key), self.signature)

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "seq": self.seq,
                "at": self.at, "subject": self.subject,
                "trace_id": self.trace_id, "summary": self.summary,
                "data": self.data, "signature": self.signature}


@dataclass(frozen=True)
class Edge:
    source: str
    relation: str
    target: str

    def to_dict(self) -> dict:
        return {"source": self.source, "relation": self.relation,
                "target": self.target}


@dataclass(frozen=True)
class Gap:
    """Somewhere the derivation could not find a cause."""
    node: str
    wanted: str
    detail: str

    def to_dict(self) -> dict:
        return {"node": self.node, "wanted": self.wanted,
                "detail": self.detail}


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: tuple[Edge, ...] = ()
    gaps: tuple[Gap, ...] = ()
    signed: bool = False

    # -- structure ---------------------------------------------------------

    def order(self) -> tuple[Node, ...]:
        return tuple(sorted(self.nodes.values(), key=lambda n: n.seq))

    def of_kind(self, kind: str) -> tuple[Node, ...]:
        return tuple(n for n in self.order() if n.kind == kind)

    def by_trace(self, trace_id: str) -> tuple[Node, ...]:
        return tuple(n for n in self.order() if n.trace_id == trace_id)

    def by_subject(self, subject: str) -> tuple[Node, ...]:
        return tuple(n for n in self.order() if n.subject == subject)

    def out_edges(self, node_id: str) -> tuple[Edge, ...]:
        return tuple(e for e in self.edges if e.source == node_id)

    def in_edges(self, node_id: str) -> tuple[Edge, ...]:
        return tuple(e for e in self.edges if e.target == node_id)

    # -- queries -----------------------------------------------------------

    def why(self, node_id: str) -> tuple[Node, ...]:
        """The causal chain behind a node, nearest cause first.

        Breadth-first over outgoing edges, which all point at causes. A
        cycle would mean a node caused itself; `seen` makes that
        terminate rather than hang, because a graph derived from a log
        should never have one and hanging is a bad way to find out.
        """
        seen: set[str] = {node_id}
        queue = [node_id]
        chain: list[Node] = []
        while queue:
            current = queue.pop(0)
            for edge in self.out_edges(current):
                if edge.target in seen:
                    continue
                seen.add(edge.target)
                target = self.nodes.get(edge.target)
                if target is not None:
                    chain.append(target)
                    queue.append(edge.target)
        return tuple(chain)

    def denials(self) -> tuple[Node, ...]:
        return tuple(n for n in self.of_kind(N_POLICY)
                     if n.data.get("outcome") == "deny")

    def failures(self) -> tuple[Node, ...]:
        return tuple(n for n in self.of_kind(N_OUTCOME)
                     if n.data.get("status") in ("failed", "escalated"))

    def explain(self, node_id: str) -> str:
        """The chain as sentences, for a person reading a post-mortem."""
        node = self.nodes.get(node_id)
        if node is None:
            return f"no node {node_id}"
        lines = [f"{node.kind}: {node.summary}"]
        relation_of = {e.target: e.relation for e in self.edges
                       if e.source == node_id}
        for cause in self.why(node_id):
            how = relation_of.get(cause.id, "because")
            lines.append(f"  {how} {cause.kind}: {cause.summary}")
            relation_of.update({e.target: e.relation for e in self.edges
                                if e.source == cause.id})
        return "\n".join(lines)

    # -- integrity ---------------------------------------------------------

    def verify(self, key: bytes) -> tuple[bool, tuple[str, ...]]:
        """Re-sign every node and report the ones that do not match."""
        if not self.signed:
            return False, ("this graph was never signed",)
        bad = tuple(n.id for n in self.order() if not n.verify(key))
        return (not bad), bad

    def to_dict(self) -> dict:
        return {"nodes": [n.to_dict() for n in self.order()],
                "edges": [e.to_dict() for e in self.edges],
                "gaps": [g.to_dict() for g in self.gaps],
                "signed": self.signed}

    def format(self) -> str:
        counts: dict[str, int] = {}
        for n in self.nodes.values():
            counts[n.kind] = counts.get(n.kind, 0) + 1
        head = (f"PROVENANCE — {len(self.nodes)} node(s), "
                f"{len(self.edges)} edge(s)"
                + (", signed" if self.signed else ", unsigned"))
        lines = [head, "  " + ", ".join(f"{k} {v}"
                                        for k, v in sorted(counts.items()))]
        if self.gaps:
            lines.append(f"  {len(self.gaps)} gap(s) where no cause was "
                         f"sealed")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------

def _node(kind: str, seq: int, at: float, subject: str, trace_id: str,
          summary: str, data: dict) -> Node:
    ident = _hash({"kind": kind, "seq": seq, "subject": subject})
    return Node(ident, kind, seq, at, subject, trace_id, summary, data)


def build(log, key: bytes | None = None) -> Graph:
    """Derive the provenance graph from an event log."""
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []
    gaps: list[Gap] = []

    # The most recent policy verdict per tool, so a call can be linked to
    # the verdict that let it through. Policy only seals non-allow
    # decisions, so an allowed call legitimately has no verdict node --
    # that is recorded as a gap, not invented.
    last_policy: dict[str, str] = {}
    step_nodes: dict[tuple[str, str], str] = {}     # (trace, path) -> node
    plan_nodes: dict[str, str] = {}                 # trace -> plan node
    outcome_nodes: dict[tuple[str, str], str] = {}

    def add(node: Node) -> str:
        nodes[node.id] = node
        return node.id

    for ev in log.events():
        if ev.type not in SOURCE_EVENTS:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        seq = int(getattr(ev, "seq", 0) or 0)
        at = float(getattr(ev, "ts", 0) or 0)
        trace = str(data.get("trace_id") or "")

        if ev.type == "policy.decision":
            tool = str(data.get("tool") or "?")
            nid = add(_node(
                N_POLICY, seq, at, tool, trace,
                f"{data.get('outcome', '?')} {tool} at "
                f"{data.get('rule', '?')}: {data.get('reason', '')}", data))
            last_policy[tool] = nid

        elif ev.type == "dispatch.call":
            tool = str(data.get("tool") or "?")
            nid = add(_node(
                N_CALL, seq, at, tool, str(data.get("trace_id") or ""),
                f"{'ok' if data.get('ok') else 'failed'} {tool}"
                + (f": {(data.get('error') or {}).get('code', '')}"
                   if not data.get("ok") else ""), data))
            verdict = last_policy.get(tool)
            if verdict is not None:
                edges.append(Edge(nid, GOVERNED_BY, verdict))
            elif not data.get("ok"):
                gaps.append(Gap(nid, GOVERNED_BY,
                                f"no policy verdict was sealed for {tool}"))

        elif ev.type == "orchestrator.plan":
            nid = add(_node(N_PLAN, seq, at, str(data.get("goal") or "?"),
                            trace,
                            f"{'accepted' if data.get('ok') else 'refused'}: "
                            f"{data.get('goal', '')}", data))
            plan_nodes[trace] = nid

        elif ev.type == "orchestrator.step":
            path = str(data.get("path") or data.get("step") or "?")
            nid = add(_node(N_STEP, seq, at, path, trace,
                            f"running {path}"
                            + (f" ({data.get('tool')})"
                               if data.get("tool") else " (saga)"), data))
            step_nodes[(trace, path)] = nid
            plan = plan_nodes.get(trace)
            if plan is not None:
                edges.append(Edge(nid, PART_OF, plan))
            else:
                gaps.append(Gap(nid, PART_OF,
                                "no plan was sealed for this trace"))

        elif ev.type == "orchestrator.step.done":
            path = str(data.get("path") or data.get("step") or "?")
            nid = add(_node(N_OUTCOME, seq, at, path, trace,
                            f"{data.get('status', '?')} {path}"
                            + (f": {data.get('detail')}"
                               if data.get("detail") else ""), data))
            outcome_nodes[(trace, path)] = nid
            attempt = step_nodes.get((trace, path))
            if attempt is not None:
                edges.append(Edge(nid, CAUSED_BY, attempt))
            else:
                gaps.append(Gap(nid, CAUSED_BY,
                                "no attempt was sealed for this step"))
            if data.get("recovery"):
                rid = add(_node(
                    N_RECOVERY, seq, at, path, trace,
                    f"{data.get('recovery')} after "
                    f"{data.get('error_code') or 'a failed check'}",
                    {"strategy": data.get("recovery"),
                     "error_code": data.get("error_code", ""),
                     "step": path}))
                edges.append(Edge(rid, DECIDED_BY, nid))

        elif ev.type == "orchestrator.rollback":
            path = str(data.get("path") or data.get("step") or "?")
            nid = add(_node(N_ROLLBACK, seq, at, path, trace,
                            f"{'undid' if data.get('ok') else 'failed to undo'}"
                            f" {path} with {data.get('tool', '?')}", data))
            target = outcome_nodes.get((trace, path)) or \
                step_nodes.get((trace, path))
            if target is not None:
                edges.append(Edge(nid, UNDID, target))
            else:
                gaps.append(Gap(nid, UNDID,
                                "nothing sealed for the step it undid"))

        elif ev.type == "orchestrator.refused":
            plan = plan_nodes.get(trace)
            nid = add(_node(N_OUTCOME, seq, at,
                            str(data.get("goal") or "?"), trace,
                            f"refused: {data.get('reason', '')}", data))
            if plan is not None:
                edges.append(Edge(nid, CAUSED_BY, plan))

        elif ev.type in ("telemetry.routing.proposed",
                         "telemetry.routing.accepted"):
            model = str(data.get("recommended") or "?")
            accepted = ev.type.endswith("accepted")
            add(_node(N_ROUTING, seq, at, model, trace,
                      (f"accepted {model} "
                       f"(by {data.get('accepted_by', '?')})" if accepted
                       else f"proposed {model} "
                            f"(in effect: {data.get('in_effect', '?')})"),
                      data))

    graph = Graph(nodes, tuple(edges), tuple(gaps))
    if key is not None:
        graph.nodes = {nid: Node(**{**n.to_dict(),
                                    "signature": n.sign(key)})
                       for nid, n in graph.nodes.items()}
        graph.signed = True
    return graph


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import shutil
    import tempfile
    from pathlib import Path

    from .dispatch import Dispatcher
    from .kernel import EventLog
    from .orchestrator import Expectation, Orchestrator, Plan, Step
    from .toolcontract import build_contracts
    from .toolpolicy import ToolPolicy
    from .tools import build_registry

    work = Path(tempfile.mkdtemp(prefix="fa-prov-"))
    here = os.getcwd()
    os.chdir(work)
    try:
        log = EventLog(path=str(work / "events.jsonl"))
        registry = build_registry()
        contracts = build_contracts(registry)

        readonly = Dispatcher(policy=ToolPolicy("readonly", log=log,
                                                roots=(str(work),)),
                              log=log, approve=lambda c, a: True)
        readonly.register_registry(registry, contracts)
        # a refusal, so there is a denial to trace
        blocked = readonly.call("write_file", {"path": str(work / "x.txt"),
                                               "content": "x"})
        assert not blocked.ok

        d = Dispatcher(policy=ToolPolicy("developer", log=log,
                                         roots=(str(work),)),
                       log=log, approve=lambda c, a: True)
        d.register_registry(registry, contracts)
        orch = Orchestrator(d, log=log, approve=lambda p, r: True)

        def w(step_id, name, expect=None):
            path = str(work / name)
            return Step(step_id, "write_file",
                        {"path": path, "content": "x\n"},
                        expect=(Expectation(path_exists=(path,))
                                if expect is None
                                else Expectation(contains=(expect,))),
                        undo_tool="delete_path", undo_args={"path": path})

        good = orch.run(Plan("a clean run", (w("one", "one.txt"),)))
        assert good.ok, good.format()
        bad = orch.run(Plan("a run that fails", (
            w("one", "two.txt"), w("two", "three.txt", expect="never"))))
        assert not bad.ok

        # --- the graph derives, it does not duplicate ------------------
        graph = build(log)
        assert graph.nodes and graph.edges
        for kind in (N_POLICY, N_CALL, N_PLAN, N_STEP, N_OUTCOME,
                     N_ROLLBACK):
            assert graph.of_kind(kind), f"no {kind} nodes were derived"

        # node ids are content-addressed and stable across two builds
        again = build(log)
        assert set(again.nodes) == set(graph.nodes), \
            "the derivation is not deterministic"
        assert [n.summary for n in again.order()] == \
            [n.summary for n in graph.order()]

        # --- a denial is traceable to the stage that refused -----------
        denials = graph.denials()
        assert denials, "no denial was derived"
        refusal = denials[0]
        assert refusal.data.get("rule") == "capability", refusal.to_dict()
        assert "write_file" in refusal.summary

        # the refused call points at the verdict that governed it
        refused_calls = [n for n in graph.of_kind(N_CALL)
                         if not n.data.get("ok")
                         and n.subject == "write_file"]
        assert refused_calls, "no refused call node"
        governed = [e for e in graph.out_edges(refused_calls[0].id)
                    if e.relation == GOVERNED_BY]
        assert governed, graph.format()
        assert graph.nodes[governed[0].target].kind == N_POLICY

        # --- a failed step's chain reaches its plan --------------------
        failures = graph.failures()
        assert failures, "no failure was derived"
        chain = graph.why(failures[0].id)
        kinds = [n.kind for n in chain]
        assert N_STEP in kinds and N_PLAN in kinds, kinds
        story = graph.explain(failures[0].id)
        assert "part-of" in story and "caused-by" in story, story

        # --- a rollback points at what it undid ------------------------
        rollbacks = graph.of_kind(N_ROLLBACK)
        assert rollbacks
        undone = [e for e in graph.out_edges(rollbacks[0].id)
                  if e.relation == UNDID]
        assert undone, "a rollback with nothing to point at"

        # --- traces partition the graph --------------------------------
        assert {n.trace_id for n in graph.by_trace(good.trace_id)} == \
            {good.trace_id}
        assert graph.by_trace("0" * 16) == ()

        # --- why() terminates on a cycle -------------------------------
        cyclic = Graph(dict(graph.nodes),
                       graph.edges + (Edge(failures[0].id, CAUSED_BY,
                                           failures[0].id),))
        assert cyclic.why(failures[0].id) is not None

        # --- signing makes an exported graph checkable -----------------
        key = b"a test key, not the real one"
        signed = build(log, key=key)
        ok, bad_nodes = signed.verify(key)
        assert ok and not bad_nodes, bad_nodes
        assert not build(log).verify(key)[0], \
            "an unsigned graph must not claim to verify"
        assert not signed.verify(b"the wrong key")[0]

        # a doctored node fails verification
        victim = signed.order()[0]
        doctored = dict(signed.nodes)
        doctored[victim.id] = Node(**{**victim.to_dict(),
                                      "summary": "something else entirely"})
        tampered = Graph(doctored, signed.edges, signed.gaps, signed=True)
        ok, bad_nodes = tampered.verify(key)
        assert not ok and victim.id in bad_nodes

        # --- gaps are reported, never filled in ------------------------
        empty = EventLog(path=str(work / "empty.jsonl"))
        empty.append("orchestrator.step",
                     {"path": "orphan", "trace_id": "t", "tool": "read_file"},
                     actor="orchestrator")
        orphaned = build(empty)
        assert orphaned.gaps and orphaned.gaps[0].wanted == PART_OF, \
            [g.to_dict() for g in orphaned.gaps]

        print(graph.format())
        print(graph.explain(failures[0].id))
        print(f"PROVENANCE SELF-TEST PASS — {len(graph.nodes)} nodes, "
              f"{len(graph.edges)} edges, {len(graph.gaps)} gap(s)")
    finally:
        os.chdir(here)
        shutil.rmtree(work, ignore_errors=True)

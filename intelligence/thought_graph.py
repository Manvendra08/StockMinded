"""
Adaptive Graph of Thoughts (AGoT) - Core Engine
===============================================

A graph-based reasoning system for dynamic market analysis.

Key Concepts:
- ThoughtNode: A decision point or hypothesis with confidence score
- ThoughtEdge: Reasoning path between nodes with weight
- ThoughtGraph: Directed acyclic graph of reasoning paths
- Branch: Alternative hypothesis paths that can be explored
- Revision: Ability to update/retract previous thoughts

The AGoT engine enables:
1. Multi-hypothesis reasoning (e.g., "Is this TREND_UP or RANGE_HIGH_VOL?")
2. Adaptive branching based on evidence strength
3. Self-correction when new data contradicts previous conclusions
4. Confidence-weighted decision aggregation
5. Reasoning trace for audit/debugging
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable


class ThoughtStatus(str, Enum):
    """Status of a thought node in the reasoning graph."""
    ACTIVE = "ACTIVE"           # Currently being evaluated
    CONFIRMED = "CONFIRMED"     # Validated by evidence
    REVISED = "REVISED"         # Updated based on new information
    REJECTED = "REJECTED"       # Disproved by evidence
    BRANCHED = "BRANCHED"       # Spawned alternative hypothesis paths
    STALE = "STALE"             # Superseded by newer thoughts


@dataclass
class Evidence:
    """A piece of evidence supporting or contradicting a thought."""
    source: str                          # Where evidence came from
    value: Any                           # The evidence data
    supports: bool = True                # True=supports, False=contradicts
    weight: float = 1.0                  # Relative importance (0-1)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "value": self.value,
            "supports": self.supports,
            "weight": self.weight,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


@dataclass
class ThoughtNode:
    """
    A node in the thought graph representing a hypothesis or decision point.
    
    The confidence score is dynamically updated as evidence accumulates.
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    label: str = ""                      # Human-readable description
    thought_type: str = "hypothesis"     # hypothesis | decision | observation
    confidence: float = 0.5              # 0.0 (certain false) to 1.0 (certain true)
    status: ThoughtStatus = ThoughtStatus.ACTIVE
    evidence: list[Evidence] = field(default_factory=list)
    children: list[str] = field(default_factory=list)     # IDs of child nodes
    parents: list[str] = field(default_factory=list)      # IDs of parent nodes
    branch_id: str | None = None         # Which reasoning branch this belongs to
    revision_of: str | None = None       # ID of thought this revises
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict = field(default_factory=dict)
    _compute_time_ms: float = 0.0

    def add_evidence(self, ev: Evidence) -> None:
        """Add evidence and update confidence using Bayesian-like update."""
        self.evidence.append(ev)
        self._recalculate_confidence()
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def _recalculate_confidence(self) -> None:
        """
        Recalculate confidence from all evidence using weighted aggregation.
        
        Formula: confidence = base + sum(support * weight) - sum(contradict * weight)
        Clamped to [0.05, 0.95] to avoid absolute certainty.
        """
        if not self.evidence:
            self.confidence = 0.5
            return

        support_sum = 0.0
        contradict_sum = 0.0
        total_weight = 0.0

        for ev in self.evidence:
            total_weight += ev.weight
            if ev.supports:
                support_sum += ev.weight
            else:
                contradict_sum += ev.weight

        if total_weight == 0:
            self.confidence = 0.5
            return

        # Bayesian-like update: prior (0.5) + evidence influence
        net_evidence = (support_sum - contradict_sum) / total_weight
        # Weight evidence by total weight (more evidence = more influence)
        evidence_influence = min(total_weight / 5.0, 1.0)  # Saturates at 5 pieces
        new_conf = 0.5 + (net_evidence * 0.4 * evidence_influence)
        self.confidence = max(0.05, min(0.95, new_conf))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "thought_type": self.thought_type,
            "confidence": round(self.confidence, 3),
            "status": self.status.value,
            "evidence": [e.to_dict() for e in self.evidence],
            "children": self.children,
            "parents": self.parents,
            "branch_id": self.branch_id,
            "revision_of": self.revision_of,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
            "compute_time_ms": round(self._compute_time_ms, 2),
        }


@dataclass
class ThoughtEdge:
    """Connection between two thought nodes representing a reasoning step."""
    from_id: str
    to_id: str
    reasoning: str = ""                  # Description of the reasoning step
    weight: float = 1.0                  # Strength of the connection
    edge_type: str = "derivation"        # derivation | contradiction | revision | branch

    def to_dict(self) -> dict:
        return {
            "from_id": self.from_id,
            "to_id": self.to_id,
            "reasoning": self.reasoning,
            "weight": round(self.weight, 3),
            "edge_type": self.edge_type,
        }


class ThoughtGraph:
    """
    Adaptive Graph of Thoughts - Core reasoning engine.
    
    Manages a directed acyclic graph of thoughts with support for:
    - Multi-branch reasoning (explore multiple hypotheses)
    - Thought revision (update previous conclusions)
    - Confidence-weighted aggregation
    - Reasoning trace export
    
    Usage:
        graph = ThoughtGraph("market_analysis")
        
        # Add initial observation
        obs = graph.add_thought("observation", "NIFTY above 200 EMA", confidence=0.9)
        
        # Branch into hypotheses
        trend_up = graph.branch_from(obs, "TREND_UP hypothesis")
        range_high = graph.branch_from(obs, "RANGE_HIGH_VOL hypothesis")
        
        # Add evidence
        trend_up.add_evidence(Evidence("ADX", 28, supports=True, weight=1.5))
        
        # Select best hypothesis
        best = graph.select_best()
    """

    def __init__(self, name: str = "reasoning_graph", session_id: str | None = None):
        self.name = name
        self.session_id = session_id or uuid.uuid4().hex[:8]
        self.nodes: dict[str, ThoughtNode] = {}
        self.edges: list[ThoughtEdge] = []
        self.branches: dict[str, list[str]] = {}   # branch_id -> list of node IDs
        self.revision_chain: list[str] = []         # Track revision history
        self._created_at = datetime.now(timezone.utc).isoformat()
        self._selectors: dict[str, Callable] = {}   # Custom selection strategies

    def add_thought(
        self,
        thought_type: str = "hypothesis",
        label: str = "",
        confidence: float = 0.5,
        parent_id: str | None = None,
        branch_id: str | None = None,
        metadata: dict | None = None,
    ) -> ThoughtNode:
        """Add a new thought node to the graph."""
        node = ThoughtNode(
            label=label,
            thought_type=thought_type,
            confidence=confidence,
            branch_id=branch_id,
            metadata=metadata or {},
        )
        self.nodes[node.id] = node

        if parent_id and parent_id in self.nodes:
            node.parents.append(parent_id)
            self.nodes[parent_id].children.append(node.id)
            self.edges.append(ThoughtEdge(
                from_id=parent_id,
                to_id=node.id,
                reasoning=f"Derived: {label}",
                edge_type="derivation",
            ))

        if branch_id:
            if branch_id not in self.branches:
                self.branches[branch_id] = []
            self.branches[branch_id].append(node.id)

        return node

    def branch_from(
        self,
        parent: ThoughtNode,
        label: str,
        branch_id: str | None = None,
        confidence: float = 0.5,
    ) -> ThoughtNode:
        """
        Create a new branch (alternative hypothesis) from a parent thought.
        
        This is the core of AGoT's multi-path reasoning: when evidence is
        ambiguous, we branch into multiple interpretations and score each.
        """
        branch_id = branch_id or f"branch_{uuid.uuid4().hex[:6]}"
        child = self.add_thought(
            thought_type="hypothesis",
            label=label,
            confidence=confidence,
            parent_id=parent.id,
            branch_id=branch_id,
        )
        # Mark parent as having branched
        parent.status = ThoughtStatus.BRANCHED
        self.edges.append(ThoughtEdge(
            from_id=parent.id,
            to_id=child.id,
            reasoning=f"Branch: {label}",
            edge_type="branch",
        ))
        return child

    def revise(
        self,
        original_id: str,
        new_label: str,
        new_confidence: float = 0.5,
        reason: str = "",
    ) -> ThoughtNode:
        """
        Revise a previous thought based on new evidence.
        
        Creates a new node that supersedes the original, maintaining the
        reasoning trace for audit purposes.
        """
        if original_id not in self.nodes:
            raise ValueError(f"Cannot revise unknown node: {original_id}")

        original = self.nodes[original_id]
        original.status = ThoughtStatus.REVISED

        revision = self.add_thought(
            thought_type=original.thought_type,
            label=f"[REVISED] {new_label}",
            confidence=new_confidence,
            parent_id=original_id,
            metadata={"revision_reason": reason, "original_label": original.label},
        )
        revision.revision_of = original_id
        self.revision_chain.append(original_id)
        self.edges.append(ThoughtEdge(
            from_id=original_id,
            to_id=revision.id,
            reasoning=f"Revision: {reason}",
            edge_type="revision",
        ))
        return revision

    def add_evidence_to(
        self,
        node_id: str,
        source: str,
        value: Any,
        supports: bool = True,
        weight: float = 1.0,
    ) -> None:
        """Add evidence to a specific node and propagate."""
        if node_id not in self.nodes:
            raise ValueError(f"Unknown node: {node_id}")

        ev = Evidence(source=source, value=value, supports=supports, weight=weight)
        self.nodes[node_id].add_evidence(ev)

    def get_branch_nodes(self, branch_id: str) -> list[ThoughtNode]:
        """Get all nodes in a specific branch."""
        ids = self.branches.get(branch_id, [])
        return [self.nodes[nid] for nid in ids if nid in self.nodes]

    def select_best(
        self,
        branch_ids: list[str] | None = None,
        min_confidence: float = 0.3,
    ) -> ThoughtNode | None:
        """
        Select the best hypothesis from competing branches.
        
        Uses confidence-weighted scoring with a minimum threshold.
        Ties are broken by evidence count (more evidence = more reliable).
        """
        candidates = []
        if branch_ids:
            for bid in branch_ids:
                nodes = self.get_branch_nodes(bid)
                for n in nodes:
                    if n.status in (ThoughtStatus.ACTIVE, ThoughtStatus.CONFIRMED):
                        candidates.append(n)
        else:
            candidates = [
                n for n in self.nodes.values()
                if n.status in (ThoughtStatus.ACTIVE, ThoughtStatus.CONFIRMED)
                and n.confidence >= min_confidence
            ]

        if not candidates:
            return None

        # Sort by confidence desc, then evidence count desc
        candidates.sort(
            key=lambda n: (n.confidence, len(n.evidence)),
            reverse=True,
        )
        best = candidates[0]
        best.status = ThoughtStatus.CONFIRMED
        return best

    def select_top_k(
        self,
        k: int = 3,
        min_confidence: float = 0.3,
    ) -> list[ThoughtNode]:
        """Select top K hypotheses ranked by confidence."""
        candidates = [
            n for n in self.nodes.values()
            if n.status in (ThoughtStatus.ACTIVE, ThoughtStatus.CONFIRMED)
            and n.confidence >= min_confidence
        ]
        candidates.sort(
            key=lambda n: (n.confidence, len(n.evidence)),
            reverse=True,
        )
        return candidates[:k]

    def aggregate_confidence(self, node_ids: list[str] | None = None) -> float:
        """
        Aggregate confidence across multiple nodes (ensemble scoring).
        
        Uses weighted average where nodes with more evidence get higher weight.
        """
        if node_ids is None:
            node_ids = list(self.nodes.keys())

        if not node_ids:
            return 0.5

        weights = []
        confidences = []
        for nid in node_ids:
            if nid in self.nodes:
                n = self.nodes[nid]
                # Weight by evidence count + 1 (to avoid zero weight)
                w = len(n.evidence) + 1
                weights.append(w)
                confidences.append(n.confidence)

        if not weights:
            return 0.5

        total_weight = sum(weights)
        return sum(c * w for c, w in zip(confidences, weights)) / total_weight

    def get_reasoning_trace(self) -> dict:
        """Export the full reasoning trace for debugging/audit."""
        return {
            "name": self.name,
            "session_id": self.session_id,
            "created_at": self._created_at,
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "branch_count": len(self.branches),
            "revision_count": len(self.revision_chain),
            "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
            "edges": [e.to_dict() for e in self.edges],
            "branches": {bid: nids for bid, nids in self.branches.items()},
            "revision_chain": self.revision_chain,
            "aggregated_confidence": round(self.aggregate_confidence(), 3),
            "best_hypothesis": (
                self.select_best().to_dict() if self.select_best() else None
            ),
        }

    def summary(self) -> str:
        """Human-readable summary of the thought graph."""
        lines = [
            f"ThoughtGraph: {self.name} (session: {self.session_id})",
            f"  Nodes: {len(self.nodes)} | Edges: {len(self.edges)} | Branches: {len(self.branches)}",
        ]
        for nid, n in self.nodes.items():
            status_icon = {
                ThoughtStatus.ACTIVE: "○",
                ThoughtStatus.CONFIRMED: "●",
                ThoughtStatus.REVISED: "↻",
                ThoughtStatus.REJECTED: "✗",
                ThoughtStatus.BRANCHED: "⑂",
                ThoughtStatus.STALE: "⊘",
            }.get(n.status, "?")
            lines.append(
                f"  {status_icon} [{n.confidence:.2f}] {n.label}"
                + (f" (rev of {n.revision_of[:6]})" if n.revision_of else "")
            )
        return "\n".join(lines)


# --- Convenience Functions ---

def create_regime_graph() -> ThoughtGraph:
    """Factory: create a thought graph pre-configured for regime classification."""
    graph = ThoughtGraph("regime_classification")
    return graph


def create_signal_graph() -> ThoughtGraph:
    """Factory: create a thought graph for signal ensemble analysis."""
    graph = ThoughtGraph("signal_ensemble")
    return graph

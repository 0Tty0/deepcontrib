"""Deterministic graph used to verify checkpoint recovery across processes."""

from __future__ import annotations

from typing import Any, NotRequired

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from typing_extensions import TypedDict


class CheckpointProbeState(TypedDict):
    """Small state shape whose continuation is safe to inspect in a test."""

    status: str
    approval: NotRequired[str]


def _wait_for_approval(state: CheckpointProbeState) -> dict[str, str]:
    decision = interrupt({"type": "checkpoint_probe", "message": "approve"})
    approved = decision.get("approved") if isinstance(decision, dict) else decision
    return {"status": "resumed", "approval": str(approved)}


def build_checkpoint_probe(checkpointer: Any) -> Any:
    """Build a one-node graph that pauses and resumes on one stable thread."""
    graph = StateGraph(CheckpointProbeState)
    graph.add_node("wait_for_approval", _wait_for_approval)
    graph.add_edge(START, "wait_for_approval")
    graph.add_edge("wait_for_approval", END)
    return graph.compile(checkpointer=checkpointer)

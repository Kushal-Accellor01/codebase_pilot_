"""B2: the upgrade graph. One LangGraph thread per upgrade run (thread_id = run_id), checkpointed in SQLite.

    prepare -> impact -> telemetry -> plan -> fix <-> validate -> approval -> act -> finish
                                         \\-> finish (report only)

The approval node pauses the thread with interrupt(); a decision resumes it, even from another process,
because the checkpoint file holds the state.
"""
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.core import events
from app.core.contracts import UpgradeState
from app.orchestrator import nodes

NodeFn = Callable[[nodes.RunContext, UpgradeState], Awaitable[dict]]
NODE_NAMES = ("prepare", "impact", "telemetry", "plan", "fix", "validate", "act", "finish")


def after_plan(state: UpgradeState) -> str:
    return "finish" if state.get("route") == "report_only" else "fix"


def after_validate(state: UpgradeState) -> str:
    """Approval once the tests pass, or after three failed attempts (route 'needs_engineer')."""
    if state.get("route") == "needs_engineer":
        return "approval"
    validation = (state.get("patch") or {}).get("validation") or {}
    return "approval" if validation.get("passed") else "fix"


def after_approval(state: UpgradeState) -> str:
    return "act" if state.get("decision") == "approved" else "finish"


def build_graph(ctx: nodes.RunContext, checkpointer: Any, overrides: dict[str, NodeFn] | None = None):
    """Compile the graph. `overrides` replaces node functions (tests use stubs)."""
    fns: dict[str, NodeFn] = {name: getattr(nodes, name) for name in NODE_NAMES} | (overrides or {})

    def bind(name: str):
        async def run(state: UpgradeState) -> dict:
            return await fns[name](ctx, state)

        run.__name__ = name
        return run

    async def approval(state: UpgradeState) -> dict:
        # Nothing may happen before interrupt(): on resume this node runs again from its first line.
        answer = interrupt({"run_id": state["run_id"], "route": state.get("route")})
        events.emit(ctx.conn, state["run_id"], "approval", "finished",
                    f"{answer['decision']} by {answer['by']}")
        return {"decision": answer["decision"], "decided_by": answer["by"]}

    graph = StateGraph(UpgradeState)
    for name in NODE_NAMES:
        graph.add_node(name, bind(name))
    graph.add_node("approval", approval)

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "impact")
    graph.add_edge("impact", "telemetry")
    graph.add_edge("telemetry", "plan")
    graph.add_conditional_edges("plan", after_plan, ["fix", "finish"])
    graph.add_edge("fix", "validate")
    graph.add_conditional_edges("validate", after_validate, ["fix", "approval"])
    graph.add_conditional_edges("approval", after_approval, ["act", "finish"])
    graph.add_edge("act", "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)


def thread_config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}}


async def is_paused(graph, run_id: str) -> bool:
    snapshot = await graph.aget_state(thread_config(run_id))
    return tuple(snapshot.next) == ("approval",)

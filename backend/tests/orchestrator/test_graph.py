import asyncio
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from app.core import db as core_db
from app.core.config import Settings
from app.core.events import list_events
from app.orchestrator import graph as graph_module
from app.orchestrator.nodes import RunContext
from tests.updates.conftest import make_db


@pytest.fixture
def ctx(tmp_path):
    conn = core_db.connect(str(make_db(tmp_path / "t.db", patched=True)))
    core_db.migrate(conn)
    return RunContext(conn=conn, http=None, llm=None, settings=Settings(_env_file=None), workspace_root=tmp_path)


def make_stubs(calls: list[str], *, route="full", passes_on=1):
    """Stub nodes: the same state keys the real ones produce."""

    def stub(name, update=None):
        async def fn(c, state):
            calls.append(name)
            return update(state) if callable(update) else (update or {})

        return fn

    def fix_update(state):
        return {"attempts": state.get("attempts", 0) + 1}

    def validate_update(state):
        n = state["attempts"]
        passed = n >= passes_on
        update = {"patch": {"validation": {"passed": passed}}}
        if not passed and n >= 3:
            update["route"] = "needs_engineer"
        return update

    return {
        "prepare": stub("prepare", {"attempts": 0, "route": "full"}),
        "impact": stub("impact"),
        "telemetry": stub("telemetry"),
        "plan": stub("plan", {"route": route}),
        "fix": stub("fix", fix_update),
        "validate": stub("validate", validate_update),
        "act": stub("act", {"pr": {"url": "x"}}),
        "finish": stub("finish"),
    }


async def start(ctx, checkpoint, stubs, run_id="r1"):
    async with AsyncSqliteSaver.from_conn_string(str(checkpoint)) as saver:
        graph = graph_module.build_graph(ctx, saver, stubs)
        await graph.ainvoke({"run_id": run_id}, graph_module.thread_config(run_id))
        return await graph_module.is_paused(graph, run_id)


async def resume(ctx, checkpoint, stubs, decision, run_id="r1"):
    """A fresh graph object over the same checkpoint file: the resume must not depend on process memory."""
    async with AsyncSqliteSaver.from_conn_string(str(checkpoint)) as saver:
        graph = graph_module.build_graph(ctx, saver, stubs)
        await graph.ainvoke(Command(resume={"decision": decision, "by": "alice"}), graph_module.thread_config(run_id))
        state = await graph.aget_state(graph_module.thread_config(run_id))
        return state.values, tuple(state.next)


def run(coro):
    return asyncio.run(coro)


def test_pauses_at_approval_then_resumes_approved_from_a_new_graph_object(ctx, tmp_path):
    calls: list[str] = []
    ck = tmp_path / "ck.db"
    assert run(start(ctx, ck, make_stubs(calls))) is True
    assert calls == ["prepare", "impact", "telemetry", "plan", "fix", "validate"]  # paused before act
    values, nxt = run(resume(ctx, ck, make_stubs(calls), "approved"))
    assert calls[-2:] == ["act", "finish"] and nxt == ()
    assert values["decision"] == "approved" and values["decided_by"] == "alice" and values["pr"] == {"url": "x"}
    events = [(e.node, e.summary) for e in list_events(ctx.conn, "r1")]
    assert ("approval", "approved by alice") in events


def test_rejection_skips_act(ctx, tmp_path):
    calls: list[str] = []
    ck = tmp_path / "ck.db"
    run(start(ctx, ck, make_stubs(calls)))
    values, nxt = run(resume(ctx, ck, make_stubs(calls), "rejected"))
    assert "act" not in calls and calls[-1] == "finish" and values["decision"] == "rejected" and nxt == ()


def test_retry_loop_runs_until_the_tests_pass(ctx, tmp_path):
    calls: list[str] = []
    assert run(start(ctx, tmp_path / "ck.db", make_stubs(calls, passes_on=3))) is True
    assert calls.count("fix") == 3 and calls.count("validate") == 3


def test_three_failures_route_to_approval_as_needs_engineer(ctx, tmp_path):
    calls: list[str] = []
    ck = tmp_path / "ck.db"
    assert run(start(ctx, ck, make_stubs(calls, passes_on=99))) is True
    assert calls.count("fix") == 3
    values, _ = run(resume(ctx, ck, make_stubs(calls, passes_on=99), "approved"))
    assert values["route"] == "needs_engineer" and calls[-2:] == ["act", "finish"]


def test_report_only_route_skips_fixing_and_never_pauses(ctx, tmp_path):
    calls: list[str] = []
    assert run(start(ctx, tmp_path / "ck.db", make_stubs(calls, route="report_only"))) is False
    assert calls == ["prepare", "impact", "telemetry", "plan", "finish"]


def test_two_runs_have_independent_threads(ctx, tmp_path):
    calls: list[str] = []
    ck = tmp_path / "ck.db"
    run(start(ctx, ck, make_stubs(calls), "a"))
    run(start(ctx, ck, make_stubs(calls), "b"))
    values, _ = run(resume(ctx, ck, make_stubs(calls), "approved", "a"))
    assert values["run_id"] == "a"

    async def state_of_b():
        async with AsyncSqliteSaver.from_conn_string(str(ck)) as saver:
            graph = graph_module.build_graph(ctx, saver, make_stubs([]))
            return await graph_module.is_paused(graph, "b")

    assert run(state_of_b()) is True  # b is still waiting


def test_routing_functions():
    assert graph_module.after_plan({"route": "report_only"}) == "finish"
    assert graph_module.after_plan({"route": "full"}) == "fix"
    assert graph_module.after_validate({"route": "needs_engineer"}) == "approval"
    assert graph_module.after_validate({"patch": {"validation": {"passed": True}}}) == "approval"
    assert graph_module.after_validate({"patch": {"validation": {"passed": False}}}) == "fix"
    assert graph_module.after_validate({}) == "fix"
    assert graph_module.after_approval({"decision": "approved"}) == "act"
    assert graph_module.after_approval({"decision": "rejected"}) == "finish"
    assert graph_module.after_approval({}) == "finish"


def test_checkpoint_file_is_separate_from_the_main_database(ctx, tmp_path):
    ck = tmp_path / "ck.db"
    run(start(ctx, ck, make_stubs([])))
    assert Path(ck).exists()
    assert "checkpoints" in {r["name"] for r in core_db.connect(str(ck)).execute("SELECT name FROM sqlite_master")}
    assert "checkpoints" not in core_db.table_names(ctx.conn)

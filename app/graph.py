"""Graph wiring — plan §2. Parser -> supervisor loop (supervisor <-> supervisor_tools,
which internally fans out to per-lane researcher subgraphs) -> Verdict. `build_graph`
is a factory rather than a module-level singleton because each node opens its own
short-lived DB connection scoped to a `db_path`, and tests need to point that at a
temp file without a global to patch."""
from __future__ import annotations

from functools import partial
from typing import Any

from langgraph.graph import END, StateGraph

from app.db import connection, write_lead_trace
from app.parser import parser_node
from app.state import SupervisorState
from app.supervisor import route_after_supervisor_tools, supervisor_node, supervisor_tools_node
from app.verdict import verdict_node


def _parser_wrapper(state: SupervisorState, *, db_path: str | None) -> dict[str, Any]:
    with connection(db_path) as conn:
        return parser_node(state, conn=conn)


async def _verdict_wrapper(state: SupervisorState, *, db_path: str | None) -> dict[str, Any]:
    with connection(db_path) as conn:
        return await verdict_node(state, conn=conn)


def build_graph(db_path: str | None = None):
    graph = StateGraph(SupervisorState)
    graph.add_node("parser", partial(_parser_wrapper, db_path=db_path))
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("supervisor_tools", supervisor_tools_node)
    graph.add_node("verdict", partial(_verdict_wrapper, db_path=db_path))

    graph.set_entry_point("parser")
    graph.add_edge("parser", "supervisor")
    graph.add_edge("supervisor", "supervisor_tools")
    graph.add_conditional_edges(
        "supervisor_tools",
        route_after_supervisor_tools,
        {"supervisor": "supervisor", "verdict": "verdict"},
    )
    graph.add_edge("verdict", END)
    return graph.compile()


async def run_lead(entity_id: str, db_path: str | None = None) -> SupervisorState:
    from app.state import new_supervisor_state, trace_event

    graph = build_graph(db_path)
    initial = new_supervisor_state(entity_id)
    accumulated: SupervisorState = initial
    try:
        async for step_state in graph.astream(initial, stream_mode="values"):
            accumulated = step_state
        return accumulated
    except Exception as exc:
        crash_event = trace_event(
            "graph", "lead_crash", error=f"{type(exc).__name__}: {exc}"
        )
        trace = list(accumulated.get("trace", [])) + [crash_event]
        with connection(db_path) as conn:
            write_lead_trace(conn, entity_id, trace)
        raise

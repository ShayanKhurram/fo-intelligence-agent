from __future__ import annotations

from langchain_core.messages import AIMessage

from app.questions import QUESTION_BATTERY
from app.state import LeadBrief, LeadBudget, new_supervisor_state
from app.supervisor import route_after_supervisor_tools, supervisor_node, supervisor_tools_node


def _state_with_brief(**budget_kwargs) -> dict:
    state = new_supervisor_state("E1")
    state["lead_brief"] = LeadBrief(
        entity_id="E1", canonical_name="Acme FO", questions=list(QUESTION_BATTERY), budget=LeadBudget(**budget_kwargs)
    )
    return state


async def test_supervisor_node_calls_llm_with_tools(fake_model):
    fake_model.queue(AIMessage(content="", tool_calls=[{"id": "1", "name": "think_tool", "args": {"reflection": "start"}}]))
    state = _state_with_brief()
    delta = await supervisor_node(state)
    assert delta["iterations"] == 1
    assert len(fake_model.calls) == 1


async def test_supervisor_tools_think_tool_acks_without_dispatch():
    state = _state_with_brief()
    state["supervisor_messages"] = [
        AIMessage(content="", tool_calls=[{"id": "1", "name": "think_tool", "args": {"reflection": "hi"}}])
    ]
    delta = await supervisor_tools_node(state)
    assert delta["claims"] == []
    assert delta["lanes_dispatched"] == {}
    assert delta["research_complete"] is False


async def test_research_complete_blocked_while_hard_gates_unanswered():
    state = _state_with_brief()
    state["supervisor_messages"] = [
        AIMessage(content="", tool_calls=[{"id": "1", "name": "research_complete", "args": {}}])
    ]
    delta = await supervisor_tools_node(state)
    assert delta["research_complete"] is False
    assert "Not yet" in delta["supervisor_messages"][0].content


async def test_research_complete_allowed_once_budget_exhausted():
    state = _state_with_brief(max_tool_calls=0, max_iterations=0)
    state["iterations"] = 5  # force _budget_exhausted True
    state["supervisor_messages"] = [
        AIMessage(content="", tool_calls=[{"id": "1", "name": "research_complete", "args": {}}])
    ]
    delta = await supervisor_tools_node(state)
    assert delta["research_complete"] is True


async def test_lane_redispatch_cap_skips_extra_dispatches(fake_model):
    state = _state_with_brief()
    state["lanes_dispatched"] = {"identity_and_type": 2}  # already at default cap (2x)
    state["supervisor_messages"] = [
        AIMessage(
            content="",
            tool_calls=[{"id": "1", "name": "conduct_research", "args": {"lane": "identity_and_type", "instructions": "again"}}],
        )
    ]
    delta = await supervisor_tools_node(state)
    assert delta["lanes_dispatched"] == {}
    assert delta["claims"] == []
    assert "already dispatched" in delta["supervisor_messages"][0].content


def test_route_after_supervisor_tools_defaults_to_loop():
    state = _state_with_brief()
    assert route_after_supervisor_tools(state) == "supervisor"


def test_route_after_supervisor_tools_goes_to_verdict_when_complete():
    state = _state_with_brief()
    state["research_complete"] = True
    assert route_after_supervisor_tools(state) == "verdict"

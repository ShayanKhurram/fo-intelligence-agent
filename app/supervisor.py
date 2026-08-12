"""supervisor + supervisor_tools — plan §4.2, §4.3. Two-node loop: supervisor (LLM)
plans, supervisor_tools (code) executes side effects — think_tool acks, ConductResearch
dispatches researcher subgraphs in parallel (capped, with a per-lane re-dispatch limit),
ResearchComplete is gated in code, not just by prompt, so a lead can't short-circuit
past its HARD gates just because the model said so."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from app.config import SETTINGS
from app.llm import get_model
from app.questions import HARD_GATE_QUESTION_IDS, QUESTIONS_BY_LANE
from app.researcher import run_researcher_lane
from app.state import LANES, Claim, SupervisorState, trace_event


@tool
def conduct_research(lane: Literal["identity_and_type", "people", "activity_signals"], instructions: str) -> str:
    """Dispatch a researcher subgraph for one lane with specific instructions. You may
    call this multiple times in one turn to dispatch several lanes in parallel.

    Args:
        lane: Which lane to dispatch — identity_and_type, people, or activity_signals.
        instructions: What this lane's researcher should focus on given what's known so far.
    """
    return "dispatched"  # intercepted by supervisor_tools_node; never actually invoked


@tool
def research_complete() -> str:
    """Signal that research is complete and the lead is ready for a verdict. Only call
    this once every HARD-gate question has a non-unanswered status, or the budget is
    exhausted."""
    return "complete"


@tool
def think_tool(reflection: str) -> str:
    """Record your reasoning before deciding what to do next: what's confirmed, what's
    still missing, and which lane (if any) needs dispatching.

    Args:
        reflection: Your analysis of progress so far and what should happen next.
    """
    return f"Reflection recorded: {reflection}"


SUPERVISOR_TOOLS = [think_tool, conduct_research, research_complete]


def _claims_by_question(claims: list[dict[str, Any]]) -> dict[str, Claim]:
    by_q: dict[str, Claim] = {}
    for c in claims:
        claim = Claim(**c)
        # last write wins — a refinement dispatch's claim supersedes the earlier one
        by_q[claim.question_id] = claim
    return by_q


def _unanswered_hard_questions(claims: list[dict[str, Any]]) -> list[str]:
    by_q = _claims_by_question(claims)
    unanswered = []
    for qid in HARD_GATE_QUESTION_IDS:
        claim = by_q.get(qid)
        if claim is None or claim.status == "could_not_verify":
            unanswered.append(qid)
    return unanswered


def _budget_exhausted(state: SupervisorState) -> bool:
    brief = state["lead_brief"]
    if brief is None:
        return False
    return (
        state["calls_spent"] >= brief.budget.max_tool_calls
        or state["iterations"] >= brief.budget.max_iterations
        or state["cost_usd"] >= brief.budget.max_usd
    )


def _lane_status_summary(state: SupervisorState) -> str:
    by_q = _claims_by_question(state["claims"])
    lines = []
    for lane in LANES:
        qids = [q.question_id for q in QUESTIONS_BY_LANE.get(lane, [])]
        answered = [qid for qid in qids if qid in by_q and by_q[qid].status != "could_not_verify"]
        hard_unanswered = [
            qid for qid in qids if qid in HARD_GATE_QUESTION_IDS and qid not in answered
        ]
        dispatched = state["lanes_dispatched"].get(lane, 0)
        lines.append(
            f"- {lane}: {len(answered)}/{len(qids)} answered, "
            f"HARD-unanswered={hard_unanswered or 'none'}, dispatched={dispatched}x "
            f"(cap {SETTINGS.supervisor.max_lane_dispatches}x)"
        )
    return "\n".join(lines)


def _supervisor_system_prompt(state: SupervisorState) -> str:
    brief = state["lead_brief"]
    assert brief is not None
    return (
        "You are the supervisor for a family-office lead-qualification pipeline. You "
        "dispatch three research lanes and decide when enough evidence exists for a "
        "verdict. You never research directly — only think_tool, conduct_research, and "
        "research_complete are available to you.\n\n"
        f"Entity: {brief.canonical_name} (aliases: {brief.aliases})\n"
        f"Injected facts: {json.dumps(brief.injected_facts)}\n\n"
        "Lanes:\n"
        "- identity_and_type: does the entity exist, current name, FO-affirmative evidence, "
        "SFO/MFO, RIA-in-costume risk, active/defunct.\n"
        "- people: named decision-maker, findability, title recency.\n"
        "- activity_signals: capital deployment, recent exits/hires/commitments, scandal check.\n\n"
        "Gate semantics: HARD-gate questions can sink the lead if left unanswered "
        "(policy varies by question); SOFT-gate questions inform confidence, not rejection.\n\n"
        "RULE: dispatch every lane at least once. After that, only RE-dispatch a lane whose "
        "HARD-gate questions are still unanswered — do not re-dispatch a lane just to chase "
        "SOFT-gate questions once its HARD gates are settled.\n"
        "(The activity_signals lane has no HARD-gate questions at all since G3.Q3 was "
        "removed, so an 'only chase HARD gates' reading would skip it entirely and leave "
        "capital-deployment and recency evidence ungathered.)\n\n"
        f"Progress so far:\n{_lane_status_summary(state)}\n\n"
        f"Budget: {state['calls_spent']}/{brief.budget.max_tool_calls} tool calls used, "
        f"turn {state['iterations']}/{brief.budget.max_iterations}.\n\n"
        "Call think_tool first to reflect, then either dispatch lane(s) via "
        "conduct_research or call research_complete."
    )


_SUPERVISOR_SYSTEM_ID = "supervisor-system"


async def supervisor_node(state: SupervisorState) -> dict[str, Any]:
    model = get_model(SETTINGS.models.supervisor_tier)
    # The system message carries a stable id so the add_messages reducer replaces it in
    # place each turn (refreshed progress/budget) instead of appending a duplicate —
    # everything else in supervisor_messages accumulates normally.
    fresh_system = SystemMessage(content=_supervisor_system_prompt(state), id=_SUPERVISOR_SYSTEM_ID)
    kickoff: list[Any] = []
    if state["supervisor_messages"]:
        invoke_messages = [fresh_system, *state["supervisor_messages"][1:]]
    else:
        # First turn needs a user message alongside the system prompt: Ollama Cloud reads a
        # system-only list as a model-load request and returns finish_reason="load" with
        # empty content, no tool calls and 0/0 tokens (HTTP 200, nothing raises). The
        # supervisor would then never emit conduct_research, so no lane was ever dispatched
        # and every lead ran out its 2 iterations and rejected on unanswered HARD gates at
        # ~$0. Same root cause and fix as app/researcher.py's researcher_node. It is
        # persisted below so later turns keep a user turn in the list too.
        kickoff = [
            HumanMessage(content="Begin. Reflect with think_tool, then dispatch the lanes you need.")
        ]
        invoke_messages = [fresh_system, *kickoff]
    response = await model.ainvoke(invoke_messages, tools=SUPERVISOR_TOOLS)
    cost = response.response_metadata.get("cost_usd", 0.0)
    event = trace_event(
        "supervisor",
        "ai_message",
        iteration=state["iterations"] + 1,
        content=response.content,
        tool_calls=[{"name": tc["name"], "args": tc["args"]} for tc in (response.tool_calls or [])],
        cost_usd=cost,
    )
    return {
        "supervisor_messages": [fresh_system, *kickoff, response],
        "iterations": state["iterations"] + 1,
        "cost_usd": state["cost_usd"] + cost,
        "trace": [event],
    }


async def supervisor_tools_node(state: SupervisorState) -> dict[str, Any]:
    last = state["supervisor_messages"][-1]
    tool_calls = last.tool_calls if isinstance(last, AIMessage) else []
    brief = state["lead_brief"]
    assert brief is not None

    tool_messages: list[ToolMessage] = []
    lanes_dispatched_delta: dict[str, int] = {}
    new_claims: list[dict[str, Any]] = []
    research_complete = state["research_complete"]
    calls_spent_delta = 0
    cost_delta = 0.0
    new_trace: list[dict[str, Any]] = []

    conduct_calls = [tc for tc in tool_calls if tc["name"] == "conduct_research"]
    other_calls = [tc for tc in tool_calls if tc["name"] != "conduct_research"]

    # Cap re-dispatch per lane, then run the rest through in concurrency-capped batches
    # (functionally equivalent to ODR's "overflow queues to next iteration" — every
    # accepted dispatch still runs, just not all in one asyncio.gather).
    accepted: list[dict[str, Any]] = []
    for tc in conduct_calls:
        lane = tc["args"]["lane"]
        already = state["lanes_dispatched"].get(lane, 0) + lanes_dispatched_delta.get(lane, 0)
        if already >= SETTINGS.supervisor.max_lane_dispatches:
            tool_messages.append(
                ToolMessage(
                    content=f"Lane {lane!r} already dispatched {already}x "
                    f"(cap {SETTINGS.supervisor.max_lane_dispatches}x) — skipped.",
                    tool_call_id=tc["id"],
                )
            )
            new_trace.append(trace_event("supervisor", "dispatch_skipped", lane=lane, already_dispatched=already))
            continue
        lanes_dispatched_delta[lane] = lanes_dispatched_delta.get(lane, 0) + 1
        accepted.append(tc)
        new_trace.append(trace_event("supervisor", "dispatch", lane=lane, instructions=tc["args"]["instructions"]))

    lead_brief_slim = {
        "canonical_name": brief.canonical_name,
        "aliases": brief.aliases,
        "injected_facts": brief.injected_facts,
    }
    cap = SETTINGS.supervisor.max_concurrent_research_units
    for i in range(0, len(accepted), cap):
        batch = accepted[i : i + cap]
        results = await asyncio.gather(
            *(
                run_researcher_lane(tc["args"]["lane"], tc["args"]["instructions"], lead_brief_slim)
                for tc in batch
            )
        )
        for tc, result in zip(batch, results):
            new_claims.extend(result["claims"])
            calls_spent_delta += result["tool_calls_used"]
            cost_delta += result["cost_usd"]
            new_trace.extend(result.get("trace", []))  # the lane's own full turn-by-turn trace
            new_trace.append(
                trace_event(
                    "supervisor",
                    "lane_complete",
                    lane=tc["args"]["lane"],
                    lane_status=result["lane_status"],
                    claim_count=len(result["claims"]),
                )
            )
            tool_messages.append(
                ToolMessage(
                    content=(
                        f"Lane {tc['args']['lane']!r} returned {len(result['claims'])} "
                        f"claim(s), status={result['lane_status']}."
                    ),
                    tool_call_id=tc["id"],
                )
            )

    for tc in other_calls:
        if tc["name"] == "think_tool":
            tool_messages.append(
                ToolMessage(content=f"Reflection recorded: {tc['args']['reflection']}", tool_call_id=tc["id"])
            )
            new_trace.append(trace_event("supervisor", "think", reflection=tc["args"]["reflection"]))
        elif tc["name"] == "research_complete":
            all_claims = state["claims"] + new_claims
            unanswered = _unanswered_hard_questions(all_claims)
            # Every lane must run at least once, enforced here rather than trusted to the
            # prompt. Since G3.Q3 was removed, activity_signals has NO HARD-gate questions,
            # so a purely HARD-gate-driven completion check would let a lead finish without
            # that lane ever being dispatched — losing G3.Q1/G3.Q2 (capital deployment and
            # recency), which compute_thin_reason and V6 completeness both read.
            undispatched = [
                lane
                for lane in LANES
                if state["lanes_dispatched"].get(lane, 0) + lanes_dispatched_delta.get(lane, 0) == 0
            ]
            exhausted = _budget_exhausted(state)
            if (unanswered or undispatched) and not exhausted:
                reasons = []
                if unanswered:
                    reasons.append(f"HARD-gate questions still unanswered: {unanswered}")
                if undispatched:
                    reasons.append(f"lane(s) never dispatched: {undispatched}")
                tool_messages.append(
                    ToolMessage(
                        content=(
                            "Not yet — " + "; ".join(reasons) + ". "
                            "Dispatch the relevant lane(s) or exhaust the budget first."
                        ),
                        tool_call_id=tc["id"],
                    )
                )
                new_trace.append(
                    trace_event(
                        "supervisor",
                        "research_complete_blocked",
                        unanswered=unanswered,
                        undispatched=undispatched,
                    )
                )
            else:
                research_complete = True
                tool_messages.append(ToolMessage(content="Research marked complete.", tool_call_id=tc["id"]))
                new_trace.append(
                    trace_event("supervisor", "research_complete_accepted", budget_exhausted=exhausted)
                )

    return {
        "supervisor_messages": tool_messages,
        "lanes_dispatched": lanes_dispatched_delta,
        "claims": new_claims,
        "calls_spent": state["calls_spent"] + calls_spent_delta,
        "cost_usd": state["cost_usd"] + cost_delta,
        "research_complete": research_complete,
        "trace": new_trace,
    }


def route_after_supervisor_tools(state: SupervisorState) -> Literal["supervisor", "verdict"]:
    if state["research_complete"] or _budget_exhausted(state):
        return "verdict"
    return "supervisor"

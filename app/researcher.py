"""Per-lane researcher subgraph — plan §4.4, §4.5. researcher -> researcher_tools loop,
capped at max_react_tool_calls, then compress_to_claims. Raw notes never leave this
subgraph; only compressed Claim objects cross back to the supervisor (ODR's
context-isolation insight, kept intact per plan §3)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from pydantic import ValidationError

from app.config import SETTINGS
from app.llm import get_model
from app.questions import QUESTIONS_BY_LANE
from app.state import Claim, ResearcherState, trace_event
from app.tools import LANE_TOOLS

logger = logging.getLogger(__name__)

_SUMMARIZE_SYSTEM = (
    "Condense the following tool-result content to at most 2 sentences, preserving any "
    "concrete facts (names, dates, figures, titles). Do not invent anything not present "
    "in the text. Output only the condensed text, no preamble."
)


async def _summarize_text(text: str) -> tuple[str, float]:
    if not text or len(text) < 240:
        return text, 0.0
    model = get_model(SETTINGS.models.summarizer_tier)
    resp = await model.ainvoke(
        [SystemMessage(content=_SUMMARIZE_SYSTEM), HumanMessage(content=text[:8000])]
    )
    cost = resp.response_metadata.get("cost_usd", 0.0)
    return str(resp.content).strip() or text[:240], cost


async def _format_tool_result_as_note(tool_name: str, result: dict[str, Any]) -> tuple[str, float]:
    """Turns a raw tool result into a compact note. URL/date fields are copied verbatim
    from the structured result — never passed through the summarizer — so provenance can
    never be corrupted by a lossy summary (plan §4.4). Returns (note, summarizer_cost_usd)."""
    total_cost = 0.0
    if "results" in result:
        lines = [f"[{tool_name}] query={result.get('query')!r}"]
        if result.get("error"):
            lines.append(f"  (tool error: {result['error']})")
        for r in result.get("results", [])[:5]:
            url = r.get("url")
            date = r.get("seendate") or r.get("filed_at") or r.get("date")
            snippet = r.get("content") or r.get("snippet") or ""
            if snippet:
                summary, cost = await _summarize_text(snippet)
                total_cost += cost
            else:
                summary = ""
            date_part = f" (date: {date})" if date else ""
            lines.append(f"  - {r.get('title') or url} — {url}{date_part}\n    {summary}")
        if not result.get("results"):
            lines.append("  (no results)")
        return "\n".join(lines), total_cost

    if tool_name == "fetch_page":
        url = result.get("url")
        content = result.get("content", "")
        if result.get("error"):
            return f"[fetch_page] {url} — error: {result['error']}", 0.0
        summary, cost = await _summarize_text(content)
        return f"[fetch_page] {url}\n  {summary}", cost

    if tool_name == "think_tool":
        return f"[think_tool] {result}", 0.0

    return f"[{tool_name}] {json.dumps(result)[:1000]}", 0.0


def _lane_system_prompt(state: ResearcherState) -> str:
    lane = state["lane"]
    questions = QUESTIONS_BY_LANE.get(lane, [])
    q_lines = "\n".join(f"- {q.question_id}: {q.text}" for q in questions)
    hard_gates = [q.question_id for q in questions if q.gate == "HARD"]
    brief = state["lead_brief_slim"]
    hard_gate_line = (
        f"\nHARD-gate questions for this lane (try at least 2 different tools or query "
        f"phrasings before answering could_not_verify — do not stop after one empty "
        f"result, up to your {SETTINGS.researcher.max_react_tool_calls}-call cap): "
        f"{', '.join(hard_gates)}\n"
        if hard_gates
        else ""
    )
    identity_hint = (
        "\nFor FO-status questions specifically, before concluding could_not_verify try "
        "edgar_submissions (SIC code / business description), fetch_page (the entity's "
        "own official website), and nonprofit_lookup (affiliated-foundation overlap). "
        "These are direct APIs (not scraping), so they keep working when web_search is "
        "throttled or rate-limited.\n"
        if lane == "identity_and_type"
        else ""
    )
    return (
        f"You are the {lane} researcher for a family-office lead-qualification pipeline.\n"
        f"Entity: {brief.get('canonical_name')} (aliases: {brief.get('aliases')})\n"
        f"Known facts: {json.dumps(brief.get('injected_facts', {}))}\n\n"
        f"Supervisor instructions for this lane:\n{state['instructions']}\n\n"
        f"Questions this lane must try to answer:\n{q_lines}\n"
        f"{hard_gate_line}"
        f"{identity_hint}\n"
        "Call tools to gather evidence. Every fact you plan to report must trace back to "
        "a source_url, or you must report it as could_not_verify. You have a limited "
        f"number of tool calls ({SETTINGS.researcher.max_react_tool_calls}) — stop and "
        "report once you have enough to answer the questions, or once you've exhausted "
        "productive leads. When you are done gathering evidence, respond with no further "
        "tool calls."
    )


async def researcher_node(state: ResearcherState) -> dict[str, Any]:
    tools = LANE_TOOLS[state["lane"]]
    model = get_model(SETTINGS.models.researcher_tier)
    messages = state["researcher_messages"]
    if not messages:
        messages = [SystemMessage(content=_lane_system_prompt(state))]
    response = await model.ainvoke(messages, tools=tools)
    cost = response.response_metadata.get("cost_usd", 0.0)
    event = trace_event(
        "researcher",
        "ai_message",
        lane=state["lane"],
        content=response.content,
        tool_calls=[{"name": tc["name"], "args": tc["args"]} for tc in (response.tool_calls or [])],
        cost_usd=cost,
    )
    return {
        "researcher_messages": [response],
        "cost_usd": state["cost_usd"] + cost,
        "trace": [event],
    }


def _result_is_usable(tool_name: str, result: Any) -> bool:
    """True if a tool result carries real evidence (non-error, non-empty). Mirrors the
    emptiness checks _format_tool_result_as_note applies: an errored result, an empty
    `results` list, or empty `fetch_page` content is NOT evidence. think_tool is a
    reflection, not evidence. A dict result with no `results` key (e.g. edgar_submissions,
    nonprofit_detail) counts as usable as long as it has no error."""
    if tool_name == "think_tool":
        return False
    if not isinstance(result, dict):
        return False
    if result.get("error"):
        return False
    if "results" in result:
        return bool(result.get("results"))
    if tool_name == "fetch_page":
        return bool(result.get("content"))
    return True


async def researcher_tools_node(state: ResearcherState) -> dict[str, Any]:
    last = state["researcher_messages"][-1]
    tool_calls = last.tool_calls if isinstance(last, AIMessage) else []
    tools_by_name = {t.name: t for t in LANE_TOOLS[state["lane"]]}

    async def run_one(tc: dict[str, Any]) -> tuple[ToolMessage, str, float, dict[str, Any], bool]:
        tool = tools_by_name.get(tc["name"])
        if tool is None:
            result: Any = {"error": f"unknown tool {tc['name']!r} for this lane"}
        else:
            try:
                result = await tool.ainvoke(tc["args"])
            except Exception as exc:  # noqa: BLE001 — a tool crash must degrade, not kill the lane
                logger.warning("Tool %r crashed: %s", tc["name"], exc)
                result = {"error": str(exc)}
        note, note_cost = await _format_tool_result_as_note(tc["name"], result)
        usable = _result_is_usable(tc["name"], result)
        event = trace_event(
            "researcher_tool",
            "tool_call",
            lane=state["lane"],
            tool=tc["name"],
            args=tc["args"],
            result=result,
        )
        return ToolMessage(content=json.dumps(result, default=str), tool_call_id=tc["id"]), note, note_cost, event, usable

    outcomes = await asyncio.gather(*(run_one(tc) for tc in tool_calls))
    tool_messages = [o[0] for o in outcomes]
    notes = [o[1] for o in outcomes]
    summarizer_cost = sum(o[2] for o in outcomes)
    events = [o[3] for o in outcomes]
    batch_has_evidence = any(o[4] for o in outcomes)
    return {
        "researcher_messages": tool_messages,
        "raw_notes": notes,
        "tool_calls_used": state["tool_calls_used"] + len(tool_calls),
        "had_real_evidence": state["had_real_evidence"] or batch_has_evidence,
        "cost_usd": state["cost_usd"] + summarizer_cost,
        "trace": events,
    }


def _route_after_researcher(state: ResearcherState) -> str:
    last = state["researcher_messages"][-1]
    has_calls = isinstance(last, AIMessage) and bool(last.tool_calls)
    if has_calls and state["tool_calls_used"] < SETTINGS.researcher.max_react_tool_calls:
        return "researcher_tools"
    return "compress_to_claims"


_COMPRESS_SYSTEM_TEMPLATE = (
    "You compress a researcher's raw notes into structured claims. For each question "
    "below that the notes touch on, emit exactly one claim. Rules:\n"
    "- Every claim needs a source_url, UNLESS status is 'could_not_verify'.\n"
    "- Never invent a question_id not in the list below.\n"
    "- If two sources disagree, set status='contradicted' and put both URLs in the answer "
    "text — do not silently pick one.\n"
    "- Respond with ONLY a JSON array of objects with keys: question_id, answer, status "
    "(confirmed|could_not_verify|contradicted), source_url, source_class, confidence "
    "(high|medium|low). No prose, no markdown fences.\n\n"
    "Questions for this lane:\n{questions}\n"
)


def _tag_evidence_gaps(claims: list[Claim], had_real_evidence: bool) -> None:
    """Distinguish "the tool was broken/empty" from "genuinely no evidence exists" on
    could_not_verify claims the LLM left unclassified. Only sets source_class on claims
    whose source_class is currently falsy — never overwrites a value the LLM already
    assigned (e.g. "uncompressed_notes" on the parse-failure fallback path)."""
    gap_class = "no_evidence_found" if had_real_evidence else "tool_unavailable"
    for c in claims:
        if c.status == "could_not_verify" and not c.source_class:
            c.source_class = gap_class


def _parse_claims_json(text: str, lane_question_ids: set[str]) -> list[Claim]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("compress_to_claims output must be a JSON array")
    claims: list[Claim] = []
    for item in data:
        if item.get("question_id") not in lane_question_ids:
            continue  # plan §4.5: no claim without a valid question_id
        claims.append(Claim(**item))
    return claims


async def compress_to_claims_node(state: ResearcherState) -> dict[str, Any]:
    lane = state["lane"]
    questions = QUESTIONS_BY_LANE.get(lane, [])
    lane_question_ids = {q.question_id for q in questions}
    notes_text = "\n\n".join(state["raw_notes"]) or "(no notes gathered)"
    system = _COMPRESS_SYSTEM_TEMPLATE.format(
        questions="\n".join(f"- {q.question_id}: {q.text}" for q in questions)
    )
    model = get_model(SETTINGS.models.compress_tier)

    call_cost = 0.0
    last_exc: Exception | None = None
    for attempt in range(2):
        response = await model.ainvoke(
            [
                SystemMessage(content=system),
                HumanMessage(content=f"Raw notes:\n{notes_text}"),
            ]
        )
        call_cost += response.response_metadata.get("cost_usd", 0.0)
        try:
            claims = _parse_claims_json(str(response.content), lane_question_ids)
            _tag_evidence_gaps(claims, state["had_real_evidence"])
            event = trace_event(
                "compress",
                "compress_output",
                lane=lane,
                attempt=attempt,
                raw_response=response.content,
                claims=[c.model_dump(mode="json") for c in claims],
            )
            return {
                "claims": [c.model_dump(mode="json") for c in claims],
                "lane_status": "ok",
                "cost_usd": state["cost_usd"] + call_cost,
                "trace": [event],
            }
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            last_exc = exc
            if attempt == 0:
                continue
            break

    # Plan §4.5/§7: parse failure after retry -> never fabricate. Fall back to raw notes
    # flagged uncompressed and mark this lane's questions could_not_verify.
    exc_str = f"{type(last_exc).__name__}: {last_exc}" if last_exc else "unknown parse error"
    fallback_claims = [
        Claim(
            question_id=q.question_id,
            answer=f"compression failed ({exc_str}); raw notes stored uncompressed",
            status="could_not_verify",
            source_url=None,
            source_class="uncompressed_notes",
            confidence="low",
        )
        for q in questions
    ]
    _tag_evidence_gaps(fallback_claims, state["had_real_evidence"])
    fallback_event = trace_event(
        "compress",
        "compress_parse_failed",
        lane=lane,
        raw_response=response.content,
        error=exc_str,
    )
    return {
        "claims": [c.model_dump(mode="json") for c in fallback_claims],
        "raw_notes": [f"[UNCOMPRESSED FALLBACK]\n{notes_text}"],
        "lane_status": "failed",
        "cost_usd": state["cost_usd"] + call_cost,
        "trace": [fallback_event],
    }


def build_researcher_graph():
    graph = StateGraph(ResearcherState)
    graph.add_node("researcher", researcher_node)
    graph.add_node("researcher_tools", researcher_tools_node)
    graph.add_node("compress_to_claims", compress_to_claims_node)
    graph.set_entry_point("researcher")
    graph.add_conditional_edges(
        "researcher",
        _route_after_researcher,
        {"researcher_tools": "researcher_tools", "compress_to_claims": "compress_to_claims"},
    )
    graph.add_edge("researcher_tools", "researcher")
    graph.add_edge("compress_to_claims", END)
    return graph.compile()


RESEARCHER_GRAPH = build_researcher_graph()


async def run_researcher_lane(
    lane: str, instructions: str, lead_brief_slim: dict[str, Any]
) -> ResearcherState:
    """Entrypoint used by supervisor_tools' ConductResearch dispatch. Enforces the
    hard per-lane timeout from plan §4.4 (SETTINGS.researcher.timeout_seconds) — on
    timeout, whatever partial state exists is still a valid (capped) result, not a
    failure. A crash inside the subgraph (plan §7)
    is caught here too: a broken lane must never take down the whole lead — it comes
    back marked 'failed' with its questions left for Verdict to treat as unanswered,
    and the supervisor is free to re-dispatch it once if budget allows."""
    initial: ResearcherState = {
        "lane": lane,
        "instructions": instructions,
        "lead_brief_slim": lead_brief_slim,
        "researcher_messages": [],
        "raw_notes": [],
        "tool_calls_used": 0,
        "had_real_evidence": False,
        "claims": [],
        "lane_status": "ok",
        "cost_usd": 0.0,
        "trace": [],
    }
    try:
        result = await asyncio.wait_for(
            RESEARCHER_GRAPH.ainvoke(initial), timeout=SETTINGS.researcher.timeout_seconds
        )
        return result
    except asyncio.TimeoutError:
        logger.warning("Lane %r timed out after %ss", lane, SETTINGS.researcher.timeout_seconds)
        event = trace_event("researcher", "lane_timeout", lane=lane, timeout_seconds=SETTINGS.researcher.timeout_seconds)
        return {**initial, "lane_status": "capped", "claims": [], "trace": [event]}
    except Exception as exc:  # noqa: BLE001 — a lane crash degrades, it never kills the lead
        # Log it: an empty-claims result is otherwise indistinguishable from "the lane
        # ran fine and genuinely found nothing" — the first live pilot run lost this
        # distinction entirely and it took manual DB forensics to even suspect a crash.
        logger.exception("Lane %r crashed for a lead — degrading to empty claims", lane)
        event = trace_event("researcher", "lane_crash", lane=lane, error=str(exc))
        return {**initial, "lane_status": "failed", "claims": [], "trace": [event]}

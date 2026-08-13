"""Per-lane researcher subgraph — plan §4.4, §4.5. researcher -> researcher_tools loop,
capped at max_react_tool_calls, then compress_to_claims. Raw notes never leave this
subgraph; only compressed Claim objects cross back to the supervisor (ODR's
context-isolation insight, kept intact per plan §3)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
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


# Keys `_format_tool_result_as_note` renders explicitly; everything else in a result row is
# rendered as compact key=value so structured facts are not silently dropped.
_NOTE_HANDLED_KEYS = frozenset(
    {"title", "url", "content", "snippet", "seendate", "filed_at", "date", "cache_hit"}
)
_NOTE_MAX_FIELDS = 12
_NOTE_MAX_VALUE_CHARS = 120


def _render_extra_fields(row: dict[str, Any]) -> str:
    """Render a result row's remaining structured fields as `key=value` pairs.

    This exists because the original formatter emitted only title/url/date/snippet, which
    silently threw away every other field a structured tool returned. The consequences were
    not subtle (found live 2026-08-12):
      * `adv_lookup` results reached the model as a bare URL twice — `sec_number=801-70776`,
        `branches_count=162`, `is_registered_investment_adviser=True` and `name_match=exact`
        were all dropped, so the tool that exists specifically to settle G1.Q4/G1.Q5
        delivered nothing and G1.Q4 stayed could_not_verify.
      * `edgar_search` hits lost `company_name` and `form_type`, so the model cited EDGAR
        archive URLs without ever seeing which company or filing type they belonged to.
    Values are truncated and capped in count so a wide record cannot flood a lane's context.
    """
    parts: list[str] = []
    for key, value in row.items():
        if key in _NOTE_HANDLED_KEYS or value is None or value == "" or value == []:
            continue
        if isinstance(value, (list, tuple)):
            rendered = ", ".join(str(v) for v in list(value)[:5])
        elif isinstance(value, dict):
            rendered = ", ".join(f"{k}={v}" for k, v in list(value.items())[:5] if v)
        else:
            rendered = str(value)
        rendered = rendered[:_NOTE_MAX_VALUE_CHARS]
        if rendered:
            parts.append(f"{key}={rendered}")
        if len(parts) >= _NOTE_MAX_FIELDS:
            break
    return "; ".join(parts)


async def _format_tool_result_as_note(tool_name: str, result: dict[str, Any]) -> tuple[str, float]:
    """Turns a raw tool result into a compact note. URL/date fields and all other structured
    fields are copied verbatim from the result — never passed through the summarizer — so
    provenance and figures can never be corrupted by a lossy summary (plan §4.4). Only
    free-text snippets are summarized. Returns (note, summarizer_cost_usd)."""
    total_cost = 0.0
    if "results" in result:
        header = f"[{tool_name}] query={result.get('query')!r}"
        # Result-set level counters (e.g. adv_lookup's exact_matches) matter for judging
        # whether any row can be trusted at all, so keep them on the header line.
        meta = _render_extra_fields(
            {k: v for k, v in result.items() if k not in ("results", "query", "error")}
        )
        if meta:
            header += f" ({meta})"
        lines = [header]
        if result.get("error"):
            lines.append(f"  (tool error: {result['error']})")
        for r in result.get("results", [])[:5]:
            if not isinstance(r, dict):
                lines.append(f"  - {str(r)[:200]}")
                continue
            url = r.get("url")
            date = r.get("seendate") or r.get("filed_at") or r.get("date")
            snippet = r.get("content") or r.get("snippet") or ""
            if snippet:
                summary, cost = await _summarize_text(snippet)
                total_cost += cost
            else:
                summary = ""
            date_part = f" (date: {date})" if date else ""
            lines.append(f"  - {r.get('title') or url} — {url}{date_part}")
            extras = _render_extra_fields(r)
            if extras:
                lines.append(f"    {extras}")
            if summary:
                lines.append(f"    {summary}")
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
    leads = brief.get("unverified_leads") or []
    if leads:
        lead_lines = [
            "UNVERIFIED LEADS from the discovery feed — these are starting points for your searches, NOT answers. You MUST confirm each one with a tool and cite the confirming source_url before reporting it. If you cannot confirm one, do not report it at all."
        ]
        for lead in leads:
            title_part = f" ({lead['title']})" if lead.get("title") else ""
            lead_lines.append(
                f"- {lead['kind']}: {lead['value']}{title_part} [via {lead['source_class']}]"
            )
        leads_block = "\n".join(lead_lines) + "\n\n"
    else:
        leads_block = ""
    identity_hint = (
        "\nFor FO-status questions specifically, before concluding could_not_verify try "
        "edgar_submissions (SIC code / business description), fetch_page (the entity's "
        "own official website), and nonprofit_lookup (affiliated-foundation overlap). "
        "These are direct APIs (not scraping), so they keep working when web_search is "
        "throttled or rate-limited.\n"
        "\nG1.Q4 (SFO vs MFO) and G1.Q5 (plain RIA in costume) MUST be settled from "
        "`adv_lookup` (SEC IAPD registration data), not from the firm's own website. A "
        "website saying 'family office' is self-branding and settles neither question. "
        "Read adv_lookup like this — but only for a result whose `name_match` is \"exact\":\n"
        "  - active 801- registration -> a registered investment adviser. A genuine "
        "single-family office normally does NOT register (it uses the family-office "
        "exclusion), so this is strong evidence for MFO and against SFO.\n"
        "  - branches_count well above 1 -> many offices serving many client families, "
        "i.e. multi-family office, not SFO.\n"
        "  - several distinct other_names -> an acquisition rollup of multiple firms.\n"
        "  - NO exact-match registration found -> that absence is itself evidence FOR a "
        "single-family office. Answer G1.Q4 'SFO' on that basis and cite the adv_lookup "
        "result; do NOT report could_not_verify just because the lookup was empty.\n"
        "Either way G1.Q4 must come back answered (SFO or MFO), not could_not_verify.\n"
        if lane == "identity_and_type"
        else ""
    )
    return (
        f"You are the {lane} researcher for a family-office lead-qualification pipeline.\n"
        f"Entity: {brief.get('canonical_name')} (aliases: {brief.get('aliases')})\n"
        f"Known facts: {json.dumps(brief.get('injected_facts', {}))}\n\n"
        f"{leads_block}"
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
    seed: list[Any] = []
    if not messages:
        # Two fixes in one, both verified live 2026-08-12:
        #
        # 1. The kick-off HumanMessage is REQUIRED, not stylistic. Ollama Cloud's
        #    OpenAI-compatible endpoint treats a system-only message list as a model-LOAD
        #    request: HTTP 200, finish_reason="load", empty content, no tool calls, 0
        #    prompt/0 completion tokens. Nothing raises, so the lane skipped straight to
        #    compress_to_claims with zero notes and marked every question could_not_verify
        #    — for every lead, at ~$0. System-only -> 0/0 tokens; +1 user turn -> 823
        #    prompt tokens and a real edgar_search call.
        #
        # 2. The seed must be PERSISTED, not just used locally. `researcher_messages` uses
        #    the add_messages reducer, so returning only the response meant turn 2 onwards
        #    ran with a list starting at an AIMessage and no system prompt at all — the
        #    lane forgot its questions, its HARD gates and its instructions after one turn.
        seed = [
            SystemMessage(content=_lane_system_prompt(state)),
            HumanMessage(content="Begin gathering evidence now."),
        ]
        messages = seed
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
        "researcher_messages": [*seed, response],
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
    "You compress a researcher's raw notes into structured claims. ONE CLAIM = ONE FACT "
    "= THE PAGE THAT SUPPORTS IT. For each question below that the notes touch on, emit "
    "one claim PER distinct supporting fact — never one combined claim. Rules:\n"
    "- A claim's `answer` and `subject_value` must contain ONLY what its single cited "
    "`source_url` actually states. Never join facts from different pages with \"and\" "
    "(or any other tie word) under one claim — if the lane found several distinct "
    "supporting facts on different pages, emit SEVERAL claims for that question, each "
    "with its OWN `source_url`, rather than one combined claim. A claim cited to a page "
    "that does not state every fact in its answer can never be verified and will be "
    "fatally rejected, so do not bundle.\n"
    "- Every claim needs a source_url, UNLESS status is 'could_not_verify'.\n"
    "- Never invent a question_id not in the list below.\n"
    "- Never answer with a bare \"Yes\"/\"No\". Every answer must restate its subject so it "
    "stands alone out of context — the claim is later read without the question text. "
    "So instead of \"Yes\" write \"Matt Blackburn is Managing Director of Class VI Family "
    "Office\", not a bare affirmative; instead of \"No\" state what was actually found or "
    "that nothing was found.\n"
    "- For these question_ids ALSO emit a `subject_value` key holding ONLY the value, no "
    "prose: G2.Q1 -> the person's full name; G2.Q2 -> the URL of that person's public "
    "profile; G2.Q3 -> their current title; G3.Q1 -> a short description of the "
    "investment/deployment; G3.Q2 -> a short description of the recent signal. Omit the "
    "`subject_value` key for every other question_id, and whenever the lane could not "
    "determine the value.\n"
    "- Use status='contradicted' ONLY when two sources assert MUTUALLY EXCLUSIVE facts "
    "about the same thing — facts that cannot both be true (e.g. one says the firm is "
    "dissolved and another says it is actively filing; one gives AUM as $50M and another "
    "as $2B for the same date). When you do, name BOTH conflicting values and cite BOTH "
    "URLs in the answer text — do not silently pick one.\n"
    "- The following are NOT contradictions. Use status='confirmed':\n"
    "  * Several people each holding a senior role. A firm can have two co-managing "
    "members, a CEO and a CIO, or a founder and a president — listing more than one "
    "decision-maker is normal and is not a conflict. Name all of them in the answer.\n"
    "  * Two sources stating the same fact in different words, units, or detail levels.\n"
    "  * One source being silent on something another source states. Silence is not "
    "disagreement — that is just one source being less complete.\n"
    "  * Figures for different dates or reporting periods (a Q1 number and a Q2 number "
    "are two facts, not a conflict).\n"
    "- Respond with ONLY a JSON array of objects with keys: question_id, answer, status "
    "(confirmed|could_not_verify|contradicted), source_url, source_class, confidence "
    "(high|medium|low), and (for the listed question_ids only) subject_value. No prose, "
    "no markdown fences.\n\n"
    "Questions for this lane:\n{questions}\n"
)

_URL_IN_TEXT_RE = re.compile(r"https?://[^\s,;'\"<>)\]]+")


def _downgrade_unsupported_contradictions(claims: list[Claim]) -> list[tuple[str, str]]:
    """Enforce the "cite BOTH URLs" half of the contradiction contract in CODE.

    A claim asserting `contradicted` has to actually demonstrate a conflict: two sources
    saying incompatible things. If the answer text doesn't cite at least two distinct URLs,
    the model has not shown one, so the status is downgraded in place.

    This exists because the compress step was by far the largest producer of false
    contradictions — 142 of the 203 contradicted claims in the ledger came from here, versus
    ZERO from V4, the check actually named for contradictions (2026-08-12). The samples were
    not close calls; they were plain confirmations, e.g. G2.Q1 "Yes – the firm's Form ADV
    lists Co-Managing Members Mary C. McNutt and Michelle J. Blass as owners" stored as
    `contradicted` purely because two people were named. A `contradicted` claim is excluded
    from V6 completeness, blocks its HARD gate in Verdict, and is dropped from the dataset —
    so a false one silently destroys a good record.

    Returns the list of (question_id, old_answer_preview) downgrades so the caller can trace
    them; downgrades are never silent.
    """
    downgraded: list[tuple[str, str]] = []
    for c in claims:
        if c.status != "contradicted":
            continue
        urls = set(_URL_IN_TEXT_RE.findall(str(c.answer or "")))
        if len(urls) >= 2:
            continue  # a genuine, cited conflict — leave it alone
        # Not demonstrated. Keep the finding honest rather than flipping to a stronger
        # status than the evidence supports: with a source it is a single-source assertion
        # (Layer V's cross-class rule will grade it), without one it is unverified.
        c.status = "confirmed" if c.source_url else "could_not_verify"
        downgraded.append((c.question_id or c.field_name or "?", str(c.answer or "")[:120]))
    return downgraded


# Values the compress model emits as a stand-in for "I have no source class" — they are
# truthy strings, so a plain falsy check treats them as a real classification and skips the
# gap tagging entirely. Observed live: a lane whose only tool returned nothing produced five
# could_not_verify claims all carrying source_class="unknown", which silently destroyed the
# tool_unavailable vs no_evidence_found distinction (2026-08-12).
_PLACEHOLDER_SOURCE_CLASSES = frozenset({"unknown", "none", "null", "n/a", "na", "-", ""})


def _tag_evidence_gaps(claims: list[Claim], had_real_evidence: bool) -> None:
    """Distinguish "the tool was broken/empty" from "genuinely no evidence exists" on
    could_not_verify claims the LLM left unclassified.

    Overwrites a falsy source_class OR one of the placeholder strings above, but never a
    real value the LLM assigned (e.g. "uncompressed_notes" on the parse-failure fallback
    path). This distinction is the only signal that separates "our tooling is down" from
    "this firm has no public footprint", so letting a placeholder win makes an outage look
    like a finding."""
    gap_class = "no_evidence_found" if had_real_evidence else "tool_unavailable"
    for c in claims:
        if c.status != "could_not_verify":
            continue
        current = (c.source_class or "").strip().lower()
        if current in _PLACEHOLDER_SOURCE_CLASSES:
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
            downgraded = _downgrade_unsupported_contradictions(claims)
            _tag_evidence_gaps(claims, state["had_real_evidence"])
            event = trace_event(
                "compress",
                "compress_output",
                lane=lane,
                attempt=attempt,
                raw_response=response.content,
                claims=[c.model_dump(mode="json") for c in claims],
                # Never silent: every downgraded contradiction is recorded so a real
                # conflict the model failed to cite properly is still recoverable from
                # the trace rather than lost.
                downgraded_contradictions=downgraded,
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

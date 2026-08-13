"""Verdict node — plan §4.6. Replaces ODR's final_report_generation. Hybrid: HARD gates
are evaluated mechanically from claim statuses first, with no LLM in that path and no way
for the LLM pass to override a code-decided reject. Only survivors reach the LLM, which
weighs SOFT-gate quality into pursue vs pursue_low. Every outcome — reject included —
gets written with its full ClaimLedger; nothing is discarded (plan §2)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from app.config import SETTINGS
from app.db import upsert_claims, write_decision, write_lead_trace, write_rejection
from app.llm import get_model
from app.questions import HARD_GATE_QUESTION_IDS, QUESTIONS_BY_ID
from app.state import Claim, SupervisorState, claims_by_question, trace_event


def evaluate_hard_gates(claims: list[dict[str, Any]]) -> dict[str, Any]:
    """Code-first, deterministic. Returns:
    {"reject": bool, "reason_code": str|None, "force_low": bool, "checks": {...}, "labels": [...]}
    `force_low` covers on_unknown="deprioritize" — the lead survives but is capped at
    pursue_low regardless of what the SOFT-gate LLM pass would otherwise conclude, since
    that policy exists specifically to keep judgment out of an unresolved HARD question."""
    by_q = claims_by_question(claims)
    reject_reasons: list[str] = []
    labels: list[str] = []
    force_low = False
    checks: dict[str, Any] = {}

    for qid in sorted(HARD_GATE_QUESTION_IDS):
        q = QUESTIONS_BY_ID[qid]
        claim = by_q.get(qid)
        if claim is not None and claim.status == "contradicted":
            reject_reasons.append(f"{qid}:contradicted")
            checks[qid] = "contradicted"
        elif claim is None or claim.status == "could_not_verify":
            checks[qid] = "unanswered"
            if q.on_unknown == "reject":
                reject_reasons.append(f"{qid}:unanswered")
            elif q.on_unknown == "deprioritize":
                force_low = True
                labels.append(f"{qid}: unanswered, deprioritized per policy")
            else:  # ship_with_label
                labels.append(f"{qid}: unanswered, shipped with label per policy")
        else:
            checks[qid] = "confirmed"

    if reject_reasons:
        return {
            "reject": True,
            "reason_code": ";".join(sorted(reject_reasons)),
            "force_low": force_low,
            "checks": checks,
            "labels": labels,
        }
    return {"reject": False, "reason_code": None, "force_low": force_low, "checks": checks, "labels": labels}


def compute_dead_ends(claims: list[dict[str, Any]]) -> list[str]:
    """Questions research attempted but never resolved — layer 2 shouldn't re-walk these."""
    by_q = claims_by_question(claims)
    return [
        f"{qid}: {claim.answer}"
        for qid, claim in sorted(by_q.items())
        if claim.status == "could_not_verify"
    ]


def compute_thin_reason(claims: list[dict[str, Any]]) -> str:
    """Seam A (enrichment_validation_dataset_plan.md §3): only meaningful on
    pursue_low. "structural" — no decision-maker findable (G2.Q1), no reachability
    footprint at all (G2.Q2), AND no capital-deployment evidence (G3.Q1) — enrichment's
    tiered contact resolution (wave 1) has nothing to build on, and V6 completeness will
    reject it regardless, so spending wave-2 budget on it is pure waste. "fixable" —
    everything else: a missing AUM/thesis/signal that wave -1/1 enrichment can plausibly
    fill without needing to invent a decision-maker out of nothing."""
    by_q = claims_by_question(claims)

    def _unresolved(qid: str) -> bool:
        c = by_q.get(qid)
        return c is None or c.status == "could_not_verify"

    no_decision_maker = _unresolved("G2.Q1")
    no_channel = _unresolved("G2.Q2")
    no_deploy_evidence = _unresolved("G3.Q1")

    if no_decision_maker and no_channel and no_deploy_evidence:
        return "structural"
    return "fixable"


_LLM_VERDICT_SYSTEM = (
    "You are the final judgment pass for a family-office lead that has already cleared "
    "every HARD gate. Weigh the SOFT-gate evidence — capital deployment plausibility, "
    "reachability strength, signs of life — and decide pursue vs pursue_low.\n"
    "Respond with ONLY a JSON object: {{\"verdict\": \"pursue\"|\"pursue_low\", "
    "\"rationale\": \"<one paragraph>\"}}. No markdown fences, no extra keys.\n\n"
    "Labels/caveats to weigh in (unresolved HARD questions shipped under policy, if any):\n"
    "{labels}\n"
)


def _parse_verdict_json(text: str) -> dict[str, str]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    data = json.loads(text)
    if data.get("verdict") not in ("pursue", "pursue_low"):
        raise ValueError(f"invalid verdict value: {data.get('verdict')!r}")
    if not isinstance(data.get("rationale"), str):
        raise ValueError("rationale must be a string")
    return data


async def _llm_soft_gate_pass(
    claims: list[dict[str, Any]], labels: list[str]
) -> tuple[dict[str, str], float]:
    model = get_model(SETTINGS.models.verdict_tier)
    system = _LLM_VERDICT_SYSTEM.format(labels="\n".join(labels) or "(none)")
    ledger_text = json.dumps(claims, default=str, indent=2)

    cost = 0.0
    for attempt in range(2):
        response = await model.ainvoke(
            [SystemMessage(content=system), HumanMessage(content=f"ClaimLedger:\n{ledger_text}")]
        )
        cost += response.response_metadata.get("cost_usd", 0.0)
        try:
            return _parse_verdict_json(str(response.content)), cost
        except (json.JSONDecodeError, ValidationError, ValueError):
            if attempt == 0:
                continue
    # Never fabricate confidence we don't have — default to the conservative outcome.
    return {
        "verdict": "pursue_low",
        "rationale": "LLM verdict pass failed to produce valid structured output twice; "
        "defaulted to pursue_low as the conservative outcome.",
    }, cost


def _timed_out_lanes(state: SupervisorState) -> list[str]:
    """Lanes that hit the hard per-lane timeout, read from the trace the lanes already
    write (`app.researcher.run_researcher_lane` emits a `lane_timeout` event). Reading the
    trace rather than threading a new counter through SupervisorState keeps this to one
    module — the evidence is already recorded and already persisted."""
    return [
        e.get("lane")
        for e in (state.get("trace") or [])
        if e.get("event") == "lane_timeout" and e.get("lane")
    ]


def _rejected_only_because_research_never_ran(
    state: SupervisorState, gates: dict[str, Any]
) -> bool:
    """True when the HARD-gate rejection is attributable to timed-out research rather than
    to anything learned about the firm.

    Deliberately narrow, because a false positive here means shipping a lead that should
    have been rejected — the failure this pipeline exists to prevent. All three must hold:

    * at least one lane timed out;
    * NO claim was produced at all (a lane that timed out after answering something did
      real work, and whatever it found should stand on its own);
    * every rejection reason is an `:unanswered` one. A `:contradicted` or a
      `V5_firm_is_fo` reason is a substantive finding and must still reject, even if a
      different lane happened to time out.
    """
    if not _timed_out_lanes(state):
        return False
    if state.get("claims"):
        return False
    reasons = [r for r in (gates.get("reason_code") or "").split(";") if r]
    return bool(reasons) and all(r.endswith(":unanswered") for r in reasons)


async def run_verdict(state: SupervisorState) -> dict[str, Any]:
    claims = state["claims"]
    gates = evaluate_hard_gates(claims)
    dead_ends = compute_dead_ends(claims)

    if gates["reject"]:
        if _rejected_only_because_research_never_ran(state, gates):
            # An infrastructure failure, not a finding. Every lane hit
            # SETTINGS.researcher.timeout_seconds and returned zero claims, so no question
            # was ever researched — "unanswered" here means "we never looked", not "we
            # looked and could not confirm". Recording a rejection would file the lead as
            # judged, permanently, at ~$0 and invisibly: the reason_code reads exactly like
            # a considered verdict. Observed 2026-08-14 on 11 of 30 leads in one batch,
            # BILTMORE FAMILY OFFICE (CRD 167174, $3.47B RAUM) among them, whose recorded
            # finding was that we could not establish it exists.
            event = trace_event(
                "verdict",
                "retry_research_incomplete",
                reason_code=gates["reason_code"],
                gate_results=gates,
                lanes_timed_out=_timed_out_lanes(state),
            )
            return {
                "verdict": None,
                "retry_reason": "lane_timeout",
                "gate_results": gates,
                "rationale": None,
                "dead_ends": dead_ends,
                "trace": [event],
            }
        event = trace_event("verdict", "code_gate_reject", reason_code=gates["reason_code"], gate_results=gates)
        return {
            "verdict": "reject",
            "gate_results": gates,
            "rationale": f"Rejected on HARD gate(s): {gates['reason_code']}",
            "dead_ends": dead_ends,
            "trace": [event],
        }

    llm_result, verdict_cost = await _llm_soft_gate_pass(claims, gates["labels"])
    final_verdict = "pursue_low" if gates["force_low"] else llm_result["verdict"]
    thin_reason = compute_thin_reason(claims) if final_verdict == "pursue_low" else None
    event = trace_event(
        "verdict",
        "verdict_output",
        llm_verdict=llm_result["verdict"],
        final_verdict=final_verdict,
        force_low=gates["force_low"],
        rationale=llm_result["rationale"],
        thin_reason=thin_reason,
    )
    return {
        "verdict": final_verdict,
        "gate_results": gates,
        "rationale": llm_result["rationale"],
        "dead_ends": dead_ends,
        "thin_reason": thin_reason,
        "cost_usd": state["cost_usd"] + verdict_cost,
        "trace": [event],
    }


def persist_verdict(conn: sqlite3.Connection, state: SupervisorState) -> None:
    entity_id = state["entity_id"]
    claims = state["claims"]
    # claims table is the durable spine (enrichment_validation_dataset_plan.md §2/§7) —
    # every claim in the ledger gets a row here in addition to the JSON blob embedded in
    # decisions/rejections below, so Layer E/V/D never has to re-derive provenance from
    # a decisions row's blob.
    upsert_claims(conn, entity_id, claims)
    if state.get("retry_reason"):
        # No verdict was reached — research never completed. Persist the claims and the
        # trace (the evidence of WHY it is being retried) but write neither a decision nor
        # a rejection: an unassessed lead must not leave a record that looks assessed. The
        # runner reads `retry_reason` off the returned state and checkpoints accordingly.
        write_lead_trace(conn, entity_id, state["trace"])
        return
    if state["verdict"] == "reject":
        write_rejection(
            conn,
            entity_id,
            reason_code=state["gate_results"]["reason_code"],
            gate_results=state["gate_results"],
            claim_ledger=claims,
        )
    else:
        write_decision(
            conn,
            entity_id,
            verdict=state["verdict"],
            rationale=state["rationale"] or "",
            gate_results=state["gate_results"] or {},
            claim_ledger=claims,
            dead_ends=state["dead_ends"],
            thin_reason=state.get("thin_reason"),
        )
    write_lead_trace(conn, entity_id, state["trace"])


async def verdict_node(state: SupervisorState, *, conn: sqlite3.Connection) -> dict[str, Any]:
    """LangGraph node entrypoint; `conn` bound via functools.partial in app/graph.py."""
    result = await run_verdict(state)
    # `result["trace"]` (if present) is only the *new* verdict-phase event(s) — that's
    # what the graph engine's reducer expects back from a node. For persistence here we
    # need the full history, so concatenate manually rather than let the **result
    # shortcut below silently replace state["trace"] with just the new entries.
    full_trace = state["trace"] + result.get("trace", [])
    merged: SupervisorState = {**state, **result, "trace": full_trace}  # type: ignore[assignment]
    persist_verdict(conn, merged)
    return result

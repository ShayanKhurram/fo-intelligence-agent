"""State models — plan §3. Pydantic for data crossing node boundaries (validated,
serializable to the DB); TypedDict + reducers for the LangGraph state boxes themselves,
mirroring ODR's AgentState / SupervisorState / ResearcherState split."""
from __future__ import annotations

import operator
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

Lane = Literal["identity_and_type", "people", "activity_signals"]
GateKind = Literal["HARD", "SOFT"]
OnUnknownPolicy = Literal["reject", "ship_with_label", "deprioritize"]
# Layer 1 (parser/researcher) only ever produces "confirmed" / "could_not_verify" /
# "contradicted" — that vocabulary is unchanged, deliberately, to avoid touching any
# tested Layer-1 code (see enrichment_validation_dataset_plan.md's deviation note in
# PROJECT_LOG.md). The remaining values are what Layer V (validation) writes when it
# annotates a claim: "verified" (cross-class corroborated), "single_source" (confirmed
# but only ever seen from one source_class), "pattern_inferred" (format-plausible but
# unconfirmed, e.g. an email guessed from a known pattern), "format_only" (passes a
# format check only, e.g. phone number shape), "removed_failed_validation" (release
# rule killed it — value blanked, see app/validation.py).
ClaimStatus = Literal[
    "confirmed",
    "could_not_verify",
    "contradicted",
    "verified",
    "single_source",
    "pattern_inferred",
    "format_only",
    "removed_failed_validation",
    # A wave -1 `produced_by="derived"` claim that a force=True re-run is about to
    # regenerate from (possibly corrected) entity_sources. Stamped on the OLD claim
    # before the fresh one is derived, so the two never sit side by side as equal,
    # live facts. Not a rejection or a blank — the value and its history are kept, it is
    # just no longer authoritative. Added 2026-08-12 after a 13F-quarter data backfill:
    # without it, force-reprocessing left both the stale and corrected `aum_as_of`
    # claims "confirmed"/"contradicted" side by side, and V4 correctly-but-uselessly
    # flagged its OWN prior output as contradicting itself — every one of that run's 38
    # rejects carried this artifact (see PROJECT_LOG.md).
    "superseded",
]
Confidence = Literal["high", "medium", "low"]
Verdict = Literal["pursue", "pursue_low", "reject"]
ThinReason = Literal["fixable", "structural"]
ProducedBy = Literal["parser", "research", "enrichment", "derived"]
Wave = Literal["-1", "0", "1", "2"]

LANES: tuple[Lane, ...] = ("identity_and_type", "people", "activity_signals")


class Claim(BaseModel):
    claim_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    question_id: str | None = None  # e.g. "G1.Q3" (gate.question) — layer-1 battery claims
    field_name: str | None = None  # join key to the dataset schema — enrichment/derived claims
    answer: Any = None
    # The bare value the answer asserts, when the answer is about a named subject a
    # later wave needs to read without the question text (PLAN.md T19). E.g. G2.Q1's
    # answer restates "Matt Blackburn is Managing Director..."; subject_value carries
    # just "Matt Blackburn". None on every historical claim and on any question_id the
    # compress step does not explicitly fill — see app/researcher.py._COMPRESS_SYSTEM_TEMPLATE.
    subject_value: str | None = None
    status: ClaimStatus
    source_url: str | None = None
    source_class: str | None = None
    extraction_method: str | None = None  # e.g. "httpx_trafilatura" | "serper_scrape" | "derived_13f" | "jsonld"
    retrieved_at: datetime | None = None
    confidence: Confidence
    produced_by: ProducedBy = "research"
    wave: Wave | None = None

    # written by validation only, never by producers (app/validation.py)
    verification_method: str | None = None
    confirming_url: str | None = None
    confirming_class: str | None = None
    verified_at: datetime | None = None


# Selection rank for `select_claim_for_question` (PLAN.md T23.2, corrected by T24.1).
# `contradicted` means "this specific claim's own page does not bear it out" — a claim
# actively refuted by its own source. `could_not_verify` means "we don't know". Both are
# weak answers and either loses to a `confirmed` sibling, but a refuted claim ranks below
# one that simply could not be checked — hence `could_not_verify` (5) < `contradicted` (6).
# A question whose only claim is `contradicted` is still returned: selection picks a
# winner, it never filters.
_CLAIM_STATUS_RANK: dict[str, int] = {
    "verified": 0,
    "confirmed": 1,
    "single_source": 2,
    "pattern_inferred": 3,
    "format_only": 4,
    "could_not_verify": 5,
    "contradicted": 6,
    "superseded": 7,
    "removed_failed_validation": 8,
}


def select_claim_for_question(claims: list[Claim]) -> Claim | None:
    """Deterministic winner among several claims for the same question_id (PLAN.md T23.2).

    A lane may now legitimately emit several claims for one question (one per supporting
    page — see `_COMPRESS_SYSTEM_TEMPLATE`), so the gates and the projection need a single
    canonical claim per question, picked by a rule that NEVER depends on input list order.

    Precedence, in order (T24.1: `contradicted` no longer wins outright — see the
    rationale on `_CLAIM_STATUS_RANK`. A `contradicted` claim means *this particular
    fact* is not supported by *its own* cited page, which says nothing about a sibling
    claim answering the same question from a different, well-supported source):
    1. the strongest status: `verified` > `confirmed` > `single_source` >
       `pattern_inferred` > `format_only` > `could_not_verify` > `contradicted` >
       `superseded` > `removed_failed_validation`;
    2. tie-break on the most recent `retrieved_at` (a `None` timestamp sorts oldest);
    3. final tie-break on `claim_id` string order, so the result is total.

    Selection picks a winner; it never filters. A question whose only claim is
    `contradicted` still returns that claim.
    """
    if not claims:
        return None

    def _key(c: Claim) -> tuple[int, int, float, str]:
        # Status rank drives selection (T24.1: no contradicted-wins special case).
        rank = _CLAIM_STATUS_RANK.get(c.status, 99)
        # Most recent retrieved_at first: a real timestamp negates to a negative number
        # that sorts earlier; None sorts oldest (last) via the leading 1.
        if c.retrieved_at is not None:
            ra: tuple[int, float] = (0, -c.retrieved_at.timestamp())
        else:
            ra = (1, 0.0)
        return (rank, ra[0], ra[1], c.claim_id)

    return min(claims, key=_key)


def claims_by_question(claims: list[dict[str, Any]]) -> dict[str, Claim]:
    """Group claims by question_id and keep ONE deterministic winner per question
    (PLAN.md T23.2) via `select_claim_for_question`. Replaces the old silent
    last-write-wins assignment, which made gate outcomes depend on list ordering — a
    live bug now that the compress step may emit several claims per question.

    Accepts the dict form claims are carried in as graph state (Claim.model_dump()).
    The single shared implementation lives here so the gates (`verdict.py`) and the
    supervisor (`supervisor.py`) can never drift on the grouping/selection rule.
    """
    grouped: dict[str, list[Claim]] = {}
    for c in claims:
        claim = Claim(**c)
        grouped.setdefault(claim.question_id, []).append(claim)
    return {qid: select_claim_for_question(group) for qid, group in grouped.items()}


class QuestionSpec(BaseModel):
    question_id: str
    text: str
    lane: Lane
    gate: GateKind
    on_unknown: OnUnknownPolicy


class LeadBudget(BaseModel):
    max_tool_calls: int = 8
    max_iterations: int = 2
    per_lane_cap: int = 5
    max_usd: float = 0.50


class LeadBrief(BaseModel):
    entity_id: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    injected_facts: dict[str, Any] = Field(default_factory=dict)
    unverified_leads: list[dict[str, Any]] = Field(default_factory=list)
    questions: list[QuestionSpec] = Field(default_factory=list)
    budget: LeadBudget = Field(default_factory=LeadBudget)


def _merge_lane_counts(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    merged = dict(left)
    for lane, count in right.items():
        merged[lane] = merged.get(lane, 0) + count
    return merged


def _merge_url_provenance(left: dict[str, str], right: dict[str, str]) -> dict[str, str]:
    """Merge per-URL retrieval-tool maps across researcher supersteps. A later write may
    refresh the tool recorded for a URL it actually saw, but must not clobber an earlier
    entry for a *different* URL — plain dict.update gives exactly that (T25.1)."""
    merged = dict(left)
    merged.update(right)
    return merged


def trace_event(phase: str, event: str, **fields: Any) -> dict[str, Any]:
    """One entry in the full reasoning/tool-call trace persisted to `lead_traces`
    (app/db.py). Not part of the original plan's state models — added because none of
    a lead's internal reasoning was recoverable after the graph run finished; see
    PROJECT_LOG.md's 2026-07-28 trace-capture entry.

    `phase` is one of "supervisor" | "researcher" | "researcher_tool" | "compress" |
    "verdict". `event` names what happened within that phase (e.g. "think",
    "dispatch", "ai_message", "tool_call", "tool_result", "compress_output",
    "verdict_output"). Everything else is free-form per event type — this is a debug
    trace, not a contract other code should parse."""
    return {"ts": utcnow().isoformat(), "phase": phase, "event": event, **fields}


class ResearcherState(TypedDict):
    """Per-lane, isolated context — raw notes never cross back to the supervisor."""

    lane: Lane
    instructions: str
    lead_brief_slim: dict[str, Any]  # name, aliases, injected facts only
    researcher_messages: Annotated[list, add_messages]
    raw_notes: Annotated[list[str], operator.add]
    tool_calls_used: int
    had_real_evidence: bool  # True once any tool result produced usable content
    claims: Annotated[list[dict[str, Any]], operator.add]  # Claim.model_dump()
    url_provenance: Annotated[dict[str, str], _merge_url_provenance]  # {url: tool_name} (T25.1)
    lane_status: str  # "ok" | "capped" | "failed"
    cost_usd: float
    trace: Annotated[list[dict[str, Any]], operator.add]  # trace_event() dicts


class SupervisorState(TypedDict):
    """Top-level graph state for one lead: Parser -> supervisor loop -> Verdict."""

    entity_id: str
    lead_brief: LeadBrief | None
    supervisor_messages: Annotated[list, add_messages]
    lanes_dispatched: Annotated[dict[str, int], _merge_lane_counts]
    claims: Annotated[list[dict[str, Any]], operator.add]  # Claim.model_dump()
    calls_spent: int
    iterations: int
    cost_usd: float
    research_complete: bool
    trace: Annotated[list[dict[str, Any]], operator.add]  # trace_event() dicts
    # Verdict output
    verdict: Verdict | None
    gate_results: dict[str, Any] | None
    rationale: str | None
    dead_ends: list[str]
    thin_reason: ThinReason | None  # set only on pursue_low — see verdict.compute_thin_reason
    # Set when the HARD gates would reject purely because research never completed (every
    # lane hit SETTINGS.researcher.timeout_seconds and returned zero claims). That is an
    # infrastructure failure, not a finding about the firm, so no verdict is recorded and
    # the lead is re-queued instead. Observed 2026-08-14: 11 of 30 leads in one batch were
    # rejected "G1.Q1:unanswered;G1.Q3:unanswered" after 3/3 lanes timed out — including
    # BILTMORE FAMILY OFFICE (CRD 167174, $3.47B RAUM), whose recorded verdict was that we
    # could not establish it exists. See app.verdict.run_verdict.
    retry_reason: str | None


def new_supervisor_state(entity_id: str) -> SupervisorState:
    return SupervisorState(
        entity_id=entity_id,
        lead_brief=None,
        supervisor_messages=[],
        lanes_dispatched={},
        claims=[],
        calls_spent=0,
        iterations=0,
        cost_usd=0.0,
        research_complete=False,
        trace=[],
        verdict=None,
        gate_results=None,
        rationale=None,
        dead_ends=[],
        thin_reason=None,
        retry_reason=None,
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================================
# Seam contracts — enrichment_validation_dataset_plan.md §3. Each is the exact payload
# handed from one layer's orchestrator to the next (app/graph.py wires layer 1 -> E;
# app/enrichment.py -> V; app/validation.py -> D). Pydantic, not TypedDict: these cross
# a layer boundary and should validate on the way in, unlike the in-graph state boxes
# above which only ever move within one graph's own supersteps.
# ============================================================================


class Finding(BaseModel):
    """One deterministic-check result (wave 0 gate or Layer V check). Mirrors the
    `findings` table (app/schema.sql)."""

    check_id: str  # e.g. "V4_contradiction", "V5_staleness", "V6_completeness"
    severity: Literal["fatal", "warn", "info"]
    detail: str = ""
    claim_id: str | None = None
    field: str | None = None
    evidence_url: str | None = None


class FieldStatus(BaseModel):
    """Per-field verification state, mirrors the `field_status` table."""

    field: str
    status: ClaimStatus
    method: str | None = None
    confirming_url: str | None = None
    confirming_class: str | None = None
    last_checked: datetime | None = None


class ChainStep(BaseModel):
    """One hop in a claim's provenance chain (originating source -> confirming source),
    mirrors the `chain_steps` table. This is the "how do we know this" deliverable."""

    step_no: int
    claim: str
    originating_class: str | None = None
    originating_url: str | None = None
    confirming_class: str | None = None
    confirming_url: str | None = None
    method: str | None = None
    result: str | None = None


class BudgetRecord(BaseModel):
    """Credit/USD spend snapshot handed from Enrichment to Validation so V1's ring-fence
    (research_layer_plan.md §4.7 global pool) can see what enrichment already spent."""

    calls_spent: int = 0
    usd_spent: float = 0.0
    serper_calls_spent: int = 0
    hunter_credits_spent: int = 0


class EnrichmentInput(BaseModel):
    """SEAM A — Verdict -> Enrichment."""

    entity_id: str
    verdict: Literal["pursue", "pursue_low"]
    thin_reason: ThinReason | None = None
    claim_ledger: list[Claim] = Field(default_factory=list)
    injected_facts: dict[str, Any] = Field(default_factory=dict)
    dead_ends: list[str] = Field(default_factory=list)
    triage_score: int = 0
    discovery_classes: list[str] = Field(default_factory=list)


class ValidationInput(BaseModel):
    """SEAM B — Enrichment -> Validation."""

    entity_id: str
    claim_ledger: list[Claim] = Field(default_factory=list)
    waves_completed: list[str] = Field(default_factory=list)
    wave0_findings: list[Finding] = Field(default_factory=list)
    budget_spent: BudgetRecord = Field(default_factory=BudgetRecord)


class DatasetInput(BaseModel):
    """SEAM C — Validation -> Dataset."""

    entity_id: str
    outcome: Literal["ship", "ship_with_caveats", "reject"]
    claim_ledger: list[Claim] = Field(default_factory=list)
    field_statuses: list[FieldStatus] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    chain: list[ChainStep] = Field(default_factory=list)
    type_final: Literal["SFO", "MFO", "type_unconfirmed"] = "type_unconfirmed"

"""The fixed question battery (plan §1, §4.4). One frozen list — the supervisor dispatches
lanes, never invents new questions. Each QuestionSpec's `gate` decides whether Verdict's
code-first pass can reject on it; `on_unknown` decides what happens when a HARD gate is
never answered (see app/verdict.py)."""
from __future__ import annotations

from app.state import QuestionSpec

QUESTION_BATTERY: list[QuestionSpec] = [
    # --- identity_and_type (G1): does this entity exist and qualify as a family office? ---
    QuestionSpec(
        question_id="G1.Q1",
        text="Does the entity exist and is it findable under its known name/aliases?",
        lane="identity_and_type",
        gate="HARD",
        on_unknown="reject",
    ),
    QuestionSpec(
        question_id="G1.Q2",
        text="What is the entity's current legal name (has it renamed/merged)?",
        lane="identity_and_type",
        gate="HARD",
        on_unknown="ship_with_label",
    ),
    QuestionSpec(
        question_id="G1.Q3",
        text="Is there affirmative evidence the entity operates as a family office "
        "(not just a discovery-stage signal)?",
        lane="identity_and_type",
        gate="HARD",
        on_unknown="reject",
    ),
    QuestionSpec(
        question_id="G1.Q4",
        text="Is it a single-family office (SFO) or multi-family office (MFO)?",
        lane="identity_and_type",
        gate="SOFT",
        on_unknown="ship_with_label",
    ),
    QuestionSpec(
        question_id="G1.Q5",
        text="Is the entity a plain RIA-in-costume rather than an actual family office?",
        lane="identity_and_type",
        gate="HARD",
        on_unknown="deprioritize",
    ),
    QuestionSpec(
        question_id="G1.Q6",
        text="Is the entity active (not defunct/dissolved/inactive)?",
        lane="identity_and_type",
        gate="HARD",
        on_unknown="deprioritize",
    ),
    # --- people (G2): is there a reachable decision-maker? ---
    QuestionSpec(
        question_id="G2.Q1",
        text="Is the firm's MOST SENIOR named decision-maker identified? Name the "
        "CEO if the firm has one, otherwise the Managing Partner, Managing Member, "
        "Founder, President, CIO, Managing Director, Principal, Partner, COO, or CFO — "
        "in that priority order. Report the single most senior person found.",
        lane="people",
        gate="HARD",
        on_unknown="deprioritize",
    ),
    QuestionSpec(
        question_id="G2.Q2",
        text="Is that person findable at all (any public professional footprint)?",
        lane="people",
        gate="SOFT",
        on_unknown="ship_with_label",
    ),
    QuestionSpec(
        question_id="G2.Q3",
        text="What is the EXACT title/role of the person named in G2.Q1, as stated on "
        "the same page that named them? Report the title verbatim from that page.",
        lane="people",
        gate="SOFT",
        on_unknown="ship_with_label",
    ),
    # --- activity_signals (G3): are there signs of life? ---
    QuestionSpec(
        question_id="G3.Q1",
        text="Is there evidence the entity deploys capital (investments/allocations)?",
        lane="activity_signals",
        gate="SOFT",
        on_unknown="ship_with_label",
    ),
    QuestionSpec(
        question_id="G3.Q2",
        text="Are there recent exits/hires/commitments within the lookback window?",
        lane="activity_signals",
        gate="SOFT",
        on_unknown="ship_with_label",
    ),
    # G3.Q3 ("does a scandal/enforcement-action check come back clean?") was REMOVED
    # 2026-08-12. It was nominally a HARD gate but had no gating power in practice:
    #   * on_unknown="ship_with_label" meant an unanswered check never rejected, and
    #   * evaluate_hard_gates tests claim *status* only, so a settled NEGATIVE answer
    #     ("No — a class-action was filed over a data breach") came back status="confirmed"
    #     and passed the gate too. Observed live on PATHSTONE FAMILY OFFICE.
    # So it could not reject anything, while costing the activity lane several web_search
    # calls per lead chasing scandal coverage. Dropped rather than repaired.
    #
    # If scandal screening is wanted back, the cheap route is mechanical, not another
    # question: app/tools/adv.py's `adv_lookup` already returns `has_disclosure_events`
    # (IAPD's regulatory-disclosure flag) for free, inside a call the identity lane makes
    # anyway. That is a structured fact, not an LLM judgement over news coverage.
]

QUESTIONS_BY_LANE: dict[str, list[QuestionSpec]] = {}
for _q in QUESTION_BATTERY:
    QUESTIONS_BY_LANE.setdefault(_q.lane, []).append(_q)

QUESTIONS_BY_ID: dict[str, QuestionSpec] = {q.question_id: q for q in QUESTION_BATTERY}

HARD_GATE_QUESTION_IDS: set[str] = {
    q.question_id for q in QUESTION_BATTERY if q.gate == "HARD"
}

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.state import Claim, LeadBrief, LeadBudget, new_supervisor_state


def test_claim_requires_valid_status():
    with pytest.raises(ValidationError):
        Claim(question_id="G1.Q1", answer="x", status="maybe", confidence="high")


def test_claim_valid():
    c = Claim(question_id="G1.Q1", answer="exists", status="confirmed", confidence="high")
    assert c.source_url is None
    assert c.retrieved_at is None


def test_lead_brief_defaults():
    brief = LeadBrief(entity_id="E1", canonical_name="Acme")
    assert brief.aliases == []
    assert brief.questions == []
    assert isinstance(brief.budget, LeadBudget)
    assert brief.budget.max_tool_calls == 8


def test_new_supervisor_state_defaults():
    state = new_supervisor_state("E1")
    assert state["entity_id"] == "E1"
    assert state["lead_brief"] is None
    assert state["claims"] == []
    assert state["calls_spent"] == 0
    assert state["cost_usd"] == 0.0
    assert state["research_complete"] is False
    assert state["verdict"] is None

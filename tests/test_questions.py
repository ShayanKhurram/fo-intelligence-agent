from __future__ import annotations

from app.questions import HARD_GATE_QUESTION_IDS, QUESTION_BATTERY, QUESTIONS_BY_ID, QUESTIONS_BY_LANE
from app.state import LANES


def test_question_ids_unique():
    ids = [q.question_id for q in QUESTION_BATTERY]
    assert len(ids) == len(set(ids))


def test_every_question_maps_to_a_known_lane():
    for q in QUESTION_BATTERY:
        assert q.lane in LANES


def test_questions_by_lane_covers_all_lanes():
    assert set(QUESTIONS_BY_LANE.keys()) == set(LANES)
    total = sum(len(v) for v in QUESTIONS_BY_LANE.values())
    assert total == len(QUESTION_BATTERY)


def test_hard_gate_ids_subset_of_battery():
    assert HARD_GATE_QUESTION_IDS.issubset(QUESTIONS_BY_ID.keys())
    assert all(QUESTIONS_BY_ID[qid].gate == "HARD" for qid in HARD_GATE_QUESTION_IDS)


def test_on_unknown_policy_is_valid_for_every_question():
    for q in QUESTION_BATTERY:
        assert q.on_unknown in ("reject", "ship_with_label", "deprioritize")

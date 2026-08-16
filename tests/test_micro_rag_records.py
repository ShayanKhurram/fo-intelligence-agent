"""T42.0 — the thesis and activity signals must reach the corpus.

First coverage ever for `micro_rag/ingest/build_records.py` and `micro_rag/ingest/chunks.py`.
`test_ingest.py` is the unrelated *discovery* ingest (`app/ingest.py`); `test_rag_sync.py`
stubs `build_record` out. Both ingest modules under test here have never had a test.

The bug these tests pin down: every record's `mandates` was `[]` and every `mandate` chunk
was identical boilerplate, while `provenance` held a settled `investing_thesis` for 55
records and `recent_investments` for 260. The signal existed; the mapping dropped it.

Offline by construction — no Postgres, no embeddings, no ingest. Just the pure mapping
functions and the chunk builder.
"""
from __future__ import annotations

import pytest

from micro_rag.ingest.build_records import build_record
from micro_rag.ingest.chunks import build_chunks


def _claim(
    field_name: str,
    answer,
    *,
    status: str = "single_source",
    question_id: str | None = None,
    retrieved_at: str | None = "2026-01-15T00:00:00Z",
) -> dict:
    return {
        "field_name": field_name,
        "answer": answer,
        "status": status,
        "question_id": question_id,
        "retrieved_at": retrieved_at,
    }


def _mandate_chunk(record: dict) -> str:
    return next(c["content"] for c in build_chunks(record) if c["facet"] == "mandate")


def _activity_chunk(record: dict) -> str:
    return next(c["content"] for c in build_chunks(record) if c["facet"] == "activity")


THESIS_A = "Backs seed-stage US industrial decarbonization companies with a hardware bent."
THESIS_B = "Late-stage growth checks into enterprise SaaS companies in North America."


def test_settled_investing_thesis_produces_nonempty_mandates():
    claims = [_claim("investing_thesis", THESIS_A)]
    record = build_record("e1", "Acme FO", "ship", claims, hq_state="CA")
    assert record["mandates"] == [THESIS_A]


def test_legacy_investing_mandates_still_produces_nonempty_mandates():
    # The CSV importer (ingest_csv.py) emits `investing_mandates`, not `investing_thesis`.
    # Falling back to that name keeps the CSV path working unchanged.
    claims = [_claim("investing_mandates", ["real estate", "technology"])]
    record = build_record("e1", "Acme FO", "ship", claims, hq_state="CA")
    assert record["mandates"] == ["real estate", "technology"]


def test_recent_investments_produces_a_real_activity_chunk():
    text = "Led a $20M Series B into a hydrogen steelmaker in Q1 2026."
    claims = [_claim("recent_investments", text)]
    record = build_record("e1", "Acme FO", "ship", claims, hq_state="CA")
    chunk = _activity_chunk(record)
    assert text in chunk
    # The bug: 339 records currently have the bare "<name> activity." stub with nothing
    # after it. The chunk must NOT be that stub.
    assert chunk != f"{record['entity_name']} activity."


def test_neither_thesis_nor_mandates_keeps_the_honest_sentence():
    claims = [_claim("principal_name", "Jane Doe")]
    record = build_record("e1", "Acme FO", "ship", claims, hq_state="CA")
    chunk = _mandate_chunk(record)
    assert chunk == (
        "Acme FO's investing mandate. No investing thesis or mandate details have been "
        "confirmed for this record."
    )


def test_recent_news_containing_principal_phone_raises():
    phone = "+1-415-555-0142"
    news = f"The firm just hired a new GC; reach the desk at {phone}."
    claims = [
        _claim("principal_phone", phone),
        _claim("recent_news", news),
    ]
    record = build_record("e1", "Acme FO", "ship", claims, hq_state="CA")
    # `_assert_no_contact_leak` must refuse to build the chunk rather than embedding the
    # phone in free prose pulled from the open web. This is the first time that guard is
    # load-bearing: `recent_news` is a plausible carrier for a phone number.
    with pytest.raises(ValueError):
        build_chunks(record)


def test_two_different_theses_produce_different_mandate_chunk_text():
    # The entire feature rests on the mandate chunk discriminating between records. Today
    # every record's mandate chunk is identical boilerplate, so this is the test that
    # would have caught the bug.
    rec_a = build_record("e1", "Acme FO", "ship", [_claim("investing_thesis", THESIS_A)], hq_state="CA")
    rec_b = build_record("e2", "Beta FO", "ship", [_claim("investing_thesis", THESIS_B)], hq_state="NY")
    assert _mandate_chunk(rec_a) != _mandate_chunk(rec_b)
    assert THESIS_A in _mandate_chunk(rec_a)
    assert THESIS_B in _mandate_chunk(rec_b)


def test_unreliable_investing_thesis_does_not_reach_mandates():
    # `_settled_value` filters `could_not_verify` / `removed_failed_validation` /
    # `contradicted`. A thesis the pipeline could not verify must NOT be published as a
    # confirmed mandate — that is the project's honesty rule, and bypassing the helper to
    # read the claim dict directly would break it.
    claims = [_claim("investing_thesis", THESIS_A, status="could_not_verify")]
    record = build_record("e1", "Acme FO", "ship", claims, hq_state="CA")
    assert record["mandates"] == []


def test_csv_mandate_tags_keep_the_focus_areas_label():
    # The CSV tag path (multi-element list) keeps the "Focus areas:" lexical anchor that
    # `lexicalRank`'s `plainto_tsquery` can hit. The thesis path emits prose bare instead.
    claims = [_claim("investing_mandates", ["real estate", "technology"])]
    record = build_record("e1", "Acme FO", "ship", claims, hq_state="CA")
    chunk = _mandate_chunk(record)
    assert "Focus areas:" in chunk


def test_thesis_without_ending_punctuation_still_terminates_the_sentence():
    # Every sentence in every chunk builder ends in a period; thesis prose that does not
    # must still terminate so the chunk reads as a closed sentence.
    thesis = "Backs seed-stage US industrial decarbonization companies with a hardware bent"
    claims = [_claim("investing_thesis", thesis)]
    record = build_record("e1", "Acme FO", "ship", claims, hq_state="CA")
    chunk = _mandate_chunk(record)
    assert thesis in chunk
    assert chunk.rstrip().endswith(".")
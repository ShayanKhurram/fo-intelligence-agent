"""T25.2/T25.3 — the V1 judge is a consistency check, not a world-adjudicator.

The model is stubbed (FakeChatModel) so no network is touched. These tests pin the new
`{"verdict": ...}` protocol shape and the severity mapping in `check_v1_source_supports_claim`,
plus the legacy `{"supported": bool}` fallback. The motivating failure (an SEC record reading
"Approved" fatalled a claim saying "ACTIVE") is case (a) below.
"""
from __future__ import annotations

from langchain_core.messages import AIMessage

from app.state import Claim
from app.validation import check_v1_source_supports_claim


def _claim(**kw) -> Claim:
    defaults = dict(
        status="confirmed",
        confidence="medium",
        field_name="G1.Q6",
        answer="The entity is active, with registration status ACTIVE and recent website activity in 2026",
        source_url="https://adviserinfo.sec.gov/firm/summary/151736",
        extraction_method="edgar_search",
    )
    defaults.update(kw)
    return Claim(**defaults)


# (a) Approved-page vs ACTIVE-claim -> supported -> info. This is the regression that
# prompted T25: the judge decided "Approved" is not "ACTIVE" and demanded website-activity
# proof, neither of which came from the page.
async def test_v1_approved_page_vs_active_claim_is_supported(fake_model):
    fake_model.queue(AIMessage(content='{"verdict": "supported", "reason": "Approved is an active registration label"}'))
    claim = _claim()
    finding, _ = await check_v1_source_supports_claim(claim, "Registration Status: Approved", fake_model)
    assert finding.severity == "info"
    assert finding.check_id == "V1_source_supports"


# (b) dissolved-company page vs active claim -> contradicted -> fatal.
async def test_v1_dissolved_page_vs_active_claim_is_contradicted(fake_model):
    fake_model.queue(AIMessage(content='{"verdict": "contradicted", "reason": "page states the firm dissolved in 2019"}'))
    claim = _claim(answer="The entity is active")
    finding, _ = await check_v1_source_supports_claim(
        claim, "The firm was dissolved in 2019 and no longer operates.", fake_model
    )
    assert finding.severity == "fatal"


# (c) unrelated-company page -> not_stated -> warn, AND the claim's status is unchanged
# (only fatal may flip a claim to contradicted downstream).
async def test_v1_unrelated_page_is_warn_and_leaves_claim_status_untouched(fake_model):
    fake_model.queue(AIMessage(content='{"verdict": "not_stated", "reason": "page is about an unrelated company"}'))
    claim = _claim(answer="The entity is active", status="confirmed")
    finding, _ = await check_v1_source_supports_claim(
        claim, "Acme Corp is a manufacturer of widgets based in Ohio.", fake_model
    )
    assert finding.severity == "warn"
    assert "claim unproven, not disproven" in finding.detail
    assert claim.status == "confirmed", "not_stated must not flip the claim's status"


# (d) the human message carries `Retrieved via:` and the plain answer text (no Python
# repr quoting).
async def test_v1_human_message_carries_retrieval_method_and_plain_answer(fake_model):
    fake_model.queue(AIMessage(content='{"verdict": "supported", "reason": "ok"}'))
    answer = "The entity is active"
    claim = _claim(answer=answer, extraction_method="edgar_search")
    await check_v1_source_supports_claim(claim, "Registration Status: Approved", fake_model)

    messages = fake_model.calls[-1]
    human_text = messages[-1].content
    assert "Retrieved via: edgar_search" in human_text
    assert answer in human_text, "the plain answer text must appear in the human message"
    # repr() of the string adds surrounding single quotes — that is the old `!r` form
    # and must NOT appear.
    assert repr(answer) not in human_text, "answer must be str(), not !r"


# (e) legacy {"supported": false} -> warn, NOT fatal. An old-shaped reply where "false"
# is ambiguous between contradicted and not_stated must never produce a spurious fatal.
async def test_v1_legacy_supported_false_is_warn_not_fatal(fake_model):
    fake_model.queue(AIMessage(content='{"supported": false, "reason": "page never mentions this"}'))
    claim = _claim(answer="The entity is active")
    finding, _ = await check_v1_source_supports_claim(claim, "some page content here", fake_model)
    assert finding.severity == "warn"
    assert finding.severity != "fatal"


# Bonus: legacy {"supported": true} -> supported -> info (the symmetric half of the
# fallback, kept for completeness).
async def test_v1_legacy_supported_true_is_info(fake_model):
    fake_model.queue(AIMessage(content='{"supported": true, "reason": "page states this directly"}'))
    claim = _claim(answer="The entity is active")
    finding, _ = await check_v1_source_supports_claim(claim, "The entity is active and filing.", fake_model)
    assert finding.severity == "info"
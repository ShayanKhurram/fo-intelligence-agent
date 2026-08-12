"""T20 — GDELT noise regression. The four fixtures below are taken VERBATIM from the
row `CLASS VI FAMILY OFFICE, LLC` shipped on 2026-08-13, where four unrelated headlines
reached the deliverable as `status="confirmed"` facts:

    recent_investments        "Ginny Robinson"
    recent_news               "Ginny Robinson"
    recent_key_hires          "Newsom and Trump agree on something: Blame Wall Street..."
    recent_fund_commitments   "Family offices make opportunistic bets on real estate..."

None of those articles is about Class VI. T20 stops that two ways: (T20.1) an
entity-mention gate skips any result whose title/snippet/description doesn't actually
name the firm, and (T20.2) a news article may only populate `recent_news` — the
`recent_key_hires` / `recent_fund_commitments` / `recent_investments` write paths off
news results are deleted. These are the exact strings that reached a deliverable, so
they are the right regression corpus.
"""
from __future__ import annotations

import app.enrichment as enrichment_module
from app.enrichment import _find_dated_signal, _find_news_signals, _mentions_entity

ENTITY = "CLASS VI FAMILY OFFICE, LLC"
ALIASES: list[str] = []


def _patch_news(monkeypatch, *, gdelt_results, news_results=None):
    """Stub the two raw search entry points `app.enrichment` calls — same monkeypatch
    style as tests/test_enrichment.py's `_patch_wave1`/`_patch_wave2`."""
    async def _fake_gdelt(query, lookback_days=365, max_records=75):
        return {"results": gdelt_results, "query": query}

    async def _fake_serper(query, topic="general", max_results=5):
        return {"results": news_results or [], "query": query}

    monkeypatch.setattr(enrichment_module, "news_search_raw", _fake_gdelt)
    monkeypatch.setattr(enrichment_module, "serper_search_raw", _fake_serper)


# --- the three shipped headlines, verbatim -------------------------------------

GINNY_RESULT = {
    "url": "https://example.com/ginny-robinson",
    "title": "Ginny Robinson",
    "seendate": "20260813000000",
}

NEWSOM_RESULT = {
    "url": "https://example.com/newsom-trump-housing",
    "title": "Newsom and Trump agree on something: Blame Wall Street for the housing crisis",
    "seendate": "20260812000000",
}

# A genuinely-about-Class-VI headline — the one that SHOULD pass the gate and surface
# as exactly one `recent_news` claim carrying the article URL.
CLASS_VI_CIO_RESULT = {
    "url": "https://example.com/class-vi-cio",
    "title": "Class VI Family Office names new CIO",
    "seendate": "20260810000000",
}


# --- T20.1 acceptance: the gate itself -----------------------------------------

def test_mentions_entity_false_for_unrelated_headline():
    assert _mentions_entity("Ginny Robinson", ENTITY, []) is False


def test_mentions_entity_false_for_newsom_trump_housing_headline():
    assert _mentions_entity(
        "Newsom and Trump agree on something: Blame Wall Street for the housing crisis",
        ENTITY, [],
    ) is False


def test_mentions_entity_true_when_headline_names_the_firm():
    assert _mentions_entity("Class VI Family Office names new CIO", ENTITY, []) is True


def test_mentions_entity_llc_suffix_not_required_in_text():
    """The core phrase strips the trailing legal suffix — the article need not repeat
    'LLC' for the firm to be recognised (PLAN.md T20.1 acceptance)."""
    assert _mentions_entity("Class VI Family Office announces fund close", ENTITY, []) is True


def test_mentions_entity_alias_match_passes():
    assert _mentions_entity("Class VI Partners closes second fund", ENTITY, ["Class VI Partners"]) is True


def test_mentions_entity_case_and_whitespace_invariant():
    assert _mentions_entity("  CLASS   vi   Family   OFFICE  in the news  ", ENTITY, []) is True


# --- T20.3 (a)/(b): a non-mentioning GDELT hit produces NOTHING ------------------

async def test_ginny_robinson_gdelt_hit_emits_nothing(monkeypatch):
    _patch_news(monkeypatch, gdelt_results=[GINNY_RESULT])
    assert await _find_dated_signal(ENTITY, ALIASES) is None
    assert await _find_news_signals(ENTITY, ALIASES) == []


async def test_newsom_trump_housing_gdelt_hit_emits_nothing(monkeypatch):
    _patch_news(monkeypatch, gdelt_results=[NEWSOM_RESULT])
    assert await _find_dated_signal(ENTITY, ALIASES) is None
    assert await _find_news_signals(ENTITY, ALIASES) == []


# --- T20.3 (c): a mentioning headline yields exactly one recent_news claim ------

async def test_class_vi_cio_headline_emits_one_recent_news_claim(monkeypatch):
    _patch_news(monkeypatch, gdelt_results=[CLASS_VI_CIO_RESULT])

    dated = await _find_dated_signal(ENTITY, ALIASES)
    assert dated is not None
    assert dated.field_name == "recent_news"
    assert dated.source_url == "https://example.com/class-vi-cio"
    assert dated.status == "confirmed"

    news = await _find_news_signals(ENTITY, ALIASES)
    assert len(news) == 1
    assert news[0].field_name == "recent_news"
    assert news[0].source_url == "https://example.com/class-vi-cio"


# --- T20.3 (d): the news path never writes the deleted fields -------------------

async def test_news_signals_never_emit_key_hires_or_fund_commitments(monkeypatch):
    """Across every fixture — including the one that passes the gate — the news path
    must never write `recent_key_hires` or `recent_fund_commitments` (T20.2 deleted
    those write paths outright)."""
    for fixture in (GINNY_RESULT, NEWSOM_RESULT, CLASS_VI_CIO_RESULT):
        _patch_news(monkeypatch, gdelt_results=[fixture])
        claims = await _find_news_signals(ENTITY, ALIASES)
        fields = {c.field_name for c in claims}
        assert "recent_key_hires" not in fields
        assert "recent_fund_commitments" not in fields
        assert "recent_investments" not in fields  # news -> recent_news only


async def test_dated_signal_never_emits_recent_investments(monkeypatch):
    """T20.2: `_find_dated_signal` emits `recent_news`, never `recent_investments` —
    a headline is evidence a story exists, not a statement the firm invested."""
    for fixture in (GINNY_RESULT, NEWSOM_RESULT, CLASS_VI_CIO_RESULT):
        _patch_news(monkeypatch, gdelt_results=[fixture])
        claim = await _find_dated_signal(ENTITY, ALIASES)
        if claim is not None:
            assert claim.field_name == "recent_news"
            assert claim.field_name != "recent_investments"
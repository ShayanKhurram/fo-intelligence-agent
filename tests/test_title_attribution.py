"""PLAN.md T27 — a title must belong to the principal: match the input first, else
search the name together with the firm.

Regression corpus built from the three live FEC-employer rows that reached a
deliverable on 2026-08-13. Before T27, wave -1's ``_principal_from_people`` emitted a
``principal_name`` and a ``principal_title`` per source row with no link between them,
so an arbitrary donor's title settled ``principal_title`` and wave 1's
``_find_role_currency`` (the name+firm LinkedIn x-ray) never ran — dead code on the
whole ``fec_employer`` path. T26 made it sharper: promoting the ``projected_`` tier
replaced Tullman's correct ``"PRESIDENT"`` with ``"Glen Tullman"`` (a NAME in the title
column). T27 attributes titles to their owner and drops names-as-titles.

These all fail against pre-T27 HEAD and pass after T27.1-T27.3.
"""
from __future__ import annotations

import app.enrichment as enrichment_module
from app.enrichment import _person_names_match, wave_1, wave_minus_1
from app.state import Claim


# --- helpers ---------------------------------------------------------------

def _source(source_class: str, payload: dict, url: str | None = None,
            retrieved_at: str = "2026-08-13T00:00:00Z"):
    return {"source_class": source_class, "payload": payload, "url": url,
            "retrieved_at": retrieved_at}


def _fec_people(people: list[dict], url: str = "http://fec.gov/row"):
    return _source("fec_employer", {"signals": {}, "people": people}, url=url)


def _layer1(qid: str, subject_value: str, *, source_class: str = "research",
            source_url: str = "http://research/x", confidence: str = "high"):
    """A Layer-1 research claim as the compress step emits it post-T19.2: a
    self-contained answer plus a bare ``subject_value`` the projection lifts into a
    field-name claim."""
    return Claim(
        question_id=qid,
        answer=f"{subject_value} (confirmed by research)",
        subject_value=subject_value,
        status="confirmed",
        source_url=source_url,
        source_class=source_class,
        confidence=confidence,
        produced_by="research",
    )


def _settled_titles(claims):
    """The principal_title claims that survived reconciliation (not superseded)."""
    return [c for c in claims
            if c.field_name == "principal_title" and c.status != "superseded"]


def _superseded_titles(claims):
    return [c for c in claims
            if c.field_name == "principal_title" and c.status == "superseded"]


def _patch_wave1_network(monkeypatch, *, serper_results=None):
    """Stub every wave-1 network tier except ``serper_search_raw`` (which we count).
    ``serper_results`` may be a dict keyed by substring-of-query -> results list, or a
    plain list applied to every query. Returns a ``calls`` list recording every
    ``serper_search_raw`` query."""
    calls: list[str] = []

    async def _fake_serper(query, topic="general", max_results=5):
        calls.append(query)
        if isinstance(serper_results, dict):
            for key, results in serper_results.items():
                if key in query:
                    return {"results": results, "query": query}
            return {"results": [], "query": query}
        return {"results": serper_results or [], "query": query}

    async def _fake_free_fetch(url, fallback=None):
        return {"url": url, "content": "", "extraction_method": "httpx_trafilatura"}

    async def _fake_raw_html(url):
        return None

    async def _fake_snov_by_name(first_name, last_name, domain):
        return {"results": []}

    async def _fake_snov_domain(domain):
        return {"results": []}

    async def _fake_gdelt(query, lookback_days=365, max_records=75):
        return {"results": [], "query": query}

    monkeypatch.setattr(enrichment_module, "serper_search_raw", _fake_serper)
    monkeypatch.setattr(enrichment_module, "fetch_page_free_first", _fake_free_fetch)
    monkeypatch.setattr(enrichment_module, "fetch_raw_html", _fake_raw_html)
    monkeypatch.setattr(enrichment_module, "snov_emails_by_name_domain_raw", _fake_snov_by_name)
    monkeypatch.setattr(enrichment_module, "snov_domain_search_raw", _fake_snov_domain)
    monkeypatch.setattr(enrichment_module, "news_search_raw", _fake_gdelt)
    return calls


# The SERP shape ``_find_role_currency`` parses into a title: a LinkedIn profile
# snippet whose first dash-segment starts with the principal's first name.
_LINKEDIN_XRAY = {
    "site:linkedin.com/in": [
        {"title": "Glen Tullman - Managing Partner | LinkedIn",
         "url": "https://www.linkedin.com/in/gtullman"},
    ],
}


# --- (f) _person_names_match unit cases (T27.1) ----------------------------

def test_person_names_match_equal_sets():
    assert _person_names_match("TULLMAN, GLEN", "Glen Tullman") is True


def test_person_names_match_different_given_names_no_match():
    assert _person_names_match("TULLMAN, CAYLEY ELYSE", "Glen Tullman") is False


def test_person_names_match_unrelated_names_no_match():
    assert _person_names_match("Santiago Ulloa", "ORTEGA, ROCIO") is False


def test_person_names_match_shared_surname_alone_never_matches():
    """Both multi-token, differing given names -> neither set is a subset -> no match,
    even with the surname in common."""
    assert _person_names_match("Tullman, Cayley", "Tullman, Glen") is False


def test_person_names_match_empty_or_none_is_false():
    assert _person_names_match(None, "Glen Tullman") is False
    assert _person_names_match("Glen Tullman", None) is False
    assert _person_names_match("", "Glen Tullman") is False
    assert _person_names_match("Glen Tullman", "") is False
    assert _person_names_match(None, None) is False


def test_person_names_match_single_token_matches_superset():
    assert _person_names_match("Glen", "Glen Tullman") is True


def test_person_names_match_single_token_against_unrelated_single_is_false():
    assert _person_names_match("Glen", "Tullman") is False


def test_person_names_match_single_token_subset_of_multitoken():
    """A single-token name matches an identical token or a superset containing it."""
    assert _person_names_match("tullman", "TULLMAN, CAYLEY ELYSE") is True


def test_person_names_match_case_insensitive_identical_single_token():
    assert _person_names_match("Glen", "glen") is True


# --- (a) TULLMAN: Glen Tullman + Cayley's PRESIDENT -> search tier fires -----

def test_tullman_no_settled_title_at_wave_minus_1():
    """Glen Tullman is the researched principal; Cayley Tullman's `PRESIDENT` is a
    donor's occupation, and a `projected_G2.Q3` value that is the principal's NAME both
    land in the title column. After reconciliation: the donor title is superseded
    (Cayley is not Glen), the name-as-title is dropped by the type guard, and no
    `principal_title` survives — so wave 1's search tier must run."""
    sources = [_fec_people([{"name": "TULLMAN, CAYLEY ELYSE", "title": "PRESIDENT"}])]
    layer1 = [
        _layer1("G2.Q1", "Glen Tullman"),
        _layer1("G2.Q3", "Glen Tullman"),  # a NAME in the title field
    ]
    claims = wave_minus_1(sources, "Tullman Capital LLC", [], layer1_claims=layer1)

    titles = _settled_titles(claims)
    assert titles == [], (
        f"expected no settled principal_title at wave -1, got {[(t.answer, t.status) for t in titles]}"
    )
    # Cayley's PRESIDENT is superseded, not deleted — the ledger keeps the evidence.
    superseded = _superseded_titles(claims)
    assert any(c.answer == "PRESIDENT" and c.status == "superseded" for c in superseded)
    # The name-as-title (projected_G2.Q3 = "Glen Tullman") is dropped by the type guard.
    assert any(c.answer == "Glen Tullman" and c.status == "superseded"
               and c.extraction_method == "projected_G2.Q3" for c in superseded)


async def test_tullman_wave1_invokes_role_search_tier(monkeypatch):
    """T27.3: with `principal_title` unsettled after T27.2, wave 1's guard reaches
    `_find_role_currency` — the search tier the user's second rule calls for."""
    sources = [_fec_people([{"name": "TULLMAN, CAYLEY ELYSE", "title": "PRESIDENT"}])]
    layer1 = [_layer1("G2.Q1", "Glen Tullman"), _layer1("G2.Q3", "Glen Tullman")]
    minus1 = wave_minus_1(sources, "Tullman Capital LLC", [], layer1_claims=layer1)
    assert _settled_titles(minus1) == []

    calls = _patch_wave1_network(monkeypatch, serper_results=_LINKEDIN_XRAY)
    new_claims, _ = await wave_1(minus1, "Tullman Capital LLC", domain="tullmancap.com")

    # The name+firm LinkedIn x-ray was issued against serper.
    xray_calls = [q for q in calls if "linkedin.com/in" in q]
    assert len(xray_calls) == 1, f"expected one role x-ray call, got {xray_calls}"
    assert "Glen Tullman" in xray_calls[0]
    assert "Tullman Capital LLC" in xray_calls[0]
    # And the search tier emitted a principal_title from the parsed snippet.
    title = next((c for c in new_claims if c.field_name == "principal_title"), None)
    assert title is not None, "wave 1 did not emit a principal_title from the search tier"
    assert title.answer == "Managing Partner"
    assert title.source_class == "serper_organic"


# --- (b) FEC person IS the researched principal -> title kept, no search ---

def test_matching_fec_person_keeps_title_from_payload():
    """The user's first rule: if the researched principal IS in the input, take that
    person's title from the input. Here the FEC person is `TULLMAN, GLEN` (matches the
    researched `Glen Tullman`) carrying `PRESIDENT` -> the title is kept from the
    payload with its original source_url/source_class, and `principal_title` is
    settled at wave -1 so the search tier never runs."""
    sources = [_fec_people([{"name": "TULLMAN, GLEN", "title": "PRESIDENT"}],
                            url="http://fec.gov/tullman-glen")]
    layer1 = [_layer1("G2.Q1", "Glen Tullman")]
    claims = wave_minus_1(sources, "Tullman Capital LLC", [], layer1_claims=layer1)

    titles = _settled_titles(claims)
    assert len(titles) == 1
    title = titles[0]
    assert title.answer == "PRESIDENT"
    # The kept title retains the payload's provenance — auditable back to the FEC row.
    assert title.source_class == "fec_employer"
    assert title.source_url == "http://fec.gov/tullman-glen"
    assert title.extraction_method == "derived_fec_employer"


async def test_matching_fec_person_does_not_invoke_search_tier(monkeypatch):
    """T27.3: the input already answered, so `_find_role_currency` is NOT called."""
    sources = [_fec_people([{"name": "TULLMAN, GLEN", "title": "PRESIDENT"}])]
    layer1 = [_layer1("G2.Q1", "Glen Tullman")]
    minus1 = wave_minus_1(sources, "Tullman Capital LLC", [], layer1_claims=layer1)
    assert _settled_titles(minus1), "fixture invariant: title must be settled at wave -1"

    calls = _patch_wave1_network(monkeypatch, serper_results=_LINKEDIN_XRAY)
    new_claims, _ = await wave_1(minus1, "Tullman Capital LLC", domain="tullmancap.com")

    xray_calls = [q for q in calls if "linkedin.com/in" in q]
    assert xray_calls == [], (
        f"search tier must not run when the input already settled the title, got {xray_calls}"
    )
    # And wave 1 did not re-emit a principal_title (the field was already settled).
    assert next((c for c in new_claims if c.field_name == "principal_title"), None) is None


# --- (c) WE FAMILY: three donor occupations all superseded -----------------

def test_we_family_donor_occupations_all_superseded():
    """Santiago Ulloa is the researched principal; none of the three FEC donors is him,
    so every donor occupation is superseded and `principal_title` is left unsettled
    (no `projected_G2.Q3` was emitted for this row)."""
    sources = [
        _fec_people([{"name": "ORTEGA, ROCIO", "title": "ADVISOR"}]),
        _fec_people([{"name": "KELLOGG, JOSEPH", "title": "LAWYER"}]),
        _fec_people([{"name": "HANCOCK, HEATHER", "title": "ATTORNEY"}]),
    ]
    layer1 = [_layer1("G2.Q1", "Santiago Ulloa")]
    claims = wave_minus_1(sources, "WE FAMILY OFFICE", [], layer1_claims=layer1)

    assert _settled_titles(claims) == [], (
        "no donor occupation should survive — none belongs to Santiago Ulloa"
    )
    superseded = _superseded_titles(claims)
    answers = sorted(c.answer for c in superseded)
    assert answers == ["ADVISOR", "ATTORNEY", "LAWYER"], (
        f"all three donor occupations should be superseded, got {answers}"
    )
    # And every superseded title keeps its owner's donor provenance (never deleted).
    assert all(c.source_class == "fec_employer" for c in superseded)


# --- (d) type guard: a projected_G2.Q3 equal to the principal name is dropped -

def test_type_guard_drops_projected_title_equal_to_principal_name():
    """Even with no people list, a `projected_G2.Q3` whose value is the principal's name
    is dropped by the type guard — a title is not a name."""
    layer1 = [_layer1("G2.Q1", "Glen Tullman"), _layer1("G2.Q3", "Glen Tullman")]
    claims = wave_minus_1([], "Tullman Capital LLC", [], layer1_claims=layer1)

    titles = _settled_titles(claims)
    assert titles == [], "a name-as-title must not survive the type guard"
    dropped = _superseded_titles(claims)
    assert len(dropped) == 1
    assert dropped[0].answer == "Glen Tullman"
    assert dropped[0].extraction_method == "projected_G2.Q3"
    assert dropped[0].status == "superseded"  # kept in the ledger, never deleted


# --- (e) Class VI: projected_G2.Q3 = "Managing Partner" survives untouched ---

def test_real_projected_title_survives_untouched():
    """A `projected_G2.Q3` that is a genuine title (not the principal's name) survives
    reconciliation unchanged — confirmed, original provenance intact."""
    layer1 = [
        _layer1("G2.Q1", "Matt Blackburn",
                 source_class="web_page",
                 source_url="https://classvifamilyoffice.com/team-member/matt-blackburn/"),
        _layer1("G2.Q3", "Managing Director",
                 source_class="web_page",
                 source_url="https://classvifamilyoffice.com/team-member/matt-blackburn/"),
    ]
    claims = wave_minus_1([], "CLASS VI FAMILY OFFICE, LLC", [], layer1_claims=layer1)

    titles = _settled_titles(claims)
    assert len(titles) == 1
    title = titles[0]
    assert title.answer == "Managing Director"
    assert title.status == "confirmed"  # untouched, not superseded
    assert title.extraction_method == "projected_G2.Q3"
    assert title.source_class == "web_page"
    assert title.source_url == "https://classvifamilyoffice.com/team-member/matt-blackburn/"
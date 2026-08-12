"""Tests for the SEC IAPD client (app/tools/adv.py) — respx-mocked, offline.

This tool exists because the identity lane qualified PATHSTONE FAMILY OFFICE with G1.Q4
(SFO vs MFO) left `could_not_verify`, having judged the question off the firm's own
marketing site. IAPD states the answer as structured fields, so the discriminating logic can
be mechanical rather than a model reading prose.
"""
from __future__ import annotations

import httpx
import respx

from app.tools.adv import _name_match, adv_firm_search_raw

SEARCH = "https://api.adviserinfo.sec.gov/search/firm"


def _hit(**over):
    src = {
        "firm_source_id": "151736",
        "firm_ia_sec_number": "70776",
        "firm_ia_full_sec_number": "801-70776",
        "firm_name": "PATHSTONE",
        "firm_other_names": ["PATHSTONE", "PATHSTONE FAMILY OFFICE, LLC"],
        "firm_ia_scope": "ACTIVE",
        "firm_ia_disclosure_fl": "N",
        "firm_branches_count": 162,
        "firm_ia_address_details": '{"officeAddress": {"street1": "10 STERLING BLVD", '
        '"city": "ENGLEWOOD", "state": "NJ", "country": "United States", "postalCode": "07631"}}',
    }
    src.update(over)
    return {"_source": src}


def _resp(hits, total=None):
    return httpx.Response(
        200, json={"hits": {"total": total if total is not None else len(hits), "hits": hits}}
    )


@respx.mock
async def test_maps_registration_fields():
    respx.get(SEARCH).mock(return_value=_resp([_hit()]))
    out = await adv_firm_search_raw("Pathstone Family Office")
    f = out["results"][0]
    assert f["sec_number"] == "801-70776"
    assert f["is_registered_investment_adviser"] is True
    assert f["registration_active"] is True
    assert f["branches_count"] == 162
    assert f["crd_number"] == "151736"
    assert f["has_disclosure_events"] is False
    assert f["url"] == "https://adviserinfo.sec.gov/firm/summary/151736"


@respx.mock
async def test_parses_address_json_string():
    """`firm_ia_address_details` arrives as a JSON *string*, not an object."""
    respx.get(SEARCH).mock(return_value=_resp([_hit()]))
    out = await adv_firm_search_raw("Pathstone")
    assert out["results"][0]["address"] == {
        "city": "ENGLEWOOD", "state": "NJ", "country": "United States", "postal_code": "07631",
    }


@respx.mock
async def test_malformed_address_degrades_to_none():
    respx.get(SEARCH).mock(return_value=_resp([_hit(firm_ia_address_details="not json")]))
    out = await adv_firm_search_raw("Pathstone")
    assert out["results"][0]["address"] is None


@respx.mock
async def test_non_801_number_is_not_a_registered_adviser():
    respx.get(SEARCH).mock(
        return_value=_resp([_hit(firm_ia_full_sec_number=None, firm_ia_sec_number=None)])
    )
    out = await adv_firm_search_raw("Some Family Office")
    assert out["results"][0]["is_registered_investment_adviser"] is False


@respx.mock
async def test_no_registration_found_is_evidence_not_an_error():
    """An empty IAPD result means no ADV registration — which is itself evidence FOR a
    single-family office (they rely on the family-office exclusion), not a failed lookup."""
    respx.get(SEARCH).mock(return_value=_resp([], total=0))
    out = await adv_firm_search_raw("Kopp Family Office")
    assert out["results"] == []
    assert out["exact_matches"] == 0
    assert "error" not in out


@respx.mock
async def test_http_error_degrades_without_raising():
    respx.get(SEARCH).mock(return_value=httpx.Response(503, text="unavailable"))
    out = await adv_firm_search_raw("Pathstone")
    assert out["results"] == []
    assert out["error"]


@respx.mock
async def test_transport_error_degrades_without_raising():
    respx.get(SEARCH).mock(side_effect=httpx.ConnectError("boom"))
    out = await adv_firm_search_raw("Pathstone")
    assert out["results"] == []
    assert "boom" in out["error"] or "ConnectError" in out["error"]


# --- name_match: the guard against a fuzzy search's unrelated hits ---


def test_exact_match_ignores_generic_tokens():
    assert _name_match("Pathstone Family Office", "PATHSTONE", ["PATHSTONE"]) == "exact"
    assert _name_match("Pathstone Family Office LLC", "PATHSTONE FAMILY OFFICE, LLC", []) == "exact"


def test_unrelated_firm_is_flagged():
    """Querying "Bakken Family Office LLC" really returns 250 hits led by "DYE FAMILY
    OFFICE". Without this guard a researcher could attribute Dye's registration, branch count
    and disclosure history to Bakken — a fabricated claim."""
    assert _name_match("Bakken Family Office LLC", "DYE FAMILY OFFICE", ["DYE FAMILY OFFICE LLC"]) == "unrelated"
    assert _name_match("Bakken Family Office LLC", "WE FAMILY OFFICES", ["GENSPRING FAMILY OFFICES"]) == "unrelated"


def test_partial_match_when_one_distinctive_token_overlaps():
    assert _name_match("Stone Tower Family Office", "STONE HARBOR ADVISORS", []) == "partial"


def test_generic_only_query_is_unrelated_not_exact():
    """"Family Office" alone shares no distinctive token with anything, so it must never
    claim an exact match."""
    assert _name_match("Family Office", "DYE FAMILY OFFICE", []) == "unrelated"


def test_match_found_via_other_names():
    assert _name_match(
        "Stone Tower Family Office", "PATHSTONE", ["PATHSTONE", "STONE TOWER FAMILY OFFICE, LLC"]
    ) == "exact"


@respx.mock
async def test_exact_matches_sort_first():
    respx.get(SEARCH).mock(
        return_value=_resp(
            [
                _hit(firm_name="DYE FAMILY OFFICE", firm_other_names=["DYE FAMILY OFFICE"]),
                _hit(firm_name="PATHSTONE", firm_other_names=["PATHSTONE FAMILY OFFICE, LLC"]),
            ],
            total=250,
        )
    )
    out = await adv_firm_search_raw("Pathstone Family Office")
    assert out["results"][0]["firm_name"] == "PATHSTONE"
    assert out["results"][0]["name_match"] == "exact"
    assert out["exact_matches"] == 1
    assert out["total_matches"] == 250

"""Tests for the EDGAR full-text-search client.

Pinned against a real bug found during the live pilot (see DEBUG_PLAN.md): the
researcher model called ``edgar_search(forms='13F,ADV')`` for an entity known to have
real 13F filings and got zero results, because SEC's actual full-text-search form-type
string is ``"13F-HR"`` (not ``"13F"``) and Form ADV filings aren't indexed in EDGAR full-
text search at all. A plausible-but-wrong form-type guess silently zeroed out real
filings instead of erroring or degrading gracefully.

These tests exercise ``edgar_full_text_search_raw`` directly (the layer that owns the
auto-widen behavior), respx-mocked, mirroring how ``tests/test_orio_search.py`` and
``tests/test_gdelt.py`` mock httpx for tool clients. The ``"note"`` key it adds flows
through ``cached_call`` unchanged on a fresh (non-cached) call, so this is also the
shape ``edgar_search`` returns to the model.
"""
from __future__ import annotations

import httpx
import respx

from app.config import SETTINGS
from app.tools.edgar import edgar_full_text_search_raw

_URL = SETTINGS.tools.edgar_fulltext_url


def _hit(cik="123", form="13F-HR", filed="2026-01-01", name="Acme Capital"):
    return {
        "_id": f"000{cik}000-26-2:0",
        "_source": {
            "ciks": [cik],
            "display_names": [name],
            "root_forms": form,
            "file_date": filed,
        },
    }


@respx.mock
async def test_forms_filter_zero_hits_auto_widens_to_unfiltered_results():
    """forms='13F,ADV' (a wrong guess) zeros out the filtered call; we retry unfiltered
    and return the real hits with a note describing the fallback."""
    filtered_route = respx.get(_URL, params={"q": "Acme", "forms": "13F,ADV"}).mock(
        return_value=httpx.Response(200, json={"hits": {"hits": []}})
    )
    unfiltered_route = respx.get(_URL, params={"q": "Acme"}).mock(
        return_value=httpx.Response(200, json={"hits": {"hits": [_hit()]}})
    )
    result = await edgar_full_text_search_raw("Acme", forms="13F,ADV")

    assert filtered_route.called
    assert unfiltered_route.called  # the widen retry fired
    assert len(result["results"]) == 1
    assert result["results"][0]["form_type"] == "13F-HR"
    assert result["query"] == "Acme"
    assert "note" in result
    assert "13F,ADV" in result["note"]
    assert "without a forms filter" in result["note"]
    assert "error" not in result


@respx.mock
async def test_forms_filter_with_hits_makes_no_retry_and_no_note():
    """forms='13F-HR' (the REAL form string) returns hits directly — one request only,
    no wasted rate-limit budget on a retry, no note key."""
    filtered_route = respx.get(_URL, params={"q": "Acme", "forms": "13F-HR"}).mock(
        return_value=httpx.Response(200, json={"hits": {"hits": [_hit(), _hit(cik="456")]}})
    )
    unfiltered_route = respx.get(_URL, params={"q": "Acme"}).mock(
        return_value=httpx.Response(200, json={"hits": {"hits": []}})
    )
    result = await edgar_full_text_search_raw("Acme", forms="13F-HR")

    assert filtered_route.call_count == 1
    assert unfiltered_route.call_count == 0  # no wasted retry
    assert len(result["results"]) == 2
    assert "note" not in result
    assert "error" not in result


@respx.mock
async def test_forms_none_behaves_as_before_one_request_no_note():
    """forms=None (the default) never triggers a widen regardless of result count —
    one request, no note key."""
    route = respx.get(_URL, params={"q": "Acme"}).mock(
        return_value=httpx.Response(200, json={"hits": {"hits": []}})
    )
    result = await edgar_full_text_search_raw("Acme", forms=None)

    assert route.call_count == 1
    assert result["results"] == []
    assert "note" not in result
    assert "error" not in result


@respx.mock
async def test_forms_none_with_hits_no_note():
    """forms=None with real hits — still one request, no note."""
    route = respx.get(_URL, params={"q": "Acme"}).mock(
        return_value=httpx.Response(200, json={"hits": {"hits": [_hit()]}})
    )
    result = await edgar_full_text_search_raw("Acme")

    assert route.call_count == 1
    assert len(result["results"]) == 1
    assert "note" not in result
    assert "error" not in result


@respx.mock
async def test_filtered_http_error_no_retry_no_note():
    """The filtered request raising an HTTP error returns the existing
    {"results": [], "query": ..., "error": ...} shape — no retry attempted, no note."""
    filtered_route = respx.get(_URL, params={"q": "Acme", "forms": "13F-HR"}).mock(
        return_value=httpx.Response(500)
    )
    unfiltered_route = respx.get(_URL, params={"q": "Acme"}).mock(
        return_value=httpx.Response(200, json={"hits": {"hits": [_hit()]}})
    )
    result = await edgar_full_text_search_raw("Acme", forms="13F-HR")

    assert filtered_route.call_count == 1
    assert unfiltered_route.call_count == 0  # no retry on error
    assert result["results"] == []
    assert result["query"] == "Acme"
    assert "error" in result
    assert "note" not in result


@respx.mock
async def test_widened_results_even_when_unfiltered_also_empty():
    """If the unfiltered widen also returns zero, we still surface the note (the caller
    should know a fallback was attempted) and an empty result list."""
    respx.get(_URL, params={"q": "Acme", "forms": "13F"}).mock(
        return_value=httpx.Response(200, json={"hits": {"hits": []}})
    )
    respx.get(_URL, params={"q": "Acme"}).mock(
        return_value=httpx.Response(200, json={"hits": {"hits": []}})
    )
    result = await edgar_full_text_search_raw("Acme", forms="13F")

    assert result["results"] == []
    assert "note" in result
    assert "13F" in result["note"]
    assert "error" not in result
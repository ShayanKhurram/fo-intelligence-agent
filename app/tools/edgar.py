"""SEC EDGAR client — edgar_search (plan §4.7, §4.2). Free, keyless, but a real
User-Agent with a contact email is mandatory or SEC blocks the caller. Rate limited
globally via SEC_BUCKET (10/s across the whole batch run, not per-lead — plan §5)."""
from __future__ import annotations

from typing import Any

import httpx
from langchain_core.tools import tool

from app.config import SETTINGS
from app.tools.cache import cached_call
from app.tools.ratelimit import SEC_BUCKET

_HEADERS = {"User-Agent": SETTINGS.tools.edgar_user_agent}


async def edgar_full_text_search_raw(
    query: str, forms: str | None = None
) -> dict[str, Any]:
    """Full-text search over filings by entity name, alias, or principal surname.
    Returns {"results": [{"cik","company_name","form_type","filed_at","url"}], "query"}.

    If ``forms`` was given but the filtered search succeeds with zero hits, the call is
    retried once WITHOUT the forms filter and those results are returned instead, with a
    ``"note"`` key describing the fallback. This guards against a plausible-but-wrong
    form-type guess (e.g. "13F" when EDGAR's real full-text-search form string is
    "13F-HR", or "ADV" which isn't indexed in EDGAR full-text search at all) silently
    zeroing out real filings instead of degrading gracefully."""

    async def _do_request(forms_filter: str | None) -> dict[str, Any]:
        await SEC_BUCKET.acquire()
        params: dict[str, Any] = {"q": query}
        if forms_filter:
            params["forms"] = forms_filter
        async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
            resp = await client.get(SETTINGS.tools.edgar_fulltext_url, params=params)
            resp.raise_for_status()
            data = resp.json()
        hits = data.get("hits", {}).get("hits", [])
        results = []
        for h in hits:
            src = h.get("_source", {})
            cik = (src.get("ciks") or [None])[0]
            accession = h.get("_id", "").split(":")[0].replace("-", "")
            results.append(
                {
                    "cik": cik,
                    "company_name": (src.get("display_names") or [None])[0],
                    "form_type": src.get("root_forms") or src.get("file_type"),
                    "filed_at": src.get("file_date"),
                    "url": (
                        f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}.txt"
                        if cik and accession
                        else None
                    ),
                }
            )
        return {"results": results, "query": query}

    try:
        filtered = await _do_request(forms)
    except (httpx.HTTPError, ValueError) as exc:
        # First call errored — leave the existing error shape as-is, no retry, no note.
        return {"results": [], "query": query, "error": str(exc)}

    # Auto-widen: a filtered search that succeeded but silently zeroed out likely means
    # the caller guessed a form-type string EDGAR full-text search doesn't index as-is.
    # Retry once unfiltered (going through SEC_BUCKET.acquire() again) and return those.
    if forms and len(filtered["results"]) == 0:
        try:
            widened = await _do_request(None)
        except (httpx.HTTPError, ValueError) as exc:
            return {"results": [], "query": query, "error": str(exc)}
        return {
            **widened,
            "note": f"no results with forms filter {forms!r} — retried without a forms filter",
        }

    return filtered


async def edgar_submissions_raw(cik: str) -> dict[str, Any]:
    """Filing history / current name for a CIK. Returns the raw submissions JSON, or
    {"error": str} on failure. `cik` should already be zero-padded to 10 digits; this
    function pads it defensively."""
    await SEC_BUCKET.acquire()
    padded = cik.zfill(10)
    url = f"{SETTINGS.tools.edgar_submissions_url}/CIK{padded}.json"
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"cik": cik, "error": str(exc)}

    return {
        "cik": cik,
        "name": data.get("name"),
        "former_names": [f.get("name") for f in data.get("formerNames", [])],
        "sic_description": data.get("sicDescription"),
        "url": url,
    }


@tool
async def edgar_search(query: str, forms: str | None = None) -> dict[str, Any]:
    """Full-text search SEC EDGAR filings by entity name, alias, or principal surname.
    Use to confirm an entity exists/is registered, its current legal name, or its
    RIA/ADV filing history.

    Args:
        query: Entity name, alias, or principal name to search for.
        forms: Optional comma-separated SEC form types to restrict to. Use REAL EDGAR
            form-type strings, e.g. "13F-HR" for 13F holdings reports (NOT "13F" — that
            string will not match). Form ADV filings are NOT indexed in EDGAR full-text
            search at all — do not filter by "ADV". If unsure of the exact form string,
            omit this argument and search unfiltered first.
    """
    return await cached_call(
        "edgar_search",
        lambda: edgar_full_text_search_raw(query, forms=forms),
        query=query,
        forms=forms,
    )


@tool
async def edgar_submissions(cik: str) -> dict[str, Any]:
    """Look up an entity's SEC filing history and current registered name by CIK.

    Args:
        cik: The entity's SEC Central Index Key (with or without leading zeros).
    """
    return await cached_call("edgar_submissions", lambda: edgar_submissions_raw(cik), cik=cik)

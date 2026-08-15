"""SEC IAPD (Investment Adviser Public Disclosure) client — `adv_lookup`.

Free, keyless JSON. This is the tool that mechanically answers G1.Q4 (single-family vs
multi-family office) and G1.Q5 (plain RIA in costume), both of which were previously left to
an LLM reading marketing copy off the firm's own website.

Why it exists: on 2026-08-12 the identity lane qualified `PATHSTONE FAMILY OFFICE` with
G1.Q4 `could_not_verify` and G1.Q5 "not an RIA in costume", off `pathstone.com`. Pathstone is
in fact a multi-family-office RIA rollup — a fact this one endpoint states outright:

    firm_ia_full_sec_number : 801-70776   (an active SEC-registered investment adviser)
    firm_ia_scope           : ACTIVE
    firm_branches_count     : 162
    firm_other_names        : PATHSTONE, STONE TOWER FAMILY OFFICE LLC,
                              PATHSTONE FEDERAL STREET, PATHSTONE FAMILY OFFICE LLC

The lane had already fetched `adviserinfo.sec.gov/firm/summary/151736` and got 595 bytes of
navigation chrome, because that page is a JS single-page app. This client goes at the JSON
API behind it instead, so the decisive facts arrive as structured fields rather than as
prose a model has to interpret.

**Interpreting the result** (the discriminating logic, stated once, here):
  * An `801-` SEC number with `scope=ACTIVE` means the firm is a **registered investment
    adviser**. A genuine single-family office usually does NOT register — it relies on the
    family-office exclusion (Advisers Act rule 202(a)(11)(G)-1) — so an active registration
    is strong evidence AGAINST "single-family office".
  * `branches_count` well above 1 means many offices serving many client families: a
    multi-family office or an RIA, not an SFO.
  * `other_names` listing several distinct firm names is the signature of an acquisition
    rollup.
None of that is a rejection on its own — an MFO is a legitimate record, it just has to be
LABELLED as one instead of shipping as `could_not_verify`.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.tools.httpclient import shared_client
from langchain_core.tools import tool

from app.tools.cache import cached_call
from app.tools.ratelimit import SEC_BUCKET

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.adviserinfo.sec.gov/search/firm"
# IAPD sits behind a CDN that returns 403 to obviously-scripted clients; a normal browser
# UA is required. This is a public search endpoint, not authenticated data.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}


def _parse_address(raw: Any) -> dict[str, Any] | None:
    """`firm_ia_address_details` arrives as a JSON *string*, not an object."""
    if not raw:
        return None
    if isinstance(raw, dict):
        blob = raw
    else:
        try:
            blob = json.loads(raw)
        except (TypeError, ValueError):
            return None
    office = blob.get("officeAddress") or blob
    if not isinstance(office, dict):
        return None
    return {
        "city": office.get("city"),
        "state": office.get("state"),
        "country": office.get("country"),
        "postal_code": office.get("postalCode"),
    }


_GENERIC_NAME_TOKENS = frozenset(
    {"family", "office", "offices", "llc", "lp", "llp", "inc", "incorporated", "the",
     "capital", "management", "advisors", "advisers", "partners", "group", "co", "company",
     "trust", "wealth", "holdings", "investments", "investment"}
)


def distinctive_name_tokens(name: str) -> set[str]:
    """Tokens that actually identify a firm. "Family", "Office", "LLC" etc. match hundreds
    of unrelated advisers, so they are excluded from match scoring.

    Public because `app.enrichment.resolve_domain` guards against the same false-positive
    class this exists for (attributing an unrelated firm's data to this entity) and should
    not carry a second, drifting copy of the stoplist."""
    tokens = {t.strip(".,&").lower() for t in name.split()}
    return {t for t in tokens if t and t not in _GENERIC_NAME_TOKENS and len(t) > 1}


def _name_match(query: str, firm_name: str | None, other_names: list[str]) -> str:
    """Classify how well an IAPD hit matches what was asked for.

    IAPD's search is fuzzy and unranked for our purposes: querying "Bakken Family Office
    LLC" returns 250 hits led by "DYE FAMILY OFFICE" — nothing to do with Bakken. Without
    this signal a researcher can attribute one firm's registration, branch count and
    disclosure history to a completely different entity, which is exactly the kind of
    false positive this pipeline exists to avoid. Returns "exact" | "partial" | "unrelated".
    """
    wanted = distinctive_name_tokens(query)
    if not wanted:
        return "unrelated"
    candidates = [firm_name or "", *(other_names or [])]
    best = "unrelated"
    for cand in candidates:
        have = distinctive_name_tokens(cand)
        if not have:
            continue
        if wanted <= have or have <= wanted:
            return "exact"
        if wanted & have:
            best = "partial"
    return best


def _map_firm(source: dict[str, Any]) -> dict[str, Any]:
    sec_number = source.get("firm_ia_full_sec_number") or source.get("firm_ia_sec_number")
    scope = source.get("firm_ia_scope")
    branches = source.get("firm_branches_count")
    is_registered_ia = bool(sec_number and str(sec_number).startswith("801"))
    return {
        "firm_name": source.get("firm_name"),
        "crd_number": source.get("firm_source_id"),
        "sec_number": sec_number,
        "registration_scope": scope,
        "is_registered_investment_adviser": is_registered_ia,
        "registration_active": str(scope or "").upper() == "ACTIVE",
        "branches_count": branches,
        "other_names": source.get("firm_other_names") or [],
        "has_disclosure_events": str(source.get("firm_ia_disclosure_fl") or "").upper() == "Y",
        "address": _parse_address(source.get("firm_ia_address_details")),
        "url": (
            f"https://adviserinfo.sec.gov/firm/summary/{source.get('firm_source_id')}"
            if source.get("firm_source_id")
            else None
        ),
    }


async def adv_firm_search_raw(query: str, max_results: int = 5) -> dict[str, Any]:
    """Search IAPD for investment-adviser firms by name.

    Returns `{"results": [...], "query": str}` on success, or
    `{"results": [], "query": str, "error": str}` on any failure. Never raises — a lane must
    be able to treat a failure as could_not_verify rather than crash.
    """
    await SEC_BUCKET.acquire()  # same SEC-facing budget as EDGAR
    params = {"query": query, "start": 0, "hits": max_results}
    try:
        client = shared_client(timeout=30.0, headers=_HEADERS, follow_redirects=True)
        resp = await client.get(_SEARCH_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"results": [], "query": query, "error": f"{type(exc).__name__}: {exc}"}

    hits = (data.get("hits") or {}).get("hits") or []
    results = []
    for h in hits:
        firm = _map_firm(h.get("_source") or {})
        firm["name_match"] = _name_match(query, firm.get("firm_name"), firm.get("other_names") or [])
        results.append(firm)
    # Surface the trustworthy hits first, so a fuzzy 250-hit response doesn't lead with an
    # unrelated firm. Order: exact, then partial, then unrelated (IAPD order within each).
    _rank = {"exact": 0, "partial": 1, "unrelated": 2}
    results.sort(key=lambda f: _rank.get(f["name_match"], 3))
    total = (data.get("hits") or {}).get("total")
    return {
        "results": results,
        "query": query,
        "total_matches": total,
        "exact_matches": sum(1 for f in results if f["name_match"] == "exact"),
    }


@tool
async def adv_lookup(query: str, max_results: int = 5) -> dict[str, Any]:
    """Look up a firm's SEC investment-adviser registration in IAPD (Form ADV data).

    Use this to settle whether an entity is a single-family office (SFO) or a multi-family
    office / RIA (MFO), and whether it is a plain RIA rather than a genuine family office.
    Free and authoritative — prefer it over reading the firm's own marketing pages.

    How to read the result:
      - `is_registered_investment_adviser` true with `registration_active` true means the
        firm is an SEC-registered investment adviser. A true single-family office normally
        does NOT register (it uses the family-office exclusion), so this is strong evidence
        the entity is a multi-family office or an RIA rather than an SFO.
      - `branches_count` far above 1 means many offices serving many families -> MFO, not SFO.
      - several distinct `other_names` indicates an acquisition rollup of multiple firms.
      - `has_disclosure_events` true means IAPD records regulatory disclosure events.

    CHECK `name_match` BEFORE USING A RESULT. This search is fuzzy: a query can return
    hundreds of loosely-related advisers, and results are sorted so `name_match: "exact"`
    comes first. Only trust a result whose `name_match` is "exact" — attributing a
    "partial" or "unrelated" firm's registration or branch count to your entity would be a
    fabricated claim. If `exact_matches` is 0, treat this as "no IAPD registration found".

    No IAPD registration found under the name is itself meaningful evidence FOR a
    single-family office, not merely a failed lookup.

    Args:
        query: Firm name to search for (e.g. "Pathstone Family Office").
        max_results: Maximum number of matching firms to return.
    """
    return await cached_call(
        "adv_lookup", lambda: adv_firm_search_raw(query, max_results), query=query,
        max_results=max_results,
    )

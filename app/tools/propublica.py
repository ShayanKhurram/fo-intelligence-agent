"""ProPublica Nonprofits API client — nonprofit_lookup (plan §4.7). Keyless. Used for
foundation/officer overlap corroboration on the identity_and_type lane: many single-family
offices run an affiliated private foundation, and an officer-name match is a useful
secondary signal for identity questions (G1.Q1/G1.Q3)."""
from __future__ import annotations

from typing import Any

import httpx

from app.tools.httpclient import shared_client
from langchain_core.tools import tool

from app.config import SETTINGS
from app.tools.cache import cached_call


async def nonprofit_search_raw(query: str) -> dict[str, Any]:
    """Returns {"results": [{"ein","name","city","state","ntee_code"}], "query"}."""
    url = f"{SETTINGS.tools.propublica_base_url}/search.json"
    try:
        client = shared_client(timeout=30.0)
        resp = await client.get(url, params={"q": query})
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"results": [], "query": query, "error": str(exc)}

    orgs = data.get("organizations", [])
    results = [
        {
            "ein": o.get("ein"),
            "name": o.get("name"),
            "city": o.get("city"),
            "state": o.get("state"),
            "ntee_code": o.get("ntee_code"),
        }
        for o in orgs
    ]
    return {"results": results, "query": query}


async def nonprofit_detail_raw(ein: int | str) -> dict[str, Any]:
    """Org detail incl. officer names, when disclosed. Returns {"error": str} on failure."""
    url = f"{SETTINGS.tools.propublica_base_url}/organizations/{ein}.json"
    try:
        client = shared_client(timeout=30.0)
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"ein": ein, "error": str(exc)}

    org = data.get("organization", {})
    return {
        "ein": ein,
        "name": org.get("name"),
        "sub_name": org.get("sub_name"),
        "filings_count": len(data.get("filings_with_data", [])),
        "url": f"https://projects.propublica.org/nonprofits/organizations/{ein}",
    }


@tool
async def nonprofit_lookup(query: str) -> dict[str, Any]:
    """Search ProPublica's Nonprofit Explorer for an affiliated private foundation.
    Use for corroboration only — an EIN match on the family name plus overlapping
    officer names strengthens (never alone confirms) an identity claim.

    Args:
        query: Entity name or family surname to search for.
    """
    return await cached_call("nonprofit_lookup", lambda: nonprofit_search_raw(query), query=query)

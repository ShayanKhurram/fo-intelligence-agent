"""Hunter.io domain search — email discovery for wave 1's tiered contact resolution
(enrichment_validation_dataset_plan.md §4). This is the one deliberate exception to
research_layer_plan.md §4.7's free/keyless-only rule: the enrichment plan explicitly
calls for Hunter, and domain search (one credit -> every known address + pattern for a
domain) is credit-efficient enough on the free 50/month tier to be worth it. Degrades
gracefully — no key set -> empty result, no HTTP call — exactly like every other tool
in this codebase (app.tools.serper, app.tools.edgar)."""
from __future__ import annotations

from typing import Any

import httpx

from app.config import SETTINGS
from app.tools.keyrotation import KeyRotator

# Process-global rotator (app/tools/keyrotation.py) — a 429 (rate limit or monthly
# credit exhaustion) rotates to the next configured HUNTER_API_KEY and retries the same
# domain lookup rather than giving up, same pattern as app.tools.serper.
_HUNTER_ROTATOR = KeyRotator(SETTINGS.tools.hunter_api_keys)


async def hunter_domain_search_raw(domain: str) -> dict[str, Any]:
    """Returns {"domain","pattern","emails":[{"value","type","first_name","last_name",
    "position","confidence"}]} on success, or {"domain","pattern":None,"emails":[],
    "error":str} on any failure (missing key, HTTP error, JSON error). Never raises —
    callers treat a missing/failed lookup as could_not_verify, same as every other tool.
    On a 429, rotates to the next configured key (if any) and retries before giving up."""
    if _HUNTER_ROTATOR.current is None:
        return {"domain": domain, "pattern": None, "emails": [], "error": "HUNTER_API_KEY not set"}

    data: dict[str, Any] | None = None
    while True:
        key = _HUNTER_ROTATOR.current
        if key is None:
            return {"domain": domain, "pattern": None, "emails": [], "error": "all Hunter API keys exhausted (429)"}
        params = {"domain": domain, "api_key": key}
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(f"{SETTINGS.tools.hunter_base_url}/v2/domain-search", params=params)
                if resp.status_code == 429:
                    if _HUNTER_ROTATOR.rotate():
                        continue
                    return {"domain": domain, "pattern": None, "emails": [], "error": "all Hunter API keys exhausted (429)"}
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            return {"domain": domain, "pattern": None, "emails": [], "error": str(exc)}
        except ValueError as exc:
            return {"domain": domain, "pattern": None, "emails": [], "error": str(exc)}
        break

    d = data.get("data") or {}
    emails = [
        {
            "value": e.get("value"),
            "type": e.get("type"),
            "first_name": e.get("first_name"),
            "last_name": e.get("last_name"),
            "position": e.get("position"),
            "confidence": e.get("confidence"),
        }
        for e in d.get("emails", [])
    ]
    return {"domain": domain, "pattern": d.get("pattern"), "emails": emails}

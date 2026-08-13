"""Snov.io client — backs the LinkedIn lane tools and enrichment wave 1's email discovery.

Replaces two things at once:
  * the vendored ScrapeOps/Scrapy LinkedIn scraper for people + company lookups
    (`/v2/li-profiles-by-urls/*` returns the profile data the spider was scraping, from
    Snov.io's own database, without ToS-violating scraping or a proxy bill), and
  * Hunter.io for email discovery (`/v2/emails-by-domain-by-name/*`).

Two structural facts about this API drive the shape of this module:

1. **Auth is OAuth2 client_credentials, not a bare API key.** `POST /v1/oauth/access_token`
   exchanges SNOV_CLIENT_ID + SNOV_CLIENT_SECRET for a Bearer token that lives 1 hour
   (3600s). The token is cached process-globally and refreshed on expiry or on a 401 —
   re-authenticating per call would waste a request on every lookup.

2. **Most endpoints are asynchronous: POST `/start` -> `task_hash`, then GET `/result`.**
   Nothing returns data on the first call, so every public function here is a
   start-then-poll pair (`_run_task`). Snov.io documents a 60 req/min cap and each logical
   lookup costs at least two requests, hence SNOV_BUCKET at 1/s.

Degrades, never raises — same contract as every other tool in this codebase. Missing
credentials produce an error result with **no HTTP call**, so a lane treats it as
could_not_verify rather than crashing (app/researcher.py's `_result_is_usable` reads the
`error` key).

Verified endpoint paths and response field names as of 2026-08-12 (docs.snov.io / snov.io/api):
    POST /v1/oauth/access_token            -> {access_token, token_type, expires_in}
    POST /v2/li-profiles-by-urls/start     urls[]      -> {task_hash, status}
    GET  /v2/li-profiles-by-urls/result    task_hash   -> {status, data[{first_name,
                                                          last_name, email, position,
                                                          company, location, industry,
                                                          social, job_history}]}
    POST /v2/emails-by-domain-by-name/start rows[{first_name,last_name,domain}]
    GET  /v2/emails-by-domain-by-name/result task_hash -> {status, data[{first_name,
                                                          last_name, domain, email,
                                                          confidence, status}]}
    POST /v2/company-domain-by-name/start  names[]     -> task_hash
    GET  /v2/company-domain-by-name/result task_hash   -> {status, data[{name, domain}]}
    POST /v2/email-verification/start      emails[]
    GET  /v2/email-verification/result     task_hash

There is **no job-postings endpoint** — `linkedin_jobs` therefore stays on the vendored
ScrapeOps spider (see app/tools/linkedin.py).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.config import SETTINGS
from app.tools.keyrotation import is_exhaustion_response
from app.tools.ratelimit import SNOV_BUCKET

logger = logging.getLogger(__name__)

_TOKEN_PATH = "/v1/oauth/access_token"
# Refresh a little before the documented 3600s so an in-flight call can't straddle expiry.
_TOKEN_SKEW_S = 120.0

# Task polling. Snov.io tasks normally settle in a few seconds; cap the wait so one
# unlucky lookup can't eat a lane's 240s budget.
_POLL_INTERVAL_S = 1.5
_POLL_MAX_WAIT_S = 45.0
_COMPLETED_STATUSES = {"completed", "complete", "finished", "success", "done"}
_PENDING_STATUSES = {"in_progress", "pending", "queued", "processing", "started", "in progress"}

_token: str | None = None
_token_expires_at: float = 0.0
_token_lock = asyncio.Lock()


def _credentials_missing() -> bool:
    return not (SETTINGS.tools.snov_client_id and SETTINGS.tools.snov_client_secret)


def _err(message: str, **extra: Any) -> dict[str, Any]:
    return {"results": [], "error": message, **extra}


async def _get_token(force_refresh: bool = False) -> str | None:
    """Return a valid Bearer token, or None if credentials are missing or auth failed.
    Cached process-globally; the lock stops 8 concurrent leads from all authenticating at
    once on a cold start."""
    global _token, _token_expires_at
    if _credentials_missing():
        return None
    now = time.monotonic()
    if not force_refresh and _token and now < _token_expires_at:
        return _token
    async with _token_lock:
        now = time.monotonic()
        if not force_refresh and _token and now < _token_expires_at:
            return _token
        payload = {
            "grant_type": "client_credentials",
            "client_id": SETTINGS.tools.snov_client_id,
            "client_secret": SETTINGS.tools.snov_client_secret,
        }
        await SNOV_BUCKET.acquire()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(f"{SETTINGS.tools.snov_base_url}{_TOKEN_PATH}", data=payload)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Snov.io auth failed: %s", exc)
            _token, _token_expires_at = None, 0.0
            return None
        token = data.get("access_token")
        if not token:
            logger.warning("Snov.io auth returned no access_token: %r", data)
            return None
        expires_in = float(data.get("expires_in") or 3600)
        _token = token
        _token_expires_at = time.monotonic() + max(60.0, expires_in - _TOKEN_SKEW_S)
        return _token


async def _request(
    method: str, path: str, *, json_body: Any = None, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One authenticated request. Returns the parsed body, or `{"error": ...}`. Retries
    once with a fresh token on 401 (an expired token is indistinguishable from a revoked
    one until you try), and reports credit exhaustion distinctly so callers can tell
    "out of credits" from "no data found" — the exact distinction that was missing when
    ScrapeOps ran dry and every LinkedIn lookup silently returned nothing."""
    token = await _get_token()
    if token is None:
        return {"error": "SNOV_CLIENT_ID/SNOV_CLIENT_SECRET not set or auth failed"}

    url = f"{SETTINGS.tools.snov_base_url}{path}"
    for attempt in range(2):
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        await SNOV_BUCKET.acquire()
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.request(
                    method, url, headers=headers, json=json_body, params=params
                )
                if resp.status_code == 401 and attempt == 0:
                    refreshed = await _get_token(force_refresh=True)
                    if refreshed is None:
                        return {"error": "Snov.io re-authentication failed"}
                    token = refreshed
                    continue
                if is_exhaustion_response(resp.status_code, resp.text):
                    return {"error": f"Snov.io credits exhausted (HTTP {resp.status_code})"}
                resp.raise_for_status()
                return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
    return {"error": "Snov.io request failed after re-authentication"}


def _extract_task_hash(started: dict[str, Any]) -> str | None:
    """`task_hash` is not in a consistent place across v2 endpoints. Verified live
    2026-08-12:
      * `/v2/li-profiles-by-urls/start`      -> top level
      * `/v2/company-domain-by-name/start`   -> top level
      * `/v2/domain-search/domain-emails/start` -> **`meta.task_hash`**, alongside an
        immediate (often empty) `data` list and a ready-made `links.result` URL.
    Checking only the top level silently broke domain-email lookups with "returned no
    task_hash", so all three locations are checked here."""
    if started.get("task_hash"):
        return str(started["task_hash"])
    for container in ("data", "meta"):
        blob = started.get(container)
        if isinstance(blob, dict) and blob.get("task_hash"):
            return str(blob["task_hash"])
    return None


# The key each endpoint echoes its input back under, wrapping the real payload in `result`.
_ECHO_KEYS = ("url", "people", "name", "email", "domain")


def _flatten_envelopes(rows: list[Any]) -> list[dict[str, Any]]:
    """Unwrap Snov.io's `{"<echo>": <input>, "result": <payload>}` envelope.

    Verified live 2026-08-12 — the published docs describe a flat `data[{first_name,...}]`
    shape, but the API actually returns one envelope per input with the real record nested
    under `result`:
        li-profiles-by-urls   -> {"url": "...",   "result": {name, first_name, positions[]}}
        emails-by-domain-name -> {"people": "..", "result": [ ...email rows... ]}
        company-domain-by-name-> {"name": "...",  "result": {"domain": "..."}}
    `result` is a dict for some endpoints and a list for others, and is empty when there is
    simply no data for that input.

    Returns flat records with the echoed input preserved as `_query`, so a caller batching
    several inputs can still tell which record answers which request. An input with no data
    contributes nothing — which is what makes "no match" an empty list, and therefore
    something app/tools/cache.py correctly refuses to cache.
    """
    flat: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if "result" not in row:
            flat.append(row)  # already flat
            continue
        echo = next((row[k] for k in _ECHO_KEYS if k in row), None)
        payload = row.get("result")
        if isinstance(payload, dict):
            if payload:
                flat.append({**payload, "_query": echo})
        elif isinstance(payload, list):
            flat.extend({**item, "_query": echo} for item in payload if isinstance(item, dict))
    return flat


async def _run_task(start_path: str, result_path: str, body: dict[str, Any]) -> dict[str, Any]:
    """Drive one start->poll->result cycle. Returns `{"results": [...]}` on success or
    `{"results": [], "error": ...}`. A task that never reports completion inside
    `_POLL_MAX_WAIT_S` is an error, not an empty result — reporting a timeout as "no data"
    is what made the previous scraper's failures invisible.

    Some endpoints embed a ready-made result URL in the start response under
    `links.result` — verified live 2026-08-12 for `/v2/domain-search/domain-emails/start`,
    whose result endpoint is **path-based** (`/v2/domain-search/domain-emails/result/{task_hash}`)
    rather than the query-param form every other endpoint uses. Polling the query form for
    that endpoint 404s forever, so when `links.result` is present we poll it directly
    (with no `task_hash` query param) and fall back to `result_path?task_hash=...` only
    when the link is absent or malformed."""
    started = await _request("POST", start_path, json_body=body)
    if started.get("error"):
        return _err(started["error"])
    task_hash = _extract_task_hash(started)
    if not task_hash:
        return _err(f"Snov.io {start_path} returned no task_hash: {str(started)[:200]}")

    # Resolve the poll endpoint. `links.result` is a full URL; strip the API base to turn
    # it into a path the rest of this client can hand to `_request`. Read defensively —
    # `links` is missing on most endpoints and may not be a dict.
    result_link_path: str | None = None
    links = started.get("links")
    if isinstance(links, dict):
        link = links.get("result")
        if isinstance(link, str) and link:
            base = SETTINGS.tools.snov_base_url
            if link.startswith(base):
                result_link_path = link[len(base):]
            elif link.startswith("http"):
                # An absolute URL on a different host — strip the scheme/host so it is
                # polled through the configured base, preserving auth/headers.
                from urllib.parse import urlsplit

                parts = urlsplit(link)
                result_link_path = parts.path
    poll_path = result_link_path or result_path
    poll_params = None if result_link_path else {"task_hash": task_hash}

    deadline = time.monotonic() + _POLL_MAX_WAIT_S
    while True:
        result = await _request("GET", poll_path, params=poll_params)
        if result.get("error"):
            return _err(result["error"])
        status = str(result.get("status") or "").lower()
        data = result.get("data")
        if status in _COMPLETED_STATUSES or (data and status not in _PENDING_STATUSES):
            rows = data if isinstance(data, list) else ([data] if data else [])
            return {"results": _flatten_envelopes(rows), "task_hash": task_hash}
        if status and status not in _PENDING_STATUSES and not data:
            return _err(f"Snov.io task {status!r}: {str(result)[:200]}", task_hash=task_hash)
        if time.monotonic() >= deadline:
            return _err(
                f"Snov.io task did not complete within {_POLL_MAX_WAIT_S}s (last status {status!r})",
                task_hash=task_hash,
            )
        await asyncio.sleep(_POLL_INTERVAL_S)


# ============================================================================
# Public raw functions — each returns {"results": [...]} or {"results": [], "error": str}
# ============================================================================


async def snov_li_profiles_by_urls_raw(urls: list[str]) -> dict[str, Any]:
    """Enrich one or more LinkedIn profile URLs into full person records.

    Costs 1 credit per profile returned. This is the replacement for the vendored
    `linkedin_people_profile` spider — same intent (full detail off a profile URL), from
    Snov.io's database instead of a live scrape.
    """
    if _credentials_missing():
        return _err("SNOV_CLIENT_ID/SNOV_CLIENT_SECRET not set")
    if not urls:
        return _err("no urls given")
    return await _run_task(
        "/v2/li-profiles-by-urls/start", "/v2/li-profiles-by-urls/result", {"urls": urls}
    )


async def snov_emails_by_name_domain_raw(
    first_name: str, last_name: str, domain: str
) -> dict[str, Any]:
    """Find one person's email from their name plus their company domain.

    Replaces `hunter_domain_search_raw`'s role in enrichment wave 1. Note the shape
    difference: Hunter's domain-search returned *every* address on a domain for one credit
    and the caller filtered by surname; this endpoint is name-targeted, so the surname
    match happens server-side.
    """
    if _credentials_missing():
        return _err("SNOV_CLIENT_ID/SNOV_CLIENT_SECRET not set")
    if not domain:
        return _err("no domain given")
    rows = [{"first_name": first_name, "last_name": last_name, "domain": domain}]
    return await _run_task(
        "/v2/emails-by-domain-by-name/start", "/v2/emails-by-domain-by-name/result", {"rows": rows}
    )


async def snov_domain_search_raw(domain: str) -> dict[str, Any]:
    """Every known address on a domain — the closest analogue to Hunter's domain search,
    used as the fallback when a name-targeted lookup finds nothing."""
    if _credentials_missing():
        return _err("SNOV_CLIENT_ID/SNOV_CLIENT_SECRET not set")
    if not domain:
        return _err("no domain given")
    return await _run_task(
        "/v2/domain-search/domain-emails/start",
        "/v2/domain-search/domain-emails/result",
        {"domain": domain},
    )


async def snov_company_domain_by_name_raw(names: list[str] | str) -> dict[str, Any]:
    """Resolve company name(s) -> domain. Needed because Snov.io's company data is keyed on
    domain while the researcher only knows the entity's name (and `linkedin_company_profile`
    was previously handed a LinkedIn slug). Accepts a single name or a list of names — the
    API's `names` field must be an array, so a bare string is coerced into a one-element
    list."""
    if _credentials_missing():
        return _err("SNOV_CLIENT_ID/SNOV_CLIENT_SECRET not set")
    # The API rejects a bare string with 422 ("The names must be an array.") — verified
    # live 2026-08-13. Accept either a single name or a list for ergonomics and coerce.
    if isinstance(names, str):
        names = [names]
    if not names:
        return _err("no names given")
    return await _run_task(
        "/v2/company-domain-by-name/start", "/v2/company-domain-by-name/result", {"names": names}
    )


async def snov_verify_emails_raw(emails: list[str]) -> dict[str, Any]:
    """Deliverability check. Enrichment currently ships `principal_email` on a format match
    plus an MX record; this is the endpoint that can upgrade that to a real verification."""
    if _credentials_missing():
        return _err("SNOV_CLIENT_ID/SNOV_CLIENT_SECRET not set")
    if not emails:
        return _err("no emails given")
    return await _run_task(
        "/v2/email-verification/start", "/v2/email-verification/result", {"emails": emails}
    )

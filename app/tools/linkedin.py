"""LinkedIn tools — plan §4.7 #4. Now Snov.io-backed, except job postings.

**Backends, and why they differ:**

* `linkedin_people_profile` and `linkedin_company_profile` run on Snov.io
  (app/tools/snov.py) — its database returns the same person/company fields the vendored
  spider used to scrape, with no proxy bill and no ToS-violating live scraping.
* `linkedin_lookup` keeps its tier-1 Serper SERP x-ray (free, fast, returns a real profile
  URL) and escalates to Snov.io profile enrichment instead of the spider.
* `linkedin_jobs` **still uses the vendored ScrapeOps spider** — Snov.io has no
  job-postings endpoint, so there is nothing to migrate it to. It is the only remaining
  consumer of `vendor/linkedin_scraper` and SCRAPEOPS_API_KEY.

`vendor/linkedin_scraper` is python-scrapy-playbook/linkedin-python-scrapy-scraper,
patched (see that directory) to take runtime args instead of hardcoded demo targets, and
invoked via `run_spider.py` as a subprocess so Scrapy's Twisted reactor never has to share
a process with this app's asyncio loop. It requires a ScrapeOps proxy API key
(SCRAPEOPS_API_KEY) — LinkedIn blocks unauthenticated/datacenter-IP scraping aggressively,
and ScrapeOps is the proxy layer that routes around that. This is real, ToS-violating
scraping of LinkedIn — the user's call to make, not something to run silently.

Degrade, never fail: any block/error/empty result returns an error-tagged empty result set,
never an exception, so a blocked or unconfigured backend can never stall the batch (plan
§4.7 #5). Crucially it returns an **`error` key**, not a bare empty — an exhausted
ScrapeOps balance previously looked identical to "this person has no LinkedIn", which hid
the failure completely and let it be cached as a legitimate empty (found live 2026-08-12)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from app.config import SETTINGS
from app.tools.cache import cached_call
from app.tools.serper import serper_search_raw
from app.tools.snov import snov_company_domain_by_name_raw, snov_li_profiles_by_urls_raw
from app.tools.ratelimit import LINKEDIN_BUCKET

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _scraper_dir() -> Path:
    d = Path(SETTINGS.tools.linkedin_scraper_dir)
    return d if d.is_absolute() else _PROJECT_ROOT / d


async def run_vendored_spider(spider_name: str, **spider_args: Any) -> dict[str, Any]:
    """Shells out to vendor/linkedin_scraper/run_spider.py. Returns
    {"results": [...]} on success or {"results": [], "error": str} on any failure —
    missing key, missing vendor dir, timeout, non-zero exit, bad JSON. Never raises."""
    if not SETTINGS.tools.scrapeops_api_key:
        return {"results": [], "error": "SCRAPEOPS_API_KEY not configured"}
    scraper_dir = _scraper_dir()
    script = scraper_dir / "run_spider.py"
    if not script.exists():
        return {"results": [], "error": f"vendored scraper not found at {script}"}

    await LINKEDIN_BUCKET.acquire()
    args = [sys.executable, "run_spider.py", spider_name]
    args += [f"{k}={v}" for k, v in spider_args.items() if v is not None]
    env = {**os.environ, "SCRAPEOPS_API_KEY": SETTINGS.tools.scrapeops_api_key}

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(scraper_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        stderr_text = stderr.decode(errors="replace")
        if proc.returncode != 0:
            return {"results": [], "error": stderr_text[:500]}
        items = json.loads(stdout.decode(errors="replace") or "[]")
    except (asyncio.TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {"results": [], "error": str(exc)}

    if items and isinstance(items[0], dict) and items[0].get("error"):
        return {"results": [], "error": items[0]["error"]}

    # An empty item list with exit code 0 is ambiguous: it means EITHER the spider ran and
    # the target genuinely has nothing, OR the ScrapeOps proxy refused every request and
    # Scrapy logged it at a level run_spider.py suppresses (LOG_LEVEL=ERROR). The second
    # case is a failure and must not be reported — or cached — as a legitimate empty. That
    # ambiguity is exactly what hid an exhausted ScrapeOps balance: the proxy was returning
    # 401 "You have consumed all your API credits" on every call while 26/26 cached
    # linkedin_jobs rows recorded a clean empty result (found live 2026-08-12).
    if not items:
        proxy_error = _detect_proxy_failure(stderr_text)
        if proxy_error:
            return {"results": [], "error": proxy_error}
    return {"results": items}


_PROXY_FAILURE_MARKERS = (
    "consumed all your api credits",
    "api credits",
    "401",
    "403",
    "proxy",
    "scrapeops",
    "upgrade to a larger plan",
)


def _detect_proxy_failure(stderr_text: str) -> str | None:
    """Scan a spider's stderr for evidence the proxy layer rejected us rather than the
    target simply having no data. Returns a message for the `error` key, or None if the
    empty result looks genuine."""
    if not stderr_text:
        return None
    haystack = stderr_text.lower()
    if any(marker in haystack for marker in _PROXY_FAILURE_MARKERS):
        snippet = " ".join(stderr_text.split())[:300]
        return f"spider returned no items and its log suggests a proxy/credit failure: {snippet}"
    return None


# --- linkedin_lookup: SERP x-ray finds the URL (tier 1) -> Snov.io enriches it (tier 2) ---


async def _tier1_serp_xray(name: str, company: str | None) -> dict[str, Any]:
    query = f'site:linkedin.com/in "{name}"'
    if company:
        query += f' "{company}"'
    raw = await serper_search_raw(query, topic="general", max_results=5)
    results = [
        {"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content")}
        for r in raw.get("results", [])
        if "linkedin.com/in" in (r.get("url") or "")
    ]
    return {"results": results, "tier": 1, "query": query, "error": raw.get("error")}


def _profile_to_result(p: dict[str, Any]) -> dict[str, Any]:
    """Map a Snov.io person record onto the {title,url,snippet} shape this module has
    always returned, so `app/researcher.py`'s note formatter is unaffected.

    Shape verified live 2026-08-12 (the published docs were wrong about this — they describe
    flat `position`/`company`/`social` fields):
        {name, first_name, last_name, industry, location, country, skills[],
         positions: [{name (the COMPANY), title, linkedin_url, url (company website),
                      industry, country, location, specializations[]}],
         _query: <the LinkedIn URL that was looked up>}

    Two consequences worth knowing:
      * current title and employer come from `positions[0]`, not top-level fields;
      * **profile enrichment returns no email.** The old scraper didn't either, so nothing
        regressed, but don't expect this tool to produce a contact — that is
        `snov_emails_by_name_domain_raw`'s job in enrichment wave 1.
    """
    first = (p.get("first_name") or "").strip()
    last = (p.get("last_name") or "").strip()
    full_name = (p.get("name") or " ".join(x for x in (first, last) if x)).strip()

    positions = p.get("positions") or []
    current = positions[0] if positions and isinstance(positions[0], dict) else {}
    position = current.get("title") or ""
    company = current.get("name") or ""

    title = " - ".join(x for x in (full_name, position) if x) or full_name or None
    snippet_bits = [
        x for x in (position, company, p.get("location"), p.get("industry")) if x
    ]
    return {
        "title": title,
        # `_query` is the URL we asked about — the most reliable profile URL available,
        # since the record itself only carries the COMPANY's linkedin_url.
        "url": p.get("_query") or current.get("linkedin_url") or None,
        "snippet": " | ".join(str(b) for b in snippet_bits) or None,
        "company": company or None,
        "company_website": current.get("url") or None,
        "email": p.get("email"),  # absent from profile enrichment; kept for shape stability
    }


async def linkedin_lookup_raw(name: str, company: str | None = None) -> dict[str, Any]:
    """Tier 1 is the free Serper SERP x-ray, which is the ONLY step that turns a name into
    a profile URL. Tier 2 is Snov.io enrichment of that URL, which upgrades a SERP snippet
    into a real record (current position, company, location, sometimes an email).

    Ordering is deliberate and unchanged in spirit from the old scraper version: tier 1 is
    free and produces the input tier 2 needs. If tier 1 finds no URL there is nothing for
    Snov.io to key on, so the result is an honest empty — Snov.io has no name+company
    reverse-lookup that returns a LinkedIn URL.
    """
    tier1 = await _tier1_serp_xray(name, company)
    urls = [r["url"] for r in tier1["results"] if r.get("url")]
    if not urls:
        return {
            "results": [],
            "tier": 1,
            "query": tier1.get("query"),
            "error": tier1.get("error")
            or "no linkedin.com/in profile found for this name/company via SERP x-ray",
        }

    # Enrich the single best candidate. One credit per profile, so don't fan out over all
    # five SERP hits — the first is the highest-ranked match.
    enriched = await snov_li_profiles_by_urls_raw(urls[:1])
    if enriched.get("error") or not enriched.get("results"):
        # Snov.io unavailable/out of credits, or simply doesn't have this person. The SERP
        # hit is still real evidence (a profile URL + snippet), so return it rather than
        # discarding it — but surface why enrichment didn't happen.
        return {**tier1, "tier": 1, "enrichment_error": enriched.get("error")}

    return {
        "results": [_profile_to_result(p) for p in enriched["results"]],
        "tier": 2,
        "query": tier1.get("query"),
        "source_url": urls[0],
    }


@tool
async def linkedin_lookup(name: str, company: str | None = None) -> dict[str, Any]:
    """Look up a person's current LinkedIn profile/title. Tries a SERP x-ray first
    (fast, free, unblockable); escalates to a scraper only if that finds nothing.
    An empty result means could_not_verify, not that the person has no LinkedIn presence.
    Once you have a profile URL from this, call linkedin_people_profile on it for full
    detail (experience, education, current title).

    Args:
        name: The person's full name.
        company: Optional company name to disambiguate.
    """
    return await cached_call(
        "linkedin_lookup",
        lambda: linkedin_lookup_raw(name, company=company),
        name=name,
        company=company,
    )


# --- Direct scraper-backed tools (require SCRAPEOPS_API_KEY) ---


def _normalize_profile_url(profile: str) -> str:
    """Snov.io keys on a full LinkedIn URL. The tool's contract has always accepted a bare
    slug too (and the model frequently passes one), so widen it here rather than fail."""
    p = profile.strip()
    if p.startswith("http://") or p.startswith("https://"):
        return p
    if p.startswith("linkedin.com") or p.startswith("www.linkedin.com"):
        return f"https://{p}"
    return f"https://www.linkedin.com/in/{p.strip('/')}"


async def linkedin_people_profile_raw(profile: str) -> dict[str, Any]:
    result = await snov_li_profiles_by_urls_raw([_normalize_profile_url(profile)])
    if result.get("error"):
        return result
    return {"results": [_profile_to_result(p) for p in result.get("results", [])]}


@tool
async def linkedin_people_profile(profile: str) -> dict[str, Any]:
    """Look up a LinkedIn people profile for full detail: current title, company, location,
    industry. Requires a profile slug or URL — get one from linkedin_lookup first if you
    only have a name. An empty result means could_not_verify, not that the person has no
    profile.

    Args:
        profile: LinkedIn profile slug (e.g. "reidhoffman") or full profile URL.
    """
    return await cached_call(
        "linkedin_people_profile",
        lambda: linkedin_people_profile_raw(profile),
        profile=profile,
    )


def _company_to_result(c: dict[str, Any]) -> dict[str, Any]:
    """Map a Snov.io company record. Verified live 2026-08-12: `company-domain-by-name`
    returns ONLY `{"domain": "..."}` — no industry, size, or founding year. The extra keys
    below are read opportunistically in case a richer plan or endpoint supplies them, but do
    not count on them (see `linkedin_company_profile_raw`'s docstring)."""
    name = c.get("name") or c.get("company_name") or c.get("_query")
    domain = c.get("domain")
    bits = [
        str(c[k])
        for k in ("industry", "size", "employees", "founded", "country", "city")
        if c.get(k)
    ]
    return {
        "title": name,
        "url": f"https://{domain}" if domain else None,
        "snippet": " | ".join(bits) or (f"domain: {domain}" if domain else None),
        "domain": domain,
    }


async def linkedin_company_profile_raw(company: str) -> dict[str, Any]:
    """Resolve a company name to its canonical domain via Snov.io.

    **This is a real capability reduction from the scraper version**, confirmed against the
    live API on 2026-08-12: the old spider read industry, size and founding year off the
    LinkedIn company page, whereas `/v2/company-domain-by-name` returns only
    `{"domain": "..."}`. Snov.io does have a `/v2/database-search/companies` endpoint that
    may carry richer firmographics, but its request schema is undocumented in the public
    docs and a guessed `filters` body is rejected with HTTP 422, so it is deliberately not
    wired up rather than half-guessed.

    The domain is still the single most useful field for this pipeline: enrichment wave 1
    keys principal-email discovery and site scraping on it, and G1.Q3/G1.Q5 corroboration
    starts from the firm's own website."""
    candidate = company.strip().rstrip("/").split("/")[-1].replace("-", " ")
    resolved = await snov_company_domain_by_name_raw([candidate])
    if resolved.get("error"):
        return resolved
    rows = resolved.get("results") or []
    if not rows:
        return {"results": [], "error": f"no company domain found for {candidate!r}"}
    return {"results": [_company_to_result(c) for c in rows if isinstance(c, dict)]}


@tool
async def linkedin_company_profile(company: str) -> dict[str, Any]:
    """Look up a company's canonical domain and available company details (industry, size,
    location) — useful corroboration for identity/type questions (is this really a family
    office, how big is it). Pass the company's NAME; a LinkedIn company slug also works and
    will be converted. An empty result means could_not_verify.

    Args:
        company: Company name (e.g. "Acme Family Office"), or a LinkedIn company slug.
    """
    return await cached_call(
        "linkedin_company_profile",
        lambda: linkedin_company_profile_raw(company),
        company=company,
    )


@tool
async def linkedin_jobs(keywords: str, location: str | None = None, max_pages: int = 1) -> dict[str, Any]:
    """Search LinkedIn job postings — hiring activity is a signs-of-life signal (recent
    hires/commitments). Capped at max_pages (25 results/page) since each request spends
    ScrapeOps proxy credits. Requires SCRAPEOPS_API_KEY to be configured; returns an empty
    result (could_not_verify) if it isn't.

    Args:
        keywords: Search terms, e.g. the entity's name or a role title.
        location: Optional location filter.
        max_pages: How many pages of ~25 results to fetch (default 1).
    """
    return await cached_call(
        "linkedin_jobs",
        lambda: run_vendored_spider("linkedin_jobs", keywords=keywords, location=location, max_pages=max_pages),
        keywords=keywords,
        location=location,
        max_pages=max_pages,
    )

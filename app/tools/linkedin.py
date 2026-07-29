"""LinkedIn tools — plan §4.7 #4, extended with the vendored scraper (plan says "tiered,
never primary"; that still holds for `linkedin_lookup`, but the three scraper-backed tools
below are also exposed directly so a researcher can pull full profile/company/jobs detail
once it has a candidate URL — e.g. from `linkedin_lookup`'s tier-1 SERP x-ray).

`vendor/linkedin_scraper` is python-scrapy-playbook/linkedin-python-scrapy-scraper,
patched (see that directory) to take runtime args instead of hardcoded demo targets, and
invoked via `run_spider.py` as a subprocess so Scrapy's Twisted reactor never has to share
a process with this app's asyncio loop. It requires a ScrapeOps proxy API key
(SCRAPEOPS_API_KEY) — LinkedIn blocks unauthenticated/datacenter-IP scraping aggressively,
and ScrapeOps is the proxy layer that routes around that. This is real, ToS-violating
scraping of LinkedIn — the user's call to make, not something to run silently.

Degrade, never fail: any block/error/empty result returns an empty result set, never an
exception, so a blocked or unconfigured scraper can never stall the batch (plan §4.7 #5)."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from app.config import SETTINGS
from app.tools.cache import cached_call
from app.tools.serper import serper_search_raw
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
        if proc.returncode != 0:
            return {"results": [], "error": stderr.decode(errors="replace")[:500]}
        items = json.loads(stdout.decode(errors="replace") or "[]")
    except (asyncio.TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {"results": [], "error": str(exc)}

    if items and isinstance(items[0], dict) and items[0].get("error"):
        return {"results": [], "error": items[0]["error"]}
    return {"results": items}


# --- linkedin_lookup: tiered SERP x-ray (tier 1) -> vendored people-profile scraper (tier 2) ---


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


async def _tier2_scraper(name: str, company: str | None) -> dict[str, Any]:
    """Escalation tier. Prefers the vendored scraper (needs SCRAPEOPS_API_KEY); falls
    back to LINKEDIN_SCRAPER_CMD if someone wired a custom command instead. Since tier 1
    only gave us a name (no URL yet), this can't target a specific profile — it searches
    by name via the vendored people-profile spider's freeform slug guess, which is why
    tier 1 (SERP x-ray, which returns an actual profile URL) should usually be enough."""
    if SETTINGS.tools.scrapeops_api_key:
        query = f"{name} {company}".strip() if company else name
        result = await run_vendored_spider("linkedin_people_profile", profile=query)
        return {"results": result["results"], "tier": 2, "error": result.get("error")}

    cmd_template = SETTINGS.tools.linkedin_scraper_cmd
    if not cmd_template:
        return {"results": [], "tier": 2, "error": "no tier-2 scraper configured (set SCRAPEOPS_API_KEY)"}

    await LINKEDIN_BUCKET.acquire()
    args = shlex.split(cmd_template.format(name=shlex.quote(name), company=shlex.quote(company or "")))
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
        if proc.returncode != 0:
            return {"results": [], "tier": 2, "error": stderr.decode(errors="replace")[:500]}
        profiles = json.loads(stdout.decode(errors="replace") or "[]")
    except (asyncio.TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {"results": [], "tier": 2, "error": str(exc)}

    results = [
        {
            "title": p.get("title") or p.get("headline"),
            "url": p.get("url") or p.get("profile_url"),
            "snippet": p.get("summary") or p.get("current_title"),
        }
        for p in profiles
    ]
    return {"results": results, "tier": 2}


async def linkedin_lookup_raw(name: str, company: str | None = None) -> dict[str, Any]:
    tier1 = await _tier1_serp_xray(name, company)
    if tier1["results"]:
        return tier1
    tier2 = await _tier2_scraper(name, company)
    tier2["tier1_error"] = tier1.get("error")
    return tier2


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


@tool
async def linkedin_people_profile(profile: str) -> dict[str, Any]:
    """Scrape a LinkedIn people profile page for full detail: current title, experience
    history, education, location. Requires a profile slug or URL — get one from
    linkedin_lookup first if you only have a name. Requires SCRAPEOPS_API_KEY to be
    configured; returns an empty result (could_not_verify) if it isn't.

    Args:
        profile: LinkedIn profile slug (e.g. "reidhoffman") or full profile URL.
    """
    return await cached_call(
        "linkedin_people_profile",
        lambda: run_vendored_spider("linkedin_people_profile", profile=profile),
        profile=profile,
    )


@tool
async def linkedin_company_profile(company: str) -> dict[str, Any]:
    """Scrape a LinkedIn company page for industry, size, and founding year — useful
    corroboration for identity/type questions (is this really a family office, how big is
    it). Requires SCRAPEOPS_API_KEY to be configured; returns an empty result
    (could_not_verify) if it isn't.

    Args:
        company: LinkedIn company slug (e.g. "acme-family-office") or full company URL.
    """
    return await cached_call(
        "linkedin_company_profile",
        lambda: run_vendored_spider("linkedin_company_profile", company=company),
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

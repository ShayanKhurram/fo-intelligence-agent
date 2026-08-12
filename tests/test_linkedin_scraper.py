"""LinkedIn tool tests.

Backends split after the 2026-08-12 re-platforming: `linkedin_people_profile` and
`linkedin_company_profile` run on Snov.io, `linkedin_lookup` is a Serper x-ray followed by
Snov.io enrichment, and ONLY `linkedin_jobs` still drives the vendored ScrapeOps spider
(Snov.io has no job-postings endpoint). The `run_vendored_spider` tests below therefore
still matter — they cover the one remaining spider consumer.
"""
from __future__ import annotations

import dataclasses

import app.db as db_module
import app.tools.linkedin as linkedin_mod


def _patch_settings(monkeypatch, **tool_overrides):
    new_tools = dataclasses.replace(linkedin_mod.SETTINGS.tools, **tool_overrides)
    monkeypatch.setattr(linkedin_mod, "SETTINGS", dataclasses.replace(linkedin_mod.SETTINGS, tools=new_tools))


def _patch_db_path(monkeypatch, db_path: str) -> None:
    monkeypatch.setattr(db_module, "SETTINGS", dataclasses.replace(db_module.SETTINGS, db_path=db_path))


# --- run_vendored_spider: still backs linkedin_jobs ---


async def test_run_vendored_spider_without_key_degrades(monkeypatch):
    _patch_settings(monkeypatch, scrapeops_api_key="")
    result = await linkedin_mod.run_vendored_spider("linkedin_people_profile", profile="x")
    assert result["results"] == []
    assert "SCRAPEOPS_API_KEY" in result["error"]


async def test_run_vendored_spider_missing_script_degrades(monkeypatch, tmp_path):
    _patch_settings(monkeypatch, scrapeops_api_key="fake-key", linkedin_scraper_dir=str(tmp_path))
    result = await linkedin_mod.run_vendored_spider("linkedin_people_profile", profile="x")
    assert result["results"] == []
    assert "not found" in result["error"]


async def test_run_vendored_spider_success(monkeypatch, tmp_path):
    _patch_settings(monkeypatch, scrapeops_api_key="fake-key", linkedin_scraper_dir=str(tmp_path))
    (tmp_path / "run_spider.py").write_text(
        "import json, sys\nprint(json.dumps([{'name': 'Jane Doe', 'url': 'http://x'}]))\n",
        encoding="utf-8",
    )
    result = await linkedin_mod.run_vendored_spider("linkedin_people_profile", profile="janedoe")
    assert result["results"] == [{"name": "Jane Doe", "url": "http://x"}]
    assert "error" not in result


async def test_run_vendored_spider_nonzero_exit_degrades(monkeypatch, tmp_path):
    _patch_settings(monkeypatch, scrapeops_api_key="fake-key", linkedin_scraper_dir=str(tmp_path))
    (tmp_path / "run_spider.py").write_text(
        "import sys\nsys.stderr.write('boom')\nsys.exit(1)\n", encoding="utf-8"
    )
    result = await linkedin_mod.run_vendored_spider("linkedin_people_profile", profile="x")
    assert result["results"] == []
    assert "boom" in result["error"]


async def test_run_vendored_spider_item_level_error_degrades(monkeypatch, tmp_path):
    _patch_settings(monkeypatch, scrapeops_api_key="fake-key", linkedin_scraper_dir=str(tmp_path))
    (tmp_path / "run_spider.py").write_text(
        "import json\nprint(json.dumps([{'error': 'crawl failed: blocked'}]))\n", encoding="utf-8"
    )
    result = await linkedin_mod.run_vendored_spider("linkedin_people_profile", profile="x")
    assert result["results"] == []
    assert "blocked" in result["error"]


async def test_run_vendored_spider_empty_with_proxy_failure_in_log_reports_error(monkeypatch, tmp_path):
    """Regression: an exhausted ScrapeOps balance made the spider exit 0 with `[]` and no
    error, which was indistinguishable from "this target genuinely has no data" — and got
    cached as a legitimate empty for 26/26 linkedin_jobs rows (found live 2026-08-12).
    An empty result whose log mentions credits/proxy must now surface an error."""
    _patch_settings(monkeypatch, scrapeops_api_key="fake-key", linkedin_scraper_dir=str(tmp_path))
    (tmp_path / "run_spider.py").write_text(
        "import json, sys\n"
        "sys.stderr.write('ERROR: 401 You have consumed all your API credits. "
        "Please upgrade to a larger plan.')\n"
        "print(json.dumps([]))\n",
        encoding="utf-8",
    )
    result = await linkedin_mod.run_vendored_spider("linkedin_jobs", keywords="x")
    assert result["results"] == []
    assert result.get("error"), "a proxy/credit failure must not look like a clean empty"
    assert "credit" in result["error"].lower()


async def test_run_vendored_spider_genuine_empty_has_no_error(monkeypatch, tmp_path):
    """The other side of the coin: a clean run that simply found nothing must NOT be
    reported as an error, or every real 'no such person' becomes a false alarm."""
    _patch_settings(monkeypatch, scrapeops_api_key="fake-key", linkedin_scraper_dir=str(tmp_path))
    (tmp_path / "run_spider.py").write_text(
        "import json\nprint(json.dumps([]))\n", encoding="utf-8"
    )
    result = await linkedin_mod.run_vendored_spider("linkedin_jobs", keywords="x")
    assert result["results"] == []
    assert not result.get("error")


async def test_linkedin_jobs_tool_still_uses_the_spider(monkeypatch, tmp_path, db_path):
    _patch_settings(monkeypatch, scrapeops_api_key="fake-key", linkedin_scraper_dir=str(tmp_path))
    _patch_db_path(monkeypatch, db_path)
    (tmp_path / "run_spider.py").write_text(
        "import json\nprint(json.dumps([{'job_title': 'Analyst'}]))\n", encoding="utf-8"
    )
    result = await linkedin_mod.linkedin_jobs.ainvoke({"keywords": "Acme Family Office"})
    assert result["results"] == [{"job_title": "Analyst"}]


# --- Snov.io-backed people/company tools ---


# Real post-flattening shape, captured from the live API on 2026-08-12. Title and employer
# live in positions[0] — NOT in flat `position`/`company` fields as the docs suggest — and
# profile enrichment carries no email. `_query` is the URL that was looked up, injected by
# app/tools/snov.py's envelope flattening.
_SNOV_PROFILE = {
    "name": "Jane Doe",
    "first_name": "Jane",
    "last_name": "Doe",
    "industry": "Financial Services",
    "location": "Minneapolis, Minnesota, United States",
    "country": "United States",
    "skills": ["investing"],
    "positions": [
        {
            "name": "Acme Capital",
            "title": "Chief Investment Officer",
            "linkedin_url": "https://www.linkedin.com/company/1234",
            "url": "http://acmecap.com",
            "industry": "Financial Services",
        }
    ],
    "_query": "https://www.linkedin.com/in/janedoe",
}


async def test_people_profile_maps_snov_record(monkeypatch, db_path):
    _patch_db_path(monkeypatch, db_path)

    async def _fake(urls):
        assert urls == ["https://www.linkedin.com/in/janedoe"]
        return {"results": [_SNOV_PROFILE]}

    monkeypatch.setattr(linkedin_mod, "snov_li_profiles_by_urls_raw", _fake)
    result = await linkedin_mod.linkedin_people_profile_raw("janedoe")
    row = result["results"][0]
    assert row["title"] == "Jane Doe - Chief Investment Officer"
    assert row["url"] == "https://www.linkedin.com/in/janedoe"
    assert row["company"] == "Acme Capital"
    assert "Acme Capital" in row["snippet"]
    assert "Chief Investment Officer" in row["snippet"]
    assert row["company_website"] == "http://acmecap.com"


async def test_people_profile_has_no_email_from_enrichment(monkeypatch):
    """Profile enrichment carries no email (verified live). The field is kept for shape
    stability, but a caller must not expect a contact from this tool — that is
    snov_emails_by_name_domain_raw's job in enrichment wave 1."""
    async def _fake(urls):
        return {"results": [_SNOV_PROFILE]}

    monkeypatch.setattr(linkedin_mod, "snov_li_profiles_by_urls_raw", _fake)
    result = await linkedin_mod.linkedin_people_profile_raw("janedoe")
    assert result["results"][0]["email"] is None


async def test_people_profile_accepts_full_url_unchanged(monkeypatch):
    seen = {}

    async def _fake(urls):
        seen["urls"] = urls
        return {"results": []}

    monkeypatch.setattr(linkedin_mod, "snov_li_profiles_by_urls_raw", _fake)
    await linkedin_mod.linkedin_people_profile_raw("https://linkedin.com/in/janedoe")
    assert seen["urls"] == ["https://linkedin.com/in/janedoe"]


async def test_people_profile_propagates_snov_error(monkeypatch):
    async def _fake(urls):
        return {"results": [], "error": "Snov.io credits exhausted (HTTP 400)"}

    monkeypatch.setattr(linkedin_mod, "snov_li_profiles_by_urls_raw", _fake)
    result = await linkedin_mod.linkedin_people_profile_raw("janedoe")
    assert "exhausted" in result["error"]


async def test_company_profile_resolves_name_to_domain(monkeypatch):
    async def _fake(names):
        assert names == ["acme family office"]
        # Live shape after envelope flattening: ONLY a domain, plus the echoed query.
        return {"results": [{"domain": "acmefo.com", "_query": "acme family office"}]}

    monkeypatch.setattr(linkedin_mod, "snov_company_domain_by_name_raw", _fake)
    result = await linkedin_mod.linkedin_company_profile_raw("acme-family-office")
    row = result["results"][0]
    assert row["domain"] == "acmefo.com"
    assert row["url"] == "https://acmefo.com"
    assert row["title"] == "acme family office"
    assert "acmefo.com" in row["snippet"]


async def test_company_profile_no_match_reports_error(monkeypatch):
    async def _fake(names):
        return {"results": []}

    monkeypatch.setattr(linkedin_mod, "snov_company_domain_by_name_raw", _fake)
    result = await linkedin_mod.linkedin_company_profile_raw("nonexistent co")
    assert result["results"] == []
    assert "no company domain found" in result["error"]


# --- linkedin_lookup: x-ray finds the URL, Snov.io enriches it ---


async def test_lookup_enriches_xray_hit_via_snov(monkeypatch):
    async def _fake_search(query, topic="general", max_results=5):
        return {"results": [{"title": "Jane Doe - CIO", "url": "https://linkedin.com/in/janedoe",
                             "content": "snippet"}]}

    async def _fake_snov(urls):
        return {"results": [_SNOV_PROFILE]}

    monkeypatch.setattr(linkedin_mod, "serper_search_raw", _fake_search)
    monkeypatch.setattr(linkedin_mod, "snov_li_profiles_by_urls_raw", _fake_snov)
    result = await linkedin_mod.linkedin_lookup_raw("Jane Doe", "Acme Capital")
    assert result["tier"] == 2
    row = result["results"][0]
    assert row["title"] == "Jane Doe - Chief Investment Officer"
    assert row["company"] == "Acme Capital"


async def test_lookup_no_xray_hit_reports_error_not_silent_empty(monkeypatch):
    async def _fake_search(query, topic="general", max_results=5):
        return {"results": []}

    monkeypatch.setattr(linkedin_mod, "serper_search_raw", _fake_search)
    result = await linkedin_mod.linkedin_lookup_raw("Nobody Atall", None)
    assert result["results"] == []
    assert result["error"]


async def test_lookup_keeps_xray_evidence_when_snov_unavailable(monkeypatch):
    """Snov.io being out of credits must not throw away a real SERP hit — the profile URL
    and snippet are still evidence. The reason enrichment didn't happen is surfaced
    separately so it can't be mistaken for 'this person has no detail'."""
    async def _fake_search(query, topic="general", max_results=5):
        return {"results": [{"title": "Jane Doe - CIO", "url": "https://linkedin.com/in/janedoe",
                             "content": "snippet"}]}

    async def _fake_snov(urls):
        return {"results": [], "error": "SNOV_CLIENT_ID/SNOV_CLIENT_SECRET not set"}

    monkeypatch.setattr(linkedin_mod, "serper_search_raw", _fake_search)
    monkeypatch.setattr(linkedin_mod, "snov_li_profiles_by_urls_raw", _fake_snov)
    result = await linkedin_mod.linkedin_lookup_raw("Jane Doe", "Acme Capital")
    assert result["tier"] == 1
    assert result["results"][0]["url"] == "https://linkedin.com/in/janedoe"
    assert "SNOV_CLIENT_ID" in result["enrichment_error"]

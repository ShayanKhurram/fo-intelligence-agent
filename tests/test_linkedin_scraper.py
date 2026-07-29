from __future__ import annotations

import dataclasses

import app.db as db_module
import app.tools.linkedin as linkedin_mod


def _patch_settings(monkeypatch, **tool_overrides):
    new_tools = dataclasses.replace(linkedin_mod.SETTINGS.tools, **tool_overrides)
    monkeypatch.setattr(linkedin_mod, "SETTINGS", dataclasses.replace(linkedin_mod.SETTINGS, tools=new_tools))


def _patch_db_path(monkeypatch, db_path: str) -> None:
    monkeypatch.setattr(db_module, "SETTINGS", dataclasses.replace(db_module.SETTINGS, db_path=db_path))


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


async def test_linkedin_people_profile_tool_delegates(monkeypatch, tmp_path, db_path):
    _patch_settings(monkeypatch, scrapeops_api_key="fake-key", linkedin_scraper_dir=str(tmp_path))
    _patch_db_path(monkeypatch, db_path)
    (tmp_path / "run_spider.py").write_text(
        "import json\nprint(json.dumps([{'name': 'Jane Doe'}]))\n", encoding="utf-8"
    )
    result = await linkedin_mod.linkedin_people_profile.ainvoke({"profile": "janedoe"})
    assert result["results"] == [{"name": "Jane Doe"}]


async def test_linkedin_company_profile_tool_delegates(monkeypatch, tmp_path, db_path):
    _patch_settings(monkeypatch, scrapeops_api_key="fake-key", linkedin_scraper_dir=str(tmp_path))
    _patch_db_path(monkeypatch, db_path)
    (tmp_path / "run_spider.py").write_text(
        "import json\nprint(json.dumps([{'name': 'Acme Co'}]))\n", encoding="utf-8"
    )
    result = await linkedin_mod.linkedin_company_profile.ainvoke({"company": "acme-co"})
    assert result["results"] == [{"name": "Acme Co"}]


async def test_linkedin_jobs_tool_delegates(monkeypatch, tmp_path, db_path):
    _patch_settings(monkeypatch, scrapeops_api_key="fake-key", linkedin_scraper_dir=str(tmp_path))
    _patch_db_path(monkeypatch, db_path)
    (tmp_path / "run_spider.py").write_text(
        "import json\nprint(json.dumps([{'job_title': 'Analyst'}]))\n", encoding="utf-8"
    )
    result = await linkedin_mod.linkedin_jobs.ainvoke({"keywords": "Acme Family Office"})
    assert result["results"] == [{"job_title": "Analyst"}]


async def test_tier2_prefers_vendored_scraper_over_legacy_cmd(monkeypatch, tmp_path):
    _patch_settings(
        monkeypatch,
        scrapeops_api_key="fake-key",
        linkedin_scraper_dir=str(tmp_path),
        linkedin_scraper_cmd="echo should-not-run",
    )
    (tmp_path / "run_spider.py").write_text(
        "import json\nprint(json.dumps([{'title': 'Jane Doe', 'url': 'http://x'}]))\n", encoding="utf-8"
    )
    result = await linkedin_mod._tier2_scraper("Jane Doe", "Acme")
    assert result["results"] == [{"title": "Jane Doe", "url": "http://x"}]


async def test_tier2_falls_back_to_legacy_cmd_without_key(monkeypatch):
    _patch_settings(monkeypatch, scrapeops_api_key="", linkedin_scraper_cmd="")
    result = await linkedin_mod._tier2_scraper("Jane Doe", None)
    assert result["results"] == []
    assert "SCRAPEOPS_API_KEY" in result["error"]

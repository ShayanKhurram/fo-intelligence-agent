"""Runs one spider to completion and prints its scraped items as a JSON array on stdout.

This is the process app/tools/linkedin.py shells out to for linkedin_people_profile,
linkedin_company_profile, and linkedin_jobs. A separate subprocess (rather than importing
Scrapy's CrawlerProcess into the main asyncio app) sidesteps any conflict between
Twisted's reactor and the app's asyncio event loop — Scrapy owns its own process here.

Usage:
    python run_spider.py <spider_name> [key=value ...]

Example:
    python run_spider.py linkedin_people_profile profile=reidhoffman
    python run_spider.py linkedin_jobs keywords="Acme Family Office" max_pages=1
"""
from __future__ import annotations

import contextlib
import json
import sys
import tempfile
from pathlib import Path

from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings


def main() -> int:
    real_stdout = sys.stdout
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: run_spider.py <spider_name> [key=value ...]"}))
        return 1

    spider_name = sys.argv[1]
    spider_kwargs: dict[str, str] = {}
    for arg in sys.argv[2:]:
        if "=" not in arg:
            print(json.dumps({"error": f"bad argument (expected key=value): {arg!r}"}))
            return 1
        key, _, value = arg.partition("=")
        spider_kwargs[key] = value

    out_path = Path(tempfile.mktemp(suffix=".jsonl"))
    settings = get_project_settings()
    settings.set("FEEDS", {str(out_path): {"format": "jsonlines"}}, priority="cmdline")
    settings.set("LOG_LEVEL", "ERROR", priority="cmdline")

    process = CrawlerProcess(settings)
    process.crawl(spider_name, **spider_kwargs)
    try:
        # Some spider parse methods use bare print() for their exception handlers
        # (upstream, not us) instead of self.logger — that writes straight to stdout and
        # would corrupt the JSON contract below. Redirect stdout to stderr for the
        # duration of the crawl; only our own final print() below writes real stdout.
        with contextlib.redirect_stdout(sys.stderr):
            process.start()
    except Exception as exc:  # noqa: BLE001 — always emit valid JSON, even on crawler crash
        print(json.dumps({"error": f"crawl failed: {exc}"}), file=real_stdout)
        return 1

    items: list[dict] = []
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                items.append(json.loads(line))
        out_path.unlink(missing_ok=True)

    print(json.dumps(items), file=real_stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())

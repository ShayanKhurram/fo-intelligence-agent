"""Central configuration, env-driven with sane free-tier defaults (plan §4.7, §5, §6)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path | None = None) -> None:
    """Loads KEY=VALUE pairs from a .env file at the project root into os.environ,
    without overwriting anything already set there (shell env always wins over the
    file — standard dotenv precedence). Every dataclass field below reads via
    os.environ.get(...) as its *default expression*, which Python evaluates once, at
    class-definition time — so this must run before any of those classes are defined,
    which is why it's a module-level call right here rather than something callers
    invoke explicitly. .env itself is untracked (see .gitignore) — this is how a real
    secret like SCRAPEOPS_API_KEY survives between runs without living in any tracked
    file or having to be re-typed into every command."""
    env_path = path or (Path(__file__).resolve().parent.parent / ".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_dotenv()


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_key_list(name: str) -> tuple[str, ...]:
    """Comma-separated multi-key support for tools with a per-key rate/credit limit
    (Serper, Hunter) — `app.tools.keyrotation.KeyRotator` rotates through these when one
    hits a 429, so a long batch run doesn't stall on one exhausted key. A single key is
    just a list of one; nothing changes for anyone who only ever set one."""
    raw = os.environ.get(name, "")
    return tuple(k.strip() for k in raw.split(",") if k.strip())


@dataclass(frozen=True)
class LeadBudgetDefaults:
    max_tool_calls: int = _env_int("FOIA_MAX_TOOL_CALLS", 8)
    max_iterations: int = _env_int("FOIA_MAX_ITERATIONS", 2)
    per_lane_cap: int = _env_int("FOIA_PER_LANE_CAP", 5)
    max_usd: float = _env_float("FOIA_MAX_USD_PER_LEAD", 0.50)


@dataclass(frozen=True)
class SupervisorConfig:
    max_concurrent_research_units: int = _env_int("FOIA_MAX_CONCURRENT_RESEARCH_UNITS", 3)
    max_lane_dispatches: int = _env_int("FOIA_MAX_LANE_DISPATCHES", 2)  # initial + one refinement
    max_supervisor_turns: int = _env_int("FOIA_MAX_SUPERVISOR_TURNS", 6)


@dataclass(frozen=True)
class ResearcherConfig:
    max_react_tool_calls: int = _env_int("FOIA_MAX_REACT_TOOL_CALLS", 5)
    # 120s was sized before app/llm.py had retry/backoff on 429/5xx (added 2026-07-28
    # after the first live pilot run). A lane needing 2-3 retried LLM calls can now
    # legitimately take longer than 120s to finish — raised to give retries room rather
    # than timing out a lane that would have succeeded. See PROJECT_LOG.md.
    timeout_seconds: int = _env_int("FOIA_RESEARCHER_TIMEOUT_S", 240)


@dataclass(frozen=True)
class RunnerConfig:
    concurrency: int = _env_int("FOIA_BATCH_CONCURRENCY", 8)
    global_budget_usd: float = _env_float("FOIA_GLOBAL_BUDGET_USD", 50.0)
    warn_at_fraction: float = _env_float("FOIA_BUDGET_WARN_FRACTION", 0.75)
    sec_rate_per_sec: float = _env_float("FOIA_SEC_RATE_PER_SEC", 10.0)
    linkedin_rate_per_sec: float = _env_float("FOIA_LINKEDIN_RATE_PER_SEC", 0.5)
    # GDELT has no published rate limit, but the first live pilot run got 429s on every
    # single call under 8-way batch concurrency — 1/s is a conservative global default.
    gdelt_rate_per_sec: float = _env_float("FOIA_GDELT_RATE_PER_SEC", 1.0)
    # Serper free/standard tier default — comfortable headroom under their per-second cap.
    serper_rate_per_sec: float = _env_float("FOIA_SERPER_RATE_PER_SEC", 5.0)
    max_lead_attempts: int = _env_int("FOIA_MAX_LEAD_ATTEMPTS", 2)


@dataclass(frozen=True)
class ToolEndpoints:
    gdelt_base_url: str = os.environ.get(
        "GDELT_BASE_URL", "https://api.gdeltproject.org/api/v2/doc/doc"
    )
    edgar_fulltext_url: str = os.environ.get(
        "EDGAR_FULLTEXT_URL", "https://efts.sec.gov/LATEST/search-index"
    )
    edgar_submissions_url: str = os.environ.get(
        "EDGAR_SUBMISSIONS_URL", "https://data.sec.gov/submissions"
    )
    edgar_user_agent: str = os.environ.get(
        "EDGAR_USER_AGENT", "FO-Intelligence-Agent research@example.com"
    )
    propublica_base_url: str = os.environ.get(
        "PROPUBLICA_BASE_URL", "https://projects.propublica.org/nonprofits/api/v2"
    )
    # Deprecated free-form override, kept for anyone who wired a custom command before
    # the vendored scraper (below) existed. Leave unset to use the vendored one.
    linkedin_scraper_cmd: str = os.environ.get("LINKEDIN_SCRAPER_CMD", "")
    # vendor/linkedin_scraper — python-scrapy-playbook/linkedin-python-scrapy-scraper,
    # patched to accept runtime args (see app/tools/linkedin.py). Requires SCRAPEOPS_API_KEY.
    linkedin_scraper_dir: str = os.environ.get("LINKEDIN_SCRAPER_DIR", "vendor/linkedin_scraper")
    scrapeops_api_key: str = os.environ.get("SCRAPEOPS_API_KEY", "")
    # Serper.dev — Google SERP search backend for `web_search` (google.serper.dev) and
    # page-scrape backend for `fetch_page` (scrape.serper.dev). Replaces the self-hosted
    # OrioSearch/SearXNG backend entirely (SearXNG returned irrelevant results on long-tail
    # entity queries; OrioSearch's local Docker container was a single point of failure).
    # When the key is unset, both web_search and fetch_page degrade to an empty/error
    # result rather than raising. Key from https://serper.dev.
    # SERPER_API_KEY may be a single key or a comma-separated list (multi-key rotation,
    # app/tools/keyrotation.py) — serper_api_key stays the first one for any call site
    # that only ever wanted one (e.g. runner.py's preflight check); serper_api_keys is
    # the full rotation list.
    serper_api_key: str = os.environ.get("SERPER_API_KEY", "").split(",")[0].strip()
    serper_api_keys: tuple[str, ...] = field(default_factory=lambda: _env_key_list("SERPER_API_KEY"))
    serper_base_url: str = os.environ.get("SERPER_BASE_URL", "https://google.serper.dev")
    serper_scrape_url: str = os.environ.get("SERPER_SCRAPE_URL", "https://scrape.serper.dev")
    # Hunter.io — email discovery for enrichment wave 1 (app/tools/hunter.py). Free tier:
    # 50 credits/month. Unset -> hunter_domain_search_raw degrades to an empty result
    # with no HTTP call, same pattern as every other optional-key tool here.
    # Also supports a comma-separated multi-key list, same rotation pattern as Serper.
    hunter_api_key: str = os.environ.get("HUNTER_API_KEY", "").split(",")[0].strip()
    hunter_api_keys: tuple[str, ...] = field(default_factory=lambda: _env_key_list("HUNTER_API_KEY"))
    hunter_base_url: str = os.environ.get("HUNTER_BASE_URL", "https://api.hunter.io")


@dataclass(frozen=True)
class ModelAssignment:
    """Plan §6 role -> model tier mapping. Provider-agnostic; llm.py resolves the tier
    name to a concrete client via env vars."""

    summarizer_tier: str = os.environ.get("FOIA_MODEL_SUMMARIZER", "cheapest")
    researcher_tier: str = os.environ.get("FOIA_MODEL_RESEARCHER", "mid")
    supervisor_tier: str = os.environ.get("FOIA_MODEL_SUPERVISOR", "strongest")
    verdict_tier: str = os.environ.get("FOIA_MODEL_VERDICT", "strongest")
    compress_tier: str = os.environ.get("FOIA_MODEL_COMPRESS", "mid")
    # V1 source-supports-claim runs ~150x per full validation pass (enrichment_validation_
    # dataset_plan.md §5) — cheap by design, it's a binary judge call, not a research task.
    validation_tier: str = os.environ.get("FOIA_MODEL_VALIDATION", "cheapest")
    # Wave 2 depth enrichment (§4) is judgment-free by design (fit was decided at layer 1),
    # which is exactly what makes it tolerant of a cheap model per the plan's own framing.
    enrichment_tier: str = os.environ.get("FOIA_MODEL_ENRICHMENT", "cheapest")


@dataclass(frozen=True)
class Settings:
    db_path: str = os.environ.get("FOIA_DB_PATH", "data/foia.db")
    lead_budget: LeadBudgetDefaults = field(default_factory=LeadBudgetDefaults)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
    researcher: ResearcherConfig = field(default_factory=ResearcherConfig)
    runner: RunnerConfig = field(default_factory=RunnerConfig)
    tools: ToolEndpoints = field(default_factory=ToolEndpoints)
    models: ModelAssignment = field(default_factory=ModelAssignment)


SETTINGS = Settings()

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.db import init_db
from app.llm import FakeChatModel
import app.llm as llm_module


@pytest.fixture(autouse=True)
def _no_remote_databases(monkeypatch):
    """Unset the remote-Postgres DSN for EVERY test, always.

    `app.log_sync.sync_runs` and `app.rag_sync.drain_queue` read `DATABASE_URL` from the
    environment, and `app.scheduler.run_scheduled_job` calls both at the end of a run. The
    moment a real DSN landed in `.env` (which `app.config` loads into `os.environ` at
    import), every scheduler test began pushing its throwaway tmp-database runs into the
    live Supabase project — 10 junk `scheduled/running` rows appeared in production before
    this was caught.

    Autouse and unconditional: a test must never be able to reach a real remote database,
    and no individual test should have to remember to opt out. The sync paths are covered
    by their own tests, which stub the driver rather than talking to a server."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


@pytest.fixture
def fake_model(monkeypatch) -> FakeChatModel:
    """A fresh FakeChatModel per test, wired in as the singleton get_model() returns,
    so tests don't leak queued responses into each other."""
    model = FakeChatModel()
    monkeypatch.setattr(llm_module, "_FAKE_SINGLETON", model)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("FOIA_LLM_PROVIDER", "fake")
    return model

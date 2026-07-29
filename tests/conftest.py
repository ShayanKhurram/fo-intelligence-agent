from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.db import init_db
from app.llm import FakeChatModel
import app.llm as llm_module


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

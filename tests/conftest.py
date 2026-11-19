import os

import pytest

os.environ["LANGCHAIN_TRACING_V2"] = "false"


@pytest.fixture(autouse=True)
def _fake_provider(monkeypatch):
    """Tests must pass with no API key."""
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

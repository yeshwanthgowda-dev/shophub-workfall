"""The one LLM factory. Fake by default, OpenRouter optional (OpenAI-compatible)."""
from __future__ import annotations

import os

from langchain_core.language_models import BaseChatModel

from orchestrator.fake_llm import FakeChatModel

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def make_llm(model: str | None = None, *, temperature: float = 0.0, max_tokens: int = 1024,
             timeout: float = 30.0) -> BaseChatModel:
    provider = os.environ.get("LLM_PROVIDER", "fake").lower()
    if provider == "fake":
        return FakeChatModel()
    if provider == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY")
        name = model or os.environ.get("LLM_MODEL")
        if not key or not name:
            raise RuntimeError("LLM_PROVIDER=openrouter needs OPENROUTER_API_KEY and LLM_MODEL")
        from langchain_openai import ChatOpenAI  # lazy: fake mode needs no key or network

        return ChatOpenAI(model=name, api_key=key, base_url=OPENROUTER_BASE_URL,
                          temperature=temperature, max_tokens=max_tokens, timeout=timeout)
    raise ValueError(f"unknown LLM_PROVIDER: {provider!r}")

"""LiteLLM token loading: env var first, repo-root .env fallback; no empty Bearer."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from pilgrim.config import load_api_key
from pilgrim.pipelines.llm import LLM, LLMError


def test_env_var_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("LITELLM_TOKEN=from-file\n")
    monkeypatch.setenv("LITELLM_TOKEN", "from-env")
    assert load_api_key(env) == "from-env"


def test_dotenv_fallback(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("LITELLM_URL=http://x\nexport LITELLM_TOKEN=\"sk-a=b\"\n")
    monkeypatch.delenv("LITELLM_TOKEN", raising=False)
    assert load_api_key(env) == "sk-a=b"


def test_missing_everywhere(tmp_path, monkeypatch):
    monkeypatch.delenv("LITELLM_TOKEN", raising=False)
    assert load_api_key(tmp_path / "nope.env") == ""


def test_llm_without_token_raises_clean_error(cfg):
    def _handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not send a request without a token")
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    llm = LLM(cfg, "", client=client)
    with pytest.raises(LLMError, match="no LiteLLM token"):
        asyncio.run(llm.chat_text("m", "sys", "user"))

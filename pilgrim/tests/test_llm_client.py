"""LLM client request shape (docs/backends.md §1). Mock transport only."""
from __future__ import annotations

import asyncio
import json

import httpx
from pilgrim.pipelines.llm import LLM

SSE = ('data: {"choices":[{"delta":{"content":"{\\"ok\\": true}"}}]}\n\n'
       "data: [DONE]\n\n")


def _llm(cfg, sent: list[dict]) -> LLM:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, text=SSE,
                              headers={"content-type": "text/event-stream"})

    return LLM(cfg, "test-token",
               client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_no_output_cap_by_default(cfg):
    """A small cap counts reasoning tokens and turned slow answers into empty
    ones. "No cap" must be sent explicitly: omitting max_tokens got qwen38's
    server default of 8192 (2026-11-01), so the client sends the model's
    whole context minus the prompt."""
    sent: list[dict] = []
    llm = _llm(cfg, sent)
    assert asyncio.run(llm.chat_json("qwen38", "sys", "user")) == {"ok": True}
    # config.yaml: the largest the server admits beside other calls (32768),
    # far above the 8192 server default that cut briefs off
    assert sent[0]["max_tokens"] == 32768
    assert sent[0]["stream"] is True


def test_explicit_cap_is_still_sent(cfg):
    sent: list[dict] = []
    llm = _llm(cfg, sent)
    asyncio.run(llm.chat_json("qwen38", "sys", "user", max_tokens=64))
    assert sent[0]["max_tokens"] == 64


def test_moderation_config_defaults_to_no_cap(cfg):
    assert cfg.requests.moderation_max_tokens is None


def test_calls_without_max_tokens_send_the_whole_context_room(cfg):
    """Omitting max_tokens got qwen38's server default of 8192, which cut long
    song briefs off empty (2026-11-01). "No limit" = context minus prompt."""
    from pilgrim.pipelines.llm import LLM
    cfg.models.context_tokens = {"qwen38": 262144}
    cfg.models.output_tokens = {}   # room math alone (admission cap tested apart)
    llm = LLM(cfg, "tok")
    room = llm.room_for("qwen38", "s" * 10_000, "u" * 2_000)
    assert room == 262144 - (12_000 // 2 + 512)
    assert llm.room_for("Ornith-1.5-9B", "s", "u") is None   # unknown: omit
    assert llm.room_for("qwen38", "x" * 600_000, "") == 1024  # never below a floor

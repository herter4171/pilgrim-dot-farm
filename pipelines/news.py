"""News pipeline (RADIO.md §6.4): bounded searxng search + qwen38 summarise -> bulletin.

SearxNG is optional/best-effort: if it is unreachable we fall back to a
strictly-refreshable generic bulletin from the model so the tape never stalls.
"""
from __future__ import annotations

import logging
import time

import httpx

from config import Config
from pipelines.llm import LLM, LLMError

log = logging.getLogger("radio.news")


class NewsPipeline:
    def __init__(self, cfg: Config, llm: LLM, prompts=None):
        self.cfg = cfg
        self.llm = llm
        self.prompts = prompts or {}

    async def search(self, query: str, max_results: int = 6) -> list:
        base = self.cfg.hosts.searxng.rstrip("/")
        try:
            r = await self._client.get(
                base + "/search", params={"q": query, "format": "json"},
                timeout=8.0)
            r.raise_for_status()
            return [s.get("content") or s.get("title") for s in r.json().get("results", [])][:max_results]
        except Exception as e:
            log.warning("searxng unavailable: %s", e)
            return []

    async def produce_bulletin(self) -> dict:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        prompt = self.prompts.get("news")
        try:
            snippets = []
            if self.cfg.news.enabled:
                snippets = await self.search("top news headlines today")[:self.cfg.news.max_searches]
            ctx = "\n".join(snippets) if snippets else "(no search results available)"
            schema = ('Return JSON only: {"headlines": ["<h1>","<h2>","<h3>"], '
                      '"gravity": "serious"|"normal", "text": "<one combined bulletin>"}')
            user = (f"Write a 3-4 headline news bulletin in the station's voice, attributed "
                    f"(\"according to...\"), never read verbatim.\n\nSearch context:\n{ctx}\n\n{schema}")
            obj = await self.llm.chat_json(self.cfg.models.news, prompt, user, max_tokens=700)
            obj.setdefault("text", " ".join(obj.get("headlines", [])))
            obj["gravity"] = obj.get("gravity", "normal")
            return {
                "type": "news", "text": obj.get("text", ""), "headlines": obj.get("headlines", []),
                "gravity": obj["gravity"], "evergreen": False,
            }
        except LLMError as e:
            log.warning("news pipeline failed: %s", e)
            raise
        finally:
            await self._client.aclose()

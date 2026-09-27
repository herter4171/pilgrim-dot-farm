"""News pipeline (RADIO.md §6.4): bounded searxng search + qwen38 summarise -> bulletin.

Search goes through the REAL searxng tool exposed by the LiteLLM MCP server
(hosts.searxng -> MCP endpoint, see docs/backends.md). SearxNG is
optional/best-effort: any failure degrades to a model-written bulletin so the
tape never stalls.
"""
from __future__ import annotations

import logging

from pilgrim.config import Config
from pilgrim.pipelines.llm import LLM, LLMError
from pilgrim.pipelines.mcp import searxng_search

log = logging.getLogger("radio.news")


class NewsPipeline:
    def __init__(self, cfg: Config, llm: LLM, api_key: str = "", prompts=None):
        self.cfg = cfg
        self.llm = llm
        self.api_key = api_key
        self.prompts = prompts or {}

    async def produce_bulletin(self) -> dict:
        prompt = self.prompts.get("news")
        if not prompt:
            raise LLMError("news prompt template missing")
        snippets: list[str] = []
        if self.cfg.news.enabled:
            snippets = await searxng_search(
                self.cfg, self.api_key, "top news headlines today",
                limit=self.cfg.news.max_searches)
        ctx = "\n".join(snippets) if snippets else "(no search results available)"
        schema = ('Return JSON only: {"headlines": ["<h1>","<h2>","<h3>"], '
                  '"gravity": "serious"|"normal", "text": "<one combined bulletin>"}')
        user = (f"Write a 3-4 headline news bulletin in the station's voice, attributed "
                f'(\"according to...\"), never read verbatim.\n\n'
                f"Search context:\n{ctx}\n\n{schema}")
        obj = await self.llm.chat_json(self.cfg.models.news, prompt, user,
                                        max_tokens=4096)
        obj.setdefault("text", " ".join(obj.get("headlines", [])))
        obj["gravity"] = obj.get("gravity", "normal")
        return {
            "type": "news", "text": obj.get("text", ""), "headlines": obj.get("headlines", []),
            "gravity": obj["gravity"], "evergreen": False,
        }

"""News pipeline (RADIO.md §6.4): one bounded Brave news search + qwen38
summarise -> bulletin.

Search is the Brave Search API news endpoint (pipelines/brave.py, BRAVE.md),
freshness = last 24 h. It is best-effort: a degraded or empty search degrades
to a model-written bulletin (operator decision, BRAVE.md §0) so the tape never
stalls. Web text reaches the model quoted as data only.
"""
from __future__ import annotations

import logging

from pilgrim.config import Config
from pilgrim.pipelines.brave import BraveSearch, format_news_context
from pilgrim.pipelines.llm import LLM, LLMError

log = logging.getLogger("radio.news")

NEWS_QUERY = "top news headlines today"


class NewsPipeline:
    def __init__(self, cfg: Config, llm: LLM, search: BraveSearch | None = None,
                 prompts=None):
        self.cfg = cfg
        self.llm = llm
        # No client given = no key = every search degrades without touching the
        # network (tests, AGENTS §1.2). The station passes its shared client.
        self.search = search or BraveSearch(cfg, "")
        self.prompts = prompts or {}

    async def produce_bulletin(self) -> dict:
        prompt = self.prompts.get("news")
        if not prompt:
            raise LLMError("news prompt template missing")
        ctx = "(no search results available)"
        if self.cfg.news.enabled:
            outcome = await self.search.news(NEWS_QUERY, count=self.cfg.news.results,
                                             freshness=self.cfg.news.freshness)
            if outcome.status == "ok":
                ctx = format_news_context(outcome.results, self.cfg.news.results)
            else:
                log.warning("news.search_%s", outcome.status,
                            extra={"error": outcome.error})
        schema = ('Return JSON only: {"headlines": ["<h1>","<h2>","<h3>"], '
                  '"gravity": "serious"|"normal", "text": "<one combined bulletin>"}')
        user = (f"Write a 3-4 headline news bulletin in the station's voice, attributed "
                f'(\"according to...\"), never read verbatim.\n\n'
                f"Search context (each line: [source, age] headline. summary):\n{ctx}\n\n{schema}")
        obj = await self.llm.chat_json(self.cfg.models.news, prompt, user)
        obj.setdefault("text", " ".join(obj.get("headlines", [])))
        obj["gravity"] = obj.get("gravity", "normal")
        return {
            "type": "news", "text": obj.get("text", ""), "headlines": obj.get("headlines", []),
            "gravity": obj["gravity"], "evergreen": False,
        }

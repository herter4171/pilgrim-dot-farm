"""LLM moderation gate for the listener request line.

Untrusted free text in, a strict yes/no + reason out. The gate is CLOSED by
default: if the modeller is unreachable or the template is missing, a request is
rejected (conservative). Anything that passes may be read on air, so we never let
explicitness slip through just because the backend hiccupped.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from pilgrim.config import Config
from pilgrim.pipelines.llm import LLM

log = logging.getLogger("radio.moderation")


class Moderation:
    def __init__(self, cfg: Config, llm: LLM, prompts: dict[str, str] | None = None) -> None:
        self.cfg = cfg
        self.llm = llm
        self.prompts = prompts or {}

    async def moderate(self, text: str) -> tuple[bool, str]:
        """Return (allowed, reason). Conservative default: not allowed."""
        prompt = self.prompts.get("moderation")
        if not prompt:
            return False, "moderator unavailable"
        schema = 'Return JSON only: {"allowed": bool, "reason": str}'
        user = f'{prompt}\n\nListener request to evaluate:\n"{text}"\n\n{schema}'
        t0 = time.monotonic()
        try:
            obj: dict[str, Any] = await self.llm.chat_json(
                self.cfg.requests.moderation_model, prompt, user, max_tokens=4096)
            allowed = bool(obj.get("allowed"))
            reason = str(obj.get("reason", "")).strip()
            reason = reason or ("ok" if allowed else "blocked by moderator")
        except Exception as e:  # gate closed if the model errors
            allowed = False
            reason = "couldn't reach the DJ's filter — rejected to be safe, try again"
            log.warning("moderation.failed", extra={"error": str(e)})
        log.info("moderation.decision", extra={
            "allowed": allowed, "reason": reason[:200],
            "duration_ms": round((time.monotonic() - t0) * 1000, 1), "attempts": 1})
        return allowed, reason[:200]

"""LLM moderation gate for the listener request line.

Untrusted free text in, a strict yes/no + reason out. The gate is CLOSED by
default: if the modeller is unreachable or the template is missing, a request is
rejected (conservative). Anything that passes may be read on air, so we never let
explicitness slip through just because the backend hiccupped.
"""
from __future__ import annotations

from typing import Any

from pilgrim.config import Config
from pilgrim.pipelines.llm import LLM


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
        try:
            obj: dict[str, Any] = await self.llm.chat_json(
                self.cfg.requests.moderation_model, prompt, user, max_tokens=256)
        except Exception as e:  # gate closed if the model errors
            return False, f"moderation error: {e}"
        allowed = bool(obj.get("allowed"))
        reason = str(obj.get("reason", "")).strip()
        return allowed, (reason or ("ok" if allowed else "blocked by moderator"))[:200]

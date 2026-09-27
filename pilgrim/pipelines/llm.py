"""LLM client (LiteLLM proxy). Handles reasoning models -> parse JSON strictly (AGENTS §5)."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from pilgrim.config import Config

log = logging.getLogger("radio.llm")


class LLMError(Exception):
    pass


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def parse_json_strict(text: str) -> dict[str, Any]:
    """Parse model output as JSON; raise on anything invalid."""
    cleaned = _strip_code_fences(text)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        # last resort: pull the first balanced {...}
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            obj = json.loads(cleaned[start:end + 1])
        else:
            raise LLMError("model returned non-JSON") from None
    if not isinstance(obj, dict):
        raise LLMError("model JSON is not an object")
    return obj


class LLM:
    def __init__(self, cfg: Config, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self.api_key = api_key
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0))

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _message_text(msg: dict[str, Any]) -> str:
        """Extract final text from an LLM message.

        Reasoning models (e.g. qwen38) split output into `reasoning_content`
        and `content`, and some providers wrap the answer in
        `provider_specific_fields`. Prefer `content`, then fall back to a
        provider-specific text field so a completed answer is never dropped.
        """
        content = msg.get("content") or ""
        if content.strip():
            return str(content)
        psf = msg.get("provider_specific_fields")
        if isinstance(psf, dict):
            stack = [psf]
            while stack:
                node = stack.pop()
                if not isinstance(node, dict):
                    continue
                for k, v in node.items():
                    if k.lower() in ("content", "text", "message") and \
                            isinstance(v, str) and v.strip():
                        return v
                    if isinstance(v, dict):
                        stack.append(v)
        return ""

    async def chat_json(self, model: str, system: str, user: str,
                        max_tokens: int = 800) -> dict[str, Any]:
        """Ask an LLM for a JSON object. Returns parsed, validated-by-caller dict."""
        content = await self._complete(model, system, user, max_tokens)
        if not content or not content.strip():
            raise LLMError("llm empty content (reasoning ate the token budget?)")
        return parse_json_strict(content)

    async def chat_text(self, model: str, system: str, user: str,
                        max_tokens: int = 800) -> str:
        """Like chat_json but just returns the raw content string."""
        return await self._complete(model, system, user, max_tokens)

    async def _complete(self, model: str, system: str, user: str,
                        max_tokens: int) -> str:
        url = self.cfg.hosts.litellm.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.8,
        }
        resp = await self._client.post(
            url, headers={"Authorization": f"Bearer {self.api_key}"}, json=payload)
        if resp.status_code != 200:
            raise LLMError(f"llm http {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"llm bad response shape: {e}") from e
        return self._message_text(msg)

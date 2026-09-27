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

    async def chat_json(self, model: str, system: str, user: str,
                        max_tokens: int = 800) -> dict[str, Any]:
        """Ask an LLM for a JSON object. Returns parsed, validated-by-caller dict."""
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
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"llm bad response shape: {e}") from e
        if not content or not content.strip():
            raise LLMError("llm empty content (reasoning ate the token budget?)")
        return parse_json_strict(content)

    async def chat_text(self, model: str, system: str, user: str,
                        max_tokens: int = 800) -> str:
        """Like chat_json but just returns the raw content string."""
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
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise LLMError(f"llm bad response shape: {e}") from e

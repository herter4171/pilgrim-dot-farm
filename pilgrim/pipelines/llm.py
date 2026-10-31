"""LLM client (LiteLLM proxy). Handles reasoning models -> parse JSON strictly (AGENTS §5)."""
from __future__ import annotations

import json
import logging
import re
import time
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
        # A song brief is a 250-450-word structured caption plus a lyric sheet
        # from a reasoning model: 120 s is not enough headroom (§6.2). Voice and
        # news calls are fast; a longer read timeout only matters when the
        # backend stalls.
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0))

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
                        max_tokens: int = 4096) -> dict[str, Any]:
        """Ask an LLM for a JSON object. Returns parsed, validated-by-caller dict."""
        content = await self._complete(model, system, user, max_tokens)
        if not content or not content.strip():
            raise LLMError("llm empty content (reasoning ate the token budget?)")
        return parse_json_strict(content)

    async def chat_text(self, model: str, system: str, user: str,
                        max_tokens: int = 4096) -> str:
        """Like chat_json but just returns the raw content string."""
        return await self._complete(model, system, user, max_tokens)

    async def _complete(self, model: str, system: str, user: str,
                        max_tokens: int) -> str:
        """One completion, read as an SSE stream.

        Streaming is deliberate: qwen38 is a reasoning model, and a full
        structured song caption (250-450 words) plus a lyric sheet can run past
        a non-streaming read timeout. With a stream the timeout measures
        inactivity between chunks, not total generation time (docs/backends.md
        §1). Reasoning deltas are ignored; only `delta.content` is accumulated.
        """
        url = self.cfg.hosts.litellm.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.8,
            "stream": True,
        }
        if not self.api_key:
            raise LLMError("no LiteLLM token (set LITELLM_TOKEN or add it to .env)")
        t0 = time.monotonic()
        content = ""
        raw: list[str] = []
        status = "ok"
        try:
            async with self._client.stream(
                    "POST", url, headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")
                    duration_ms = round((time.monotonic() - t0) * 1000, 1)
                    log.warning("llm.call", extra={
                        "model": model, "duration_ms": duration_ms,
                        "status": f"http_{resp.status_code}", "empty": False,
                        "max_tokens": max_tokens, "error": body[:200]})
                    raise LLMError(f"llm http {resp.status_code}: {body[:300]}")
                async for line in resp.aiter_lines():
                    raw.append(line)
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            content += str(delta["content"])
                if not content.strip():
                    # proxy ignored `stream`: fall back to a plain JSON body
                    body = "\n".join(raw).strip()
                    if body:
                        try:
                            obj = json.loads(body)
                            msg = (obj.get("choices") or [{}])[0].get("message") or {}
                            content = self._message_text(msg)
                        except (json.JSONDecodeError, AttributeError):
                            pass  # no content: chat_json reports the empty budget
        except httpx.HTTPError as e:
            duration_ms = round((time.monotonic() - t0) * 1000, 1)
            log.warning("llm.call", extra={
                "model": model, "duration_ms": duration_ms, "status": "transport",
                "empty": True, "max_tokens": max_tokens,
                "error": f"{type(e).__name__}: {e}"})
            raise LLMError(f"llm transport: {type(e).__name__}: {e}") from None
        duration_ms = round((time.monotonic() - t0) * 1000, 1)
        log.info("llm.call", extra={
            "model": model, "duration_ms": duration_ms, "status": status,
            "empty": not bool(content.strip()), "max_tokens": max_tokens})
        return content

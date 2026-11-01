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


class LLMTruncated(LLMError):  # type: ignore[no-redef]
    """The model's output was cut off (empty answer, or JSON that never
    closes): a backend limit or overload, not the request's fault. Callers
    may retry without charging the item an attempt."""


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

    def room_for(self, model: str, system: str, user: str) -> int | None:
        """Output room for "no limit": the model's context minus a conservative
        prompt estimate (2 chars/token over-counts English, plus chat-template
        slack). None when the context isn't configured (max_tokens omitted)."""
        ctx = (self.cfg.models.context_tokens or {}).get(model)
        if not ctx:
            return None
        prompt_est = (len(system) + len(user)) // 2 + 512
        room = max(1024, ctx - prompt_est)
        cap = (self.cfg.models.output_tokens or {}).get(model)
        return min(room, cap) if cap else room

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
                        max_tokens: int | None = None) -> dict[str, Any]:
        """Ask an LLM for a JSON object. Returns parsed, validated-by-caller dict."""
        content = await self._complete(model, system, user, max_tokens)
        if not content or not content.strip():
            raise LLMTruncated("llm empty content (reasoning ate the token budget?)")
        try:
            return parse_json_strict(content)
        except LLMError:
            if not content.rstrip().endswith(("}", "```")):
                raise LLMTruncated("llm output cut off mid-JSON") from None
            raise

    async def chat_text(self, model: str, system: str, user: str,
                        max_tokens: int | None = None) -> str:
        """Like chat_json but just returns the raw content string."""
        return await self._complete(model, system, user, max_tokens)

    async def _complete(self, model: str, system: str, user: str,
                        max_tokens: int | None) -> str:
        """One completion, read as an SSE stream.

        Streaming is deliberate: qwen38 is a reasoning model, and a full
        structured song caption (250-450 words) plus a lyric sheet can run past
        a non-streaming read timeout. With a stream the timeout measures
        inactivity between chunks, not total generation time (docs/backends.md
        §1). Reasoning deltas are ignored; only `delta.content` is accumulated.

        `max_tokens` is omitted by default: the reasoning trace counts against
        it, and a cap only ever turned a slow answer into an empty one
        (moderation at 4096, briefs at 4096). Both models stop on their own
        with no cap (probed 2026-10-31, docs/backends.md §1).
        """
        url = self.cfg.hosts.litellm.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.8,
            "stream": True,
        }
        if max_tokens is None:
            max_tokens = self.room_for(model, system, user)
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if not self.api_key:
            raise LLMError("no LiteLLM token (set LITELLM_TOKEN or add it to .env)")
        t0 = time.monotonic()
        content = ""
        finish = ""  # stop | length (output cap hit) | ...
        stream_error = ""
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
                    if isinstance(chunk.get("error"), dict):
                        # the proxy reports backend failures IN the stream
                        stream_error = str(chunk["error"].get("message") or chunk["error"])
                        break
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            content += str(delta["content"])
                        if choice.get("finish_reason"):
                            finish = str(choice["finish_reason"])
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
        if stream_error and not content.strip():
            status = "stream_error"
        log.info("llm.call", extra={
            "model": model, "duration_ms": duration_ms, "status": status,
            "empty": not bool(content.strip()), "max_tokens": max_tokens,
            "finish_reason": finish, "error": stream_error[:200] or None})
        if stream_error and not content.strip():
            # overload/admission/connection trouble upstream: retryable
            raise LLMTruncated(f"llm backend error: {stream_error[:200]}")
        return content

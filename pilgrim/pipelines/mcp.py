"""Minimal MCP (Streamable HTTP) client for the searxng web-search tool exposed
through the LiteLLM proxy (docs/backends.md).

Probed against the live proxy (2026-09-27):
- endpoint: POST {hosts.searxng}  (the LiteLLM MCP URL, e.g. http://localhost:4000/mcp)
- headers:  Authorization: Bearer <LITELLM_TOKEN>,
            Content-Type: application/json,
            Accept: application/json, text/event-stream
- initialize returns a session id in the `mcp-session-id` response header and
  an SSE body (`event: message` / `data: {...}`).
- tools/call to `web_search-searxng_web_search` returns text items with real
  ranked search results.
"""
from __future__ import annotations

import contextlib
import json as _json
import logging
from typing import Any

import httpx

from pilgrim.config import Config

log = logging.getLogger("radio.mcp")

PROTOCOL_VERSION = "2025-03-26"


class MCPError(Exception):
    pass


def _parse_sse(text: str) -> list[dict[str, Any]]:
    """Parse SSE frames into the JSON objects carried by each `data:` line."""
    msgs: list[dict[str, Any]] = []
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            buf.append(line[5:].strip())
        elif not line.strip() and buf:
            raw = "".join(buf)
            buf = []
            try:
                msgs.append(_json.loads(raw))
            except _json.JSONDecodeError:
                log.debug("non-JSON SSE data ignored: %s", raw[:120])
    if buf:
        with contextlib.suppress(_json.JSONDecodeError):
            msgs.append(_json.loads("".join(buf)))
    return msgs


class MCPSession:
    """A single MCP client session with initialize handshake + tools/call."""

    def __init__(self, endpoint: str, api_key: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._session_id: str | None = None
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0),
                                         headers=headers)

    # ------------------------------------------------------------------ init
    async def initialize(self) -> None:
        r = await self._client.post(self.endpoint, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "pilgrim-radio", "version": "0.2.0"}}})
        if r.status_code == 401:
            raise MCPError("MCP endpoint requires auth (401)")
        if r.status_code != 200:
            raise MCPError(f"MCP initialize http {r.status_code}: {r.text[:200]}")
        self._session_id = (r.headers.get("mcp-session-id")
                            or r.headers.get("Mcp-Session-Id"))
        for m in _parse_sse(r.text):
            if m.get("error"):
                raise MCPError(f"MCP initialize error: {m['error']}")
        if self._session_id:
            await self._client.post(self.endpoint, headers=self._session_headers(),
                                    json={"jsonrpc": "2.0",
                                          "method": "notifications/initialized"})

    def _session_headers(self) -> dict[str, str]:
        return {"mcp-session-id": self._session_id} if self._session_id else {}

    # ----------------------------------------------------------------- tools
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        r = await self._client.post(
            self.endpoint, headers=self._session_headers(), json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}})
        if r.status_code != 200:
            raise MCPError(f"MCP tools/call http {r.status_code}: {r.text[:200]}")
        msgs = _parse_sse(r.text)
        if not msgs:
            raise MCPError("no MCP result in response")
        last = msgs[-1]
        if last.get("error"):
            raise MCPError(f"MCP tool error: {last['error']}")
        result = last.get("result")
        if not isinstance(result, dict):
            raise MCPError(f"MCP tools/call unexpected payload: {r.text[:200]}")
        if result.get("isError"):
            raise MCPError(f"MCP tool reported error: {result}")
        return result

    async def close(self) -> None:
        await self._client.aclose()


async def searxng_search(cfg: Config, api_key: str, query: str,
                         limit: int = 6) -> list[str]:
    """Search searxng through the LiteLLM MCP. Returns ranked text snippets.

    Bounded and best-effort: any failure degrades to no snippets (the news
    pipeline still falls back to a model-written bulletin), never crashes.
    """
    if not api_key:
        log.warning("no LiteLLM token; searxng search disabled")
        return []
    sess = MCPSession(cfg.hosts.searxng, api_key)
    try:
        await sess.initialize()
        result = await sess.call_tool("web_search-searxng_web_search", {
            "query": query, "limit": limit, "result_detail": "compact"})
        out: list[str] = []
        for item in result.get("content", []):
            if item.get("type") == "text" and item.get("text"):
                out.append(str(item["text"]))
        return out
    except Exception as e:  # noqa: BLE001 - degraded, never fatal
        log.warning("searxng MCP search failed: %s", e)
        return []
    finally:
        await sess.close()

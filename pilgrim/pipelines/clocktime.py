"""Real local time for the DJ (OVERHAUL 5.3 — fixes 'always 10:30').

Gets the local time for the configured station timezone through the LiteLLM MCP
`time-get_current_time` tool (resolved by SUFFIX, never hardcoded). On ANY
failure it falls back to `zoneinfo`, so the DJ never goes without a time just
because the MCP is down. `spoken_time` renders the time loosely (rounded to the
nearest 5 minutes) so the voice never sounds like it's reading a clock.
"""
from __future__ import annotations

import json as _json
import logging
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from pilgrim.config import Config
from pilgrim.logging_setup import err_text
from pilgrim.pipelines.mcp import MCPSession

log = logging.getLogger("radio.time")

# tool-name cache per MCP endpoint: resolved by suffix on first use
_TOOL_CACHE: dict[str, str] = {}


def _resolve_time_tool(names: list[str]) -> str:
    """Return the first tool ending in `get_current_time` (the LiteLLM MCP may
    prefix names, e.g. `time-get_current_time`)."""
    for n in names:
        if n.endswith("get_current_time"):
            return n
    raise ValueError("no get_current_time tool on MCP server")


async def current_local_time(
        cfg: Config, api_key: str,
        session_factory: Callable[..., MCPSession] | None = None) -> datetime:
    """Local time via the MCP time tool; falls back to zoneinfo on any failure."""
    factory = session_factory or MCPSession
    try:
        session = factory(cfg.hosts.searxng, api_key)
        try:
            await session.initialize()
            tool = _TOOL_CACHE.get(cfg.hosts.searxng)
            if tool is None:
                names = [t["name"] for t in await session.list_tools()]
                tool = _resolve_time_tool(names)
                _TOOL_CACHE[cfg.hosts.searxng] = tool
            result = await session.call_tool(tool, {"timezone": cfg.station.timezone})
        finally:
            await session.close()
        text = result["content"][0]["text"]
        data = _json.loads(text)
        return datetime.fromisoformat(data["datetime"])
    except Exception as e:  # noqa: BLE001 - never let the MCP silence the DJ
        log.warning("time.fallback", extra={"error": err_text(e)})
        return datetime.now(ZoneInfo(cfg.station.timezone))


def _hour_word(h: int) -> str:
    words = {0: "twelve", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
             6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven",
             12: "twelve"}
    return words[h % 12 or 12]


def _period(h: int) -> str:
    if 5 <= h < 12:
        return "in the morning"
    if 12 <= h < 17:
        return "in the afternoon"
    if 17 <= h < 21:
        return "in the evening"
    return "at night"


def spoken_time(dt: datetime) -> str:
    """Round to the nearest 5 minutes, loose phrasing: 'just after ten',
    'about quarter past ten', 'coming up on eleven', with a time-of-day period.
    Deterministic (a pure function of the datetime)."""
    h, m = dt.hour, dt.minute
    period = _period(h)
    if m >= 56:  # just before the hour: "coming up on eleven"
        hh = (h + 1) % 24
        return f"coming up on {_hour_word(hh)} {_period(hh)}"
    mm = ((m + 2) // 5) * 5  # round to nearest 5
    base = _hour_word(h)
    nxt = _hour_word((h + 1) % 24)
    if mm <= 5:
        return f"just after {base} {period}"
    if mm == 10:
        return f"about ten past {base} {period}"
    if mm == 15:
        return f"about quarter past {base} {period}"
    if mm == 20:
        return f"about twenty past {base} {period}"
    if mm == 25:
        return f"about twenty-five past {base} {period}"
    if mm == 30:
        return f"about half past {base} {period}"
    if mm == 35:
        return f"about twenty-five to {nxt} {period}"
    if mm == 40:
        return f"about twenty to {nxt} {period}"
    if mm == 45:
        return f"about quarter to {nxt} {period}"
    if mm == 50:
        return f"about ten to {nxt} {period}"
    if mm == 55:
        return f"about five to {nxt} {period}"
    return f"just after {base} {period}"

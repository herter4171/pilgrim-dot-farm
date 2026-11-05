"""Fake Brave Search API (BRAVE.md Step 7): an httpx MockTransport that answers
`/res/v1/news/search` and `/res/v1/web/search` with the observed shapes
(docs/backends.md §11), plus failure injection. Tests never reach the network
(AGENTS §1.2)."""
from __future__ import annotations

import json
from typing import Any

import httpx


def news_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Observed news shape: `{type, query, results[]}`."""
    return {
        "type": "news",
        "query": {"original": "q", "spellcheck_off": False, "show_strict_warning": False},
        "results": [{
            "type": "news_result",
            "title": r.get("title", "Headline"),
            "url": r.get("url", "https://example.test/story"),
            "description": r.get("description", ""),
            "age": r.get("age", "2 hours ago"),
            "page_age": "2026-11-04T02:49:19",
            "meta_url": {"scheme": "https", "netloc": r.get("host", "example.test"),
                         "hostname": "www." + r.get("host", "example.test")},
            "thumbnail": {"src": "https://imgs.search.brave.com/x"},
            "extra_snippets": r.get("extra_snippets", []),
        } for r in rows],
    }


def web_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Observed web shape; **no `web` key** when nothing matched."""
    body: dict[str, Any] = {
        "type": "search",
        "query": {"original": "q", "altered": None, "is_navigational": bool(rows)},
        "mixed": {"type": "mixed", "main": [], "top": [], "side": []},
    }
    if rows:
        body["web"] = {"type": "search", "family_friendly": True, "results": [{
            "title": r.get("title", "Result"),
            "url": r.get("url", "https://example.test/r"),
            "description": r.get("description", ""),
            "profile": {"name": "Example"},
        } for r in rows]}
    return body


class FakeBrave:
    """`transport` plugs into `BraveSearch(cfg, key, transport=...)`.

    `news_rows` / `web_rows` are the results to return. `fail` is a list of
    outcomes consumed one per request before the normal answer: an int is an
    HTTP status, `"timeout"` raises httpx.ReadTimeout, `"garbage"` returns a
    non-JSON 200. `requests` records every (path, query dict, headers)."""

    def __init__(self, news_rows: list[dict[str, Any]] | None = None,
                 web_rows: list[dict[str, Any]] | None = None,
                 fail: list[int | str] | None = None) -> None:
        self.news_rows = news_rows or []
        self.web_rows = web_rows or []
        self.fail = list(fail or [])
        self.requests: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append((request.url.path, params, dict(request.headers)))
        if self.fail:
            step = self.fail.pop(0)
            if step == "timeout":
                raise httpx.ReadTimeout("fake timeout", request=request)
            if step == "garbage":
                return httpx.Response(200, content=b"<html>nope</html>")
            return httpx.Response(int(step), json={"type": "ErrorResponse",
                                                   "error": {"status": int(step)}})
        if request.url.path.endswith("/news/search"):
            return httpx.Response(200, content=json.dumps(news_payload(self.news_rows)))
        if request.url.path.endswith("/web/search"):
            return httpx.Response(200, content=json.dumps(web_payload(self.web_rows)))
        return httpx.Response(404, json={"error": "unknown path"})

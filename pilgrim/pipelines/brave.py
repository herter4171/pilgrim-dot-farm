"""Brave Search API client (BRAVE.md; docs/backends.md §11).

Two endpoints, both plain GETs with the key in `X-Subscription-Token`:

- news  `GET {hosts.brave}/res/v1/news/search?q=&count=&freshness=`
        -> `{type, query, results[]}`; a result has title, url, description,
        age, page_age, meta_url{hostname}, extra_snippets.
- web   `GET {hosts.brave}/res/v1/web/search?q=&count=&result_filter=web`
        -> `{type, query, mixed, web{results[]}}`; **no `web` key at all**
        when nothing matched (observed 2026-11-04), which is the clean
        "empty", not an error.

Every call is bounded (config `search.timeout_s`, `search.retries`) and
degrades instead of raising: the news bulletin falls back to a model-written
one and the copyright screen records "unchecked". Titles and descriptions
carry HTML entities (`&#x27;`), so they are unescaped here, once, before any
caller compares words. The key is never logged.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from pilgrim.config import Config
from pilgrim.logging_setup import err_text

log = logging.getLogger("radio.search")

NEWS_PATH = "/res/v1/news/search"
WEB_PATH = "/res/v1/web/search"


@dataclass
class SearchOutcome:
    """One query, keeping "found nothing" apart from "search broken".

    status: ``ok`` (results), ``empty`` (Brave answered, nothing matched), or
    ``degraded`` (no key, HTTP/transport error, rate limited, bad payload).
    Callers that must not mistake an outage for a clean result (the copyright
    screen) branch on this; the news pipeline treats empty and degraded alike.
    """
    status: str
    results: list[dict[str, str]] = field(default_factory=list)
    error: str = ""


class BraveError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _clean(value: Any) -> str:
    return html.unescape(str(value or "")).strip()


def _hostname(meta: Any) -> str:
    host = ""
    if isinstance(meta, dict):
        host = str(meta.get("hostname") or meta.get("netloc") or "")
    return host.removeprefix("www.")


def news_result(raw: dict[str, Any]) -> dict[str, str]:
    """Normalised news row: title, description, url, source, age."""
    return {
        "title": _clean(raw.get("title")),
        "description": _clean(raw.get("description")),
        "url": str(raw.get("url") or ""),
        "source": _hostname(raw.get("meta_url")),
        "age": _clean(raw.get("age")),
    }


def web_result(raw: dict[str, Any]) -> dict[str, str]:
    """Normalised web row (the shape `copyright.judge` reads)."""
    return {
        "title": _clean(raw.get("title")),
        "description": _clean(raw.get("description")),
        "url": str(raw.get("url") or ""),
    }


def format_news_context(results: list[dict[str, str]], limit: int | None = None) -> str:
    """Lines the anchor prompt can attribute: `- [source, age] Title. Description`.
    Web text is untrusted: it is quoted into the prompt as data, never acted on."""
    lines: list[str] = []
    for r in results[: limit or len(results)]:
        tag = ", ".join(x for x in (r.get("source", ""), r.get("age", "")) if x)
        body = r.get("title", "")
        if r.get("description"):
            body = f"{body}. {r['description']}" if body else r["description"]
        lines.append(f"- [{tag}] {body}" if tag else f"- {body}")
    return "\n".join(lines)


class BraveSearch:
    """Bounded Brave Search client. One instance per station; `last` carries
    the most recent outcome for the admin health report (no paid probe)."""

    def __init__(self, cfg: Config, api_key: str,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base = cfg.hosts.brave.rstrip("/")
        self.opts = cfg.search
        self.api_key = api_key
        self._transport = transport  # tests inject an httpx.MockTransport
        self.last: dict[str, Any] = {"status": "unused", "at": None, "error": ""}

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def healthy(self) -> bool:
        """True until a call degrades; True again after the next good one."""
        return self.configured and self.last["status"] != "degraded"

    # ---------------------------------------------------------------- public
    async def news(self, query: str, *, count: int, freshness: str = "pd") -> SearchOutcome:
        params = {"q": query, "count": count, "freshness": freshness,
                  "country": self.opts.country, "search_lang": self.opts.lang,
                  "spellcheck": "false"}
        return await self._outcome("news", NEWS_PATH, params,
                                   lambda d: [news_result(r) for r in d.get("results") or []
                                              if isinstance(r, dict)])

    async def web(self, query: str, *, count: int) -> SearchOutcome:
        params = {"q": query, "count": count, "result_filter": "web",
                  "text_decorations": "false", "country": self.opts.country,
                  "search_lang": self.opts.lang, "spellcheck": "false"}

        def rows(d: dict[str, Any]) -> list[dict[str, str]]:
            web = d.get("web")
            if not isinstance(web, dict):
                return []  # no `web` key at all = nothing matched (observed)
            return [web_result(r) for r in web.get("results") or [] if isinstance(r, dict)]
        return await self._outcome("web", WEB_PATH, params, rows)

    # -------------------------------------------------------------- internals
    async def _outcome(self, kind: str, path: str, params: dict[str, Any],
                       rows: Any) -> SearchOutcome:
        if not self.api_key:
            return self._record(SearchOutcome("degraded", error="no BRAVE_SEARCH_KEY"), kind)
        t0 = time.monotonic()
        try:
            data = await self._get(path, params)
            results = rows(data)
            out = SearchOutcome("ok", results) if results else SearchOutcome("empty")
        except BraveError as e:
            out = SearchOutcome("degraded", error=str(e)[:200])
        except Exception as e:  # noqa: BLE001 - an outage is an outcome, never fatal
            out = SearchOutcome("degraded", error=err_text(e)[:200])
        extra = {"stage": f"search.{kind}", "status": out.status, "results": len(out.results),
                 "duration_ms": round((time.monotonic() - t0) * 1000, 1)}
        if out.status == "degraded":
            log.warning("search.degraded", extra={**extra, "error": out.error})
        else:
            log.info("search.done", extra=extra)
        return self._record(out, kind)

    def _record(self, out: SearchOutcome, kind: str) -> SearchOutcome:
        self.last = {"status": out.status, "at": time.time(), "kind": kind,
                     "error": out.error}
        return out

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {"Accept": "application/json", "X-Subscription-Token": self.api_key}
        timeout = httpx.Timeout(self.opts.timeout_s, connect=min(10.0, self.opts.timeout_s))
        attempts = 1 + self.opts.retries
        last_err: BraveError | None = None
        async with httpx.AsyncClient(timeout=timeout, headers=headers,
                                     transport=self._transport) as client:
            for attempt in range(1, attempts + 1):
                try:
                    return await self._once(client, path, params)
                except BraveError as e:
                    last_err = e
                    if not e.retryable or attempt == attempts:
                        break
                except httpx.HTTPError as e:
                    last_err = BraveError(f"transport {e.__class__.__name__}", retryable=True)
                    if attempt == attempts:
                        break
                await asyncio.sleep(self.opts.backoff_s * attempt)
        assert last_err is not None
        raise last_err

    async def _once(self, client: httpx.AsyncClient, path: str,
                    params: dict[str, Any]) -> dict[str, Any]:
        r = await client.get(self.base + path, params=params)
        if r.status_code == 429:
            raise BraveError("rate limited (429)", retryable=True)
        if r.status_code >= 500:
            raise BraveError(f"http {r.status_code}", retryable=True)
        if r.status_code in (401, 403):
            raise BraveError(f"auth rejected (http {r.status_code})", retryable=False)
        if r.status_code != 200:
            raise BraveError(f"http {r.status_code}: {r.text[:120]}", retryable=False)
        try:
            data = r.json()
        except ValueError as e:
            raise BraveError("non-JSON body", retryable=False) from e
        if not isinstance(data, dict):
            raise BraveError("unexpected payload", retryable=False)
        return data

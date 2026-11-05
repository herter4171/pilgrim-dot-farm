# BRAVE.md — Web search moves from searxng to the Brave Search API

Status: **shipped 2026-11-04** (Steps 1–8). Kept as the reference for the
search client, the config keys it added, and why searxng is gone.

## 0. Why

searxng, reached as a tool on the LiteLLM MCP server, stopped being usable:
its engines were suspended most of the time (CAPTCHA / too many requests),
bing truncated quoted queries to one word, and only startpage answered the
copied-lyrics screen (docs/backends.md §5, 2026-10-31). The news bulletin was
running on "(no search results available)" more often than not. We have a
paid Brave Search API key, so the station now calls Brave directly.

Decision (operator, 2026-11-04): **direct REST client, not the Brave MCP
server.** Nothing in the station lets an LLM choose tools; the MCP server is a
Node sidecar that would translate JSON-RPC into the same REST call. A sidecar
would add a launchd unit, a Node dependency on the M5 and an unauthenticated
loopback port, and its web tool reports "no results" as `isError`, which
would blur the copyright screen's empty-vs-degraded distinction.

Also decided: the news pipeline **keeps its model-written fallback** when
search is degraded (the operator likes the mix of invented and real
headlines; RADIO §6.4's "keep the previous bulletin" is not adopted). searxng
is retired entirely; there is no fallback to it.

## 1. What changed

| Area | Change |
|---|---|
| `pilgrim/pipelines/brave.py` | New. `BraveSearch` (news + web), `SearchOutcome` tri-state (`ok` / `empty` / `degraded`), bounded retries with backoff, HTML-entity unescape, `last` outcome for health. `format_news_context` renders results for the anchor prompt |
| `pilgrim/pipelines/mcp.py` | Only `MCPSession` remains (the DJ's time tool). `searxng_search`, `searxng_lookup`, `parse_results` removed |
| `pilgrim/pipelines/news.py` | One Brave **news** search (`freshness` last 24 h), context lines carry source and age so "according to…" has a real name. Degraded or empty → model-written bulletin, as before |
| `pilgrim/pipelines/copyright.py` | Probes go through Brave **web** search as quoted phrases; `judge` is unchanged |
| `pilgrim/config.py` / `config.yaml` | `hosts.searxng` → `hosts.mcp` (time tool only); new `hosts.brave`; new `search: {timeout_s, retries, backoff_s, country, lang}`; `news.results`, `news.freshness`; `payments.copyright.engines` removed |
| `pilgrim/server.py` | One `BraveSearch` per station, shared by news and the copyright screen. Admin `/api/health` `backends.search` (was `searxng`) reports the last outcome; no paid probe. Startup warning when `BRAVE_SEARCH_KEY` is missing |
| `pilgrim/tests/smoke.py` | One 1-result Brave news query; the MCP handshake probe moves to `hosts.mcp` |
| `pilgrim/tests/fakes/fake_brave.py` | httpx `MockTransport` for Brave with failure injection |
| Docs | docs/backends.md §5 (now the time tool) and §11 (Brave, observed shapes); RADIO.md host table, §6.4, §11, §12; AGENTS.md; README; RUNBOOK §2 |

## 2. Secrets

`BRAVE_SEARCH_KEY` in the origin `.env` (or the environment), read through
`config.load_secret` like the Stripe keys. Sent as `X-Subscription-Token`.
Never logged. Tests clear it (conftest) so a developer's shell can never put
the suite on the network.

## 3. Observed (2026-11-04, docs/backends.md §11)

- Plan: rate-limit headers `x-ratelimit-limit: 50, 0` (50 req/s, no monthly
  cap reported); `extra_snippets` present → paid tier.
- News: `GET /res/v1/news/search?q=&count=&freshness=pd` → `{type, query,
  results[]}`; result has `title, url, description, age, page_age, meta_url
  {hostname}, extra_snippets, thumbnail`.
- Web: `GET /res/v1/web/search?q=&count=&result_filter=web&text_decorations=false`
  → `{type, query, mixed, web{results[]}}`. **No `web` key at all when nothing
  matched** (the clean "empty"). Titles/descriptions carry HTML entities
  (`&#x27;`, `&quot;`); unescape before matching.
- Quoted lyric lines return Genius / Musixmatch / AZLyrics pages naming the
  song (Aerosmith, Queen probes); an original line returned nothing.

## 4. Steps (all done)

1. Probe Brave news + web; record shapes.
2. Config schema + RADIO §12 in the same change.
3. News pipeline on Brave news search.
4. Copyright screen on Brave web search.
5. (No deploy change: direct REST.)
6. Health + smoke.
7. Tests with the Brave fake (never the network).
8. Docs; `make lint`, `make test`, `make sim`, `make e2e`, `make smoke`.

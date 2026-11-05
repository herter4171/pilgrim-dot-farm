"""BRAVE.md Step 7: the Brave Search client against the fake transport.

Shapes follow docs/backends.md §11 (observed 2026-11-04). Nothing here
touches the network (AGENTS §1.2): every client gets `FakeBrave.transport`,
and the no-key paths are asserted to make zero requests.
"""
from __future__ import annotations

import asyncio

import pytest
from pilgrim.pipelines import copyright as cr
from pilgrim.pipelines.brave import BraveSearch, format_news_context
from pilgrim.pipelines.news import NewsPipeline
from pilgrim.tests.fakes import FakeLLM
from pilgrim.tests.fakes.fake_brave import FakeBrave

HEADLINE = {"title": "Council approves the new orchard bridge",
            "description": "Work starts in spring, the council said.",
            "host": "example-news.test", "age": "3 hours ago"}
LYRIC = {"title": "Aerosmith – Walk This Way Lyrics | Genius Lyrics",
         "description": "Backstroke lover Always hidin&#x27; &#x27;neath the covers "
                        "&#x27;Til I talked to your daddy, he say",
         "url": "https://genius.com/Aerosmith-walk-this-way-lyrics"}


def _fast(cfg):
    cfg = cfg.model_copy(deep=True)
    cfg.search.backoff_s = 0
    return cfg


def _client(cfg, fake: FakeBrave, key: str = "test-key") -> BraveSearch:
    return BraveSearch(_fast(cfg), key, transport=fake.transport)


# ----------------------------------------------------------------- news shape
def test_news_ok_normalises_rows_and_sends_key_as_header(cfg):
    fake = FakeBrave(news_rows=[HEADLINE])
    out = asyncio.run(_client(cfg, fake).news("top news", count=5, freshness="pd"))
    assert out.status == "ok" and len(out.results) == 1
    row = out.results[0]
    assert row["source"] == "example-news.test"          # www. stripped
    assert row["age"] == "3 hours ago"
    assert row["title"] == HEADLINE["title"]
    path, params, headers = fake.requests[0]
    assert path == "/res/v1/news/search"
    assert params["q"] == "top news" and params["count"] == "5" and params["freshness"] == "pd"
    assert headers["x-subscription-token"] == "test-key"
    assert "authorization" not in headers


def test_news_empty_is_not_degraded(cfg):
    out = asyncio.run(_client(cfg, FakeBrave(news_rows=[])).news("x", count=3))
    assert out.status == "empty" and out.results == [] and out.error == ""


def test_format_news_context_lines_carry_source_and_age(cfg):
    out = asyncio.run(_client(cfg, FakeBrave(news_rows=[HEADLINE])).news("x", count=3))
    text = format_news_context(out.results)
    assert text == ("- [example-news.test, 3 hours ago] Council approves the new "
                    "orchard bridge. Work starts in spring, the council said.")


# ------------------------------------------------------------------ web shape
def test_web_unescapes_html_entities(cfg):
    out = asyncio.run(_client(cfg, FakeBrave(web_rows=[LYRIC])).web('"x" lyrics', count=5))
    assert out.status == "ok"
    assert "hidin' 'neath" in out.results[0]["description"]
    assert "&#x27;" not in out.results[0]["description"]


def test_web_missing_web_key_is_empty(cfg):
    fake = FakeBrave(web_rows=[])
    out = asyncio.run(_client(cfg, fake).web('"never written line" lyrics', count=5))
    assert out.status == "empty"
    _, params, _ = fake.requests[0]
    assert params["result_filter"] == "web" and params["text_decorations"] == "false"


# ------------------------------------------------------------ degraded paths
def test_no_key_degrades_without_a_request(cfg):
    fake = FakeBrave(news_rows=[HEADLINE])
    client = _client(cfg, fake, key="")
    out = asyncio.run(client.news("x", count=1))
    assert out.status == "degraded" and "BRAVE_SEARCH_KEY" in out.error
    assert fake.requests == []
    assert client.healthy is False


@pytest.mark.parametrize("fail", [[429], [503], ["timeout"]])
def test_retryable_failure_then_success(cfg, fail):
    fake = FakeBrave(news_rows=[HEADLINE], fail=list(fail))
    client = _client(cfg, fake)
    out = asyncio.run(client.news("x", count=1))
    assert out.status == "ok" and len(fake.requests) == 2
    assert client.healthy and client.last["status"] == "ok"


def test_retries_are_bounded(cfg):
    fake = FakeBrave(news_rows=[HEADLINE], fail=[503, 503, 503, 503])
    client = _client(cfg, fake)   # retries: 1 -> two attempts
    out = asyncio.run(client.news("x", count=1))
    assert out.status == "degraded" and "503" in out.error
    assert len(fake.requests) == 2
    assert client.healthy is False and client.last["status"] == "degraded"


def test_auth_rejection_is_not_retried(cfg):
    fake = FakeBrave(news_rows=[HEADLINE], fail=[401])
    out = asyncio.run(_client(cfg, fake).news("x", count=1))
    assert out.status == "degraded" and "auth" in out.error
    assert len(fake.requests) == 1


def test_garbage_body_is_degraded(cfg):
    out = asyncio.run(_client(cfg, FakeBrave(fail=["garbage"])).web("x", count=1))
    assert out.status == "degraded" and "JSON" in out.error


# --------------------------------------------------------------- consumers
def test_news_pipeline_feeds_context_to_the_anchor(cfg):
    llm = FakeLLM(responses={"Search context": {
        "headlines": ["h1", "h2", "h3"], "gravity": "normal", "text": "bulletin"}})
    seen: list[str] = []
    orig = llm.chat_json

    async def spy(model, system, user, max_tokens=800):
        seen.append(user)
        return await orig(model, system, user, max_tokens)
    llm.chat_json = spy  # type: ignore[method-assign]
    news = NewsPipeline(cfg, llm, search=_client(cfg, FakeBrave(news_rows=[HEADLINE])),
                        prompts={"news": "anchor prompt"})
    item = asyncio.run(news.produce_bulletin())
    assert item["type"] == "news" and item["text"] == "bulletin"
    assert "[example-news.test, 3 hours ago] Council approves" in seen[0]
    assert "(no search results available)" not in seen[0]


def test_news_pipeline_degraded_search_still_writes_a_bulletin(cfg):
    """Operator decision (BRAVE.md §0): invented news beats no news."""
    llm = FakeLLM(responses={"Search context": {
        "headlines": ["h1"], "gravity": "normal", "text": "made up"}})
    seen: list[str] = []
    orig = llm.chat_json

    async def spy(model, system, user, max_tokens=800):
        seen.append(user)
        return await orig(model, system, user, max_tokens)
    llm.chat_json = spy  # type: ignore[method-assign]
    fake = FakeBrave(fail=[500, 500])
    news = NewsPipeline(cfg, llm, search=_client(cfg, fake), prompts={"news": "p"})
    assert asyncio.run(news.produce_bulletin())["text"] == "made up"
    assert "(no search results available)" in seen[0]


def test_news_pipeline_default_client_never_networks(cfg):
    llm = FakeLLM(responses={"Search context": {"headlines": [], "text": "t"}})
    news = NewsPipeline(cfg, llm, prompts={"news": "p"})
    assert news.search.configured is False
    assert asyncio.run(news.produce_bulletin())["text"] == "t"


def test_copyright_screen_matches_through_brave_web_search(cfg):
    """A famous line as Brave returns it (HTML entities and all) is a match
    when the snippet continues with more of the submission."""
    fake = FakeBrave(web_rows=[LYRIC])
    screen = cr.CopyrightScreen(cfg, client=_client(cfg, fake))
    text = ("[verse]\nBackstroke lover always hidin' neath the covers\n"
            "Til I talked to your daddy he say\n")
    v = asyncio.run(screen.check(text))
    assert v.outcome == "match" and "Walk This Way" in v.label
    assert all(p == "/res/v1/web/search" for p, _, _ in fake.requests)
    assert all(params["q"].startswith('"') for _, params, _ in fake.requests)


ORIGINAL = "[verse]\nthe lantern in the orchard hums a tune my brother wrote"


def test_copyright_screen_empty_web_is_clear(cfg):
    screen = cr.CopyrightScreen(cfg, client=_client(cfg, FakeBrave(web_rows=[])))
    v = asyncio.run(screen.check(ORIGINAL))
    assert v.outcome == "clear" and v.degraded == 0


def test_copyright_screen_outage_is_unchecked(cfg):
    fake = FakeBrave(fail=[429, 429, 429, 429])
    screen = cr.CopyrightScreen(cfg, client=_client(cfg, fake))
    v = asyncio.run(screen.check(ORIGINAL))
    assert v.outcome == "unchecked" and v.allowed

"""Song reproducibility tests (seed + lyrics forwarded to mlx). Fakes only."""
from __future__ import annotations

import asyncio

from pilgrim.pipelines.songs import SongPipeline


class _Resp:
    status_code = 200
    content = b"FAKEWAV"


class _FakePost:
    """Captures the mlx payload; returns a fake 200 WAV response."""

    def __init__(self):
        self.payload = None

    async def post(self, url, json):
        self.payload = json
        return _Resp()


def _pipeline(cfg, tmp_path):
    sp = SongPipeline.__new__(SongPipeline)
    sp.cfg = cfg
    sp.media_dir = tmp_path
    sp.prompts = {}
    sp._client = _FakePost()
    return sp


def test_generate_forwards_seed_lyrics_and_max_duration(cfg, tmp_path):
    """duration_s = songs.max_duration_s (without it mlx-serve cuts at ~60 s)."""
    sp = _pipeline(cfg, tmp_path)
    brief = {
        "title": "Tune", "artist": "The Canning Ladies", "genre": "polka",
        "style_prompt": "a jaunty polka", "lyrics": "[verse] pour the beans",
        "seed": 12345,
    }
    asyncio.run(sp.generate(brief))
    assert sp._client.payload["duration_seconds"] == cfg.songs.max_duration_s
    assert sp._client.payload["seed"] == 12345
    assert sp._client.payload["lyrics"] == "[verse] pour the beans"
    assert sp._client.payload["prompt"] == "a jaunty polka"


def test_instrumental_has_no_lyrics_and_keeps_seed(cfg, tmp_path):
    sp = _pipeline(cfg, tmp_path)
    brief = {"style_prompt": "quiet pad", "lyrics": "", "seed": 7}
    asyncio.run(sp.generate(brief))
    assert sp._client.payload["instrumental"] is True
    assert "lyrics" not in sp._client.payload
    assert sp._client.payload["duration_seconds"] == cfg.songs.max_duration_s
    assert sp._client.payload["seed"] == 7


def test_brief_includes_json_escaped_request_text(cfg):
    """OVERHAUL 4.5: the brief's user message carries the JSON-escaped request
    text (DATA, not instructions)."""
    import json as _json

    class _Rec:
        def __init__(self):
            self.user = ""

        async def chat_json(self, model, system, user, max_tokens=800):
            self.user = user
            return {"title": "T", "artist": "A", "genre": "polka",
                    "style_prompt": "p", "lyrics": ""}

        async def close(self):
            pass

    sp = SongPipeline.__new__(SongPipeline)
    sp.cfg = cfg
    sp.prompts = {"song_brief": "write a song"}
    sp.llm = _Rec()
    asyncio.run(sp.brief([], request_text='say "hi" & more <script>'))
    assert _json.dumps('say "hi" & more <script>') in sp.llm.user


def test_request_genre_overrides_station_list(cfg):
    """A request's style wins over the fixed genre list: the list is offered
    only as a fallback and there is no recency-avoid line for requests."""
    class _Rec:
        def __init__(self):
            self.user = ""

        async def chat_json(self, model, system, user, max_tokens=800):
            self.user = user
            return {"title": "T", "artist": "A", "genre": "grunge",
                    "style_prompt": "p", "lyrics": ""}

        async def close(self):
            pass

    sp = SongPipeline.__new__(SongPipeline)
    sp.cfg = cfg
    sp.prompts = {"song_brief": "write a song"}
    sp.llm = _Rec()
    asyncio.run(sp.brief(["polka"], request_text="grunge rock about plowing"))
    assert "OVERRIDES" in sp.llm.user
    assert "Genres to pick from" not in sp.llm.user
    assert "Avoid genres (recently aired)" not in sp.llm.user
    asyncio.run(sp.brief(["polka"]))
    assert "Genres to pick from" in sp.llm.user and "polka" in sp.llm.user


def test_short_brief_asks_for_short_song(cfg):
    class _Rec:
        user = ""

        async def chat_json(self, model, system, user, max_tokens=800):
            self.user = user
            return {"title": "T", "artist": "A", "genre": "g",
                    "style_prompt": "p", "lyrics": ""}

    sp = SongPipeline.__new__(SongPipeline)
    sp.cfg = cfg
    sp.prompts = {"song_brief": "write a song"}
    sp.llm = _Rec()
    asyncio.run(sp.brief([], request_text="x", short=True))
    assert "SHORT" in sp.llm.user
    asyncio.run(sp.brief([], request_text="x"))
    assert "SHORT" not in sp.llm.user

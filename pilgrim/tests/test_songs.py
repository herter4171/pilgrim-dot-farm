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


def test_generate_forwards_seed_and_lyrics(cfg, tmp_path):
    sp = _pipeline(cfg, tmp_path)
    brief = {
        "title": "Tune", "artist": "The Canning Ladies", "genre": "polka",
        "style_prompt": "a jaunty polka", "lyrics": "[verse] pour the beans",
        "target_duration_s": 60, "seed": 12345,
    }
    asyncio.run(sp.generate(brief))
    assert sp._client.payload["seed"] == 12345
    assert sp._client.payload["lyrics"] == "[verse] pour the beans"
    assert sp._client.payload["prompt"] == "a jaunty polka"
    assert sp._client.payload["duration_s"] == 60


def test_instrumental_has_no_lyrics_and_keeps_seed(cfg, tmp_path):
    sp = _pipeline(cfg, tmp_path)
    brief = {"style_prompt": "quiet pad", "lyrics": "", "target_duration_s": 60, "seed": 7}
    asyncio.run(sp.generate(brief))
    assert sp._client.payload["instrumental"] is True
    assert "lyrics" not in sp._client.payload
    assert sp._client.payload["seed"] == 7

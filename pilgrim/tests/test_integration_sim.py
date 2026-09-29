"""OVERHAUL 6.1 — producer + scheduler integration over 6 simulated hours.

Real Scheduler/RandomSelector + real Producer with instant fake voice/song/news
pipelines. Catches cross-module regressions unit tests miss: DJ talk refill
(5.1), liner runaway (2.3), request songs airing in order with their intros
(4.7), the speech-rate gate on aired voice (2.1), and continuous coverage.
Everything returns instantly, so the test stays well under 5 s.
"""
from __future__ import annotations

import asyncio
import json as _json
from datetime import datetime  # noqa: F401  (used by the time stub below)


class FakeVoice:
    def __init__(self) -> None:
        self.calls = 0

    async def produce_item(self, role: str, target_s: float,
                           context: str | None = None) -> dict:
        self.calls += 1
        dur = max(float(target_s), 2.0)
        words = max(int(dur * 2.8), 2)  # ~2.8 w/s: inside the [1.2, 3.6] gate
        return {"media_path": f"/tmp/{role}_{self.calls}.flac", "duration_s": dur,
                "sample_rate": 24000, "channels": 1, "role": role,
                "meta": {"text": " ".join(["word"] * words)}}


class FakeSong:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    async def brief(self, genres, request_text=None):
        self.calls.append(request_text)
        return {"title": "Tune for " + (request_text or "the night")[:18],
                "artist": "The Barn Cats", "genre": "polka",
                "style_prompt": "a jaunty polka", "lyrics": "[verse] yip"}

    async def produce_song(self, brief):
        return {"type": "song", "media_path": "/tmp/s.flac", "duration_s": 45.0,
                "title": brief["title"], "artist": brief["artist"],
                "genre": brief["genre"], "meta": {"brief": brief, "seed": 1}}


class FakeNews:
    async def produce_bulletin(self):
        return {"text": "Weather in Thistledown tonight: mild and breezy.", "gravity": "normal"}


class FakeSink:
    async def close(self):
        pass


async def _stub_clock_time(cfg, api_key):
    return datetime(2026, 1, 1, 22, 2)


def test_integration_sim_over_6_hours(cfg, tmp_path):
    from pilgrim.config import RNG, SimClock, ensure_dirs
    from pilgrim.producer import Producer
    from pilgrim.scheduler import Scheduler
    from pilgrim.selector import RandomSelector
    from pilgrim.store import Store

    cfg = cfg.model_copy(deep=True)
    cfg.library.dir = str(tmp_path / "library")
    cfg.library.db = str(tmp_path / "station.db")
    ensure_dirs(cfg)
    store = Store(tmp_path / "station.db")

    clock = SimClock()
    voice, songs, news = FakeVoice(), FakeSong(), FakeNews()
    sink = FakeSink()
    prod = Producer(cfg, store, llm=sink, kokoro=sink, voice=voice, songs=songs,
                    clock=clock, prompts={}, media_dir=tmp_path / "library", api_key="",
                    rng=RNG(1), news_pipeline=news, clock_time=_stub_clock_time)
    sched = Scheduler(cfg, store, RandomSelector(RNG(7), cfg), clock, RNG(7))

    step = 30
    injected_at = {3600, 7200, 10800}
    seen: list[tuple[int, str, float, int]] = []  # (t, type, duration, item_id)
    hour_types: dict[int, set[str]] = {h: set() for h in range(7)}
    last_seq = 0
    min_cov_after_300 = float("inf")

    for t in range(0, 6 * 3600 + 1, step):
        if t in injected_at:
            store.add_request(f"request at hour {t // 3600}", cap=10)
        if t % 180 == 0:  # simulated 3-minute song-generation cost
            asyncio.run(prod.song_step())
        asyncio.run(prod.ensure_commercials())
        asyncio.run(prod.ensure_liners())
        asyncio.run(prod.ensure_dj())
        clock.advance(step)
        sched.commit_lookahead()
        if t >= 300:
            min_cov_after_300 = min(min_cov_after_300, sched.coverage())
        for r in store.program_after(last_seq):
            seen.append((t, r["type"], r["duration_s"], r["item_id"]))
            last_seq = r["seq"]
            hour_types[t // 3600].add(r["type"])

    max_wps = cfg.audio.max_words_per_s
    target_total = cfg.inventory.liners_per_bucket * len(cfg.inventory.liner_buckets_s)

    # 1. coverage never hits 0 after the first 5 minutes
    assert min_cov_after_300 > 0, "coverage dropped to 0 after the first 5 minutes"

    # 2. at least one dj_talk airs in EVERY hour (5.1 must not regress)
    for h in range(6):
        assert "dj_talk" in hour_types[h], f"no dj_talk aired in hour {h}"

    # 3. all three requests aired, in submission order, each first airing of its
    # song preceded by its intro (recycled reruns air without the consumed intro,
    # which is correct — 4.7). Seed 1 keeps the DJ-adjacency exception from firing.
    aired = sorted((r for r in store.all_requests() if r["status"] == "aired"),
                   key=lambda r: r["id"])
    assert len(aired) == 3
    assert [r["text"] for r in aired] == [
        "request at hour 1", "request at hour 2", "request at hour 3"]
    seen_requests: set[int] = set()
    for i, (_, typ, _, item_id) in enumerate(seen):
        if typ != "song":
            continue
        meta = _json.loads((store.get_item(item_id) or {}).get("meta_json") or "{}")
        rid = meta.get("request_id")
        if rid is None or rid in seen_requests:
            continue
        seen_requests.add(rid)
        assert i >= 1 and seen[i - 1][1] == "intro", "request song w/o its intro"
        imeta = _json.loads((store.get_item(seen[i - 1][3]) or {}).get("meta_json") or "{}")
        assert imeta.get("song_item_id") == item_id, "intro glued to wrong song"

    # 4. no aired voice item exceeds the speech-rate ceiling (2.1)
    for _, typ, dur, item_id in seen:
        if typ in ("liner", "commercial", "dj_talk", "news", "intro"):
            meta = _json.loads((store.get_item(item_id) or {}).get("meta_json") or "{}")
            text = meta.get("text")
            if isinstance(text, str) and text.strip():
                wps = len(text.split()) / max(dur - 0.15, 0.1)
                assert wps <= max_wps, f"{typ} at {wps:.2f} w/s exceeds {max_wps}"

    # 5. liner count stays at/under the total target (2.3 must not regress)
    assert store.count_usable_of_type("liner") <= target_total

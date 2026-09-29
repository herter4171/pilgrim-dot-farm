"""Producer tests (RADIO.md §6.1): reusable (evergreen) inventory is held at a
low-water target and NOT over-produced merely because items have aired. Fakes
only — no real backends."""
from __future__ import annotations

from conftest import make_item
from pilgrim.config import RNG, SimClock
from pilgrim.producer import Producer


class FakeVoice:
    """Minimal fake of VoicePipeline.produce_item — no llm/kokoro/render."""

    def __init__(self) -> None:
        self.calls = 0
        self.contexts: list[str | None] = []

    async def produce_item(self, role: str, target_s: float,
                           context: str | None = None) -> dict:
        self.calls += 1
        self.contexts.append(context)
        return {"media_path": f"/tmp/{role}_{self.calls}.flac", "duration_s": target_s,
                "sample_rate": 24000, "channels": 1, "role": role}


class _Sink:
    """Dummy stand-ins for the deps Producer never touches in these tests."""

    async def close(self):
        pass


def make_producer(cfg, store, voice: FakeVoice) -> Producer:
    # Duck-typed fakes in place of LLM/Kokoro/VoicePipeline/SongPipeline/NewsPipeline.
    from typing import Any
    sink: Any = _Sink()
    voice_any: Any = voice
    clock: Any = SimClock()
    return Producer(
        cfg, store, llm=sink, kokoro=sink, voice=voice_any, songs=sink,
        clock=clock, prompts={}, media_dir=cfg.library.dir, api_key="",
        rng=RNG(1), news_pipeline=sink, clock_time=_stub_clock_time)


async def _stub_clock_time(cfg, api_key):
    """Fixed local time for DJ-talk tests (no real backends)."""
    from datetime import datetime
    return datetime(2026, 1, 1, 22, 2)


def test_counts_use_usable_for_evergreen_types(cfg, tmp_env):
    """Aired evergreen items still count toward the low-water target.
    dj_talk is NOT evergreen (contextual, never recycled — OVERHAUL 5.1), so an
    aired dj clip counts 0: the producer keeps topping up fresh DJ talk."""
    _, store, _ = tmp_env
    make_item(cfg, store, "commercial", 20.0, fresh=True)
    make_item(cfg, store, "commercial", 21.0, fresh=False)   # already aired, reusable
    make_item(cfg, store, "liner", 4.0, fresh=False)         # already aired, reusable
    make_item(cfg, store, "dj_talk", 16.0, fresh=False)      # already aired, NOT reusable
    make_item(cfg, store, "news", 18.0, fresh=True)          # expiring, keep fresh
    voice = FakeVoice()
    prod = make_producer(cfg, store, voice)
    c = prod.counts()
    assert c["commercial"] == 2      # aired one still usable
    assert c["liner"] == 1
    assert c["dj_talk"] == 0         # aired DJ clips are spent (5.1)
    assert c["news"] == 1            # news is fresh-only
    assert voice.calls == 0


def test_ensure_dj_refills_after_all_clips_aired(cfg, tmp_env):
    """With dj_talk_min aired (non-fresh) DJ clips in store, ensure_dj produces
    dj_talk_min new fresh ones (OVERHAUL 5.1 — DJ talk must keep flowing)."""
    _, store, _ = tmp_env
    target = cfg.inventory.dj_talk_min
    for i in range(target):
        make_item(cfg, store, "dj_talk", 16.0 + i, fresh=False)  # all aired
    voice = FakeVoice()
    prod = make_producer(cfg, store, voice)
    import asyncio
    asyncio.run(prod.ensure_dj())
    assert store.count_fresh_of_type("dj_talk") == target
    assert voice.calls == target


def test_ensure_commercials_stops_at_usable_target(cfg, tmp_env):
    """Once `commercials_min` usable spots exist, no more are produced even if
    they've all aired (previously it counted fresh and over-produced forever)."""
    _, store, _ = tmp_env
    target = cfg.inventory.commercials_min
    for i in range(target):
        make_item(cfg, store, "commercial", 20.0 + i, fresh=False)  # all aired
    voice = FakeVoice()
    prod = make_producer(cfg, store, voice)
    n_before = store.count_usable_of_type("commercial")
    import asyncio
    asyncio.run(prod.ensure_commercials())
    assert store.count_usable_of_type("commercial") == n_before
    assert voice.calls == 0


def test_ensure_commercials_produces_when_below_target(cfg, tmp_env):
    _, store, _ = tmp_env
    target = cfg.inventory.commercials_min
    for i in range(2):
        make_item(cfg, store, "commercial", 20.0 + i)
    voice = FakeVoice()
    prod = make_producer(cfg, store, voice)
    import asyncio
    asyncio.run(prod.ensure_commercials())
    assert store.count_usable_of_type("commercial") == target
    assert voice.calls == target - 2


def test_ensure_liners_replaces_old_bucket_test(cfg, tmp_env):
    """Replaces test_ensure_liners_buckets_usable_not_fresh: the per-bucket
    logic it exercised is gone (OVERHAUL 2.3). Liners now fill to a TOTAL
    target with a per-cycle cap, so this is covered by the tests below."""
    assert cfg.inventory.liners_per_bucket * len(cfg.inventory.liner_buckets_s) > 0


def test_ensure_liners_fills_to_total_target(cfg, tmp_env):
    """One call produces at most 3; repeated calls stop at the total target."""
    _, store, _ = tmp_env
    voice = FakeVoice()
    prod = make_producer(cfg, store, voice)
    import asyncio
    target_total = cfg.inventory.liners_per_bucket * len(cfg.inventory.liner_buckets_s)
    asyncio.run(prod.ensure_liners())
    assert voice.calls <= 3
    assert voice.calls == store.count_usable_of_type("liner")
    # repeated calls fill up to the target, never beyond
    for _ in range(20):
        asyncio.run(prod.ensure_liners())
    assert store.count_usable_of_type("liner") == target_total


def test_ensure_liners_stops_at_target_when_fake_voice_fixed_short(cfg, tmp_env):
    """The runaway case: a voice that always returns 5.5 s liners (never in
    most buckets) still stops at the total target — no infinite loop (2.3)."""
    _, store, _ = tmp_env

    class FixedVoice(FakeVoice):
        async def produce_item(self, role, target_s, context=None):
            self.calls += 1
            return {"media_path": f"/tmp/{role}_{self.calls}.flac",
                    "duration_s": 5.5, "sample_rate": 24000, "channels": 1,
                    "role": role}

    voice = FixedVoice()
    prod = make_producer(cfg, store, voice)
    import asyncio
    target_total = cfg.inventory.liners_per_bucket * len(cfg.inventory.liner_buckets_s)
    for _ in range(20):
        asyncio.run(prod.ensure_liners())
    assert store.count_usable_of_type("liner") == target_total


# --------------------------------------------------------------------------- #
# OVERHAUL 4.5 — listener requests become prioritized song generations.
# --------------------------------------------------------------------------- #

class FakeSong:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def brief(self, genres, request_text=None):
        self.calls.append(("brief", request_text))
        return {"title": "Mittens the Night", "artist": "The Barn Cats",
                "genre": "polka", "style_prompt": "a jaunty polka", "lyrics": "[verse] meow"}

    async def produce_song(self, brief):
        if self.fail:
            raise RuntimeError("mlx down")
        return {"type": "song", "media_path": "/tmp/s.flac", "duration_s": 45.0,
                "title": brief["title"], "artist": brief["artist"],
                "genre": brief["genre"], "meta": {"seed": 1, "brief": brief}}


class _SongSink(_Sink):
    """Adds the song_loop-facing duck methods to the base sink."""


def make_song_producer(cfg, store, voice, songs):
    from typing import Any
    sink: Any = _SongSink()
    return Producer(cfg, store, llm=sink, kokoro=sink, voice=voice, songs=songs,
                    clock=SimClock(), prompts={}, media_dir=cfg.library.dir, api_key="",
                    rng=RNG(1), news_pipeline=sink)


def _meta(store, item_id):
    import json
    row = store.get_item(item_id)
    return json.loads(row["meta_json"] or "{}") if row else {}


def test_request_song_gets_priority_even_at_stock_target(cfg, tmp_env):
    """Stock at target + one queued request: a song_loop pass produces the
    request's song (meta.request_id set) and marks the request ready."""
    _, store, _ = tmp_env
    for _ in range(cfg.inventory.fresh_songs_ready):
        make_item(cfg, store, "song", 120.0)
    req = store.add_request("play a song for Mittens", cap=10)
    voice = FakeVoice()
    songs = FakeSong()
    prod = make_song_producer(cfg, store, voice, songs)
    import asyncio
    assert asyncio.run(prod.song_step()) is True
    # exactly one new song, carrying request_id
    new_ids = [i["id"] for i in store.list_items("song")
               if _meta(store, i["id"]).get("request_id") == req["id"]]
    assert len(new_ids) == 1
    song_id = new_ids[0]
    # an intro was made and glued via meta.song_item_id
    intros = store.list_items("intro")
    assert any(_meta(store, i["id"]).get("song_item_id") == song_id for i in intros)
    ready = store.ready_request_songs()
    assert len(ready) == 1
    assert ready[0]["id"] == req["id"]
    assert ready[0]["song_item_id"] == song_id
    # the brief got the request text as the request_text parameter
    assert ("brief", "play a song for Mittens") in songs.calls


def test_two_requests_produced_oldest_first(cfg, tmp_env):
    _, store, _ = tmp_env
    a = store.add_request("first request", cap=10)
    b = store.add_request("second request", cap=10)
    prod = make_song_producer(cfg, store, FakeVoice(), FakeSong())
    import asyncio
    asyncio.run(prod.song_step())
    assert store.get_request(a["id"])["status"] == "ready"
    assert store.get_request(b["id"])["status"] == "queued"
    asyncio.run(prod.song_step())
    assert store.get_request(b["id"])["status"] == "ready"


def test_request_song_fails_after_3_attempts(cfg, tmp_env):
    _, store, _ = tmp_env
    req = store.add_request("doomed request", cap=10)
    prod = make_song_producer(cfg, store, FakeVoice(), FakeSong(fail=True))
    import asyncio

    import pytest
    for _ in range(3):
        with pytest.raises(RuntimeError):
            asyncio.run(prod.song_step())
    assert store.get_request(req["id"])["status"] == "failed"
    # head of line cleared: nothing else is queued so next_request is None
    assert store.next_request_to_produce() is None


# --------------------------------------------------------------------------- #
# OVERHAUL 4.6 — per-song DJ intros (request intros credit the listener).
# --------------------------------------------------------------------------- #

class _RecordingVoice(FakeVoice):
    def __init__(self):
        super().__init__()
        self.role_contexts = []

    async def produce_item(self, role, target_s, context=None):
        self.role_contexts.append((role, context))
        return await super().produce_item(role, target_s, context)


def test_request_intro_context_contains_request_text(cfg, tmp_env):
    _, store, _ = tmp_env
    store.add_request("play a song for Mittens", cap=10)
    voice = _RecordingVoice()
    prod = make_song_producer(cfg, store, voice, FakeSong())
    import asyncio
    asyncio.run(prod.song_step())
    intro_calls = [ctx for role, ctx in voice.role_contexts if role == "intro"]
    assert intro_calls
    assert 'Listener request: "play a song for Mittens"' in intro_calls[0]


def test_stock_song_gets_intro_without_request_line(cfg, tmp_env):
    """Stock songs get an intro too (non-fatal, no request line), stored as a
    non-evergreen 'intro' item whose meta.song_item_id points at the song."""
    _, store, _ = tmp_env
    voice = _RecordingVoice()
    prod = make_song_producer(cfg, store, voice, FakeSong())
    import asyncio
    asyncio.run(prod.song_step())
    intros = store.list_items("intro")
    assert intros, "stock song should produce an intro"
    intro = intros[0]
    assert intro["evergreen"] == 0
    song_id = _meta(store, intro["id"])["song_item_id"]
    assert store.get_item(song_id)["type"] == "song"
    intro_calls = [ctx for role, ctx in voice.role_contexts if role == "intro"]
    assert "Listener request" not in intro_calls[0]


def test_ensure_dj_context_has_recent_songs_not_next(cfg, tmp_env):
    """OVERHAUL 5.2: DJ filler is written from songs that ALREADY played (no
    fake 'next song' claims). Context carries a recent song title."""
    _, store, _ = tmp_env
    s1 = store.add_item(type_="song", media_path="/a.flac", duration_s=120.0,
                        title="Barn Cat Boogie", artist="The Canning Ladies", genre="polka")
    s2 = store.add_item(type_="song", media_path="/b.flac", duration_s=110.0,
                        title="Pickle Parade", artist="Lil' Clementine", genre="bluegrass")
    store.append_program(s1, "song", 120.0)
    store.append_program(s2, "song", 110.0)
    voice = FakeVoice()
    prod = make_producer(cfg, store, voice)
    import asyncio
    asyncio.run(prod.ensure_dj())
    ctx = voice.contexts[0] or ""
    assert "Barn Cat Boogie" in ctx and "Pickle Parade" in ctx
    assert "Songs that played recently" in ctx
    assert "next" not in ctx.lower() and "Next song" not in ctx

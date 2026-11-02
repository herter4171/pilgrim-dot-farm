"""Producer tests (RADIO.md §6.1): reusable (evergreen) inventory is held at a
low-water target and NOT over-produced merely because items have aired. Fakes
only — no real backends."""
from __future__ import annotations

import asyncio

from conftest import make_item, valid_brief
from pilgrim.config import RNG, SimClock
from pilgrim.producer import Producer


class FakeVoice:
    """Minimal fake of VoicePipeline.produce_item — no llm/kokoro/render."""

    def __init__(self) -> None:
        self.calls = 0
        self.contexts: list[str | None] = []
        self.rendered_voices: list[str | None] = []

    async def produce_item(self, role: str, target_s: float,
                           context: str | None = None,
                           voice: str | None = None,
                           host: str | None = None) -> dict:
        self.calls += 1
        self.contexts.append(context)
        return {"media_path": f"/tmp/{role}_{self.calls}.flac", "duration_s": target_s,
                "sample_rate": 24000, "channels": 1, "role": role,
                "meta": {"text": "Pilgrim Dot Farm."}}

    async def write_copy(self, role: str, target_s: float,
                         context: str | None = None,
                         host: str | None = None) -> dict:
        self.calls += 1
        self.contexts.append(context)
        return {"text": "Pilgrim Dot Farm. [[event_name]] [[days_until]].",
                "est_duration_s": target_s, "evergreen": True}

    async def render(self, copy: dict, role: str, target_s: float,
                     voice: str | None = None) -> dict:
        self.rendered_voices.append(voice)
        return {"path": f"/tmp/{role}_{self.calls}.flac", "duration_s": target_s,
                "sample_rate": 24000, "channels": 1,
                "words": len(str(copy.get("text", "")).split()),
                "words_per_s": 2.5}

    @staticmethod
    def item_dict(role: str, copy: dict, rendered: dict) -> dict:
        return {"type": role, "media_path": rendered["path"],
                "duration_s": rendered["duration_s"],
                "sample_rate": rendered["sample_rate"],
                "channels": rendered["channels"], "role": role,
                "evergreen": bool(copy.get("evergreen", True)),
                "gravity": copy.get("gravity"),
                "meta": {"text": copy.get("text")}}


class _Sink:
    """Dummy stand-ins for the deps Producer never touches in these tests."""

    async def close(self):
        pass


def make_producer(cfg, store, voice: FakeVoice, clock_override=None) -> Producer:
    # Duck-typed fakes in place of LLM/Kokoro/VoicePipeline/SongPipeline/NewsPipeline.
    from typing import Any
    sink: Any = _Sink()
    voice_any: Any = voice
    clock: Any = clock_override or SimClock()
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
    # a few per cycle (callouts come first in the voice worker), so it takes
    # several cycles to reach the target; it never overshoots
    asyncio.run(prod.ensure_commercials())
    assert voice.calls == min(3, target - 2)
    for _ in range(target):
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

    async def brief(self, genres, request_text=None, short=False, paid=False):
        self.calls.append(("brief", request_text))
        self.short = short
        return valid_brief("country-americana", title="Mittens the Night",
                           artist="The Barn Cats", lyrics="[verse]\nmeow")

    async def produce_song(self, brief, prefer="slow"):
        await asyncio.sleep(0)  # let an early-art task start before the item
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


class FakeArtWorker:
    """Duck-typed early-art worker: renders from the brief, attaches on call."""

    def __init__(self):
        self.pending_early = set()
        self.calls = []
        self.attached = []

    async def render_brief(self, brief, token):
        self.calls.append((brief["title"], token))
        return "/tmp/early.jpg", {"scene": "early", "seed": brief.get("seed")}

    def attach(self, item_id, jpg_path, meta):
        self.attached.append((item_id, jpg_path))
        self.pending_early.discard(item_id)

    def clear_pending(self, item_id):
        self.pending_early.discard(item_id)


def test_request_song_starts_and_attaches_early_art(cfg, tmp_env):
    """The cover render starts from the brief and lands on the new song."""
    _, store, _ = tmp_env
    cfg.art.enabled = True
    req = store.add_request("early art please", cap=10)
    prod = make_song_producer(cfg, store, FakeVoice(), FakeSong())
    art = FakeArtWorker()
    prod.art_worker = art
    assert asyncio.run(prod.song_step()) is True
    song_id = store.get_request(req["id"])["song_item_id"]
    assert art.calls and art.calls[0][0] == "Mittens the Night"
    assert art.calls[0][1] == f"req{req['id']}"
    assert art.attached == [(song_id, "/tmp/early.jpg")]


def test_request_production_starts_prep_clock_at_writing(cfg, tmp_env):
    """RADIO §11/§14: the prep clock is running by the time the brief/lyrics
    are written, not at submission (queue idle is not prep work)."""
    _, store, _ = tmp_env
    req = store.add_request("start the clock", cap=10)

    class _BriefSpy(FakeSong):
        def __init__(self):
            super().__init__()
            self.prep_at_brief = None

        async def brief(self, genres, request_text=None, short=False, paid=False):
            self.prep_at_brief = store.get_request(req["id"])["prep_started_at"]
            return await super().brief(genres, request_text, short, paid)

    songs = _BriefSpy()
    prod = make_song_producer(cfg, store, FakeVoice(), songs)
    import asyncio
    assert asyncio.run(prod.song_step()) is True
    row = store.get_request(req["id"])
    assert songs.prep_at_brief, "prep clock must start before lyrics are written"
    assert row["status"] == "ready"
    assert row["prep_started_at"] >= row["created_at"]


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

    async def produce_item(self, role, target_s, context=None, voice=None, host=None):
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


def test_stock_song_gets_named_intro_and_recycles(cfg, tmp_env):
    """Revision 11: a stock song's intro is code-written (no LLM) from
    "Here's <title> from <artist>", rendered once, and EVERGREEN so it recycles
    with the song. meta.song_item_id points at the song."""
    _, store, _ = tmp_env
    voice = _RecordingVoice()
    prod = make_song_producer(cfg, store, voice, FakeSong())
    import asyncio
    asyncio.run(prod.song_step())
    intros = store.list_items("intro")
    assert intros, "stock song should produce an intro"
    intro = intros[0]
    assert intro["evergreen"] == 1, "stock intros recycle like liners"
    meta = _meta(store, intro["id"])
    song = store.get_item(meta["song_item_id"])
    assert song["type"] == "song"
    assert song["title"] in meta["text"] and song["artist"] in meta["text"]
    # no LLM call for a stock intro: write_copy was never asked for one
    assert all(role != "intro" for role, _ in voice.role_contexts)


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


def test_counts_skip_expired_dj_and_news(cfg, tmp_env):
    """Expired time-mention DJ clips and stale bulletins can't air, so they
    aren't stock — and ensure_dj refills past them (5.3)."""
    from datetime import UTC, datetime
    _, store, _ = tmp_env
    voice = FakeVoice()
    prod = make_producer(cfg, store, voice)
    wall = prod.clock.wall()
    past = datetime.fromtimestamp(wall - 60, UTC).isoformat()
    future = datetime.fromtimestamp(wall + 600, UTC).isoformat()
    make_item(cfg, store, "dj_talk", 16.0, fresh=True, expires_at=past)
    make_item(cfg, store, "dj_talk", 16.0, fresh=True, expires_at=future)
    make_item(cfg, store, "news", 18.0, fresh=True, expires_at=past)
    c = prod.counts()
    assert c["dj_talk"] == 1
    assert c["news"] == 0


class _SnoopVoice(FakeVoice):
    """Records how many songs the store held while each intro was rendering."""

    def __init__(self, store):
        super().__init__()
        self.store = store
        self.songs_during_intro: list[int] = []

    async def produce_item(self, role, target_s, context=None, voice=None, host=None):
        if role == "intro":
            self.songs_during_intro.append(len(self.store.list_items("song")))
        return await super().produce_item(role, target_s, context)

    async def render(self, copy, role, target_s, voice=None):
        if role == "intro":  # stock intros are code-written (revision 11)
            self.songs_during_intro.append(len(self.store.list_items("song")))
        return await super().render(copy, role, target_s)


def test_song_not_in_library_while_its_intro_renders(cfg, tmp_env):
    """The scheduler shares the event loop: if the song is stored before its
    intro renders, it airs bare as a stock song in that gap (the 23:11 and
    23:22 incidents). Song, intro and 'ready' land together."""
    import asyncio
    _, store, _ = tmp_env
    voice = _SnoopVoice(store)
    prod = make_song_producer(cfg, store, voice, FakeSong())
    req = store.add_request("play a song for Mittens", cap=10)
    asyncio.run(prod.song_step())  # request song
    asyncio.run(prod.song_step())  # stock song (below fresh target)
    assert voice.songs_during_intro == [0, 1]
    assert store.get_request(req["id"])["status"] == "ready"
    # the stock intro is code-written, so it carries no LLM context at all
    assert not [c for c in voice.contexts if c and "Listener request" not in c]


def test_ensure_station_ids_builds_the_emergency_pack_once(cfg, tmp_env):
    _, store, _ = tmp_env
    voice = FakeVoice()
    prod = make_producer(cfg, store, voice)
    import asyncio
    asyncio.run(prod.ensure_station_ids())
    asyncio.run(prod.ensure_station_ids())  # idempotent
    ids = store.list_items("station_id")
    assert len(ids) == cfg.inventory.station_ids_min == voice.calls
    assert all(i["emergency"] and i["evergreen"] for i in ids)
    # emergency items never count as ordinary liner stock (§8.3)
    assert store.count_usable_of_type("station_id") == 0


class _DownSong(FakeSong):
    async def produce_song(self, brief, prefer="slow"):
        self.calls.append(("produce", brief.get("request_id")))
        import httpx
        raise httpx.ReadError("connection dropped")


def test_backend_outage_does_not_burn_request_attempts(cfg, tmp_env):
    """A transport error (generator down) re-queues the request without
    spending an attempt, however many times it happens; the brief carries the
    request_id for log tracing."""
    import asyncio

    import httpx
    import pytest
    _, store, _ = tmp_env
    req = store.add_request("grunge song about plowing", cap=10)
    songs = _DownSong()
    prod = make_song_producer(cfg, store, FakeVoice(), songs)
    for _ in range(5):
        with pytest.raises(httpx.TransportError):
            asyncio.run(prod.song_step())
    r = store.get_request(req["id"])
    assert r["status"] == "queued"
    assert int(r["attempts"] or 0) == 0
    assert ("produce", req["id"]) in songs.calls


def test_real_failure_still_counts_attempts(cfg, tmp_env):
    import asyncio

    import pytest
    _, store, _ = tmp_env
    req = store.add_request("a song", cap=10)
    prod = make_song_producer(cfg, store, FakeVoice(), FakeSong(fail=True))
    for _ in range(3):
        with pytest.raises(RuntimeError):
            asyncio.run(prod.song_step())
    assert store.get_request(req["id"])["status"] == "failed"


class _FailingVoice(FakeVoice):
    """LLM-written intros always fail; direct render (the scripted fallback)
    works."""

    def __init__(self) -> None:
        super().__init__()
        self.rendered: list[str] = []

    async def produce_item(self, role, target_s, context=None, voice=None, host=None):
        self.calls += 1
        raise ValueError("voice speech rate 3.9 w/s outside [1.2,3.6]")

    async def render(self, copy, role, target_s):
        self.rendered.append(copy["text"])
        return {"path": "/tmp/fallback.flac", "duration_s": 4.0,
                "sample_rate": 24000, "channels": 1}


def test_request_intro_always_exists_via_scripted_fallback(cfg, tmp_env):
    """A request song never airs without its 'you asked, we made it' intro:
    two LLM tries, then a scripted shout-out naming the song."""
    import asyncio
    _, store, _ = tmp_env
    req = store.add_request("grunge about plowing", cap=None)
    voice = _FailingVoice()
    prod = make_song_producer(cfg, store, voice, FakeSong())
    assert asyncio.run(prod.song_step()) is True
    r = store.get_request(req["id"])
    assert r["status"] == "ready" and r["intro_item_id"] is not None
    assert voice.calls == 2
    assert voice.rendered and "Request line!" in voice.rendered[0]
    assert "Mittens the Night" in voice.rendered[0]


def test_request_intro_prompt_demands_shout_out(cfg, tmp_env):
    import asyncio
    _, store, _ = tmp_env
    store.add_request("a song for Mittens", cap=None)
    voice = FakeVoice()
    asyncio.run(make_song_producer(cfg, store, voice, FakeSong()).song_step())
    assert any(c and "listener request" in c.lower() and "shout-out" in c
               for c in voice.contexts)


def test_request_backlog_asks_for_short_songs(cfg, tmp_env):
    import asyncio
    _, store, _ = tmp_env
    for i in range(4):
        store.add_request(f"request {i}", cap=None)
    songs = FakeSong()
    asyncio.run(make_song_producer(cfg, store, FakeVoice(), songs).song_step())
    assert songs.short is True


# --------------------------------------------------------------------------- #
# RADIO §6.1 — stock songs are spaced by songs.stock_gap_s; requests skip it.
# --------------------------------------------------------------------------- #

def test_stock_songs_wait_stock_gap_between_generations(cfg, tmp_env):
    import asyncio
    cfg, store, _ = tmp_env
    assert cfg.songs.stock_gap_s == 600  # default from config.yaml
    prod = make_song_producer(cfg, store, FakeVoice(), FakeSong())
    assert asyncio.run(prod.song_step()) is True  # first stock song: no wait
    assert asyncio.run(prod.song_step()) is False  # below target, but inside the gap
    prod.clock.advance(599)
    assert asyncio.run(prod.song_step()) is False
    prod.clock.advance(1)
    assert asyncio.run(prod.song_step()) is True
    assert len(store.list_items("song")) == 2


def test_request_skips_stock_gap_and_does_not_reset_it(cfg, tmp_env):
    import asyncio
    cfg, store, _ = tmp_env
    cfg.inventory.fresh_songs_ready = 5  # keep the fresh target out of the way
    prod = make_song_producer(cfg, store, FakeVoice(), FakeSong())
    assert asyncio.run(prod.song_step()) is True  # stock song starts the gap
    req = store.add_request("play a song for Mittens", cap=10)
    assert asyncio.run(prod.song_step()) is True  # request jumps the gap
    assert store.get_request(req["id"])["status"] == "ready"
    prod.clock.advance(600)  # gap counts from the stock song, not the request
    assert asyncio.run(prod.song_step()) is True


def test_stock_gap_zero_means_back_to_back(cfg, tmp_env):
    import asyncio
    cfg, store, _ = tmp_env
    cfg.songs.stock_gap_s = 0
    prod = make_song_producer(cfg, store, FakeVoice(), FakeSong())
    assert asyncio.run(prod.song_step()) is True
    assert asyncio.run(prod.song_step()) is True


# --------------------------------------------------------------------------- #
# RADIO §6.1 — songs.stock_max_library pauses the expensive stock song worker
# once the library is full; listener requests still produce.
# --------------------------------------------------------------------------- #

def test_stock_generation_pauses_at_library_cap(cfg, tmp_env):
    import asyncio
    cfg, store, _ = tmp_env
    cfg.songs.stock_gap_s = 0
    cfg.songs.stock_max_library = 3
    cfg.inventory.fresh_songs_ready = 10  # keep the fresh target out of the way
    songs = FakeSong()
    prod = make_song_producer(cfg, store, FakeVoice(), songs)
    for _ in range(3):
        assert asyncio.run(prod.song_step()) is True
    assert store.count_usable_of_type("song") == 3
    assert prod.stock_capped() is True
    assert asyncio.run(prod.song_step()) is False  # at cap: no mlx call
    assert len([c for c in songs.calls if c[1] is None]) == 3
    # a listener request still produces, even at the cap
    req = store.add_request("play a song for Mittens", cap=10)
    assert asyncio.run(prod.song_step()) is True
    assert store.get_request(req["id"])["status"] == "ready"
    assert store.count_usable_of_type("song") == 4


def test_stock_cap_zero_disables_the_cap(cfg, tmp_env):
    import asyncio
    cfg, store, _ = tmp_env
    cfg.songs.stock_gap_s = 0
    cfg.songs.stock_max_library = 0
    cfg.inventory.fresh_songs_ready = 10  # keep the fresh target out of the way
    for _ in range(5):
        make_item(cfg, store, "song", 120.0)
    prod = make_song_producer(cfg, store, FakeVoice(), FakeSong())
    assert prod.stock_capped() is False
    assert asyncio.run(prod.song_step()) is True


def test_default_library_cap_is_100(cfg):
    assert cfg.songs.stock_max_library == 100



def test_ensure_promos_steady_kinds_without_a_campaign(cfg, tmp_env):
    """With no active campaign the producer fills the steady self-promotion:
    request-line plugs, paid-tier promos and one Tree Fiddy Report. All are
    code-written (no LLM) and the report uses the administrator's voice."""
    import asyncio
    import json
    cfg.promotions.campaigns = []
    _, store, _ = tmp_env
    voice = FakeVoice()
    prod = make_producer(cfg, store, voice)
    asyncio.run(prod.ensure_promos())
    items = store.list_items("promo")
    kinds = [json.loads(i["meta_json"])["kind"] for i in items]
    assert kinds.count("request_line") == 2
    assert kinds.count("paid_tier") == 2
    assert kinds.count("tree_fiddy") == 1
    tf = [i for i in items if json.loads(i["meta_json"])["kind"] == "tree_fiddy"][0]
    meta = json.loads(tf["meta_json"])
    assert meta["paid_aired_7d"] == 0
    assert "no priority requests" in meta["text"]
    assert "am_justin" in voice.rendered_voices
    paid = [i for i in items if json.loads(i["meta_json"])["kind"] == "paid_tier"]
    assert all(cfg.station.name in json.loads(i["meta_json"])["text"]
               for i in paid)


def test_tree_fiddy_report_counts_real_paid_airings(cfg, tmp_env):
    import asyncio
    import json
    cfg.promotions.campaigns = []
    _, store, _ = tmp_env
    rid = store.add_request("a song about the night bus", None, priority=1)["id"]
    store.mark_request_aired(rid)
    import time as _time
    prod = make_producer(cfg, store, FakeVoice(),
                         clock_override=SimClock(epoch=_time.time()))
    asyncio.run(prod.ensure_promos())
    tf = [i for i in store.list_items("promo")
          if json.loads(i["meta_json"])["kind"] == "tree_fiddy"][0]
    meta = json.loads(tf["meta_json"])
    assert meta["paid_aired_7d"] == 1
    assert "1 priority request aired" in meta["text"]


def test_ensure_promos_campaign_fills_day_tokens_and_expires_at_midnight(cfg, tmp_env):
    """Campaign promos are LLM-written but the code fills the event tokens and
    stamps expiry at local midnight (revision 6)."""
    import asyncio
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from pilgrim.config import Campaign, CampaignStep
    _, store, _ = tmp_env
    today = datetime.now(ZoneInfo(cfg.station.timezone)).date().isoformat()
    cfg.promotions.campaigns = [Campaign(
        id="t", label="Thanksgiving", date="2026-11-26", cta="the request line",
        lines=["a song for the table"],
        schedule=[CampaignStep.model_validate(
            {"from": today, "until": today, "per_hour": 1})])]
    import time as _time
    prod = make_producer(cfg, store, FakeVoice(),
                         clock_override=SimClock(epoch=_time.time()))
    asyncio.run(prod.ensure_promos())
    items = [i for i in store.list_items("promo")
             if json.loads(i["meta_json"]).get("kind") == "campaign"]
    assert len(items) == cfg.inventory.promos_min
    meta = json.loads(items[0]["meta_json"])
    assert meta["campaign_id"] == "t"
    assert "Thanksgiving" in meta["text"]
    assert cfg.station.name in meta["text"]
    assert items[0]["expires_at"] is not None


def test_ensure_time_checks_renders_one_per_upcoming_mark(cfg, tmp_env):
    import asyncio
    import json
    _, store, _ = tmp_env
    prod = make_producer(cfg, store, FakeVoice())
    asyncio.run(prod.ensure_time_checks())
    items = store.list_items("time_check")
    assert len(items) == 12  # one 5-minute bucket per hour
    buckets = [json.loads(i["meta_json"])["bucket"] for i in items]
    assert len(set(buckets)) == 12
    assert all(b % 300 == 0 for b in buckets)
    assert all(i["expires_at"] for i in items)


def test_host_shifts_by_day_part(cfg, tmp_env):
    """Q2: two hosts, one per shift. Evening -> the evening host, morning ->
    the morning host (ANNOUNCER_OVERHAUL §10)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from pilgrim.config import Host
    cfg.talk.hosts = [
        Host(id="liam", name="Liam", voice="am_liam",
             day_parts=["overnight", "morning drive"]),
        Host(id="mara", name="Mara", voice="af_bella",
             day_parts=["midday", "afternoon", "evening"]),
    ]
    _, store, _ = tmp_env
    tz = ZoneInfo(cfg.station.timezone)
    evening = datetime(2026, 11, 1, 18, 30, tzinfo=tz).timestamp()
    morning = datetime(2026, 11, 1, 8, 30, tzinfo=tz).timestamp()
    prod = make_producer(cfg, store, FakeVoice(),
                         clock_override=SimClock(epoch=evening))
    assert prod._current_host().name == "Mara"
    prod = make_producer(cfg, store, FakeVoice(),
                         clock_override=SimClock(epoch=morning))
    assert prod._current_host().name == "Liam"


def test_ensure_dj_renders_in_the_shift_hosts_voice(cfg, tmp_env):
    import asyncio
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from pilgrim.config import Host
    cfg.talk.hosts = [Host(id="mara", name="Mara", voice="af_bella")]
    _, store, _ = tmp_env
    voice = FakeVoice()
    tz = ZoneInfo(cfg.station.timezone)
    ts = datetime(2026, 11, 1, 18, 30, tzinfo=tz).timestamp()
    prod = make_producer(cfg, store, voice, clock_override=SimClock(epoch=ts))
    asyncio.run(prod.ensure_dj())
    assert voice.rendered_voices
    assert all(v == "af_bella" for v in voice.rendered_voices)

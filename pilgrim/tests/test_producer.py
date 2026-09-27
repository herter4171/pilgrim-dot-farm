"""Producer tests (RADIO.md §6.1): reusable (evergreen) inventory is held at a
low-water target and NOT over-produced merely because items have aired. Fakes
only — no real backends."""
from __future__ import annotations

from conftest import make_item
from pilgrim.config import RNG
from pilgrim.producer import Producer


class FakeVoice:
    """Minimal fake of VoicePipeline.produce_item — no llm/kokoro/render."""

    def __init__(self) -> None:
        self.calls = 0

    async def produce_item(self, role: str, target_s: float,
                           context: str | None = None) -> dict:
        self.calls += 1
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
    clock: Any = None
    return Producer(
        cfg, store, llm=sink, kokoro=sink, voice=voice_any, songs=sink,
        clock=clock, prompts={}, media_dir=cfg.library.dir, api_key="",
        rng=RNG(1), news_pipeline=sink)


def test_counts_use_usable_for_evergreen_types(cfg, tmp_env):
    """Aired evergreen items still count toward the low-water target."""
    _, store, _ = tmp_env
    make_item(cfg, store, "commercial", 20.0, fresh=True)
    make_item(cfg, store, "commercial", 21.0, fresh=False)   # already aired, reusable
    make_item(cfg, store, "liner", 4.0, fresh=False)         # already aired, reusable
    make_item(cfg, store, "dj_talk", 16.0, fresh=False)      # already aired, reusable
    make_item(cfg, store, "news", 18.0, fresh=True)          # expiring, keep fresh
    voice = FakeVoice()
    prod = make_producer(cfg, store, voice)
    c = prod.counts()
    assert c["commercial"] == 2      # aired one still usable
    assert c["liner"] == 1
    assert c["dj_talk"] == 1
    assert c["news"] == 1            # news is fresh-only
    assert voice.calls == 0


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


def test_ensure_liners_buckets_usable_not_fresh(cfg, tmp_env):
    """Liners per bucket are counted from all usable liners, not just unaired:
    the already-full short bucket must not be over-filled."""
    _, store, _ = tmp_env
    target = cfg.inventory.liners_per_bucket
    # exactly `target` short-bucket liners, all but one already aired (reusable)
    make_item(cfg, store, "liner", 3.0, fresh=True)
    for d in (3.5, 4.0):
        make_item(cfg, store, "liner", d, fresh=False)
    voice = FakeVoice()
    prod = make_producer(cfg, store, voice)
    import asyncio
    asyncio.run(prod.ensure_liners())
    liners = store.list_items("liner")
    short = [i for i in liners if 0 <= i["duration_s"] < 5]
    # aired liners still satisfy the bucket -> no extra production in it
    assert len(short) == target
    # but the empty longer buckets must have been filled to target
    for lo, hi in ((5, 9), (9, 15), (15, 20), (20, 60)):
        b = [i for i in liners if lo <= i["duration_s"] < hi]
        assert len(b) == target

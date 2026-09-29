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

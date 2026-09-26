"""Producer (RADIO.md §6): demand-driven inventory refill.

Loops keep inventory at low-water targets using real pipelines. Slow work
(songs via mlx) runs in its own background task so voice inventory never stalls.
Playout never waits on production: the scheduler only commits rendered items.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Dict, List, Optional

from config import Clock, Config, RNG
from pipelines.llm import LLM, LLMError
from pipelines.news import NewsPipeline
from pipelines.songs import SongPipeline
from pipelines.voice import KokoroClient, VoicePipeline
from store import Store

log = logging.getLogger("radio.producer")

_LINER_BUCKETS = [(0, 5), (5, 9), (9, 15), (15, 20), (20, 60)]


class Producer:
    def __init__(self, cfg: Config, store: Store, llm: LLM, kokoro: KokoroClient,
                 voice: VoicePipeline, songs: SongPipeline, clock: Clock,
                 prompts: Dict[str, str], media_dir: Path, api_key: str, rng: RNG,
                 db=None, news_pipeline: Optional[NewsPipeline] = None):
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.kokoro = kokoro
        self.voice = voice
        self.songs = songs
        self.clock = clock
        self.prompts = prompts
        self.media_dir = media_dir
        self.rng = rng
        self.news = news_pipeline or NewsPipeline(cfg, llm, prompts)
        self._news_ok: Optional[dict] = None

    # --------------------------------------------------------------- counts
    def counts(self) -> Dict[str, int]:
        return self.store.count_fresh_of_type_public() if hasattr(self.store, "count_fresh_of_type_public") else {
            t: self.store.count_fresh_of_type(t) for t in
            ("song", "commercial", "liner", "dj_talk", "news")}

    # ------------------------------------------------------------- producers
    async def ensure_liners(self) -> None:
        target = self.cfg.inventory.liners_per_bucket
        liners = self.store.list_items("liner")
        voiced = [i for i in liners if i["fresh"]]
        by_bucket: Dict[int, int] = {}
        for i in voiced:
            for idx, (lo, hi) in enumerate(_LINER_BUCKETS):
                if lo <= i["duration_s"] < hi:
                    by_bucket[idx] = by_bucket.get(idx, 0) + 1
        for idx, (lo, hi) in enumerate(_LINER_BUCKETS):
            have = by_bucket.get(idx, 0)
            need = target - have
            for _ in range(max(0, need)):
                target_s = float((lo + hi) / 2) if hi < 60 else 25.0
                try:
                    item = await self.voice.produce_item("liner", target_s)
                    self._store_voice(item, "liner")
                    log.info("produced liner %.1fs", item["duration_s"])
                except Exception as e:
                    log.warning("liner production failed: %s", e)
                    return  # back off; don't hammer a failing backend

    async def ensure_commercials(self) -> None:
        have = self.store.count_fresh_of_type("commercial")
        need = self.cfg.inventory.commercials_min - have
        if need <= 0:
            return
        role = "commercial"
        for _ in range(need):
            try:
                item = await self.voice.produce_item(role, 25.0)
                self._store_voice(item, role)
                log.info("produced commercial %.1fs", item["duration_s"])
            except Exception as e:
                log.warning("commercial production failed: %s", e)
                return

    async def ensure_dj(self) -> None:
        have = self.store.count_fresh_of_type("dj_talk")
        need = self.cfg.inventory.dj_talk_min - have
        if need <= 0:
            return
        for _ in range(max(0, need)):
            try:
                item = await self.voice.produce_item("dj_talk", 18.0)
                self._store_voice(item, "dj_talk", evergreen=True)
                log.info("produced dj_talk %.1fs", item["duration_s"])
            except Exception as e:
                log.warning("dj talk production failed: %s", e)
                return

    async def ensure_news(self) -> None:
        if not self.cfg.news.enabled:
            return
        latest = self.store.list_items("news")
        if latest and any(self._not_expired(i) for i in latest):
            fresh = [i for i in latest if i["fresh"]]
            if any(self._not_expired(i) for i in fresh):
                return  # a valid, unaired bulletin exists
        # need a fresh bulletin rendered as audio
        try:
            bulletin = await self.news.produce_bulletin()
            text = bulletin["text"]
            item = await self.voice.produce_item("news", 20.0, context=text)
            import datetime as dt
            import dateutil  # noqa (not used)
            from datetime import datetime, timedelta, timezone
            expires = datetime.now(timezone.utc) + timedelta(seconds=self.cfg.news.refresh_s + 900)
            self._store_voice(item, "news", evergreen=False,
                              expires_at=expires.isoformat(), gravity=bulletin["gravity"])
            self._news_ok = bulletin
            log.info("produced news bulletin (gravity=%s)", bulletin["gravity"])
        except Exception as e:
            log.warning("news production failed: %s", e)

    def _not_expired(self, item: dict) -> bool:
        exp = item.get("expires_at")
        if not exp:
            return True
        from datetime import datetime
        try:
            return datetime.fromisoformat(exp).timestamp() > __import__("time").time()
        except Exception:
            return True

    def _store_voice(self, item: dict, type_: str, evergreen: bool = True,
                     expires_at: Optional[str] = None, gravity: Optional[str] = None) -> None:
        self.store.add_item(
            type_=type_, media_path=item["media_path"], duration_s=item["duration_s"],
            sample_rate=item.get("sample_rate"), channels=item.get("channels"),
            role=item.get("role"), evergreen=evergreen, fresh=True,
            expires_at=expires_at, gravity=gravity, meta=item.get("meta"))

    # --------------------------------------------------------------- loops
    async def run(self) -> None:
        log.info("producer running")
        while True:
            try:
                await self.ensure_commercials()
                await self.ensure_liners()
                await self.ensure_dj()
            except Exception as e:
                log.warning("producer voice cycle error: %s", e)
            await asyncio.sleep(6.0)

    async def news_loop(self) -> None:
        while True:
            try:
                await self.ensure_news()
            except Exception as e:
                log.warning("news loop error: %s", e)
            await asyncio.sleep(5.0)

    async def song_loop(self) -> None:
        """Slow worker: keeps generating songs (very expensive on mlx)."""
        log.info("song worker started")
        while True:
            try:
                fresh = self.store.count_fresh_of_type("song")
                if fresh >= self.cfg.inventory.fresh_songs_ready:
                    await asyncio.sleep(20.0)
                    continue
                prev_genres = self._recent_genres()
                brief = await self.songs.brief(prev_genres)
                item = await self.songs.produce_song(brief)
                self.store.add_item(
                    type_="song", media_path=item["media_path"], duration_s=item["duration_s"],
                    sample_rate=item.get("sample_rate"), channels=item.get("channels"),
                    title=item.get("title"), artist=item.get("artist"), genre=item.get("genre"),
                    evergreen=True, fresh=True, meta=item.get("meta"))
                log.info("produced song '%s' (%.1fs)", item.get("title"), item["duration_s"])
            except Exception as e:
                log.warning("song production failed: %s", e)
                await asyncio.sleep(15.0)
            await asyncio.sleep(2.0)

    def _recent_genres(self) -> List[str]:
        out = []
        for it in self.store.list_items("song"):
            if it.get("genre"):
                out.append(it["genre"])
        return out[-5:]

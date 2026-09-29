"""Producer (RADIO.md §6): demand-driven inventory refill.

Loops keep inventory at low-water targets using real pipelines. Slow work
(songs via mlx) runs in its own background task so voice inventory never stalls.
Playout never waits on production: the scheduler only commits rendered items.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC
from pathlib import Path

from pilgrim.config import RNG, Clock, Config
from pilgrim.pipelines.llm import LLM
from pilgrim.pipelines.news import NewsPipeline
from pilgrim.pipelines.songs import SongPipeline
from pilgrim.pipelines.voice import KokoroClient, VoicePipeline
from pilgrim.store import Store

log = logging.getLogger("radio.producer")


class Producer:
    def __init__(self, cfg: Config, store: Store, llm: LLM, kokoro: KokoroClient,
                 voice: VoicePipeline, songs: SongPipeline, clock: Clock,
                 prompts: dict[str, str], media_dir: Path, api_key: str, rng: RNG,
                 db=None, news_pipeline: NewsPipeline | None = None):
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
        self.news = news_pipeline or NewsPipeline(cfg, llm, api_key=api_key, prompts=prompts)
        self._news_ok: dict | None = None

    # --------------------------------------------------------------- counts
    def counts(self) -> dict[str, int]:
        # Evergreen types (song/commercial/liner) recycle: keep a usable
        # low-water stock. dj_talk is contextual and NEVER recycled (AGENTS rule
        # 10), so it only counts unaired clips (OVERHAUL 5.1). News is expiring.
        return {
            "song": self.store.count_fresh_of_type("song"),
            "commercial": self.store.count_usable_of_type("commercial"),
            "liner": self.store.count_usable_of_type("liner"),
            "dj_talk": self.store.count_fresh_of_type("dj_talk"),
            "news": self.store.count_fresh_of_type("news"),
        }

    # ------------------------------------------------------------- producers
    async def ensure_liners(self) -> None:
        """Fill liners toward a TOTAL target (liners_per_bucket * #buckets) by
        cycling the target duration through the configured buckets. The old
        per-bucket filling looped forever when rendered durations never landed
        in the target bucket (OVERHAUL 2.3). At most 3 items per call."""
        inv = self.cfg.inventory
        target_total = inv.liners_per_bucket * len(inv.liner_buckets_s)
        usable = self.store.count_usable_of_type("liner")
        log.debug("producer.need", extra={
            "item_type": "liner", "have": usable, "target": target_total})
        if usable >= target_total:
            return
        made = 0
        while made < 3 and usable < target_total:
            bucket = float(inv.liner_buckets_s[made % len(inv.liner_buckets_s)])
            try:
                item = await self.voice.produce_item("liner", bucket)
                self._store_voice(item, "liner")
                log.info("produced liner %.1fs", item["duration_s"])
                made += 1
                usable += 1
            except Exception as e:
                log.warning("liner production failed: %s", e)
                return  # back off; don't hammer a failing backend

    async def ensure_commercials(self) -> None:
        have = self.store.count_usable_of_type("commercial")
        need = self.cfg.inventory.commercials_min - have
        log.debug("producer.need", extra={
            "item_type": "commercial", "have": have,
            "target": self.cfg.inventory.commercials_min})
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
        # dj_talk is contextual: only unaired clips are stock (AGENTS rule 10,
        # OVERHAUL 5.1) — aired DJ clips are gone, so we keep topping up.
        have = self.store.count_fresh_of_type("dj_talk")
        need = self.cfg.inventory.dj_talk_min - have
        log.debug("producer.need", extra={
            "item_type": "dj_talk", "have": have,
            "target": self.cfg.inventory.dj_talk_min})
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
        fresh_live = [i for i in latest if self._not_expired(i) and i["fresh"]]
        log.debug("producer.need", extra={
            "item_type": "news", "have": len(fresh_live), "target": 1})
        if latest and any(self._not_expired(i) for i in latest):
            fresh = [i for i in latest if i["fresh"]]
            if any(self._not_expired(i) for i in fresh):
                return  # a valid, unaired bulletin exists
        # need a fresh bulletin rendered as audio
        try:
            bulletin = await self.news.produce_bulletin()
            text = bulletin["text"]
            item = await self.voice.produce_item("news", 20.0, context=text)
            from datetime import datetime, timedelta
            expires = datetime.now(UTC) + timedelta(seconds=self.cfg.news.refresh_s + 900)
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
                     expires_at: str | None = None, gravity: str | None = None) -> None:
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
        """Slow worker: keeps generating songs (very expensive on mlx). Listener
        requests are produced FIRST (they jump ahead of stock), then stock songs
        fill to the fresh target (OVERHAUL 4.5)."""
        log.info("song worker started")
        while True:
            try:
                did = await self.song_step()
                if not did:
                    await asyncio.sleep(20.0)
                    continue
            except Exception as e:
                log.warning("song production failed: %s", e)
                await asyncio.sleep(15.0)
            await asyncio.sleep(2.0)

    async def song_step(self) -> bool:
        """One pass of the song worker. Returns True if it did work.
        1) Oldest queued listener request gets produced (even at stock target).
        2) Otherwise, a stock song if fresh stock is below target."""
        req = self.store.next_request_to_produce()
        if req:
            return await self._produce_request_song(req)
        fresh = self.store.count_fresh_of_type("song")
        if fresh >= self.cfg.inventory.fresh_songs_ready:
            return False
        prev_genres = self._recent_genres()
        brief = await self.songs.brief(prev_genres)
        item = await self.songs.produce_song(brief)
        song_id = self.store.add_item(
            type_="song", media_path=item["media_path"], duration_s=item["duration_s"],
            sample_rate=item.get("sample_rate"), channels=item.get("channels"),
            title=item.get("title"), artist=item.get("artist"), genre=item.get("genre"),
            evergreen=True, fresh=True, meta=item.get("meta"))
        await self._make_intro(item, song_id, None)  # stock intros are non-fatal
        log.info("song.produced", extra={
            "title": item.get("title"), "genre": item.get("genre"),
            "duration_s": item.get("duration_s")})
        return True

    async def _produce_request_song(self, req: dict) -> bool:
        """Turn one queued listener request into a song (+ short intro). On a
        hard failure, record the attempt (3 max -> failed) and re-raise so the
        loop backs off; a failing intro never blocks the request (4.5/4.6)."""
        log.info("request.producing", extra={"request_id": req["id"]})
        self.store.mark_request_producing(req["id"])
        try:
            prev_genres = self._recent_genres()
            brief = await self.songs.brief(prev_genres, request_text=req["text"])
            item = await self.songs.produce_song(brief)
            song_id = self.store.add_item(
                type_="song", media_path=item["media_path"], duration_s=item["duration_s"],
                sample_rate=item.get("sample_rate"), channels=item.get("channels"),
                title=item.get("title"), artist=item.get("artist"), genre=item.get("genre"),
                evergreen=True, fresh=True,
                meta={**dict(item.get("meta") or {}), "request_id": req["id"]})
            intro_id = await self._make_intro(item, song_id, req)
            self.store.mark_request_ready(req["id"], song_id, intro_id)
            log.info("request.ready", extra={
                "request_id": req["id"], "song_item_id": song_id,
                "title": item.get("title"), "intro_item_id": intro_id})
            return True
        except Exception as e:
            n = self.store.request_failed_attempt(req["id"])
            log.warning("request.failed", extra={
                "request_id": req["id"], "attempt": n, "error": str(e)})
            raise

    async def _make_intro(self, item: dict, song_id: int | None,
                          req: dict | None) -> int | None:
        """Produce a short DJ intro naming the just-created song, crediting the
        listener for request songs. Failure is non-fatal (returns None)."""
        try:
            title = item.get("title") or "this next one"
            artist = item.get("artist") or ""
            genre = item.get("genre") or ""
            context = f'Next song: "{title}" by {artist} ({genre}).\n'
            meta: dict = {"song_item_id": song_id, "text": None}
            if req is not None:
                context += f"Listener request: {json.dumps(req['text'])}\n"
                meta["request_id"] = req["id"]
            context += "Thank the listener for the request in one short phrase. " \
                       "Do not mention the clock time."
            intro = await self.voice.produce_item("intro", 10.0, context=context)
            meta.update({k: intro.get("meta", {}).get(k) for k in ("text",) if intro.get("meta")})
            intro_id = self.store.add_item(
                type_="intro", media_path=str(intro["media_path"]),
                duration_s=intro["duration_s"], sample_rate=intro.get("sample_rate"),
                channels=intro.get("channels"), role="intro", evergreen=False,
                fresh=True, meta=meta)
            return intro_id
        except Exception as e:
            log.warning("intro.failed", extra={"error": str(e)})
            return None

    def _recent_genres(self) -> list[str]:
        out = []
        for it in self.store.list_items("song"):
            if it.get("genre"):
                out.append(it["genre"])
        return out[-5:]

"""Producer (RADIO.md §6): demand-driven inventory refill.

Loops keep inventory at low-water targets using real pipelines. Slow work
(songs via mlx) runs in its own background task so voice inventory never stalls.
Playout never waits on production: the scheduler only commits rendered items.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pilgrim.config import RNG, Clock, Config
from pilgrim.logging_setup import err_text
from pilgrim.pipelines.clocktime import current_local_time, spoken_time
from pilgrim.pipelines.llm import LLM
from pilgrim.pipelines.news import NewsPipeline
from pilgrim.pipelines.sfx import SFX_HOST_TYPES, SfxPipeline, beat_offset
from pilgrim.pipelines.songs import SongPipeline
from pilgrim.pipelines.voice import KokoroClient, VoicePipeline
from pilgrim.store import Store

log = logging.getLogger("radio.producer")


class Producer:
    def __init__(self, cfg: Config, store: Store, llm: LLM, kokoro: KokoroClient,
                 voice: VoicePipeline, songs: SongPipeline, clock: Clock,
                 prompts: dict[str, str], media_dir: Path, api_key: str, rng: RNG,
                 db=None, news_pipeline: NewsPipeline | None = None,
                 clock_time=None, sfx: SfxPipeline | None = None) -> None:
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
        self.api_key = api_key
        self.news = news_pipeline or NewsPipeline(cfg, llm, api_key=api_key, prompts=prompts)
        # real local time for DJ talk; injectable in tests (OVERHAUL 5.3)
        self._clock_time = clock_time or current_local_time
        self._news_ok: dict | None = None
        self.sfx = sfx  # None = station airs dry (no SFX backend configured)

    # --------------------------------------------------------------- counts
    def counts(self) -> dict[str, int]:
        # Evergreen types (song/commercial/liner) recycle: keep a usable
        # low-water stock. dj_talk is contextual and NEVER recycled (AGENTS rule
        # 10), so it only counts unaired clips (OVERHAUL 5.1). News is expiring.
        # This is THE inventory count: /api/health reports it and the ensure_*
        # loops refill against it, so the site and the producer never disagree.
        return {
            "song": self.store.count_fresh_of_type("song"),
            "commercial": self.store.count_usable_of_type("commercial"),
            "liner": self.store.count_usable_of_type("liner"),
            "dj_talk": self._count_fresh_unexpired("dj_talk"),
            "field_report": self._count_fresh_unexpired("field_report"),
            "news": self._count_fresh_unexpired("news"),
            "sfx": self.count_sfx_stock(),
        }

    def sfx_stock_target(self) -> int:
        """Approved evergreen cues: the stock stinger pool's target size."""
        if self.sfx is None or not self.cfg.sfx.enabled:
            return 0
        return sum(1 for c in self.cfg.sfx.cues.values() if c.evergreen and c.approved)

    def count_sfx_stock(self) -> int:
        approved = {n for n, c in self.cfg.sfx.cues.items() if c.evergreen and c.approved}
        if self.sfx is None or not self.cfg.sfx.enabled:
            return 0
        return len({self._sfx_cue(it) for it in self.store.list_items("sfx")
                    if it["evergreen"]} & approved)

    def _count_fresh_unexpired(self, type_: str) -> int:
        """Unaired clips the scheduler can still air: expired time-mention DJ
        clips (5.3) and stale bulletins are not stock."""
        now = self.clock.wall()
        n = 0
        for it in self.store.list_items(type_):
            if not it["fresh"] or it["emergency"]:
                continue
            exp = it.get("expires_at")
            try:
                if exp and datetime.fromisoformat(exp).timestamp() <= now:
                    continue
            except ValueError:
                pass
            n += 1
        return n

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
                await self._attach_sfx(item, "liner")
                self._store_voice(item, "liner")
                log.info("produced liner %.1fs", item["duration_s"])
                made += 1
                usable += 1
            except Exception as e:
                log.warning("liner production failed: %s", err_text(e))
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
                await self._attach_sfx(item, role)
                self._store_voice(item, role)
                log.info("produced commercial %.1fs", item["duration_s"])
            except Exception as e:
                log.warning("commercial production failed: %s", err_text(e))
                return

    async def ensure_dj(self) -> None:
        # dj_talk is contextual: only unaired clips are stock (AGENTS rule 10,
        # OVERHAUL 5.1) — aired DJ clips are gone, so we keep topping up.
        have = self._count_fresh_unexpired("dj_talk")
        need = self.cfg.inventory.dj_talk_min - have
        log.debug("producer.need", extra={
            "item_type": "dj_talk", "have": have,
            "target": self.cfg.inventory.dj_talk_min})
        if need <= 0:
            return
        # Real material to talk about: the songs that just played (chosen NOW,
        # so no fake 'next song' claims) and the real local time (5.3).
        recent = self._recent_committed_songs(3)
        context_parts: list[str] = []
        if recent:
            context_parts.append("Songs that played recently: " + "; ".join(
                f'"{t}" by {a} ({g})' for t, a, g in recent))
        try:
            now_dt = await self._clock_time(self.cfg, self.api_key)
            context_parts.append(f"Time of day right now: {spoken_time(now_dt)}")
        except Exception:
            pass  # never block DJ production on the time
        context = "\n".join(context_parts)
        # DJ clips that mention the time expire, so a stale 'just after ten'
        # never airs hours later (5.3). Based on the injected clock so tests on
        # SimClock and production agree about what 'now' is.
        wall = self.clock.wall()
        expires = (datetime.fromtimestamp(wall, UTC) + timedelta(
            seconds=self.cfg.talk.time_mention_ttl_s)).isoformat()
        for _ in range(max(0, need)):
            try:
                item = await self.voice.produce_item("dj_talk", 18.0, context=context)
                await self._attach_sfx(item, "dj_talk")
                self._store_voice(item, "dj_talk", evergreen=True, expires_at=expires)
                log.info("produced dj_talk %.1fs", item["duration_s"])
            except Exception as e:
                log.warning("dj talk production failed: %s", err_text(e))
                return

    async def ensure_field_reports(self) -> None:
        """Keep `field_reports_min` unaired field reports (SFX.md §4.2). Like
        dj_talk they are contextual: aired once, never recycled."""
        have = self._count_fresh_unexpired("field_report")
        need = self.cfg.inventory.field_reports_min - have
        log.debug("producer.need", extra={
            "item_type": "field_report", "have": have,
            "target": self.cfg.inventory.field_reports_min})
        for _ in range(max(0, need)):
            try:
                item = await self.voice.produce_item(
                    "field_report", 20.0,
                    context="Do not mention the clock time.")
                await self._attach_sfx(item, "field_report")
                self._store_voice(item, "field_report", evergreen=False)
                log.info("produced field_report %.1fs", item["duration_s"])
            except Exception as e:
                log.warning("field report production failed: %s", err_text(e))
                return

    # ------------------------------------------------------------------ sfx
    async def ensure_sfx_pool(self) -> int:
        """Render each approved evergreen cue once into the recycled stock pool
        (SFX.md §6.2). Contextual cues are rendered per host in _attach_sfx.
        Returns how many were made; a failing backend just leaves gaps."""
        if self.sfx is None or not self.cfg.sfx.enabled:
            return 0
        have = {self._sfx_cue(it) for it in self.store.list_items("sfx")}
        made = 0
        for name, cue in self.cfg.sfx.cues.items():
            if not (cue.evergreen and cue.approved) or name in have:
                continue
            try:
                r = await self.sfx.render(name)
            except Exception as e:
                log.warning("sfx.failed", extra={"cue": name, "error": err_text(e)})
                return made
            self._store_sfx(r, evergreen=True)
            made += 1
        return made

    @staticmethod
    def _sfx_cue(it: dict) -> str | None:
        try:
            return (json.loads(it.get("meta_json") or "{}") or {}).get("cue")
        except ValueError:
            return None

    def _stock_sfx(self, name: str) -> dict | None:
        for it in self.store.list_items("sfx"):
            if it["evergreen"] and self._sfx_cue(it) == name:
                return it
        return None

    def _store_sfx(self, r: dict, evergreen: bool) -> int:
        return self.store.add_item(
            type_="sfx", media_path=r["media_path"], duration_s=r["duration_s"],
            sample_rate=r.get("sample_rate"), channels=r.get("channels"),
            role="sfx", evergreen=evergreen, fresh=True, meta=r.get("meta"))

    async def _attach_sfx(self, item: dict, role: str) -> None:
        """Resolve a rendered host's script cues to stored sfx items and record
        them as `meta.sfx` overlays (SFX.md §7.1). Which of them air (coin flip,
        rate budget) is decided at commit time by sfx_plan. Never fatal: any
        cue that can't be rendered is dropped and the host airs dry."""
        meta = item.setdefault("meta", {})
        cues = list(meta.pop("sfx_cues", None) or [])
        joke = meta.pop("joke_after_sentence", None)
        sfx = self.sfx
        if sfx is None or not self.cfg.sfx.enabled or role not in SFX_HOST_TYPES:
            return
        text = str(meta.get("text") or "")
        dur = float(item["duration_s"])
        lead = self.cfg.audio.edge_pad_ms / 1000.0
        sfx_cfg = self.cfg.sfx
        overlays: list[dict] = []

        async def resolve(name: str, **kw) -> dict | None:
            cue = sfx_cfg.cues.get(name)
            if not cue or not cue.approved:
                return None
            if cue.evergreen:
                return self._stock_sfx(name)
            try:
                r = await sfx.render(name, seed=self.rng.randint(0, 2**31 - 1), **kw)
            except Exception as e:
                log.warning("sfx.failed", extra={"cue": name, "error": err_text(e)})
                return None
            sid = self._store_sfx(r, evergreen=False)
            return {"id": sid, "duration_s": r["duration_s"]}

        wanted = [(c["cue"], "stinger", c["after_sentence"]) for c in cues]
        if joke is not None:
            wanted.append((sfx_cfg.joke_cue, "joke", joke))
        for name, kind, after in wanted:
            got = await resolve(name)
            if got:
                overlays.append({"item_id": got["id"], "cue": name, "kind": kind,
                                 "offset_s": beat_offset(text, after, dur, lead),
                                 "duration_s": got["duration_s"]})
        if role == "field_report":
            bed_s = min(sfx_cfg.bed_max_s, dur - lead)
            if bed_s >= 1.0:
                got = await resolve(sfx_cfg.bed_cue, duration_s=bed_s)
                if got:
                    overlays.append({"item_id": got["id"], "cue": sfx_cfg.bed_cue,
                                     "kind": "bed", "offset_s": 0.0,
                                     "duration_s": got["duration_s"]})
        if overlays:
            meta["sfx"] = overlays

    def _recent_committed_songs(self, n: int) -> list[tuple[str, str, str]]:
        """Last n songs in the committed program as (title, artist, genre)."""
        out: list[tuple[str, str, str]] = []
        for r in self.store.program_since(1):
            if r["type"] != "song":
                continue
            it = self.store.get_item(r["item_id"])
            if it:
                out.append((it.get("title") or "", it.get("artist") or "",
                            it.get("genre") or ""))
        return out[-n:] if n else []

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
            log.warning("news production failed: %s", err_text(e))

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
                # stock stingers first, so new talk can reference them
                await self.ensure_sfx_pool()
                await self.ensure_commercials()
                await self.ensure_liners()
                await self.ensure_dj()
                await self.ensure_field_reports()
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
                log.warning("song production failed: %s", err_text(e))
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
        # render the intro BEFORE the song enters the library: the scheduler
        # shares this event loop and would air the song bare in the gap
        intro = await self._render_intro(item, None)  # stock intros are non-fatal
        song_id = self.store.add_item(
            type_="song", media_path=item["media_path"], duration_s=item["duration_s"],
            sample_rate=item.get("sample_rate"), channels=item.get("channels"),
            title=item.get("title"), artist=item.get("artist"), genre=item.get("genre"),
            evergreen=True, fresh=True, meta=item.get("meta"))
        self._store_intro(intro, song_id, None)
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
            # Everything awaited is done before the song is stored: song, intro
            # and 'ready' land with no await between them, so the scheduler can
            # never air a request song bare, as stock, before its intro exists.
            intro = await self._render_intro(item, req)
            song_id = self.store.add_item(
                type_="song", media_path=item["media_path"], duration_s=item["duration_s"],
                sample_rate=item.get("sample_rate"), channels=item.get("channels"),
                title=item.get("title"), artist=item.get("artist"), genre=item.get("genre"),
                evergreen=True, fresh=True,
                meta={**dict(item.get("meta") or {}), "request_id": req["id"]})
            intro_id = self._store_intro(intro, song_id, req)
            self.store.mark_request_ready(req["id"], song_id, intro_id)
            log.info("request.ready", extra={
                "request_id": req["id"], "song_item_id": song_id,
                "title": item.get("title"), "intro_item_id": intro_id})
            return True
        except Exception as e:
            n = self.store.request_failed_attempt(req["id"])
            log.warning("request.failed", extra={
                "request_id": req["id"], "attempt": n, "error": err_text(e)})
            raise

    async def _render_intro(self, item: dict, req: dict | None) -> dict | None:
        """Render a short DJ intro naming the just-created song, crediting the
        listener for request songs. Failure is non-fatal (returns None)."""
        try:
            title = item.get("title") or "this next one"
            artist = item.get("artist") or ""
            genre = item.get("genre") or ""
            context = f'Next song: "{title}" by {artist} ({genre}).\n'
            if req is not None:
                context += f"Listener request: {json.dumps(req['text'])}\n"
                context += "Thank the listener for the request in one short phrase. "
            context += "Do not mention the clock time."
            return await self.voice.produce_item("intro", 10.0, context=context)
        except Exception as e:
            log.warning("intro.failed", extra={"error": err_text(e)})
            return None

    def _store_intro(self, intro: dict | None, song_id: int,
                     req: dict | None) -> int | None:
        """Store a rendered intro against its song. Synchronous on purpose."""
        if intro is None:
            return None
        meta: dict = {"song_item_id": song_id,
                      "text": (intro.get("meta") or {}).get("text")}
        if req is not None:
            meta["request_id"] = req["id"]
        return self.store.add_item(
            type_="intro", media_path=str(intro["media_path"]),
            duration_s=intro["duration_s"], sample_rate=intro.get("sample_rate"),
            channels=intro.get("channels"), role="intro", evergreen=False,
            fresh=True, meta=meta)

    def _recent_genres(self) -> list[str]:
        out = []
        for it in self.store.list_items("song"):
            if it.get("genre"):
                out.append(it["genre"])
        return out[-5:]

"""Producer (RADIO.md §6): demand-driven inventory refill.

Loops keep inventory at low-water targets using real pipelines. Slow work
(songs on the generator pool) runs in its own background task so voice
inventory never stalls; up to the pool's capacity song jobs run concurrently.
Playout never waits on production: the scheduler only commits rendered items.
"""
from __future__ import annotations

import asyncio
import contextlib
import heapq
import itertools
import json
import logging
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from pilgrim import genres
from pilgrim.config import RNG, Clock, Config
from pilgrim.logging_setup import err_text
from pilgrim.pipelines.clocktime import (
    current_local_time,
    mentions_clock,
    mentions_day,
    spoken_time,
)
from pilgrim.pipelines.llm import LLM, LLMTruncated
from pilgrim.pipelines.news import NewsPipeline
from pilgrim.pipelines.sfx import SFX_HOST_TYPES, SfxPipeline, beat_offset
from pilgrim.pipelines.songs import SongPipeline
from pilgrim.pipelines.voice import KokoroClient, VoicePipeline
from pilgrim.promotions import (
    active_campaign,
    days_until,
    fill_day_tokens,
    next_local_midnight,
    phrase_for,
    target_per_hour,
    validate_promo_copy,
    validate_promo_template,
)
from pilgrim.store import Store

log = logging.getLogger("radio.producer")

# cut-off song briefs re-queued without spending a request attempt, per
# request; after that they count like any failure (a request that always cuts
# off must still end, and a paid one get its refund flag)
LLM_FREE_CUTS = 4


class PriorityGate:
    """One holder at a time, granted to the waiter with the LOWEST key, not
    the earliest (asyncio.Lock is FIFO: a paid request re-queued after a
    cut-off brief waited behind a free one, 2026-11-01)."""

    def __init__(self) -> None:
        self._busy = False
        self._waiters: list[tuple[tuple, int, asyncio.Future]] = []
        self._seq = itertools.count()

    def _release(self) -> None:
        while self._waiters:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(None)  # hand off: stays busy for the new holder
                return
        self._busy = False

    @contextlib.asynccontextmanager
    async def hold(self, key: tuple) -> AsyncIterator[None]:
        if self._busy:
            fut = asyncio.get_running_loop().create_future()
            heapq.heappush(self._waiters, (key, next(self._seq), fut))
            try:
                await fut
            except asyncio.CancelledError:
                if fut.done() and not fut.cancelled():
                    self._release()  # granted as we were cancelled: pass it on
                raise
        else:
            self._busy = True
        try:
            yield
        finally:
            self._release()


# stock briefs queue behind every request brief (keys sort by first element)
STOCK_BRIEF_KEY = (1,)


@dataclass
class SongJob:
    """One unit of song work: brief -> render -> intro -> store (RADIO §6.1)."""
    kind: str                      # "request" | "stock"
    req: dict | None = None
    genre: str | None = None       # set once the brief exists (genre context)
    # set when the job ends (ready, failed or re-queued): releases later
    # requests held behind it
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def paid(self) -> bool:
        return bool(self.req and self.req.get("priority"))

    def queue_key(self) -> tuple:
        """Position in queue order (store.QUEUE_ORDER: paid first, then by
        paid_at / created_at, then id)."""
        r = self.req or {}
        return (-int(r.get("priority") or 0),
                str(r.get("paid_at") or r.get("created_at") or ""), int(r.get("id") or 0))

# Copy brief for the emergency station IDs (RADIO §8.3): timeless on purpose,
# since they may air any hour, any day.
STATION_ID_BRIEF = (
    "Write a plain station ID, one or two short sentences: say the station name, "
    "Pilgrim Dot Farm, and that the listener is tuned in. Timeless: no time of "
    "day, no weather, no song names, no news.")


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
        # Set by server.py after the ArtWorker exists. Early art is optional:
        # tests and art-disabled stations leave it None.
        self.art_worker: Any | None = None
        # clock.now() when the last stock song finished (None = none yet this run)
        self._last_stock_song_at: float | None = None
        # one-shot log so a capped stock worker doesn't spam every 20 s
        self._stock_cap_logged = False
        # song jobs in flight under song_loop (RADIO §6.1): task -> job
        self._jobs: dict[asyncio.Task, SongJob] = {}
        # song briefs one at a time (requests and stock): parallel briefs
        # slowed qwen38 into its ~120 s cutoff, and each brief sees the last
        # one's genre
        self._brief_gate = PriorityGate()
        # request_id -> cut-off briefs so far; the first few are free retries
        self._llm_cuts: dict[int, int] = {}
        # injectable wait for song_loop: tests drive it without wall time
        self._sleep = asyncio.sleep
        # request_id -> "waiting" (its turn at the brief LLM) | "writing"
        # (brief/lyrics) | "generating" (song model, then intro); only for
        # `producing` rows. In memory on purpose: a
        # restart re-queues producing requests anyway (RADIO §11 board stage)
        self.request_stage: dict[int, str] = {}

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
            "promo": self._count_promos(now_wall=self.clock.wall()),
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
        idx = 0
        buckets = list(inv.liner_buckets_s) or [5]
        # A bucket whose copy overshoots its duration tolerance (the 3 s bucket
        # renders ~9 s) must not pin the loop forever: try each bucket once per
        # call and move on.
        while made < 3 and usable < target_total and idx < len(buckets):
            bucket = float(buckets[idx % len(buckets)])
            idx += 1
            try:
                item = await self.voice.produce_item("liner", bucket)
                self._store_voice(item, "liner")
                log.info("produced liner %.1fs", item["duration_s"])
                made += 1
                usable += 1
            except Exception as e:
                log.warning("liner production failed (%.0fs bucket): %s",
                            bucket, err_text(e))
                continue

    async def ensure_station_ids(self) -> None:
        """Keep `inventory.station_ids_min` emergency station IDs (RADIO §8.3).
        They are the floor under 'words between songs' (§5.2): rendered once,
        never deleted, aired only when no other liner exists."""
        have = sum(1 for i in self.store.list_items("station_id") if i["emergency"])
        for _ in range(max(0, self.cfg.inventory.station_ids_min - have)):
            try:
                item = await self.voice.produce_item("liner", 4.0, context=STATION_ID_BRIEF)
            except Exception as e:
                log.warning("station id production failed: %s", err_text(e))
                return
            meta = {"text": (item.get("meta") or {}).get("text")}
            self.store.add_item(
                type_="station_id", media_path=item["media_path"],
                duration_s=item["duration_s"], sample_rate=item.get("sample_rate"),
                channels=item.get("channels"), role="liner", evergreen=True,
                emergency=True, fresh=True, meta=meta)
            log.info("produced station_id %.1fs", item["duration_s"])

    async def ensure_commercials(self) -> None:
        have = self.store.count_usable_of_type("commercial")
        need = self.cfg.inventory.commercials_min - have
        log.debug("producer.need", extra={
            "item_type": "commercial", "have": have,
            "target": self.cfg.inventory.commercials_min})
        if need <= 0:
            return
        role = "commercial"
        for _ in range(min(need, 3)):  # a few per cycle: callouts come first
            try:
                item = await self.voice.produce_item(role, 25.0)
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
        # Material to talk about: the songs that just played (chosen NOW, so
        # no fake 'next song' claims), code-computed day-level facts (revision
        # 6), a weather phrase from config, and a rotated topic. The CLOCK is
        # deliberately absent: time_check carries it (A4), so the DJ stops
        # reading the time on every break.
        recent = self._recent_committed_songs(3)
        tz = self.cfg.station.timezone
        now_wall = self.clock.wall()
        now_dt = datetime.fromtimestamp(now_wall, ZoneInfo(tz))
        context_parts: list[str] = []
        if recent:
            context_parts.append("Songs that played recently: " + "; ".join(
                f'"{t}" by {a} ({genres.display(g)})' for t, a, g in recent))
        context_parts.append(
            f"Day: {now_dt.strftime('%A')}, {now_dt.strftime('%B')} {now_dt.day}.")
        context_parts.append(f"Day part: {self._day_part(now_dt.hour)}.")
        weather = self._weather_phrase()
        if weather:
            context_parts.append(f"Thistledown weather: {weather}.")
        topic = self._pick_topic()
        if topic:
            context_parts.append(f"Topic: {topic}.")
        host = self._current_host()
        host_name = host.name if host else "Liam"
        host_voice = host.voice if host else None
        if host and host.persona:
            context_parts.append(f"Host persona: {host.persona}.")
        context_parts.extend(self._request_context())
        context = "\n".join(context_parts)
        # Expiry is stamped AFTER the LLM call (revision 3). A clock/day-part
        # statement expires on the short TTL; a weekday/date fact expires at
        # local midnight (revision 6); copy with neither is not perishable and
        # may wait on the shelf.
        for _ in range(max(0, need)):
            try:
                copy = await self.voice.write_copy(
                    "dj_talk", 18.0, context=context, host=host_name)
                reason = self._motif_reason(copy["text"])
                if reason:
                    copy = await self.voice.write_copy(
                        "dj_talk", 18.0,
                        context=context + f"\nDo not use the word/phrase "
                                          f"'{reason}' this time.",
                        host=host_name)
                    reason = self._motif_reason(copy["text"])
                    if reason:
                        log.warning("talk.motif_reuse", extra={"motif": reason})
                rendered = await self.voice.render(copy, "dj_talk", 18.0,
                                                   voice=host_voice)
                item = self.voice.item_dict("dj_talk", copy, rendered)
                text = copy["text"]
                wall = self.clock.wall()  # after the LLM call, not before
                expires = None
                if mentions_clock(text):
                    expires = (datetime.fromtimestamp(wall, UTC) + timedelta(
                        seconds=self.cfg.talk.time_mention_ttl_s)).isoformat()
                elif mentions_day(text):
                    expires = datetime.fromtimestamp(
                        next_local_midnight(wall, tz), UTC).isoformat()
                item["meta"]["topic"] = topic
                item["meta"]["motifs"] = [
                    m for m in self.cfg.talk.motif_bank if m.lower() in text.lower()]
                self._store_voice(item, "dj_talk", evergreen=True,
                                  expires_at=expires)
                log.info("produced dj_talk %.1fs", item["duration_s"])
            except Exception as e:
                log.warning("dj talk production failed: %s", err_text(e))
                return

    def _day_part(self, hour: int) -> str:
        if 5 <= hour < 11:
            return "morning drive"
        if 11 <= hour < 14:
            return "midday"
        if 14 <= hour < 17:
            return "afternoon"
        if 17 <= hour < 21:
            return "evening"
        return "overnight"

    def _current_host(self):
        """The host on shift now (by day part); None when hosts are not
        configured (single-host station)."""
        hosts = self.cfg.talk.hosts
        if not hosts:
            return None
        hour = datetime.fromtimestamp(
            self.clock.wall(), ZoneInfo(self.cfg.station.timezone)).hour
        part = self._day_part(hour)
        for h in hosts:
            if not h.day_parts or part in h.day_parts:
                return h
        return hosts[0]

    def _weather_phrase(self) -> str:
        phrases = self.cfg.talk.weather_phrases
        return phrases[self.rng.randint(0, len(phrases) - 1)] if phrases else ""

    def _recent_dj_meta(self, n: int) -> list[dict]:
        """meta of the newest n fresh dj_talk clips (for topic/motif rotation)."""
        out: list[dict] = []
        for it in reversed(self.store.list_items("dj_talk")):
            if not it["fresh"] or it["emergency"]:
                continue
            try:
                out.append(json.loads(it["meta_json"]) if it.get("meta_json") else {})
            except ValueError:
                out.append({})
            if len(out) >= n:
                break
        return out

    def _pick_topic(self) -> str:
        topics = self.cfg.talk.topic_bank
        if not topics:
            return ""
        recent = {m.get("topic") for m in self._recent_dj_meta(8)}
        candidates = [t for t in topics if t not in recent] or topics
        return candidates[self.rng.randint(0, len(candidates) - 1)]

    def _request_context(self) -> list[str]:
        """Request back-announce / tease (revision 8). Copy only: never a name
        or email (AGENTS rule 11 / PAYMENT §0.3)."""
        out: list[str] = []
        ready = self.store.ready_request_songs()
        if ready:
            out.append(f'A listener request is coming up: "{ready[0]["text"]}".')
        since = datetime.fromtimestamp(self.clock.wall() - 1800, UTC).isoformat()
        recent = self.store.recently_aired_request(since)
        if recent:
            out.append(f'You just played a listener request: "{recent["text"]}".')
        return out

    def _motif_reason(self, text: str) -> str | None:
        """A banked motif that aired inside the gap, or None (revision 10)."""
        recent: set[str] = set()
        for m in self._recent_dj_meta(self.cfg.talk.motif_gap_items):
            recent.update(w.lower() for w in (m.get("motifs") or []))
        low = text.lower()
        for motif in self.cfg.talk.motif_bank:
            if motif.lower() in low and motif.lower() in recent:
                return motif
        return None

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
                self._store_voice(item, "field_report", evergreen=False)
                log.info("produced field_report %.1fs", item["duration_s"])
            except Exception as e:
                log.warning("field report production failed: %s", err_text(e))
                return

    # --------------------------------------------------------------- promos
    def _count_promos(self, campaign_id: str | None = None,
                      kind: str | None = None,
                      now_wall: float | None = None) -> int:
        """Ready promos (recyclable), optionally filtered by campaign or kind.
        Day-level promos expire at local midnight, so they are not stock after
        that even though the row survives (revision 6)."""
        n = 0
        for it in self.store.list_items("promo"):
            if it["emergency"]:
                continue
            if now_wall is not None:
                exp = it.get("expires_at")
                if exp:
                    try:
                        if datetime.fromisoformat(exp).timestamp() <= now_wall:
                            continue
                    except ValueError:
                        pass
            meta = json.loads(it["meta_json"]) if it.get("meta_json") else {}
            if campaign_id is not None and meta.get("campaign_id") != campaign_id:
                continue
            if kind is not None and meta.get("kind") != kind:
                continue
            n += 1
        return n

    def _retire_finished_promos(self, now_wall: float, tz: str) -> None:
        """A campaign's promos are retired (not deleted) once its ramp ends, so
        nothing recycles into next year (ANNOUNCER_OVERHAUL C5)."""
        for it in self.store.list_items("promo"):
            meta = json.loads(it["meta_json"]) if it.get("meta_json") else {}
            cid = meta.get("campaign_id")
            if not cid:
                continue
            camp = next((c for c in self.cfg.promotions.campaigns if c.id == cid), None)
            expired = False
            exp = it.get("expires_at")
            if exp:
                with contextlib.suppress(ValueError):
                    expired = datetime.fromisoformat(exp).timestamp() <= now_wall
            if expired or camp is None or target_per_hour(now_wall, tz, camp) <= 0:
                self.store.retire_item(it["id"], "campaign promo expired/ended")

    async def ensure_promos(self) -> None:
        """Keep ready promos per source. Campaign promos only while the ramp
        runs; the steady kinds (request line, paid tier, Tree Fiddy Report) are
        folded into the normal promo mix so the station advertises itself all
        day instead of in bursts (operator: "advertise steadily")."""
        cfg = self.cfg
        tz = cfg.station.timezone
        now = self.clock.wall()
        camp = active_campaign(now, tz, cfg.promotions.campaigns)
        self._retire_finished_promos(now, tz)  # expired day-level promos too
        jobs: list[tuple[str, int, Any, Any]] = []
        if camp is not None:
            jobs.append((
                f"campaign:{camp.id}", cfg.inventory.promos_min,
                lambda c=camp: self._make_campaign_promo(c),
                lambda c=camp: self._count_promos(campaign_id=c.id, now_wall=now)))
        jobs += [
            ("request_line", 2, self._make_request_line,
             lambda: self._count_promos(kind="request_line", now_wall=now)),
            ("paid_tier", 2, self._make_paid_tier,
             lambda: self._count_promos(kind="paid_tier", now_wall=now)),
            ("tree_fiddy", 1, self._make_tree_fiddy,
             lambda: self._count_promos(kind="tree_fiddy", now_wall=now)),
        ]
        for name, target, maker, counter in jobs:
            have = counter()
            need = target - have
            log.debug("producer.need", extra={
                "item_type": f"promo/{name}", "have": have, "target": target})
            for _ in range(max(0, need)):
                try:
                    if await maker() is None:
                        break
                except Exception as e:
                    log.warning("promo production failed (%s): %s", name, err_text(e))
                    break

    async def _make_campaign_promo(self, camp) -> dict | None:
        """LLM-written campaign promo; the code fills the day tokens and
        rejects wrong claims (revision 5/6)."""
        cfg = self.cfg
        tz = cfg.station.timezone
        now = self.clock.wall()
        days = days_until(now, tz, camp)
        phrase = phrase_for(days)
        ctx = (f"Event: {camp.label}. Date: {camp.date}. CTA: {camp.cta}.\n"
               f"Timing phrase: {phrase}. Write it exactly, or use the "
               f"[[days_until]] token.\n"
               f"Angles you may use: {'; '.join(camp.lines)}")
        host = self._current_host()
        copy = await self.voice.write_copy(
            "promo", 12.0, context=ctx, host=host.name if host else "Liam")
        reason = validate_promo_template(copy["text"], camp, cfg.station.name,
                                         days)
        if reason:
            # one retry with the rejection fed back, then give up rather
            # than air a wrong claim
            copy = await self.voice.write_copy(
                "promo", 12.0,
                context=ctx + f"\nYour previous attempt was rejected: "
                              f"{reason}. Fix exactly that and keep "
                              f"[[event_name]].",
                host=host.name if host else "Liam")
            reason = validate_promo_template(copy["text"], camp,
                                             cfg.station.name, days)
            if reason:
                log.warning("promo.rejected", extra={"reason": reason})
                return None
        copy["text"] = fill_day_tokens(copy["text"], now, tz, camp)
        reason = validate_promo_copy(copy["text"], camp, cfg.station.name)
        if reason:
            log.warning("promo.rejected", extra={"reason": reason})
            return None
        copy["evergreen"] = True
        rendered = await self.voice.render(
            copy, "promo", 12.0, voice=host.voice if host else None)
        item = self.voice.item_dict("promo", copy, rendered)
        item["meta"]["kind"] = "campaign"
        item["meta"]["campaign_id"] = camp.id
        expires = datetime.fromtimestamp(
            next_local_midnight(now, tz), UTC).isoformat()
        self._store_voice(item, "promo", evergreen=True, expires_at=expires)
        log.info("produced campaign promo %.1fs", item["duration_s"])
        return item

    async def _make_request_line(self) -> dict | None:
        lines = self.cfg.promotions.request_line_lines
        if not lines:
            return None
        text = lines[self.rng.randint(0, len(lines) - 1)]
        host = self._current_host()
        rendered = await self.voice.render(
            {"text": text}, "promo", 8.0, voice=host.voice if host else None)
        item = self.voice.item_dict(
            "promo", {"text": text, "evergreen": True}, rendered)
        item["meta"]["kind"] = "request_line"
        self._store_voice(item, "promo", evergreen=True)
        log.info("produced request_line promo %.1fs", item["duration_s"])
        return item

    async def _make_paid_tier(self) -> dict | None:
        """Steady paid-tier promo: code-written, copy only, no payment code
        (AGENTS rule 11)."""
        lines = self.cfg.promotions.paid_tier_lines
        if not lines:
            return None
        text = lines[self.rng.randint(0, len(lines) - 1)]
        host = self._current_host()
        rendered = await self.voice.render(
            {"text": text}, "promo", 14.0, voice=host.voice if host else None)
        item = self.voice.item_dict(
            "promo", {"text": text, "evergreen": True}, rendered)
        item["meta"]["kind"] = "paid_tier"
        self._store_voice(item, "promo", evergreen=True)
        log.info("produced paid_tier promo %.1fs", item["duration_s"])
        return item

    async def _make_tree_fiddy(self) -> dict | None:
        """The administrator's weekly count, in the operator character's voice
        (am_justin). Real numbers from the requests table, regenerated hourly
        so the count is current; never names or emails."""
        cfg = self.cfg
        now = self.clock.wall()
        since = datetime.fromtimestamp(now - 7 * 86400, UTC).isoformat()
        n = self.store.count_paid_aired_since(since)
        if n:
            text = cfg.promotions.tree_fiddy_lines[0].format(
                n=n, s="" if n == 1 else "s")
        else:
            text = cfg.promotions.tree_fiddy_none
        voice = cfg.dj.character.voice or cfg.voices.dj
        rendered = await self.voice.render(
            {"text": text}, "promo", 20.0, voice=voice)
        item = self.voice.item_dict(
            "promo", {"text": text, "evergreen": False}, rendered)
        item["meta"]["kind"] = "tree_fiddy"
        item["meta"]["paid_aired_7d"] = n
        expires = datetime.fromtimestamp(
            now + cfg.promotions.tree_fiddy_gap_s, UTC).isoformat()
        self._store_voice(item, "promo", evergreen=False, expires_at=expires)
        log.info("produced tree_fiddy report (%d paid aired in 7d)", n)
        return item

    # ----------------------------------------------------------- time checks
    def _time_check_text(self, bucket: float, tz: str) -> str:
        variants = self.cfg.talk.time_check_variants or ["It is {clock}."]
        clock = spoken_time(datetime.fromtimestamp(bucket, ZoneInfo(tz)))
        idx = int(bucket // 300) % len(variants)  # rotate deterministically
        return variants[idx].format(clock=clock)

    async def ensure_time_checks(self) -> None:
        """Render a code-written clock read for each 5-minute bucket in the
        next hour. The scheduler airs one only near a quarter mark (operator
        pick: top, quarter past, bottom, quarter til) and picks the variant
        whose bucket matches the ACTUAL slot, so the clock is never more than
        the 5-minute rounding off. Kokoro is ~0.2 s: a dozen variants an hour
        is nearly free, and each expires at the end of its bucket."""
        cfg = self.cfg
        tz = cfg.station.timezone
        now = self.clock.wall()
        buckets = [int((now + k * 300) // 300 * 300) for k in range(12)]
        existing = set()
        for it in self.store.list_items("time_check"):
            if it["retired"]:
                continue
            exp = it.get("expires_at")
            if exp:
                try:
                    if datetime.fromisoformat(exp).timestamp() < now - 3600:
                        # stale renders would otherwise accumulate forever
                        self.store.retire_item(it["id"], "clock bucket passed")
                        continue
                except ValueError:
                    pass
            meta = json.loads(it["meta_json"]) if it.get("meta_json") else {}
            existing.add(meta.get("bucket"))
        for bucket in buckets:
            if bucket in existing:
                continue
            copy = {"text": self._time_check_text(float(bucket), tz),
                    "est_duration_s": 4.0, "evergreen": False}
            try:
                host = self._current_host()
                rendered = await self.voice.render(
                    copy, "time_check", 4.0, voice=host.voice if host else None)
            except Exception as e:
                log.warning("time_check production failed: %s", err_text(e))
                return
            item = self.voice.item_dict("time_check", copy, rendered)
            item["meta"]["bucket"] = bucket
            expires = datetime.fromtimestamp(bucket + 300, UTC).isoformat()
            self._store_voice(item, "time_check", evergreen=False,
                              expires_at=expires)
            log.info("produced time_check %.1fs", item["duration_s"])

    # ------------------------------------------------------------------ sfx
    async def ensure_sfx_pool(self, limit: int | None = None) -> int:
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
            if limit is not None and made >= limit:
                break
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
                # stock cue not rendered yet: make it now (once, for everyone)
                if self._stock_sfx(name) is None:
                    try:
                        self._store_sfx(await sfx.render(name), evergreen=True)
                    except Exception as e:
                        log.warning("sfx.failed", extra={"cue": name, "error": err_text(e)})
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
            if not got:
                continue
            # a beat after the last sentence lands at the clip's end, where
            # the hit could never fit: pull it in so it ends with the clip
            off = min(beat_offset(text, after, dur, lead),
                      round(dur - got["duration_s"] - 0.05, 3))
            if off < lead:
                continue  # clip too short for this hit
            overlays.append({"item_id": got["id"], "cue": name, "kind": kind,
                             "offset_s": off, "duration_s": got["duration_s"]})
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
            # injected clock, not datetime.now (AGENTS rule 3)
            expires = (datetime.fromtimestamp(self.clock.wall(), UTC)
                       + timedelta(seconds=self.cfg.news.refresh_s + 900))
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
        try:
            return datetime.fromisoformat(exp).timestamp() > self.clock.wall()
        except Exception:
            return True

    def _store_voice(self, item: dict, type_: str, evergreen: bool = True,
                     expires_at: str | None = None, gravity: str | None = None) -> None:
        """Store a rendered voice clip right away, airable dry. Its script cues
        stay in meta as `sfx_pending` for the SFX worker (sfx_step): talk never
        waits on the SFX backend."""
        meta = item.setdefault("meta", {})
        wants = bool(meta.get("sfx_cues") or meta.get("joke_after_sentence") is not None
                     or type_ == "field_report")
        if wants and self.sfx is not None and self.cfg.sfx.enabled \
                and type_ in SFX_HOST_TYPES:
            meta["sfx_pending"] = True
        else:
            meta.pop("sfx_cues", None)
            meta.pop("joke_after_sentence", None)
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
                # words between songs first (§5.2): station IDs, liners, DJ
                # talk; SFX render in their own worker (sfx_loop)
                await self.ensure_station_ids()
                await self.ensure_liners()
                await self.ensure_dj()
                await self.ensure_time_checks()
                await self.ensure_promos()
                await self.ensure_commercials()
                await self.ensure_field_reports()
            except Exception as e:
                log.warning("producer voice cycle error: %s", e)
            await asyncio.sleep(6.0)

    async def sfx_loop(self) -> None:
        """SFX worker (SFX.md §7.3): attaches overlays to stored talk, then
        fills the evergreen stock pool. One render at a time; the backend is
        slow (docs/backends.md §7) and nothing waits on it."""
        while True:
            try:
                did = await self.sfx_step()
            except Exception as e:
                log.warning("sfx loop error: %s", err_text(e))
                did = False
            await asyncio.sleep(1.0 if did else 5.0)

    async def sfx_step(self) -> bool:
        """One unit of SFX work. Returns True if it did any.
        1) the oldest unaired talk clip with pending cues gets its overlays
           (aired evergreen clips after that: they air again);
        2) otherwise one missing evergreen stock cue is rendered."""
        if self.sfx is None or not self.cfg.sfx.enabled:
            return False
        pending: list[dict] = []
        for t in sorted(SFX_HOST_TYPES):
            for it in self.store.list_items(t):
                meta = json.loads(it["meta_json"]) if it.get("meta_json") else {}
                if meta.get("sfx_pending"):
                    pending.append({**it, "meta": meta})
        if pending:
            it = min(pending, key=lambda i: (not i["fresh"], i["id"]))
            item = {"duration_s": it["duration_s"], "meta": it["meta"]}
            await self._attach_sfx(item, it["type"])
            item["meta"].pop("sfx_pending", None)
            self.store.update_item_meta(it["id"], item["meta"])
            return True
        return await self.ensure_sfx_pool(limit=1) > 0

    async def news_loop(self) -> None:
        while True:
            try:
                await self.ensure_news()
            except Exception as e:
                log.warning("news loop error: %s", e)
            await asyncio.sleep(5.0)

    async def song_loop(self) -> None:
        """Song decider (RADIO §6.1/§6.2). Keeps up to `songs.capacity()` song
        jobs in flight, each one brief -> render -> intro -> store:
        - the next listener request first (queue order: paid, then FIFO). Up to
          songs.free_request_slots free requests render at once, paid ones in any
          free slot;
          a finished request waits (`_wait_turn`) until those ahead of it are
          ready, because ready order is air order;
        - free requests and stock never take the last slot, which stays open for
          a paid request (with capacity 1 they use it when it is idle);
        - a backend outage (transport error) pauses new jobs 15 s doubling to
          2 min; any other failure pauses 15 s (a dead LLM must not spin briefs).
        Jobs are never cancelled for a request: a generator doesn't abort on
        disconnect, so cancelling only wastes the render."""
        log.info("song worker started")
        outage_s = 15.0
        try:
            while True:
                pause = 0.0
                for task in [t for t in self._jobs if t.done()]:
                    outcome = self._reap(task, self._jobs.pop(task))
                    if outcome == "outage":
                        pause = max(pause, outage_s)
                        outage_s = min(outage_s * 2, 120.0)
                    elif outcome == "failed":
                        pause = max(pause, 15.0)
                    else:
                        outage_s = 15.0
                if pause:
                    await self._sleep(pause)
                    continue
                while (job := self._next_job()) is not None:
                    self._spawn(job)
                tick = asyncio.ensure_future(self._sleep(20.0))
                try:
                    await asyncio.wait({tick, *self._jobs},
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    tick.cancel()
        finally:
            for task in self._jobs:
                task.cancel()

    def _spawn(self, job: SongJob) -> None:
        if job.req is not None:
            # claim it now: the next _next_job() runs before the task does
            self.store.mark_request_producing(job.req["id"])
            self.request_stage[job.req["id"]] = "waiting"
        task = asyncio.create_task(self._run_job(job))
        self._jobs[task] = job
        log.info("song.job.spawned", extra={
            "kind": job.kind, "paid": job.paid,
            "request_id": job.req["id"] if job.req else None,
            "jobs": len(self._jobs), "capacity": self._capacity()})

    def _reap(self, task: asyncio.Task, job: SongJob) -> str:
        """Observe a finished job (no task dies unobserved): ok | outage | failed."""
        rid = job.req["id"] if job.req else None
        if task.cancelled():
            return "failed"
        exc = task.exception()
        if exc is None:
            outcome = "ok"
        elif isinstance(exc, (httpx.TransportError, LLMTruncated)):
            outcome = "outage"
            log.warning("song backend unavailable: %s", err_text(exc))
        else:
            outcome = "failed"
            log.warning("song production failed: %s", err_text(exc))
        log.info("song.job.reaped", extra={
            "kind": job.kind, "request_id": rid, "outcome": outcome,
            "jobs": len(self._jobs)})
        return outcome

    def _capacity(self) -> int:
        """Concurrent song jobs allowed now: the generator pool's healthy
        slots (1 for a pipeline without a pool)."""
        cap = getattr(self.songs, "capacity", None)
        return max(0, int(cap())) if callable(cap) else 1

    def _next_job(self) -> SongJob | None:
        """Pick the next song job given what is in flight (sync, no awaits).
        Same gates as ever for the serial path (`song_step`, nothing in flight):
        a queued request first, even at stock target or library cap; else stock
        if fresh (+ in-flight stock) is below target, the library is under
        songs.stock_max_library and songs.stock_gap_s has passed (§6.1)."""
        jobs = list(self._jobs.values())
        cap = self._capacity()
        if len(jobs) >= cap:
            return None
        # free requests and stock together never fill every slot: one stays
        # open for a paid request. Paid jobs don't count against that, so with
        # two paid rendering a free one may take the third. (Capacity 1: they
        # use the slot when it is idle.)
        unpaid = sum(1 for j in jobs if not j.paid)
        room = unpaid < cap - 1 if cap > 1 else not jobs
        # first eligible queued request in queue order: paid may take any slot
        # (several at once); free only a non-last slot, up to
        # songs.free_request_slots. Finished songs still air in queue order
        # (_wait_turn), so starting a later request early is safe.
        free_busy = sum(1 for j in jobs if j.kind == "request" and not j.paid)
        free_ok = room and free_busy < self.cfg.songs.free_request_slots
        claimed = {j.req["id"] for j in jobs if j.req is not None}
        for req in self.store.queued_requests():
            if req["id"] in claimed:
                continue
            if req.get("priority") or free_ok:
                return SongJob("request", req)
        if room and self._stock_due(sum(1 for j in jobs if j.kind == "stock")):
            return SongJob("stock")
        return None

    def _stock_due(self, in_flight: int) -> bool:
        if self.stock_capped():
            if not self._stock_cap_logged:
                log.info("song.stock_capped", extra={
                    "library_songs": self.store.count_usable_of_type("song"),
                    "stock_max_library": self.cfg.songs.stock_max_library})
                self._stock_cap_logged = True
            return False
        self._stock_cap_logged = False
        # in-flight stock counts toward the target, or cold start overshoots
        fresh = self.store.count_fresh_of_type("song") + in_flight
        if fresh >= self.cfg.inventory.fresh_songs_ready:
            return False
        return self._last_stock_song_at is None or (
            self.clock.now() - self._last_stock_song_at >= self.cfg.songs.stock_gap_s)

    async def _run_job(self, job: SongJob) -> bool:
        try:
            if job.req is not None:
                return await self._produce_request_song(job.req, job)
            return await self._produce_stock_song(job)
        finally:
            if job.req is not None:
                self.request_stage.pop(job.req["id"], None)
            job.finished.set()

    async def _wait_turn(self, job: SongJob | None) -> None:
        """Hold a finished request song until every request AHEAD of it in
        queue order that is still in flight has ended. Ready order is air
        order (the splice, §5.2), so parallel requests must become ready in
        queue order. The song waits outside the library: stored now, the
        scheduler could air it as stock. If the one ahead fails or is
        re-queued, this one goes."""
        if job is None:
            return
        while True:
            ahead = [j for j in self._jobs.values()
                     if j is not job and j.req is not None and not j.finished.is_set()
                     and j.queue_key() < job.queue_key()]
            if not ahead:
                return
            log.info("request.held", extra={
                "request_id": job.req["id"] if job.req else None,
                "behind": [j.req["id"] for j in ahead if j.req]})
            waits = {asyncio.ensure_future(j.finished.wait()) for j in ahead}
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waits:
                    w.cancel()

    async def song_step(self) -> bool:
        """One song job, run to completion inline (seed, tests, the sim).
        Returns True if it did work. Gates: `_next_job`."""
        job = self._next_job()
        if job is None:
            return False
        return await self._run_job(job)

    def _genre_context(self) -> list[str]:
        """`_recent_genres()` plus the genres of in-flight jobs whose brief is
        written: a parallel render isn't in the store yet, but it will air
        next to this one (§5.2 genre_no_repeat)."""
        n = self.cfg.playout.genre_no_repeat
        out = self._recent_genres()
        for j in self._jobs.values():
            if j.genre:
                if j.genre in out:
                    out.remove(j.genre)
                out.append(j.genre)
        return out[-n:]

    def _start_early_art(self, brief: dict[str, Any], token: str):
        """Start the cover render now that the brief (title/artist/lyrics/
        style) exists, in parallel with the song generator. Optional."""
        aw = self.art_worker
        if aw is None or not self.cfg.art.enabled:
            return None
        if not (brief.get("title") and brief.get("artist")):
            return None
        # share the song seed so the art meta records a reproducible seed
        brief.setdefault("seed", secrets.randbits(32))
        return asyncio.create_task(aw.render_brief(brief, token))

    def _finish_early_art(self, song_id: int, task) -> None:
        """Attach an early cover to the item, or let the background worker
        pick it up if the render failed/was slower than the song."""
        aw = self.art_worker
        if task is None or aw is None:
            return
        aw.pending_early.add(song_id)
        if task.done():
            self._attach_early_art(song_id, task, aw)
        else:
            task.add_done_callback(
                lambda t, sid=song_id, worker=aw:
                self._attach_early_art(sid, t, worker))

    def _attach_early_art(self, song_id: int, task, aw) -> None:
        if task.cancelled():
            aw.clear_pending(song_id)
            return
        try:
            result = task.result()
        except Exception:  # noqa: BLE001 - early art is cosmetic
            result = None
        if result is None:
            aw.clear_pending(song_id)
            return
        jpg_path, meta = result
        try:
            aw.attach(song_id, jpg_path, meta)
        except Exception as e:  # noqa: BLE001 - DB hiccup: worker retries later
            log.warning("art.attach_failed", extra={
                "item_id": song_id, "stage": "attach", "error": err_text(e)})
            aw.clear_pending(song_id)
            return
        log.info("art.attached", extra={
            "item_id": song_id, "stage": "attach", "early": True})

    async def _produce_stock_song(self, job: SongJob) -> bool:
        art_task = None
        attached = False
        try:
            async with self._brief_gate.hold(STOCK_BRIEF_KEY):
                brief = await self.songs.brief(self._genre_context())
                job.genre = brief.get("genre")
            art_task = self._start_early_art(
                brief, f"stock{secrets.token_hex(6)}")
            item = await self.songs.produce_song(brief, prefer="slow")
            # render the intro BEFORE the song enters the library: the scheduler
            # shares this event loop and would air the song bare in the gap
            intro = await self._render_intro(item, None)  # stock intros are non-fatal
            song_id = self.store.add_item(
                type_="song", media_path=item["media_path"], duration_s=item["duration_s"],
                sample_rate=item.get("sample_rate"), channels=item.get("channels"),
                title=item.get("title"), artist=item.get("artist"), genre=item.get("genre"),
                evergreen=True, fresh=True, meta=item.get("meta"))
            self._store_intro(intro, song_id, None)
            self._finish_early_art(song_id, art_task)
            attached = True
            self._last_stock_song_at = self.clock.now()
            log.info("song.produced", extra={
                "title": item.get("title"), "genre": item.get("genre"),
                "duration_s": item.get("duration_s")})
            return True
        finally:
            if art_task is not None and not attached:
                art_task.cancel()

    def stock_capped(self) -> bool:
        """True when the library already holds songs.stock_max_library usable
        (non-retired) songs: stock generation pauses so the expensive song
        model only runs for listener requests (§6.1). 0 disables the cap."""
        cap = self.cfg.songs.stock_max_library
        return cap > 0 and self.store.count_usable_of_type("song") >= cap

    async def _produce_request_song(self, req: dict, job: SongJob | None = None) -> bool:
        """Turn one queued listener request into a song (+ short intro). On a
        hard failure, record the attempt (3 max -> failed) and re-raise so the
        loop backs off; a failing intro never blocks the request (4.5/4.6)."""
        log.info("request.producing", extra={"request_id": req["id"]})
        self.store.mark_request_producing(req["id"])
        self.request_stage[req["id"]] = "waiting"  # for its turn at the brief LLM
        paid = bool(req.get("priority"))
        art_task = None
        attached = False
        try:
            backlog = len(self.store.queued_requests())
            # Paid requests are never shortened by the backlog rule (§0.5).
            short = backlog >= 3 and not paid
            # turns go in queue order (paid first, then oldest), whoever asked first
            async with self._brief_gate.hold(SongJob("request", req).queue_key()):
                self.request_stage[req["id"]] = "writing"
                # Prep Time on the board starts here: lyrics/brief writing, not
                # submission (RADIO §11) — queue idle is not prep work.
                self.store.mark_request_writing(req["id"])
                brief = await self.songs.brief(self._genre_context(),
                                               request_text=req["text"],
                                               short=short, paid=paid)
                if job is not None:  # inside the lock: the next brief sees it
                    job.genre = brief.get("genre")
            brief["request_id"] = req["id"]
            # start the cover now, in parallel with the song render: the brief
            # already has everything FLUX needs (title/artist/genre/lyrics/style)
            art_task = self._start_early_art(brief, f"req{req['id']}")
            # requests take the fastest idle generator (RADIO §6.2)
            self.request_stage[req["id"]] = "generating"
            item = await self.songs.produce_song(brief, prefer="fast")
            # the generator is done; what is left (intro, waiting behind the
            # requests ahead of it) must not read "being generated"
            self.request_stage[req["id"]] = "rendered"
            # Everything awaited is done before the song is stored: song, intro
            # and 'ready' land with no await between them, so the scheduler can
            # never air a request song bare, as stock, before its intro exists.
            intro = await self._render_intro(item, req)
            await self._wait_turn(job)
            song_id = self.store.add_item(
                type_="song", media_path=item["media_path"], duration_s=item["duration_s"],
                sample_rate=item.get("sample_rate"), channels=item.get("channels"),
                title=item.get("title"), artist=item.get("artist"), genre=item.get("genre"),
                evergreen=True, fresh=True,
                meta={**dict(item.get("meta") or {}), "request_id": req["id"]})
            intro_id = self._store_intro(intro, song_id, req)
            self._finish_early_art(song_id, art_task)
            attached = True
            self.store.mark_request_ready(req["id"], song_id, intro_id)
            log.info("request.ready", extra={
                "request_id": req["id"], "song_item_id": song_id,
                "title": item.get("title"), "intro_item_id": intro_id})
            return True
        except LLMTruncated as e:
            # the brief model was cut off (overload/limit), not the request's
            # fault: re-queue for free a few times, then count attempts again
            cuts = self._llm_cuts.get(req["id"], 0) + 1
            self._llm_cuts[req["id"]] = cuts
            if cuts <= LLM_FREE_CUTS:
                self.store.requeue_request(req["id"])
                log.warning("request.llm_cut", extra={
                    "request_id": req["id"], "cuts": cuts, "error": err_text(e)})
                raise
            self._fail_request_attempt(req, paid, e)
            raise
        except httpx.TransportError as e:
            # backend down/dropped the connection: not the request's fault
            self.store.requeue_request(req["id"])
            log.warning("request.backend_unavailable", extra={
                "request_id": req["id"], "error": err_text(e)})
            raise
        except Exception as e:
            self._fail_request_attempt(req, paid, e)
            raise
        finally:
            if art_task is not None and not attached:
                art_task.cancel()

    def _fail_request_attempt(self, req: dict, paid: bool, e: BaseException) -> None:
        max_attempts = self.cfg.payments.max_attempts if paid else 3
        n = self.store.request_failed_attempt(req["id"], max_attempts=max_attempts)
        log.warning("request.failed", extra={
            "request_id": req["id"], "attempt": n, "error": err_text(e)})
        if paid and n >= max_attempts:
            log.warning("payment.refund_due", extra={
                "request_id": req["id"],
                "payment_intent_id": req.get("payment_intent_id")})

    def _named_intro_text(self, title: str, artist: str) -> str:
        variants = self.cfg.talk.intro_variants or ["Here's {title} from {artist}."]
        return variants[self.rng.randint(0, len(variants) - 1)].format(
            title=title, artist=artist or "the house band")

    async def _render_intro(self, item: dict, req: dict | None) -> dict | None:
        """Render a short DJ intro naming the just-created song. Stock intros
        are code-written and recycle (revision 11: no LLM, every airing of the
        song gets its name said). A request intro credits the listener and MUST
        exist so they hear their request was serviced: two LLM tries, then a
        scripted shout-out rendered directly."""
        title = item.get("title") or "this next one"
        artist = item.get("artist") or ""
        if req is None:
            host = self._current_host()
            text = self._named_intro_text(title, artist)
            try:
                r = await self.voice.render({"text": text}, "intro", 6.0,
                                            voice=host.voice if host else None)
            except Exception as e:
                log.warning("intro.failed", extra={"error": err_text(e)})
                return None
            return {"media_path": str(r["path"]), "duration_s": r["duration_s"],
                    "sample_rate": r.get("sample_rate"),
                    "channels": r.get("channels"), "meta": {"text": text}}
        genre = genres.display(item.get("genre") or "")
        context = f'Next song: "{title}" by {artist} ({genre}).\n'
        if req is not None:
            paid = bool(req.get("priority"))
            context += f"Listener request: {json.dumps(req['text'])}\n"
            if paid:
                context += ("This song was made for a PAID priority request. OPEN "
                            "with an excited shout-out that it's a priority request "
                            "(e.g. 'Priority request line! You paid, we played!'), "
                            "then name the song. Never mention the amount or any "
                            "name from the request text. ")
            else:
                context += ("This song was made for a listener's request. OPEN with an "
                            "excited shout-out that it's a listener request (e.g. "
                            "'Request line! You asked, we made it!'), then name the "
                            "song. ")
        context += "Do not mention the clock time."
        for _ in range(2 if req is not None else 1):
            try:
                host = self._current_host()
                return await self.voice.produce_item(
                    "intro", 10.0, context=context,
                    voice=host.voice if host else None,
                    host=host.name if host else "Liam")
            except Exception as e:
                log.warning("intro.failed", extra={
                    "error": err_text(e),
                    "request_id": req["id"] if req is not None else None})
        if req is None:
            return None
        if req.get("priority"):
            text = f"Priority request line! You paid, we played! Here's {title}"
        else:
            text = f"Request line! You asked, we made it! Here's {title}"
        text += f", by {artist}!" if artist else "!"
        try:
            r = await self.voice.render({"text": text}, "intro", 6.0)
        except Exception as e:
            log.warning("intro.fallback_failed", extra={
                "error": err_text(e), "request_id": req["id"]})
            return None
        log.info("intro.fallback", extra={"request_id": req["id"]})
        return {"type": "intro", "media_path": str(r["path"]),
                "duration_s": r["duration_s"], "sample_rate": r["sample_rate"],
                "channels": r["channels"], "role": "intro", "meta": {"text": text}}

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
            channels=intro.get("channels"), role="intro", evergreen=req is None,
            fresh=True, meta=meta)

    def _recent_genres(self) -> list[str]:
        """Style families of the last N songs in COMMITTED (aired) order, padded
        with the most recently created songs when the program is short. The
        scheduler's §5.2 rule is about what listeners just heard, not what the
        producer just generated."""
        n = self.cfg.playout.genre_no_repeat
        heard = [g for _, _, g in self._recent_committed_songs(n) if g]
        created = [it["genre"] for it in self.store.list_items("song")
                   if it.get("genre")]
        out: list[str] = []
        for g in created[-n:] + heard:
            if g not in out:
                out.append(g)
        return out[-n:]

"""Station server (RADIO.md §11).

Two listeners on one Station (SPLIT_PLAN §1): a PUBLIC app with only the
edge-facing routes (program/state/heartbeat/media/requests/visitors/health)
and an ADMIN app with the full API including `/api/admin/*` and station
control. The scheduler + producer run in the background so the station is on
air regardless of whether a listener is tuned in.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import ipaddress
import json
import logging
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.responses import Response
from starlette.types import Scope

from pilgrim import dj_contracts as dj
from pilgrim import voting
from pilgrim.album_art import ArtWorker
from pilgrim.config import (
    PAID_OVERRIDE_ENV,
    RNG,
    ROOT,
    Clock,
    Config,
    ensure_dirs,
    load_api_key,
    load_config,
    load_secret,
    stripe_env,
)
from pilgrim.dj_security import require_dj
from pilgrim.logging_setup import setup_logging
from pilgrim.media_publish import MediaPublisher, S3ObjectStore, art_url, public_url
from pilgrim.payments import (
    PaymentService,
    StripeClient,
    StripeError,
    WebhookError,
    verify_webhook,
)
from pilgrim.pipelines.copyright import CopyrightScreen
from pilgrim.pipelines.llm import LLM
from pilgrim.pipelines.moderation import Moderation
from pilgrim.pipelines.news import NewsPipeline
from pilgrim.pipelines.operator_phrase import PhraseManager
from pilgrim.pipelines.request_filter import prefilter
from pilgrim.pipelines.sfx import SfxClient, SfxPipeline
from pilgrim.pipelines.songs import SongPipeline, song_backend_health
from pilgrim.pipelines.voice import KokoroClient, VoicePipeline
from pilgrim.producer import Producer
from pilgrim.scheduler import Scheduler
from pilgrim.selector import RandomSelector
from pilgrim.store import Store

log = logging.getLogger("radio.server")


class Station:
    def __init__(self, cfg: Config, api_key: str) -> None:
        self.cfg = cfg
        self.api_key = api_key
        self._auth = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        ensure_dirs(cfg)
        self.media_dir = ROOT / cfg.library.dir
        self.db = Store(ROOT / cfg.library.db)
        self.db.reset_producing_to_queued()  # crash mid-generation mustn't strand (4.4)
        self.rng = RNG(cfg.station.rng_seed)
        self.llm = LLM(cfg, api_key)
        self.kokoro = KokoroClient(cfg)
        self.prompts = _load_prompts(cfg)
        self.moderator = Moderation(cfg, self.llm, prompts=self.prompts)
        self.copyright = CopyrightScreen(cfg, api_key)
        self.voice = VoicePipeline(cfg, self.llm, self.kokoro, self.db, self.media_dir,
                                   prompts=self.prompts)
        self.songs = SongPipeline(cfg, self.llm, self.media_dir,
                                  prompts=self.prompts, rng=self.rng, api_key=api_key)
        swept = self.songs.sweep_temp()  # render temp files a crash left behind
        if swept:
            log.info("song.tmp_swept", extra={"count": swept})
        self.news = NewsPipeline(cfg, self.llm, api_key=api_key,
                                prompts=self.prompts)
        self.sfx_client = SfxClient(cfg)
        self.sfx = SfxPipeline(cfg, self.sfx_client, self.media_dir)
        self.clock = Clock()
        self._listeners: dict[str, float] = {}  # player_id -> last heartbeat (clock.now)
        # Playback telemetry per live player (RADIO §9.2/§11): platform, audio
        # context state, hidden, output sink, interruption recoveries. Pruned
        # with the listener map; transitions are logged (`listener.telemetry`)
        # so background/lock behaviour on phones is visible after the fact.
        self._player_tel: dict[str, dict] = {}
        # IDLE.md (RADIO §3): the playhead freezes and production pauses when
        # nobody has been listening for `idle.after_s`. Policy lives here; the
        # mechanics are Scheduler.freeze()/thaw().
        self._idle_since: float | None = None
        self._last_listener_at: float = self.clock.now()
        self.idle_wakes = 0
        self.selector = RandomSelector(self.rng, cfg)
        self.epoch = uuid.uuid4().hex[:12]  # changes on restart (TUI.md §5)
        self.scheduler = Scheduler(cfg, self.db, self.selector, self.clock, self.rng,
                                   epoch=self.epoch)
        self.phrases = PhraseManager(cfg, self.db, self.voice, self.media_dir,
                                     scheduler=self.scheduler)
        # Close the phrase lifecycle (RADIO §5.5; TUI.md §6): when a committed
        # phrase row falls behind the playhead the scheduler marks the job
        # 'aired'; the TTL in PhraseManager._expire handles the never-placed
        # rest. (Hook set post-construction: phrases needs the scheduler,
        # which cannot yet see phrases.)
        self.scheduler.on_phrase_aired = self.phrases.mark_item_aired
        self.producer = Producer(
            cfg, self.db, self.llm, self.kokoro, self.voice, self.songs,
            self.clock, prompts=_load_prompts(cfg), media_dir=self.media_dir,
            api_key=api_key, rng=self.rng, news_pipeline=self.news, sfx=self.sfx,
            idle=self.is_idle)
        # Paid priority requests (PAYMENT.md). Off unless config + keys agree.
        # The keys come from the environment or the repo-root .env fallback
        # (config.stripe_env), so a plain `python -m pilgrim.server` restart
        # does not silently close payments (Step 12 regression, Oct 31).
        stripe_keys = stripe_env()
        self.payments_active = cfg.payments.active(stripe_keys)
        self.payment_service: PaymentService | None = None
        self.stripe_client: StripeClient | None = None
        self.publishable_key = ""
        self.webhook_secret = ""
        if self.payments_active:
            secret_key = stripe_keys["STRIPE_SECRET_KEY"]
            self.publishable_key = stripe_keys["STRIPE_PUBLISHABLE_KEY"]
            self.webhook_secret = stripe_keys["STRIPE_WEBHOOK_SECRET"]
            self.stripe_client = StripeClient(cfg.payments, secret_key)
            self.payment_service = PaymentService(
                cfg.payments, self.db, self.stripe_client, secret_key,
                self.publishable_key, self.webhook_secret, now=self.clock.wall)
            log.info("payment.mode", extra={"mode": self.payment_service.mode})
        cutoff = (datetime.now(UTC)
                  - timedelta(seconds=cfg.payments.abandon_after_s)).isoformat()
        abandoned = self.db.abandon_stale_checkouts(cutoff)
        if abandoned:
            log.info("payment.abandoned_stale", extra={"count": abandoned})
        # Staged checks run as background tasks; a restart orphans them.
        orphaned = self.db.abandon_checking_requests()
        if orphaned:
            log.info("payment.abandoned_checking", extra={"count": orphaned})
        # Operator comp secret (PAYMENT.md Step 12b). Never logged or stored.
        self.override_token = load_secret(PAID_OVERRIDE_ENV)
        self._tasks: list = []
        self._http = httpx.AsyncClient(timeout=4.0)
        # Cover art (ALBUM_ART.md Step 5): its own task, never inside playout.
        # A song without art airs exactly as today (hard rule 1).
        self.art_worker: ArtWorker | None = None
        # S3 publishing (RADIO §8.4): its own task, never inside playout.
        self.publisher: MediaPublisher | None = None
        if cfg.media.publish:
            try:
                self.publisher = MediaPublisher(cfg, self.db, S3ObjectStore(cfg))
            except Exception as e:  # noqa: BLE001 - no creds/boto3: degrade to /api/media
                log.warning("media.publisher_disabled", extra={"error": str(e)[:300]})
        if cfg.art.enabled:
            try:
                self.art_worker = ArtWorker(cfg, self.db, self.llm, self.prompts)
            except Exception as e:  # noqa: BLE001 - bad config/creds: art is cosmetic
                log.warning("art.worker_disabled", extra={"error": str(e)[:300]})
        # The producer can start a cover from the song brief (early art), in
        # parallel with the song render. Optional: no worker -> worker path only.
        if self.art_worker is not None:
            self.producer.art_worker = self.art_worker

    def media_url(self, item_id: int, inv: dict | None = None) -> str:
        """Browser URL for an item's audio (RADIO §8.4, §11)."""
        if inv is None:
            inv = self.db.get_item(item_id)
        return public_url(self.cfg, inv, item_id)

    def _with_urls(self, overlays: list[dict]) -> list[dict]:
        return [{**ov, "url": self.media_url(int(ov["media_id"]))} for ov in overlays]

    # ------------------------------------------------------------------ health
    async def backend_status(self) -> dict:
        def ok(status: int) -> bool:
            return 200 <= status < 500
        st: dict = {"litellm": False, "kokoro": False, "music": False,
                    "searxng": False, "sfx": False}
        try:
            r = await self._http.get(
                self.cfg.hosts.litellm.rstrip("/") + "/models", headers=self._auth)
            st["litellm"] = ok(r.status_code)
        except Exception:
            st["litellm"] = False
        try:
            r = await self._http.get(self.cfg.hosts.kokoro.rstrip("/") + "/health")
            st["kokoro"] = ok(r.status_code)
        except Exception:
            st["kokoro"] = False
        st["music_backends"] = await song_backend_health(
            self.cfg, self._http, self.api_key)
        st["music"] = any(st["music_backends"].values())
        try:
            r = await self._http.get(self.cfg.hosts.searxng.rstrip("/") + "/health",
                                     headers=self._auth)
            st["searxng"] = ok(r.status_code)
        except Exception:
            st["searxng"] = False
        try:
            r = await self._http.get(self.cfg.hosts.sfx.rstrip("/") + "/health")
            st["sfx"] = ok(r.status_code)
        except Exception:
            st["sfx"] = False
        return st

    async def inventory_levels(self) -> dict:
        # same counts the producer refills against (songs/dj/news: unaired and
        # unexpired; commercials/liners: everything usable, they recycle)
        have = self.producer.counts()
        inv = self.cfg.inventory
        return {
            "song": {"have": have["song"], "target": inv.fresh_songs_ready},
            "commercial": {"have": have["commercial"], "target": inv.commercials_min},
            "liner": {"have": have["liner"],
                      "target": inv.liners_per_bucket * len(inv.liner_buckets_s)},
            "dj_talk": {"have": have["dj_talk"], "target": inv.dj_talk_min},
            "field_report": {"have": have["field_report"], "target": inv.field_reports_min},
            "news": {"have": have["news"], "target": 1},
            "sfx": {"have": have["sfx"], "target": self.producer.sfx_stock_target()},
            "promo": {"have": have["promo"], "target": inv.promos_min},
        }

    def rotation(self) -> dict[str, int]:
        """What can air right now (the listener-facing numbers): every
        non-retired song/commercial/liner recycles; DJ talk and news only while
        unaired and unexpired. Unlike `inventory`, songs count aired ones too."""
        have = self.producer.counts()
        return {
            "song": self.db.count_usable_of_type("song"),
            "commercial": have["commercial"],
            "liner": have["liner"],
            "dj_talk": have["dj_talk"],
            "field_report": have["field_report"],
            "news": have["news"],
            "promo": have["promo"],
        }

    def art_health(self) -> dict:
        """Admin-only art health: the worker's cached backend plus DB counts.
        Never probes FLUX (a render blocks /health for ~40 s)."""
        counts = self.db.art_counts()
        backend = self.art_worker.backend if self.art_worker else "disabled"
        return {"backend": backend, **counts}

    @property
    def on_air(self) -> bool:
        return self.scheduler.coverage() > 0

    async def health(self) -> dict:
        backends = await self.backend_status()
        inv = await self.inventory_levels()
        # Rendered audio remains playable when production backends fail (§5.4).
        return {
            "on_air": self.on_air,
            "backends": backends,
            "inventory": inv,
            "rotation": self.rotation(),
            "committed_coverage_s": round(self.scheduler.coverage(), 1),
            "committed_count": len(self.scheduler._items),
            "song_realtime": round(self.songs.generation_rate, 2),
            "art": self.art_health(),
            "time": time.time(),
            # IDLE.md D10: operator visibility. `on_air` stays true while frozen
            # (coverage > 0), so PLAY is never disabled by idle.
            "idle": {"idle": self.is_idle(),
                     "since": self._idle_since,
                     "wakes": self.idle_wakes},
            "listeners": self.listener_telemetry(),
        }

    # ------------------------------------------------------------------ program
    def program_response(self, after_seq: int | None,
                         client_epoch: str | None = None,
                         client_revision: int | None = None) -> dict:
        """Committed programme (RADIO §11, §5.3). A stale client cursor
        (epoch/revision mismatch) returns a full snapshot with `reset: true`."""
        scheduler = self.scheduler
        stale = ((client_epoch is not None and client_epoch != scheduler.epoch)
                 or (client_revision is not None
                     and client_revision != scheduler.revision))
        if after_seq is None or stale:
            seq, offset = scheduler.on_air()
            since = seq if seq is not None else scheduler.store.max_seq()
            items = scheduler.store.program_since(since
                                                  if since else 1)
            start_offset = offset
        else:
            items = scheduler.store.program_after(after_seq)
            start_offset = 0.0
        out = []
        for it in items:
            inv = self.db.get_item(it["item_id"])
            title = (inv.get("title") if inv else None) or ""
            artist = (inv.get("artist") if inv else None) or ""
            out.append({"seq": it["seq"], "media_id": it["item_id"],
                        "url": self.media_url(it["item_id"], inv),
                        # ALBUM_ART Step 7: "" until the JPEG is in S3; no
                        # origin fallback, art is cosmetic.
                        "art_url": art_url(self.cfg, inv),
                        "type": it["type"], "duration_s": it["duration_s"],
                        "sfx": self._with_urls(scheduler.overlays_for(it["seq"])),
                        "title": title, "artist": artist})
        return {"items": out, "start_offset_s": start_offset,
                "epoch": scheduler.epoch, "revision": scheduler.revision,
                "reset": stale, "server_time": time.time()}

    def public_state(self) -> dict:
        """Cheap station state for DJ-aware clients (RADIO §11, §5.3).
        Reads in-memory schedule state; never probes production backends."""
        st = self.scheduler
        seq = ctype = url = art = None
        offset = 0.0
        if st._items:
            s, off = st.on_air()
            if s is not None:
                row = next((r for r in st._items if r["seq"] == s), None)
                seq, offset = s, off
                ctype = row["type"] if row else None
                if row:
                    inv = self.db.get_item(row["item_id"])
                    url = self.media_url(row["item_id"], inv)
                    art = art_url(self.cfg, inv)
        return {"epoch": st.epoch, "revision": st.revision,
                "server_time": time.time(), "current_seq": seq,
                "current_type": ctype, "current_url": url,
                "current_art_url": art,
                "current_offset_s": round(offset, 3),
                "prepared_cutover": None, "listeners": self.active_listeners(),
                # IDLE.md D10: public clients can show the freeze (PLAY is never
                # disabled by it: on_air stays true while coverage > 0).
                "idle": self.is_idle(),
                # VOTING.md: the on-air song's rating window + lifetime aggregate
                # (`null` when ratings are disabled). Voter-independent, so the
                # shared max-age=1 cache stays correct.
                "rating": self._public_rating(seq)}

    # ------------------------------------------------------------- idle (IDLE.md)
    def is_idle(self) -> bool:
        """True while the playhead is frozen (no listeners for idle.after_s)."""
        return self.scheduler.frozen

    def wake(self, reason: str) -> None:
        """Thaw the frozen station. Synchronous by design (IDLE.md D2/rule 1):
        it only touches already-rendered inventory, so the request that woke it
        returns a playable programme in the same response. No-op when awake.

        A thaw failure leaves the station frozen (still playable) rather than
        half-thawed: the next program fetch or heartbeat retries."""
        if not self.scheduler.frozen:
            return
        try:
            frozen_s = (self.clock.now() - self._idle_since
                        if self._idle_since is not None else 0.0)
            dropped = self.scheduler.thaw()
        except Exception as e:  # noqa: BLE001 - a wake must never break radio
            log.exception("station.wake_failed: %s", e)
            return
        self._idle_since = None
        self.idle_wakes += 1
        self._last_listener_at = self.clock.now()
        log.info("station.wake", extra={"reason": reason,
                                        "idle_s": round(frozen_s),
                                        "dropped": dropped})

    def _idle_check(self) -> None:
        """One idle decision (sync so tests can call it directly)."""
        if not self.cfg.idle.enabled or self.scheduler.frozen:
            return
        if self.active_listeners() > 0:
            self._last_listener_at = self.clock.now()
            return
        if self.clock.now() - self._last_listener_at < self.cfg.idle.after_s:
            return
        self.scheduler.freeze()
        self._idle_since = self.clock.now()
        log.info("station.idle", extra={"after_s": self.cfg.idle.after_s,
                                        "coverage_s": round(self.scheduler.coverage(), 1)})

    async def idle_loop(self) -> None:
        """Freeze when nobody is listening (IDLE.md D1); wake is immediate, on
        the program/heartbeat/admin request, never on a poll. server.py may use
        asyncio.sleep; the scheduler may not (hard rule 3)."""
        while True:
            try:
                self._idle_check()
            except Exception as e:  # noqa: BLE001 - idle must never break radio
                log.warning("idle check failed: %s", e)
            await asyncio.sleep(self.cfg.idle.check_s)

    def _public_rating(self, seq: int | None) -> dict | None:
        """The on-air item's rating window + lifetime aggregate (VOTING.md V10).
        Zeroes when the on-air row is not a song; null when ratings are off."""
        if not self.cfg.ratings.enabled:
            return None
        st = self.scheduler
        open_, item_id = voting.rating_window(
            st, st.epoch, int(seq) if seq is not None else -1,
            self.cfg.ratings.grace_s)
        if open_ and item_id is not None:
            agg = self.db.song_rating(item_id)
            return {"open": True, "count": agg["count"], "avg": agg["avg"]}
        return {"open": False, "count": 0, "avg": None}

    # ------------------------------------------------------------- listeners
    def _prune_listeners(self) -> None:
        cutoff = self.clock.now() - self.cfg.visitors.listener_timeout_s
        for pid in [p for p, t in self._listeners.items() if t < cutoff]:
            del self._listeners[pid]
            self._player_tel.pop(pid, None)

    def record_player_telemetry(self, player_id: str | None,
                                payload: dj.HeartbeatIn) -> None:
        """Keep the latest playback telemetry per live player and log a
        `listener.telemetry` line whenever it changes (RADIO §9.2/§11). Only
        players already in the listener map are kept (bounded by
        `visitors.max_listeners`); a heartbeat without telemetry is ignored."""
        if not player_id or player_id not in self._listeners:
            return
        if (payload.platform is None and payload.ctx_state is None
                and payload.hidden is None and payload.sink is None
                and payload.resumed is None):
            return
        tel = {"platform": payload.platform or "other",
               "ctx_state": payload.ctx_state or "",
               "hidden": bool(payload.hidden),
               "sink": payload.sink or "",
               "resumed": int(payload.resumed or 0)}
        prev = self._player_tel.get(player_id)
        self._player_tel[player_id] = tel
        if prev != tel:
            log.info("listener.telemetry", extra={
                "player_id": player_id[:16], **tel,
                "underrun": bool(payload.underrun)})

    def listener_telemetry(self) -> dict:
        """Admin health summary of live players' playback telemetry."""
        self._prune_listeners()
        tels = list(self._player_tel.values())
        by_platform: dict[str, int] = {}
        for t in tels:
            by_platform[t["platform"]] = by_platform.get(t["platform"], 0) + 1
        return {
            "count": len(self._listeners),
            "reporting": len(tels),
            "by_platform": by_platform,
            "hidden": sum(1 for t in tels if t["hidden"]),
            "not_running": sum(1 for t in tels
                               if t["ctx_state"] and t["ctx_state"] != "running"),
            "sink_element": sum(1 for t in tels if t["sink"] == "element"),
            "resumed_total": sum(t["resumed"] for t in tels),
        }

    def touch_listener(self, player_id: str | None) -> None:
        """Mark a player as listening (RADIO §9.1, §14). In-memory only: a
        player counts while its heartbeats arrive within
        `visitors.listener_timeout_s`. New IDs past `visitors.max_listeners`
        are ignored so client-supplied IDs can't grow the map unbounded."""
        if not player_id or len(player_id) > 64:
            return
        self._prune_listeners()
        if (player_id in self._listeners
                or len(self._listeners) < self.cfg.visitors.max_listeners):
            self._listeners[player_id] = self.clock.now()
            self._last_listener_at = self.clock.now()
            # IDLE.md D2: a heartbeat is a listener; wake before the handler
            # reads anything so the response reflects the thawed programme.
            self.wake("heartbeat")

    def active_listeners(self) -> int:
        self._prune_listeners()
        return len(self._listeners)

    # ---------------------------------------------------------------- DJ (§11)
    def dj_state(self) -> dict:
        """Read-only console state — computed in-process, no backend probes
        (TUI.md §4)."""
        st = self.scheduler
        on_air = self.on_air
        current = None
        if st._items:
            seq, off = st.on_air()
            row = next((r for r in st._items if r["seq"] == seq), None)
            if seq is not None and row is not None:
                inv = self.db.get_item(row["item_id"])
                title = (inv.get("title") if inv else None) or ""
                artist = (inv.get("artist") if inv else None) or ""
                current = {"seq": row["seq"], "media_id": row["item_id"],
                           "type": row["type"], "title": title,
                           "artist": artist, "position_s": round(off, 3),
                           "remaining_s": round(max(0.0, row["duration_s"] - off), 3)}
        floor_seq = current["seq"] if current else -1
        upcoming = []
        for r in st._items:
            if r["seq"] <= floor_seq:
                continue
            inv = self.db.get_item(r["item_id"])
            title = (inv.get("title") if inv else None) or ""
            artist = (inv.get("artist") if inv else None) or ""
            upcoming.append({"seq": r["seq"], "media_id": r["item_id"],
                             "type": r["type"], "title": title,
                             "artist": artist, "duration_s": r["duration_s"]})
            if len(upcoming) >= 12:
                break
        return {"epoch": st.epoch, "revision": st.revision,
                "server_time": time.time(), "on_air": on_air,
                "current": current, "upcoming": upcoming,
                "pending_commands": [],
                "catalogue_revision": self.db.catalogue_revision(),
                "player_status": "unknown"}

    @staticmethod
    def _encode_cursor(c: tuple[str, str, int] | None) -> str | None:
        if c is None:
            return None
        return base64.urlsafe_b64encode(
            json.dumps(list(c)).encode()).decode()

    @staticmethod
    def _decode_cursor(s: str | None) -> tuple[str, str, int] | None:
        if not s:
            return None
        try:
            raw = json.loads(base64.urlsafe_b64decode(s.encode()))
            return str(raw[0]), str(raw[1]), int(raw[2])
        except Exception:
            raise HTTPException(
                422, detail={"code": "invalid_cursor",
                             "detail": "bad /api/admin/dj/songs cursor"}) from None

    def dj_songs(self, q: str | None, sort: str, direction: str,
                 cursor: str | None, limit: int) -> dict:
        """Read-only song catalogue with advisory eligibility (TUI.md §4)."""
        cur = self._decode_cursor(cursor)
        rows, nxt, cat_rev = self.db.song_catalogue(q, sort, direction, cur, limit)
        floor = float(min(self.cfg.playout.song_min_spacing_s))
        earliest_wall = self.clock.wall() + self.scheduler.coverage()
        items = []
        for r in rows:
            last = r["last_aired_at"]
            eligible = (bool(r["fresh"]) or last is None
                        or (earliest_wall - last) >= floor)
            items.append({"id": r["id"], "title": r["title"] or "",
                          "artist": r["artist"] or "",
                          "duration_s": r["duration_s"], "eligible": eligible,
                          "reason": None if eligible
                          else "repeats too soon (min spacing)"})
        return {"items": items, "next_cursor": self._encode_cursor(nxt),
                "catalogue_revision": cat_rev}

    # -------------------------------------------------------------- visitors
    @staticmethod
    def _canonical_ip(raw: str | None) -> str | None:
        """Canonicalize a client IP string via stdlib ipaddress, folding
        IPv4-mapped IPv6 into IPv4 and taking the first of an XFF list (13)."""
        if not raw:
            return None
        raw = raw.strip()
        if "," in raw:
            raw = raw.split(",")[0].strip()  # leftmost = the client we saw
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return None
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        return ip.compressed

    def _client_ip(self, request: Request) -> str | None:
        """Resolve the canonical public client IP through the deployment's
        trusted-proxy chain (SPLIT_PLAN §3.2).

        A peer is trusted only when its canonical address is inside a
        configured network (`visitors.trusted_proxies`, single IPs and CIDRs).
        From a trusted peer: `X-Real-IP`, else the first `X-Forwarded-For`
        entry. From an untrusted peer: the socket peer address; both headers
        are ignored so a spoof cannot reach the visitor counter."""
        peer = request.client.host if (request.client and request.client.host) else None
        if self.cfg.visitors.is_trusted(self._canonical_ip(peer)):
            header = request.headers.get("x-real-ip")
            if not header:
                xff = request.headers.get("x-forwarded-for")
                header = xff.split(",")[0].strip() if xff else None
        else:
            header = peer
        return self._canonical_ip(header)

    def register_visitor(self, request: Request) -> int:
        """Store the raw canonical client IP (deduped) and return the unique
        count, starting from 0. Disabled, an unresolvable identity, or a store
        error all just return the current count — the counter never fails and
        never breaks radio/requests (RADIO §14)."""
        ip = self._client_ip(request) if self.cfg.visitors.enabled else None
        try:
            if ip:
                return self.db.register_visitor(ip)
            log.warning("visitor.register skipped: no canonical client identity")
            return self.db.unique_visitors()
        except Exception:
            log.exception("visitor.register failed")
            return 0

    def request_counts(self) -> dict[str, int]:
        """Paid + total request counts for the HIT COUNTER pane (RADIO §9.1).
        Never fails: a store error reports zeros, exactly like the visitor
        counter, so the pane renders instead of throwing (RADIO §14)."""
        try:
            paid, total = self.db.request_counts()
            return {"paid_requests": paid, "total_requests": total}
        except Exception:
            log.exception("request.counts failed")
            return {"paid_requests": 0, "total_requests": 0}

    async def startup(self) -> None:
        # warm the program immediately with whatever inventory exists
        self.scheduler.commit_lookahead()
        self._tasks.append(asyncio.create_task(self.scheduler.run()))
        self._tasks.append(asyncio.create_task(self.phrases.run()))
        self._tasks.append(asyncio.create_task(self.producer.run()))
        self._tasks.append(asyncio.create_task(self.producer.news_loop()))
        self._tasks.append(asyncio.create_task(self.producer.song_loop()))
        self._tasks.append(asyncio.create_task(self.producer.sfx_loop()))
        self._tasks.append(asyncio.create_task(self.idle_loop()))
        self._tasks.append(asyncio.create_task(_backfill_paid_summaries(self)))
        if self.publisher is not None:
            self._tasks.append(asyncio.create_task(self.publisher.run()))
        if self.art_worker is not None:
            self._tasks.append(asyncio.create_task(self.art_worker.run()))
        inv = await self.inventory_levels()
        log.info("station.startup", extra={
            "lookahead_s": self.cfg.playout.committed_lookahead_s, "inventory": inv})


def _load_prompts(cfg: Config) -> dict:
    pdir = ROOT / cfg.library.prompts_dir
    out = {}
    for f in ("voice.md", "song_brief.md", "station_song.md", "news.md", "moderation.md",
              "field.md", "sfx_cues.md", "sfx_joke.md", "paid_request.md",
              "paid_summary.md", "promo.md", "album_art_scene.md",
              "album_art_flux.md"):
        p = pdir / f
        if p.exists():
            out[f.replace(".md", "")] = p.read_text()
    return out


class RequestIn(BaseModel):
    text: str = Field(..., min_length=1)


class PaidRequestIn(BaseModel):
    text: str = Field(..., min_length=1)


PAID_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

_SECTION_TAG = re.compile(r"^\[[^\]]{1,40}\]$")


def _fallback_summary(text: str, max_length: int) -> str:
    """First non-tag line, collapsed; never a bare [verse]/[intro] marker."""
    for line in text.split("\n"):
        clean = " ".join(line.split())
        if not clean or _SECTION_TAG.match(clean):
            continue
        if len(clean) > max_length:
            clean = clean[: max_length - 1] + "…"
        return clean
    clean = " ".join(text.split())
    return clean[: max_length - 1] + "…" if len(clean) > max_length else clean


async def _summarise_paid(station: Station, text: str, max_length: int) -> str:
    """One-line board label for a paid request (PAYMENT.md §3.4). LLM first,
    deterministic fallback when the model fails; never blocks the request."""
    fallback = _fallback_summary(text, max_length)
    template = station.prompts.get("paid_summary")
    if not template:
        return fallback
    started = time.monotonic()
    try:
        raw = await station.llm.chat_text(
            station.cfg.models.briefs, template, json.dumps(text))
    except Exception as exc:  # noqa: BLE001 - a label must never fail the request
        log.warning("payment.summary_failed",
                    extra={"error": type(exc).__name__})
        return fallback
    summary = " ".join(raw.split()).strip().strip('"').strip("'")
    summary = summary.lstrip("*").strip()
    if not summary:
        # An empty answer used to fall back silently (a 512-token cap starved
        # the reasoning model, Oct 31); say so.
        log.warning("payment.summary_empty")
        return fallback
    if len(summary) > max_length:
        summary = summary[: max_length - 1] + "…"
    log.info("payment.summarised",
             extra={"len": len(summary),
                    "duration_ms": round((time.monotonic() - started) * 1000, 1)})
    return summary


async def _backfill_paid_summaries(station: Station) -> None:
    """Regenerate board labels for paid requests on the board (waiting or
    aired) whose label is missing or just the raw-text fallback (left by the
    old 512-token cap)."""
    max_length = station.cfg.requests.max_length
    for row in station.db.paid_rows_needing_summary():
        text = str(row.get("text") or "")
        if row.get("summary") and row["summary"] != _fallback_summary(text, max_length):
            continue
        summary = await _summarise_paid(station, text, max_length)
        if summary != row.get("summary"):
            station.db.set_request_summary(int(row["id"]), summary)
            log.info("payment.summary_backfilled", extra={"request_id": row["id"]})


def _normalise_paid(text: str) -> str:
    """PAYMENT.md Step 4.2: CRLF -> LF, rstrip lines, strip, 3+ blank -> 2."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


# Minimum length for the comp secret: a short value could be guessed through
# the public form, so anything shorter leaves the feature off.
_MIN_OVERRIDE_LEN = 24


def _override_secret(station: Station) -> str:
    secret = station.override_token or ""
    return secret if len(secret) >= _MIN_OVERRIDE_LEN else ""


def _split_override(text: str, secret: str) -> tuple[bool, str]:
    """(comped, prompt): the first line equal to the secret (constant-time)
    marks an operator comp and is removed before anything else sees the text."""
    if not secret:
        return False, text
    first, _, rest = text.partition("\n")
    if hmac.compare_digest(first.strip().encode(), secret.encode()):
        return True, _normalise_paid(rest)
    return False, text


def _leaks_secret(text: str, secret: str) -> bool:
    return bool(secret) and secret in text


_LEAK_REASON = "That request can't be accepted."
_CHECK_FAILED = ("Couldn't finish checking your request. You were not charged; "
                 "try again.")


async def _run_paid_checks(station: Station, request_id: int, text: str,
                           comped: bool) -> None:
    """Moderation -> copyright screen -> summary -> ready (or comped into the
    queue). Runs after the POST has answered; the client polls the stage
    (PAYMENT.md Step 12b). Any crash rejects the row: nobody is charged."""
    db = station.db
    cfg = station.cfg
    try:
        t0 = time.monotonic()
        allowed, reason = await station.moderator.moderate(text)
        log.info("request.moderated", extra={
            "request_id": request_id, "allowed": allowed, "reason": reason,
            "prefilter": False, "paid": True,
            "duration_ms": round((time.monotonic() - t0) * 1000, 1)})
        if not allowed:
            db.reject_checking_request(request_id, reason or "rejected")
            return
        if not db.set_request_stage(request_id, "copyright"):
            return
        verdict = await station.copyright.check(text, request_id)
        if not verdict.allowed:
            db.reject_checking_request(request_id, verdict.reason)
            return
        summary = await _summarise_paid(station, text, cfg.requests.max_length)
        if comped:
            db.set_request_stage(request_id, "ready", summary=summary)
            if db.mark_request_comped(request_id):
                log.info("payment.comped", extra={"request_id": request_id})
            return
        db.set_request_stage(request_id, "ready", summary=summary)
    except Exception as exc:  # noqa: BLE001 - a failed check must never charge
        log.warning("payment.check_failed",
                    extra={"request_id": request_id, "error": type(exc).__name__})
        db.reject_checking_request(request_id, _CHECK_FAILED)


def build_station(cfg: Config) -> Station:
    """Create the one Station for this process (store, pipelines, scheduler)."""
    setup_logging(cfg)
    api_key = load_api_key()
    if not api_key:
        logging.getLogger("radio.server").warning(
            "LITELLM_TOKEN not set (env or .env); LLM and MCP calls will fail")
    return Station(cfg, api_key)


def _make_rate_limiter(station: Station,
                       limit: int | None = None) -> Callable[[Request], bool]:
    """Per-app request-line rate limit: client IP -> timestamps (4.3).

    Keyed on the resolved client IP (`Station._client_ip`, the trusted-proxy
    chain, SPLIT_PLAN §3.2), not the socket peer: behind the gateway every
    request arrives from the same peer, which would put the whole station in
    one bucket. Each app gets its own window; the public app is the only one
    a browser reaches.
    """
    _rate: dict[str, deque[float]] = {}
    limit = limit or station.cfg.requests.per_client_per_10min

    def _rate_limited(request: Request) -> bool:
        host = (station._client_ip(request)
                or (request.client.host if request.client else None) or "unknown")
        now = time.monotonic()
        window = 10 * 60.0
        dq = _rate.setdefault(host, deque(maxlen=limit))
        # drop timestamps outside the 10-minute window
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= limit:
            return True
        dq.append(now)
        return False

    return _rate_limited


async def _public_health(station: Station) -> dict:
    """Public `/api/health`: only what the browser reads.

    No backend probes, no inventory query, no network call (SPLIT_PLAN §1.3).
    `on_air` is the scheduler's committed coverage, which is pure.
    """
    return {"on_air": station.on_air,
            # cached worker state only: no DB read, no FLUX probe (SPLIT_PLAN §1.3)
            "art": {"backend": station.art_worker.backend
                    if station.art_worker else "disabled"},
            "time": time.time()}


async def _admin_health(station: Station) -> dict:
    """Full health for the admin listener (unchanged from today)."""
    return await station.health()


class PlayNextIn(BaseModel):
    """Operator "play this song next" (POST /api/admin/dj/play).

    Deliberately not the full `dj.QueueRequest` command contract (command_id /
    expected_epoch / expected_revision): this route is the narrow operator
    convenience, not the scheduler command service."""

    media_id: int


class _RevalidatingStaticFiles(StaticFiles):
    """Static client files are served with `Cache-Control: no-cache`, so a
    browser (or Cloudflare) revalidates them on each load instead of keeping a
    heuristically-fresh copy: a web edit goes live on the next page load with
    no server restart and no cache-busting build step (RADIO §11). Edges set
    their own `max-age=60` for the copy they serve; media keeps its own
    immutable header."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            resp.headers["Cache-Control"] = "no-cache"
        return resp


def _mount_web(app: FastAPI, cfg: Config) -> None:
    """Mount the static client at "/" (RADIO §9, §11). Mounted last so API
    routes take precedence; html=True serves index.html at "/"."""
    web_dir = ROOT / cfg.library.web_dir
    app.mount("/", _RevalidatingStaticFiles(directory=str(web_dir), html=True),
              name="web")


def _add_public_routes(app: FastAPI, station: Station,
                       health: Callable[[Station], Awaitable[dict]]) -> None:
    """Register the edge-facing surface exactly once, shared by both apps.

    Only these routes exist on the public listener (SPLIT_PLAN §1.2). The
    admin app calls this too, passing the full health handler; admin-only
    routes are added separately by `_add_admin_routes`.
    """
    cfg = station.cfg
    _rate_limited = _make_rate_limiter(station)
    _paid_rate_limited = _make_rate_limiter(station, cfg.payments.per_client_per_10min)
    _vote_rate_limited = _make_rate_limiter(station, cfg.voting.per_client_per_10min)
    _rating_rate_limited = _make_rate_limiter(station, cfg.ratings.per_client_per_10min)

    def _board() -> dict:
        """The public board, with the voting fields and bury policy applied
        (VOTING.md §1/§2). Every handler that returns the board uses this so a
        mixed-version deploy cannot show a row without its score."""
        return station.db.request_board(
            cfg.requests.queue_cap, cfg.requests.max_length,
            stages=station.producer.request_stage,
            voting_enabled=cfg.voting.enabled,
            bury_threshold=cfg.voting.bury_threshold)

    @app.get("/api/health")
    async def health_endpoint():
        return JSONResponse(await health(station),
                            headers={"Cache-Control": "public, max-age=2"})

    @app.get("/api/station/program")
    async def program(after_seq: int | None = Query(default=None, ge=0),
                      epoch: str | None = Query(default=None, max_length=64),
                      revision: int | None = Query(default=None, ge=0)):
        # IDLE.md D2: PLAY is a program fetch with no cursor. Wake first, then
        # build the response, so the waking request already gets a playable
        # programme (thaw only touches rendered inventory: hard rule 1).
        station.wake("program")
        return JSONResponse(station.program_response(after_seq, epoch, revision),
                            headers={"Cache-Control": "public, max-age=1"})

    @app.get("/api/station/state")
    async def station_state():
        """Cheap public station state for DJ-aware clients (RADIO §11, §5.3)."""
        return JSONResponse(station.public_state(),
                            headers={"Cache-Control": "public, max-age=1"})

    @app.post("/api/station/heartbeat")
    async def heartbeat(payload: dj.HeartbeatIn):
        # item_id comes from the client as media_id (the inventory id); seq is
        # the program position (OVERHAUL 2.5 — they used to be conflated). The
        # revision-aware fields (player_id, observed_epoch/revision, readiness)
        # are accepted and ignored until the browser upgrade lands (TUI.md §9.2),
        # except player_id, which feeds the live listener count (RADIO §14).
        station.touch_listener(payload.player_id)
        station.record_player_telemetry(payload.player_id, payload)
        station.db.record_airplay(
            item_id=int(payload.media_id or 0),
            seq=int(payload.seq or 0),
            item_type=payload.type,
            started_at=payload.started_at,
            position=payload.position,
            underrun=1 if payload.underrun else 0)
        return {"ok": True}

    @app.get("/api/media/{item_id}")
    async def media(item_id: int):
        it = station.db.get_item(item_id)
        if not it:
            return JSONResponse({"detail": "no such item"}, status_code=404,
                                headers={"Cache-Control": "public, max-age=30"})
        p = ROOT / it["media_path"]
        if not p.exists():
            return JSONResponse({"detail": "media missing"}, status_code=404,
                                headers={"Cache-Control": "public, max-age=30"})
        # A media id always maps to the same bytes: every pipeline writes a new
        # file before add_item(); retire keeps it, purge deletes it (SPLIT_PLAN
        # §2 check, no in-place rewrite exists). Safe to cache for a year.
        # Transitional (RADIO §8.4): only items not yet in S3 are fetched here.
        mtype = "audio/mpeg" if p.suffix == ".mp3" else "audio/flac"
        return FileResponse(str(p), media_type=mtype,
                            headers={"Cache-Control":
                                     "public, max-age=31536000, immutable"})

    @app.post("/api/requests")
    async def submit_request(payload: RequestIn, request: Request):
        if _rate_limited(request):
            raise HTTPException(
                429,
                "Easy there — a few requests every ten minutes, please.")
        text = payload.text.strip()
        if not text or len(text) > cfg.requests.max_length:
            raise HTTPException(422, "request empty or too long")
        if _leaks_secret(text, _override_secret(station)):
            # The comp secret pasted into the free line: never stored, logged,
            # moderated or put on the board (PAYMENT.md Step 12b).
            return {"ok": False, "rejected": True, "reason": _LEAK_REASON,
                    "paid_enabled": station.payments_active}
        if station.db.waiting_free_requests() >= cfg.requests.free_queue_max:
            # Free line full (RADIO §14): refuse the new one, never drop a
            # waiting one. Paid requests are never capped.
            log.info("request.line_full",
                     extra={"limit": cfg.requests.free_queue_max})
            board = _board()
            return {"ok": False, "rejected": True, "line_full": True,
                    "reason": "The request line is full right now. Try again in "
                              "a little while, or jump the line with a priority "
                              "request.",
                    "queue": board["queue"], "recent": board["recent"],
                    "cap": cfg.requests.queue_cap,
                    "paid_enabled": station.payments_active}
        t0 = time.monotonic()
        # Deterministic pre-filter first: URLs/contact info never reach the LLM (4.1).
        pref = prefilter(text)
        if pref:
            req = station.db.add_request(text, cap=None,
                                         status="rejected", reason=pref)
            log.info("request.received", extra={"request_id": req["id"],
                                                  "len": len(text), "prefilter": True})
            log.info("request.moderated", extra={
                "request_id": req["id"], "allowed": False, "reason": pref,
                "prefilter": True, "duration_ms": 0})
            board = _board()
            return {
                "ok": False, "rejected": True, "request": req, "reason": pref,
                "queue": board["queue"], "recent": board["recent"],
                "cap": cfg.requests.queue_cap,
                "paid_enabled": station.payments_active,
            }
        allowed, reason = await station.moderator.moderate(text)
        status = "queued" if allowed else "rejected"
        req = station.db.add_request(text, cap=None,
                                     status=status, reason=None if allowed else reason)
        log.info("request.received", extra={"request_id": req["id"], "len": len(text)})
        log.info("request.moderated", extra={
            "request_id": req["id"], "allowed": allowed, "reason": reason,
            "prefilter": False,
            "duration_ms": round((time.monotonic() - t0) * 1000, 1)})
        board = _board()
        return {
            "ok": allowed,
            "rejected": not allowed,
            "request": req,
            "reason": None if allowed else reason,
            "queue": board["queue"],
            "recent": board["recent"],
            "cap": cfg.requests.queue_cap,
            "paid_enabled": station.payments_active,
        }

    @app.get("/api/requests")
    async def list_requests():
        board = _board()
        return JSONResponse({**board, "cap": cfg.requests.queue_cap,
                             "paid_enabled": station.payments_active,
                             "voting_enabled": cfg.voting.enabled},
                            headers={"Cache-Control": "public, max-age=2"})

    # ------------------------------------------- community voting (VOTING.md §2)
    @app.post("/api/requests/{request_id}/vote")
    async def vote_request(request_id: int, payload: voting.VoteIn,
                           request: Request):
        """+1/-1/0 (retract) on a free request. POST only: the gateway has no
        limit_req zone for PUT/DELETE (VOTING.md §0.1)."""
        if not cfg.voting.enabled:
            raise HTTPException(404, "not found")
        if _vote_rate_limited(request):
            raise HTTPException(429, "Easy there — a few votes every ten minutes.")
        status, body = voting.cast_vote(
            station.db, cfg, request_id, payload.voter_id, payload.value,
            station._client_ip(request))
        return JSONResponse(status_code=status, content=body,
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/station/rating")
    async def rate_song(payload: voting.RatingIn, request: Request):
        """Five turkeys for the on-air song. The window is server-authoritative
        (VOTING.md V8); the client only hides the control."""
        if not cfg.ratings.enabled:
            raise HTTPException(404, "not found")
        if _rating_rate_limited(request):
            raise HTTPException(429, "Easy there — a few ratings every ten minutes.")
        status, body = voting.cast_rating(
            station.db, cfg, station.scheduler, payload,
            station._client_ip(request))
        return JSONResponse(status_code=status, content=body,
                            headers={"Cache-Control": "no-store"})

    # ------------------------------------------------- paid priority (PAYMENT §4)
    @app.post("/api/paid-requests")
    async def submit_paid_request(payload: PaidRequestIn, request: Request,
                                  background: BackgroundTasks):
        """PAYMENT.md Step 12b: prefilter now, then moderation + copyright in
        the background; the client polls the stage and asks for Checkout
        only once the row is `ready`."""
        if _paid_rate_limited(request):
            raise HTTPException(
                429, "Easy there — a few paid requests every ten minutes, please.")
        secret = _override_secret(station)
        comped, text = _split_override(_normalise_paid(payload.text), secret)
        if not comped and (not station.payments_active
                           or station.payment_service is None):
            return JSONResponse(status_code=503, content={
                "detail": "Paid requests are closed right now.",
                "paid_closed": True})
        lines = text.count("\n") + 1
        if (not text or len(text) > cfg.payments.paid_max_length
                or lines > cfg.payments.paid_max_lines):
            raise HTTPException(422, "paid request empty or too long")
        if _leaks_secret(text, secret):
            return {"ok": False, "rejected": True, "reason": _LEAK_REASON}
        pref = prefilter(text, phone_mode="runs")
        if pref:
            req = station.db.add_request(text, cap=None, status="rejected",
                                         reason=pref, priority=1)
            log.info("request.moderated", extra={
                "request_id": req["id"], "allowed": False, "reason": pref,
                "prefilter": True, "paid": True, "duration_ms": 0})
            return {"ok": False, "rejected": True, "reason": pref}
        row = station.db.add_checking_request(text, comped=comped)
        log.info("payment.requested",
                 extra={"request_id": row["id"], "len": len(text), "lines": lines,
                        "comped": comped,
                        "client_ip": station._client_ip(request) if comped else None})
        background.add_task(_run_paid_checks, station, int(row["id"]), text, comped)
        return {"ok": True, "token": row["public_token"], "stage": "moderation"}

    @app.post("/api/paid-requests/{token}/checkout")
    async def start_paid_checkout(token: str):
        """Create the Checkout Session for a row that passed every check. The
        client_secret is returned once and never stored (AGENTS §1.11)."""
        if not PAID_TOKEN_RE.match(token):
            raise HTTPException(404, "not found")
        if not station.payments_active or station.payment_service is None:
            return JSONResponse(status_code=503, content={
                "detail": "Paid requests are closed right now.",
                "paid_closed": True})
        row = station.db.get_request_by_token(token)
        if row is None:
            raise HTTPException(404, "not found")
        if not station.db.claim_for_checkout(int(row["id"])):
            return JSONResponse(status_code=409, content={
                "detail": "That request isn't ready for payment."})
        try:
            session = await station.payment_service.start_checkout(row)
        except StripeError as exc:
            station.db.mark_request_abandoned(int(row["id"]))
            log.warning("payment.checkout_failed",
                        extra={"request_id": row["id"], "kind": exc.kind})
            return JSONResponse(status_code=424, content={
                "detail": "Payments are having a moment. You were not charged."})
        if not session.client_secret:
            station.db.mark_request_abandoned(int(row["id"]))
            log.warning("payment.checkout_no_secret",
                        extra={"request_id": row["id"], "session_id": session.id})
            return JSONResponse(status_code=424, content={
                "detail": "Payments are having a moment. You were not charged."})
        return JSONResponse(content={
            "ok": True,
            "client_secret": session.client_secret,
            "publishable_key": station.publishable_key,
            "expires_at": session.expires_at
            or int(station.clock.wall() + cfg.payments.checkout_expiry_s),
        })

    # ------------------------------------------- paid status + reconcile (§6)
    @app.get("/api/paid-requests/{token}")
    async def paid_request_status(token: str):
        if not PAID_TOKEN_RE.match(token):
            raise HTTPException(404, "not found")
        if station.payment_service is not None:
            row = await station.payment_service.reconcile(token)
        else:
            row = station.db.get_request_by_token(token)
        if row is None:
            raise HTTPException(404, "not found")
        status = str(row["status"])
        position = (station.db.request_position(int(row["id"]))
                    if status in ("queued", "producing", "ready") else None)
        body: dict = {"status": status, "position": position,
                      "stage": row.get("stage") if status == "checking" else None,
                      "comped": bool(row.get("comped"))}
        if status == "rejected":
            body["reason"] = row.get("reason")
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------- Stripe webhook (§5)
    @app.post("/api/stripe/webhook")
    async def stripe_webhook(request: Request):
        if station.payment_service is None:
            return JSONResponse(status_code=503, content={"detail": "payments disabled"})
        payload = await request.body()
        if len(payload) > 256 * 1024:
            return JSONResponse(status_code=413, content={"detail": "payload too large"})
        try:
            event = verify_webhook(
                payload,
                request.headers.get("stripe-signature", ""),
                station.webhook_secret,
                station.clock.wall(),
                cfg.payments.webhook_tolerance_s,
            )
        except WebhookError as exc:
            log.warning("payment.webhook_rejected", extra={"reason": str(exc)})
            return JSONResponse(status_code=400, content={"detail": "bad signature"})
        outcome = station.payment_service.handle_event(event)
        return JSONResponse({"received": True, "outcome": outcome})

    @app.post("/api/visitors")
    async def register_visitor(request: Request):
        """Register the server-derived client IP and return the unique count
        (RADIO §14). Idempotent per client; media fetches / heartbeats / health
        polls / request submissions never hit this. Always 200 with a count.
        Also returns the DB request counts the HIT COUNTER pane shows
        (RADIO §9.1): `paid_requests` (paid priority rows) and
        `total_requests` (every request row)."""
        n = station.register_visitor(request)
        resp = JSONResponse({"unique_visitors": n, **station.request_counts()},
                            headers={"Cache-Control": "no-store"})
        return resp


def _add_admin_routes(app: FastAPI, station: Station) -> None:
    """Admin-only surface: control + DJ console (SPLIT_PLAN §1.2).

    Never registered on the public app. The admin app is a superset, so the
    TUI and existing tests keep working against it.
    """
    cfg = station.cfg

    @app.post("/api/station/start")
    async def start():
        return {"ok": True, "message": "station is always on air"}

    @app.post("/api/station/stop")
    async def stop():
        return {"ok": True, "message": "stop is a no-op in Phase 1 (station stays on air)"}

    @app.get("/api/admin/voices")
    async def admin_voices():
        with contextlib.suppress(Exception):
            return {"voices": await station.kokoro.voices()}
        return {"voices": []}

    # ---- DJ console (RADIO §11): read-only behind the operator token. Control
    # mutations (skip/queue/phrases) land with the scheduler command service and
    # additionally require dj.enabled (TUI.md §7). ----
    @app.post("/api/admin/dj/play", dependencies=[Depends(require_dj)])
    async def dj_play(payload: PlayNextIn):
        """Operator: air this song next (ALBUM_ART demo / TUI §5).

        A narrow control mutation: it splices a *ready, airable song* into the
        slot after whatever is playing, with the words-between-songs bridges,
        and bumps the revision so clients refetch. It does not create a
        request, does not touch payments, and never waits on production.
        """
        if not cfg.dj.enabled:
            raise HTTPException(403, detail={
                "code": "dj_disabled", "detail": "DJ control is disabled"})
        # IDLE.md D2: an operator mutation is a human presence; wake first so
        # the splice lands in a running programme, not a frozen window.
        station.wake("dj")
        seq = station.scheduler.place_song_next(int(payload.media_id))
        if seq is None:
            raise HTTPException(409, detail={
                "code": "cannot_place",
                "detail": "not an airable song, or no legal slot this cycle"})
        return {"ok": True, "seq": seq,
                "revision": station.scheduler.revision}

    @app.get("/api/admin/voting", dependencies=[Depends(require_dj)])
    async def admin_voting(limit: int = Query(default=100, ge=1, le=500)):
        """Audit only (VOTING.md §2): recent votes and ratings, plus the
        currently buried free rows. Never exposes voter ids or IPs."""
        threshold = cfg.voting.bury_threshold
        return {"votes": station.db.recent_votes(limit),
                "ratings": station.db.recent_ratings(limit),
                "buried": (station.db.buried_requests(threshold, limit)
                           if threshold is not None else [])}

    @app.get("/api/admin/dj/state", response_model=dj.DjState,
             dependencies=[Depends(require_dj)])
    async def dj_state():
        """Read-only console state; no backend probes (TUI.md §4)."""
        return station.dj_state()

    @app.get("/api/admin/dj/songs", response_model=dj.DjSongPage,
             dependencies=[Depends(require_dj)])
    async def dj_songs(q: str | None = Query(default=None, max_length=200),
                       sort: dj.SortField = "title",
                       direction: dj.SortDirection = "asc",
                       cursor: str | None = Query(default=None, max_length=512),
                       limit: int = Query(default=100, ge=1, le=500)):
        """Sortable/searchable song catalogue (TUI.md §2, §4)."""
        return station.dj_songs(q, sort, direction, cursor, limit)

    @app.get("/api/admin/dj/character", response_model=dj.CharacterInfo,
             dependencies=[Depends(require_dj)])
    async def dj_character():
        """Operator character + setup readiness + unused voices (TUI.md §6).
        The voice list is the one live probe here; failure is surfaced as an
        explicit error, never a silent empty pick."""
        ch = cfg.dj.character
        # The exclusion set is every role voice *plus the character's own
        # reservation*: an assigned voice is not an "available unused" one
        # (RADIO.md §10). Empty strings never match a real voice id.
        used = {cfg.voices.dj, cfg.voices.news, cfg.voices.commercials,
                cfg.voices.liners, cfg.voices.field_reporter}
        if ch.voice:
            used.add(ch.voice)
        available: list = []
        error: str | None = None
        try:
            voices = await station.kokoro.voices()
            # Kokoro /voices is `{voices: [...], default: ...}` (probe, §2);
            # tolerate a bare list too.
            if isinstance(voices, dict):
                voices = voices.get("voices") or []
            available = [v for v in voices if v not in used]
        except Exception as e:  # noqa: BLE001 - surfaced to the operator untouched
            error = str(e)
        return {
            "character_id": ch.id,
            "name": ch.name,
            "setup_complete": bool(ch.name and ch.voice),
            "voice": ch.voice or None,
            "available_voices": available,
            "voice_discovery_error": error,
            "block_reason": None if bool(ch.name and ch.voice)
            else "character name/voice not both set",
        }

    @app.post("/api/admin/dj/phrases/validate", response_model=dj.ValidateResponse,
              dependencies=[Depends(require_dj)])
    async def phrases_validate(payload: dj.PhraseValidateRequest):
        """Pure cleaned-text validation; no synthesis or scheduling (TUI.md §6)."""
        return station.phrases.validate(payload.character_id, payload.text)

    @app.post("/api/admin/dj/phrases", response_model=dj.PhraseSubmitResponse,
              dependencies=[Depends(require_dj)], status_code=202)
    async def phrases_submit(payload: dj.PhraseSubmitRequest):
        """Validate the confirmed text, persist a job, render it in the
        background and place it at the earliest legal boundary. Idempotent by
        command_id (identical retry returns the existing job; different content
        with the same command_id is 409)."""
        try:
            return station.phrases.submit(
                payload.command_id, payload.character_id,
                payload.text, payload.cleaned_text_hash)
        except ValueError as e:
            code = str(e)
            if code in ("dj_disabled",):
                raise HTTPException(403, detail={"code": code,
                    "detail": "DJ controls are not enabled yet"}) from None
            if code in ("conflict", "hash_mismatch", "no_voice"):
                raise HTTPException(409, detail={"code": code,
                    "detail": "phrase conflicts with prior submission"}) from None
            raise HTTPException(422, detail={"code": code,
                "detail": "phrase rejected"}) from None

    @app.get("/api/admin/dj/phrases/{job_id}", response_model=dj.PhraseJob,
             dependencies=[Depends(require_dj)])
    async def phrases_job(job_id: int):
        """Phrase job render stage and finished clip (TUI.md §6)."""
        job = station.phrases.get(job_id)
        if not job:
            raise HTTPException(404, detail={"code": "no_such_job",
                "detail": "no phrase job with that id"})
        return job

    @app.get("/api/admin/payments", dependencies=[Depends(require_dj)])
    async def admin_payments(
        status: str = Query(default="failed,awaiting_payment,abandoned,queued"),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        """Paid request audit + money-without-a-request events (PAYMENT.md §9)."""
        statuses = [s for s in status.split(",") if s]
        keys = ("id", "status", "priority", "created_at", "paid_at",
                "amount_cents", "currency", "payment_intent_id",
                "checkout_session_id", "attempts", "text")
        return {
            "requests": [{k: row.get(k) for k in keys}
                         for row in station.db.paid_requests(statuses, limit)],
            "mismatches": station.db.mismatch_events(50),
        }



def _new_app(title: str) -> FastAPI:
    app = FastAPI(title=title, version="0.2.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])

    @app.middleware("http")
    async def _no_store_posts(request: Request, call_next):
        """SPLIT_PLAN §2: every POST is no-store, including error responses.

        GET cache policy is set by the individual handlers; static files are
        left to the edge.
        """
        response = await call_next(request)
        if request.method == "POST":
            response.headers["Cache-Control"] = "no-store"
        return response

    return app


def _add_api_404(app: FastAPI) -> None:
    """Any `/api/*` path the public app does not register is 404, never 405.

    The static mount at "/" would otherwise answer unmatched POSTs with 405
    (SPLIT_PLAN §1.5 expects 404 for control routes on the public listener).
    Registered before the static mount; real routes are matched first.
    """

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT",
                                               "PATCH", "DELETE", "HEAD",
                                               "OPTIONS"],
                   include_in_schema=False)
    async def _unknown_api(rest: str):
        raise HTTPException(404, "not found")


def create_public_app(cfg: Config, station: Station) -> FastAPI:
    """The public listener: only the edge-facing routes (§1.2)."""
    app = _new_app("Pilgrim Dot Farm Radio")
    app.state.station = station
    _add_public_routes(app, station, _public_health)
    _add_api_404(app)
    if cfg.station.serve_web:
        _mount_web(app, cfg)
    return app


def create_admin_app(cfg: Config, station: Station) -> FastAPI:
    """The admin listener: full API, no static web mount (§1.2)."""
    app = _new_app("Pilgrim Dot Farm Radio (admin)")
    app.state.station = station
    _add_public_routes(app, station, _admin_health)
    _add_admin_routes(app, station)
    return app


def create_app(cfg: Config | None = None) -> FastAPI:
    """Legacy single-app factory for tests (kept name + signature).

    Returns the admin app with the static web mount and the deferred startup
    hook, exactly as before (SPLIT_PLAN §1.2). Production uses `main()`.
    """
    cfg = cfg or load_config()
    station = build_station(cfg)
    app = create_admin_app(cfg, station)
    if cfg.station.serve_web:
        _mount_web(app, cfg)

    @app.on_event("startup")
    async def _startup():
        asyncio.create_task(_post_start(station))

    return app


async def _post_start(station: Station) -> None:
    await asyncio.sleep(0.5)
    await station.startup()


def listener_configs(cfg: Config, public: FastAPI, admin: FastAPI) -> list:
    """uvicorn configs for the public and admin listeners (SPLIT_PLAN §1.2).

    `proxy_headers=False`: uvicorn would otherwise rewrite `request.client`
    from `X-Forwarded-For` for loopback peers before our code runs, a second
    trust decision beside `visitors.trusted_proxies` (SPLIT_PLAN §3.2).
    `log_config=None`: uvicorn's lines propagate to the root JSON logger
    (AGENTS §5).
    """
    import uvicorn

    return [
        uvicorn.Config(public, host=cfg.station.host, port=cfg.station.port,
                       log_config=None, proxy_headers=False),
        uvicorn.Config(admin, host=cfg.station.admin_host,
                       port=cfg.station.admin_port, log_config=None,
                       proxy_headers=False),
    ]


def main() -> None:
    import signal

    import uvicorn

    cfg = load_config()
    station = build_station(cfg)
    public = create_public_app(cfg, station)
    admin = create_admin_app(cfg, station)

    class _Server(uvicorn.Server):
        """uvicorn.Server without its own signal capture.

        Two servers share one event loop, so each would install SIGINT/SIGTERM
        and the second install would replace the first — leaving one listener
        running after Ctrl-C/SIGTERM. `main()` installs a single handler that
        stops both (SPLIT_PLAN §1.2, deviation noted in the report).
        """

        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            yield

    servers = [_Server(c) for c in listener_configs(cfg, public, admin)]

    def _shutdown(signum: int, frame: object) -> None:
        for s in servers:
            s.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _shutdown)

    async def _serve() -> None:
        await station.startup()  # exactly once per process
        await asyncio.gather(*(s.serve() for s in servers))

    asyncio.run(_serve())


if __name__ == "__main__":
    main()

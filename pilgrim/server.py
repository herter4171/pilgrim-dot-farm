"""Station server (RADIO.md §11). FastAPI + uvicorn on config.port.

Serves the web client, the program/live endpoints, media, and health. Starts
the producer + scheduler in the background so the station is on air regardless
of whether a listener is tuned in.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import logging
import time
import uuid
from collections import deque

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pilgrim import dj_contracts as dj
from pilgrim.config import RNG, ROOT, Clock, Config, ensure_dirs, load_api_key, load_config
from pilgrim.dj_security import require_dj
from pilgrim.logging_setup import setup_logging
from pilgrim.pipelines.llm import LLM
from pilgrim.pipelines.moderation import Moderation
from pilgrim.pipelines.news import NewsPipeline
from pilgrim.pipelines.operator_phrase import PhraseManager
from pilgrim.pipelines.request_filter import prefilter
from pilgrim.pipelines.sfx import SfxClient, SfxPipeline
from pilgrim.pipelines.songs import SongPipeline
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
        self.llm = LLM(cfg, api_key)
        self.kokoro = KokoroClient(cfg)
        self.moderator = Moderation(cfg, self.llm, prompts=_load_prompts(cfg))
        self.voice = VoicePipeline(cfg, self.llm, self.kokoro, self.db, self.media_dir,
                                   prompts=_load_prompts(cfg))
        self.songs = SongPipeline(cfg, self.llm, self.media_dir, prompts=_load_prompts(cfg))
        self.news = NewsPipeline(cfg, self.llm, api_key=api_key,
                                prompts=_load_prompts(cfg))
        self.sfx_client = SfxClient(cfg)
        self.sfx = SfxPipeline(cfg, self.sfx_client, self.media_dir)
        self.clock = Clock()
        self._listeners: dict[str, float] = {}  # player_id -> last heartbeat (clock.now)
        self.rng = RNG(cfg.station.rng_seed)
        self.selector = RandomSelector(self.rng, cfg)
        self.epoch = uuid.uuid4().hex[:12]  # changes on restart (TUI.md §5)
        self.scheduler = Scheduler(cfg, self.db, self.selector, self.clock, self.rng,
                                   epoch=self.epoch)
        self.phrases = PhraseManager(cfg, self.db, self.voice, self.media_dir,
                                     scheduler=self.scheduler)
        self.producer = Producer(
            cfg, self.db, self.llm, self.kokoro, self.voice, self.songs,
            self.clock, prompts=_load_prompts(cfg), media_dir=self.media_dir,
            api_key=api_key, rng=self.rng, news_pipeline=self.news, sfx=self.sfx)
        self._tasks: list = []
        self._http = httpx.AsyncClient(timeout=4.0)

    # ------------------------------------------------------------------ health
    async def backend_status(self) -> dict:
        def ok(status: int) -> bool:
            return 200 <= status < 500
        st = {"litellm": False, "kokoro": False, "mlx": False, "searxng": False,
              "sfx": False}
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
        try:
            r = await self._http.get(self.cfg.hosts.mlx_serve.rstrip("/") + "/v1/models")
            st["mlx"] = ok(r.status_code)
        except Exception:
            st["mlx"] = False
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
        }

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
            "time": time.time(),
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
                        "type": it["type"], "duration_s": it["duration_s"],
                        "sfx": scheduler.overlays_for(it["seq"]),
                        "title": title, "artist": artist})
        return {"items": out, "start_offset_s": start_offset,
                "epoch": scheduler.epoch, "revision": scheduler.revision,
                "reset": stale, "server_time": time.time()}

    def public_state(self) -> dict:
        """Cheap station state for DJ-aware clients (RADIO §11, §5.3).
        Reads in-memory schedule state; never probes production backends."""
        st = self.scheduler
        seq = ctype = None
        offset = 0.0
        if st._items:
            s, off = st.on_air()
            if s is not None:
                row = next((r for r in st._items if r["seq"] == s), None)
                seq, offset = s, off
                ctype = row["type"] if row else None
        return {"epoch": st.epoch, "revision": st.revision,
                "server_time": time.time(), "current_seq": seq,
                "current_type": ctype, "current_offset_s": round(offset, 3),
                "prepared_cutover": None, "listeners": self.active_listeners()}

    # ------------------------------------------------------------- listeners
    def _prune_listeners(self) -> None:
        cutoff = self.clock.now() - self.cfg.visitors.listener_timeout_s
        for pid in [p for p, t in self._listeners.items() if t < cutoff]:
            del self._listeners[pid]

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
        verified trusted-proxy setup (nginx sets X-Real-IP to $remote_addr on
        loopback; COSMETIC_PATCHING §6). Never trust a spoofable header from an
        untrusted peer — use the socket peer address directly."""
        peer = request.client.host if (request.client and request.client.host) else None
        trusted = set(self.cfg.visitors.trusted_proxies)
        if peer in trusted:
            header = (request.headers.get("x-real-ip")
                      or request.headers.get("x-forwarded-for"))
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

    async def startup(self) -> None:
        # warm the program immediately with whatever inventory exists
        self.scheduler.commit_lookahead()
        self._tasks.append(asyncio.create_task(self.scheduler.run()))
        self._tasks.append(asyncio.create_task(self.phrases.run()))
        self._tasks.append(asyncio.create_task(self.producer.run()))
        self._tasks.append(asyncio.create_task(self.producer.news_loop()))
        self._tasks.append(asyncio.create_task(self.producer.song_loop()))
        self._tasks.append(asyncio.create_task(self.producer.sfx_loop()))
        inv = await self.inventory_levels()
        log.info("station.startup", extra={
            "lookahead_s": self.cfg.playout.committed_lookahead_s, "inventory": inv})


def _load_prompts(cfg: Config) -> dict:
    pdir = ROOT / cfg.library.prompts_dir
    out = {}
    for f in ("voice.md", "song_brief.md", "news.md", "moderation.md", "field.md",
              "sfx_cues.md", "sfx_joke.md"):
        p = pdir / f
        if p.exists():
            out[f.replace(".md", "")] = p.read_text()
    return out


class RequestIn(BaseModel):
    text: str = Field(..., min_length=1)


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    setup_logging(cfg)
    api_key = load_api_key()
    if not api_key:
        logging.getLogger("radio.server").warning(
            "LITELLM_TOKEN not set (env or .env); LLM and MCP calls will fail")
    station = Station(cfg, api_key)
    app = FastAPI(title="Pilgrim Dot Farm Radio", version="0.2.0")
    app.state.station = station
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])
    # request-line rate limit: client.host -> timestamps within the window (4.3)
    _rate: dict[str, deque[float]] = {}

    def _rate_limited(request: Request) -> bool:
        host = request.client.host if request.client else "unknown"
        now = time.monotonic()
        window = 10 * 60.0
        dq = _rate.setdefault(host, deque(maxlen=cfg.requests.per_client_per_10min))
        # drop timestamps outside the 10-minute window
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= cfg.requests.per_client_per_10min:
            return True
        dq.append(now)
        return False

    @app.get("/api/health")
    async def health():
        return await station.health()

    @app.post("/api/station/start")
    async def start():
        return {"ok": True, "message": "station is always on air"}

    @app.post("/api/station/stop")
    async def stop():
        return {"ok": True, "message": "stop is a no-op in Phase 1 (station stays on air)"}

    @app.get("/api/station/program")
    async def program(after_seq: int | None = Query(default=None, ge=0),
                      epoch: str | None = Query(default=None, max_length=64),
                      revision: int | None = Query(default=None, ge=0)):
        return station.program_response(after_seq, epoch, revision)

    @app.get("/api/station/state")
    async def station_state():
        """Cheap public station state for DJ-aware clients (RADIO §11, §5.3)."""
        return station.public_state()

    @app.post("/api/station/heartbeat")
    async def heartbeat(payload: dj.HeartbeatIn):
        # item_id comes from the client as media_id (the inventory id); seq is
        # the program position (OVERHAUL 2.5 — they used to be conflated). The
        # revision-aware fields (player_id, observed_epoch/revision, readiness)
        # are accepted and ignored until the browser upgrade lands (TUI.md §9.2),
        # except player_id, which feeds the live listener count (RADIO §14).
        station.touch_listener(payload.player_id)
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
            raise HTTPException(404, "no such item")
        p = ROOT / it["media_path"]
        if not p.exists():
            raise HTTPException(404, "media missing")
        return FileResponse(str(p), media_type="audio/flac")

    @app.get("/api/admin/voices")
    async def admin_voices():
        with contextlib.suppress(Exception):
            return {"voices": await station.kokoro.voices()}
        return {"voices": []}

    # ---- DJ console (RADIO §11): read-only behind the operator token. Control
    # mutations (skip/queue/phrases) land with the scheduler command service and
    # additionally require dj.enabled (TUI.md §7). ----
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
        used = {cfg.voices.dj, cfg.voices.news, cfg.voices.commercials,
                cfg.voices.liners, cfg.voices.field_reporter}
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

    @app.post("/api/requests")
    async def submit_request(payload: RequestIn, request: Request):
        if _rate_limited(request):
            raise HTTPException(
                429,
                "Easy there — a few requests every ten minutes, please.")
        text = payload.text.strip()
        if not text or len(text) > cfg.requests.max_length:
            raise HTTPException(422, "request empty or too long")
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
            board = station.db.request_board(cfg.requests.queue_cap)
            return {
                "ok": False, "rejected": True, "request": req, "reason": pref,
                "queue": board["queue"], "recent": board["recent"],
                "cap": cfg.requests.queue_cap,
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
        board = station.db.request_board(cfg.requests.queue_cap)
        return {
            "ok": allowed,
            "rejected": not allowed,
            "request": req,
            "reason": None if allowed else reason,
            "queue": board["queue"],
            "recent": board["recent"],
            "cap": cfg.requests.queue_cap,
        }

    @app.get("/api/requests")
    async def list_requests():
        board = station.db.request_board(cfg.requests.queue_cap)
        return {**board, "cap": cfg.requests.queue_cap}

    @app.post("/api/visitors")
    async def register_visitor(request: Request):
        """Register the server-derived client IP and return the unique count
        (RADIO §14). Idempotent per client; media fetches / heartbeats / health
        polls / request submissions never hit this. Always 200 with a count."""
        n = station.register_visitor(request)
        resp = JSONResponse({"unique_visitors": n},
                            headers={"Cache-Control": "no-store"})
        return resp

    @app.on_event("startup")
    async def _startup():
        asyncio.create_task(_post_start(station))

    # Serve the web client (RADIO.md §9, §11 GET / static UI). Mounted last so
    # the API routes above take precedence; html=True serves index.html at "/".
    web_dir = ROOT / cfg.library.web_dir
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")

    return app


async def _post_start(station: Station) -> None:
    await asyncio.sleep(0.5)
    await station.startup()


def main() -> None:
    import uvicorn
    cfg = load_config()
    app = create_app(cfg)
    # log_config=None: uvicorn's access/error lines propagate to the root JSON
    # logger, so they carry timestamps like everything else (AGENTS §5)
    uvicorn.run(app, host=cfg.station.host, port=cfg.station.port, log_config=None)


if __name__ == "__main__":
    main()

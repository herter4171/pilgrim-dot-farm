"""Station server (RADIO.md §11). FastAPI + uvicorn on config.port.

Serves the web client, the program/live endpoints, media, and health. Starts
the producer + scheduler in the background so the station is on air regardless
of whether a listener is tuned in.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pilgrim.config import RNG, ROOT, Clock, Config, ensure_dirs, load_config
from pilgrim.pipelines.llm import LLM
from pilgrim.pipelines.news import NewsPipeline
from pilgrim.pipelines.songs import SongPipeline
from pilgrim.pipelines.voice import KokoroClient, VoicePipeline
from pilgrim.producer import Producer
from pilgrim.scheduler import Scheduler
from pilgrim.selector import RandomSelector
from pilgrim.store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("radio.server")


class Station:
    def __init__(self, cfg: Config, api_key: str) -> None:
        self.cfg = cfg
        self.api_key = api_key
        ensure_dirs(cfg)
        self.media_dir = ROOT / cfg.library.dir
        self.db = Store(ROOT / cfg.library.db)
        self.llm = LLM(cfg, api_key)
        self.kokoro = KokoroClient(cfg)
        self.voice = VoicePipeline(cfg, self.llm, self.kokoro, self.db, self.media_dir,
                                   prompts=_load_prompts(cfg))
        self.songs = SongPipeline(cfg, self.llm, self.media_dir, prompts=_load_prompts(cfg))
        self.news = NewsPipeline(cfg, self.llm, api_key=api_key,
                                prompts=_load_prompts(cfg))
        self.clock = Clock()
        self.rng = RNG(cfg.station.rng_seed)
        self.selector = RandomSelector(self.rng, cfg)
        self.scheduler = Scheduler(cfg, self.db, self.selector, self.clock, self.rng)
        self.producer = Producer(
            cfg, self.db, self.llm, self.kokoro, self.voice, self.songs,
            self.clock, prompts=_load_prompts(cfg), media_dir=self.media_dir,
            api_key=api_key, rng=self.rng, news_pipeline=self.news)
        self._tasks: list = []
        self._http = httpx.AsyncClient(timeout=4.0)

    # ------------------------------------------------------------------ health
    async def backend_status(self) -> dict:
        def ok(status: int) -> bool:
            return 200 <= status < 500
        st = {"litellm": False, "kokoro": False, "mlx": False, "searxng": False}
        try:
            r = await self._http.get(
                self.cfg.hosts.litellm.rstrip("/") + "/models",
                headers={"Authorization": f"Bearer {self.api_key}"})
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
            r = await self._http.get(self.cfg.hosts.searxng.rstrip("/") + "/health")
            st["searxng"] = ok(r.status_code)
        except Exception:
            st["searxng"] = False
        return st

    async def inventory_levels(self) -> dict:
        def c(t: str) -> int:
            return self.db.count_fresh_of_type(t)
        inv = self.cfg.inventory
        return {
            "song": {"have": c("song"), "target": inv.fresh_songs_ready},
            "commercial": {"have": c("commercial"), "target": inv.commercials_min},
            "liner": {"have": c("liner"),
                      "target": inv.liners_per_bucket * len(inv.liner_buckets_s)},
            "dj_talk": {"have": c("dj_talk"), "target": inv.dj_talk_min},
            "news": {"have": c("news"), "target": 1},
        }

    @property
    def on_air(self) -> bool:
        return True  # placeholder; replaced by real check at request time

    async def health(self) -> dict:
        backends = await self.backend_status()
        critical = backends["litellm"] and backends["kokoro"] and backends["mlx"]
        inv = await self.inventory_levels()
        # on-air: critical backends reachable AND we can still cover the window
        return {
            "on_air": bool(critical),
            "backends": backends,
            "inventory": inv,
            "committed_coverage_s": round(self.scheduler.coverage(), 1),
            "committed_count": len(self.scheduler._items),
            "song_realtime": round(self.songs.generation_rate, 2),
            "time": time.time(),
        }

    # ------------------------------------------------------------------ program
    def program_response(self, after_seq: int | None) -> dict:
        scheduler = self.scheduler
        if after_seq is None:
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
            out.append({"seq": it["seq"], "media_id": it["item_id"],
                        "type": it["type"], "duration_s": it["duration_s"]})
        return {"items": out, "start_offset_s": start_offset}

    async def startup(self) -> None:
        # warm the program immediately with whatever inventory exists
        self.scheduler.commit_lookahead()
        self._tasks.append(asyncio.create_task(self.scheduler.run()))
        self._tasks.append(asyncio.create_task(self.producer.run()))
        self._tasks.append(asyncio.create_task(self.producer.news_loop()))
        self._tasks.append(asyncio.create_task(self.producer.song_loop()))
        log.info("station startup complete")


def _load_prompts(cfg: Config) -> dict:
    pdir = ROOT / cfg.library.prompts_dir
    out = {}
    for f in ("voice.md", "song_brief.md", "news.md"):
        p = pdir / f
        if p.exists():
            out[f.replace(".md", "")] = p.read_text()
    return out


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    api_key = os.environ.get("LITELLM_TOKEN", "")
    station = Station(cfg, api_key)
    app = FastAPI(title="Pilgrim Dot Farm Radio", version="0.1.0")
    app.state.station = station
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])

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
    async def program(after_seq: int | None = Query(default=None, ge=0)):
        return station.program_response(after_seq)

    @app.post("/api/station/heartbeat")
    async def heartbeat(payload: dict):
        station.db.record_airplay(
            item_id=int(payload.get("seq", 0) or 0),
            seq=int(payload.get("seq", 0) or 0),
            item_type=payload.get("type"),
            started_at=payload.get("started_at"),
            position=payload.get("position"),
            underrun=1 if payload.get("underrun") else 0)
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
    uvicorn.run(app, host=cfg.station.host, port=cfg.station.port)


if __name__ == "__main__":
    main()

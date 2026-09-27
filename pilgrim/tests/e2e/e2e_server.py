"""E2E server for the Playwright client gap test (RADIO.md §15, M9).

Boots the REAL scheduler (SimClock-free, real Clock) over a temp Store seeded
with SHORT rendered FLAC items generated on the fly (no real backends — AGENTS
§2). Exposes just the endpoints the client needs. Run by the Playwright
webServer so the browser can exercise PLAY -> heartbeats -> gapless joins.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root

import numpy as np
import soundfile as sf
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pilgrim.config import RNG, Clock, Config, ensure_dirs, load_config
from pilgrim.scheduler import Scheduler
from pilgrim.selector import RandomSelector
from pilgrim.store import Store


def _make_flac(dst: Path, seconds: float) -> None:
    sr = 24000
    n = int(sr * seconds)
    t = np.linspace(0, seconds, n, endpoint=False)
    x = (0.25 * np.sin(2 * np.pi * 330 * t)).astype(np.float32)
    sf.write(dst, x, sr, format="FLAC")


def _seed(cfg: Config, store: Store, tmp: Path) -> None:
    lib = tmp / "library"
    lib.mkdir(parents=True, exist_ok=True)
    genres = list(cfg.songs.genres.keys())
    for i in range(30):  # songs
        f = lib / f"song_{i}.flac"
        _make_flac(f, 1.5 + (i % 3) * 0.5)
        store.add_item(type_="song", media_path=str(f), duration_s=1.5 + (i % 3) * 0.5,
                       genre=genres[i % len(genres)], evergreen=True, fresh=True)
    for t, n in (("liner", 20), ("commercial", 20), ("dj_talk", 15)):
        for j in range(n):
            f = lib / f"{t}_{j}.flac"
            _make_flac(f, 2.0 + (j % 3) * 0.5)
            store.add_item(type_=t, media_path=str(f),
                           duration_s=2.0 + (j % 3) * 0.5, evergreen=True, fresh=True)


def _load_cfg() -> tuple[Config, Path]:
    base = load_config()
    tmp = Path(tempfile.mkdtemp(prefix="radio_e2e_"))
    base.library.dir = str(tmp / "library")
    base.library.db = str(tmp / "station.db")
    ensure_dirs(base)
    return base, tmp


def build_app() -> FastAPI:
    cfg, tmp = _load_cfg()
    store = Store(tmp / "station.db")
    _seed(cfg, store, tmp)
    clock = Clock()
    rng = RNG(1)
    sched = Scheduler(cfg, store, RandomSelector(rng, cfg), clock, rng)
    sched.commit_lookahead()

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        task = asyncio.create_task(sched.run())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="pilgrim e2e", lifespan=_lifespan)
    app.state.store = store
    app.state.sched = sched

    @app.post("/api/station/start")
    async def start():
        return {"ok": True}

    @app.post("/api/station/stop")
    async def stop():
        return {"ok": True}

    @app.get("/api/station/program")
    async def program(after_seq: int | None = None):
        sched.commit_lookahead()
        if after_seq is None:
            seq, offset = sched.on_air()
            since = seq if seq is not None else store.max_seq()
            items = store.program_since(since if since else 1)
            start_offset = offset
        else:
            items = store.program_after(after_seq)
            start_offset = 0.0
        return {"items": [{"seq": it["seq"], "media_id": it["item_id"],
                           "type": it["type"], "duration_s": it["duration_s"]}
                          for it in items], "start_offset_s": start_offset}

    @app.post("/api/station/heartbeat")
    async def heartbeat(payload: dict):
        store.record_airplay(
            item_id=int(payload.get("seq", 0) or 0),
            seq=int(payload.get("seq", 0) or 0),
            item_type=payload.get("type"),
            started_at=payload.get("started_at"),
            position=payload.get("position"),
            underrun=1 if payload.get("underrun") else 0)
        return {"ok": True}

    @app.get("/api/media/{item_id}")
    async def media(item_id: int):
        it = store.get_item(item_id)
        if not it:
            return {"error": "no such item"}
        p = Path(it["media_path"])
        return FileResponse(str(p), media_type="audio/flac")

    @app.get("/api/health")
    async def health():
        return {"on_air": True, "committed_coverage_s": sched.coverage(),
                "inventory": {}, "time": time.time()}

    @app.get("/api/test/report")
    async def report():
        """Test-only: airplay stats for the gap assertion (§15)."""
        rows = store.recent_airplay(500)
        # Heartbeats include periodic updates for the same clip; report starts
        # in chronological order, collapsing only consecutive duplicates.
        seqs: list[int] = []
        for row in reversed(rows):
            if row["item_type"] and (not seqs or row["seq"] != seqs[-1]):
                seqs.append(row["seq"])
        underrun = sum(1 for r in rows if r["underrun"])
        return {"seqs": seqs, "underrun": underrun,
                "coverage_s": sched.coverage()}

    web_dir = Path(__file__).resolve().parents[2] / "web"
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    return app


app = build_app()

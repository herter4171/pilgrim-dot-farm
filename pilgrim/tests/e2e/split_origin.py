"""Real-origin runner for the local split smoke checks (SPLIT_PLAN §7.3).

`e2e_server.py` reimplements the endpoints in its own app, so it cannot prove
anything about `server.py`. This runner serves the REAL `create_public_app`
(Step 1 route split, Step 2 cache headers, Step 3 trusted-proxy chain with
`proxy_headers=False`) over a temp library:

- every backend host points at a closed loopback port, and the moderation LLM
  is the in-process `FakeLLM`: no real backend is ever contacted (AGENTS §1.2);
- only the scheduler runs (no producer/song/news/SFX loops);
- `<state-dir>/station.db` is reused on restart (smoke check 9b), and the smoke
  script reads visitor IPs straight from it: there is no test route on the
  public app.

    python -m pilgrim.tests.e2e.split_origin --port N --state-dir DIR
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pilgrim.config import Config, Visitors, ensure_dirs, load_config
from pilgrim.server import (
    _add_api_404,
    _add_public_routes,
    _new_app,
    _public_health,
    build_station,
    create_admin_app,
    listener_configs,
)
from pilgrim.store import Store
from pilgrim.tests.e2e.e2e_server import _seed
from pilgrim.tests.fakes import FakeLLM

CLOSED = "http://127.0.0.1:9"  # discard port: nothing listens, calls fail fast


def _config(state: Path, port: int) -> Config:
    cfg = load_config().model_copy(deep=True)
    cfg.library.dir = str(state / "library")
    cfg.library.db = str(state / "station.db")
    cfg.logging.dir = str(state / "logs")
    for name in type(cfg.hosts).model_fields:
        setattr(cfg.hosts, name, CLOSED)
    # Payments went live in config.yaml and the repo .env holds live keys:
    # never let the smoke origin reach Stripe (checks 11-15 expect closed).
    cfg.payments.enabled = False
    cfg.payments.api_base = CLOSED
    cfg.station.host = "127.0.0.1"
    cfg.station.port = port
    cfg.station.admin_port = port + 1 if port < 65535 else port - 1  # unused
    cfg.station.serve_web = False      # the edge serves the site
    cfg.visitors = Visitors(trusted_proxies=["127.0.0.1", "::1"])  # the gateway
    # Cover art (ALBUM_ART Step 8): the fixture serves the seeded PNGs itself
    # through a test-only /api/art route, reached via the edge (which proxies
    # /api/*), so the client's art_url path is exercised in split mode too.
    # Unset -> production-shaped run: no art_url, the client shows the
    # placeholder. The FLUX worker never runs in the fixture.
    cfg.art.enabled = False
    art_base = os.environ.get("PILGRIM_E2E_ART_BASE", "").strip()
    if art_base:
        cfg.media.public_base_url = art_base.rstrip("/")
    # 2 s clips: scale spacing like e2e_server so 30 songs aren't a drought
    cfg.playout.song_min_spacing_s = [30, 20, 10]
    ensure_dirs(cfg)
    return cfg


def _fixture_public_app(cfg: Config, station):
    """The real public app, plus a test-only cover-art route.

    Built from the same pieces as `create_public_app`, in the same order
    (`_add_public_routes`, then the art route, then the /api 404 catch-all),
    so route precedence matches production. With PILGRIM_E2E_ART_BASE set the
    seeded songs are marked published under `api/art/<id>`, which the real
    `art_url` turns into `<edge>/api/art/<id>`.
    """
    app = _new_app("Pilgrim Dot Farm Radio (e2e split origin)")
    app.state.station = station
    _add_public_routes(app, station, _public_health)

    if cfg.media.public_base_url and os.environ.get("PILGRIM_E2E_ART_BASE"):
        for it in station.db.list_items("song"):
            if it.get("art_path") and not it.get("art_s3_key"):
                station.db.set_art_published(int(it["id"]), f"api/art/{it['id']}")

        @app.get("/api/art/{item_id}")
        async def art(item_id: int):
            it = station.db.get_item(item_id)
            if not it or not it.get("art_path"):
                raise HTTPException(404, "no art")
            return FileResponse(str(it["art_path"]), media_type="image/png",
                                headers={"Cache-Control":
                                         "public, max-age=31536000, immutable"})

    _add_api_404(app)
    return app


async def _serve(cfg: Config, fresh: bool) -> None:
    import uvicorn

    if fresh:
        _seed(cfg, Store(Path(cfg.library.db)), Path(cfg.library.dir).parent)
    station = build_station(cfg)
    station.llm = FakeLLM()  # type: ignore[assignment]
    station.moderator.llm = station.llm  # type: ignore[assignment]
    station.override_token = ""         # no operator comps in the smoke origin
    public = _fixture_public_app(cfg, station)
    station.scheduler.commit_lookahead()
    task = asyncio.create_task(station.scheduler.run())
    # Production listener settings (proxy_headers=False), public listener only.
    config = listener_configs(cfg, public, create_admin_app(cfg, station))[0]
    try:
        await uvicorn.Server(config).serve()
    finally:
        task.cancel()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--state-dir", type=Path, required=True)
    args = ap.parse_args(argv)
    state = args.state_dir.resolve()
    state.mkdir(parents=True, exist_ok=True)
    fresh = not (state / "station.db").exists()
    asyncio.run(_serve(_config(state, args.port), fresh))


if __name__ == "__main__":
    main()

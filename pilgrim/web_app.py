"""The Fargate web tier (SCALABILITY.md Step 5; RADIO §11 "Web tier").

`python -m pilgrim.web_app` serves the static site and the public API from
Valkey and Postgres only. It never reaches the Mac: the programme, state,
health and request stages come from the hot-state keys the station publishes
(RADIO §14), the request board, votes, ratings, visitors and payments from
Postgres, and heartbeats go back into Valkey as `player:<id>` keys plus an
`airplay` row per heartbeat (operator decision: 5 s precision).

It reuses `server._add_public_routes` unchanged, so every public route keeps
the RADIO §11 shape; `WebStation` is the object those routes call, backed by
the cache instead of a scheduler. What the EC2 edge used to do moves here:
off-air bodies when the station's `alive` key is missing (503
`{"off_air": true, "detail": "station is off the air"}`; the public
`/api/health` answers 200 `{"on_air": false, "off_air": true}` so the ALB
never drains healthy tasks over an origin outage), admin paths 404, media
404 (everything a listener plays is on CloudFront; RADIO §8.4), and the
client IP from `CF-Connecting-IP` behind the ALB (RADIO §14).

Required environment: `REDIS_URL` (the station's hot state) and, in
production, `DATABASE_URL` + `DB_PASS_SECRET_ARN` (RADIO §12). Optional:
`LITELLM_TOKEN` (moderation, paid summaries), `BRAVE_SEARCH_KEY` (copied-lyrics
screen), the Stripe keys and `PAID_OVERRIDE_TOKEN` (PAYMENT.md); any of those
may instead arrive through `STRIPE_SECRET_ARN`, a Secrets Manager JSON whose
allow-listed keys are exported into the environment at start
(`pilgrim/aws_secrets.py`).
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pilgrim import dj_contracts as dj
from pilgrim.aws_secrets import STRIPE_SECRET_ARN_ENV, export_secret_env
from pilgrim.config import Clock, Config, load_api_key, load_brave_key, load_config, load_secret
from pilgrim.db import env_summary as db_env_summary
from pilgrim.db import open_store
from pilgrim.hotstate import REDIS_URL_ENV, HotState
from pilgrim.hotstate import connect as cache_connect
from pilgrim.logging_setup import setup_logging
from pilgrim.pipelines.brave import BraveSearch
from pilgrim.pipelines.copyright import CopyrightScreen
from pilgrim.pipelines.llm import LLM
from pilgrim.pipelines.moderation import Moderation
from pilgrim.server import (
    PublicSurface,
    _add_api_404,
    _add_public_routes,
    _load_prompts,
    _mount_web,
    _new_app,
)
from pilgrim.store import Store

log = logging.getLogger("radio.web")

OFF_AIR_BODY = {"off_air": True, "detail": "station is off the air"}
OFF_AIR_HEALTH = {"on_air": False, "off_air": True}


class OffAir(Exception):
    """The station's `alive`/document key is missing or expired."""


class CachedScheduler:
    """The slice of `Scheduler` that `voting.rating_window` reads, rebuilt from
    the published `state` and `program` documents. `on_air()` is the station's
    snapshot (up to one publish tick old), which the rating grace already
    tolerates (VOTING.md V8)."""

    def __init__(self, state: dict[str, Any], program: dict[str, Any]) -> None:
        self.epoch: str = str(state.get("epoch") or "")
        self.revision: int = int(state.get("revision") or 0)
        self.frozen: bool = bool(state.get("idle"))
        self._items: list[dict[str, Any]] = [
            {"seq": int(it["seq"]), "type": it.get("type"),
             "item_id": int(it.get("media_id") or 0),
             "duration_s": float(it.get("duration_s") or 0.0)}
            for it in program.get("items", [])]
        seq = state.get("current_seq")
        self._on_air: tuple[int | None, float] = (
            int(seq) if seq is not None else None,
            float(state.get("current_offset_s") or 0.0))

    def on_air(self) -> tuple[int | None, float]:
        return self._on_air


class _Stages:
    """`station.producer.request_stage` for the board, read from the cache."""

    def __init__(self, hot: HotState) -> None:
        self._hot = hot

    @property
    def request_stage(self) -> dict[int, str]:
        doc = self._hot.read("stages")
        if not isinstance(doc, dict):
            return {}
        out: dict[int, str] = {}
        for k, v in doc.items():
            try:
                out[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
        return out


class WebStation(PublicSurface):
    """What `_add_public_routes` calls, backed by Valkey + Postgres."""

    def __init__(self, cfg: Config, store: Store, hot: HotState, api_key: str) -> None:
        self.cfg = cfg
        self.db = store
        self.hot = hot
        self.clock = Clock()
        self.llm = LLM(cfg, api_key)
        self.prompts = _load_prompts(cfg)
        self.moderator = Moderation(cfg, self.llm, prompts=self.prompts)
        self.search = BraveSearch(cfg, load_brave_key())
        self.copyright = CopyrightScreen(cfg, self.search)
        self.producer = _Stages(hot)
        self._setup_payments(cfg)

    # ------------------------------------------------------------ documents
    def _doc(self, name: str) -> dict[str, Any]:
        doc = self.hot.read(name)
        if not isinstance(doc, dict):
            raise OffAir(name)
        return doc

    def health(self) -> dict[str, Any]:
        """Public health: the station's published body, or the off-air body.
        Always 200 (the ALB health check must not depend on the station)."""
        try:
            return self._doc("health")
        except OffAir:
            return dict(OFF_AIR_HEALTH)
        except Exception as e:  # noqa: BLE001 - a cache outage reads as off air
            log.warning("web.health_cache_failed: %s", e)
            return dict(OFF_AIR_HEALTH)

    @property
    def on_air(self) -> bool:
        return bool(self.health().get("on_air"))

    @property
    def scheduler(self) -> CachedScheduler:  # type: ignore[override]  # fresh per request
        return CachedScheduler(self._doc("state"), self._doc("program"))

    # ------------------------------------------------------------ programme
    def wake(self, reason: str) -> None:
        """IDLE.md through the cache: a frozen station gets the `wake` key and
        thaws on its next tick (RADIO §3). Not frozen: nothing to write."""
        try:
            state = self.hot.read("state")
            if isinstance(state, dict) and state.get("idle"):
                self.hot.set_wake(reason)
        except Exception as e:  # noqa: BLE001 - a failed wake must not fail the fetch
            log.warning("web.wake_failed: %s", e)

    def program_response(self, after_seq: int | None,
                         client_epoch: str | None = None,
                         client_revision: int | None = None) -> dict[str, Any]:
        """RADIO §11 semantics over the published full-window snapshot: no
        cursor or a stale one returns the whole window with `reset`; a live
        cursor returns only rows after it, from offset 0."""
        doc = self._doc("program")
        stale = ((client_epoch is not None and client_epoch != doc.get("epoch"))
                 or (client_revision is not None
                     and client_revision != doc.get("revision")))
        if after_seq is None or stale:
            return {**doc, "reset": stale, "server_time": time.time()}
        items = [it for it in doc.get("items", []) if int(it["seq"]) > after_seq]
        return {**doc, "items": items, "start_offset_s": 0.0, "reset": False,
                "server_time": time.time()}

    def public_state(self) -> dict[str, Any]:
        return {**self._doc("state"), "server_time": time.time()}

    # ------------------------------------------------------------ listeners
    def touch_listener(self, player_id: str | None) -> None:
        """A heartbeat: write the player key (the station counts it on its next
        tick) and wake a frozen station. A cache failure is logged; the
        heartbeat still answers ok and its airplay row is still written."""
        if not player_id or len(player_id) > 64:
            return
        try:
            self.hot.write_player(player_id, {})
        except Exception as e:  # noqa: BLE001
            log.warning("web.heartbeat_cache_failed: %s", e)
        self.wake("heartbeat")

    def record_player_telemetry(self, player_id: str | None,
                                payload: dj.HeartbeatIn) -> None:
        if not player_id or len(player_id) > 64:
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
        try:
            self.hot.write_player(player_id, tel)
        except Exception as e:  # noqa: BLE001
            log.warning("web.telemetry_cache_failed: %s", e)


# ------------------------------------------------------------------- app
async def _web_health(station: Any) -> dict[str, Any]:
    return station.health()


def create_web_app(cfg: Config, station: WebStation) -> FastAPI:
    """The public surface over the cache + Postgres, plus the static site."""
    app = _new_app("Pilgrim Dot Farm Radio (web)")
    app.state.station = station

    @app.exception_handler(OffAir)
    async def _off_air(request: Request, exc: OffAir) -> JSONResponse:
        return JSONResponse(status_code=503, content=dict(OFF_AIR_BODY),
                            headers={"Cache-Control": "no-store"})

    _add_public_routes(app, station, _web_health)
    _add_api_404(app)
    if cfg.station.serve_web:
        _mount_web(app, cfg)
    return app


def build_web_station(cfg: Config) -> WebStation:
    """Open the cache and the store from the environment (RADIO §12)."""
    setup_logging(cfg)
    url = load_secret(REDIS_URL_ENV)
    if not url:
        log.error("web.no_redis_url", extra={"env": REDIS_URL_ENV})
        raise SystemExit(f"{REDIS_URL_ENV} is required: the web tier reads the "
                         "station's hot state from Valkey (RADIO §14)")
    # The task carries ARNs, not values: STRIPE_SECRET_ARN is a JSON secret with
    # the three Stripe keys (RADIO §12). Exported before anything reads them.
    export_secret_env(STRIPE_SECRET_ARN_ENV, cfg.media.aws_profile)
    store = open_store(cfg)
    hot = HotState(cfg, cache_connect(url, cfg), epoch="web")
    api_key = load_api_key()
    if not api_key:
        log.warning("LITELLM_TOKEN not set; request moderation and paid summaries will fail")
    station = WebStation(cfg, store, hot, api_key)
    log.info("web.ready", extra={"db": db_env_summary(), "serve_web": cfg.station.serve_web,
                                 "payments": station.payments_active})
    return station


def main() -> None:
    import uvicorn

    cfg = load_config()
    station = build_web_station(cfg)
    app = create_web_app(cfg, station)
    # proxy_headers=False: the trusted-proxy decision is ours (RADIO §14),
    # never uvicorn's. log_config=None: lines go to the JSON root logger.
    uvicorn.run(app, host=cfg.station.host, port=cfg.station.port,
                log_config=None, proxy_headers=False)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code and not isinstance(e.code, int):
            print(e.code, file=sys.stderr)
            raise SystemExit(2) from None
        raise

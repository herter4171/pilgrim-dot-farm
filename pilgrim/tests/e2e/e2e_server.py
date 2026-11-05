"""E2E server for the Playwright client gap test (RADIO.md §15, M9).

Boots the REAL scheduler (SimClock-free, real Clock) over a temp Store seeded
with SHORT rendered MP3 items (the delivery format, RADIO §8.2) generated on
the fly (no real backends — AGENTS §2). Exposes just the endpoints the client
needs. Run by the Playwright
webServer so the browser can exercise PLAY -> heartbeats -> gapless joins.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root

import numpy as np
import soundfile as sf
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pilgrim import voting
from pilgrim.audio.normalize import encode_mp3
from pilgrim.config import RNG, Clock, Config, ensure_dirs, load_config
from pilgrim.scheduler import Scheduler
from pilgrim.selector import RandomSelector
from pilgrim.store import Store
from pilgrim.tests.fakes.fake_flux import encode_png

_CFG = load_config()


def _make_audio(dst: Path, seconds: float) -> None:
    """A tone encoded exactly like production delivery (48 kHz stereo MP3 with
    a gapless header), so the gap test exercises the browser's MP3 trimming."""
    sr = 24000
    n = int(sr * seconds)
    t = np.linspace(0, seconds, n, endpoint=False)
    x = (0.25 * np.sin(2 * np.pi * 330 * t)).astype(np.float32)
    src = dst.with_suffix(".src.flac")
    sf.write(src, x, sr, format="FLAC")
    encode_mp3(src, dst, _CFG, _CFG.audio.mp3_mono_source_kbps)
    src.unlink()


def _seed(cfg: Config, store: Store, tmp: Path) -> None:
    lib = tmp / "library"
    lib.mkdir(parents=True, exist_ok=True)
    genres = list(cfg.songs.genres.keys())
    for i in range(30):  # songs
        f = lib / f"song_{i}.mp3"
        _make_audio(f, 1.5 + (i % 3) * 0.5)
        sid = store.add_item(type_="song", media_path=str(f), duration_s=1.5 + (i % 3) * 0.5,
                             genre=genres[i % len(genres)], evergreen=True, fresh=True,
                             title=f"Song {i}", artist="The Fakes")
        # cover art (ALBUM_ART.md Step 8): every song has one, served locally
        art = lib / f"art_{i}.png"
        art.write_bytes(encode_png(48, 48, color=(20 + i, 140, 90)))
        store.set_art(sid, str(art), {"scene": "e2e", "seed": i})
    # one short SFX stinger that rides on talk clips (SFX.md §7.4): exercises
    # the client's overlay path without touching the host joins
    sfx = lib / "sfx_0.mp3"
    _make_audio(sfx, 0.5)
    sfx_id = store.add_item(type_="sfx", media_path=str(sfx), duration_s=0.5,
                            meta={"cue": "cow"})
    cue = {"item_id": sfx_id, "cue": "cow", "kind": "stinger", "offset_s": 0.6,
           "duration_s": 0.5}
    for t, n in (("liner", 20), ("commercial", 20), ("dj_talk", 15)):
        for j in range(n):
            f = lib / f"{t}_{j}.mp3"
            _make_audio(f, 2.0 + (j % 3) * 0.5)
            store.add_item(type_=t, media_path=str(f),
                           duration_s=2.0 + (j % 3) * 0.5, evergreen=True, fresh=True,
                           meta={"sfx": [cue]} if j % 2 == 0 else None)


def _load_cfg() -> tuple[Config, Path]:
    base = load_config()
    tmp = Path(tempfile.mkdtemp(prefix="radio_e2e_"))
    base.library.dir = str(tmp / "library")
    base.library.db = str(tmp / "station.db")
    # 2 s clips: scale the song spacing floors to match, so 30 songs behave like
    # a stocked library, not a song drought (which only bridges ~60 s, §5.2)
    base.playout.song_min_spacing_s = [30, 20, 10]
    ensure_dirs(base)
    return base, tmp


def _seed_requests(cfg: Config, store: Store) -> None:
    """VOTING.md Step 7: a real queue board — 12 free requests plus one paid
    priority row (paid rows are not votable, V5) — and 5 aired requests so the
    AIRED REQUESTS table keeps its prep-time coverage (cosmetic.spec.ts)."""
    # Aired first (low ids) so a later airing of a queue row still lands in
    # the board's "5 most recent"; k=1 stays unmeasured (an em dash).
    song_ids = [i["id"] for i in store.list_items("song")][:5]
    base = 1_800_000_000.0
    for k in range(1, 6):
        r = store.add_request(f"played {k}", cap=None)
        store.mark_request_ready(r["id"], song_ids[k - 1])
        store.mark_request_aired(r["id"])
        if k >= 2:
            start = datetime.fromtimestamp(base + k * 60, tz=UTC).isoformat()
            end = datetime.fromtimestamp(base + k * 60 + k * 900, tz=UTC).isoformat()
            with store._lock:
                store._conn.execute(
                    "UPDATE requests SET prep_started_at=?, aired_at=? WHERE id=?",
                    (start, end, r["id"]))
                store._conn.commit()
    for i in range(1, 13):
        store.add_request(f"e2e free request {i}", cap=None)
    paid = store.add_paid_request("e2e paid request")
    store.attach_checkout(paid["id"], f"cs_e2e_{paid['id']}")
    store.mark_request_paid(
        paid["id"], session_id=f"cs_e2e_{paid['id']}",
        payment_intent_id="pi_e2e", amount_cents=350, currency="usd",
        livemode=False)


def build_app() -> FastAPI:
    cfg, tmp = _load_cfg()
    store = Store(tmp / "station.db")
    _seed(cfg, store, tmp)
    _seed_requests(cfg, store)
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
    async def program(after_seq: int | None = None, epoch: str | None = None,
                      revision: int | None = None):
        sched.commit_lookahead()
        stale = ((epoch is not None and epoch != sched.epoch)
                 or (revision is not None and revision != sched.revision))
        if after_seq is None or stale:
            seq, offset = sched.on_air()
            since = seq if seq is not None else store.max_seq()
            items = store.program_since(since if since else 1)
            start_offset = offset
        else:
            items = store.program_after(after_seq)
            start_offset = 0.0
        return JSONResponse({"items": [{"seq": it["seq"], "media_id": it["item_id"],
                           "type": it["type"], "duration_s": it["duration_s"],
                           "sfx": sched.overlays_for(it["seq"]),
                           "title": (store.get_item(it["item_id"]) or {}).get("title") or "",
                           "artist": (store.get_item(it["item_id"]) or {}).get("artist") or "",
                           "art_url": _art_url(store, it["item_id"])}
                          for it in items], "start_offset_s": start_offset,
                "epoch": sched.epoch, "revision": sched.revision,
                "reset": stale, "server_time": time.time()},
                headers={"Cache-Control": "public, max-age=1"})

    @app.get("/api/station/state")
    async def state():
        seq, offset = sched.on_air()
        row = next((r for r in sched._items if r["seq"] == seq), None)
        return JSONResponse({"epoch": sched.epoch, "revision": sched.revision,
                "server_time": time.time(), "current_seq": seq,
                "current_type": row["type"] if row else None,
                "current_offset_s": offset, "prepared_cutover": None,
                "listeners": 0, "idle": sched.frozen,
                "rating": _rating_state()},
                headers={"Cache-Control": "public, max-age=1"})

    def _rating_state():
        """e2e mirror of Station._public_rating (VOTING.md V10)."""
        if not cfg.ratings.enabled:
            return None
        seq, _ = sched.on_air()
        open_, item_id = voting.rating_window(
            sched, sched.epoch, int(seq) if seq is not None else -1,
            cfg.ratings.grace_s)
        if open_ and item_id is not None:
            agg = store.song_rating(item_id)
            return {"open": True, "count": agg["count"], "avg": agg["avg"]}
        return {"open": False, "count": 0, "avg": None}

    @app.post("/api/station/heartbeat")
    async def heartbeat(payload: dict):
        store.record_airplay(
            item_id=int(payload.get("seq", 0) or 0),
            seq=int(payload.get("seq", 0) or 0),
            item_type=payload.get("type"),
            started_at=payload.get("started_at"),
            position=payload.get("position"),
            underrun=1 if payload.get("underrun") else 0)
        return JSONResponse({"ok": True},
                            headers={"Cache-Control": "no-store"})

    def _art_url(store: Store, item_id: int) -> str:
        """e2e mirror of art_url (ALBUM_ART Step 7): the local art route when
        the item has a rendered cover, else ""."""
        it = store.get_item(item_id) or {}
        return f"/api/art/{item_id}" if it.get("art_path") else ""

    @app.get("/api/art/{item_id}")
    async def art(item_id: int):
        it = store.get_item(item_id)
        if not it or not it.get("art_path"):
            return JSONResponse({"detail": "no art"}, status_code=404)
        return FileResponse(str(it["art_path"]), media_type="image/png",
                            headers={"Cache-Control":
                                     "public, max-age=31536000, immutable"})

    @app.get("/api/media/{item_id}")
    async def media(item_id: int):
        it = store.get_item(item_id)
        if not it:
            return JSONResponse({"detail": "no such item"}, status_code=404,
                                headers={"Cache-Control": "public, max-age=30"})
        p = Path(it["media_path"])
        return FileResponse(str(p), media_type="audio/mpeg",
                            headers={"Cache-Control":
                                     "public, max-age=31536000, immutable"})

    @app.get("/api/health")
    async def health():
        return JSONResponse({"on_air": True,
                "committed_coverage_s": sched.coverage(),
                "inventory": {}, "time": time.time()},
                headers={"Cache-Control": "public, max-age=2"})

    @app.post("/api/visitors")
    async def visitors(request: Request):
        """e2e mirror of RADIO §14: store the server-derived peer IP and
        return the unique count (current count when no identity), plus the
        HIT COUNTER request counts (RADIO §9.1)."""
        peer = request.client.host if (request.client and request.client.host) else None
        n = store.register_visitor(peer) if peer else store.unique_visitors()
        paid, total = store.request_counts()
        return JSONResponse({"unique_visitors": n, "paid_requests": paid,
                             "total_requests": total},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/requests")
    async def list_requests():
        board = store.request_board(
            cfg.requests.queue_cap, cfg.requests.max_length,
            voting_enabled=cfg.voting.enabled,
            bury_threshold=cfg.voting.bury_threshold)
        return JSONResponse({**board, "cap": cfg.requests.queue_cap,
                             "paid_enabled": False,
                             "voting_enabled": cfg.voting.enabled},
                            headers={"Cache-Control": "public, max-age=2"})

    @app.post("/api/requests")
    async def submit_request(payload: dict):
        store.add_request(str(payload.get("text") or "e2e request"), cap=None)
        board = store.request_board(
            cfg.requests.queue_cap, cfg.requests.max_length,
            voting_enabled=cfg.voting.enabled,
            bury_threshold=cfg.voting.bury_threshold)
        return JSONResponse({"ok": True, "rejected": False, **board,
                "cap": cfg.requests.queue_cap, "paid_enabled": False,
                "voting_enabled": cfg.voting.enabled},
                headers={"Cache-Control": "no-store"})

    @app.post("/api/requests/{request_id}/vote")
    async def vote(request_id: int, payload: voting.VoteIn):
        status, body = voting.cast_vote(
            store, cfg, request_id, payload.voter_id, payload.value, None)
        return JSONResponse(status_code=status, content=body,
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/station/rating")
    async def rate(payload: voting.RatingIn):
        status, body = voting.cast_rating(store, cfg, sched, payload, None)
        return JSONResponse(status_code=status, content=body,
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/test/air/{request_id}")
    async def test_air(request_id: int):
        """Test-only: mark a request aired so the spec can watch the row lose
        its vote buttons on the next board poll (VOTING.md V4)."""
        store.mark_request_aired(request_id)
        return {"ok": True}

    @app.post("/api/test/splice")
    async def splice():
        """Test-only: submit a rendered request and let the scheduler splice it
        into the next song slot (revision-aware client reset, TUI.md §5)."""
        lib = tmp / "library"
        song_f = lib / "req_song.mp3"
        intro_f = lib / "req_intro.mp3"
        # 2.2 s: no stock clip has this length, so the request song's byte size
        # is unique (the dead-air spec hangs the decode of exactly these bytes)
        _make_audio(song_f, 2.2)
        _make_audio(intro_f, 0.8)
        req = store.add_request("e2e request", cap=10)
        song = store.add_item(type_="song", media_path=str(song_f), duration_s=2.2,
                              fresh=True, title="Request Song", artist="Listener")
        intro = store.add_item(type_="intro", media_path=str(intro_f), duration_s=0.8,
                               evergreen=False, fresh=True,
                               meta={"song_item_id": song, "request_id": req["id"]})
        store.mark_request_ready(req["id"], song, intro)
        sched.commit_lookahead()
        song_seq = next((r["seq"] for r in sched._items if r["item_id"] == song), None)
        return {"ok": True, "revision": sched.revision,
                "song_id": song, "song_seq": song_seq}

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
        # SPLIT_PLAN §7.3 check 8: the origin-side canonical visitor IPs, so
        # the smoke test can prove a spoofed X-Real-IP was discarded.
        ips = [r[0] for r in store._conn.execute(
            "SELECT ip FROM visitor_ips ORDER BY ip").fetchall()]
        return {"seqs": seqs, "underrun": underrun,
                "coverage_s": sched.coverage(),
                "committed_lookahead_s": sched.cfg.playout.committed_lookahead_s,
                "refill_interval_s": sched.REFILL_INTERVAL_S,
                "visitor_ips": ips}

    web_dir = Path(__file__).resolve().parents[2] / "web"
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    return app


app = build_app()

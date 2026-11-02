"""Cover art per song (ALBUM_ART.md Steps 4-5; RADIO §8.4, §14).

Storage half (Step 4): where the master PNG and the delivered JPEG live, and
how they are written without ever touching an existing library file
(hard rule 5). The worker half (Step 5) is `ArtWorker` below.

The FLUX server is single-flight and blocks while it renders, so everything
here is one-at-a-time, with a memory gate and a back-off. Nothing in playout
imports this module (hard rule 1): a song without art airs exactly as before.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from pilgrim.config import ROOT, Config
from pilgrim.pipelines.art import (
    ARTIST_CAP,
    TITLE_CAP,
    ArtError,
    FluxBusy,
    FluxClient,
    FluxTimeout,
    SceneWriter,
    build_flux_prompt,
    lyric_excerpt,
    pick_typography,
    sanitize_text,
    style_hint,
)

log = logging.getLogger("radio.art")

ART_SUBDIR = "art"          # pilgrim/library/art/ (gitignored via library/)
PNG_SUFFIX = ".png"
JPEG_SUFFIX = ".jpg"
ART_CONTENT_TYPE = "image/jpeg"
ART_CACHE_CONTROL = "public, max-age=31536000, immutable"
RETRY_AFTER_S = 300.0       # matches media_publish's failed-item back-off


# --------------------------------------------------------------------------- #
# Files (Step 4)
# --------------------------------------------------------------------------- #
def art_dir(cfg: Config) -> Path:
    d = ROOT / cfg.library.dir / ART_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """`<stem><suffix>`, else `<stem>-2<suffix>`, `-3`, … Never overwrite an
    existing library file (hard rule 5)."""
    p = directory / f"{stem}{suffix}"
    n = 2
    while p.exists():
        p = directory / f"{stem}-{n}{suffix}"
        n += 1
    return p


def _ffmpeg() -> str:
    return "ffmpeg"


def encode_jpeg(png_path: Path, jpg_path: Path, quality: int) -> None:
    """PNG -> JPEG with the ffmpeg CLI (no new dependency). Writes a temp file
    and renames it into place, so a half-written JPEG never looks finished."""
    # keep the .jpg extension so ffmpeg can pick the muxer, but a distinct name
    tmp = jpg_path.with_name(jpg_path.stem + ".tmp" + jpg_path.suffix)
    cmd = [_ffmpeg(), "-y", "-loglevel", "error", "-i", str(png_path),
           "-q:v", str(quality), str(tmp)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if p.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise ArtError(f"ffmpeg PNG->JPEG failed: {p.stderr.strip()[:300]}")
    os.replace(tmp, jpg_path)


def save_art(cfg: Config, png_bytes: bytes, stem: str) -> tuple[Path, Path]:
    """Write the master PNG and the delivered JPEG as new files. Returns
    (png_path, jpg_path). Raises ArtError on any failure."""
    d = art_dir(cfg)
    png_path = unique_path(d, stem, PNG_SUFFIX)
    png_tmp = png_path.with_name(png_path.name + ".tmp")
    png_tmp.write_bytes(png_bytes)
    os.replace(png_tmp, png_path)
    try:
        jpg_path = unique_path(d, stem, JPEG_SUFFIX)
        encode_jpeg(png_path, jpg_path, cfg.art.jpeg_quality)
    except Exception:
        # keep the PNG master (it is a new file, never an overwrite) and let
        # the caller record the failure; a retry will add a -2 pair.
        raise
    return png_path, jpg_path


def art_stem(item: dict[str, Any]) -> str:
    return Path(str(item["media_path"])).stem


def art_seed(item_id: int) -> int:
    """Deterministic seed so a re-render of the same item is reproducible."""
    return (int(item_id) * 7919) % (2 ** 31)


def scene_texts(item: dict[str, Any]) -> tuple[str, str]:
    """The two art-text strings after sanitising (DB values never change)."""
    return (sanitize_text(str(item.get("title") or ""), TITLE_CAP),
            sanitize_text(str(item.get("artist") or ""), ARTIST_CAP))


# --------------------------------------------------------------------------- #
# Memory gate (ALBUM_ART.md §0.2 rule 3): the same vm_stat formula the FLUX
# server uses, so we skip before it refuses.
# --------------------------------------------------------------------------- #
def available_gb() -> float | None:
    """Free + inactive + compressor pages x page size. None when unmeasurable
    (not macOS, or vm_stat unavailable): an unmeasurable gate never blocks."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True,
                             timeout=10).stdout
        m = re.search(r"page size of (\d+)", out)
        if m is None:
            return None
        psize = int(m.group(1))

        def pages(key: str) -> int:
            m = re.search(re.escape(key) + r":\s+(\d+)", out)
            return int(m.group(1)) if m else 0

        total = (pages("Pages free") + pages("Pages inactive")
                 + pages("Pages occupied by compressor")) * psize
        return total / 1e9
    except Exception:  # noqa: BLE001 - unmeasurable -> never block
        return None


LEASE_KEY = "art_worker_lease"   # ISO heartbeat; backfill refuses a fresh lease
LEASE_STALE_S = 120.0


# --------------------------------------------------------------------------- #
# Worker (Step 5)
# --------------------------------------------------------------------------- #
class ArtWorker:
    """One background task: pick a song, write a scene, render, encode, record.

    Station-wide exactly one render is in flight (it is the only caller), and
    every failure is a back-off, never an exception out of `run()`. Playout
    never waits on this (hard rule 1).
    """

    def __init__(self, cfg: Config, db: Any, llm: Any, prompts: dict[str, str],
                 flux: FluxClient | None = None,
                 free_gb: Any = None) -> None:
        self.cfg = cfg
        self.db = db
        self.flux = flux or FluxClient(cfg)
        self.scene = SceneWriter(cfg, llm, prompts.get("album_art_scene", ""))
        self.template = prompts.get("album_art_flux", "")
        self._free_gb = free_gb or available_gb
        self.backend = "down"       # cached: ok | down | busy (public health reads it)
        self.rendered = 0
        self.failed = 0
        # One FLUX render station-wide: the early producer path and the
        # background worker share this lock.
        self._render_lock = asyncio.Lock()
        # Item ids whose cover is being rendered early (producer path); the
        # background worker skips them so it does not duplicate the work.
        self.pending_early: set[int] = set()

    # -- health ------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """The worker's cached view (never probes the server inline)."""
        return {"backend": self.backend}

    # -- one song ----------------------------------------------------------
    async def _render_one(self, item: dict[str, Any], seed: int,
                          item_id: int | None = None,
                          request_text: str | None = None,
                          paid: bool = False
                          ) -> tuple[Path, Path, dict[str, Any]] | None:
        """Scene -> FLUX -> files. Returns None when the memory gate says no.
        Raises on any real failure; the caller decides whether to back off."""
        iid = int(item["id"]) if item_id is None else item_id
        title, artist = scene_texts(item)
        try:
            free = self._free_gb()
        except Exception:  # noqa: BLE001
            free = None
        if free is not None and free < self.cfg.art.min_free_gb:
            log.info("art.low_memory", extra={
                "item_id": iid, "stage": "render",
                "free_gb": round(free, 1), "min_free_gb": self.cfg.art.min_free_gb})
            return None

        t0 = time.monotonic()
        scene = await self.scene.write(
            title=title, artist=artist, genre=item.get("genre"),
            hint=style_hint(_meta(item)), lyrics=lyric_excerpt(_meta(item)),
            item_id=iid, request_text=request_text, paid=paid)
        typography_style = pick_typography(scene, seed)
        prompt = build_flux_prompt(self.template, scene, title, artist,
                                   typography_style)
        log.info("art.prompt", extra={
            "item_id": iid, "stage": "scene",
            "duration_ms": round((time.monotonic() - t0) * 1000)})

        self.backend = "ok" if await self.flux.health() else "down"
        if self.backend != "ok":
            raise FluxTimeout("flux health probe failed")

        t0 = time.monotonic()
        png = await self.flux.generate(prompt, seed)
        log.info("art.rendered", extra={
            "item_id": iid, "stage": "render", "seed": seed,
            "duration_ms": round((time.monotonic() - t0) * 1000)})

        t0 = time.monotonic()
        png_path, jpg_path = await asyncio.to_thread(
            save_art, self.cfg, png, art_stem(item))
        log.info("art.encoded", extra={
            "item_id": iid, "stage": "encode",
            "duration_ms": round((time.monotonic() - t0) * 1000),
            "png": str(png_path), "jpg": str(jpg_path)})

        meta = {
            "scene": scene, "prompt": prompt, "seed": seed,
            "title_text": title, "artist_text": artist,
            "typography": typography_style,
            "request_text": request_text, "paid": bool(paid),
            "png_path": str(png_path), "model": self.cfg.art.size,
            "rendered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        return png_path, jpg_path, meta

    async def process(self, item: dict[str, Any], seed: int | None = None) -> bool:
        """Render one song's cover. Returns True on success; records a failed
        attempt (back-off) and returns False otherwise. Never raises.

        `seed` overrides the deterministic per-item seed (art_backfill --seed).
        """
        iid = int(item["id"])
        seed = art_seed(iid) if seed is None else int(seed)
        meta = _meta(item)
        request_text = str(meta.get("request_text") or "") or None
        paid = bool(meta.get("paid"))
        async with self._render_lock:
            try:
                result = await self._render_one(item, seed,
                                                request_text=request_text,
                                                paid=paid)
            except FluxBusy as e:
                self.backend = "busy"
                self._failed(iid, "render", e)
                return False
            except ArtError as e:
                self._failed(iid, "render", e)
                return False
            except Exception as e:  # noqa: BLE001 - disk/DB/LLM surprises: back off
                self._failed(iid, "scene", e)
                return False
        if result is None:
            return False
        png_path, jpg_path, meta = result
        try:
            self.db.set_art(iid, str(jpg_path), meta)
        except Exception as e:  # noqa: BLE001 - a DB hiccup must not kill the worker
            self._failed(iid, "store", e)
            return False
        self.rendered += 1
        self.backend = "ok"
        return True

    async def render_brief(self, brief: dict[str, Any], token: str,
                           request_text: str | None = None,
                           paid: bool = False
                           ) -> tuple[str, dict[str, Any]] | None:
        """Render a cover from a song brief before the song item exists.

        This is the early path: the producer starts it as soon as the song
        brief is written, so FLUX runs while the song generator renders. The
        result is attached to the item by the producer. Failures are logged
        and left for the background worker (the item keeps art_path NULL).
        """
        title = sanitize_text(str(brief.get("title") or ""), TITLE_CAP)
        artist = sanitize_text(str(brief.get("artist") or ""), ARTIST_CAP)
        if not title or not artist:
            return None
        iid = int(brief.get("request_id") or 0)
        item = {
            "id": iid,
            "media_path": f"_pending_{token}.mp3",
            "title": brief.get("title"),
            "artist": brief.get("artist"),
            "genre": brief.get("genre"),
            "meta_json": {"brief": brief, "lyrics": brief.get("lyrics", ""),
                          "style_prompt": brief.get("style_prompt", ""),
                          "request_text": request_text, "paid": bool(paid)},
        }
        seed = int(brief.get("seed") or art_seed(iid))
        async with self._render_lock:
            try:
                result = await self._render_one(item, seed,
                                                request_text=request_text,
                                                paid=paid)
            except Exception as e:  # noqa: BLE001 - early art never blocks a song
                log.warning("art.early_failed", extra={
                    "request_id": iid or None, "stage": "render",
                    "error": f"{type(e).__name__}: {e}"[:300]})
                return None
        if result is None:
            return None
        _png_path, jpg_path, meta = result
        self.rendered += 1
        return str(jpg_path), meta

    def attach(self, item_id: int, jpg_path: str, meta: dict[str, Any]) -> None:
        """Record an early-rendered cover on the now-created item."""
        self.db.set_art(item_id, jpg_path, meta)
        self.pending_early.discard(item_id)

    def clear_pending(self, item_id: int) -> None:
        self.pending_early.discard(item_id)

    def _failed(self, item_id: int, stage: str, exc: Exception) -> None:
        self.failed += 1
        log.warning("art.failed", extra={
            "item_id": item_id, "stage": stage,
            "error": f"{type(exc).__name__}: {exc}"[:300]})
        try:
            self.db.set_art_attempted(item_id, time.time())
        except Exception:  # noqa: BLE001 - a DB hiccup must not kill the worker
            log.exception("art.backoff_write_failed", extra={"item_id": item_id})

    # -- loop --------------------------------------------------------------
    async def run(self) -> None:
        log.info("art.worker_start", extra={
            "enabled": self.cfg.art.enabled, "size": self.cfg.art.size})
        while True:
            delay = self.cfg.art.idle_poll_s
            try:
                self.db.meta_set(LEASE_KEY, _iso_now())
                pending = self.db.songs_needing_art(
                    10, retry_after_s=self.cfg.art.retry_after_s)
                pending = [it for it in pending
                           if int(it["id"]) not in self.pending_early]
                if pending:
                    ok = await self.process(pending[0])
                    if not ok:
                        delay = self.cfg.art.retry_after_s
                else:
                    log.info("art.idle", extra={"with_art": self.db.art_counts()["with_art"]})
            except Exception as e:  # noqa: BLE001 - never raise out of the loop
                log.warning("art.loop_error", extra={"error": f"{type(e).__name__}: {e}"[:300]})
                delay = self.cfg.art.retry_after_s
            await asyncio.sleep(delay)

    async def close(self) -> None:
        await self.flux.close()


def _meta(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("meta_json")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        import json
        out = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return out if isinstance(out, dict) else {}


def _iso_now() -> str:
    import datetime
    return datetime.datetime.now(datetime.UTC).isoformat()

"""Cover art per song (ALBUM_ART.md Steps 4-5; RADIO §8.4, §14).

Storage half (Step 4): where the master PNG and the delivered JPEG live, and
how they are written without ever touching an existing library file
(hard rule 5). The worker half (Step 5) is `ArtWorker` below.

The FLUX server is single-flight and blocks while it renders, so everything
here is one-at-a-time, with a memory gate and a back-off. Nothing in playout
imports this module (hard rule 1): a song without art airs exactly as before.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from pilgrim.config import ROOT, Config
from pilgrim.pipelines.art import ARTIST_CAP, TITLE_CAP, ArtError, sanitize_text

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

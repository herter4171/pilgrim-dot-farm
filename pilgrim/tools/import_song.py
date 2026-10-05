"""Operator import: add an existing audio file (MP3/WAV/FLAC/...) to the song
library. QC (§8.1) -> two-pass normalize -> FLAC delivery (§8.2) -> store as a
fresh evergreen song -> best-effort DJ intro, exactly like the stock song path
(§6.2 / producer.song_step). The scheduler airs fresh songs newest-first
(Tier 1, RADIO §5.2), so an imported song plays at the next song slot after
any interjection.

Operator tool: runs against the LIVE station.db while the server is up
(WAL mode; the scheduler sees the new item at its next commit tick).

Usage:
    python -m pilgrim.tools.import_song ~/uploads/foo.mp3
    python -m pilgrim.tools.import_song ~/uploads/foo.mp3 --title "Foo" --artist "Bar"
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import cast

import numpy as np
import soundfile as sf

from pilgrim.audio import normalize
from pilgrim.audio.qc import grade_audio
from pilgrim.config import (
    ROOT,
    Config,
    load_api_key,
    load_config,
)
from pilgrim.pipelines.llm import LLM
from pilgrim.pipelines.voice import KokoroClient, VoicePipeline
from pilgrim.store import Store

log = logging.getLogger("radio.import")

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus", ".aiff", ".aif"}


def _title_from_filename(path: Path) -> str:
    """real_whistle_tips.mp3 -> Real Whistle Tips (small words stay lower)."""
    stem = path.stem.replace("_", " ").replace("-", " ")
    small = {"a", "an", "the", "and", "or", "of", "on", "in", "to", "for"}
    words = stem.split()
    out = []
    for i, w in enumerate(words):
        if i == 0 or i == len(words) - 1 or w not in small:
            out.append(w.capitalize())
        else:
            out.append(w.lower())
    return " ".join(out).strip() or stem


async def import_song(src: Path, title: str | None, artist: str | None,
                      genre: str | None) -> int:
    if not src.exists():
        print(f"not found: {src}")
        return 2
    if src.suffix.lower() not in AUDIO_EXTS:
        print(f"unsupported type: {src.suffix} (try {sorted(AUDIO_EXTS)})")
        return 2
    cfg: Config = load_config()
    db = Store(ROOT / cfg.library.db)
    media_dir = ROOT / cfg.library.dir

    # 1) Decode + QC (§8.1) with the same bounds as generated songs.
    x, sr = sf.read(str(src), dtype="float32")
    x = np.asarray(x, dtype=np.float32)
    duration = float(len(x)) / sr
    verdict = grade_audio(
        x, sr, duration_s=duration,
        min_dur=float(cfg.songs.min_duration_s),
        max_dur=float(cfg.songs.max_duration_s),
        kind="song", max_gap_s=2.0, silence_db=cfg.audio.silence_db)
    if not verdict.ok:
        print(f"QC failed: {verdict.reasons} (duration {duration:.1f}s)")
        return 1
    print(f"QC ok: {duration:.1f}s, {sr} Hz, {x.shape[1] if x.ndim > 1 else 1}ch"
          + (", abrupt end -> soft fade" if verdict.abrupt_end else ""))

    # 2) Two-pass loudnorm -> FLAC (§8.2), same soft landing as stock songs.
    dst = media_dir / f"song_{int(time.time() * 1000)}.flac"
    meta = normalize.normalize(src, dst, cfg,
                               fade_out_s=cfg.songs.abrupt_fade_s
                               if verdict.abrupt_end else 0.0)
    out_sr = cast(int, meta["sample_rate"])
    out_ch = cast(int, meta["channels"])
    out_dur = cast(float, meta["duration_s"])
    print(f"normalized: {out_dur:.1f}s -> {dst.name}")

    # 3) Store as a fresh evergreen song (Tier-1 LIFO playout).
    song_id = db.add_item(
        type_="song", media_path=str(dst), duration_s=out_dur,
        sample_rate=out_sr, channels=out_ch,
        title=title, artist=artist, genre=genre,
        evergreen=True, fresh=True,
        meta={"source": "upload", "original_path": str(src.resolve()),
              "abrupt_end": bool(verdict.abrupt_end)})
    log.info("import.song", extra={"item_id": song_id, "title": title,
                                   "source": str(src.resolve())})
    print(f"stored song {song_id}: {title!r} (fresh, plays at the next song slot)")

    # 4) Best-effort DJ intro (stock intros are non-fatal, §6.2 / 4.7).
    llm = LLM(cfg, load_api_key())
    kokoro = KokoroClient(cfg)
    voice = VoicePipeline(cfg, llm, kokoro, db, media_dir,
                          prompts=_load_prompts(cfg))
    try:
        ctx = f'Next song: "{title}"'
        if artist:
            ctx += f" by {artist}"
        if genre:
            ctx += f" ({genre})"
        ctx += ".\nDo not mention the clock time."
        try:
            item = await voice.produce_item("intro", 10.0, context=ctx)
            intro_id = db.add_item(
                type_="intro", media_path=str(item["media_path"]),
                duration_s=item["duration_s"],
                sample_rate=item.get("sample_rate"), channels=item.get("channels"),
                role="intro", evergreen=False, fresh=True,
                meta={"song_item_id": song_id,
                      "text": (item.get("meta") or {}).get("text")})
            print(f"intro {intro_id} rendered ({item['duration_s']:.1f}s)")
        except Exception as e:
            log.warning("import.intro_failed", extra={"error": str(e)})
            print(f"intro failed (song airs with the fallback callout): {e}")
    finally:
        await kokoro.close()
        await llm.close()
    return 0


def _load_prompts(cfg: Config) -> dict[str, str]:
    pdir = ROOT / cfg.library.prompts_dir
    out: dict[str, str] = {}
    if pdir.is_dir():
        for p in sorted(pdir.glob("*.md")):
            out[p.stem] = p.read_text()
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", help="audio file to import (mp3/wav/flac/...)")
    ap.add_argument("--title", help="song title (default: from filename)")
    ap.add_argument("--artist", help="artist (default: none)")
    ap.add_argument("--genre", help="genre (default: none)")
    a = ap.parse_args()
    src = Path(a.file).expanduser()
    title = a.title or _title_from_filename(src)
    try:
        return asyncio.run(import_song(src, title, a.artist, a.genre))
    except Exception as e:  # decode/QC/TTS/DB: one clear failure, no retry storm
        log.error("import failed: %s", e)
        print(f"import failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

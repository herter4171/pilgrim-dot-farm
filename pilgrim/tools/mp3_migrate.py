"""Backfill the library to MP3 + S3 (RADIO §8.2, §8.4).

    python -m pilgrim.tools.mp3_migrate                 # dry run: counts only
    python -m pilgrim.tools.mp3_migrate --apply         # encode + repoint rows
    python -m pilgrim.tools.mp3_migrate --apply --upload  # ... then upload to S3
    python -m pilgrim.tools.mp3_migrate --upload        # upload only

For every non-emergency item whose `media_path` is a FLAC, encode a 48 kHz
stereo MP3 **next to it** (the FLAC master is never deleted or modified,
hard rule 5), verify its gapless header, and point the row at the MP3.
Emergency items (§8.3) are left alone and keep playing from the origin.

Resumable: a row already on .mp3 is skipped; an MP3 left by an interrupted
run is re-verified and reused. `--apply` takes a SQLite `.backup` of
station.db first. Safe while the station runs: each row update is one short
transaction, and a FLAC that is mid-air keeps decoding from cache.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import soundfile as sf

from pilgrim.audio.normalize import NormalizeError, encode_mp3, gapless_info
from pilgrim.config import ROOT, Config, load_config
from pilgrim.store import Store


def flac_rows(db_path: Path) -> tuple[list[dict[str, Any]], int]:
    """(convertible rows, emergency rows skipped)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id, type, media_path, emergency FROM items "
            "WHERE media_path LIKE '%.flac' ORDER BY id")]
    finally:
        con.close()
    keep = [r for r in rows if not r["emergency"]]
    return keep, len(rows) - len(keep)


def backup_db(db_path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%SZ")
    dst = db_path.with_name(f"{db_path.name}.mp3-migration-{stamp}.bak")
    src = sqlite3.connect(str(db_path))
    out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dst


def convert_one(cfg: Config, row: dict[str, Any]) -> dict[str, Any]:
    """Encode one FLAC master to MP3 beside it. Returns the encode meta."""
    src = ROOT / str(row["media_path"])
    if not src.exists():
        raise FileNotFoundError(src.name)
    dst = src.with_suffix(".mp3")
    if dst.exists():
        try:  # leftover from an interrupted run: reuse if it verifies
            gapless_info(dst)
            d = sf.info(str(dst))
            if d.samplerate == cfg.audio.sample_rate and d.channels == 2:
                return {"path": dst, "duration_s": round(d.frames / d.samplerate, 3),
                        "sample_rate": d.samplerate, "channels": d.channels}
        except (NormalizeError, RuntimeError):
            pass
    kbps = cfg.audio.mp3_kbps if sf.info(str(src)).channels > 1 \
        else cfg.audio.mp3_mono_source_kbps
    meta = encode_mp3(src, dst, cfg, kbps)
    return {"path": dst, **meta}


def convert(cfg: Config, store: Store, rows: list[dict[str, Any]], jobs: int) -> tuple[int, int]:
    ok = fail = 0
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(convert_one, cfg, r): r for r in rows}
        for i, f in enumerate(as_completed(futs), 1):
            r = futs[f]
            try:
                m = f.result()
            except Exception as e:  # noqa: BLE001 - report and continue
                fail += 1
                print(f"  FAIL item {r['id']} ({Path(r['media_path']).name}): {e}",
                      file=sys.stderr)
                continue
            store.set_media_file(int(r["id"]), media_path=str(m["path"]),
                                 duration_s=float(m["duration_s"]),
                                 sample_rate=int(m["sample_rate"]),
                                 channels=int(m["channels"]))
            ok += 1
            if i % 100 == 0 or i == len(rows):
                print(f"  encoded {i}/{len(rows)} ({time.monotonic() - t0:.0f}s)")
    return ok, fail


async def upload_all(cfg: Config, store: Store) -> int:
    from pilgrim.media_publish import MediaPublisher, S3ObjectStore

    pub = MediaPublisher(cfg, store, S3ObjectStore(cfg))
    total = 0
    while True:
        n = await pub.publish_pending()
        total += n
        if n:
            print(f"  uploaded {total}")
        if n == 0:
            # nothing left, or only items in their failure back-off window
            return total


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="encode MP3s and repoint rows")
    ap.add_argument("--upload", action="store_true", help="upload unpublished MP3s to S3")
    ap.add_argument("--jobs", type=int, default=6, help="parallel ffmpeg encodes")
    ap.add_argument("--limit", type=int, default=0, help="convert at most N items")
    ap.add_argument("--config", type=Path, default=None)
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    db_path = ROOT / cfg.library.db
    rows, emergency = flac_rows(db_path)
    if a.limit:
        rows = rows[:a.limit]
    print(f"DB:        {db_path}")
    print(f"To encode: {len(rows)} FLAC item(s); emergency skipped: {emergency}")
    if not (a.apply or a.upload):
        print("Dry run. Add --apply to encode, --upload to publish.")
        return 0
    store = Store(db_path)
    fail = 0
    if a.apply and rows:
        print(f"Backup:    {backup_db(db_path)}")
        ok, fail = convert(cfg, store, rows, max(1, a.jobs))
        print(f"Encoded:   {ok} ok, {fail} failed")
    if a.upload:
        if not cfg.media.bucket:
            print("media.bucket is not set", file=sys.stderr)
            return 2
        n = asyncio.run(upload_all(cfg, store))
        left = len(store.items_to_publish(limit=100000))
        print(f"Uploaded:  {n}; still unpublished: {left}")
        fail += left
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())

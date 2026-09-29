"""Audit the library and retire truncated inventory (OVERHAUL 2.2).

Voice types (commercial, liner, dj_talk, news) whose `meta.text` implies a
speech rate above `audio.max_words_per_s` were made before the speech-rate gate
(OVERHAUL 2.1) and are retired. Songs shorter than the sanity floor
(`songs.min_duration_s`, default 20) are retired. Songs that end abruptly are
REPORTED but never retired — Phase 3 handles those, and they're expensive.

Files on disk are never touched; retiring only flips a DB flag.

Dry run by default; `--apply` actually retires.

Usage:
    python -m pilgrim.tools.audit_library            # dry run
    python -m pilgrim.tools.audit_library --apply    # retire flagged items
"""
from __future__ import annotations

import argparse
import json

from pilgrim.config import ROOT, load_config
from pilgrim.store import Store

_VOICE_TYPES = ("commercial", "liner", "dj_talk", "news")


def classify(rows: list[dict], cfg) -> tuple[list[tuple[int, str, str]], list[int]]:
    """Return (to_retire [(id, type, reason)], abrupt_song_ids).

    Voice buggy items fail the speech-rate rule; short songs fail the floor;
    abrupt-ending songs are collected but never retired here."""
    max_wps = cfg.audio.max_words_per_s
    song_floor = float(getattr(cfg.songs, "min_duration_s", 20))
    to_retire: list[tuple[int, str, str]] = []
    abrupt: list[int] = []
    for r in rows:
        t: str = r["type"]
        meta = json.loads(r["meta_json"]) if r["meta_json"] else {}
        if t in _VOICE_TYPES:
            text = meta.get("text")
            if isinstance(text, str) and text.strip():
                words = len(text.split())
                speech_s = r["duration_s"] - 0.15
                if speech_s > 0.1 and words / speech_s > max_wps:
                    to_retire.append((r["id"], t, f"speech rate "
                                      f"{words / speech_s:.1f} w/s > {max_wps}"))
        elif t == "song":
            if r["duration_s"] < song_floor:
                to_retire.append((r["id"], t, f"duration {r['duration_s']:.0f}s "
                                  f"< floor {song_floor:.0f}s"))
            if meta.get("abrupt_end"):
                abrupt.append(r["id"])
    return to_retire, abrupt


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually retire flagged items (default is dry run)")
    args = ap.parse_args(argv)

    cfg = load_config()
    store = Store(ROOT / cfg.library.db)
    rows = store.list_items()  # already excludes retired rows
    to_retire, abrupt = classify(rows, cfg)
    by_type: dict[str, int] = {}
    for _, t, _ in to_retire:
        by_type[t] = by_type.get(t, 0) + 1

    if not args.apply:
        print(f"DRY RUN — {len(to_retire)} item(s) would be retired:")
        print(f"  by type: {by_type or 'none'}")
        for item_id, t, reason in to_retire:
            dur = next(r["duration_s"] for r in rows if r["id"] == item_id)
            print(f"  #{item_id:5} {t:12} {dur:6.1f}s  {reason}")
        print(f"abrupt-ending songs reported (NOT retired here): {len(abrupt)}")
        if abrupt:
            print(f"  ids: {sorted(abrupt)}")
        print("\nRe-run with --apply to retire.")
        return

    for item_id, _t, reason in to_retire:
        store.retire_item(item_id, reason)
    print(f"Retired {len(to_retire)} item(s): {by_type or 'none'}")
    if abrupt:
        print(f"abrupt-ending songs still active (Phase 3 will fade them): "
              f"{sorted(abrupt)}")


if __name__ == "__main__":
    main()

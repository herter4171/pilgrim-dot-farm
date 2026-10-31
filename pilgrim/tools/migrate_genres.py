"""Map stored free-text song genres onto the 18 MiniMax Music 3 style families.

Before the family vocabulary, `items.genre` held whatever the brief LLM wrote
("yacht rock", "1970s jazz disco cool sax farm ballad", "Linkin Park"). §5.2
no-repeat compares exact strings, so those labels never matched each other.
This rewrites each song's genre to the nearest family id using the same router
the request line uses. The original label is preserved in `meta_json.brief`.

Dry-run by default; `--apply` writes a timestamped station.db backup first.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from collections import Counter
from pathlib import Path

from pilgrim import genres
from pilgrim.config import ROOT, load_config


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    cfg = load_config()
    db = ROOT / cfg.library.db
    conn = _connect(db)
    rows = list(conn.execute(
        "select id, title, genre from items where type='song' and retired=0"))
    moves: Counter[tuple[str, str]] = Counter()
    unmapped: Counter[str] = Counter()
    changes: list[tuple[int, str]] = []
    for r in rows:
        old = (r["genre"] or "").strip()
        if old in genres.FAMILIES:
            continue
        new = genres.route(old)
        if new is None:
            unmapped[old or "(empty)"] += 1
            continue
        moves[(old, new)] += 1
        changes.append((r["id"], new))

    print(f"database: {db}")
    print(f"songs scanned: {len(rows)}   to remap: {len(changes)}")
    for (old, new), n in moves.most_common():
        print(f"  {n:4d}  {old!r} -> {new}")
    if unmapped:
        print("unmapped (left as-is):")
        for old, n in unmapped.most_common():
            print(f"  {n:4d}  {old!r}")

    if not args.apply:
        print("\ndry run; pass --apply to write")
        return 0

    backup = db.with_name(f"station.db.genre-migration-{time.strftime('%Y%m%d-%H%M%S')}.bak")
    shutil.copy2(db, backup)
    print(f"backup: {backup}")
    with conn:
        for item_id, new in changes:
            row = conn.execute("select meta_json from items where id=?",
                               (item_id,)).fetchone()
            meta = json.loads(row["meta_json"] or "{}")
            meta.setdefault("genre_before_migration", meta.get("genre"))
            conn.execute("update items set genre=?, meta_json=? where id=?",
                         (new, json.dumps(meta), item_id))
    print(f"updated {len(changes)} songs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

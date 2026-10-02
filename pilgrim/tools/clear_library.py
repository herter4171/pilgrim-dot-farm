"""Wipe station content for a fresh start (operator-approved, 2026-09-29).

Default wipes everything: every row from the items, program, airplay, and
requests tables, resets the AUTOINCREMENT sequences, and removes the rendered
FLAC files in the app library dir. Files on disk are NOT preserved: this is a
full clean-slate reset (an explicit operator override of AGENTS rule 5, "never
delete pilgrim/library/ files").

``--keep-songs`` instead retains the song inventory: song item rows and song
media files survive, along with the listener request log and the emergency
pack (RADIO §8.3, AGENTS rule 5); every other type
(news, liner, dj_talk, commercial, intro, sfx, field_report) is removed from
items and disk, and the committed program + airplay history are cleared.

Default is a dry run. ``--apply`` performs the wipe; ``--yes`` skips the
interactive confirmation.

Usage:
    python -m pilgrim.tools.clear_library           # dry run (full wipe)
    python -m pilgrim.tools.clear_library --keep-songs   # dry run (keep songs)
    python -m pilgrim.tools.clear_library --apply --yes
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from pilgrim.config import ROOT, Config, load_config

_TABLES = ("items", "program", "airplay", "requests")

def _dry_counts(db_path: Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out: dict[str, int] = {}
        for t in _TABLES:
            try:
                out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                out[t] = 0
        return out
    finally:
        con.close()


def _dry_keep_songs(db_path: Path, library_dir: Path) -> dict[str, int]:
    """Plan for --keep-songs: non-song item rows, non-song files, program and
    airplay rows that would be removed. Requests and songs are untouched."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        non_items = con.execute(
            "SELECT COUNT(*) FROM items WHERE type NOT IN ('song') AND emergency=0").fetchone()[0]
        rows = con.execute(
            "SELECT media_path FROM items WHERE type NOT IN ('song') AND emergency=0").fetchall()
        files = sum(1 for (p,) in rows if Path(p).exists())
        extra = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in ("program", "airplay")}
        return {"items": non_items, "files": files, "program": extra["program"],
                "airplay": extra["airplay"]}
    finally:
        con.close()

def _delete_sequence(con: sqlite3.Connection, table: str) -> None:
    """Reset one table's AUTOINCREMENT sequence after it is fully emptied."""
    con.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))

def wipe_db(db_path: Path) -> dict[str, int]:
    """Delete all content rows and reset id sequences. Returns rows removed."""
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        removed: dict[str, int] = {}
        for t in _TABLES:
            cur = con.execute(f"DELETE FROM {t}")
            removed[t] = cur.rowcount
        con.execute("DELETE FROM sqlite_sequence")
        con.commit()  # VACUUM may not run inside a transaction (then it fails)
        con.execute("VACUUM")  # shrink the file back down to empty-database size
        return removed
    finally:
        con.close()


def wipe_non_songs(db_path: Path, library_dir: Path) -> dict[str, int]:
    """For --keep-songs: delete non-song item rows + their media files, clear the
    committed program and airplay history. Song rows/files and the request log
    are preserved; the items and requests sequences are left intact."""
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        doomed = [Path(p) for (p,) in con.execute(
            "SELECT media_path FROM items WHERE type NOT IN ('song') AND emergency=0").fetchall()]
        cur = con.execute("DELETE FROM items WHERE type NOT IN ('song') AND emergency=0")
        removed_items = cur.rowcount
        prog = con.execute("DELETE FROM program").rowcount
        air = con.execute("DELETE FROM airplay").rowcount
        _delete_sequence(con, "program")
        _delete_sequence(con, "airplay")
        con.commit()
        removed_files = 0
        for p in doomed:
            if p.exists() and p.parent == library_dir.resolve():
                p.unlink()
                removed_files += 1
        con.execute("VACUUM")
        return {"items": removed_items, "files": removed_files,
                "program": prog, "airplay": air}
    finally:
        con.close()


def flac_files(library_dir: Path) -> list[Path]:
    return sorted(library_dir.glob("*.flac"))


def wipe_files(library_dir: Path) -> int:
    n = 0
    for p in flac_files(library_dir):
        p.unlink()
        n += 1
    return n


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually wipe (default is dry run)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--keep-songs", action="store_true",
                    help="delete only non-song content; keep songs + request log")
    args = ap.parse_args(argv)

    cfg: Config = load_config()
    db_path = ROOT / cfg.library.db
    library_dir = ROOT / cfg.library.dir

    if args.keep_songs:
        counts = _dry_keep_songs(db_path, library_dir)
        lib_gb = sum(p.stat().st_size for p in flac_files(library_dir)) / 1e9
        n_flac = len(flac_files(library_dir))
        print(f"DB:        {db_path} ({db_path.stat().st_size / 1e6:.1f} MB)")
        print(f"Library:   {library_dir} ({lib_gb:.2f} GB, {n_flac} .flac)")
        print(f"Would DELETE non-song item rows: {counts['items']}")
        print(f"Would DELETE non-song files:     {counts['files']}")
        print(f"Would DELETE program rows:       {counts['program']}")
        print(f"Would DELETE airplay rows:       {counts['airplay']}")
        print("KEEP: songs (items + files), the emergency pack, and the request log.")
        if not args.apply:
            print("\nDry run — nothing changed. Re-run with --apply to wipe.")
            return
        if not args.yes and input("\nType 'wipe' to confirm: ") != "wipe":
            print("Aborted.")
            return
        removed = wipe_non_songs(db_path, library_dir)
        print("\nWiped non-song content.", dict(removed))
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        songs = con.execute("SELECT COUNT(*) FROM items WHERE type='song'").fetchone()[0]
        con.close()
        print(f"Remaining: {songs} song items kept "
              f"({len(flac_files(library_dir))} file(s) on disk).")
        return

    counts = _dry_counts(db_path)
    flacs = flac_files(library_dir)
    total_rows = sum(counts.values())

    lib_gb = sum(p.stat().st_size for p in flacs) / 1e9
    print(f"DB:        {db_path} ({db_path.stat().st_size / 1e6:.1f} MB)")
    print(f"Library:   {library_dir} ({lib_gb:.2f} GB, {len(flacs)} .flac)")
    print(f"Would DELETE rows: {total_rows}  {dict(counts)}")
    print(f"Would DELETE files: {len(flacs)}")
    if not args.apply:
        print("\nDry run — nothing changed. Re-run with --apply to wipe.")
        return

    if not args.yes and input("\nType 'wipe' to confirm: ") != "wipe":
        print("Aborted.")
        return

    removed = wipe_db(db_path)
    removed_files = wipe_files(library_dir)
    print("\nWiped. Rows removed:", dict(removed), f"| files removed: {removed_files}")
    new_size = db_path.stat().st_size / 1e6
    removed_bytes = sum(p.stat().st_size for p in flac_files(library_dir))
    print(f"DB now {new_size:.1f} MB; library now {removed_bytes / 1e6:.1f} MB")


if __name__ == "__main__":
    main()

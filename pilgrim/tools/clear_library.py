"""Wipe all station content for a fresh start (operator-approved, 2026-09-29).

Deletes every row from the inventory, program, airplay, and requests tables,
resets the AUTOINCREMENT sequences, and removes the rendered FLAC files in the
app library dir. Files on disk are NOT preserved: this is the full clean-slate
reset the operator asked for, an explicit one-time override of AGENTS rule 5
("never delete pilgrim/library/ files").

Default is a dry run. ``--apply`` performs the wipe; ``--yes`` skips the
interactive confirmation.

Usage:
    python -m pilgrim.tools.clear_library           # dry run
    python -m pilgrim.tools.clear_library --apply   # confirm, then wipe
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
    args = ap.parse_args(argv)

    cfg: Config = load_config()
    db_path = ROOT / cfg.library.db
    library_dir = ROOT / cfg.library.dir

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

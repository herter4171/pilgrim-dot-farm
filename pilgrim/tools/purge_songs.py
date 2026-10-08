"""Purge stock songs — songs no listener asked for (operator tool, RADIO §14).

The producer keeps `inventory.fresh_songs_ready` stock songs in play (§6.1) and
they are evergreen, so they pile up: RADIO §14 puts continuous generation at
several GB per day and defers the pruning policy to Phase 3. This is the
operator's manual prune — it deletes stock songs from `items` **and their FLAC
files from disk**, and keeps every song somebody actually requested.

A song is KEPT when any of these is true:

* a `requests` row names it (`requests.song_item_id` / `intro_item_id`);
* its `meta_json` carries `request_id` (the producer tags request songs, §6.1);
* it is an operator upload (`meta_json.source == "upload"`, tools/import_song);
* it is flagged `emergency` (AGENTS rule 5 — the emergency pack is never deleted);
* it sits inside the live committed program — deleting that would 404 a client
  mid-window (§5). Such songs are DEFERRED and a later pass removes them once
  the program has trimmed past them.

Nothing else is touched: the request log, the airplay ledger and the committed
program stay as they are. `--drop-intros` additionally removes `intro` items
that name a purged song — they can never air again (`_fresh_intro_for` matches
on the song id).

Default is a dry run; `--apply` deletes, `--yes` skips the confirmation.
Take a snapshot first: `VACUUM INTO 'station.db.before-purge.bak'`.

Usage:
    python -m pilgrim.tools.purge_songs                    # dry run
    python -m pilgrim.tools.purge_songs --drop-intros      # dry run incl. intros
    python -m pilgrim.tools.purge_songs --apply --yes
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from pilgrim.config import ROOT, Config, load_config

_UPLOAD_SOURCE = "upload"
_BATCH = 500  # keep IN (...) lists well inside SQLite's variable limit


def _meta(row: dict) -> dict:
    raw = row.get("meta_json")
    if not raw:
        return {}
    try:
        meta = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _in(clause_ids: list[int]) -> str:
    return "(" + ",".join("?" * len(clause_ids)) + ")"


def protected_song_ids(con: sqlite3.Connection) -> set[int]:
    """Song ids a listener request produced — never purged."""
    out: set[int] = set()
    for col in ("song_item_id", "intro_item_id"):
        out |= {int(r[0]) for r in con.execute(
            f"SELECT {col} FROM requests WHERE {col} IS NOT NULL")}
    return out


def live_program_ids(con: sqlite3.Connection) -> set[int]:
    """Item ids inside the committed program window (may be mid-flight)."""
    return {int(r[0]) for r in con.execute("SELECT item_id FROM program")}


def classify(songs: list[dict], protected: set[int],
             live: set[int]) -> tuple[list[int], list[int]]:
    """Split song rows into (purge_now, deferred). Pure — no I/O.

    `songs` rows need `id`, `meta_json`, `emergency`. Request-spawned, uploaded
    and emergency songs are dropped; unrequested songs still in the program
    window go to `deferred` instead of `purge_now`."""
    purge: list[int] = []
    deferred: list[int] = []
    for s in songs:
        meta = _meta(s)
        if (int(s["id"]) in protected or meta.get("request_id") is not None
                or meta.get("source") == _UPLOAD_SOURCE or s["emergency"]):
            continue
        (deferred if int(s["id"]) in live else purge).append(int(s["id"]))
    return purge, deferred


def intro_ids_for(con: sqlite3.Connection, song_ids: list[int]) -> list[int]:
    """Intro items whose meta names one of `song_ids` (dead once it is gone)."""
    wanted = set(song_ids)
    if not wanted:
        return []
    return [int(r[0]) for r in con.execute(
        "SELECT id, meta_json FROM items WHERE type='intro'")
        if _meta({"meta_json": r[1]}).get("song_item_id") in wanted]


def _chunks(ids: list[int]) -> list[list[int]]:
    return [ids[i:i + _BATCH] for i in range(0, len(ids), _BATCH)]


def plan(db_path: Path) -> dict:
    """Read-only plan: which songs to purge, which are deferred, and the sizes."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        songs = [dict(r) for r in con.execute(
            "SELECT id, title, media_path, duration_s, meta_json, emergency, retired "
            "FROM items WHERE type='song'").fetchall()]
        purge, deferred = classify(songs, protected_song_ids(con), live_program_ids(con))
        intro_ids = intro_ids_for(con, purge)
        kept = len(songs) - len(purge) - len(deferred)
        intro_bytes = 0
        if intro_ids:
            marks = _in(intro_ids)
            intro_bytes = sum(_size(path) for (path,) in con.execute(
                f"SELECT media_path FROM items WHERE id IN {marks}", intro_ids))
        by_id = {int(s["id"]): s for s in songs}
        purge_bytes = sum(_size(by_id[i]["media_path"]) for i in purge)
        return {
            "songs_total": len(songs), "purge": purge, "deferred": deferred,
            "intros": intro_ids, "kept": kept, "purge_bytes": purge_bytes,
            "intro_bytes": intro_bytes,
            "titles": [(i, by_id[i]["title"] or "?") for i in purge],
        }
    finally:
        con.close()


def _size(path: str) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def apply_purge(db_path: Path, library_dir: Path, item_ids: list[int]) -> dict:
    """Delete the given item rows and their media files. Returns counts.

    Only files that live directly in the configured library dir are unlinked;
    anything else is reported as skipped, never touched."""
    deleted_files = 0
    skipped: list[str] = []
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA busy_timeout=10000")
        removed = 0
        for chunk in _chunks(item_ids):
            paths = [p for (p,) in con.execute(
                f"SELECT media_path FROM items WHERE id IN {_in(chunk)}", chunk)]
            removed += con.execute(
                f"DELETE FROM items WHERE id IN {_in(chunk)}", chunk).rowcount
            for p in paths:
                f = Path(p)
                if f.parent == library_dir.resolve():
                    if f.exists():
                        f.unlink()
                        deleted_files += 1
                else:
                    skipped.append(p)
        if removed:  # song catalogue changed — let the DJ console refetch (TUI §4)
            con.execute(
                "INSERT INTO meta (key, value) VALUES ('catalogue_revision','1') "
                "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")
        con.commit()
        try:
            con.execute("VACUUM")
        except sqlite3.OperationalError as e:  # e.g. disk full: rows are already gone
            print(f"VACUUM skipped: {e}")
        return {"items": removed, "files": deleted_files, "skipped": skipped}
    finally:
        con.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default is a dry run)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--drop-intros", action="store_true",
                    help="also delete intros that name a purged song")
    ap.add_argument("--list", action="store_true", help="print every purged song title")
    args = ap.parse_args(argv)

    cfg: Config = load_config()
    db_path = ROOT / cfg.library.db
    library_dir = ROOT / cfg.library.dir

    p = plan(db_path)
    item_ids = list(p["purge"]) + (p["intros"] if args.drop_intros else [])
    total_bytes = p["purge_bytes"] + (p["intro_bytes"] if args.drop_intros else 0)

    print(f"DB:      {db_path}")
    print(f"Library: {library_dir}")
    print(f"Songs: {p['songs_total']} total -> keep {p['kept']} "
          f"(request-spawned / uploaded / emergency), "
          f"purge {len(p['purge'])} unrequested"
          + (f", defer {len(p['deferred'])} still in the program window"
             if p["deferred"] else ""))
    print(f"Media to delete: {total_bytes / 1e9:.2f} GB "
          f"({p['purge_bytes'] / 1e9:.2f} GB of songs"
          + (f", {p['intro_bytes'] / 1e6:.1f} MB of intros" if args.drop_intros else
             f"; {len(p['intros'])} intros name a purged song "
             f"({p['intro_bytes'] / 1e6:.1f} MB) — kept, use --drop-intros") + ")")
    if p["deferred"]:
        print(f"Deferred (in committed program, re-run later): {p['deferred']}")
    shown = p["titles"] if args.list else p["titles"][:10]
    for i, title in shown:
        print(f"  - {i}  {title}")
    if not args.list and len(p["titles"]) > 10:
        print(f"  ... {len(p['titles']) - 10} more (--list for all)")

    if not item_ids:
        print("\nNothing to purge.")
        return
    if not args.apply:
        print(f"\nDry run — nothing changed. {len(item_ids)} item(s) would be deleted. "
              f"Re-run with --apply --yes.")
        return
    if not args.yes:
        prompt = f"\nType 'purge' to delete {len(item_ids)} items + files: "
        if input(prompt) != "purge":
            print("Aborted.")
            return

    removed = apply_purge(db_path, library_dir, item_ids)
    print(f"\nPurged {removed['items']} item row(s), {removed['files']} file(s)."
          + (f" Skipped outside library dir: {removed['skipped']}" if removed["skipped"] else ""))
    after = plan(db_path)
    print(f"Songs now: {after['songs_total']} (kept {after['kept']}), "
          f"deferred: {after['deferred']}")


if __name__ == "__main__":
    main()

"""One-shot station.db -> Postgres copy (SCALABILITY.md Step 2; RADIO §14).

    make pg-migrate                               # dry run: full copy in a transaction, rolled back
    make pg-migrate PG_MIGRATE_ARGS="--apply"     # commit
    make pg-migrate PG_MIGRATE_ARGS="--apply --truncate"   # target not empty: wipe it first

The target is whatever `DATABASE_URL` (+ `DB_PASS_SECRET_ARN` /
`DATABASE_PASSWORD`) names; the schema is created by `Store` itself, so the
copy can never disagree with what the station will open. Ids are preserved
(items.id is in file names, S3 keys and requests.song_item_id; program.seq is
never reused), identity sequences are set from SQLite's high-water marks, and
every table's row count is verified before the commit. The SQLite file is
opened read-only and never modified (AGENTS §1.5).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

from pilgrim.config import ROOT, load_config
from pilgrim.db import database_settings, resolve_password
from pilgrim.store import Store

# Dependency order (requests/votes reference items/requests by id; nothing is
# enforced by a foreign key, but the order keeps a partial failure legible).
TABLES = ["items", "program", "airplay", "requests", "visitor_ips", "meta",
          "stripe_events", "request_votes", "song_ratings"]
IDENTITY = {"items": "id", "program": "seq", "airplay": "id", "requests": "id",
            "song_ratings": "id"}
BATCH = 2000


def _source(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _target_counts(store: Store) -> dict[str, int]:
    return {t: int(store._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            for t in TABLES}


def copy(src: sqlite3.Connection, store: Store, *, apply: bool, truncate: bool,
         out=sys.stdout) -> int:
    """Copy every table; return 0 on success. Prints a per-table report."""
    conn = store._conn
    with store._lock:
        conn.begin()   # one transaction: the connection is autocommit otherwise
        before = _target_counts(store)
        if any(before.values()):
            if not truncate:
                print(f"target is not empty: {before}; pass --truncate to wipe it", file=out)
                conn.rollback()
                return 2
            for t in reversed(TABLES):
                conn.execute(f"DELETE FROM {t}")
            print(f"truncated target tables ({sum(before.values())} rows)", file=out)

        src_tables = {r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        copied: dict[str, int] = {}
        t0 = time.monotonic()
        for t in TABLES:
            if t not in src_tables:
                print(f"{t:16s} absent in source, skipped", file=out)
                continue
            src_cols = [r[1] for r in src.execute(f"PRAGMA table_info({t})")]
            dst_cols = conn.columns(t)
            cols = [c for c in src_cols if c in dst_cols]
            missing = [c for c in src_cols if c not in dst_cols]
            if missing:
                print(f"{t:16s} WARNING source columns not in target, dropped: {missing}",
                      file=out)
            collist = ", ".join(cols)
            marks = ",".join("?" * len(cols))
            sql = f"INSERT INTO {t} ({collist}) VALUES ({marks})"
            n = 0
            cur = src.execute(f"SELECT {collist} FROM {t}")
            while True:
                rows = cur.fetchmany(BATCH)
                if not rows:
                    break
                conn.executemany(sql, [tuple(r) for r in rows])
                n += len(rows)
            copied[t] = n
            print(f"{t:16s} {n:>8d} rows", file=out)

        # sequences: SQLite's high-water mark (never below the max id)
        seqs = {r[0]: int(r[1]) for r in src.execute("SELECT name, seq FROM sqlite_sequence")}
        for t, col in IDENTITY.items():
            if t in copied:
                conn.sync_sequence(t, col)
                if t in seqs:
                    conn.set_sequence_high(t, col, seqs[t])

        # verify
        after = _target_counts(store)
        bad = {t: (copied[t], after[t]) for t in copied if copied[t] != after[t]}
        src_ids = int(src.execute("SELECT COALESCE(SUM(id),0) FROM items").fetchone()[0])
        dst_ids = int(conn.execute("SELECT COALESCE(SUM(id),0) FROM items").fetchone()[0])
        if bad or src_ids != dst_ids:
            conn.rollback()
            print(f"VERIFY FAILED counts={bad} items.id checksum {src_ids} vs {dst_ids}; "
                  "rolled back", file=out)
            return 1
        elapsed = time.monotonic() - t0
        if apply:
            conn.commit()
            print(f"COMMITTED {sum(copied.values())} rows in {elapsed:.1f}s", file=out)
        else:
            conn.rollback()
            print(f"DRY RUN OK: {sum(copied.values())} rows would be copied "
                  f"({elapsed:.1f}s); rolled back. Re-run with --apply.", file=out)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", type=Path, default=None,
                    help="SQLite file (default: library.db from config.yaml)")
    ap.add_argument("--apply", action="store_true", help="commit (default: dry run)")
    ap.add_argument("--truncate", action="store_true",
                    help="wipe a non-empty target before copying")
    ap.add_argument("--dsn", default=None,
                    help="Postgres URL (default: DATABASE_URL); for a local test target")
    ap.add_argument("--password", default=None,
                    help="Postgres password (default: DATABASE_PASSWORD or the secret)")
    args = ap.parse_args(argv)

    cfg = load_config()
    source = args.source or (ROOT / cfg.library.db)
    if not source.exists():
        print(f"no such SQLite file: {source}", file=sys.stderr)
        return 2
    settings = database_settings()
    dsn = args.dsn or (settings.url if settings else None)
    if not dsn:
        print("no target: set DATABASE_URL (and DB_PASS_SECRET_ARN) or pass --dsn",
              file=sys.stderr)
        return 2
    password = args.password
    if password is None and settings is not None and dsn == settings.url:
        password = resolve_password(settings, cfg.media.aws_profile)

    print(f"source: {source}\ntarget: {dsn.split('@')[-1]}  "
          f"({'APPLY' if args.apply else 'dry run'})")
    src = _source(source)
    store = Store(dsn=dsn, password=password)
    try:
        return copy(src, store, apply=args.apply, truncate=args.truncate)
    finally:
        store.close()
        src.close()


if __name__ == "__main__":
    raise SystemExit(main())

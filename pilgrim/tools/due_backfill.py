"""Backfill `requests.due_at` from the station log (RADIO §11, §14).

    python -m pilgrim.tools.due_backfill            # dry run: table only
    python -m pilgrim.tools.due_backfill --apply    # .backup station.db, then write

`due_at` is the wall time a request's song was scheduled to air when it was
teed up (committed or spliced). Going forward the scheduler stamps it; this
tool recovers it for rows that aired before the column existed, so their
board Prep Time is capped the same way (a frozen station — IDLE.md D3 — is
not prep).

For every `aired` row with `aired_at` and no `due_at`:

    T   = the last `request.committed` / `request.spliced` log line for the
          request at or before `aired_at` (tee-up)
    A   = `aired_at`
    due = A - (time the playhead was frozen between T and A)

While frozen the playhead does not move, so the song's distance from the
playhead at T equals the running time between T and A. Frozen spans come
from `program.frozen` -> `station.wake` pairs; a `station.startup` also ends
a span (a restart thaws without a wake line). A row with no tee-up line in
the retained logs is left NULL (its metric stays `aired - prep`, as today).

Safe while the station runs: read-only without `--apply`; with it, one
short UPDATE per row after a SQLite `.backup`. Never touches items, the
library or any other column (hard rule 5).
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pilgrim.config import ROOT, load_config

_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+\w+\s+\S+\s+(\S+)(.*)$")
_RID = re.compile(r"\brequest_id=(\d+)\b")

TEE_UP_EVENTS = ("request.committed", "request.spliced")


def _ts(iso_z: str) -> float:
    return datetime.fromisoformat(iso_z.replace("Z", "+00:00")).timestamp()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def parse_log_lines(lines: list[str]) -> tuple[dict[int, list[float]], list[tuple[float, float]]]:
    """(tee-ups per request id, frozen spans) from station log lines, in any
    file order; events are sorted by timestamp before pairing."""
    teeups: dict[int, list[float]] = {}
    events: list[tuple[float, str]] = []
    for line in lines:
        m = _LINE.match(line)
        if not m:
            continue
        t, event, rest = _ts(m.group(1)), m.group(2), m.group(3)
        if event in TEE_UP_EVENTS:
            r = _RID.search(rest)
            if r:
                teeups.setdefault(int(r.group(1)), []).append(t)
        elif event in ("program.frozen", "station.wake", "station.startup"):
            events.append((t, event))
    events.sort()
    spans: list[tuple[float, float]] = []
    frozen_at: float | None = None
    for t, event in events:
        if event == "program.frozen":
            if frozen_at is None:
                frozen_at = t
        elif frozen_at is not None:           # wake or restart ends the span
            spans.append((frozen_at, t))
            frozen_at = None
    for v in teeups.values():
        v.sort()
    return teeups, spans


def frozen_between(spans: list[tuple[float, float]], start: float, end: float) -> float:
    return sum(max(0.0, min(w, end) - max(f, start)) for f, w in spans if w > start and f < end)


def read_logs(log_dir: Path, name: str) -> list[str]:
    lines: list[str] = []
    for p in sorted(log_dir.glob(f"{name}*")):
        try:
            lines.extend(p.read_text(errors="replace").splitlines())
        except OSError:
            continue
    return lines


def plan(db_path: Path, log_lines: list[str]) -> list[dict[str, Any]]:
    """One row per aired request still without `due_at`: what would be written."""
    teeups, spans = parse_log_lines(log_lines)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(requests)")}
        # Before the station has restarted onto the new code the column does
        # not exist yet: a dry run still plans every aired row; --apply
        # refuses until the Store migration has added it.
        where = ("status='aired' AND aired_at IS NOT NULL"
                 + (" AND due_at IS NULL" if "due_at" in cols else ""))
        rows = [dict(r) for r in con.execute(
            f"SELECT id, aired_at, prep_started_at FROM requests WHERE {where} ORDER BY id")]
    finally:
        con.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        aired = datetime.fromisoformat(r["aired_at"]).timestamp()
        tees = [t for t in teeups.get(int(r["id"]), []) if t <= aired + 1.0]
        if not tees:
            out.append({"id": int(r["id"]), "due_at": None, "capped_s": None, "note": "no log"})
            continue
        frozen = frozen_between(spans, tees[-1], aired)
        due = aired - frozen
        prep = (datetime.fromisoformat(r["prep_started_at"]).timestamp()
                if r.get("prep_started_at") else None)
        out.append({"id": int(r["id"]), "due_at": _iso(due), "capped_s": round(frozen),
                    "wait_before_s": round(aired - prep) if prep is not None else None,
                    "wait_after_s": round(due - prep) if prep is not None else None,
                    "note": "" if frozen else "no freeze"})
    return out


def backup_db(db_path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%SZ")
    dst = db_path.with_name(f"{db_path.name}.due-backfill-{stamp}.bak")
    src = sqlite3.connect(str(db_path))
    out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dst


def apply(db_path: Path, rows: list[dict[str, Any]]) -> int:
    con = sqlite3.connect(str(db_path), timeout=30)
    n = 0
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(requests)")}
        if "due_at" not in cols:
            raise SystemExit("requests.due_at missing: restart the station once "
                             "(Store migration) before --apply")
        for r in rows:
            if not r.get("due_at"):
                continue
            con.execute("UPDATE requests SET due_at=? WHERE id=? AND due_at IS NULL",
                        (r["due_at"], r["id"]))
            con.commit()
            n += 1
    finally:
        con.close()
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write due_at (after a .backup)")
    ap.add_argument("--db", type=Path, default=None, help="station.db (default: config)")
    ap.add_argument("--logs", type=Path, default=None, help="log dir (default: config)")
    args = ap.parse_args(argv)
    cfg = load_config()
    db_path = args.db or (ROOT / cfg.library.db)
    log_dir = args.logs or (ROOT / cfg.logging.dir)
    rows = plan(db_path, read_logs(log_dir, cfg.logging.file))
    print(f"{'id':>6} {'capped':>8} {'before':>8} {'after':>8}  due_at / note")
    for r in rows:
        if r["due_at"] is None:
            print(f"{r['id']:>6} {'-':>8} {'-':>8} {'-':>8}  {r['note']}")
            continue
        b = "-" if r["wait_before_s"] is None else r["wait_before_s"]
        a = "-" if r["wait_after_s"] is None else r["wait_after_s"]
        print(f"{r['id']:>6} {r['capped_s']:>8} {b:>8} {a:>8}  {r['due_at']} {r['note']}")
    writable = [r for r in rows if r["due_at"]]
    print(f"{len(rows)} rows without due_at; {len(writable)} recoverable from the log; "
          f"{sum(1 for r in writable if r['capped_s'])} spanned a freeze")
    if not args.apply:
        print("dry run; add --apply to write")
        return 0
    if not writable:
        return 0
    print(f"backup: {backup_db(db_path)}")
    print(f"updated {apply(db_path, writable)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())

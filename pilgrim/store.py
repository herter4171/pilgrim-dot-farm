"""station.db: inventory metadata + airplay ledger + committed program (RADIO.md §7, §11)."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    """Thin wrapper over SQLite. All writes go through a single connection per thread."""

    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = __import__("threading").Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    -- song|commercial|liner|dj_talk|news|station_id|emergency
                    type TEXT NOT NULL,
                    media_path TEXT NOT NULL,
                    duration_s REAL NOT NULL,
                    title TEXT,
                    artist TEXT,
                    genre TEXT,
                    sample_rate INTEGER,
                    channels INTEGER,
                    role TEXT,
                    evergreen INTEGER DEFAULT 0,
                    emergency INTEGER DEFAULT 0,
                    fresh INTEGER DEFAULT 1,       -- 1 = unaired
                    expires_at TEXT,
                    gravity TEXT,                  -- serious|normal
                    meta_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS program (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    duration_s REAL NOT NULL,
                    committed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS airplay (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    item_type TEXT,
                    started_at REAL,
                    position REAL,
                    underrun INTEGER DEFAULT 0,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',  -- queued|evicted|rejected|serviced
                    reason TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_items_type ON items(type);
                CREATE INDEX IF NOT EXISTS idx_items_fresh ON items(fresh);
                CREATE INDEX IF NOT EXISTS idx_program_seq ON program(seq);
                CREATE INDEX IF NOT EXISTS idx_airplay_item ON airplay(item_id);
                CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------ items
    def add_item(self, *, type_: str, media_path: str, duration_s: float,
                 title: str | None = None, artist: str | None = None,
                 genre: str | None = None, sample_rate: int | None = None,
                 channels: int | None = None, role: str | None = None,
                 evergreen: bool = False, emergency: bool = False,
                 fresh: bool = True, expires_at: str | None = None,
                 gravity: str | None = None, meta: dict | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO items
                   (type, media_path, duration_s, title, artist, genre, sample_rate,
                    channels, role, evergreen, emergency, fresh, expires_at, gravity,
                    meta_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (type_, media_path, duration_s, title, artist, genre, sample_rate,
                 channels, role, 1 if evergreen else 0, 1 if emergency else 0,
                 1 if fresh else 0, expires_at, gravity,
                 json.dumps(meta) if meta else None, _now_iso()))
            self._conn.commit()
            rid = cur.lastrowid
            assert rid is not None
            return int(rid)

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return dict(r) if r else None

    def list_items(self, type_: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM items"
        args: tuple = ()
        if type_:
            q += " WHERE type=?"
            args = (type_,)
        q += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def count_fresh_of_type(self, type_: str) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM items WHERE type=? AND fresh=1 AND emergency=0",
                (type_,)).fetchone()[0])

    def count_usable_of_type(self, type_: str) -> int:
        """Evergreen types recycle: any non-emergency item is usable whatever its
        fresh flag (RADIO.md §5.4 fallback chain)."""
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM items WHERE type=? AND emergency=0",
                (type_,)).fetchone()[0])

    def mark_aired(self, item_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE items SET fresh=0 WHERE id=?", (item_id,))
            self._conn.commit()

    def last_played_at(self, item_id: int) -> float | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT MAX(recorded_at) FROM airplay WHERE item_id=?", (item_id,)).fetchone()
        return r[0]

    def play_count(self, item_id: int) -> int:
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) FROM airplay WHERE item_id=?", (item_id,)).fetchone()
        return int(r[0])

    # --------------------------------------------------------------- program
    def append_program(self, item_id: int, type_: str, duration_s: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO program (item_id, type, duration_s, committed_at) VALUES (?,?,?,?)",
                (item_id, type_, duration_s, _now_iso()))
            self._conn.commit()
            rid = cur.lastrowid
            assert rid is not None
            return int(rid)

    def program_after(self, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT p.seq, p.item_id, p.type AS type, p.duration_s "
                "FROM program p WHERE p.seq>? ORDER BY p.seq", (seq,)).fetchall()
        return [dict(r) for r in rows]

    def max_seq(self) -> int | None:
        with self._lock:
            r = self._conn.execute("SELECT COALESCE(MAX(seq),0) FROM program").fetchone()
        return int(r[0]) if r and r[0] else 0

    def truncate_program_before(self, seq: int) -> None:
        """Drop committed rows older than `seq` (keeps the live window bounded)."""
        with self._lock:
            self._conn.execute("DELETE FROM program WHERE seq<?", (seq,))
            self._conn.commit()

    def program_since(self, since_seq: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT p.seq, p.item_id, p.type AS type, p.duration_s "
                "FROM program p WHERE p.seq>=? ORDER BY p.seq", (since_seq,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- airplay
    def record_airplay(self, item_id: int, seq: int, item_type: str | None,
                       started_at: float | None, position: float | None,
                       underrun: int = 0) -> int:
        with self._lock:
            cur = self._conn.execute(
                ("INSERT INTO airplay (item_id, seq, item_type, started_at, position, "
                 "underrun, recorded_at) VALUES (?,?,?,?,?,?,?)"),
                (item_id, seq, item_type, started_at, position, underrun, _now_iso()))
            self._conn.commit()
            rid = cur.lastrowid
            assert rid is not None
            return int(rid)

    def recent_airplay(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM airplay ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def last_air_type_sequence(self, program_slice: list[dict[str, Any]]) -> list[str]:
        """Map most recent committed items to their types in program order."""
        return [p["item_type"] for p in program_slice]

    # -------------------------------------------------------------- requests
    def add_request(self, text: str, cap: int, status: str = "queued",
                    reason: str | None = None) -> dict[str, Any]:
        """Insert a listener request, then apply FIFO eviction. If more than
        `cap` are queued, the oldest ones fall out (marked 'evicted'). Returns
        the stored row."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO requests (text, status, reason, created_at) VALUES (?,?,?,?)",
                (text, status, reason, _now_iso()))
            self._conn.commit()
            rid = int(cur.lastrowid or 0)
        self._evict_overflow(cap)
        r = self.get_request(rid)
        assert r is not None
        return r

    def get_request(self, request_id: int) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        return dict(r) if r else None

    def _evict_overflow(self, cap: int) -> None:
        """FIFO: when queued requests exceed the cap, the oldest fall out
        ('ass end' drops) so the queue always holds the latest `cap`."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM requests WHERE status='queued' ORDER BY id ASC").fetchall()
            if len(rows) > cap:
                drop = len(rows) - cap
                ids = [r[0] for r in rows[:drop]]
                for i in ids:
                    self._conn.execute(
                        "UPDATE requests SET status='evicted', reason='fifo over cap' WHERE id=?",
                        (i,))
                self._conn.commit()

    def queued_requests(self, cap: int | None = None) -> list[dict[str, Any]]:
        """Live queue in FIFO order (oldest first). Rejected/evicted/serviced
        requests are not part of the air-able queue."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requests WHERE status='queued' ORDER BY id ASC").fetchall()
        out = [dict(r) for r in rows]
        return out[:cap] if cap else out

    def oldest_queued_request(self) -> dict[str, Any] | None:
        """Oldest still-queued request (the next one to be serviced on air)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM requests WHERE status='queued' ORDER BY id ASC LIMIT 1").fetchone()
        return dict(r) if r else None

    def mark_serviced(self, request_id: int) -> None:
        """Mark a request serviced (read on air) — removes it from the live queue."""
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='serviced', reason='serviced on air' WHERE id=?",
                (request_id,))
            self._conn.commit()

    def all_requests(self, limit: int = 500) -> list[dict[str, Any]]:
        """Full request ledger (all statuses), newest first — for audit/history."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

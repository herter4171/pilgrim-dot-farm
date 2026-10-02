"""station.db: inventory metadata + airplay ledger + committed program (RADIO.md §7, §11)."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_epoch(iso: str | None) -> float | None:
    """Parse an ISO-8601 timestamp into a wall-clock epoch (PRIORITIES §4)."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return None


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
                    -- queued|producing|ready|aired|rejected|evicted|failed (OVERHAUL 4.4)
                    status TEXT NOT NULL DEFAULT 'queued',
                    reason TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_items_type ON items(type);
                CREATE INDEX IF NOT EXISTS idx_items_fresh ON items(fresh);
                CREATE INDEX IF NOT EXISTS idx_program_seq ON program(seq);
                CREATE INDEX IF NOT EXISTS idx_airplay_item ON airplay(item_id);
                CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
                -- Persistent unique-visitor dedup (RADIO §14): raw canonical
                -- client IPs. Supersedes the HMAC-only visitor_signatures table.
                CREATE TABLE IF NOT EXISTS visitor_ips (
                    ip TEXT PRIMARY KEY,
                    first_seen REAL NOT NULL DEFAULT (strftime('%s','now'))
                );
                -- DJ console (TUI.md §4): small key/value store for counters
                -- like catalogue_revision (bumped when the song catalogue set
                -- changes so the console can detect a stale page).
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                DROP TABLE IF EXISTS visitor_signatures;
                """
            )
            # idempotent migration (OVERHAUL 2.2): retire truncated inventory
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(items)")}
            if "retired" not in cols:
                self._conn.execute(
                    "ALTER TABLE items ADD COLUMN retired INTEGER DEFAULT 0")
            # idempotent migration (PRIORITIES §4): persisted airplay so a
            # restart doesn't reset song rotation (replaces the in-memory map).
            # last_aired_at is a wall-clock epoch set at commit; NULL = never aired
            # (or aired before the ledger existed) -> treated as unheard forever.
            if "last_aired_at" not in cols:
                self._conn.execute("ALTER TABLE items ADD COLUMN last_aired_at REAL")
            if "play_count" not in cols:
                self._conn.execute(
                    "ALTER TABLE items ADD COLUMN play_count INTEGER DEFAULT 0")
            # idempotent migration (OVERHAUL 4.4): request lifecycle columns
            req_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(requests)")}
            for col, ddl in (("attempts", "INTEGER DEFAULT 0"),
                             ("song_item_id", "INTEGER"),
                             ("intro_item_id", "INTEGER"),
                             ("updated_at", "TEXT")):
                if col not in req_cols:
                    self._conn.execute(f"ALTER TABLE requests ADD COLUMN {col} {ddl}")
            self._conn.commit()

    # ------------------------------------------------------------------ items
    def add_item(self, *, type_: str, media_path: str, duration_s: float,
                 title: str | None = None, artist: str | None = None,
                 genre: str | None = None, sample_rate: int | None = None,
                 channels: int | None = None, role: str | None = None,
                 evergreen: bool = False, emergency: bool = False,
                 fresh: bool = True, expires_at: str | None = None,
                 gravity: str | None = None, meta: dict | None = None,
                 created_at: str | None = None) -> int:
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
                 json.dumps(meta) if meta else None, created_at or _now_iso()))
            rid = cur.lastrowid
            assert rid is not None
            if type_ == "song":
                self._bump_catalogue()
            self._conn.commit()
            return int(rid)

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return dict(r) if r else None

    def list_items(self, type_: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM items WHERE retired=0"
        args: tuple = ()
        if type_:
            q += " AND type=?"
            args = (type_,)
        q += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def count_fresh_of_type(self, type_: str) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM items WHERE type=? AND fresh=1 AND emergency=0 "
                "AND retired=0",
                (type_,)).fetchone()[0])

    def count_usable_of_type(self, type_: str) -> int:
        """Evergreen types recycle: any non-emergency item is usable whatever its
        fresh flag (RADIO.md §5.4 fallback chain).
        """
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM items WHERE type=? AND emergency=0 AND retired=0",
                (type_,)).fetchone()[0])

    def retire_item(self, item_id: int, reason: str) -> None:
        """Flag an item retired (excluded from playout + production) with the
        reason merged into its meta. Rows stay for history; files stay on disk.
        """
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not r:
                return
            meta = json.loads(r["meta_json"]) if r["meta_json"] else {}
            meta["retired_reason"] = reason
            self._conn.execute(
                "UPDATE items SET retired=1, meta_json=? WHERE id=?",
                (json.dumps(meta), item_id))
            if r["type"] == "song":
                self._bump_catalogue()
            self._conn.commit()

    def update_item_meta(self, item_id: int, meta: dict) -> None:
        """Replace an item's meta (e.g. SFX overlays attached after storage)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT type FROM items WHERE id=?", (item_id,)).fetchone()
            self._conn.execute("UPDATE items SET meta_json=? WHERE id=?",
                               (json.dumps(meta) if meta else None, item_id))
            # catalogue membership (a ``song`` set member) can change via meta in
            # future edit paths; keep the console's revision honest about the set
            if r and r["type"] == "song":
                self._bump_catalogue()
            self._conn.commit()

    # ------------------------------------------------------- DJ catalogue (11)
    def _bump_catalogue(self) -> None:
        """Increment catalogue_revision (under the store lock). Bumped only when
        the **song** catalogue set changes (add/retire/edit), so the console can
        detect a stale page and refetch (TUI.md §4)."""
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES ('catalogue_revision','1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")

    def catalogue_revision(self) -> int:
        with self._lock:
            return self._catalogue_revision_locked()

    def _catalogue_revision_locked(self) -> int:
        r = self._conn.execute(
            "SELECT value FROM meta WHERE key='catalogue_revision'").fetchone()
        return int(r[0]) if r and r[0] else 0

    def song_catalogue(self, q: str | None, sort: str, direction: str,
                       cursor: tuple[str, str, int] | None,
                       limit: int) -> tuple[list[dict[str, Any]],
                                            tuple[str, str, int] | None, int]:
        """All non-retired, non-emergency library songs (fresh AND already
        aired; TUI.md §2). `sort` is allowlisted title|artist; the tie-break is
        the other column then id, so sorting never reorders or duplicates rows
        across pages. The cursor is (sort_value, other_value, id) of the last
        row returned. Returns (rows, next_cursor, catalogue_revision); fetches
        one extra row to know whether more remain. The only interpolated SQL
        is the allowlisted column/direction pair (AGENTS §5)."""
        if sort not in ("title", "artist") or direction not in ("asc", "desc"):
            raise ValueError("invalid sort/direction")
        order = "ASC" if direction == "asc" else "DESC"
        other = "artist" if sort == "title" else "title"
        where = ["type='song'", "retired=0", "emergency=0"]
        args: list[Any] = []
        if q:
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{esc}%"
            where.append("(title LIKE ? ESCAPE '\\' COLLATE NOCASE OR "
                         "artist LIKE ? ESCAPE '\\' COLLATE NOCASE)")
            args += [like, like]
        if cursor:
            p0, s0, i0 = cursor
            op = ">" if direction == "asc" else "<"
            where.append(
                f"({sort} COLLATE NOCASE {op} ? OR ({sort} COLLATE NOCASE = ? AND "
                f"({other} COLLATE NOCASE {op} ? OR ({other} COLLATE NOCASE = ? "
                f"AND id {op} ?))))")
            args += [p0, p0, s0, s0, i0]
        sql = (f"SELECT id, title, artist, duration_s, fresh, last_aired_at "
               f"FROM items WHERE {' AND '.join(where)} "
               f"ORDER BY {sort} COLLATE NOCASE {order}, "
               f"{other} COLLATE NOCASE {order}, id {order} LIMIT ?")
        with self._lock:
            rows = self._conn.execute(sql, args + [limit + 1]).fetchall()
            rev = self._catalogue_revision_locked()
        out = [dict(r) for r in rows[:limit]]
        nxt = None
        if len(rows) > limit:
            last = rows[limit - 1]
            nxt = (str(last[sort] or ""), str(last[other] or ""), int(last["id"]))
        return out, nxt, rev

    def mark_aired(self, item_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE items SET fresh=0 WHERE id=?", (item_id,))
            self._conn.commit()

    def commit_song_air(self, item_id: int, wall: float) -> None:
        """Persist a song's air at commit time (PRIORITIES §4). `wall` is the
        injected clock's wall epoch (never time.time()). Spacing and the
        recycled-song weighting read last_aired_at, so a restart keeps the
        rotation instead of resetting it. play_count feeds the stats endpoint."""
        with self._lock:
            self._conn.execute(
                "UPDATE items SET last_aired_at=?, play_count=play_count+1 WHERE id=?",
                (wall, item_id))
            self._conn.commit()

    def backfill_airplay_history(self) -> None:
        """One-time backfill (PRIORITIES §4): for items that predate the
        last_aired_at/play_count columns, seed them from the airplay ledger so
        the live pool starts with real history rather than all-NULL. Rerunnable
        and safe: only NULL last_aired_at rows are touched, so live values set
        by read/replay of a given song survive."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_id, COUNT(*) AS plays, MAX(recorded_at) AS last "
                "FROM airplay GROUP BY item_id").fetchall()
            for r in rows:
                last = _parse_epoch(r["last"])
                self._conn.execute(
                    "UPDATE items SET last_aired_at=?, play_count=? "
                    "WHERE id=? AND last_aired_at IS NULL",
                    (last, r["plays"], r["item_id"]))
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
    def clear_program(self) -> None:
        """Wipe the committed program. Used on station start: the committed
        program is live-only state referencing a clock that no longer exists
        (OVERHAUL 2.4)."""
        with self._lock:
            self._conn.execute("DELETE FROM program")
            self._conn.commit()

    def append_program(self, item_id: int, type_: str, duration_s: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO program (item_id, type, duration_s, committed_at) VALUES (?,?,?,?)",
                (item_id, type_, duration_s, _now_iso()))
            self._conn.commit()
            rid = cur.lastrowid
            assert rid is not None
            return int(rid)

    def append_program_at(self, *, item_id: int, type_: str, duration_s: float,
                          seq: int) -> int:
        """Insert a committed program row at an explicit seq (operator-phrase
        head-of-queue splicing, scheduler.place_phrase). Caller must have shifted
        later rows up (``shift_program_up_from``) so ``seq`` is free."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO program (seq, item_id, type, duration_s, committed_at) "
                "VALUES (?,?,?,?,?)",
                (seq, item_id, type_, duration_s, _now_iso()))
            self._conn.commit()
        return int(seq)

    def shift_program_up_from(self, seq: int) -> None:
        """Shift all committed rows with seq >= ``seq`` up by one, descending so
        the AUTOINCREMENT primary key never collides. Used to open a slot for a
        head-of-queue operator phrase (scheduler.place_phrase). Only future rows
        move (already-aired seqs are below the splice point and untouched), so
        airplay ledger references stay valid."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq FROM program WHERE seq >= ? ORDER BY seq DESC",
                (seq,)).fetchall()
            for (s,) in rows:
                self._conn.execute("UPDATE program SET seq=? WHERE seq=?", (s + 1, s))
            self._conn.commit()

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

    # ------------------------------------------------------------ visitors (14)
    def register_visitor(self, ip: str) -> int:
        """Idempotently record a canonical client IP (insert-on-conflict no-op)
        and return the unique count, all under the store's lock so a concurrent
        first-seen IP can't double-count (RADIO §14)."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO visitor_ips (ip) VALUES (?)", (ip,))
            n = self._conn.execute(
                "SELECT COUNT(*) FROM visitor_ips").fetchone()[0]
            self._conn.commit()
        return int(n)

    def unique_visitors(self) -> int:
        with self._lock:
            n = self._conn.execute(
                "SELECT COUNT(*) FROM visitor_ips").fetchone()[0]
        return int(n)

    # -------------------------------------------------------------- requests
    def add_request(self, text: str, cap: int | None, status: str = "queued",
                    reason: str | None = None) -> dict[str, Any]:
        """Insert a listener request, then apply FIFO eviction. If more than
        `cap` are queued, the oldest QUEUED ones fall out (marked 'evicted').
        producing/ready/failed rows are never evicted (OVERHAUL 4.4).
        `cap=None` disables eviction (the public line: every accepted request
        gets made). Returns the stored row."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO requests (text, status, reason, created_at, updated_at) "
                "VALUES (?,?,?,?,?)",
                (text, status, reason, _now_iso(), _now_iso()))
            self._conn.commit()
            rid = int(cur.lastrowid or 0)
        if cap is not None:
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
        """FIFO: when QUEUED requests exceed the cap, the oldest fall out
        ('ass end' drops) so the queue always holds the latest `cap`. Only
        `queued` rows are evicted — never producing/ready/failed (OVERHAUL 4.4)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM requests WHERE status='queued' ORDER BY id ASC").fetchall()
            if len(rows) > cap:
                drop = len(rows) - cap
                ids = [r[0] for r in rows[:drop]]
                for i in ids:
                    self._conn.execute(
                        "UPDATE requests SET status='evicted', reason='fifo over cap' "
                        "WHERE id=?", (i,))
                self._conn.commit()

    def queued_requests(self, cap: int | None = None) -> list[dict[str, Any]]:
        """Live queue in FIFO order (oldest first). Rejected/evicted/airéd/...
        requests are not part of the air-able queue."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requests WHERE status='queued' ORDER BY id ASC").fetchall()
        out = [dict(r) for r in rows]
        return out[:cap] if cap else out

    def oldest_queued_request(self) -> dict[str, Any] | None:
        """Oldest still-queued request (the next one to be serviced on air)."""
        return self.next_request_to_produce()

    # --------------------------------------------------- request lifecycle (4.4)
    def next_request_to_produce(self) -> dict[str, Any] | None:
        """Oldest `queued` request — the next one to become a song."""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM requests WHERE status='queued' ORDER BY id ASC LIMIT 1"
            ).fetchone()
        return dict(r) if r else None

    def mark_request_producing(self, request_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='producing', updated_at=? WHERE id=?",
                (_now_iso(), request_id))
            self._conn.commit()

    def mark_request_ready(self, request_id: int, song_item_id: int | None,
                           intro_item_id: int | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='ready', song_item_id=?, intro_item_id=?, "
                "updated_at=? WHERE id=?",
                (song_item_id, intro_item_id, _now_iso(), request_id))
            self._conn.commit()

    def mark_request_aired(self, request_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='aired', updated_at=? WHERE id=?",
                (_now_iso(), request_id))
            self._conn.commit()

    def request_failed_attempt(self, request_id: int, max_attempts: int = 3) -> int:
        """Record a failed production attempt. Back to `queued` until
        `max_attempts`, then `failed` (head-of-line blocking disappears — the
        next request can be produced; OVERHAUL 4.4). Returns the new attempt #."""
        with self._lock:
            r = self._conn.execute(
                "SELECT attempts FROM requests WHERE id=?", (request_id,)).fetchone()
            n = int(r[0] if r and r[0] else 0) + 1
            status = "failed" if n >= max_attempts else "queued"
            self._conn.execute(
                "UPDATE requests SET attempts=?, status=?, updated_at=? WHERE id=?",
                (n, status, _now_iso(), request_id))
            self._conn.commit()
        return n

    def requeue_request(self, request_id: int) -> None:
        """Backend unreachable (transport error): back to `queued` WITHOUT
        spending an attempt — an outage must not burn listener requests."""
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='queued', updated_at=? "
                "WHERE id=? AND status='producing'", (_now_iso(), request_id))
            self._conn.commit()

    def ready_request_songs(self) -> list[dict[str, Any]]:
        """`ready` requests, oldest first (their songs are ready to air)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requests WHERE status='ready' ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]

    def reset_producing_to_queued(self) -> None:
        """Startup: a crash mid-generation must not strand a request in
        `producing` (OVERHAUL 4.4)."""
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='queued', updated_at=? "
                "WHERE status='producing'", (_now_iso(),))
            self._conn.commit()

    def request_board(self, cap: int) -> dict[str, Any]:
        """Board for the UI: live queue (queued+producing+ready, oldest first,
        <= cap) plus the most recent aired requests with their song titles."""
        with self._lock:
            queue = [dict(r) for r in self._conn.execute(
                "SELECT * FROM requests WHERE status IN ('queued','producing','ready') "
                "ORDER BY id ASC LIMIT ?", (cap,)).fetchall()]
            recent = [dict(r) for r in self._conn.execute(
                "SELECT r.id, r.text, r.created_at, i.title AS song_title, "
                "i.artist AS song_artist FROM requests r "
                "LEFT JOIN items i ON i.id = r.song_item_id "
                "WHERE r.status IN ('aired','serviced') "
                "ORDER BY r.id DESC LIMIT 3").fetchall()]
        return {"queue": queue, "recent": recent}

    def mark_serviced(self, request_id: int) -> None:
        """Legacy alias: marks the request as read on air (recorded as `aired`
        so reads treat it as finished; OVERHAUL 4.4)."""
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='aired', reason='serviced on air', "
                "updated_at=? WHERE id=?", (_now_iso(), request_id))
            self._conn.commit()

    def all_requests(self, limit: int = 500) -> list[dict[str, Any]]:
        """Full request ledger (all statuses), newest first — for audit/history."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

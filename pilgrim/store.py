"""station.db: inventory metadata + airplay ledger + committed program (RADIO.md §7, §11)."""
from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Queue order everywhere (PAYMENT.md §3.2): paid first, FIFO within each class
# (paid by payment time, free by creation time).
QUEUE_ORDER = "ORDER BY priority DESC, COALESCE(paid_at, created_at) ASC, id ASC"
# The same order for queries that alias requests as `r` (joined with items).
QUEUE_ORDER_R = ("ORDER BY r.priority DESC, COALESCE(r.paid_at, r.created_at) ASC, "
                 "r.id ASC")



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


def _project_text(text: str, paid: bool, max_length: int,
                  summary: str | None = None) -> str:
    """Public board text. Paid rows show the generated summary when present,
    else the first line, collapsed and cut to the request-line limit
    (PAYMENT.md §3.4); free rows are unchanged."""
    if not paid:
        return text
    if summary:
        return summary[:max_length]
    first = " ".join(text.split("\n", 1)[0].split())
    if len(first) > max_length:
        first = first[: max_length - 1] + "…"
    return first


def _wait_s(prep_started_at: str | None, aired_at: str | None) -> int | None:
    """Prep time on the board: the prep clock starts when the lyrics/brief
    are first written (`prep_started_at`), NOT at submission — time spent in
    line or waiting for the station to come back is not prep work. Rows that
    never recorded a writing start (aired before this column existed) read
    null, so the UI omits the label rather than showing queue idle time."""
    start = _parse_epoch(prep_started_at)
    end = _parse_epoch(aired_at)
    if start is None or end is None:
        return None
    return max(0, int(round(end - start)))


def _project_queue_row(row: dict[str, Any], max_length: int,
                       stages: Mapping[int, str] | None = None) -> dict[str, Any]:
    paid = bool(row.get("priority"))
    stage = ""
    if row["status"] == "producing":
        # writing = lyrics/brief, generating = song model (RADIO §11)
        stage = (stages or {}).get(int(row["id"]), "writing")
    return {"text": _project_text(row["text"], paid, max_length,
                                  row.get("summary")),
            "status": row["status"], "paid": paid, "stage": stage,
            # the song exists from `ready` on (title shown in the board table)
            "song_title": row.get("song_title") or ""}


def _project_recent_row(row: dict[str, Any], max_length: int) -> dict[str, Any]:
    paid = bool(row.get("priority"))
    return {"text": _project_text(row["text"], paid, max_length,
                                  row.get("summary")),
            "song_title": row.get("song_title") or "",
            "song_artist": row.get("song_artist") or "",
            "paid": paid,
            "wait_s": _wait_s(row.get("prep_started_at"), row.get("aired_at"))}


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
                -- Stripe webhook idempotency (PAYMENT.md §3.1): one row per
                -- event id; `outcome` is applied|ignored|mismatch|duplicate.
                CREATE TABLE IF NOT EXISTS stripe_events (
                    event_id    TEXT PRIMARY KEY,
                    type        TEXT NOT NULL,
                    session_id  TEXT,
                    outcome     TEXT NOT NULL,
                    received_at TEXT NOT NULL
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
            # idempotent migration (RADIO §8.4, §14): S3 is the media source of
            # truth. s3_key is the object key in media.bucket (NULL = not yet
            # uploaded; the API then falls back to /api/media/<id>).
            if "s3_key" not in cols:
                self._conn.execute("ALTER TABLE items ADD COLUMN s3_key TEXT")
            if "uploaded_at" not in cols:
                self._conn.execute("ALTER TABLE items ADD COLUMN uploaded_at TEXT")
            # idempotent migration (OVERHAUL 4.4 + PAYMENT.md §3.1): request
            # lifecycle + paid-priority columns. Additive only.
            req_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(requests)")}
            for col, ddl in (("attempts", "INTEGER DEFAULT 0"),
                             ("song_item_id", "INTEGER"),
                             ("intro_item_id", "INTEGER"),
                             ("updated_at", "TEXT"),
                             ("priority", "INTEGER DEFAULT 0"),
                             ("public_token", "TEXT"),
                             ("paid_at", "TEXT"),
                             ("amount_cents", "INTEGER"),
                             ("currency", "TEXT"),
                             ("checkout_session_id", "TEXT"),
                             ("payment_intent_id", "TEXT"),
                             ("livemode", "INTEGER"),
                             ("reconciled_at", "REAL"),
                             ("aired_at", "TEXT"),
                             # board "Prep Time" starts here (first lyrics write),
                             # not at created_at (queue idle must not count)
                             ("prep_started_at", "TEXT"),
                             ("summary", "TEXT"),
                             # PAYMENT.md Step 12b: staged checks + operator comp
                             ("stage", "TEXT"),
                             ("comped", "INTEGER DEFAULT 0")):
                if col not in req_cols:
                    self._conn.execute(f"ALTER TABLE requests ADD COLUMN {col} {ddl}")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_public_token "
                "ON requests(public_token) WHERE public_token IS NOT NULL")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_session "
                "ON requests(checkout_session_id) WHERE checkout_session_id IS NOT NULL")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_requests_queue "
                "ON requests(status, priority)")
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

    def items_to_publish(self, limit: int = 50) -> list[dict[str, Any]]:
        """MP3 items not yet in S3 (RADIO §8.4), oldest first. Emergency items
        are local-only (§8.3) and never published."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM items WHERE s3_key IS NULL AND emergency=0 "
                "AND media_path LIKE '%.mp3' ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def set_published(self, item_id: int, s3_key: str) -> None:
        """Record a confirmed upload. Only a successful PUT calls this."""
        with self._lock:
            self._conn.execute(
                "UPDATE items SET s3_key=?, uploaded_at=? WHERE id=?",
                (s3_key, _now_iso(), item_id))
            self._conn.commit()

    def set_media_file(self, item_id: int, *, media_path: str, duration_s: float,
                       sample_rate: int, channels: int) -> None:
        """Point an item at a re-encoded file (tools/mp3_migrate). The old file
        stays on disk (hard rule 5); any previous upload no longer matches, so
        s3_key is cleared and the uploader re-publishes."""
        with self._lock:
            self._conn.execute(
                "UPDATE items SET media_path=?, duration_s=?, sample_rate=?, "
                "channels=?, s3_key=NULL, uploaded_at=NULL WHERE id=?",
                (media_path, duration_s, sample_rate, channels, item_id))
            self._conn.commit()

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

    def count_fresh_unexpired_of_type(self, type_: str, now_wall: float) -> int:
        """Unaired, non-emergency items still valid at `now_wall`.

        Contextual types (dj_talk, field_report, news) deplete: an expired clip
        is not stock, so the selector and /api/health must agree with
        `Producer._count_fresh_unexpired` (RADIO §5.2, OVERHAUL 5.3).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT expires_at FROM items WHERE type=? AND fresh=1 "
                "AND emergency=0 AND retired=0", (type_,)).fetchall()
        n = 0
        for r in rows:
            exp = _parse_epoch(r["expires_at"])
            if exp is not None and exp <= now_wall:
                continue
            n += 1
        return n

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

    def update_program_seq(self, old_seq: int, new_seq: int) -> None:
        """Renumber one committed row (request splice). New seqs are always
        above the current high-water mark, so an old occurrence id is never
        reused for a different item (TUI.md §5)."""
        with self._lock:
            self._conn.execute(
                "UPDATE program SET seq=? WHERE seq=?", (new_seq, old_seq))
            self._conn.commit()

    def set_program_seq_high(self, seq: int) -> None:
        """Raise the AUTOINCREMENT high-water mark after a splice renumbers
        future rows; otherwise the next append could reuse a shifted seq."""
        with self._lock:
            r = self._conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='program'").fetchone()
            if r:
                self._conn.execute(
                    "UPDATE sqlite_sequence SET seq=? WHERE name='program'",
                    (max(int(r[0]), int(seq)),))
            else:
                self._conn.execute(
                    "INSERT INTO sqlite_sequence (name, seq) VALUES ('program', ?)",
                    (int(seq),))
            self._conn.commit()

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
                    reason: str | None = None, priority: int = 0) -> dict[str, Any]:
        """Insert a listener request, then apply FIFO eviction. If more than
        `cap` are queued, the oldest FREE ones fall out (marked 'evicted').
        producing/ready/failed and paid rows are never evicted (OVERHAUL 4.4,
        PAYMENT.md §3.2). `cap=None` disables eviction (the public line: every
        accepted request gets made). Returns the stored row."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO requests (text, status, reason, created_at, updated_at, priority) "
                "VALUES (?,?,?,?,?,?)",
                (text, status, reason, _now_iso(), _now_iso(), priority))
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
        """FIFO: when FREE queued requests exceed the cap, the oldest fall out
        ('ass end' drops) so the queue always holds the latest `cap`. Only
        `queued`, `priority=0` rows are evicted — never producing/ready/failed
        and never a paid request (OVERHAUL 4.4, PAYMENT.md §3.2)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM requests WHERE status='queued' AND priority=0 "
                "ORDER BY id ASC").fetchall()
            if len(rows) > cap:
                drop = len(rows) - cap
                ids = [r[0] for r in rows[:drop]]
                for i in ids:
                    self._conn.execute(
                        "UPDATE requests SET status='evicted', reason='fifo over cap' "
                        "WHERE id=?", (i,))
                self._conn.commit()

    def queued_requests(self, cap: int | None = None) -> list[dict[str, Any]]:
        """Live queue in queue order (paid first, FIFO within each class).
        Rejected/evicted/airéd/... requests are not part of the air-able queue."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM requests WHERE status='queued' {QUEUE_ORDER}").fetchall()
        out = [dict(r) for r in rows]
        return out[:cap] if cap else out

    def oldest_queued_request(self) -> dict[str, Any] | None:
        """Oldest still-queued request (the next one to be serviced on air)."""
        return self.next_request_to_produce()

    # --------------------------------------------------- request lifecycle (4.4)
    def next_request_to_produce(self) -> dict[str, Any] | None:
        """Next `queued` request in queue order (paid first, then FIFO)."""
        with self._lock:
            r = self._conn.execute(
                f"SELECT * FROM requests WHERE status='queued' {QUEUE_ORDER} LIMIT 1"
            ).fetchone()
        return dict(r) if r else None

    def mark_request_producing(self, request_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='producing', updated_at=? WHERE id=?",
                (_now_iso(), request_id))
            self._conn.commit()

    def mark_request_writing(self, request_id: int) -> None:
        """The request got its turn at the brief LLM and lyrics/brief writing
        begins: start the board's Prep Time clock (RADIO §11). Set again on a
        retry so a request that sat out an outage or a failed attempt does not
        count that idle time as prep."""
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET prep_started_at=?, updated_at=? WHERE id=?",
                (now, now, request_id))
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
                "UPDATE requests SET status='aired', aired_at=?, updated_at=? WHERE id=?",
                (_now_iso(), _now_iso(), request_id))
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

    def pending_requests(self) -> list[dict[str, Any]]:
        """Requests still awaiting air (queued, producing, or ready) in queue
        order. The scheduler holds the committed window short while one of these
        is uncommitted so it can air as soon as its song + intro are ready."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requests WHERE status IN "
                f"('queued','producing','ready') {QUEUE_ORDER}").fetchall()
        return [dict(r) for r in rows]

    def ready_request_songs(self) -> list[dict[str, Any]]:
        """`ready` requests in queue order (their songs are ready to air)."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM requests WHERE status='ready' {QUEUE_ORDER}").fetchall()
        return [dict(r) for r in rows]

    def reset_producing_to_queued(self) -> None:
        """Startup: a crash mid-generation must not strand a request in
        `producing` (OVERHAUL 4.4)."""
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='queued', updated_at=? "
                "WHERE status='producing'", (_now_iso(),))
            self._conn.commit()

    def request_board(self, cap: int, max_length: int = 160,
                      stages: Mapping[int, str] | None = None) -> dict[str, Any]:
        """Board for the UI: live queue (queued+producing+ready, queue order,
        <= cap) plus the 5 most recent aired requests with song titles and how
        long they waited. Rows are projected — never raw (PAYMENT.md §3.4)."""
        with self._lock:
            queue = [dict(r) for r in self._conn.execute(
                "SELECT r.*, i.title AS song_title FROM requests r "
                "LEFT JOIN items i ON i.id = r.song_item_id "
                "WHERE r.status IN ('queued','producing','ready') "
                f"{QUEUE_ORDER_R} LIMIT ?", (cap,)).fetchall()]
            recent = [dict(r) for r in self._conn.execute(
                "SELECT r.id, r.text, r.summary, r.created_at, r.aired_at, "
                "r.prep_started_at, r.priority, "
                "i.title AS song_title, i.artist AS song_artist FROM requests r "
                "LEFT JOIN items i ON i.id = r.song_item_id "
                "WHERE r.status IN ('aired','serviced') "
                "ORDER BY r.id DESC LIMIT 5").fetchall()]
        return {
            "queue": [_project_queue_row(r, max_length, stages) for r in queue],
            "recent": [_project_recent_row(r, max_length) for r in recent],
        }

    def mark_serviced(self, request_id: int) -> None:
        """Legacy alias: marks the request as read on air (recorded as `aired`
        so reads treat it as finished; OVERHAUL 4.4)."""
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='aired', reason='serviced on air', "
                "aired_at=?, updated_at=? WHERE id=?",
                (_now_iso(), _now_iso(), request_id))
            self._conn.commit()

    # ------------------------------------------------- paid priority (PAYMENT §3)
    def add_paid_request(self, text: str, summary: str | None = None) -> dict[str, Any]:
        """Insert a moderated paid request: awaiting payment, priority 1,
        unguessable public token, plus the board summary. Not in any queue
        until Stripe confirms."""
        token = secrets.token_urlsafe(24)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO requests (text, status, created_at, updated_at, "
                "priority, public_token, summary) VALUES (?,?,?,?,?,?,?)",
                (text, "awaiting_payment", _now_iso(), _now_iso(), 1, token,
                 summary))
            self._conn.commit()
            rid = int(cur.lastrowid or 0)
        row = self.get_request(rid)
        assert row is not None
        return row

    def waiting_free_requests(self) -> int:
        """Free requests not yet aired (queued/producing/ready): the number the
        free line caps at `requests.free_queue_max`. Paid rows never count."""
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) FROM requests WHERE COALESCE(priority, 0)=0 "
                "AND status IN ('queued','producing','ready')").fetchone()
        return int(r[0])

    def paid_rows_needing_summary(self) -> list[dict[str, Any]]:
        """Paid rows the board can show (waiting, or among the aired) —
        candidates for a summary regenerated after the 512-token cap left
        raw-text labels (Oct 31)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requests WHERE priority=1 AND status IN "
                "('queued','producing','ready','aired') "
                "ORDER BY id DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]

    def set_request_summary(self, request_id: int, summary: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE requests SET summary=?, updated_at=? WHERE id=?",
                               (summary, _now_iso(), request_id))
            self._conn.commit()

    def add_checking_request(self, text: str, *, comped: bool = False) -> dict[str, Any]:
        """Insert a prefiltered paid request whose checks run in the background
        (PAYMENT.md Step 12b): status `checking`, stage `moderation`, priority 1,
        unguessable public token. Not in any queue, not on the board."""
        token = secrets.token_urlsafe(24)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO requests (text, status, stage, created_at, updated_at, "
                "priority, public_token, comped) VALUES (?,?,?,?,?,?,?,?)",
                (text, "checking", "moderation", _now_iso(), _now_iso(), 1, token,
                 1 if comped else 0))
            self._conn.commit()
            rid = int(cur.lastrowid or 0)
        row = self.get_request(rid)
        assert row is not None
        return row

    def set_request_stage(self, request_id: int, stage: str,
                          summary: str | None = None) -> bool:
        """Advance a `checking` row to its next stage (moderation -> copyright
        -> ready). False when the row is no longer checking."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET stage=?, summary=COALESCE(?, summary), "
                "updated_at=? WHERE id=? AND status='checking'",
                (stage, summary, _now_iso(), request_id))
            self._conn.commit()
            return cur.rowcount > 0

    def reject_checking_request(self, request_id: int, reason: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET status='rejected', reason=?, stage=NULL, "
                "updated_at=? WHERE id=? AND status='checking'",
                (reason, _now_iso(), request_id))
            self._conn.commit()
            return cur.rowcount > 0

    def claim_for_checkout(self, request_id: int) -> bool:
        """`checking`/`ready` -> `awaiting_payment`, exactly once, so a double
        click cannot create two Checkout Sessions. Comped rows never pay."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET status='awaiting_payment', stage=NULL, "
                "updated_at=? WHERE id=? AND status='checking' AND stage='ready' "
                "AND COALESCE(comped, 0)=0", (_now_iso(), request_id))
            self._conn.commit()
            return cur.rowcount > 0

    def mark_request_comped(self, request_id: int) -> bool:
        """Operator comp (PAYMENT.md Step 12b): a checked `comped` row joins
        the priority queue with no Stripe session and amount 0."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET status='queued', stage=NULL, paid_at=?, "
                "amount_cents=0, livemode=0, updated_at=? WHERE id=? "
                "AND status='checking' AND comped=1", (_now_iso(), _now_iso(), request_id))
            self._conn.commit()
            return cur.rowcount > 0

    def abandon_checking_requests(self) -> int:
        """At startup: a `checking` row's background task died with the old
        process, so nothing will ever advance it."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET status='abandoned', stage=NULL, updated_at=? "
                "WHERE status='checking'", (_now_iso(),))
            self._conn.commit()
            return cur.rowcount

    def attach_checkout(self, request_id: int, session_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET checkout_session_id=?, updated_at=? WHERE id=?",
                (session_id, _now_iso(), request_id))
            self._conn.commit()

    def get_request_by_token(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM requests WHERE public_token=?", (token,)).fetchone()
        return dict(r) if r else None

    def get_request_by_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM requests WHERE checkout_session_id=?", (session_id,)).fetchone()
        return dict(r) if r else None

    def mark_request_paid(self, request_id: int, *, session_id: str,
                          payment_intent_id: str | None, amount_cents: int,
                          currency: str, livemode: bool) -> bool:
        """The single transition into the priority queue. Only from
        awaiting_payment/abandoned, and only for the matching session. True
        only when a row actually changed (idempotent)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET status='queued', paid_at=?, amount_cents=?, "
                "currency=?, payment_intent_id=?, livemode=?, updated_at=? "
                "WHERE id=? AND checkout_session_id=? "
                "AND status IN ('awaiting_payment','abandoned')",
                (_now_iso(), amount_cents, currency, payment_intent_id,
                 1 if livemode else 0, _now_iso(), request_id, session_id))
            self._conn.commit()
            return cur.rowcount > 0

    def mark_request_abandoned(self, request_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET status='abandoned', updated_at=? "
                "WHERE id=? AND status='awaiting_payment'", (_now_iso(), request_id))
            self._conn.commit()
            return cur.rowcount > 0

    def abandon_stale_checkouts(self, older_than_iso: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET status='abandoned', updated_at=? "
                "WHERE status='awaiting_payment' AND created_at < ?",
                (_now_iso(), older_than_iso))
            self._conn.commit()
            return cur.rowcount

    def record_stripe_event(self, event_id: str, type_: str, session_id: str | None,
                            outcome: str) -> bool:
        """INSERT OR IGNORE; False means this event id was already seen."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO stripe_events "
                "(event_id, type, session_id, outcome, received_at) VALUES (?,?,?,?,?)",
                (event_id, type_, session_id, outcome, _now_iso()))
            self._conn.commit()
            return cur.rowcount > 0

    def update_stripe_event_outcome(self, event_id: str, outcome: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE stripe_events SET outcome=? WHERE event_id=?", (outcome, event_id))
            self._conn.commit()

    def touch_reconciled(self, request_id: int, at: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET reconciled_at=? WHERE id=?", (at, request_id))
            self._conn.commit()

    def request_position(self, request_id: int) -> int | None:
        """1-based place in the combined live queue, else None."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM requests WHERE status IN ('queued','producing','ready') "
                f"{QUEUE_ORDER}").fetchall()
        for index, row in enumerate(rows, start=1):
            if row[0] == request_id:
                return index
        return None

    def paid_requests(self, statuses: list[str], limit: int = 50) -> list[dict[str, Any]]:
        if not statuses:
            return []
        marks = ",".join("?" * len(statuses))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM requests WHERE priority=1 AND status IN ({marks}) "
                "ORDER BY id DESC LIMIT ?", (*statuses, limit)).fetchall()
        return [dict(r) for r in rows]

    def mismatch_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM stripe_events WHERE outcome='mismatch' "
                "ORDER BY received_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def all_requests(self, limit: int = 500) -> list[dict[str, Any]]:
        """Full request ledger (all statuses), newest first — for audit/history."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

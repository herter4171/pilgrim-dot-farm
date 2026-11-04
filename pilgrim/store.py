"""station.db: inventory metadata + airplay ledger + committed program (RADIO.md §7, §11)."""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


# Queue order everywhere (RADIO §14; supersedes PAYMENT.md §3.2).
#
#   1. paid first (priority DESC) — paid priority is never voted on
#   2. work already in flight (producing/ready) before free queued rows, so a
#      job that started is never reordered
#   3. free queued rows by vote score DESC (votes decide what is written next)
#   4. then paid_at / claimed_at / created_at ASC, id ASC — the old FIFO, so
#      with no votes nothing changes.
#
# The score is read from request_votes on every query (VOTING.md V9): always
# accurate, no cached counters to keep in sync.
def queue_order(alias: str = "") -> str:
    a = f"{alias}." if alias else ""
    score = ("(SELECT COALESCE(SUM(v.value), 0) FROM request_votes v "
             f"WHERE v.request_id = {a}id)")
    return (
        f"ORDER BY {a}priority DESC, "
        f"CASE WHEN {a}status = 'queued' THEN 1 ELSE 0 END ASC, "
        f"CASE WHEN {a}status = 'queued' AND {a}priority = 0 "
        f"THEN -{score} ELSE 0 END ASC, "
        f"COALESCE({a}paid_at, {a}claimed_at, {a}created_at) ASC, "
        f"{a}id ASC")


QUEUE_ORDER = queue_order()
# The same order for queries that alias requests as `r` (joined with items).
QUEUE_ORDER_R = queue_order("r")


def _queued_where(bury_threshold: int | None) -> tuple[str, tuple[Any, ...]]:
    """WHERE clause for the air-able queue: `queued` rows, minus buried free
    ones when a threshold is given (VOTING.md V7)."""
    where = "status='queued'"
    params: tuple[Any, ...] = ()
    if bury_threshold is not None:
        where += (" AND NOT (priority = 0 AND "
                  "(SELECT COALESCE(SUM(v.value), 0) FROM request_votes v "
                  "WHERE v.request_id = requests.id) <= ?)")
        params = (int(bury_threshold),)
    return where, params



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


def _wait_s(prep_started_at: str | None, aired_at: str | None,
            due_at: str | None = None) -> int | None:
    """Prep time on the board: the prep clock starts when the lyrics/brief
    are first written (`prep_started_at`), NOT at submission — time spent in
    line or waiting for the station to come back is not prep work. Rows that
    never recorded a writing start (aired before this column existed) read
    null, so the UI omits the label rather than showing queue idle time.

    The clock stops at the earlier of `aired_at` and `due_at` (RADIO §11):
    `due_at` is the wall time the song was scheduled to air when it was teed
    up (committed or spliced). With listeners present the two agree to within
    seconds; with nobody listening the playhead is frozen (IDLE.md D3) and
    `aired_at` only lands on the next PLAY, hours later — that wait is nobody's
    prep, so the metric is capped at when the song WOULD have aired."""
    start = _parse_epoch(prep_started_at)
    ends = [t for t in (_parse_epoch(aired_at), _parse_epoch(due_at)) if t is not None]
    if start is None or not ends:
        return None
    return max(0, int(round(min(ends) - start)))


def _project_queue_row(row: dict[str, Any], max_length: int,
                       stages: Mapping[int, str] | None = None,
                       score: int = 0, votable: bool = False,
                       buried: bool = False) -> dict[str, Any]:
    paid = bool(row.get("priority"))
    stage = ""
    if row["status"] == "producing":
        # writing = lyrics/brief, generating = song model (RADIO §11)
        stage = (stages or {}).get(int(row["id"]), "writing")
    return {"id": int(row["id"]),
            "text": _project_text(row["text"], paid, max_length,
                                  row.get("summary")),
            "status": row["status"], "paid": paid, "stage": stage,
            # the song exists from `ready` on (title shown in the board table)
            "song_title": row.get("song_title") or "",
            # VOTING.md: score is display-only once the row is in flight
            "score": int(score), "votable": bool(votable),
            "buried": bool(buried)}


def _project_recent_row(row: dict[str, Any], max_length: int) -> dict[str, Any]:
    paid = bool(row.get("priority"))
    return {"text": _project_text(row["text"], paid, max_length,
                                  row.get("summary")),
            "song_title": row.get("song_title") or "",
            "song_artist": row.get("song_artist") or "",
            "paid": paid,
            "wait_s": _wait_s(row.get("prep_started_at"), row.get("aired_at"),
                              row.get("due_at"))}


REQUEST_PRIORITY_H = 24.0   # a request song made this recently jumps the queue


def _iso_ago(epoch: float, hours: float) -> str:
    return datetime.fromtimestamp(epoch - hours * 3600, tz=UTC).isoformat()


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
                -- Request-queue votes (VOTING.md V2): one row per voter and
                -- request, value -1|+1 (0 = retracted -> row deleted). The
                -- canonical client IP is stored for the per-target abuse cap
                -- (V3), like visitor_ips (RADIO §14).
                CREATE TABLE IF NOT EXISTS request_votes (
                    request_id INTEGER NOT NULL,
                    voter_id   TEXT    NOT NULL,
                    ip         TEXT,
                    value      INTEGER NOT NULL,
                    created_at TEXT    NOT NULL,
                    updated_at TEXT    NOT NULL,
                    PRIMARY KEY (request_id, voter_id)
                );
                CREATE INDEX IF NOT EXISTS idx_request_votes_ip
                    ON request_votes(request_id, ip);
                -- Song ratings (VOTING.md V2, V8): one row per AIRING, keyed by
                -- (epoch, seq, voter) so the same song can be rated again the
                -- next time it airs. `item_id` feeds the lifetime aggregate.
                CREATE TABLE IF NOT EXISTS song_ratings (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id    INTEGER NOT NULL,
                    epoch      TEXT    NOT NULL,
                    seq        INTEGER NOT NULL,
                    voter_id   TEXT    NOT NULL,
                    ip         TEXT,
                    stars      INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
                    created_at TEXT    NOT NULL,
                    updated_at TEXT    NOT NULL,
                    UNIQUE (epoch, seq, voter_id)
                );
                CREATE INDEX IF NOT EXISTS idx_song_ratings_item
                    ON song_ratings(item_id);
                CREATE INDEX IF NOT EXISTS idx_song_ratings_ip
                    ON song_ratings(epoch, seq, ip);
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
            # idempotent migration (ALBUM_ART.md Step 4, RADIO §14): cover art
            # per song. art_path is the absolute local JPEG (NULL = no art yet),
            # art_s3_key the published object key (NULL = not uploaded), and
            # art_attempted_at the epoch of the last failed attempt (back-off).
            for col, ddl in (("art_path", "TEXT"), ("art_s3_key", "TEXT"),
                             ("art_uploaded_at", "TEXT"),
                             ("art_attempted_at", "REAL")):
                if col not in cols:
                    self._conn.execute(f"ALTER TABLE items ADD COLUMN {col} {ddl}")
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
                             # wall time the song was scheduled to air when it
                             # was teed up (committed/spliced); caps Prep Time
                             # so a frozen station does not inflate it
                             ("due_at", "TEXT"),
                             ("summary", "TEXT"),
                             # PAYMENT.md Step 12b: staged checks + operator comp
                             ("stage", "TEXT"),
                             ("comped", "INTEGER DEFAULT 0"),
                             # VOTING.md V6: first time the producer claims the
                             # row, so in-flight order is the claim order
                             ("claimed_at", "TEXT")):
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

    # ------------------------------------------------------------------- art
    def songs_needing_art(self, limit: int = 10, *, retry_after_s: float = 300.0,
                          now: float | None = None) -> list[dict[str, Any]]:
        """Songs with no art yet (ALBUM_ART.md Step 5): both title and artist
        present, not retired/emergency, and not attempted within
        `retry_after_s`.

        Order: **fresh listener-request songs first** (made in the last
        `REQUEST_PRIORITY_H` hours, newest first — they air minutes after
        being made, so a cover rendered now lands before the first spin;
        ALBUM_ART Step 5 addendum), then songs already in the committed
        program (they are about to air), then newest `created_at`.
        """
        now = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                """SELECT i.* FROM items i
                   WHERE i.type='song' AND i.retired=0 AND i.emergency=0
                     AND i.art_path IS NULL
                     AND COALESCE(i.title,'') <> '' AND COALESCE(i.artist,'') <> ''
                     AND (i.art_attempted_at IS NULL OR i.art_attempted_at <= ?)
                   ORDER BY CASE WHEN i.meta_json LIKE '%"request_id"%'
                                  AND i.created_at >= ?
                            THEN 0 ELSE 1 END,
                            CASE WHEN EXISTS (
                              SELECT 1 FROM program p WHERE p.item_id = i.id)
                            THEN 0 ELSE 1 END,
                            COALESCE((SELECT MIN(p.seq) FROM program p
                                      WHERE p.item_id = i.id), 0),
                            i.created_at DESC
                   LIMIT ?""",
                (now - retry_after_s, _iso_ago(now, REQUEST_PRIORITY_H), limit)).fetchall()
        return [dict(r) for r in rows]

    def set_art(self, item_id: int, art_path: str, art_meta: dict[str, Any]) -> None:
        """Record a rendered cover: the local JPEG plus the reproducible scene
        (scene, final FLUX prompt, seed) merged into the item's meta."""
        with self._lock:
            r = self._conn.execute(
                "SELECT meta_json FROM items WHERE id=?", (item_id,)).fetchone()
            meta = json.loads(r["meta_json"]) if r and r["meta_json"] else {}
            meta["art"] = art_meta
            # A re-render (art_backfill --force) writes new bytes, so any
            # previous upload no longer matches: clear the key and let the
            # publisher upload the new JPEG (same rule as set_media_file).
            self._conn.execute(
                "UPDATE items SET art_path=?, meta_json=?, art_attempted_at=NULL, "
                "art_s3_key=NULL, art_uploaded_at=NULL WHERE id=?",
                (art_path, json.dumps(meta), item_id))
            self._conn.commit()

    def set_art_attempted(self, item_id: int, when: float) -> None:
        """Mark a failed art attempt (back-off for `art.retry_after_s`)."""
        with self._lock:
            self._conn.execute(
                "UPDATE items SET art_attempted_at=? WHERE id=?", (when, item_id))
            self._conn.commit()

    def set_art_published(self, item_id: int, art_s3_key: str) -> None:
        """Record a confirmed art upload. Only a successful PUT calls this."""
        with self._lock:
            self._conn.execute(
                "UPDATE items SET art_s3_key=?, art_uploaded_at=? WHERE id=?",
                (art_s3_key, _now_iso(), item_id))
            self._conn.commit()

    def items_to_publish_art(self, limit: int = 50) -> list[dict[str, Any]]:
        """Songs with a rendered JPEG not yet in S3 (ALBUM_ART.md Step 6)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM items WHERE type='song' AND retired=0 "
                "AND art_path IS NOT NULL AND art_s3_key IS NULL "
                "ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def art_counts(self) -> dict[str, int]:
        """Songs with / without a rendered cover (admin health only)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT SUM(CASE WHEN art_path IS NOT NULL THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN art_path IS NULL THEN 1 ELSE 0 END) FROM items "
                "WHERE type='song' AND retired=0 AND emergency=0").fetchone()
        return {"with_art": int(r[0] or 0), "without_art": int(r[1] or 0)}

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

    def delete_program_rows(self, seqs: list[int]) -> int:
        """Delete committed future rows by seq (IDLE.md thaw revalidation).
        Deleted seqs are never reused: new rows always take seqs above the
        AUTOINCREMENT high-water mark (update_program_seq/set_program_seq_high).
        Returns the number of rows removed."""
        if not seqs:
            return 0
        with self._lock:
            marks = ",".join("?" * len(seqs))
            cur = self._conn.execute(
                f"DELETE FROM program WHERE seq IN ({marks})", tuple(seqs))
            self._conn.commit()
            return int(cur.rowcount or 0)

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

    def queued_requests(self, cap: int | None = None, *,
                        bury_threshold: int | None = None) -> list[dict[str, Any]]:
        """Live queue in queue order (paid first, in flight next, then free by
        vote score, then FIFO). Rejected/evicted/aired/... requests are not part
        of the air-able queue. `bury_threshold` skips free queued rows at/below
        that score (VOTING.md V7); None disables burying."""
        where, params = _queued_where(bury_threshold)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM requests WHERE {where} {QUEUE_ORDER}",
                params).fetchall()
        out = [dict(r) for r in rows]
        return out[:cap] if cap else out

    def oldest_queued_request(self) -> dict[str, Any] | None:
        """Oldest still-queued request (the next one to be serviced on air)."""
        return self.next_request_to_produce()

    # --------------------------------------------------- request lifecycle (4.4)
    def next_request_to_produce(self, *,
                                bury_threshold: int | None = None
                                ) -> dict[str, Any] | None:
        """Next `queued` request in queue order (paid first, in flight next,
        then free by vote score, then FIFO). Buried free rows are skipped."""
        where, params = _queued_where(bury_threshold)
        with self._lock:
            r = self._conn.execute(
                f"SELECT * FROM requests WHERE {where} {QUEUE_ORDER} LIMIT 1",
                params).fetchone()
        return dict(r) if r else None

    def mark_request_producing(self, request_id: int) -> str | None:
        """Claim the row for production. `claimed_at` is set the first time
        only (COALESCE): VOTING.md V6 orders in-flight work by claim time, so a
        retry after an outage keeps its original place. Returns claimed_at."""
        with self._lock:
            now = _now_iso()
            self._conn.execute(
                "UPDATE requests SET status='producing', updated_at=?, "
                "claimed_at=COALESCE(claimed_at, ?) WHERE id=?",
                (now, now, request_id))
            self._conn.commit()
            r = self._conn.execute(
                "SELECT claimed_at FROM requests WHERE id=?", (request_id,)).fetchone()
        return r["claimed_at"] if r else None

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

    def mark_request_due(self, request_id: int, due_wall: float) -> None:
        """The request's song was teed up (committed or spliced into the
        programme): record the wall time its slot airs (RADIO §11). Stamped
        again if the song is re-committed; never cleared."""
        due = datetime.fromtimestamp(float(due_wall), UTC).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET due_at=?, updated_at=? WHERE id=?",
                (due, _now_iso(), request_id))
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

    def pending_requests(self, *,
                         bury_threshold: int | None = None) -> list[dict[str, Any]]:
        """Requests still awaiting air (queued, producing, or ready) in queue
        order. The scheduler holds the committed window short while one of these
        is uncommitted so it can air as soon as its song + intro are ready.
        Buried free rows are excluded (VOTING.md V7): nothing is produced for
        them, so the window must not be held for one either."""
        where = "status IN ('queued','producing','ready')"
        params: tuple[Any, ...] = ()
        if bury_threshold is not None:
            where += (" AND NOT (priority = 0 AND status = 'queued' AND "
                      "(SELECT COALESCE(SUM(v.value), 0) FROM request_votes v "
                      "WHERE v.request_id = requests.id) <= ?)")
            params = (int(bury_threshold),)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM requests WHERE {where} {QUEUE_ORDER}",
                params).fetchall()
        return [dict(r) for r in rows]

    def ready_request_songs(self) -> list[dict[str, Any]]:
        """`ready` requests in queue order (their songs are ready to air)."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM requests WHERE status='ready' {QUEUE_ORDER}").fetchall()
        return [dict(r) for r in rows]

    def count_paid_aired_since(self, since_iso: str) -> int:
        """Priority (paid) requests that aired at/after `since_iso`. Feeds the
        Tree Fiddy Report; never names or emails."""
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM requests WHERE priority=1 AND status='aired' "
                "AND aired_at IS NOT NULL AND aired_at >= ?",
                (since_iso,)).fetchone()[0])

    def recently_aired_request(self, since_iso: str) -> dict[str, Any] | None:
        """The most recently aired request at/after `since_iso` (wall ISO), if
        any. Used for the DJ's back-announce (never a name or email)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM requests WHERE status='aired' AND aired_at IS NOT NULL "
                "AND aired_at >= ? ORDER BY aired_at DESC LIMIT 1",
                (since_iso,)).fetchone()
        return dict(r) if r else None

    def reset_producing_to_queued(self) -> None:
        """Startup: a crash mid-generation must not strand a request in
        `producing` (OVERHAUL 4.4)."""
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='queued', updated_at=? "
                "WHERE status='producing'", (_now_iso(),))
            self._conn.commit()

    def request_board(self, cap: int, max_length: int = 160,
                      stages: Mapping[int, str] | None = None, *,
                      voting_enabled: bool = True,
                      bury_threshold: int | None = None) -> dict[str, Any]:
        """Board for the UI: live queue (queued+producing+ready, queue order,
        <= cap) plus the 5 most recent aired requests with song titles and how
        long they waited. Rows are projected — never raw (PAYMENT.md §3.4).

        Queue rows carry `id`, `score`, `votable` and `buried` (VOTING.md §1);
        recent rows never do (no retrospective voting)."""
        with self._lock:
            queue = [dict(r) for r in self._conn.execute(
                "SELECT r.*, i.title AS song_title FROM requests r "
                "LEFT JOIN items i ON i.id = r.song_item_id "
                "WHERE r.status IN ('queued','producing','ready') "
                f"{QUEUE_ORDER_R} LIMIT ?", (cap,)).fetchall()]
            scores = {int(r[0]): int(r[1]) for r in self._conn.execute(
                "SELECT request_id, COALESCE(SUM(value), 0) FROM request_votes "
                "GROUP BY request_id").fetchall()}
            recent = [dict(r) for r in self._conn.execute(
                "SELECT r.id, r.text, r.summary, r.created_at, r.aired_at, "
                "r.prep_started_at, r.due_at, r.priority, "
                "i.title AS song_title, i.artist AS song_artist FROM requests r "
                "LEFT JOIN items i ON i.id = r.song_item_id "
                "WHERE r.status IN ('aired','serviced') "
                "ORDER BY r.id DESC LIMIT 5").fetchall()]
        out = []
        for r in queue:
            rid = int(r["id"])
            paid = bool(r.get("priority"))
            score = scores.get(rid, 0)
            buried = (bury_threshold is not None and not paid
                      and r["status"] == "queued" and score <= bury_threshold)
            out.append(_project_queue_row(
                r, max_length, stages, score=score,
                votable=bool(voting_enabled and not paid),
                buried=bool(buried)))
        return {
            "queue": out,
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

    # ------------------------------------------- community votes (VOTING.md)
    def request_score(self, request_id: int) -> int:
        with self._lock:
            r = self._conn.execute(
                "SELECT COALESCE(SUM(value), 0) FROM request_votes "
                "WHERE request_id=?", (request_id,)).fetchone()
        return int(r[0]) if r else 0

    def cast_request_vote(self, request_id: int, voter_id: str,
                          ip: str | None, value: int, *,
                          max_voters_per_ip: int) -> dict[str, Any]:
        """Upsert one vote (VOTING.md V2/V3/V4/V5). One locked transaction:
        window + paid + IP-cap checks and the write are atomic, so the row
        cannot move between check and insert.

        Returns {ok: True, score, mine, open} or {ok: False, code} where code
        is not_found | not_votable | voting_closed | too_many_voters."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, status, priority FROM requests WHERE id=?",
                (request_id,)).fetchone()
            if row is None:
                return {"ok": False, "code": "not_found"}
            if int(row["priority"]):
                return {"ok": False, "code": "not_votable"}
            if row["status"] not in ("queued", "producing", "ready"):
                return {"ok": False, "code": "voting_closed"}
            if ip:
                n = self._conn.execute(
                    "SELECT COUNT(DISTINCT voter_id) FROM request_votes "
                    "WHERE request_id=? AND ip=? AND voter_id<>?",
                    (request_id, ip, voter_id)).fetchone()[0]
                if int(n) >= max_voters_per_ip:
                    return {"ok": False, "code": "too_many_voters"}
            now = _now_iso()
            if value == 0:
                self._conn.execute(
                    "DELETE FROM request_votes WHERE request_id=? AND voter_id=?",
                    (request_id, voter_id))
            else:
                self._conn.execute(
                    "INSERT INTO request_votes (request_id, voter_id, ip, value, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(request_id, voter_id) DO UPDATE SET "
                    "value=excluded.value, ip=excluded.ip, "
                    "updated_at=excluded.updated_at",
                    (request_id, voter_id, ip, value, now, now))
            self._conn.commit()
            score = int(self._conn.execute(
                "SELECT COALESCE(SUM(value), 0) FROM request_votes "
                "WHERE request_id=?", (request_id,)).fetchone()[0])
        return {"ok": True, "score": score, "mine": value, "open": True}

    def cast_song_rating(self, *, item_id: int, epoch: str, seq: int,
                         voter_id: str, ip: str | None, stars: int,
                         max_voters_per_ip: int) -> dict[str, Any]:
        """Upsert one rating for an airing (VOTING.md V2/V3). The window is
        checked by the caller (it is scheduler state, not DB state); this only
        enforces uniqueness and the per-target IP cap."""
        with self._lock:
            if ip:
                n = self._conn.execute(
                    "SELECT COUNT(DISTINCT voter_id) FROM song_ratings "
                    "WHERE epoch=? AND seq=? AND ip=? AND voter_id<>?",
                    (epoch, seq, ip, voter_id)).fetchone()[0]
                if int(n) >= max_voters_per_ip:
                    return {"ok": False, "code": "too_many_voters"}
            now = _now_iso()
            self._conn.execute(
                "INSERT INTO song_ratings (item_id, epoch, seq, voter_id, ip, "
                "stars, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(epoch, seq, voter_id) DO UPDATE SET "
                "stars=excluded.stars, ip=excluded.ip, "
                "updated_at=excluded.updated_at",
                (item_id, epoch, seq, voter_id, ip, stars, now, now))
            self._conn.commit()
            agg = self._conn.execute(
                "SELECT COUNT(*), AVG(stars) FROM song_ratings WHERE item_id=?",
                (item_id,)).fetchone()
        count = int(agg[0] or 0)
        avg = round(float(agg[1]), 2) if agg[1] is not None else None
        return {"ok": True, "item_id": item_id, "count": count,
                "avg": avg, "mine": stars}

    def song_rating(self, item_id: int) -> dict[str, Any]:
        """Lifetime aggregate over every airing of `item_id` (VOTING.md V10)."""
        with self._lock:
            agg = self._conn.execute(
                "SELECT COUNT(*), AVG(stars) FROM song_ratings WHERE item_id=?",
                (item_id,)).fetchone()
        count = int(agg[0] or 0)
        avg = round(float(agg[1]), 2) if agg[1] is not None else None
        return {"count": count, "avg": avg}

    def recent_votes(self, limit: int = 100) -> list[dict[str, Any]]:
        """Admin audit trail. Deliberately omits voter_id and ip (they are
        pseudonymous identifiers; the DB keeps them for the abuse cap only)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT request_id, value, created_at, updated_at "
                "FROM request_votes ORDER BY updated_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def recent_ratings(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_id, epoch, seq, stars, created_at, updated_at "
                "FROM song_ratings ORDER BY updated_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def buried_requests(self, bury_threshold: int,
                        limit: int = 100) -> list[dict[str, Any]]:
        """Free queued rows at/below `bury_threshold` (admin audit)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text, created_at, score FROM ("
                "SELECT id, text, created_at, "
                "(SELECT COALESCE(SUM(v.value), 0) FROM request_votes v "
                "WHERE v.request_id = requests.id) AS score "
                "FROM requests WHERE status='queued' AND priority=0) "
                "WHERE score <= ? ORDER BY score ASC, id ASC LIMIT ?",
                (int(bury_threshold), limit)).fetchall()
        return [dict(r) for r in rows]

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


    # -------------------------------------------------------------- key/value
    def meta_get(self, key: str) -> str | None:
        """One small key/value from the `meta` table (leases, counters)."""
        with self._lock:
            r = self._conn.execute("SELECT value FROM meta WHERE key=?",
                                   (key,)).fetchone()
        return None if r is None else r["value"]

    def meta_set(self, key: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self._conn.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))
            self._conn.commit()

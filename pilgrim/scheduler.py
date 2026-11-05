"""Playout scheduler (RADIO.md §5). Keeps the committed program filled >= lookahead
ahead of the live playhead, which advances on the real clock regardless of whether
any listener is tuned in (station always on air). Playout never blocks on rendering:
only fully-rendered inventory enters the program.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pilgrim.config import RNG, Clock, Config
from pilgrim.pipelines.sfx import SFX_HOST_TYPES
from pilgrim.promotions import active_campaign, target_per_hour
from pilgrim.selector import PlayoutState, Selector
from pilgrim.sfx_plan import plan_overlays
from pilgrim.store import Store

# Committed types that put words between songs (§5.2). `intro` counts: it
# names the song that follows it.
CALLOUT_TYPES = frozenset({"liner", "station_id", "dj_talk", "field_report", "intro",
                           "promo", "time_check"})

# Segment types that can bridge air while a listener request waits for its
# song slot. A stock song is never one of them: it would queue ahead of the
# request (§5.2, request priority).
NON_SONG_TYPES = ("dj_talk", "commercial_break", "liner", "news", "field_report",
                  "promo", "time_check")

# Types a manually-placed operator phrase must never sit adjacent to in either
# order (RADIO §11: "DJ-like news adjacency", TUI.md §6). A song is the only
# guaranteed separator.
PHRASE_ADJACENT = frozenset({"dj_talk", "news", "operator_phrase"})

log = logging.getLogger("radio.scheduler")


def _utc_iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return None


class Scheduler:
    # Cadence of the refill loop below. Coverage is only guaranteed at the
    # moment of a commit, so between cycles it dips by up to this much: the
    # `committed_lookahead_s` target is a floor for the scheduler, not an
    # invariant for an arbitrary instant (§5.3). Exposed so the e2e report can
    # assert against the real cadence instead of a hard-coded tolerance.
    REFILL_INTERVAL_S = 2.0
    # Request splice gate (RADIO §5.2, §8.4): when the station publishes media,
    # a ready request waits until its song + intro are in S3 so the browser
    # fetches them from CloudFront like every other item, not from the
    # transitional origin route seconds before airtime. Bounded: a failed
    # upload must never strand a request (the origin fallback still exists).
    PUBLISH_WAIT_MAX_S = 30.0

    def __init__(self, cfg: Config, store: Store, selector: Selector,
                 clock: Clock, rng: RNG, epoch: str | None = None,
                 on_phrase_aired: Callable[[int], None] | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.selector = selector
        self.clock = clock
        self.rng = rng
        # Phrase lifecycle hook (RADIO §5.5; TUI.md §6): called with an item_id
        # when its committed phrase row falls fully behind the playhead (it
        # aired). The PhraseManager marks the job terminal. Optional so tests
        # and the simulation can build a scheduler without a phrase manager.
        self.on_phrase_aired = on_phrase_aired
        # Station identity for the DJ protocol (TUI.md §5, RADIO §5.3): the
        # `epoch` changes on restart; the programme `revision` bumps whenever
        # already-published future playback changes (cutovers bump it, §5.5).
        self.epoch = epoch or uuid.uuid4().hex[:12]
        self.revision = 1
        # Operator phrases ready to air (RADIO §11): the run loop appends them
        # at the committed tail when it is legal; none wait on production
        # (AGENTS rule 1).
        self.pending_phrases: deque[dict] = deque()
        self._start_wall = time.monotonic()
        # Song air history is PERSISTED on items (last_aired_at, play_count) so a
        # restart keeps the rotation (§PRIORITIES §4). Only commercials keep an
        # in-memory air map (they can follow the same pattern later).
        self._last_comm_air: dict[int, float] = {}  # item_id -> air-clock of last commit
        # Promo / time-check spacing is live-only like commercials (a restart
        # re-derives the calendar target from the committed window).
        self._last_promo_air = -1e9
        self._last_request_line_air = -1e9
        self._last_promo_kind_air: dict[str, float] = {}
        self._promo_log: list[tuple[float, str]] = []  # (program start, kind)
        self._last_time_check_air = -1e9
        self._last_liner_air: dict[int, float] = {}
        self._air_clock = 0.0  # monotonic committed-program time (independent of trimming)
        # in-memory cumulative mapping built each cycle
        self._program_start = 0.0  # absolute start of retained program history
        self._items: list[dict] = []
        self._cum: list[float] = []  # absolute start times (program seconds)
        self._total = 0.0
        self._offset = 0.0  # playhead anchor; shifts forward if the program starves
        # IDLE.md: clock.now() when the playhead froze (None = running). While
        # frozen, position()/coverage() are static and the committed window is
        # left untouched (it is already fully rendered).
        self._frozen_at: float | None = None
        # committed request songs not yet on air: seq -> request_id. A request
        # is 'aired' when the playhead reaches its song, not when committed.
        self._pending_aired: dict[int, int] = {}
        # Set by the server when a media publisher runs (RADIO §8.4): ready
        # requests then wait (bounded) for their audio to be in S3 before the
        # splice. Off by default so tests, the simulation and the e2e server
        # (no uploader) splice immediately. request_id -> clock.now() first seen
        # waiting.
        self.require_published = False
        self._publish_wait_since: dict[int, float] = {}
        # While a listener request is still waiting for its song slot, hold the
        # committed window at filler_horizon_s so no stock song queues ahead of
        # it (§5.2). Tracked per request id so a stuck request releases the hold
        # after playout.request_hold_max_s instead of starving the lookahead.
        self._hold_request_id: int | None = None
        self._hold_since: float = 0.0
        # SFX overlays that air with each committed host: seq -> overlays, plus
        # the absolute program start of recent stingers (rolling budget).
        # Live-only, like the program itself (SFX.md §7.2).
        self._sfx_by_seq: dict[int, list[dict]] = {}
        self._stinger_starts: list[float] = []
        # The committed program is live-only state: old rows refer to a clock
        # that no longer exists, so start clean on every run (OVERHAUL 2.4).
        self.store.clear_program()
        # Seed rotation history for songs that predate the persistence columns,
        # so the live pool starts with real last-aired/play-count data (PRIOR §4).
        self.store.backfill_airplay_history()
        self._rebuild_program()

    # ------------------------------------------------------------------ index
    def _rebuild_program(self) -> None:
        rows = self.store.program_since(1)
        self._items = rows
        self._cum = []
        t = self._program_start
        for r in rows:
            self._cum.append(t)
            t += r["duration_s"]
        self._total = t

    def position(self) -> float:
        """Live playhead in program-seconds (advances on the real clock, unless
        the station is idle: IDLE.md D3)."""
        now = self._frozen_at if self._frozen_at is not None else self.clock.now()
        return min(now - self._offset, self._total)

    # ------------------------------------------------------------- idle (IDLE.md)
    @property
    def frozen(self) -> bool:
        return self._frozen_at is not None

    def freeze(self) -> None:
        """Stop the playhead where it is (IDLE.md D3). Idempotent: the frozen
        window stays exactly as committed, already rendered and playable."""
        if self._frozen_at is not None:
            return
        self._frozen_at = self.clock.now()
        log.info("program.frozen", extra={"position_s": round(self.position(), 1),
                                          "coverage_s": round(self.coverage(), 1)})

    def thaw(self) -> int:
        """Resume the playhead and revalidate the committed future (IDLE.md D4).

        Shifts `_offset` by the frozen span (so playback resumes exactly where
        it stopped), drops future `time_check` rows and any future row whose
        `expires_at` has passed for its new slot wall, bridges song-after-song
        gaps the drops opened with a liner/station ID, bumps the revision when
        anything changed, and tops the window up from rendered inventory only
        (hard rule 1). Idempotent when not frozen: returns 0, no revision bump.
        """
        if self._frozen_at is None:
            return 0
        frozen_s = self.clock.now() - self._frozen_at
        self._offset += frozen_s
        self._frozen_at = None
        # A freeze must not burn a pending request's hold budget (§5.2).
        if self._hold_request_id is not None:
            self._hold_since = self.clock.now()
        dropped = bridged = 0
        on_seq, _ = self.on_air()
        start = 0
        if on_seq is not None:
            start = next((i for i, r in enumerate(self._items)
                          if r["seq"] == on_seq), 0) + 1
        drops: list[int] = []
        for i in range(start, len(self._items)):
            r = self._items[i]
            if r["type"] == "time_check":
                # The clock read is for its 5-minute bucket; a long freeze makes
                # it wrong. (time_check items already carry expires_at = bucket
                # + 300, so the check below would catch them too: belt and
                # braces for an emergency item rendered without one.)
                drops.append(r["seq"])
                continue
            it = self.store.get_item(r["item_id"])
            exp = _parse_iso(it.get("expires_at")) if it else None
            if exp is not None and exp < self._slot_wall(self._cum[i]):
                drops.append(r["seq"])
        if drops:
            # Songs are evergreen and never dropped, so no committed request
            # song can be in this list (_pending_aired keys are song seqs).
            assert not [s for s in drops if s in self._pending_aired]
            for s in drops:
                self._sfx_by_seq.pop(s, None)
            dropped = self.store.delete_program_rows(drops)
            self._rebuild_program()
        bridged = self._bridge_song_gaps(start)
        if dropped or bridged:
            self.advance_revision()
            log.info("program.thaw_revalidated", extra={
                "dropped": dropped, "bridged": bridged,
                "frozen_s": round(frozen_s, 1), "revision": self.revision})
        # Top the window up from already-rendered inventory so the waking
        # request returns a full, playable programme (hard rule 1).
        self.commit_lookahead()
        return dropped

    def _bridge_song_gaps(self, start: int) -> int:
        """Insert a liner/station ID before any future song that directly
        follows a song (IDLE.md D4, §5.2: words between songs).

        Future rows are renumbered with fresh seqs above the high-water mark
        (never reusing an occurrence id) and the revision is bumped by the
        caller. With no voice inventory at all the songs stay adjacent (§5.2:
        with no voice item at all, the song airs). Returns the number of
        inserted rows."""
        future = self._items[start:]
        if not future:
            return 0
        inserts: dict[int, list[dict]] = {}
        prev_type = self._items[start - 1]["type"] if start > 0 else None
        for k, r in enumerate(future):
            if r["type"] == "song" and prev_type == "song":
                bridge = self._pick_callout_fallback()
                if bridge:
                    inserts[k] = bridge
            # The row itself is appended after any bridge, so the next row's
            # "previous" type is this row's type either way.
            prev_type = r["type"]
        if not inserts:
            return 0
        final: list[dict] = []
        for k, r in enumerate(future):
            for e in inserts.get(k, []):
                final.append({"item_id": e["item_id"], "type": e["type"],
                              "duration_s": e["duration_s"],
                              "consume": bool(e.get("consume"))})
            final.append({"item_id": r["item_id"], "type": r["type"],
                          "duration_s": r["duration_s"],
                          "old_seq": r["seq"]})
        high = int(self.store.max_seq() or 0)
        seqs = [high + 1 + i for i in range(len(final))]
        remap = {row["old_seq"]: seq
                 for row, seq in zip(final, seqs, strict=True) if "old_seq" in row}
        if remap:
            self._pending_aired = {remap.get(s, s): rid
                                   for s, rid in self._pending_aired.items()}
            self._sfx_by_seq = {remap.get(s, s): v
                                for s, v in self._sfx_by_seq.items()}
        for row, seq in zip(final, seqs, strict=True):
            if "old_seq" in row:
                self.store.update_program_seq(row["old_seq"], seq)
            else:
                self.store.append_program_at(
                    item_id=row["item_id"], type_=row["type"],
                    duration_s=row["duration_s"], seq=seq)
                if row.get("consume"):
                    self.store.mark_aired(row["item_id"])
        self.store.set_program_seq_high(seqs[-1])
        # Inserted words are part of the committed programme: keep the spacing
        # clock consistent with the (now longer) committed end.
        self._air_clock += sum(row["duration_s"]
                               for row, seq in zip(final, seqs, strict=True)
                               if "old_seq" not in row)
        self._rebuild_program()
        return sum(len(v) for v in inserts.values())

    def advance_revision(self) -> int:
        """Bump the programme revision (an edit changed already-published future
        playback; §5.5). Returns the new revision so callers can record it."""
        self.revision += 1
        return self.revision

    def on_air(self) -> tuple[int | None, float]:
        """Return (seq of on-air item, offset seconds into it)."""
        pos = self.position()
        if not self._items:
            return None, 0.0
        # find last item whose start <= pos
        idx = 0
        for i in range(len(self._items)):
            if self._cum[i] <= pos + 1e-6:
                idx = i
            else:
                break
        return self._items[idx]["seq"], pos - self._cum[idx]

    # ------------------------------------------------------------- state view
    def build_state(self) -> PlayoutState:
        st = PlayoutState(self.cfg)
        # Availability follows the §5.4 fallback chain: songs/commercials/liners
        # are evergreen and recycle (fresh OR recycled counts as available);
        # dj_talk and news are produced fresh and deplete if unrefilled.
        # Contextual counts are expiry-aware against the NEXT slot's air time
        # (A1), so the selector never draws a type whose only stock is stale.
        air_wall = self._slot_wall(self._total)
        st.inventory_counts = {
            "song": self.store.count_usable_of_type("song"),
            "dj_talk": self.store.count_fresh_unexpired_of_type("dj_talk", air_wall),
            "field_report": self.store.count_fresh_unexpired_of_type(
                "field_report", air_wall),
            "commercial": self.store.count_usable_of_type("commercial"),
            # station IDs are liners for the draw (§4: a liner is a station ID
            # or a gag line); _pick_liner falls back to them
            "liner": (self.store.count_usable_of_type("liner")
                      + self.store.count_usable_of_type("station_id")),
        }
        st.available = {
            # a song is only drawable if one clears the spacing floor, so a thin
            # pool never loops the same track back to back (§5.2)
            "song": bool(self._eligible_songs()) or bool(self._uncommitted_ready_requests()),
            "dj_talk": st.inventory_counts["dj_talk"] > 0,
            "field_report": st.inventory_counts["field_report"] > 0,
            "commercial_break": st.inventory_counts["commercial"] > 0,
            "liner": st.inventory_counts["liner"] > 0,
            "news": self._news_valid() is not None,
            "promo": self._promo_available(),
            "time_check": self._time_check_item() is not None,
        }
        st.request_songs_ready = len(self._uncommitted_ready_requests())
        # recent types / genres in COMMITTED order (the air order). Constraints
        # apply to the committed sequence since that is exactly what airs.
        # `intro` entries are PART of their song, so they never appear here (4.7):
        # they don't count toward DJ adjacency or the non-song run, and the
        # 'no song after song' rule looks at the last non-intro type.
        recent = [r["type"] for r in self._items[-8:] if r["type"] != "intro"]
        st.recent_types = recent
        st.recent_song_genres = [
            self._genre_for(r["item_id"]) for r in self._items if r["type"] == "song"][-3:]
        # seconds (program time) since each category last aired (committed order)
        st.secs_since = {}
        for cat, ctype in (("news", "news"), ("commercial_break", "commercial"),
                           ("song", "song"), ("promo", "promo"),
                           ("time_check", "time_check")):
            st.secs_since[cat] = self._secs_since(self._total, ctype)
        ni = self._news_valid()
        if ni:
            st.news_valid = True
            st.news_gravity = ni.get("gravity") or "normal"
        st.callout_pending = self._callout_pending() and not self._next_song_has_intro()
        st.near_top_of_hour = self._near_top_of_hour()
        st.time_check_due = st.available["time_check"]
        st.campaign_due = self._campaign_due()
        st.promo_steady_due = self._steady_promo_due()
        return st

    # ------------------------------------------------- words between songs
    def _callout_pending(self) -> bool:
        """True when no voice item has been committed since the last song, so
        the next song needs words in front of it (§5.2). An empty program counts
        as pending: the stream opens with words, not a bare song."""
        for r in reversed(self._items):
            if r["type"] in CALLOUT_TYPES:
                return False
            if r["type"] == "song":
                return True
        return True

    def _next_song_has_intro(self) -> bool:
        """Best guess whether the next song slot brings its own intro (a ready
        request, or the newest fresh song), which is words enough."""
        reqs = self._uncommitted_ready_requests()
        if reqs:
            return bool(reqs[0].get("intro_item_id"))
        fresh = [i for i in self._eligible_songs() if i["fresh"]]
        if not fresh:
            return False
        song_id = max(fresh, key=lambda i: i["id"])["id"]
        return self._intro_for(song_id) is not None

    def _slot_wall(self, program_s: float) -> float:
        """Wall-clock time at which program position `program_s` airs."""
        return self.clock.wall() + max(0.0, program_s - self.position())

    def _near_top_of_hour(self) -> bool:
        """Does the next slot air within top_of_hour_window_s of :00 (wall)?
        Soft time awareness only: it nudges the draw, it never waits."""
        m = self._slot_wall(self._total) % 3600.0
        return min(m, 3600.0 - m) <= self.cfg.playout.top_of_hour_window_s

    def _pick_callout_fallback(self) -> list[dict]:
        """Words before a song when the draw produced none: any liner or
        station ID (repeats allowed), then the emergency pack (§5.4, §8.3).
        Returns [] only when no voice item exists at all."""
        recent = {r["item_id"] for r in self._items[-10:]}
        everything = self.store.list_items("liner") + self.store.list_items("station_id")
        live = [i for i in everything if not i["emergency"] and not i["retired"]]
        emergency = [i for i in everything if i["emergency"] and not i["retired"]]
        for pool in (live, emergency):
            if pool:
                unspent = [i for i in pool if i["id"] not in recent]
                it = self.rng.choice(unspent or pool)
                return [{"item_id": it["id"], "type": it["type"],
                         "duration_s": it["duration_s"], "consume": True}]
        return []

    def _genre_probe(self, recent_types: list[str]) -> list[tuple[int, str]]:
        out = []
        for r in reversed(self._items):
            if r["type"] == "song":
                out.append((r["item_id"], r["type"]))
        return out

    def _genre_for(self, item_id: int) -> str:
        it = self.store.get_item(item_id)
        return (it.get("genre") or "") if it else ""

    def _secs_since(self, pos: float, category: str) -> float:
        """Program-time seconds between the current commit point and the end of the
        last committed item of `category`, both measured in committed order."""
        if not self._items:
            return float("inf")
        last_end = -1.0
        for i, r in enumerate(self._items):
            if r["type"] == category:
                last_end = self._cum[i] + r["duration_s"]
        return pos - last_end if last_end >= 0 else float("inf")

    def _news_valid(self) -> dict | None:
        # latest bulletin that will still be valid when it AIRS, not when it is
        # committed (A1): expiry is checked against the next slot's wall time.
        air_wall = self._slot_wall(self._total)
        for it in reversed(self.store.list_items("news")):
            exp = _parse_iso(it.get("expires_at"))
            if exp is not None and exp < air_wall:
                continue  # would expire before it airs
            fresh = it.get("fresh")
            if fresh:
                return it
        return None

    # -------------------------------------------------------------- committing
    def commit_lookahead(self) -> None:
        """Append committed items until coverage >= lookahead (or no inventory).

        While a listener request is pending (queued/producing/ready and not yet
        committed), the window is held at filler_horizon_s instead of the full
        lookahead. That drains the stock songs already ahead of the request and
        keeps new ones from queueing in front of it, so the moment the song +
        intro land they take the next slot instead of airing three songs later
        (§5.2 request priority).
        """
        cfg = self.cfg.playout
        if self._frozen_at is not None:
            # IDLE.md D3/D8: while frozen the window is static (position does
            # not move, so coverage and trim are static too) and filling it
            # would consume one-shot inventory for airtime nobody hears. A
            # ready listener request still takes the next slot the moment
            # someone wakes the station.
            self._splice_ready_requests()
            return
        lookahead = cfg.committed_lookahead_s
        guard = 0
        # types that came up empty (e.g. every liner is inside its no-repeat
        # window): re-draw without them until something commits
        self._settle_aired()
        # A ready request jumps into the next song slot before the window fills
        # with stock songs; only then is the window held for requests still
        # rendering (§5.2).
        self._splice_ready_requests()
        request_waiting = self._request_hold()
        blocked: set[str] = set()
        while self.coverage() < lookahead and guard < 100:
            guard += 1
            st = self.build_state()
            for t in blocked:
                st.available[t] = False
            if request_waiting:
                ready_req = st.request_songs_ready > 0
                if ready_req:
                    # A ready request takes the next song slot. If the last
                    # committed item is a song, the selector supplies the
                    # required interjection first; then the request follows.
                    pass
                elif self.coverage() >= cfg.filler_horizon_s:
                    break  # let the window drain; the request takes the tail
                elif any(st.available[t] for t in NON_SONG_TYPES
                         if t not in blocked):
                    # No stock song may queue ahead of the request. Bridge with
                    # words/spots; fall back to a song only to avoid dead air.
                    st.available["song"] = False
            try:
                type_ = self.selector.choose_next(st)
            except ValueError:
                break  # nothing airable right now; producer will refill
            if type_ != "song" and self._song_drought(st):
                break  # hold the slot open for the next song (§5.2)
            entries = self._materialize(type_)
            if not entries:
                blocked.add(type_)
                continue
            if (type_ == "song" and entries[0]["type"] != "intro"
                    and self._callout_pending()):
                entries = self._pick_callout_fallback() + entries
            for e in entries:
                self._append(e)
            self._rebuild_program()
            blocked.clear()  # the program moved on; re-check everything
        self._trim()
        cov = self.coverage()
        if cov < 60:
            log.warning("program.low_coverage", extra={
                "coverage_s": round(cov, 1),
                "inventory": self.build_state().inventory_counts})

    def _song_drought(self, st: PlayoutState) -> bool:
        """New songs air as soon as they are made. A run of interjections past
        max_consecutive_non_song means no song was airable, so only bridge dead
        air (coverage below filler_horizon_s) instead of committing ten minutes
        of spots a fresh song would have to wait behind (§5.2)."""
        run = 0
        for t in reversed(st.recent_types):
            if t == "song":
                break
            run += 1
        return (run >= self.cfg.playout.max_consecutive_non_song
                and self.coverage() >= self.cfg.playout.filler_horizon_s)

    def _may_repeat(self) -> bool:
        """A spacing-breaking repeat (a liner inside its 10-item window, a spot
        inside its 30-min window) only bridges imminent dead air (§5.2, §5.4).
        Padding the whole lookahead with repeats would queue ten minutes of the
        same two liners ahead of anything the producer makes next."""
        return self.coverage() < self.cfg.playout.filler_horizon_s

    def coverage(self) -> float:
        pos = self.position()
        return max(0.0, self._total - pos)

    def _append(self, e: dict) -> None:
        item_id = e["item_id"]
        lag = (self.clock.now() - self._offset) - self._total
        if lag > 0:  # program ran dry: start the new item at "now", not in the past
            self._offset += lag
            log.warning("program.starved", extra={"gap_s": round(lag, 1)})
        host_start = self._total
        # A1 backstop: the picker already filtered against this slot, so this
        # should never fire. Log it if it does; never drop the item (that would
        # open a gap) and never wait on a render.
        exp = _parse_iso(e.get("expires_at"))
        if exp is not None and exp < self._slot_wall(host_start):
            log.warning("program.expired_commit", extra={
                "item_id": item_id, "item_type": e["type"],
                "expires_at": e.get("expires_at")})
        seq = self.store.append_program(item_id, e["type"], e["duration_s"])
        self._air_clock += e["duration_s"]  # monotonic: never resets on trim
        self._plan_sfx(seq, e, host_start)
        if e.get("consume"):
            self.store.mark_aired(item_id)
        if e["type"] == "promo":
            kind = e.get("promo_kind") or "campaign"
            self._last_promo_air = host_start
            self._last_promo_kind_air[kind] = host_start
            self._promo_log.append((host_start, kind))
            if kind == "request_line":
                self._last_request_line_air = host_start
        if e["type"] == "time_check":
            self._last_time_check_air = host_start
        if e["type"] in ("liner", "station_id"):
            self._last_liner_air[item_id] = self._slot_wall(host_start)
        if e["type"] == "song":
            # persist the wall-clock air (injected clock, never time.time) so
            # spacing + weighting survive a restart (PRIORITIES §4)
            # recorded at its scheduled air time, not commit time: the lead
            # (committed coverage) varies, and spacing is about what listeners hear
            self.store.commit_song_air(item_id, self._slot_wall(host_start))
        if e.get("request_id"):
            # committed, not yet heard: _settle_aired marks it when it airs
            self._pending_aired[seq] = e["request_id"]
            # teed up: the slot's wall time caps the board's Prep Time so a
            # frozen station (IDLE.md D3) cannot inflate it (RADIO §11)
            self.store.mark_request_due(e["request_id"], self._slot_wall(host_start))
            log.info("request.committed", extra={
                "request_id": e["request_id"], "item_id": item_id, "seq": seq,
                "due_in_s": round(host_start - self.position(), 1)})
        # a break commits several entries before the next rebuild: advance the
        # end now so each entry's start (and SFX timing) is its own
        self._total += e["duration_s"]
        log.debug("program.commit", extra={
            "seq": seq, "item_id": item_id, "item_type": e["type"],
            "duration_s": e["duration_s"], "coverage_s": round(self.coverage(), 1)})

    def _plan_sfx(self, seq: int, e: dict, host_start: float) -> None:
        """Decide which of the host's rendered SFX overlays air (coin flip,
        rate budget; sfx_plan). Missing/retired sfx items are skipped: a
        stinger is optional, never a stall (SFX.md §7.4)."""
        if e["type"] not in SFX_HOST_TYPES:
            return  # songs, intros and news never carry SFX
        it = self.store.get_item(e["item_id"])
        try:
            meta = json.loads(it["meta_json"]) if it and it.get("meta_json") else {}
        except ValueError:
            meta = {}
        cues = [c for c in (meta.get("sfx") or [])
                if isinstance(c, dict) and self._sfx_airable(c.get("item_id"))]
        if not cues:
            return
        horizon = host_start - self.cfg.sfx.window_s
        self._stinger_starts = [t for t in self._stinger_starts if t > horizon]
        overlays, added = plan_overlays(e["type"], host_start, e["duration_s"], cues,
                                        self._stinger_starts, self.rng, self.cfg.sfx)
        self._stinger_starts.extend(added)
        if overlays:
            self._sfx_by_seq[seq] = overlays

    def _sfx_airable(self, item_id: object) -> bool:
        if not isinstance(item_id, int):
            return False
        it = self.store.get_item(item_id)
        return bool(it and it["type"] == "sfx" and not it["retired"])

    def overlays_for(self, seq: int) -> list[dict]:
        """SFX overlays committed with program row `seq` (empty if none)."""
        return list(self._sfx_by_seq.get(seq, []))

    def _settle_aired(self) -> None:
        """Mark committed request songs 'aired' once the playhead reaches them."""
        if not self._pending_aired:
            return
        pos = self.position()
        for i, r in enumerate(self._items):
            rid = self._pending_aired.get(r["seq"])
            if rid is not None and self._cum[i] <= pos + 1e-6:
                del self._pending_aired[r["seq"]]
                self.store.mark_request_aired(rid)
                log.info("request.aired", extra={
                    "request_id": rid, "item_id": r["item_id"], "seq": r["seq"]})

    def _trim(self) -> None:
        """Drop committed rows fully behind the playhead (keep a little history)."""
        keep = self.cfg.playout.window_trim_keep_s
        pos = self.position()
        cutoff = pos - keep
        threshold_seq: int | None = None
        for i, r in enumerate(self._items):
            if self._cum[i] + r["duration_s"] < cutoff:
                threshold_seq = r["seq"]
                # Phrase lifecycle: a committed phrase row that fell fully
                # behind the playhead has aired — mark its job terminal
                # (RADIO §5.5; TUI.md §6). Defensive: a callback error must
                # not break playout or the trim itself.
                if r["type"] == "operator_phrase" and self.on_phrase_aired:
                    try:
                        self.on_phrase_aired(r["item_id"])
                    except Exception as e:  # noqa: BLE001
                        log.warning("phrase.aired_mark_failed: %s", e)
            else:
                break
        if threshold_seq is not None:
            log.debug("program.trim", extra={"before_seq": threshold_seq})
            # truncate_program_before keeps threshold_seq itself. Preserve its
            # absolute start so trimming cannot advance the audible playhead.
            retained_index = next(
                i for i, row in enumerate(self._items) if row["seq"] == threshold_seq
            )
            self._program_start = self._cum[retained_index]
            self.store.truncate_program_before(threshold_seq)
            for s in [s for s in self._sfx_by_seq if s < threshold_seq]:
                del self._sfx_by_seq[s]
            self._rebuild_program()

    def _materialize(self, type_: str) -> list[dict]:
        st = self.build_state()
        try:
            if type_ == "song":
                return self._pick_song(st)
            if type_ == "liner":
                return self._pick_liner(st)
            if type_ == "dj_talk":
                return self._pick_dj(st)
            if type_ == "field_report":
                return self._pick_fresh_talk("field_report")
            if type_ == "commercial_break":
                return self._pick_commercial_break(st)
            if type_ == "news":
                return self._pick_news(st)
            if type_ == "promo":
                return self._pick_promo()
            if type_ == "time_check":
                return self._pick_time_check()
            if type_ == "station_id":
                return self._pick_liner(st)
        except Exception as e:  # inventory race or constraint: skip
            log.warning("materialize %s failed: %s", type_, e)
        return []

    # -------------------------------------------------------------- pickers
    def _pick_song(self, st: PlayoutState) -> list[dict]:
        """Pick a song (PRIORITIES §2 tier table). Ready listener-request songs
        jump the line (oldest first) unchanged; their pre-made intro is glued on.
        Otherwise the stock logic: fresh (never-aired) songs air newest-first
        (LIFO), then recycled songs are a weighted-random draw over those that
        clear spacing. Returns [intro?, song] so the intro airs before its song.
        Air/rotation history is committed to the store at commit time (§4)."""
        reqs = self._uncommitted_ready_requests()
        after_dj = bool(self._items) and self._items[-1]["type"] == "dj_talk"
        # a request's intro is its on-air thank-you: don't take the request in
        # a slot where the intro would be dropped (right after dj_talk); it
        # takes the next song slot instead (4.7)
        if reqs and after_dj and reqs[0].get("intro_item_id"):
            return []  # no stock song either: the request owns the next slot
        if reqs:
            req = reqs[0]
            song_id = req.get("song_item_id")
            it = self.store.get_item(song_id) if song_id else None
            if it and it["type"] == "song" and not it["retired"]:
                entries = self._glue_intro(it["id"], it["duration_s"])
                for e in entries:
                    if e["type"] == "song":
                        e["request_id"] = req["id"]
                return entries
        return self._pick_stock_song(st)

    def _pick_stock_song(self, st: PlayoutState) -> list[dict]:
        """Tier 1 (fresh, LIFO) then Tier 2 (recycled, weighted random)."""
        pool = self._eligible_songs()
        if not pool:
            return []
        fresh = [i for i in pool if i["fresh"]]
        if fresh:
            # tier 1: never-aired songs air newest-first (LIFO) at the next song
            # slot; the drought rule (§5.2) decides when that slot opens
            cand = max(fresh, key=lambda i: i["id"])
            return self._glue_intro(cand["id"], cand["duration_s"])
        # tier 2: weighted random over recycled songs that clear spacing. The
        # full first spacing step (song_min_spacing_s[0], the 1 h preference) is
        # honored whenever the pool allows; only when nothing clears it do we
        # relax toward the 15-min floor (same relaxation as today). Weighting
        # never breaks the floor — it only orders what already passes.
        now = self._slot_wall(self._total)
        # PRIORITIES §6 starvation guard: if an aired song has waited past
        # starvation_h, it goes next regardless of the weighted draw. Spacing
        # is already satisfied (this is the eligible pool).
        rot = self.cfg.playout.song_rotation
        limit = rot.starvation_h * 3600.0 - rot.starvation_margin_s
        starving = [i for i in pool
                    if i.get("last_aired_at") is not None
                    and now - i["last_aired_at"] >= limit]
        if starving:
            cand = min(starving, key=lambda i: i["last_aired_at"])
            return self._glue_intro(cand["id"], cand["duration_s"])
        min_gap = float(self.cfg.playout.song_min_spacing_s[0])
        spaced = [i for i in pool
                  if i.get("last_aired_at") is None or now - i["last_aired_at"] >= min_gap]
        candidates = self._genre_filtered(spaced or pool, st.recent_song_genres)
        cand = self._weighted_pick(candidates, now)
        return self._glue_intro(cand["id"], cand["duration_s"])

    def _genre_filtered(self, items: list[dict], recent_genres: list[str]) -> list[dict]:
        """Soft genre no-repeat (§5.2): drop songs whose genre is among the last
        3 aired, but if that empties the pool keep them (a soft rule, not a wall)."""
        if not recent_genres:
            return items
        kept = [i for i in items if (i.get("genre") or "") not in recent_genres]
        return kept or items

    def _song_weight(self, it: dict, now_wall: float) -> float:
        """PRIORITIES §3 recycled-song weight. youth halves every half_life_h
        hours (floored at youth_floor so old songs never starve); wait grows 0..1
        while unheard, saturating at wait_target_h. NULL last_aired_at = unheard
        forever (wait 1.0)."""
        rot = self.cfg.playout.song_rotation
        created = _parse_iso(it.get("created_at")) or now_wall
        age_h = max(0.0, (now_wall - created) / 3600.0)
        last = it.get("last_aired_at")
        since_h = float("inf") if last is None else max(0.0, (now_wall - last) / 3600.0)
        youth = max(rot.youth_floor, 0.5 ** (age_h / rot.half_life_h))
        wait = min(1.0, since_h / rot.wait_target_h)
        weight = youth * (rot.wait_floor + (1 - rot.wait_floor) * wait)
        # VOTING.md Step 8 (off by default): let ratings nudge the recycled
        # draw. A 3-turkey song is neutral; a 1-turkey song keeps a quarter of
        # its pull, a 5-turkey song gets half again. Store read only — pure of
        # time/random — and it never breaks spacing (it orders songs that
        # already pass it) or the starvation guard above.
        ratings = self.cfg.ratings
        if ratings.rotation_weight:
            agg = self.store.song_rating(int(it["id"]))
            if agg["count"] >= ratings.rotation_min_votes and agg["avg"] is not None:
                weight *= min(1.5, max(0.25, agg["avg"] / 3.0))
        return weight

    def _weighted_pick(self, candidates: list[dict], now_wall: float) -> dict:
        """Weighted random over eligible recycled songs (injected RNG, pure)."""
        weights = [self._song_weight(i, now_wall) for i in candidates]
        total = sum(weights)
        if total <= 0:
            return self.rng.choice(candidates)
        return self.rng.choices(candidates, weights=weights, k=1)[0]

    def _request_hold(self) -> bool:
        """True while a listener request is still waiting for its slot.

        Requests are produced FIFO and their song + intro land together, so the
        oldest uncommitted pending request is the one to hold for. The hold is
        bounded per request by `playout.request_hold_max_s`: a backend outage
        that leaves a request queued forever must not pin the station at a
        shallow lookahead (the stock song fallback resumes after the limit)."""
        if self.cfg.playout.request_hold_max_s <= 0:
            return False  # disabled: stock keeps airing; ready requests splice in
        committed = set(self._pending_aired.values())
        pending = [r for r in self.store.pending_requests(
                       bury_threshold=self.cfg.voting.bury_threshold)
                   if r["id"] not in committed]
        if not pending:
            self._hold_request_id = None
            self._hold_since = 0.0
            return False
        rid = int(pending[0]["id"])
        if self._hold_request_id != rid:
            self._hold_request_id = rid
            self._hold_since = self.clock.now()
        return (self.clock.now() - self._hold_since
                <= self.cfg.playout.request_hold_max_s)

    def _uncommitted_ready_requests(self) -> list[dict]:
        """Ready requests not already committed (they stay 'ready' until the
        playhead reaches them, so filter out the ones waiting in the window)."""
        pending = set(self._pending_aired.values())
        return [r for r in self.store.ready_request_songs() if r["id"] not in pending]

    def _eligible_songs(self) -> list[dict]:
        """Fresh songs, plus recycled songs last aired at least the smallest
        song_min_spacing_s ago (the relaxation floor, §5.2). Below that a song
        is not airable; the slot goes to a commercial, DJ talk, news or liner.
        A NULL last_aired_at (never aired, or aired before the ledger) counts
        as unheard forever, so it is always airable (PRIORITIES §4, root 1)."""
        floor = float(min(self.cfg.playout.song_min_spacing_s))
        now = self._slot_wall(self._total)
        return [i for i in self.store.list_items("song")
                if not i["emergency"] and (
                    i["fresh"] or i.get("last_aired_at") is None or
                    now - i["last_aired_at"] >= floor)]

    def _intro_for(self, song_id: int) -> dict | None:
        """The intro naming `song_id`, if any. Stock intros recycle (revision
        11); a request intro is one-shot, so an aired one is skipped."""
        for it in self.store.list_items("intro"):
            if it["retired"] or it["emergency"]:
                continue
            try:
                meta = json.loads(it["meta_json"]) if it.get("meta_json") else {}
            except Exception:
                continue
            if meta.get("song_item_id") != song_id:
                continue
            if meta.get("request_id") is not None and not it["fresh"]:
                continue
            return it
        return None

    def _glue_intro(self, song_id: int, song_dur: float) -> list[dict]:
        """If this song has an unaired intro, materialize [intro, song] with the
        intro first. Intros are consumed when committed and never recycled. A
        recycled song with no fresh intro airs alone. DJ adjacency: if the last
        committed item is dj_talk, skip the intro (two DJ segments back to back
        is worse than a missing intro — intros are cheap, the song is not; 4.7)."""
        it = self._intro_for(song_id)
        if it is not None and not (self._items and self._items[-1]["type"] == "dj_talk"):
            try:
                meta = json.loads(it["meta_json"]) if it.get("meta_json") else {}
            except Exception:
                meta = {}
            # request intros are contextual: one-shot (AGENTS rule 10)
            return [
                {"item_id": it["id"], "type": "intro",
                 "duration_s": it["duration_s"],
                 "consume": meta.get("request_id") is not None},
                {"item_id": song_id, "type": "song",
                 "duration_s": song_dur, "consume": True},
            ]
        return [{"item_id": song_id, "type": "song",
                 "duration_s": song_dur, "consume": True}]

    def _fresh_intro_for(self, song_id: int) -> dict | None:
        """The unaired intro naming `song_id`, if any."""
        for it in self.store.list_items("intro"):
            if not it["fresh"] or it["emergency"]:
                continue
            try:
                meta = json.loads(it["meta_json"]) if it.get("meta_json") else {}
            except Exception:
                continue
            if meta.get("song_item_id") == song_id:
                return it
        return None

    def _pick_liner(self, st: PlayoutState) -> list[dict]:
        items = [i for i in self.store.list_items("liner") + self.store.list_items("station_id")
                 if not i["emergency"] and not i["retired"]]
        if not items:
            return []
        # B4: no liner inside its committed-item window or wall-time gap
        gap_items = self.cfg.liners.repeat_gap_items
        recent = {r["item_id"] for r in self._items[-gap_items:]}
        now = self._slot_wall(self._total)
        unspent = [i for i in items
                   if i["id"] not in recent
                   and now - self._last_liner_air.get(i["id"], -1e9)
                   >= self.cfg.liners.repeat_gap_s]
        if not unspent and not self._may_repeat():
            return []
        it = self.rng.choice(unspent or items)
        return [{"item_id": it["id"], "type": it["type"], "duration_s": it["duration_s"],
                 "consume": True}]

    def _pick_dj(self, st: PlayoutState) -> list[dict]:
        return self._pick_fresh_talk("dj_talk")

    def _pick_fresh_talk(self, type_: str) -> list[dict]:
        """An unaired clip that is still valid when it AIRS (not when it is
        committed), newest first (revision 4). Consumed on commit: contextual
        talk is never recycled (AGENTS rule 10)."""
        air_wall = self._slot_wall(self._total)
        items = []
        for i in self.store.list_items(type_):
            if not i["fresh"] or i["emergency"]:
                continue
            exp = _parse_iso(i.get("expires_at"))
            if exp is not None and exp < air_wall:
                continue  # would expire before it airs (A1)
            items.append(i)
        if not items:
            return []
        # Newest first: the producer makes ~15/hour and the station airs ~8.6,
        # so a uniform draw let 14-minute-old clips air as often as fresh ones.
        it = max(items, key=lambda i: i["id"])
        return [{"item_id": it["id"], "type": type_, "duration_s": it["duration_s"],
                 "consume": True, "expires_at": it.get("expires_at")}]

    # --------------------------------------------------- promo / time check
    def _promos(self) -> list[dict]:
        """Recyclable promos still valid when they would air (A1)."""
        air_wall = self._slot_wall(self._total)
        out = []
        for it in self.store.list_items("promo"):
            if it["emergency"]:
                continue
            exp = _parse_iso(it.get("expires_at"))
            if exp is not None and exp < air_wall:
                continue
            out.append(it)
        return out

    def _promo_kind_gap(self, kind: str) -> float:
        p = self.cfg.promotions
        if kind == "request_line":
            return float(p.request_line_gap_s)
        if kind == "paid_tier":
            return float(p.paid_tier_gap_s)
        if kind == "tree_fiddy":
            return float(p.tree_fiddy_gap_s)
        return float(p.promo_min_spacing_s)  # campaign

    def _due_promos(self) -> list[tuple[dict, dict]]:
        """Promos whose per-kind spacing has passed, newest first."""
        now = self._air_clock
        out: list[tuple[dict, dict]] = []
        for it in self._promos():
            meta = json.loads(it["meta_json"]) if it.get("meta_json") else {}
            kind = meta.get("kind") or "campaign"
            last = self._last_promo_kind_air.get(kind, -1e9)
            if now - last < self._promo_kind_gap(kind):
                continue
            out.append((it, meta))
        return out

    def _promo_available(self) -> bool:
        if self._air_clock - self._last_promo_air < self.cfg.promotions.promo_min_spacing_s:
            return False
        return bool(self._due_promos())

    def _campaign_due(self) -> bool:
        """An active campaign is under its per-hour target for the committed
        hour (a soft nudge; never forced past a constraint). Counts only
        campaign promos: the steady kinds have their own gaps."""
        air_wall = self._slot_wall(self._total)
        camp = active_campaign(air_wall, self.cfg.station.timezone,
                               self.cfg.promotions.campaigns)
        if camp is None:
            return False
        target = target_per_hour(air_wall, self.cfg.station.timezone, camp)
        if target <= 0:
            return False
        cutoff = self._total - 3600.0
        self._promo_log = [(s, k) for s, k in self._promo_log if s >= cutoff]
        aired = sum(1 for _s, k in self._promo_log if k == "campaign")
        return aired < target

    def _steady_promo_due(self) -> bool:
        """A paid-tier or Tree Fiddy promo is past its own gap, so the promo
        weight gets a nudge (steady self-promotion, not a burst)."""
        now = self._air_clock
        for it in self._promos():
            meta = json.loads(it["meta_json"]) if it.get("meta_json") else {}
            kind = meta.get("kind")
            if kind not in ("paid_tier", "tree_fiddy"):
                continue
            if now - self._last_promo_kind_air.get(kind, -1e9) >= self._promo_kind_gap(kind):
                return True
        return False

    def _pick_promo(self) -> list[dict]:
        items = self._due_promos()
        if not items:
            return []
        now = self._air_clock
        # The most overdue kind wins. When several are equally overdue (fresh
        # start: all gaps reset), the kind with the LONGEST gap goes first, so
        # the hourly Tree Fiddy Report is not starved by the 30-minute paid
        # promo. Newest first inside a kind.
        def rank(p: tuple[dict, dict]) -> tuple[float, float, int]:
            kind = p[1].get("kind") or "campaign"
            return (now - self._last_promo_kind_air.get(kind, -1e9),
                    self._promo_kind_gap(kind), p[0]["id"])
        it, meta = max(items, key=rank)
        return [{"item_id": it["id"], "type": "promo",
                 "duration_s": it["duration_s"], "consume": True,
                 "expires_at": it.get("expires_at"),
                 "promo_kind": meta.get("kind") or "campaign"}]

    def _near_mark(self, air_wall: float) -> bool:
        """Does this wall time sit within the window of a configured clock mark
        (:00/:15/:30/:45)?"""
        marks = self.cfg.talk.time_check_marks_min
        if not marks:
            return False
        minute = int((air_wall % 3600) // 60)
        d = min(abs(minute - k) for k in marks)
        d = min(d, 60 - d)  # wrap across the hour
        return d * 60 <= self.cfg.talk.time_check_window_s

    def _time_check_item(self) -> dict | None:
        """The clock read for the slot that is about to air: due only near a
        quarter mark, and the variant must be the slot's own 5-minute bucket so
        the spoken time is never more than the rounding off."""
        if self._air_clock - self._last_time_check_air < self.cfg.talk.time_check_min_gap_s:
            return None
        air_wall = self._slot_wall(self._total)
        if not self._near_mark(air_wall):
            return None
        bucket = int(air_wall // 300 * 300)
        for it in self.store.list_items("time_check"):
            if it["retired"] or it["emergency"]:
                continue
            meta = json.loads(it["meta_json"]) if it.get("meta_json") else {}
            if meta.get("bucket") != bucket:
                continue
            exp = _parse_iso(it.get("expires_at"))
            if exp is not None and exp < air_wall:
                continue
            return it
        return None

    def _pick_time_check(self) -> list[dict]:
        it = self._time_check_item()
        if it is None:
            return []
        return [{"item_id": it["id"], "type": "time_check",
                 "duration_s": it["duration_s"], "consume": True,
                 "expires_at": it.get("expires_at")}]

    def _pick_commercial_break(self, st: PlayoutState) -> list[dict]:
        items = [i for i in self.store.list_items("commercial") if not i["emergency"]]
        if not items:
            return []
        # cap break size so the non-song run limit is honored (a break = 1-2 items)
        cap_limit = self.cfg.playout.max_consecutive_non_song
        trailing_run = 0
        for t in reversed(st.recent_types):
            if t == "song":
                break
            trailing_run += 1
        # leave the last non-song slot for words when none aired yet (§5.2)
        cap = max(1, cap_limit - trailing_run - (1 if st.callout_pending else 0))
        n = 1 if len(items) == 1 else self.rng.randint(1, min(cap, len(items)))
        now = self._air_clock
        min_gap = float(self.cfg.playout.commercial_min_spacing_s)
        # prefer spots aired longer ago than the min spacing; only reuse a
        # recently-aired spot to bridge imminent dead air (_may_repeat).
        spaced = [i for i in items
                  if now - self._last_comm_air.get(i["id"], -1e9) >= min_gap]
        pool = spaced if len(spaced) >= n or not self._may_repeat() else items
        if not pool:
            return []
        # sample WITHOUT replacement: never the same spot twice in one break
        chosen = self.rng.sample(pool, k=min(n, len(pool)))
        out = []
        for it in chosen:
            self._last_comm_air[it["id"]] = now
            out.append({"item_id": it["id"], "type": "commercial",
                        "duration_s": it["duration_s"], "consume": True})
        return out

    def _pick_news(self, st: PlayoutState) -> list[dict]:
        ni = self._news_valid()
        if not ni:
            return []
        item_id = ni["id"]
        # render the news bulletin via the voice pipeline at commit time is NOT allowed
        # (playout never blocks). Producer pre-renders news into a 'news' audio item;
        # here we just commit the pre-rendered cliplike item if it exists.
        return [{"item_id": item_id, "type": "news", "duration_s": ni["duration_s"],
                 "consume": True, "expires_at": ni.get("expires_at")}]

    # ------------------------------------------------- request splice (§5.2)
    def _splice_ready_requests(self) -> None:
        """Splice every ready, uncommitted request song into the next song slot.

        This is what makes a request air *as soon as* its song + intro are
        rendered: appending at the tail would leave it behind the already-
        committed lookahead (the 'up next for three songs' bug). Each splice
        bumps the programme revision; the revision-aware client cancels its
        obsolete future sources and refetches the snapshot (TUI.md §5, §9.2)."""
        committed = set(self._pending_aired.values())
        for req in self.store.ready_request_songs():
            if req["id"] in committed:
                continue
            if self.require_published and self._awaiting_publish(req):
                continue
            self._publish_wait_since.pop(int(req["id"]), None)
            self.place_request_next(int(req["id"]))
            committed.add(req["id"])

    def _awaiting_publish(self, req: dict) -> bool:
        """True while a ready request's song or intro MP3 is not yet in S3 and
        the wait is inside PUBLISH_WAIT_MAX_S (RADIO §5.2, §8.4). Emergency
        items and non-MP3 media are never published, so they never wait."""
        rid = int(req["id"])
        unpublished: list[int] = []
        for key in ("song_item_id", "intro_item_id"):
            iid = req.get(key)
            it = self.store.get_item(int(iid)) if iid else None
            if (it and not it.get("s3_key") and not it.get("emergency")
                    and str(it.get("media_path") or "").endswith(".mp3")):
                unpublished.append(int(it["id"]))
        if not unpublished:
            self._publish_wait_since.pop(rid, None)
            return False
        now = self.clock.now()
        since = self._publish_wait_since.get(rid)
        if since is None:
            self._publish_wait_since[rid] = now
            log.info("request.awaiting_publish", extra={
                "request_id": rid, "items": unpublished})
            return True
        if now - since >= self.PUBLISH_WAIT_MAX_S:
            log.warning("request.publish_wait_expired", extra={
                "request_id": rid, "items": unpublished,
                "waited_s": round(now - since, 1)})
            return False  # air it from the origin route rather than strand it
        return True

    def _request_splice_index(self) -> int:
        """First future item (or the tail). The request lands immediately
        after whatever is playing, before the next committed item."""
        pos = self.position()
        for i in range(len(self._items)):
            if self._cum[i] > pos + 1e-6:
                return i
        return len(self._items)

    def _next_nonsong_index(self, start: int) -> int:
        """First non-song at/after `start` (or the tail): the fallback splice
        point when no liner is available to separate a request from a song."""
        for i in range(start, len(self._items)):
            if self._items[i]["type"] != "song":
                return i
        return len(self._items)

    def place_request_next(self, request_id: int) -> int | None:
        """Splice a ready request's intro + song into the next song slot.

        Future rows are renumbered with fresh seqs (never reusing an old
        occurrence id), and the revision is bumped so revision-aware clients
        refetch the snapshot. The request stays `ready` until the playhead
        reaches its song (`_settle_aired`). Returns the new song seq, or None
        when the request is not ready / has no song / is already committed."""
        req = self.store.get_request(request_id)
        if not req or req["status"] != "ready":
            return None
        if request_id in self._pending_aired.values():
            return None
        song_id = req.get("song_item_id")
        if not song_id:
            return None
        song = self.store.get_item(int(song_id))
        if not song or song["type"] != "song" or song["retired"]:
            return None
        intro = (self.store.get_item(int(req["intro_item_id"]))
                 if req.get("intro_item_id") else None)
        entries: list[dict] = []
        if intro and intro["type"] == "intro" and not intro["retired"]:
            entries.append({"item_id": intro["id"], "type": "intro",
                            "duration_s": intro["duration_s"], "consume": True})
        entries.append({"item_id": song["id"], "type": "song",
                        "duration_s": song["duration_s"], "consume": True})

        k = self._request_splice_index()
        # The request's thank-you intro must not sit right after DJ talk (§5.2):
        # let a non-song bridge it, or place it after the next item.
        if k > 0 and self._items[k - 1]["type"] == "dj_talk":
            bridge = self._pick_callout_fallback()
            if bridge:
                entries = bridge + entries
            elif k < len(self._items):
                k += 1  # the next item bridges DJ talk and the intro
            else:
                return None  # no legal slot this cycle
        # The no-song-after-song rule still applies: if the request would land
        # directly before a committed song, put a liner/ID after it (or, with no
        # liner available, place it after the next non-song instead). Without an
        # intro, a preceding song needs the same bridge before the request.
        if not intro and k > 0 and self._items[k - 1]["type"] == "song":
            bridge = self._pick_callout_fallback()
            if not bridge:
                return None  # cannot place legally; leave it for the tail path
            entries = bridge + entries
        if k < len(self._items) and self._items[k]["type"] == "song":
            bridge = self._pick_callout_fallback()
            if bridge:
                entries = entries + bridge
            else:
                k = self._next_nonsong_index(k)
        after = self._items[k:]
        high = int(self.store.max_seq() or 0)
        spliced = [(e, high + 1 + i) for i, e in enumerate(entries)]
        shifted = [(r, high + 1 + len(entries) + i) for i, r in enumerate(after)]
        remap = {r["seq"]: seq for r, seq in shifted}
        # remap live references before the rows move
        if remap:
            self._pending_aired = {
                remap.get(s, s): rid for s, rid in self._pending_aired.items()}
            self._sfx_by_seq = {
                remap.get(s, s): v for s, v in self._sfx_by_seq.items()}
        for e, seq in spliced:
            self.store.append_program_at(
                item_id=e["item_id"], type_=e["type"],
                duration_s=e["duration_s"], seq=seq)
        for r, seq in sorted(shifted, key=lambda x: x[0]["seq"], reverse=True):
            self.store.update_program_seq(r["seq"], seq)
        if shifted:
            self.store.set_program_seq_high(shifted[-1][1])
        song_seq = next(seq for e, seq in spliced if e["type"] == "song")
        self._pending_aired[song_seq] = request_id
        for e in entries:
            if e.get("consume"):
                self.store.mark_aired(e["item_id"])
        self._air_clock += sum(e["duration_s"] for e in entries)
        self._rebuild_program()
        # Persist the song's scheduled air time (as _append does for songs) so
        # it cannot be re-picked as recycled stock inside its spacing window.
        idx = next(i for i, r in enumerate(self._items) if r["seq"] == song_seq)
        self.store.commit_song_air(song["id"], self._slot_wall(self._cum[idx]))
        # Teed up: while frozen, _slot_wall is "now + offset from the frozen
        # playhead", i.e. when the song would air if woken now (RADIO §11).
        self.store.mark_request_due(request_id, self._slot_wall(self._cum[idx]))
        self.advance_revision()
        log.info("request.spliced", extra={
            "request_id": request_id, "item_id": song["id"],
            "seq": song_seq, "revision": self.revision,
            "due_in_s": round(self._cum[idx] - self.position(), 1)})
        return song_seq

    def place_song_next(self, item_id: int) -> int | None:
        """Operator: air this song next (RADIO §11 admin route).

        Splices it into the slot right after whatever is playing, with a voice
        bridge when the preceding committed item is a song and another when it
        would land directly before a song (the words-between-songs rule, §5.2).
        Future rows are renumbered with fresh seqs and the revision is bumped,
        so revision-aware clients refetch and cancel obsolete future sources.
        Returns the new seq, or None when the item is not an airable song or no
        legal slot exists this cycle.
        """
        song = self.store.get_item(int(item_id))
        if not song or song["type"] != "song" or song["retired"] or song["emergency"]:
            return None
        if not Path(str(song["media_path"])).exists():
            return None
        entries: list[dict] = [{"item_id": song["id"], "type": "song",
                                "duration_s": song["duration_s"], "consume": True}]
        k = self._request_splice_index()
        if k > 0 and self._items[k - 1]["type"] == "song":
            bridge = self._pick_callout_fallback()
            if not bridge:
                return None      # no voice item to separate two songs
            entries = bridge + entries
        if k < len(self._items) and self._items[k]["type"] == "song":
            bridge = self._pick_callout_fallback()
            if bridge:
                entries = entries + bridge
            else:
                k = self._next_nonsong_index(k)
        after = self._items[k:]
        high = int(self.store.max_seq() or 0)
        spliced = [(e, high + 1 + i) for i, e in enumerate(entries)]
        shifted = [(r, high + 1 + len(entries) + i) for i, r in enumerate(after)]
        remap = {r["seq"]: seq for r, seq in shifted}
        if remap:
            self._pending_aired = {
                remap.get(s, s): rid for s, rid in self._pending_aired.items()}
            self._sfx_by_seq = {
                remap.get(s, s): v for s, v in self._sfx_by_seq.items()}
        for e, seq in spliced:
            self.store.append_program_at(
                item_id=e["item_id"], type_=e["type"],
                duration_s=e["duration_s"], seq=seq)
        for r, seq in sorted(shifted, key=lambda x: x[0]["seq"], reverse=True):
            self.store.update_program_seq(r["seq"], seq)
        if shifted:
            self.store.set_program_seq_high(shifted[-1][1])
        song_seq = next(seq for e, seq in spliced if e["type"] == "song")
        for e in entries:
            if e.get("consume"):
                self.store.mark_aired(e["item_id"])
        self._air_clock += sum(e["duration_s"] for e in entries)
        self._rebuild_program()
        idx = next(i for i, r in enumerate(self._items) if r["seq"] == song_seq)
        self.store.commit_song_air(song["id"], self._slot_wall(self._cum[idx]))
        self.advance_revision()
        log.info("program.song_placed_next", extra={
            "item_id": song["id"], "seq": song_seq, "revision": self.revision})
        return song_seq

    # ------------------------------------------------------- operator phrases
    def _drain_phrases(self) -> None:
        """Append ready operator phrases at the committed tail (head-of-queue).
        A phrase whose tail predecessor is dj_talk/news/another phrase stays
        queued and is retried next cycle — the window keeps extending, so a
        legal tail always clears in time. Runs inside the scheduler loop, so
        program edits stay serialized."""
        while self.pending_phrases:
            ph = self.pending_phrases[0]
            seq = self.place_phrase(ph["item_id"], ph["duration_s"])
            if seq is None:
                break  # held; keep it and retry next cycle
            self.pending_phrases.popleft()
            log.info("program.phrase_placed", extra={
                "item_id": ph["item_id"], "seq": seq,
                "revision": self.revision})

    def place_phrase(self, item_id: int, duration_s: float) -> int | None:
        """Append an operator phrase at the TAIL of the committed program.

        Phrases are still tail-appended (no need to splice them ahead of stock
        songs); the revision-aware browser refetches on the bump and hears it at
        the tail. A tail append is always a brand-new seq, so it is safe even for
        a client that has already fetched the current window.

        The tail is legal when its predecessor is not dj_talk / news / another
        phrase (RADIO §11; TUI.md §6). Otherwise the caller defers and retries
        next cycle; the committed window keeps extending, so a legal tail
        appears within a commit cycle. Bumps the programme revision because
        already-published future playback changes (§5.5). Returns the new seq,
        or None if the tail is not legal.
        """
        items = self._items
        if items and items[-1]["type"] in PHRASE_ADJACENT:
            return None
        new_seq = (self.store.max_seq() or 0) + 1
        self.store.append_program_at(
            item_id=item_id, type_="operator_phrase", duration_s=duration_s,
            seq=new_seq)
        # one-shot: consumed the moment it is placed; never recycled (rule 10)
        self.store.mark_aired(item_id)
        self._rebuild_program()
        self.advance_revision()
        return new_seq

    # ------------------------------------------------------------------ run
    async def run(self) -> None:
        log.info("scheduler running: lookahead=%ss", self.cfg.playout.committed_lookahead_s)
        while True:
            try:
                self.commit_lookahead()
                self._drain_phrases()
            except Exception as e:
                log.exception("scheduler cycle failed: %s", e)
            await asyncio.sleep(self.REFILL_INTERVAL_S)


_CAT_TO_TYPE = {
    "news": "news",
    "commercial_break": "commercial",
    "song": "song",
}

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

from pilgrim.config import RNG, Clock, Config
from pilgrim.pipelines.sfx import SFX_HOST_TYPES
from pilgrim.selector import PlayoutState, Selector
from pilgrim.sfx_plan import plan_overlays
from pilgrim.store import Store

# Committed types that put words between songs (§5.2). `intro` counts: it
# names the song that follows it.
CALLOUT_TYPES = frozenset({"liner", "station_id", "dj_talk", "field_report", "intro"})

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
        self._air_clock = 0.0  # monotonic committed-program time (independent of trimming)
        # in-memory cumulative mapping built each cycle
        self._program_start = 0.0  # absolute start of retained program history
        self._items: list[dict] = []
        self._cum: list[float] = []  # absolute start times (program seconds)
        self._total = 0.0
        self._offset = 0.0  # playhead anchor; shifts forward if the program starves
        # committed request songs not yet on air: seq -> request_id. A request
        # is 'aired' when the playhead reaches its song, not when committed.
        self._pending_aired: dict[int, int] = {}
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
        """Live playhead in program-seconds (advances on the real clock)."""
        return min(self.clock.now() - self._offset, self._total)

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
        st.inventory_counts = {
            "song": self.store.count_usable_of_type("song"),
            "dj_talk": self.store.count_fresh_of_type("dj_talk"),
            "field_report": self.store.count_fresh_of_type("field_report"),
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
        for cat, ctype in (("news", "news"), ("commercial_break", "commercial"), ("song", "song")):
            st.secs_since[cat] = self._secs_since(self._total, ctype)
        ni = self._news_valid()
        if ni:
            st.news_valid = True
            st.news_gravity = ni.get("gravity") or "normal"
        st.callout_pending = self._callout_pending() and not self._next_song_has_intro()
        st.near_top_of_hour = self._near_top_of_hour()
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
        return self._fresh_intro_for(song_id) is not None

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
        # latest not-expired bulletin not yet aired
        for it in reversed(self.store.list_items("news")):
            exp = _parse_iso(it.get("expires_at"))
            if exp is not None and exp < self.clock.wall():
                continue  # expired
            fresh = it.get("fresh")
            if fresh:
                return it
        return None

    # -------------------------------------------------------------- committing
    def commit_lookahead(self) -> None:
        """Append committed items until coverage >= lookahead (or no inventory)."""
        cfg = self.cfg.playout
        lookahead = cfg.committed_lookahead_s
        guard = 0
        # types that came up empty (e.g. every liner is inside its no-repeat
        # window): re-draw without them until something commits
        self._settle_aired()
        blocked: set[str] = set()
        while self.coverage() < lookahead and guard < 100:
            guard += 1
            st = self.build_state()
            for t in blocked:
                st.available[t] = False
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
        seq = self.store.append_program(item_id, e["type"], e["duration_s"])
        self._air_clock += e["duration_s"]  # monotonic: never resets on trim
        self._plan_sfx(seq, e, host_start)
        if e.get("consume"):
            self.store.mark_aired(item_id)
        if e["type"] == "song":
            # persist the wall-clock air (injected clock, never time.time) so
            # spacing + weighting survive a restart (PRIORITIES §4)
            # recorded at its scheduled air time, not commit time: the lead
            # (committed coverage) varies, and spacing is about what listeners hear
            self.store.commit_song_air(item_id, self._slot_wall(host_start))
        if e.get("request_id"):
            # committed, not yet heard: _settle_aired marks it when it airs
            self._pending_aired[seq] = e["request_id"]
            log.info("request.committed", extra={
                "request_id": e["request_id"], "item_id": item_id, "seq": seq})
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
        return youth * (rot.wait_floor + (1 - rot.wait_floor) * wait)

    def _weighted_pick(self, candidates: list[dict], now_wall: float) -> dict:
        """Weighted random over eligible recycled songs (injected RNG, pure)."""
        weights = [self._song_weight(i, now_wall) for i in candidates]
        total = sum(weights)
        if total <= 0:
            return self.rng.choice(candidates)
        return self.rng.choices(candidates, weights=weights, k=1)[0]

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

    def _glue_intro(self, song_id: int, song_dur: float) -> list[dict]:
        """If this song has an unaired intro, materialize [intro, song] with the
        intro first. Intros are consumed when committed and never recycled. A
        recycled song with no fresh intro airs alone. DJ adjacency: if the last
        committed item is dj_talk, skip the intro (two DJ segments back to back
        is worse than a missing intro — intros are cheap, the song is not; 4.7)."""
        it = self._fresh_intro_for(song_id)
        if it is not None and not (self._items and self._items[-1]["type"] == "dj_talk"):
            return [
                {"item_id": it["id"], "type": "intro",
                 "duration_s": it["duration_s"], "consume": True},
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
        # don't replay a liner that aired in the last few committed items
        recent = {r["item_id"] for r in self._items[-10:]}
        unspent = [i for i in items if i["id"] not in recent]
        if not unspent and not self._may_repeat():
            return []
        it = self.rng.choice(unspent or items)
        return [{"item_id": it["id"], "type": it["type"], "duration_s": it["duration_s"],
                 "consume": True}]

    def _pick_dj(self, st: PlayoutState) -> list[dict]:
        return self._pick_fresh_talk("dj_talk")

    def _pick_fresh_talk(self, type_: str) -> list[dict]:
        """An unaired, unexpired contextual clip (dj_talk / field_report).
        Consumed on commit: contextual talk is never recycled (AGENTS rule 10)."""
        now_wall = self.clock.wall()
        items = []
        for i in self.store.list_items(type_):
            if not i["fresh"] or i["emergency"]:
                continue
            exp = _parse_iso(i.get("expires_at"))
            if exp is not None and exp < now_wall:
                continue  # time-mention clip expired (OVERHAUL 5.3)
            items.append(i)
        if not items:
            return []
        it = self.rng.choice(items)
        return [{"item_id": it["id"], "type": type_, "duration_s": it["duration_s"],
                 "consume": True}]

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
                 "consume": True}]

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

        Legacy clients merge fetched rows by seq only (TUI.md §9.2 — the
        revision-aware browser has not landed): a splice inside rows they
        already fetched is silently lost — the phrase never airs — and the
        shifted rows can even be played twice. A tail append is always a brand
        new seq that every client receives on its next fetch, so the tail is
        the only placement a listener is guaranteed to hear.

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
            await asyncio.sleep(2.0)


_CAT_TO_TYPE = {
    "news": "news",
    "commercial_break": "commercial",
    "song": "song",
}

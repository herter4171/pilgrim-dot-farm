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
from datetime import UTC, datetime

from pilgrim.config import RNG, Clock, Config
from pilgrim.selector import PlayoutState, Selector
from pilgrim.store import Store

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
                 clock: Clock, rng: RNG) -> None:
        self.cfg = cfg
        self.store = store
        self.selector = selector
        self.clock = clock
        self.rng = rng
        self._start_wall = time.monotonic()
        self._last_song_air: dict[int, float] = {}  # item_id -> air-clock of last commit
        self._last_comm_air: dict[int, float] = {}  # item_id -> air-clock of last commit
        self._air_clock = 0.0  # monotonic committed-program time (independent of trimming)
        # in-memory cumulative mapping built each cycle
        self._program_start = 0.0  # absolute start of retained program history
        self._items: list[dict] = []
        self._cum: list[float] = []  # absolute start times (program seconds)
        self._total = 0.0
        self._offset = 0.0  # playhead anchor; shifts forward if the program starves
        # The committed program is live-only state: old rows refer to a clock
        # that no longer exists, so start clean on every run (OVERHAUL 2.4).
        self.store.clear_program()
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
            "commercial": self.store.count_usable_of_type("commercial"),
            "liner": self.store.count_usable_of_type("liner"),
        }
        st.available = {
            # a song is only drawable if one clears the spacing floor, so a thin
            # pool never loops the same track back to back (§5.2)
            "song": bool(self._eligible_songs()) or bool(self.store.ready_request_songs()),
            "dj_talk": st.inventory_counts["dj_talk"] > 0,
            "commercial_break": st.inventory_counts["commercial"] > 0,
            "liner": st.inventory_counts["liner"] > 0,
            "news": self._news_valid() is not None,
        }
        st.request_songs_ready = len(self.store.ready_request_songs())
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
        return st

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
        # types that came up empty this tick (e.g. every liner is inside its
        # no-repeat window): re-draw without them instead of retrying them
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
            entries = self._materialize(type_)
            if not entries:
                blocked.add(type_)
                continue
            for e in entries:
                self._append(e)
            self._rebuild_program()
        self._trim()
        cov = self.coverage()
        if cov < 60:
            log.warning("program.low_coverage", extra={
                "coverage_s": round(cov, 1),
                "inventory": self.build_state().inventory_counts})

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
        seq = self.store.append_program(item_id, e["type"], e["duration_s"])
        self._air_clock += e["duration_s"]  # monotonic: never resets on trim
        if e.get("consume"):
            self.store.mark_aired(item_id)
        if e.get("request_id"):
            # a listener-request song hit the air
            self.store.mark_request_aired(e["request_id"])
            log.info("request.aired", extra={
                "request_id": e["request_id"], "item_id": item_id, "seq": seq})
        log.debug("program.commit", extra={
            "seq": seq, "item_id": item_id, "item_type": e["type"],
            "duration_s": e["duration_s"], "coverage_s": round(self.coverage(), 1)})

    def _trim(self) -> None:
        """Drop committed rows fully behind the playhead (keep a little history)."""
        keep = self.cfg.playout.window_trim_keep_s
        pos = self.position()
        cutoff = pos - keep
        threshold_seq: int | None = None
        for i, r in enumerate(self._items):
            if self._cum[i] + r["duration_s"] < cutoff:
                threshold_seq = r["seq"]
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
        """Pick a song. Ready listener-request songs jump the line (oldest
        first); their pre-made intro is glued on. Otherwise the stock logic
        (fresh first, then oldest-aired with min spacing). Returns
        [intro?, song] so the intro airs immediately before its song (4.7)."""
        reqs = self.store.ready_request_songs()
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
        pool = self._eligible_songs()
        if not pool:
            return []
        now = self._air_clock  # monotonic air-clock: where this song will start
        min_gap = float(self.cfg.playout.song_min_spacing_s[0])
        last = st.recent_song_genres

        def key(it):
            aired = self._last_song_air.get(it["id"], float("inf"))
            return (it.get("genre") in last,    # soft: genre freshness
                    0 if it["fresh"] else aired,  # fresh first, else oldest air
                    it["id"])

        fresh = [i for i in pool if i["fresh"]]
        recycled = [i for i in pool if not i["fresh"]]
        if fresh:
            cand = min(fresh, key=key)
        else:
            spaced = [
                i for i in recycled
                if now - self._last_song_air.get(i["id"], -1e9) >= min_gap
            ]
            cand = min(spaced or recycled, key=key)  # relax toward the floor only
        self._last_song_air[cand["id"]] = now
        return self._glue_intro(cand["id"], cand["duration_s"])

    def _eligible_songs(self) -> list[dict]:
        """Fresh songs, plus recycled songs last aired at least the smallest
        song_min_spacing_s ago (the relaxation floor, §5.2). Below that a song
        is not airable; the slot goes to a commercial, DJ talk, news or liner."""
        floor = float(min(self.cfg.playout.song_min_spacing_s))
        now = self._air_clock
        return [i for i in self.store.list_items("song")
                if not i["emergency"] and (
                    i["fresh"] or now - self._last_song_air.get(i["id"], -1e9) >= floor)]

    def _glue_intro(self, song_id: int, song_dur: float) -> list[dict]:
        """If this song has an unaired intro, materialize [intro, song] with the
        intro first. Intros are consumed when committed and never recycled. A
        recycled song with no fresh intro airs alone. DJ adjacency: if the last
        committed item is dj_talk, skip the intro (two DJ segments back to back
        is worse than a missing intro — intros are cheap, the song is not; 4.7)."""
        for it in self.store.list_items("intro"):
            if not it["fresh"] or it["emergency"]:
                continue
            try:
                meta = json.loads(it["meta_json"]) if it.get("meta_json") else {}
            except Exception:
                continue
            if meta.get("song_item_id") != song_id:
                continue
            if self._items and self._items[-1]["type"] == "dj_talk":
                continue
            return [
                {"item_id": it["id"], "type": "intro",
                 "duration_s": it["duration_s"], "consume": True},
                {"item_id": song_id, "type": "song",
                 "duration_s": song_dur, "consume": True},
            ]
        return [{"item_id": song_id, "type": "song",
                 "duration_s": song_dur, "consume": True}]

    def _pick_liner(self, st: PlayoutState) -> list[dict]:
        items = [i for i in self.store.list_items("liner") if not i["emergency"]]
        if not items:
            items = [i for i in self.store.list_items("station_id") if not i["emergency"]]
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
        now_wall = self.clock.wall()
        items = []
        for i in self.store.list_items("dj_talk"):
            if not i["fresh"] or i["emergency"]:
                continue
            exp = _parse_iso(i.get("expires_at"))
            if exp is not None and exp < now_wall:
                continue  # time-mention clip expired (OVERHAUL 5.3)
            items.append(i)
        if not items:
            return []
        it = self.rng.choice(items)
        return [{"item_id": it["id"], "type": "dj_talk", "duration_s": it["duration_s"],
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
        cap = max(1, cap_limit - trailing_run)
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

    # ------------------------------------------------------------------ run
    async def run(self) -> None:
        log.info("scheduler running: lookahead=%ss", self.cfg.playout.committed_lookahead_s)
        while True:
            try:
                self.commit_lookahead()
            except Exception as e:
                log.exception("scheduler cycle failed: %s", e)
            await asyncio.sleep(2.0)


_CAT_TO_TYPE = {
    "news": "news",
    "commercial_break": "commercial",
    "song": "song",
}

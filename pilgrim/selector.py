"""Segment selectors (RADIO.md §5). Phase 1 ships RandomSelector as a pure
function of (state, rng) so tests are deterministic with a seed.
"""
from __future__ import annotations

from typing import Protocol

from pilgrim.config import RNG, Config

# Segment types the selector chooses from.
SEGMENTS = ["song", "dj_talk", "commercial_break", "liner", "news"]

# Segment types whose inventory must be present for the type to be drawable.
INVENTORY_TYPES = {
    "song": "song",
    "dj_talk": "dj_talk",
    "commercial_break": "commercial",
    "liner": "liner",
    "news": "news",
}


class PlayoutState:
    """Snapshot of everything the selector may read. Built fresh each draw."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.available: dict[str, bool] = {s: False for s in SEGMENTS}
        # counts of freshly-air-able inventory per type
        self.inventory_counts: dict[str, int] = {}
        # most recent program item types, most recent LAST
        self.recent_types: list[str] = []
        # genres of the last N songs (most recent last)
        self.recent_song_genres: list[str] = []
        # seconds (program time) since the last item of a category aired
        self.secs_since: dict[str, float] = {
            "news": float("inf"), "commercial_break": float("inf"), "song": float("inf")}
        self.news_valid: bool = False
        self.news_gravity: str = "normal"
        self.pending_dj_targets: int = 0  # how many dj_talk items are pre-planned
        self.request_songs_ready: int = 0  # ready listener-request songs (OVERHAUL 4.7)


class Selector(Protocol):
    def choose_next(self, state: PlayoutState) -> str: ...


class RandomSelector:
    """Weighted random draw over available segment types with §5.2 constraints."""

    def __init__(self, rng: RNG, cfg: Config) -> None:
        self.rng = rng
        self.cfg = cfg

    def choose_next(self, state: PlayoutState) -> str:
        cfg = self.cfg.playout
        weights = dict(cfg.weights)
        available = {s for s, ok in state.available.items() if ok}

        # ---- hard constraints that can force a specific type ----
        non_song_run = self._consecutive_non_song(state.recent_types)
        if non_song_run >= cfg.max_consecutive_non_song and "song" in available:
            return "song"

        # At least one interjection between songs: a song on the tail forbids
        # another song immediately (the interjection may be a liner, dj_talk,
        # commercial_break, or news). Only the no-inventory fallback below can
        # ever emit a second song back-to-back (and only if nothing else exists).
        if state.recent_types and state.recent_types[-1] == "song":
            available.discard("song")
            weights.pop("song", None)

        # Listener-request songs jump the line: when their song is ready and a
        # song is allowed right now (the interjection rule above still holds),
        # commit it (OVERHAUL 4.7). Pure: state + rng only.
        if state.request_songs_ready > 0 and "song" in available:
            return "song"

        if not state.news_valid:
            available.discard("news")
            weights.pop("news", None)

        if state.secs_since.get("news", float("inf")) < cfg.news_min_spacing_s:
            available.discard("news")
            weights.pop("news", None)

        # adjacency: dj_talk never next to dj_talk or news
        last = state.recent_types[-1] if state.recent_types else None
        if last in ("dj_talk", "news"):
            available.discard("dj_talk")
            weights.pop("dj_talk", None)
        if last == "dj_talk":  # ...in either order (§5.2)
            available.discard("news")
            weights.pop("news", None)

        # seriousness adjacency: serious bulletin -> next non-song isn't a commercial
        if state.news_gravity == "serious" and (last is None or last != "song"):
            available.discard("commercial_break")
            weights.pop("commercial_break", None)

        # A liner is a bridge, not filler: never liner -> liner while any other
        # interjection (commercial, dj_talk, news) is ready. Without this a song
        # drought turns into Liam reading station IDs back to back.
        if last == "liner" and available - {"liner", "song"}:
            available.discard("liner")
            weights.pop("liner", None)

        if last == "song" and state.recent_song_genres:
            # genre no-repeat handled separately; nothing forcing here
            pass

        if not available:
            # fallback chain (scheduler handles final emergency); pick anything with stock.
            # Honor the interjection rule here too: if the last committed is a song and any
            # non-song interjection stock exists, take the interjection before the song.
            # "song" is only revisited when no non-song inventory exists at all.
            last = state.recent_types[-1] if state.recent_types else None
            order = ("commercial_break", "dj_talk", "news", "liner", "song") \
                if last == "song" else ("song", "commercial_break", "dj_talk", "news", "liner")
            for s in order:
                if state.available.get(s):
                    return s
            raise ValueError("no inventory at all")

        # renormalize weights over the available set
        wlist = {s: weights.get(s, 0.0) for s in sorted(available)}
        total = sum(wlist.values())
        if total <= 0:
            for s in ("song", "liner", "commercial_break"):
                if s in available:
                    return s
            return self.rng.choice(sorted(available))
        return self.rng.weighted_choice({k: v / total for k, v in wlist.items()})

    @staticmethod
    def _consecutive_non_song(recent_types: list[str]) -> int:
        n = 0
        for t in reversed(recent_types):
            if t == "song":
                break
            n += 1
        return n

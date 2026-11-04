"""Community voting: request-queue votes and five-turkey song ratings
(VOTING.md §2, RADIO §11, §14).

Shared by `pilgrim/server.py` (both apps) and the e2e server, so the window
check, the payload models and the service functions exist exactly once. No
payments import, no playout dependency: the scheduler is only *read* through
`rating_window`, which is pure over its in-memory state.
"""
from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from pilgrim.config import Config
from pilgrim.scheduler import Scheduler
from pilgrim.store import Store

log = logging.getLogger("radio.voting")

# A per-browser random id (VOTING.md V1): 32 lowercase hex chars from
# crypto.getRandomValues. Client-supplied and indicative only; the canonical
# client IP is stored alongside it for the abuse cap (V3).
VOTER_RE = re.compile(r"^[0-9a-f]{32}$")
VOTER_PATTERN = "^[0-9a-f]{32}$"


class VoteIn(BaseModel):
    voter_id: str = Field(..., pattern=VOTER_PATTERN)
    value: Literal[-1, 0, 1]


class RatingIn(BaseModel):
    voter_id: str = Field(..., pattern=VOTER_PATTERN)
    epoch: str = Field(..., min_length=1, max_length=64)
    seq: int = Field(..., ge=0)
    stars: int = Field(..., ge=1, le=5)


# --------------------------------------------------------------------------- #
# Rating window (VOTING.md V8, §3): server-authoritative, pure over scheduler
# state. The browser only hides the control; the server decides.
# --------------------------------------------------------------------------- #
def rating_window(scheduler: Scheduler, epoch: str, seq: int,
                  grace_s: float) -> tuple[bool, int | None]:
    """(open, item_id) for the song at `(epoch, seq)`.

    Open when the epoch matches and `seq` is the on-air row and it is a song,
    or the row immediately before the on-air one is that song and the playhead
    is within `grace_s` of the on-air row's start (the click was in flight when
    the song ended). Anything else — future seq, older seq, intro, non-song —
    is closed.

    A frozen station (IDLE.md) has no listeners by definition, so its rating
    window is closed: a stale tab must not rate an airing that ended hours ago.
    """
    if not epoch or epoch != scheduler.epoch or scheduler.frozen:
        return False, None
    items = scheduler._items
    if not items:
        return False, None
    on_seq, _offset = scheduler.on_air()
    if on_seq is None:
        return False, None
    idx = next((i for i, r in enumerate(items) if r["seq"] == on_seq), None)
    if idx is None:
        return False, None
    row = items[idx]
    if row["type"] == "song" and row["seq"] == seq:
        return True, int(row["item_id"])
    if idx > 0:
        prev = items[idx - 1]
        # The on-air row starts when the previous row ended. The previous row
        # must be the song itself (an intro belongs to the song *after* it).
        if prev["type"] == "song" and prev["seq"] == seq:
            started = scheduler._cum[idx]
            if scheduler.position() - started <= grace_s:
                return True, int(prev["item_id"])
    return False, None


# --------------------------------------------------------------------------- #
# Services: (status_code, body) so server.py and e2e_server.py share them.
# --------------------------------------------------------------------------- #
def cast_vote(store: Store, cfg: Config, request_id: int, voter_id: str,
              value: int, ip: str | None) -> tuple[int, dict]:
    """POST /api/requests/{id}/vote (VOTING.md §2)."""
    res = store.cast_request_vote(
        request_id, voter_id, ip, value,
        max_voters_per_ip=cfg.voting.max_voters_per_ip)
    if res.get("ok"):
        log.info("vote.cast", extra={"request_id": request_id,
                                     "value": value, "score": res["score"]})
        return 200, res
    code = res.get("code")
    if code == "not_found":
        return 404, {"ok": False, "code": code}
    if code == "too_many_voters":
        return 429, {"ok": False, "code": code,
                     "detail": "Too many voters from this address."}
    # not_votable / voting_closed: 409 with the current score so the client can
    # show the truth and disable the pair.
    return 409, {"ok": False, "code": code,
                 "score": store.request_score(request_id)}


def cast_rating(store: Store, cfg: Config, scheduler: Scheduler,
                payload: RatingIn, ip: str | None) -> tuple[int, dict]:
    """POST /api/station/rating (VOTING.md §2, §3).

    The window check and the insert run in the same event-loop turn with no
    await between them, and the scheduler loop runs on the same loop, so the
    window cannot move between check and write ("atomic closing").
    """
    open_, item_id = rating_window(scheduler, payload.epoch, payload.seq,
                                   cfg.ratings.grace_s)
    if not open_ or item_id is None:
        return 409, {"ok": False, "code": "rating_closed"}
    res = store.cast_song_rating(
        item_id=item_id, epoch=payload.epoch, seq=payload.seq,
        voter_id=payload.voter_id, ip=ip, stars=payload.stars,
        max_voters_per_ip=cfg.ratings.max_voters_per_ip)
    if res.get("ok"):
        log.info("rating.cast", extra={"item_id": item_id, "seq": payload.seq,
                                       "stars": payload.stars,
                                       "count": res["count"]})
        return 200, res
    return 429, {"ok": False, "code": res.get("code", "too_many_voters"),
                 "detail": "Too many voters from this address."}

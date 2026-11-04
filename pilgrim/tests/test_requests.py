"""Request-line tests (RADIO.md §7): FIFO top-N eviction + LLM moderation.

Fakes only — no real backends (AGENTS §1.2). The moderation LLM is a tiny
duck-typed fake that returns allowed/reject per the request text.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from pilgrim.pipelines.moderation import Moderation
from pilgrim.store import Store


def _store():
    tmp = tempfile.mkdtemp()
    return Store(Path(tmp) / "station.db")


class _FakeModeratorLLM:
    """Returns a canned moderation verdict keyed on a substring of the prompt."""

    def __init__(self, rules=None):
        self.rules = rules or {}
        self.query = ""
        self.system = ""

    async def chat_json(self, model, system, user, max_tokens=800):
        self.query = user
        self.system = system
        for key, val in self.rules.items():
            if key and key in user:
                return val
        return {"allowed": True, "reason": "ok"}

    async def close(self):
        pass


def test_fifo_eviction_keeps_latest_ten(cfg):
    s = _store()
    for i in range(10):
        s.add_request(f"request {i}", cap=10)
    assert len(s.queued_requests()) == 10
    assert s.queued_requests()[0]["text"] == "request 0"

    # an 11th pushes the oldest out (the ass end falls out)
    s.add_request("request 10", cap=10)
    q = s.queued_requests()
    assert len(q) == 10
    assert q[0]["text"] == "request 1"
    assert q[-1]["text"] == "request 10"

    # the evicted one is recorded, not deleted
    evicted = [r for r in s.all_requests() if r["status"] == "evicted"]
    assert any(r["text"] == "request 0" for r in evicted)


def test_rejected_request_does_not_evict(cfg):
    s = _store()
    for i in range(10):
        s.add_request(f"r{i}", cap=10, status="queued")
    # rejected requests aren't part of the live queue, so they don't evict
    s.add_request("spam", cap=10, status="rejected", reason="no explicit content")
    q = s.queued_requests()
    assert len(q) == 10
    assert q[0]["text"] == "r0"
    assert all(r["status"] == "queued" for r in q)


def test_moderation_allows_clean(cfg):
    llm = _FakeModeratorLLM({"Play us a tune": {"allowed": True, "reason": "ok"}})
    m = Moderation(cfg, llm, prompts={"moderation": "mod template"})
    allowed, reason = asyncio.run(m.moderate("Play us a tune, Liam"))
    assert allowed
    assert reason == "ok"
    assert "mod template" in llm.system   # template is the system prompt only
    assert "mod template" not in llm.query  # request framing is purely the user msg


def test_moderation_rejects_explicit(cfg):
    llm = _FakeModeratorLLM({"underage": {"allowed": False, "reason": "explicit/illegal"}})
    m = Moderation(cfg, llm, prompts={"moderation": "mod template"})
    allowed, reason = asyncio.run(m.moderate("request about underage stuff"))
    assert not allowed
    assert "explicit" in reason


def test_moderation_gate_closed_on_model_error(cfg):
    class _Boom:
        async def chat_json(self, *a, **k):
            raise RuntimeError("model down")

    m = Moderation(cfg, _Boom(), prompts={"moderation": "mod template"})
    allowed, reason = asyncio.run(m.moderate("anything at all"))
    assert not allowed  # never slips through when the gate is down
    # fail-closed message must NOT leak internal error text to the user
    assert "moderation error" not in reason
    assert reason  # a friendly reason is shown


def test_moderation_missing_template_rejects(cfg):
    m = Moderation(cfg, _FakeModeratorLLM(), prompts={})
    allowed, _ = asyncio.run(m.moderate("anything"))
    assert not allowed


# --------------------------------------------------------------------------- #
# OVERHAUL 4.2 — hardened moderation: strict boolean, injection-safe framing,
# retry-once, configurable token cap.
# --------------------------------------------------------------------------- #

class _Recorder:
    """Records (system, user) per call; returns canned JSON from a script."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.max_tokens = []

    async def chat_json(self, model, system, user, max_tokens=800):
        self.calls.append((system, user))
        self.max_tokens.append(max_tokens)
        if not self.answers:
            raise RuntimeError("no more answers")
        nxt = self.answers.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def close(self):
        pass


@pytest.mark.parametrize("raw,expected", [
    ({"allowed": "false", "reason": "x"}, False),   # string -> rejected
    ({"allowed": "true", "reason": "x"}, False),    # string -> rejected
    ({"allowed": "True", "reason": "x"}, False),
    ({"allowed": 1, "reason": "x"}, False),         # truthy int -> rejected
    ({"allowed": True, "reason": "ok"}, True),      # real boolean -> allowed
])
def test_moderation_strict_boolean(cfg, raw, expected):
    llm = _Recorder([raw])
    m = Moderation(cfg, llm, prompts={"moderation": "be strict"})
    allowed, _ = asyncio.run(m.moderate("play the accordion"))
    assert allowed == expected
    # user message carries the JSON-escaped text inside <request>
    sys_msg, user_msg = llm.calls[0]
    assert '"play the accordion"' in user_msg
    assert "<request>" in user_msg
    assert "play the accordion" not in sys_msg  # request never reaches the system prompt


def test_moderation_missing_allowed_rejected(cfg):
    llm = _Recorder([{}])
    m = Moderation(cfg, llm, prompts={"moderation": "be strict"})
    allowed, reason = asyncio.run(m.moderate("anything"))
    assert allowed is False
    assert reason == "blocked by moderator"


def test_moderation_retries_once_then_allows(cfg):
    llm = _Recorder([TypeError("boom"), {"allowed": True, "reason": "ok"}])
    m = Moderation(cfg, llm, prompts={"moderation": "be strict"})
    allowed, _ = asyncio.run(m.moderate("play the accordion"))
    assert allowed is True
    assert len(llm.calls) == 2  # one retry happened
    assert llm.max_tokens == [cfg.requests.moderation_max_tokens] * 2


def test_moderation_fails_closed_after_retry(cfg):
    llm = _Recorder([RuntimeError("down"), RuntimeError("still down")])
    m = Moderation(cfg, llm, prompts={"moderation": "be strict"})
    allowed, reason = asyncio.run(m.moderate("anything"))
    assert allowed is False
    assert len(llm.calls) == 2
    assert "couldn't reach" in reason


# --------------------------------------------------------------------------- #
# OVERHAUL 4.4 — request lifecycle: queued -> producing -> ready -> aired.
# --------------------------------------------------------------------------- #

def test_lifecycle_transitions(cfg):
    s = _store()
    r = s.add_request("play for mittens", cap=10)
    rid = r["id"]
    s.mark_request_producing(rid)
    assert s.get_request(rid)["status"] == "producing"
    s.mark_request_ready(rid, song_item_id=77, intro_item_id=88)
    row = s.get_request(rid)
    assert row["status"] == "ready"
    assert row["song_item_id"] == 77 and row["intro_item_id"] == 88
    assert s.ready_request_songs()[0]["id"] == rid
    s.mark_request_aired(rid)
    assert s.get_request(rid)["status"] == "aired"


def test_failed_attempts_reach_failed_and_clear_head_of_line(cfg):
    s = _store()
    a = s.add_request("first", cap=10)
    b = s.add_request("second", cap=10)
    # first fails 3 times -> failed; the next-to-produce moves to b
    for _ in range(3):
        n = s.request_failed_attempt(a["id"], max_attempts=3)
    assert n == 3
    assert s.get_request(a["id"])["status"] == "failed"
    assert s.next_request_to_produce()["id"] == b["id"]  # no head-of-line block


def test_producing_resets_to_queued_on_start(cfg):
    s = _store()
    r = s.add_request("mid-generation crash", cap=10)
    s.mark_request_producing(r["id"])
    s.reset_producing_to_queued()
    assert s.get_request(r["id"])["status"] == "queued"


def test_eviction_never_touches_producing_or_ready(cfg):
    s = _store()
    # fill 10 queued
    ids = []
    for i in range(10):
        ids.append(s.add_request(f"q{i}", cap=10)["id"])
    # one producing + one ready (out of the evictable set)
    s.mark_request_producing(s.add_request("producing", cap=1)["id"])
    s.mark_request_ready(s.add_request("ready", cap=1)["id"], song_item_id=5)
    # adding one more queued evicts an old QUEUED row, never producing/ready
    s.add_request("newest", cap=10)
    states = {r["status"] for r in s.all_requests()}
    assert "producing" in states and "ready" in states
    assert s.ready_request_songs()  # the ready one is still there
    evicted = [r for r in s.all_requests() if r["status"] == "evicted"]
    assert all(ev["text"].startswith("q") for ev in evicted)


def test_request_board_queue_and_recent(cfg):
    s = _store()
    a = s.add_request("play mittens a song", cap=10)
    s.mark_request_producing(a["id"])
    b = s.add_request("alien abductions", cap=10)
    s.mark_request_ready(b["id"], song_item_id=42)
    # an aired one with a song title joined from items
    c = s.add_request("dedicate to night shift", cap=10)
    song_id = _store_add_song(s)
    s.mark_request_ready(c["id"], song_item_id=song_id)
    s.mark_request_aired(c["id"])
    board = s.request_board(cap=10)
    q = board["queue"]
    # projected rows only (PAYMENT.md §3.4); VOTING.md §1 adds the voting
    # fields: id, score, votable, buried.
    assert [x["text"] for x in q] == ["play mittens a song", "alien abductions"]
    assert q[0]["status"] == "producing" and q[1]["status"] == "ready"
    assert set(q[0]) == {"id", "text", "status", "paid", "stage",
                         "song_title", "score", "votable", "buried"}
    assert q[0]["id"] == a["id"] and q[0]["score"] == 0
    assert q[0]["votable"] is True and q[0]["buried"] is False
    assert q[0]["stage"] == "writing" and q[1]["stage"] == ""
    recent = board["recent"]
    assert any(r["song_title"] == "Night Shift" for r in recent)
    assert set(recent[0]) == {"text", "song_title", "song_artist", "paid", "wait_s"}


def _store_add_song(s):
    """Add an items row and return its id (for request_board join)."""
    with s._lock:
        cur = s._conn.execute(
            "INSERT INTO items (type, media_path, duration_s, title, artist, created_at) "
            "VALUES ('song','/x.flac',60,'Night Shift','The Night Owls', datetime('now'))")
        s._conn.commit()
        return int(cur.lastrowid)


def test_request_board_recent_shows_latest_five(cfg):
    """PAYMENT.md §0.4: 'recently played' shows the latest 5 by id order."""
    s = _store()
    for i in range(7):
        rid = s.add_request(f"played {i}", cap=10)["id"]
        s.mark_request_writing(rid)
        s.mark_request_aired(rid)
    board = s.request_board(cap=10)
    recent = board["recent"]
    assert len(recent) == 5
    assert [r["text"] for r in recent] == [f"played {i}" for i in (6, 5, 4, 3, 2)]
    assert all(isinstance(r["wait_s"], int) for r in recent)
    assert board["queue"] == []


def _set_request_times(s, rid, *, created=None, prep=None, aired=None):
    """Pin ISO timestamps so Prep Time math is deterministic (the store
    stamps real wall-clock time)."""
    with s._lock:
        s._conn.execute(
            "UPDATE requests SET created_at=COALESCE(?, created_at), "
            "prep_started_at=COALESCE(?, prep_started_at), "
            "aired_at=COALESCE(?, aired_at) WHERE id=?",
            (created, prep, aired, rid))
        s._conn.commit()


def test_prep_time_starts_when_lyrics_are_written(cfg):
    """RADIO §11/§14: Prep Time is aired_at - prep_started_at (the writing
    start), so a request that sat in line overnight reads as minutes of prep,
    not as the 11.5 hours between submission and air."""
    s = _store()
    rid = s.add_request("sat in line overnight", cap=10)["id"]
    s.mark_request_ready(rid, song_item_id=1)
    s.mark_request_aired(rid)
    _set_request_times(s, rid, created="2026-11-01T02:04:22+00:00",
                       prep="2026-11-01T13:20:00+00:00",
                       aired="2026-11-01T13:35:07+00:00")
    recent = s.request_board(cap=10)["recent"]
    assert recent[0]["wait_s"] == 907        # 15 min 7 s of prep


def test_prep_time_null_without_a_writing_start(cfg):
    """Rows aired before prep_started_at existed carry no writing start: the
    board sends null (blank cell) rather than queue idle time (PAYMENT §3.4)."""
    s = _store()
    rid = s.add_request("legacy row", cap=10)["id"]
    s.mark_request_aired(rid)
    assert s.request_board(cap=10)["recent"][0]["wait_s"] is None


def test_mark_request_writing_stamps_and_restarts_the_clock(cfg):
    """The prep clock starts at the writing stage and is re-stamped on a
    retry, so an outage between attempts is not counted as prep work."""
    s = _store()
    rid = s.add_request("retry me", cap=10)["id"]
    s.mark_request_producing(rid)
    assert s.get_request(rid)["prep_started_at"] is None
    s.mark_request_writing(rid)
    first = s.get_request(rid)["prep_started_at"]
    assert first
    # a retry re-stamps the clock instead of keeping the first attempt's start
    _set_request_times(s, rid, prep="2020-01-01T00:00:00+00:00")
    s.mark_request_writing(rid)
    assert s.get_request(rid)["prep_started_at"] > "2020-01-01T00:00:00+00:00"


def test_request_board_recent_zero_to_two_renders(cfg):
    s = _store()
    assert s.request_board(cap=10)["recent"] == []
    r = s.add_request("only one", cap=10)
    s.mark_request_aired(r["id"])
    assert len(s.request_board(cap=10)["recent"]) == 1


def test_cap_none_never_evicts(cfg):
    """The public request line passes cap=None: every accepted request stays
    queued until it is made, however long the line gets."""
    s = _store()
    for i in range(25):
        s.add_request(f"request {i}", cap=None)
    assert len(s.queued_requests()) == 25
    assert not [r for r in s.all_requests() if r["status"] == "evicted"]


# ------------------------------------------------- paid priority (PAYMENT §3)
def _make_paid(s, text, paid_at):
    row = s.add_paid_request(text)
    s.attach_checkout(row["id"], f"cs_{row['id']}")
    assert s.mark_request_paid(
        row["id"], session_id=f"cs_{row['id']}", payment_intent_id="pi_x",
        amount_cents=350, currency="usd", livemode=False)
    with s._lock:
        s._conn.execute("UPDATE requests SET paid_at=? WHERE id=?",
                        (paid_at, row["id"]))
        s._conn.commit()
    return row


def test_paid_requests_are_fifo_by_payment_time(cfg):
    """PAYMENT.md §3.2: paid first, FIFO by payment time, free FIFO by creation."""
    s = _store()
    a = s.add_request("free A", cap=None)
    b = s.add_request("free B", cap=None)
    c = _make_paid(s, "paid C", "2026-10-31T10:00:00+00:00")
    d = _make_paid(s, "paid D", "2026-10-31T11:00:00+00:00")
    order = [r["text"] for r in s.queued_requests()]
    assert order == ["paid C", "paid D", "free A", "free B"]
    assert s.next_request_to_produce()["text"] == "paid C"
    assert [r["text"] for r in s.pending_requests()] == order
    assert [r["text"] for r in s.ready_request_songs()] == []
    assert [r["text"] for r in s.request_board(20)["queue"]] == order
    assert max(a["id"], b["id"]) < min(c["id"], d["id"])


def test_paid_created_before_but_paid_after_orders_by_payment(cfg):
    s = _store()
    first_created = _make_paid(s, "created first, paid second", "2026-10-31T11:00:00+00:00")
    second_created = _make_paid(s, "created second, paid first", "2026-10-31T10:00:00+00:00")
    assert first_created["id"] < second_created["id"]
    assert [r["text"] for r in s.queued_requests()] == [
        "created second, paid first", "created first, paid second"]


def test_awaiting_and_abandoned_are_in_no_queue(cfg):
    s = _store()
    awaiting = s.add_paid_request("never paid")
    abandoned = s.add_paid_request("expired checkout")
    s.attach_checkout(abandoned["id"], "cs_abandoned")
    s.mark_request_abandoned(abandoned["id"])
    s.add_request("free one", cap=None)
    assert [r["text"] for r in s.queued_requests()] == ["free one"]
    assert [r["text"] for r in s.pending_requests()] == ["free one"]
    assert [r["text"] for r in s.request_board(20)["queue"]] == ["free one"]
    assert s.next_request_to_produce()["text"] == "free one"
    assert s.get_request(awaiting["id"])["status"] == "awaiting_payment"
    assert s.get_request(abandoned["id"])["status"] == "abandoned"


def test_mark_request_paid_is_idempotent_and_only_from_awaiting(cfg):
    s = _store()
    row = s.add_paid_request("paid once")
    s.attach_checkout(row["id"], "cs_1")
    assert s.mark_request_paid(row["id"], session_id="cs_1", payment_intent_id="pi",
                               amount_cents=350, currency="usd", livemode=False)
    assert not s.mark_request_paid(row["id"], session_id="cs_1", payment_intent_id="pi",
                                   amount_cents=350, currency="usd", livemode=False)
    # abandoned -> paid wins
    other = s.add_paid_request("abandoned then paid")
    s.attach_checkout(other["id"], "cs_2")
    s.mark_request_abandoned(other["id"])
    assert s.mark_request_paid(other["id"], session_id="cs_2", payment_intent_id="pi",
                               amount_cents=350, currency="usd", livemode=False)
    # queued/aired/rejected -> never
    free = s.add_request("free", cap=None)
    for status in ("queued", "aired", "rejected"):
        with s._lock:
            s._conn.execute("UPDATE requests SET status=? WHERE id=?", (status, free["id"]))
            s._conn.commit()
        assert not s.mark_request_paid(free["id"], session_id="cs_x",
                                       payment_intent_id=None, amount_cents=350,
                                       currency="usd", livemode=False)


def test_record_stripe_event_dedupes(cfg):
    s = _store()
    assert s.record_stripe_event("evt_1", "checkout.session.completed", "cs_1", "applied")
    assert not s.record_stripe_event("evt_1", "checkout.session.completed", "cs_1", "applied")
    s.update_stripe_event_outcome("evt_1", "mismatch")
    assert s.mismatch_events()[0]["event_id"] == "evt_1"


def test_board_paid_text_is_first_line_and_cut(cfg):
    s = _store()
    long_first = "x" * 200
    row = _make_paid(s, long_first + "\nsecond line", "2026-10-31T10:00:00+00:00")
    item = s.request_board(20, max_length=160)["queue"][0]
    assert item["paid"] is True
    assert "\n" not in item["text"]
    assert item["text"].endswith("…")
    assert len(item["text"]) == 160
    assert row["id"]


def test_board_prefers_paid_summary(cfg):
    s = _store()
    row = s.add_paid_request("line one\nline two", summary="a tidy label")
    s.attach_checkout(row["id"], f"cs_{row['id']}")
    assert s.mark_request_paid(row["id"], session_id=f"cs_{row['id']}",
                               payment_intent_id="pi", amount_cents=350,
                               currency="usd", livemode=False)
    assert s.request_board(20)["queue"][0]["text"] == "a tidy label"


# --------------------------------------------------------------------------- #
# VOTING.md §1: request votes, ratings, the new queue order, bury, migration.
# --------------------------------------------------------------------------- #

def _voter(i: int) -> str:
    return f"{i:032x}"


def test_vote_upsert_retract_and_score(cfg):
    s = _store()
    a = s.add_request("free A", cap=None)
    assert s.cast_request_vote(a["id"], _voter(1), "1.2.3.4", 1,
                               max_voters_per_ip=3)["score"] == 1
    assert s.cast_request_vote(a["id"], _voter(1), "1.2.3.4", -1,
                               max_voters_per_ip=3)["score"] == -1
    with s._lock:
        n = s._conn.execute("SELECT COUNT(*) FROM request_votes").fetchone()[0]
    assert n == 1                       # upsert, not a second row
    assert s.cast_request_vote(a["id"], _voter(1), "1.2.3.4", 0,
                               max_voters_per_ip=3)["score"] == 0
    assert s.request_score(a["id"]) == 0
    assert s.cast_request_vote(a["id"], _voter(2), "5.6.7.8", -1,
                               max_voters_per_ip=3)["score"] == -1


def test_vote_window_and_paid(cfg):
    s = _store()
    a = s.add_request("free A", cap=None)
    paid = _make_paid(s, "paid", "2026-10-31T10:00:00+00:00")
    assert s.cast_request_vote(paid["id"], _voter(1), None, 1,
                               max_voters_per_ip=3)["code"] == "not_votable"
    s.mark_request_aired(a["id"])
    assert s.cast_request_vote(a["id"], _voter(1), None, 1,
                               max_voters_per_ip=3)["code"] == "voting_closed"
    assert s.cast_request_vote(999, _voter(1), None, 1,
                               max_voters_per_ip=3)["code"] == "not_found"


def test_vote_ip_cap_is_per_request_and_excludes_own_vote(cfg):
    s = _store()
    a = s.add_request("free A", cap=None)
    b = s.add_request("free B", cap=None)
    for i in (1, 2, 3):
        assert s.cast_request_vote(a["id"], _voter(i), "9.9.9.9", 1,
                                   max_voters_per_ip=3)["ok"]
    assert s.cast_request_vote(a["id"], _voter(4), "9.9.9.9", 1,
                               max_voters_per_ip=3)["code"] == "too_many_voters"
    # the same voter changing its vote is not a new voter
    assert s.cast_request_vote(a["id"], _voter(1), "9.9.9.9", -1,
                               max_voters_per_ip=3)["ok"]
    # the cap is per request, not per IP
    assert s.cast_request_vote(b["id"], _voter(4), "9.9.9.9", 1,
                               max_voters_per_ip=3)["ok"]


def test_queue_order_votes_then_in_flight_then_fifo(cfg):
    """VOTING.md V6: paid first; in-flight by claim time; free queued by score
    DESC, then FIFO. With no votes the order is today's."""
    s = _store()
    a = s.add_request("free A", cap=None)
    b = s.add_request("free B", cap=None)
    c = _make_paid(s, "paid C", "2026-10-31T10:00:00+00:00")
    assert [r["text"] for r in s.queued_requests()] == ["paid C", "free A", "free B"]
    # B upvoted twice -> written next (after paid)
    s.cast_request_vote(b["id"], _voter(1), "1.1.1.1", 1, max_voters_per_ip=3)
    s.cast_request_vote(b["id"], _voter(2), "2.2.2.2", 1, max_voters_per_ip=3)
    assert [r["text"] for r in s.queued_requests()] == ["paid C", "free B", "free A"]
    assert s.next_request_to_produce()["text"] == "paid C"
    # B claimed -> in flight first; A voted +5 still waits behind it
    s.mark_request_producing(b["id"])
    for i in range(3, 8):
        s.cast_request_vote(a["id"], _voter(i), f"{i}.{i}.{i}.{i}", 1,
                            max_voters_per_ip=3)
    assert [r["text"] for r in s.queued_requests()] == ["paid C", "free A"]
    assert [r["text"] for r in s.pending_requests()] == ["paid C", "free B", "free A"]
    # C is still queued (paid), so it is the next to produce; once claimed, A
    # (the highest-scoring free row) is next even though B is ahead on the board.
    assert s.next_request_to_produce()["text"] == "paid C"
    s.mark_request_producing(c["id"])
    assert s.next_request_to_produce()["text"] == "free A"


def test_claimed_at_is_set_once_and_orders_in_flight(cfg):
    s = _store()
    a = s.add_request("free A", cap=None)
    b = s.add_request("free B", cap=None)
    ca = s.mark_request_producing(a["id"])
    assert ca
    cb = s.mark_request_producing(b["id"])
    assert cb and cb >= ca
    # a retry (requeue -> producing) keeps the original claim time
    s.requeue_request(a["id"])
    again = s.mark_request_producing(a["id"])
    assert again == ca


def test_bury_skips_free_queued_but_keeps_it_on_the_board(cfg):
    s = _store()
    a = s.add_request("buried noise", cap=None)
    b = s.add_request("fine", cap=None)
    for i in (1, 2, 3):
        s.cast_request_vote(a["id"], _voter(i), f"{i}.{i}.{i}.{i}", -1,
                            max_voters_per_ip=3)
    assert s.request_score(a["id"]) == -3
    assert [r["text"] for r in s.queued_requests(bury_threshold=-3)] == ["fine"]
    assert s.next_request_to_produce(bury_threshold=-3)["id"] == b["id"]
    assert [r["text"] for r in s.pending_requests(bury_threshold=-3)] == ["fine"]
    board = s.request_board(20, bury_threshold=-3)
    buried = next(r for r in board["queue"] if r["id"] == a["id"])
    assert buried["buried"] is True and buried["score"] == -3
    assert buried["votable"] is True
    # a single upvote revives it
    s.cast_request_vote(a["id"], _voter(1), "1.1.1.1", 1, max_voters_per_ip=3)
    assert s.request_board(20, bury_threshold=-3)["queue"][0]["buried"] is False
    assert s.next_request_to_produce(bury_threshold=-3) is not None
    # None disables burying entirely
    assert len(s.queued_requests(bury_threshold=None)) == 2


def test_board_votable_false_when_voting_disabled(cfg):
    s = _store()
    s.add_request("free A", cap=None)
    row = s.request_board(20, voting_enabled=False)["queue"][0]
    assert row["votable"] is False


def test_ratings_upsert_per_airing_and_lifetime_aggregate(cfg):
    s = _store()
    item = s.add_item(type_="song", media_path="/tmp/s.flac", duration_s=120)
    r1 = s.cast_song_rating(item_id=item, epoch="e1", seq=5,
                            voter_id=_voter(1), ip="1.1.1.1", stars=4,
                            max_voters_per_ip=3)
    assert r1 == {"ok": True, "item_id": item, "count": 1, "avg": 4.0, "mine": 4}
    # same voter, same airing: replaces
    r2 = s.cast_song_rating(item_id=item, epoch="e1", seq=5,
                            voter_id=_voter(1), ip="1.1.1.1", stars=2,
                            max_voters_per_ip=3)
    assert r2["count"] == 1 and r2["avg"] == 2.0
    # same song, next airing: a second rating counts
    s.cast_song_rating(item_id=item, epoch="e1", seq=40,
                       voter_id=_voter(1), ip="1.1.1.1", stars=5,
                       max_voters_per_ip=3)
    agg = s.song_rating(item)
    assert agg["count"] == 2 and agg["avg"] == 3.5


def test_rating_ip_cap_and_stars_check(cfg):
    import sqlite3
    s = _store()
    item = s.add_item(type_="song", media_path="/tmp/s.flac", duration_s=120)
    for i in (1, 2, 3):
        assert s.cast_song_rating(item_id=item, epoch="e", seq=1,
                                  voter_id=_voter(i), ip="9.9.9.9", stars=3,
                                  max_voters_per_ip=3)["ok"]
    assert s.cast_song_rating(item_id=item, epoch="e", seq=1,
                              voter_id=_voter(4), ip="9.9.9.9", stars=3,
                              max_voters_per_ip=3)["code"] == "too_many_voters"
    import pytest
    with pytest.raises(sqlite3.IntegrityError), s._lock:
        s._conn.execute(
            "INSERT INTO song_ratings (item_id, epoch, seq, voter_id, ip, "
            "stars, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (item, "e", 1, _voter(9), None, 6, "x", "x"))


def test_vote_migration_is_idempotent_on_old_schema(cfg, tmp_path):
    """An old station.db gains claimed_at and both tables on open; reopening
    is a no-op (VOTING.md §1)."""
    import sqlite3

    from pilgrim.store import Store
    db = tmp_path / "old.db"
    s0 = Store(db)          # current schema
    s0._conn.close()
    con = sqlite3.connect(db)   # strip the VOTING.md additions
    con.executescript(
        "DROP TABLE request_votes; DROP TABLE song_ratings;"
        "ALTER TABLE requests DROP COLUMN claimed_at;")
    con.commit()
    con.close()
    s1 = Store(db)
    cols = {r[1] for r in s1._conn.execute("PRAGMA table_info(requests)")}
    assert "claimed_at" in cols
    tables = {r[0] for r in s1._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"request_votes", "song_ratings"} <= tables
    s1._conn.close()
    s2 = Store(db)  # reopening must not fail
    assert "claimed_at" in {r[1] for r in s2._conn.execute(
        "PRAGMA table_info(requests)")}

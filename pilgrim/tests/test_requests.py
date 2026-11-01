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
    # projected rows only: {text, status, paid} (PAYMENT.md §3.4)
    assert [x["text"] for x in q] == ["play mittens a song", "alien abductions"]
    assert q[0]["status"] == "producing" and q[1]["status"] == "ready"
    assert set(q[0]) == {"text", "status", "paid", "stage", "song_title"}
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

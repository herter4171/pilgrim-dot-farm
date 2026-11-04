"""due_backfill tool (RADIO §11): recover requests.due_at from the station
log so pre-column rows get the same Prep Time cap. Pure log parsing + a temp
DB; no backend, no real station.db (hard rule 5)."""
from __future__ import annotations

from datetime import datetime

from pilgrim.store import Store
from pilgrim.tools import due_backfill as bf

LOG = """\
2026-11-04T09:35:29.706Z INFO radio.scheduler request.spliced request_id=540 seq=18898
2026-11-04T09:35:56.133Z INFO radio.scheduler request.aired item_id=8921 request_id=540 seq=18898
2026-11-04T09:46:48.959Z INFO radio.scheduler program.frozen coverage_s=301.1 position_s=7460.7
2026-11-04T09:46:48.959Z INFO radio.server station.idle after_s=120.0 coverage_s=301.1
2026-11-04T09:56:40.529Z INFO radio.scheduler request.spliced request_id=541 seq=18911
2026-11-04T14:49:54.783Z INFO radio.server station.wake dropped=0 idle_s=18186 reason=program
2026-11-04T14:52:13.083Z INFO radio.scheduler request.aired item_id=8925 request_id=541 seq=18911
2026-11-04T14:56:45.222Z INFO radio.scheduler program.frozen coverage_s=412.6 position_s=7871.1
2026-11-04T16:00:00.000Z INFO radio.server station.startup public=127.0.0.1:5080
2026-11-04T16:05:00.000Z INFO radio.scheduler request.committed request_id=542
2026-11-04T16:07:00.000Z INFO radio.scheduler request.aired request_id=542
garbage line that is not a log record
"""


def _t(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def test_parse_pairs_freezes_with_wakes_and_restarts():
    tees, spans = bf.parse_log_lines(LOG.splitlines())
    assert tees[541] == [_t("2026-11-04T09:56:40.529+00:00")]
    assert tees[542] == [_t("2026-11-04T16:05:00+00:00")]
    assert spans == [
        (_t("2026-11-04T09:46:48.959+00:00"), _t("2026-11-04T14:49:54.783+00:00")),
        # a restart thaws without a wake line: station.startup ends the span
        (_t("2026-11-04T14:56:45.222+00:00"), _t("2026-11-04T16:00:00+00:00")),
    ]


def test_frozen_between_clips_to_the_window():
    spans = [(100.0, 200.0), (300.0, 400.0)]
    assert bf.frozen_between(spans, 0.0, 500.0) == 200.0
    assert bf.frozen_between(spans, 150.0, 350.0) == 100.0   # 50 + 50
    assert bf.frozen_between(spans, 210.0, 290.0) == 0.0


def _db(tmp_path):
    s = Store(tmp_path / "station.db")
    rows = {}
    for rid, prep, aired in (
            (540, "2026-11-04T09:22:49+00:00", "2026-11-04T09:35:56+00:00"),
            (541, "2026-11-04T09:42:49+00:00", "2026-11-04T14:52:13+00:00"),
            (542, "2026-11-04T16:03:00+00:00", "2026-11-04T16:07:00+00:00"),
            (543, "2026-11-04T17:00:00+00:00", "2026-11-04T17:20:00+00:00")):
        r = s.add_request(f"req {rid}", cap=None)
        s.mark_request_aired(r["id"])
        with s._lock:
            s._conn.execute("UPDATE requests SET id=?, prep_started_at=?, aired_at=? WHERE id=?",
                            (rid, prep, aired, r["id"]))
            s._conn.commit()
        rows[rid] = r
    s.close() if hasattr(s, "close") else None
    return tmp_path / "station.db"


def test_plan_caps_only_rows_that_spanned_a_freeze(tmp_path):
    db = _db(tmp_path)
    plan = {r["id"]: r for r in bf.plan(db, LOG.splitlines())}
    # 541: teed up while frozen at 09:56:40, woke 14:49:54, aired 14:52:13 ->
    # due = aired - frozen(09:56:40..14:49:54) = 09:58:58.3
    assert plan[541]["due_at"].startswith("2026-11-04T09:58:58")
    assert plan[541]["wait_before_s"] == 18564
    assert 965 <= plan[541]["wait_after_s"] <= 975
    # 540 and 542: no freeze between tee-up and air -> due = aired
    assert plan[540]["due_at"] == "2026-11-04T09:35:56+00:00"
    assert plan[540]["capped_s"] == 0
    assert plan[542]["due_at"] == "2026-11-04T16:07:00+00:00"
    # 543: aired after log retention -> left alone
    assert plan[543]["due_at"] is None and plan[543]["note"] == "no log"


def test_apply_writes_only_recoverable_rows_and_is_idempotent(tmp_path):
    db = _db(tmp_path)
    rows = bf.plan(db, LOG.splitlines())
    assert bf.apply(db, rows) == 3
    s = Store(db)
    assert s.get_request(543)["due_at"] is None
    assert s.get_request(541)["due_at"].startswith("2026-11-04T09:58:58")
    board = {r["text"]: r["wait_s"] for r in s.request_board(cap=10)["recent"]}
    assert board["req 541"] < 1000 and board["req 543"] == 1200
    # second pass: nothing left to plan for the written rows
    again = {r["id"] for r in bf.plan(db, LOG.splitlines())}
    assert again == {543}


def test_backup_then_apply_leaves_a_copy(tmp_path):
    db = _db(tmp_path)
    bak = bf.backup_db(db)
    assert bak.exists() and bak.name.startswith("station.db.due-backfill-")

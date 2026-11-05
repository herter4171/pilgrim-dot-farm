"""Persistent unique-visitor counter, store layer (COSMETIC_PATCHING §6, RADIO §14).

Verifies raw-IP storage, the idempotent insert-on-conflict dedupe, the unique count, retention
across a restart against the same database, and that no existing station tables
are disturbed.
"""
from __future__ import annotations

from pilgrim.store import Store


def test_register_visitor_idempotent_and_unique(tmp_path):
    s = Store(tmp_path / "station.db")
    assert s.unique_visitors() == 0
    assert s.register_visitor("1.2.3.4") == 1
    assert s.register_visitor("1.2.3.4") == 1     # same IP doesn't double-count
    assert s.register_visitor("5.6.7.8") == 2
    assert s.unique_visitors() == 2


def test_restart_retains_count_and_touches_no_station_rows(tmp_path):
    db = tmp_path / "station.db"
    s = Store(db)
    s.add_item(type_="liner", media_path="l.flac", duration_s=5)
    s.add_request("play for mittens", cap=10)
    s.register_visitor("1.2.3.4")
    s.register_visitor("5.6.7.8")
    s.register_visitor("1.2.3.4")   # duplicate, ignored

    s2 = Store(db)                 # a fresh connection on the same file
    assert s2.unique_visitors() == 2
    # existing station tables are untouched (schema + data preserved)
    assert len(s2.list_items()) == 1
    assert len(s2.queued_requests()) == 1


def test_visitors_table_schema_is_idempotent(tmp_path):
    s = Store(tmp_path / "station.db")
    s._init_schema()               # running migration twice is a no-op
    s2 = Store(tmp_path / "station.db")
    assert s2.register_visitor("9.9.9.9") == 1
    assert s.register_visitor("9.9.9.9") == 1


def test_raw_ip_is_stored_and_legacy_signature_table_dropped(tmp_path):
    s = Store(tmp_path / "station.db")
    s.register_visitor("1.2.3.4")
    assert s._conn.execute("SELECT ip FROM visitor_ips").fetchall()[0][0] == "1.2.3.4"
    tables = {r[0] for r in s._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "visitor_signatures" not in tables


def test_request_counts_paid_and_total(tmp_path):
    """HIT COUNTER counts (RADIO §9.1): `paid` = paid priority rows only;
    `total` = every request row, free or paid."""
    s = Store(tmp_path / "station.db")
    assert s.request_counts() == (0, 0)

    s.add_request("free one", cap=None)
    s.add_request("free two", cap=None)
    assert s.request_counts() == (0, 2)

    # paid row: awaiting_payment/checking/abandoned never count as paid
    paid = s.add_paid_request("paid one")
    s.attach_checkout(paid["id"], "cs_test_1")
    assert s.request_counts() == (0, 3)
    assert s.mark_request_paid(
        paid["id"], session_id="cs_test_1", payment_intent_id="pi_test",
        amount_cents=350, currency="usd", livemode=False)
    assert s.request_counts() == (1, 3)

    # an operator comp (paid_at set, amount 0) also counts as a paid request
    comp = s.add_checking_request("comped one", comped=True)
    s.set_request_stage(comp["id"], "ready")
    assert s.mark_request_comped(comp["id"])
    assert s.request_counts() == (2, 4)

    # a paid row abandoned at checkout stays out of the paid count
    abandoned = s.add_paid_request("never paid")
    s.attach_checkout(abandoned["id"], "cs_test_2")
    assert s.mark_request_abandoned(abandoned["id"])
    assert s.request_counts() == (2, 5)

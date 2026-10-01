"""Persistent unique-visitor counter, store layer (COSMETIC_PATCHING §6, RADIO §14).

Verifies the idempotent insert-on-conflict dedupe, the unique count, retention
across a restart against the same database, and that no existing station tables
are disturbed.
"""
from __future__ import annotations

from pilgrim.store import Store


def test_register_visitor_idempotent_and_unique(tmp_path):
    s = Store(tmp_path / "station.db")
    assert s.unique_visitors() == 0
    assert s.register_visitor("sig-a") == 1
    assert s.register_visitor("sig-a") == 1     # same signature doesn't double-count
    assert s.register_visitor("sig-b") == 2
    assert s.unique_visitors() == 2


def test_restart_retains_count_and_touches_no_station_rows(tmp_path):
    db = tmp_path / "station.db"
    s = Store(db)
    s.add_item(type_="liner", media_path="l.flac", duration_s=5)
    s.add_request("play for mittens", cap=10)
    s.register_visitor("sig-a")
    s.register_visitor("sig-b")
    s.register_visitor("sig-a")   # duplicate, ignored

    s2 = Store(db)                 # a fresh connection on the same file
    assert s2.unique_visitors() == 2
    # existing station tables are untouched (schema + data preserved)
    assert len(s2.list_items()) == 1
    assert len(s2.queued_requests()) == 1


def test_visitors_table_schema_is_idempotent(tmp_path):
    s = Store(tmp_path / "station.db")
    s._init_schema()               # running migration twice is a no-op
    s2 = Store(tmp_path / "station.db")
    assert s2.register_visitor("x") == 1
    assert s.register_visitor("x") == 1

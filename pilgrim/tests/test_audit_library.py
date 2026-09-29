"""OVERHAUL 2.2 — retired flag on items + library audit tool."""
from __future__ import annotations

from conftest import make_item
from pilgrim.tools.audit_library import classify, main


def test_retired_excluded_from_reads_still_gettable(tmp_env):
    cfg, store, _ = tmp_env
    id_a = make_item(cfg, store, "commercial", 20.0)
    store.retire_item(id_a, "truncated")
    assert store.get_item(id_a) is not None
    assert store.get_item(id_a)["retired"] == 1
    assert all(i["id"] != id_a for i in store.list_items("commercial"))
    assert store.count_fresh_of_type("commercial") == 0
    assert store.count_usable_of_type("commercial") == 0


def test_init_schema_twice_harmless(tmp_env):
    _, store, _ = tmp_env
    store._init_schema()  # second run must not error on the migration


def test_classify_flags_fast_commercial_not_slow_one(cfg, tmp_env):
    _, store, _ = tmp_env
    fast = store.add_item(type_="commercial", media_path="/a.flac", duration_s=3.5,
                          meta={"text": " ".join(["word"] * 80)})
    slow = store.add_item(type_="commercial", media_path="/b.flac", duration_s=25.0,
                          meta={"text": " ".join(["word"] * 60)})
    to_retire, abrupt = classify(store.list_items(), cfg)
    ids = [i for i, _, _ in to_retire]
    assert fast in ids
    assert slow not in ids
    assert abrupt == []


def test_audit_main_dry_run_changes_nothing_apply_retires(cfg, tmp_env, capsys):
    _, store, _ = tmp_env
    store.add_item(type_="commercial", media_path="/a.flac", duration_s=3.5,
                   meta={"text": " ".join(["word"] * 80)})
    good = store.add_item(type_="commercial", media_path="/b.flac", duration_s=25.0,
                          meta={"text": " ".join(["word"] * 60)})
    import pilgrim.tools.audit_library as mod
    mod.load_config = lambda *a, **k: cfg
    mod.Store = lambda path: store

    main(["--apply"])
    out = capsys.readouterr().out
    assert "Retired 1" in out
    assert store.count_usable_of_type("commercial") == 1  # only the flagged one
    assert store.get_item(good)["retired"] == 0

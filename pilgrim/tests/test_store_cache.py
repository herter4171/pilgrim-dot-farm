"""Store.get_item read-through cache (SCALABILITY.md Step 2 follow-up): every
write to `items` invalidates it, so no reader ever sees a stale flag."""
from __future__ import annotations


def test_get_item_cache_hits_and_invalidates(tmp_env):
    _, s, _ = tmp_env
    iid = s.add_item(type_="song", media_path="/a.mp3", duration_s=10.0, title="A")
    first = s.get_item(iid)
    assert first["title"] == "A" and iid in s._item_cache
    first["title"] = "mutated copy"                 # callers get copies
    assert s.get_item(iid)["title"] == "A"
    s.set_published(iid, "audio/a.mp3")
    assert iid not in s._item_cache                 # write cleared it
    assert s.get_item(iid)["s3_key"] == "audio/a.mp3"
    s.mark_aired(iid)
    assert s.get_item(iid)["fresh"] == 0
    s.commit_song_air(iid, 1_800_000_000.0)
    assert s.get_item(iid)["play_count"] == 1
    s.retire_item(iid, "test")
    assert s.get_item(iid)["retired"] == 1
    assert s.get_item(999_999) is None and 999_999 not in s._item_cache


def test_cache_is_bounded(tmp_env, monkeypatch):
    import pilgrim.store as st
    _, s, _ = tmp_env
    monkeypatch.setattr(st, "ITEM_CACHE_MAX", 3)
    ids = [s.add_item(type_="liner", media_path=f"/{i}.mp3", duration_s=1.0) for i in range(5)]
    for i in ids:
        s.get_item(i)
    assert len(s._item_cache) <= 3


def test_cache_entries_expire_so_other_processes_writes_show_up(tmp_env, monkeypatch):
    """tools/art_backfill writes items from another process: no in-process
    invalidation, so the TTL is what makes the station see it."""
    import pilgrim.store as st
    from pilgrim.db import open_store
    cfg, s, tmp = tmp_env
    iid = s.add_item(type_="song", media_path="/t.mp3", duration_s=10.0, title="T")
    assert s.get_item(iid)["art_s3_key"] is None
    # a second store (another process, same database) publishes the art
    other = open_store(cfg, path=tmp / "station.db")
    other.set_art_published(iid, "art/t.jpg")
    assert s.get_item(iid)["art_s3_key"] is None            # cached, within TTL
    t0 = st._cache_clock()
    monkeypatch.setattr(st, "_cache_clock", lambda: t0 + st.ITEM_CACHE_TTL_S + 1)
    assert s.get_item(iid)["art_s3_key"] == "art/t.jpg"     # expired -> re-read
    other.close()

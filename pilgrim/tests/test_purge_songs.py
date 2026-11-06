"""purge_songs — only songs no listener asked for are deleted from DB and disk."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from pilgrim.tools.purge_songs import (
    apply_purge,
    classify,
    intro_ids_for,
    plan,
    protected_song_ids,
)


def _row(id_: int, **kw) -> dict:
    return {"id": id_, "meta_json": kw.get("meta_json"), "emergency": kw.get("emergency", 0)}


def test_classify_keeps_everything_anyone_asked_for() -> None:
    songs = [
        _row(1),                                                    # stock -> purge
        _row(2, meta_json='{"request_id": 7}'),                     # request-tagged
        _row(3, meta_json='{"source": "upload"}'),                  # operator import
        _row(4, emergency=1),                                       # emergency pack
        _row(5, meta_json='not json'),                              # stock, bad meta
        _row(6, meta_json='{"request_id": null}'),                  # stock, null tag
    ]
    purge, deferred = classify(songs, protected={6}, live={5})
    assert purge == [1]
    assert deferred == [5]          # in the live window: not yet
    assert classify(songs, set(), set())[0] == [1, 5, 6]


def test_plan_and_apply_delete_stock_songs_only(sqlite_env) -> None:
    cfg, store, tmp = sqlite_env
    lib = Path(cfg.library.dir)

    def song(name: str, **meta) -> tuple[int, Path]:
        f = lib / f"{name}.flac"
        f.write_bytes(b"flac" * 10)
        return store.add_item(type_="song", media_path=str(f), duration_s=120.0,
                              title=name, meta=meta or None), f

    stock_id, stock_file = song("stock")
    req_id, req_file = song("requested")
    imp_id, imp_file = song("uploaded", source="upload")
    req = store.add_request("a song please", cap=None)
    store.mark_request_ready(req["id"], req_id, None)
    rev_before = store.catalogue_revision()

    (lib / "intro.flac").write_bytes(b"flac" * 10)
    intro = store.add_item(type_="intro", media_path=str(lib / "intro.flac"),
                           duration_s=6.0, meta={"song_item_id": stock_id})
    store.append_program(req_id, "song", 120.0)      # the requested song is on air
    assert protected_song_ids(sqlite3.connect(store.path)) == {req_id}

    p = plan(Path(cfg.library.db))
    assert p["purge"] == [stock_id]
    assert p["deferred"] == []          # the only live item is a kept song
    assert p["kept"] == 2
    assert p["intros"] == [intro]

    removed = apply_purge(Path(cfg.library.db), lib, p["purge"] + p["intros"])
    assert removed == {"items": 2, "files": 2, "skipped": []}
    assert not stock_file.exists()
    assert req_file.exists() and imp_file.exists()

    left = {r["id"] for r in store.list_items("song")}
    assert left == {req_id, imp_id}
    assert store.catalogue_revision() == rev_before + 1   # console refetches (TUI §4)
    assert plan(Path(cfg.library.db))["purge"] == []


def test_apply_purge_never_touches_files_outside_the_library(sqlite_env) -> None:
    cfg, store, tmp = sqlite_env
    lib = Path(cfg.library.dir)
    outside = tmp / "elsewhere.flac"
    outside.write_bytes(b"keep me")
    item = store.add_item(type_="song", media_path=str(outside), duration_s=60.0)

    removed = apply_purge(Path(cfg.library.db), lib, [item])
    assert removed["items"] == 1 and removed["files"] == 0
    assert removed["skipped"] == [str(outside)]
    assert outside.exists() and store.get_item(item) is None


def test_intro_ids_matches_only_the_purged_song(sqlite_env) -> None:
    cfg, store, tmp = sqlite_env
    a = store.add_item(type_="song", media_path="/a.flac", duration_s=60.0)
    b = store.add_item(type_="song", media_path="/b.flac", duration_s=60.0)
    ia = store.add_item(type_="intro", media_path="/ia.flac", duration_s=5.0,
                        meta={"song_item_id": a})
    store.add_item(type_="intro", media_path="/ib.flac", duration_s=5.0,
                   meta={"song_item_id": b})
    store.add_item(type_="intro", media_path="/plain.flac", duration_s=5.0)
    con = sqlite3.connect(store.path)
    assert intro_ids_for(con, [a]) == [ia]
    assert intro_ids_for(con, []) == []

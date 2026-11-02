"""Cover-art storage (ALBUM_ART.md Step 4): files, migration and queries."""
from __future__ import annotations

import subprocess

import pytest
from pilgrim.album_art import (
    art_dir,
    art_seed,
    art_stem,
    encode_jpeg,
    save_art,
    scene_texts,
    unique_path,
)
from pilgrim.store import Store
from pilgrim.tests.fakes.fake_flux import encode_png


def _song(store, tmp, *, title="T", artist="A", created_at=None, media="s1.mp3",
          emergency=False, retired=False):
    iid = store.add_item(type_="song", media_path=str(tmp / media), duration_s=1,
                         title=title, artist=artist, created_at=created_at)
    if retired:
        store.retire_item(iid, "test")
    return iid


# ------------------------------------------------------------------- schema
def test_migration_is_additive_and_idempotent(tmp_path):
    db = tmp_path / "station.db"
    s1 = Store(db)
    cols = {r[1] for r in s1._conn.execute("PRAGMA table_info(items)")}
    assert {"art_path", "art_s3_key", "art_uploaded_at", "art_attempted_at"} <= cols
    s1._conn.close()
    # re-opening the same db re-runs the migration without error
    s2 = Store(db)
    cols2 = {r[1] for r in s2._conn.execute("PRAGMA table_info(items)")}
    assert cols2 == cols
    s2._conn.close()


# -------------------------------------------------------------------- files
def test_save_art_writes_png_and_jpeg_without_overwriting(tmp_env):
    cfg, store, tmp = tmp_env
    png = encode_png(64, 64)
    png_path, jpg_path = save_art(cfg, png, "song_1")
    assert png_path.read_bytes() == png
    assert jpg_path.read_bytes().startswith(b"\xff\xd8")     # JPEG SOI
    assert png_path.parent == art_dir(cfg) == tmp / "library" / "art"

    # a second render of the same stem must not touch the first pair
    png2, jpg2 = save_art(cfg, encode_png(64, 64, color=(1, 2, 3)), "song_1")
    assert png2.name == "song_1-2.png" and jpg2.name == "song_1-2.jpg"
    assert png_path.read_bytes() == png


def test_unique_path_suffixes(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "a-2.jpg").write_bytes(b"x")
    assert unique_path(tmp_path, "a", ".jpg").name == "a-3.jpg"
    assert unique_path(tmp_path, "b", ".jpg").name == "b.jpg"


def test_encode_jpeg_rejects_a_non_image(tmp_env):
    from pilgrim.pipelines.art import ArtError
    cfg, _, tmp = tmp_env
    bad = tmp / "bad.png"
    bad.write_bytes(b"not a png")
    with pytest.raises(ArtError):
        encode_jpeg(bad, tmp / "bad.jpg", 3)


def test_jpeg_is_square_and_readable(tmp_env):
    cfg, _, tmp = tmp_env
    _, jpg = save_art(cfg, encode_png(128, 128), "sq")
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height,codec_name",
                          "-of", "csv=p=0", str(jpg)],
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "mjpeg,128,128"


def test_stem_seed_and_texts():
    item = {"media_path": "/x/library/song_1790809859341_ab12cd.mp3",
            "title": "Biscuits and Gravy (Two-Step Flow)",
            "artist": 'The "Dirty" Gravy Boys 🎻'}
    assert art_stem(item) == "song_1790809859341_ab12cd"
    assert art_seed(132) == (132 * 7919) % 2 ** 31
    assert art_seed(132) == art_seed(132)
    title, artist = scene_texts(item)
    assert title == "Biscuits and Gravy"
    assert artist == "The 'Dirty' Gravy Boys"


# -------------------------------------------------------------- store queries
def test_songs_needing_art_filters(tmp_env):
    cfg, store, tmp = tmp_env
    ok = _song(store, tmp, title="T", artist="A", media="a.mp3")
    _song(store, tmp, title="", artist="A", media="b.mp3")        # no title
    _song(store, tmp, title="T", artist="", media="c.mp3")        # no artist
    _song(store, tmp, media="d.mp3", retired=True)                # retired
    em = store.add_item(type_="song", media_path=str(tmp / "e.mp3"), duration_s=1,
                        title="T", artist="A", emergency=True)   # emergency
    ids = [i["id"] for i in store.songs_needing_art(50, now=1000.0)]
    assert ok in ids and em not in ids
    assert len(ids) == 1


def test_songs_needing_art_respects_backoff(tmp_env):
    cfg, store, tmp = tmp_env
    iid = _song(store, tmp)
    store.set_art_attempted(iid, 900.0)
    assert store.songs_needing_art(10, retry_after_s=300, now=1000.0) == []
    assert [i["id"] for i in store.songs_needing_art(10, retry_after_s=300, now=1300.0)] == [iid]


def test_songs_needing_art_orders_program_first_then_newest(tmp_env):
    cfg, store, tmp = tmp_env
    old = _song(store, tmp, media="old.mp3", created_at="2026-01-01T00:00:00+00:00")
    new = _song(store, tmp, media="new.mp3", created_at="2026-03-01T00:00:00+00:00")
    mid = _song(store, tmp, media="mid.mp3", created_at="2026-02-01T00:00:00+00:00")
    store.append_program(mid, "song", 1.0)
    ids = [i["id"] for i in store.songs_needing_art(10, now=0.0)]
    assert ids == [mid, new, old]


def test_songs_needing_art_skips_songs_with_art(tmp_env):
    cfg, store, tmp = tmp_env
    iid = _song(store, tmp)
    store.set_art(iid, str(tmp / "art.jpg"), {"scene": "s", "seed": 1})
    assert store.songs_needing_art(10, now=0.0) == []


def test_set_art_merges_meta_and_clears_backoff(tmp_env):
    cfg, store, tmp = tmp_env
    iid = store.add_item(type_="song", media_path=str(tmp / "s.mp3"), duration_s=1,
                         title="T", artist="A", meta={"lyrics": "la la"})
    store.set_art_attempted(iid, 5.0)
    store.set_art(iid, "/art/s.jpg", {"scene": "barn", "seed": 9, "prompt": "p"})
    item = store.get_item(iid)
    assert item["art_path"] == "/art/s.jpg"
    assert item["art_attempted_at"] is None
    assert item["meta_json"] is not None
    import json
    meta = json.loads(item["meta_json"])
    assert meta["lyrics"] == "la la" and meta["art"]["seed"] == 9


def test_art_publish_and_counts(tmp_env):
    cfg, store, tmp = tmp_env
    a = _song(store, tmp, media="a.mp3")
    b = _song(store, tmp, media="b.mp3")
    store.set_art(a, "/art/a.jpg", {"scene": "s"})
    assert store.art_counts() == {"with_art": 1, "without_art": 1}
    pending = store.items_to_publish_art()
    assert [i["id"] for i in pending] == [a]
    store.set_art_published(a, "art/a-abc.jpg")
    item = store.get_item(a)
    assert item["art_s3_key"] == "art/a-abc.jpg" and item["art_uploaded_at"]
    assert store.items_to_publish_art() == []
    assert b not in [i["id"] for i in store.items_to_publish_art()]


# --------------------------------------------------------------- publishing
@pytest.mark.asyncio
async def test_art_publishes_with_the_right_key_and_headers(tmp_env):
    from pilgrim.media_publish import ART_CONTENT_TYPE, CACHE_CONTROL, MediaPublisher, art_url
    from pilgrim.tests.fakes.fake_s3 import FakeS3

    cfg, store, tmp = tmp_env
    cfg.media.bucket = "bucket"
    cfg.media.public_base_url = "https://cdn.example/"
    iid = store.add_item(type_="song", media_path=str(tmp / "song_9.mp3"),
                         duration_s=1, title="T", artist="A")
    _, jpg = save_art(cfg, encode_png(32, 32), "song_9")
    store.set_art(iid, str(jpg), {"scene": "s"})
    s3 = FakeS3()
    pub = MediaPublisher(cfg, store, s3)
    assert await pub.publish_pending_art() == 1
    item = store.get_item(iid)
    key = item["art_s3_key"]
    assert key.startswith("art/song_9-") and key.endswith(".jpg")
    assert s3.headers[key]["ContentType"] == ART_CONTENT_TYPE == "image/jpeg"
    assert s3.headers[key]["CacheControl"] == CACHE_CONTROL
    assert art_url(cfg, item) == "https://cdn.example/" + key
    assert item["art_uploaded_at"]
    assert store.items_to_publish_art() == []
    # art_url is "" until uploaded (no origin fallback, ALBUM_ART Step 7)
    assert art_url(cfg, {**item, "art_s3_key": None}) == ""


@pytest.mark.asyncio
async def test_art_publish_failure_backs_off_and_keeps_the_row(tmp_env):
    from pilgrim.media_publish import MediaPublisher
    from pilgrim.tests.fakes.fake_s3 import FakeS3

    cfg, store, tmp = tmp_env
    cfg.media.bucket = "bucket"
    cfg.media.public_base_url = "https://cdn.example/"
    iid = store.add_item(type_="song", media_path=str(tmp / "song_10.mp3"),
                         duration_s=1, title="T", artist="A")
    _, jpg = save_art(cfg, encode_png(32, 32), "song_10")
    store.set_art(iid, str(jpg), {"scene": "s"})
    s3 = FakeS3()
    s3.fail_next = 1
    pub = MediaPublisher(cfg, store, s3)
    assert await pub.publish_pending_art() == 0
    item = store.get_item(iid)
    assert item["art_s3_key"] is None and item["art_uploaded_at"] is None
    assert [i["id"] for i in store.items_to_publish_art()] == [iid]
    # the 5-minute back-off keeps it out of the next batch
    assert await pub.publish_pending_art() == 0
    pub._failed_art.clear()
    assert await pub.publish_pending_art() == 1


def test_missing_art_file_is_a_failure_not_a_crash(tmp_env):
    from pilgrim.media_publish import MediaPublisher
    from pilgrim.tests.fakes.fake_s3 import FakeS3

    cfg, store, tmp = tmp_env
    cfg.media.bucket = "bucket"
    iid = store.add_item(type_="song", media_path=str(tmp / "song_11.mp3"),
                         duration_s=1, title="T", artist="A")
    store.set_art(iid, str(tmp / "gone.jpg"), {"scene": "s"})
    pub = MediaPublisher(cfg, store, FakeS3())
    with pytest.raises(FileNotFoundError):
        pub.publish_art_item(store.get_item(iid))
    assert store.get_item(iid)["art_s3_key"] is None


@pytest.mark.asyncio
async def test_publish_pending_uploads_art_when_no_audio_is_pending(tmp_env):
    """Regression: an empty audio batch must not skip pending covers."""
    from pilgrim.media_publish import MediaPublisher
    from pilgrim.tests.fakes.fake_s3 import FakeS3

    cfg, store, tmp = tmp_env
    cfg.media.bucket = "bucket"
    cfg.media.public_base_url = "https://cdn.example/"
    iid = store.add_item(type_="song", media_path=str(tmp / "song_12.mp3"),
                         duration_s=1, title="T", artist="A")
    _, jpg = save_art(cfg, encode_png(32, 32), "song_12")
    store.set_art(iid, str(jpg), {"scene": "s"})
    s3 = FakeS3()
    pub = MediaPublisher(cfg, store, s3)
    assert await pub.publish_pending() == 1          # nothing to upload but art
    assert store.get_item(iid)["art_s3_key"]

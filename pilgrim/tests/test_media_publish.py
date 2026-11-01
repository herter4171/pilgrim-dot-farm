"""S3 media publishing (RADIO §8.4) against fakes/fake_s3.py — never real S3."""
from __future__ import annotations

import shutil
import sqlite3

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from pilgrim import media_publish
from pilgrim.media_publish import CACHE_CONTROL, MediaPublisher, public_url
from pilgrim.server import create_app
from pilgrim.tests.fakes.fake_s3 import FakeS3
from pilgrim.tools import mp3_migrate


def _mp3(path, body=b"fake mp3 bytes"):
    path.write_bytes(body)
    return str(path)


def _cfg(cfg):
    cfg.media.bucket = "bucket"
    cfg.media.public_base_url = "https://cdn.example/"
    cfg.media.key_prefix = "audio/"
    return cfg


async def test_publish_uploads_and_records_key(tmp_env):
    cfg, store, tmp = tmp_env
    _cfg(cfg)
    iid = store.add_item(type_="song", media_path=_mp3(tmp / "song_1.mp3"), duration_s=1)
    s3 = FakeS3()
    pub = MediaPublisher(cfg, store, s3)
    assert await pub.publish_pending() == 1
    item = store.get_item(iid)
    key = item["s3_key"]
    assert key.startswith("audio/song/song_1-") and key.endswith(".mp3")
    assert s3.objects[key] == b"fake mp3 bytes"
    assert s3.headers[key] == {"ContentType": "audio/mpeg", "CacheControl": CACHE_CONTROL}
    assert item["uploaded_at"]
    assert public_url(cfg, item, iid) == f"https://cdn.example/{key}"
    # nothing left; a second pass is a no-op
    assert await pub.publish_pending() == 0
    assert s3.puts == [key]


async def test_publish_skips_flac_and_emergency(tmp_env):
    cfg, store, tmp = tmp_env
    _cfg(cfg)
    store.add_item(type_="song", media_path=str(tmp / "old.flac"), duration_s=1)
    store.add_item(type_="station_id", media_path=_mp3(tmp / "id.mp3"), duration_s=1,
                   emergency=True)
    assert await MediaPublisher(cfg, store, FakeS3()).publish_pending() == 0


async def test_publish_failure_backs_off_and_never_marks(tmp_env):
    cfg, store, tmp = tmp_env
    _cfg(cfg)
    a = store.add_item(type_="liner", media_path=_mp3(tmp / "a.mp3", b"a"), duration_s=1)
    b = store.add_item(type_="liner", media_path=_mp3(tmp / "b.mp3", b"b"), duration_s=1)
    s3 = FakeS3()
    s3.fail_next = 1
    pub = MediaPublisher(cfg, store, s3)
    cfg.media.concurrency = 1
    assert await pub.publish_pending() == 1
    assert store.get_item(a)["s3_key"] is None          # failed PUT: no key
    assert store.get_item(b)["s3_key"] is not None
    assert await pub.publish_pending() == 0             # a is backing off
    assert public_url(cfg, store.get_item(a), a) == f"/api/media/{a}"


async def test_existing_object_same_size_is_not_reput(tmp_env):
    cfg, store, tmp = tmp_env
    _cfg(cfg)
    iid = store.add_item(type_="news", media_path=_mp3(tmp / "n.mp3"), duration_s=1)
    s3 = FakeS3()
    key = media_publish.object_key(cfg, store.get_item(iid),
                                   media_publish.sha256_file(tmp / "n.mp3"))
    s3.objects[key] = b"fake mp3 bytes"
    await MediaPublisher(cfg, store, s3).publish_pending()
    assert s3.puts == [] and store.get_item(iid)["s3_key"] == key


def test_set_media_file_clears_publication(tmp_env):
    _, store, tmp = tmp_env
    iid = store.add_item(type_="song", media_path="x.flac", duration_s=1)
    store.set_published(iid, "audio/song/x.mp3")
    store.set_media_file(iid, media_path="x.mp3", duration_s=1.5,
                         sample_rate=48000, channels=2)
    it = store.get_item(iid)
    assert it["media_path"] == "x.mp3" and it["s3_key"] is None


def test_program_and_state_carry_urls(tmp_env):
    """RADIO §11: program items, their SFX overlays and the on-air state carry
    the CloudFront URL once uploaded, the origin fallback before."""
    cfg, _, _ = tmp_env
    _cfg(cfg)
    app = create_app(cfg)
    st = app.state.station
    sdb = st.db
    up = sdb.add_item(type_="song", media_path="s.mp3", duration_s=120)
    sdb.set_published(up, "audio/song/s-abc.mp3")
    down = sdb.add_item(type_="liner", media_path="l.mp3", duration_s=5)
    sdb.append_program(up, "song", 120)
    sdb.append_program(down, "liner", 5)
    items = TestClient(app).get("/api/station/program?after_seq=0").json()["items"]
    assert items[0]["url"] == "https://cdn.example/audio/song/s-abc.mp3"
    assert items[1]["url"] == f"/api/media/{down}"
    assert st._with_urls([{"media_id": up}])[0]["url"].startswith("https://cdn.example/")
    assert "current_url" in st.public_state()


def test_media_route_serves_mp3_type(tmp_env):
    cfg, _, tmp = tmp_env
    app = create_app(cfg)
    iid = app.state.station.db.add_item(type_="liner", media_path=_mp3(tmp / "l.mp3"),
                                        duration_s=1)
    r = TestClient(app).get(f"/api/media/{iid}")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
def test_migrate_encodes_beside_flac_and_repoints(tmp_env):
    """tools/mp3_migrate: MP3 next to each FLAC, FLAC kept (hard rule 5),
    emergency rows untouched, a backup taken, rows repointed."""
    cfg, store, tmp = tmp_env
    lib = tmp / "library"
    x = (0.3 * np.sin(2 * np.pi * 330 * np.arange(24000) / 24000)).astype(np.float32)
    flac = lib / "liner_1.flac"
    sf.write(str(flac), x, 24000)
    emer = lib / "id_1.flac"
    sf.write(str(emer), x, 24000)
    a = store.add_item(type_="liner", media_path=str(flac), duration_s=1)
    e = store.add_item(type_="station_id", media_path=str(emer), duration_s=1, emergency=True)
    rows, skipped = mp3_migrate.flac_rows(tmp / "station.db")
    assert [r["id"] for r in rows] == [a] and skipped == 1
    bak = mp3_migrate.backup_db(tmp / "station.db")
    assert sqlite3.connect(bak).execute("select count(*) from items").fetchone()[0] == 2
    ok, fail = mp3_migrate.convert(cfg, store, rows, jobs=2)
    assert (ok, fail) == (1, 0)
    it = store.get_item(a)
    assert it["media_path"].endswith("liner_1.mp3")
    assert it["sample_rate"] == 48000 and it["channels"] == 2
    assert abs(it["duration_s"] - 1.0) < 0.002
    assert flac.exists() and emer.exists()
    assert store.get_item(e)["media_path"] == str(emer)
    # resumable: a second pass finds nothing left to convert
    assert mp3_migrate.flac_rows(tmp / "station.db")[0] == []

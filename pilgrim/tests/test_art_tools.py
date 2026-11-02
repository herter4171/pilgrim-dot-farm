"""art-smoke + reel tools (ALBUM_ART.md Steps 10-11), fakes only.

The reel test runs real ffmpeg over tiny generated inputs (ffmpeg is a CLI
tool, not a backend); the art-smoke test uses FakeFlux + FakeS3 + a fake
CloudFront getter, so no real render or upload happens in `make test`.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from pilgrim.album_art import encode_jpeg
from pilgrim.audio.normalize import encode_mp3
from pilgrim.tests.fakes.fake_flux import FakeFlux, encode_png
from pilgrim.tools import art_smoke, make_reel


# ------------------------------------------------------------------ art-smoke
def test_art_smoke_round_trip_through_fakes(tmp_env, monkeypatch):
    from pilgrim.tests.fakes.fake_s3 import FakeS3

    cfg, _, tmp = tmp_env
    cfg.art.size = "64x64"
    cfg.media.bucket = "bucket"
    cfg.media.public_base_url = "https://cdn.example"
    cfg.logging.dir = str(tmp / "logs")

    fake = FakeFlux(size=(64, 64))
    s3 = FakeS3()
    seen: list[str] = []

    def getter(url: str):
        seen.append(url)
        key = url.rsplit("/", 1)[-1]
        body = s3.objects[f"art/smoke/{key}"]

        class R:
            status_code = 200
            headers = {"content-type": "image/jpeg"}
            content = body

        return R()

    code = art_smoke.run(cfg, fake.client(cfg), s3, getter=getter)
    assert code == 0
    assert seen and seen[0] == "https://cdn.example/art/smoke/" + seen[0].rsplit("/", 1)[-1]
    assert any(k.startswith("art/smoke/art-smoke-") for k in s3.objects)
    assert s3.headers[next(iter(s3.objects))]["ContentType"] == "image/jpeg"
    assert (tmp / "logs" / "art-smoke.png").exists()
    assert fake.calls[0]["response_format"] == "b64_json"


def test_art_smoke_fails_on_a_bad_render(tmp_env):
    from pilgrim.tests.fakes.fake_s3 import FakeS3

    cfg, _, tmp = tmp_env
    cfg.art.size = "64x64"
    cfg.logging.dir = str(tmp / "logs")
    fake = FakeFlux(size=(64, 64))
    fake.fail_mode = "wrong_size"

    def getter(url: str):
        raise AssertionError("must not upload after a bad render")

    from pilgrim.pipelines.art import FluxMalformed
    with pytest.raises(FluxMalformed):
        art_smoke.run(cfg, fake.client(cfg), FakeS3(), getter=getter)


# ----------------------------------------------------------------------- reel
def _mp3(path: Path, seconds: float) -> None:
    sr = 24000
    n = int(sr * seconds)
    t = np.linspace(0, seconds, n, endpoint=False)
    x = (0.25 * np.sin(2 * np.pi * 330 * t)).astype(np.float32)
    src = path.with_suffix(".src.flac")
    sf.write(src, x, sr, format="FLAC")
    from pilgrim.config import load_config
    encode_mp3(src, path, load_config(), 96)
    src.unlink()


def test_reel_builds_to_the_instagram_spec(tmp_env):
    cfg, _, tmp = tmp_env
    cfg.logging.dir = str(tmp / "logs")
    cfg.media.public_base_url = "https://cdn.example"
    song = tmp / "song_reel.mp3"
    _mp3(song, 6.0)
    art = tmp / "art.jpg"
    encode_jpeg_png = encode_png(64, 64)
    png = tmp / "a.png"
    png.write_bytes(encode_jpeg_png)
    encode_jpeg(png, art, 3)

    item = {"media_path": str(song), "s3_key": "audio/song/song_reel-x.mp3",
            "art_s3_key": "art/song_reel-y.jpg"}

    def get(url: str) -> bytes:
        assert url.startswith("https://cdn.example/")
        return art.read_bytes() if url.endswith(".jpg") else song.read_bytes()

    out = make_reel.run(cfg, item, start=0.0, duration=4.0, get=get)
    assert out.name == "song_reel.mp4" and out.exists()
    info = make_reel.probe(out)
    assert make_reel.verify(info, 4.0) == []
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert audio["sample_rate"] == "48000" and audio["channels"] == 2


def test_reel_refuses_unpublished_inputs(tmp_env):
    cfg, _, tmp = tmp_env
    with pytest.raises(RuntimeError, match="s3_key"):
        make_reel.run(cfg, {"media_path": "x.mp3", "s3_key": None,
                            "art_s3_key": "art/x.jpg"}, start=0, duration=1)
    with pytest.raises(RuntimeError, match="cover"):
        make_reel.run(cfg, {"media_path": "x.mp3", "s3_key": "a.mp3",
                            "art_s3_key": None}, start=0, duration=1)


def test_reel_verify_catches_wrong_specs():
    good = {"streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 1080,
         "height": 1920, "r_frame_rate": "30/1", "profile": "High"},
        {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000",
         "channels": 2}], "format": {"duration": "60.0"}}
    assert make_reel.verify(good, 60.0) == []
    bad = {"streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 720,
         "height": 1280, "r_frame_rate": "25/1", "profile": "Main"},
        {"codec_type": "audio", "codec_name": "mp3", "sample_rate": "44100",
         "channels": 1}], "format": {"duration": "90.0"}}
    problems = make_reel.verify(bad, 60.0)
    assert len(problems) == 7
    assert any("duration" in p for p in problems)


def test_reel_uses_cloudfront_urls(tmp_env):
    cfg, _, tmp = tmp_env
    cfg.media.public_base_url = "https://cdn.example/"
    assert make_reel._url(cfg, "art/a.jpg") == "https://cdn.example/art/a.jpg"
    assert "scale=1080:1080" in make_reel.filter_graph(60)
    assert "afade=t=out:st=58" in make_reel.filter_graph(60)

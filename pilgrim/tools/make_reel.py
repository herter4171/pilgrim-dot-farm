"""Instagram Reel builder (ALBUM_ART.md Step 11; RADIO §8.4).

Inputs come from S3 **through CloudFront** — the song's published MP3 and its
published cover JPEG — which proves the published copies are good. Output is
a 1080x1920 H.264/AAC vertical Reel under `pilgrim/logs/reels/`. ffmpeg only;
uploading to Instagram is out of scope.

    python -m pilgrim.tools.make_reel --item 132 [--start 0] [--duration 60]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import httpx

from pilgrim.config import ROOT, Config, load_config
from pilgrim.db import open_store

WIDTH, HEIGHT, FPS = 1080, 1920, 30


def _ffmpeg() -> str:
    return "ffmpeg"


def _ffprobe() -> str:
    return "ffprobe"


def downloader(url: str) -> bytes:
    with httpx.Client(timeout=120.0, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.content


def filter_graph(duration_s: float) -> str:
    return (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=40:2,eq=brightness=-0.15[bg];"
        "[0:v]scale=1080:1080[fg];"
        "[bg][fg]overlay=0:(H-h)/2,format=yuv420p[v];"
        f"[1:a]afade=t=in:d=0.5,afade=t=out:st={max(0.0, duration_s - 2):g}:d=2[a]"
    )


def build_command(art: Path, song: Path, out: Path, start: float,
                  duration: float) -> list[str]:
    return [
        _ffmpeg(), "-y", "-loglevel", "error",
        "-loop", "1", "-framerate", str(FPS), "-i", str(art),
        "-ss", f"{start:g}", "-t", f"{duration:g}", "-i", str(song),
        "-filter_complex", filter_graph(duration),
        "-map", "[v]", "-map", "[a]", "-t", f"{duration:g}", "-r", str(FPS),
        "-c:v", "libx264", "-profile:v", "high", "-preset", "medium",
        "-crf", "20", "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(out),
    ]


def probe(path: Path) -> dict:
    p = subprocess.run(
        [_ffprobe(), "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,"
         "channels,profile:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {p.stderr.strip()[:300]}")
    return json.loads(p.stdout)


def verify(info: dict, duration: float) -> list[str]:
    """The Reel spec checks; returns a list of failures (empty = good)."""
    bad: list[str] = []
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video:
        bad.append("no video stream")
    else:
        if (video.get("width"), video.get("height")) != (WIDTH, HEIGHT):
            bad.append(f"video is {video.get('width')}x{video.get('height')}, "
                       f"want {WIDTH}x{HEIGHT}")
        if video.get("codec_name") != "h264":
            bad.append(f"video codec {video.get('codec_name')}, want h264")
        if video.get("profile") not in ("High", "high"):
            bad.append(f"profile {video.get('profile')}, want High")
        if video.get("r_frame_rate") != f"{FPS}/1":
            bad.append(f"frame rate {video.get('r_frame_rate')}, want {FPS}/1")
    if not audio:
        bad.append("no audio stream")
    else:
        if audio.get("codec_name") != "aac":
            bad.append(f"audio codec {audio.get('codec_name')}, want aac")
        if str(audio.get("sample_rate")) != "48000":
            bad.append(f"sample rate {audio.get('sample_rate')}, want 48000")
        if int(audio.get("channels") or 0) != 2:
            bad.append(f"channels {audio.get('channels')}, want 2")
    got = float((info.get("format") or {}).get("duration") or 0)
    if got <= 0 or got > duration + 0.5:
        bad.append(f"duration {got:.2f}s (asked {duration:g}s)")
    return bad


def _url(cfg: Config, key: str) -> str:
    return cfg.media.public_base_url.rstrip("/") + "/" + key


def run(cfg: Config, item: dict, *, start: float, duration: float,
        get: Callable[[str], bytes] = downloader) -> Path:
    """Download from CloudFront, build the Reel, verify it. Returns the path."""
    if not item.get("s3_key"):
        raise RuntimeError("song is not in S3 yet (no s3_key)")
    if not item.get("art_s3_key"):
        raise RuntimeError("song has no published cover (no art_s3_key)")
    out_dir = ROOT / cfg.logging.dir / "reels"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(str(item["media_path"])).stem
    out = out_dir / f"{stem}.mp4"
    with tempfile.TemporaryDirectory() as td:
        art = Path(td) / "art.jpg"
        song = Path(td) / "song.mp3"
        art.write_bytes(get(_url(cfg, str(item["art_s3_key"]))))
        song.write_bytes(get(_url(cfg, str(item["s3_key"]))))
        if not art.read_bytes().startswith(b"\xff\xd8"):
            raise RuntimeError("CloudFront art is not a JPEG")
        if not song.read_bytes().startswith((b"ID3", b"\xff\xfb", b"\xff\xf3",
                                            b"\xff\xf2")):
            raise RuntimeError("CloudFront song is not an MP3")
        cmd = build_command(art, song, out, start, duration)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if p.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {p.stderr.strip()[:500]}")
    bad = verify(probe(out), duration)
    if bad:
        raise RuntimeError("; ".join(bad))
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--item", type=int, required=True)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--duration", type=float, default=60.0)
    args = ap.parse_args(argv)

    cfg = load_config()
    item = open_store(cfg).get_item(args.item)
    if not item or item["type"] != "song":
        print(f"no song {args.item}", file=sys.stderr)
        raise SystemExit(2)
    out = run(cfg, item, start=args.start, duration=args.duration)
    print(f"reel: {out}")
    raise SystemExit(0)


if __name__ == "__main__":
    main()

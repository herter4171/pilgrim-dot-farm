"""Real-backend media smoke (RADIO §8.4): `make media-smoke`. Only when asked.

Encodes a 2 s tone exactly like production delivery, uploads it with the
station's S3ObjectStore under `<key_prefix>_smoke/`, then fetches it through
CloudFront as a browser on the public site would and checks status, type,
cache and CORS headers and the bytes. Prints PASS/FAIL per check.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf

from pilgrim.audio.normalize import encode_mp3
from pilgrim.config import load_config
from pilgrim.media_publish import CACHE_CONTROL, CONTENT_TYPE, S3ObjectStore, sha256_file

ORIGIN = "https://pilgrim.farm"


def main() -> int:
    cfg = load_config()
    fails = 0

    def check(ok: bool, what: str) -> None:
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + what)
        fails += 0 if ok else 1

    with tempfile.TemporaryDirectory() as d:
        src, mp3 = Path(d) / "tone.flac", Path(d) / "tone.mp3"
        t = np.arange(48000 * 2) / 48000
        sf.write(str(src), (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), 48000)
        encode_mp3(src, mp3, cfg, cfg.audio.mp3_mono_source_kbps)
        key = f"{cfg.media.key_prefix}_smoke/tone-{sha256_file(mp3)[:16]}.mp3"
        store = S3ObjectStore(cfg)
        store.put_file(key, mp3, content_type=CONTENT_TYPE, cache_control=CACHE_CONTROL)
        check(store.head(key) == mp3.stat().st_size, f"s3 put+head s3://{cfg.media.bucket}/{key}")
        url = f"{cfg.media.public_base_url.rstrip('/')}/{key}"
        r = httpx.get(url, headers={"Origin": ORIGIN}, timeout=20.0)
        print(f"     GET {url} -> {r.status_code}")
        for h in ("content-type", "cache-control", "access-control-allow-origin",
                  "x-cache", "content-length"):
            print(f"     {h}: {r.headers.get(h)}")
        check(r.status_code == 200, "cloudfront 200")
        check(r.content == mp3.read_bytes(), "cloudfront bytes match")
        check(r.headers.get("content-type") == CONTENT_TYPE, "content-type audio/mpeg")
        check(r.headers.get("cache-control") == CACHE_CONTROL, "immutable cache-control")
        check(r.headers.get("access-control-allow-origin") in ("*", ORIGIN),
              "CORS allows the site (fetch + decodeAudioData)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

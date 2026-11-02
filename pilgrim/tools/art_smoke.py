"""Cover-art smoke test (ALBUM_ART.md Step 10; RADIO §8.4).

One real FLUX render of a fixed test title -> PNG dimension check -> JPEG ->
PUT to `art/smoke/` -> GET it back through CloudFront (status + content-type).
The PNG is saved to `pilgrim/logs/art-smoke.png` so a human can eyeball it
(title spelled right? square, not a disc?). Real backends: only when asked
(`make art-smoke`).

    python -m pilgrim.tools.art_smoke
"""
from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path

import httpx

from pilgrim.album_art import ART_CONTENT_TYPE, encode_jpeg
from pilgrim.config import ROOT, Config, load_config
from pilgrim.media_publish import CACHE_CONTROL, S3ObjectStore, sha256_file
from pilgrim.pipelines.art import FluxClient, build_flux_prompt, parse_size, validate_png

SMOKE_TITLE = "Biscuits and Gravy"
SMOKE_ARTIST = "The Dirty Gravy Boys"
SMOKE_SCENE = ("A rust-orange risograph print of a dented grain silo at dusk, its hatch "
               "propped open on a tangle of extension cords, three crows perched along "
               "the rim; flat limited palette of rust, bone, midnight blue and sage, "
               "heavy paper grain and hard-edged shapes.")
SMOKE_SEED = 7


def _template(cfg: Config) -> str:
    return (ROOT / cfg.library.prompts_dir / "album_art_flux.md").read_text()


def _getter(url: str) -> httpx.Response:
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        return c.get(url)


def run(cfg: Config, flux: FluxClient, objects: S3ObjectStore,
        getter=_getter) -> int:
    """Returns a process exit code; prints PASS/FAIL lines."""
    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
        if not ok:
            failures += 1

    prompt = build_flux_prompt(_template(cfg), SMOKE_SCENE, SMOKE_TITLE, SMOKE_ARTIST)

    async def render() -> tuple[bool, bytes | None]:
        # One event loop for health + generate + close: httpx.AsyncClient is
        # bound to the loop it first ran on.
        try:
            healthy = await flux.health()
            png = await flux.generate(prompt, SMOKE_SEED) if healthy else None
            return healthy, png
        finally:
            await flux.close()

    healthy, png = asyncio.run(render())
    check("FLUX /health", healthy)
    if png is None:
        print("art-smoke: FAIL")
        return 1
    want = parse_size(cfg.art.size)
    dims = validate_png(png, want)
    check("PNG is the requested size", dims == want, f"{dims[0]}x{dims[1]}")

    logs = ROOT / cfg.logging.dir
    logs.mkdir(parents=True, exist_ok=True)
    png_path = logs / "art-smoke.png"
    png_path.write_bytes(png)
    check("PNG saved for eyeballing", png_path.exists(), str(png_path))

    with tempfile.TemporaryDirectory() as td:
        jpg_path = Path(td) / "art-smoke.jpg"
        encode_jpeg(png_path, jpg_path, cfg.art.jpeg_quality)
        ok = jpg_path.exists() and jpg_path.read_bytes().startswith(b"\xff\xd8")
        check("JPEG encodes", ok, f"{jpg_path.stat().st_size if ok else 0} bytes")
        if not ok:
            return 1
        jpg_bytes = jpg_path.read_bytes()

        key = f"{cfg.art.key_prefix}smoke/art-smoke-{sha256_file(jpg_path)[:16]}.jpg"
        objects.put_file(key, jpg_path, content_type=ART_CONTENT_TYPE,
                         cache_control=CACHE_CONTROL)
        check("PUT to S3", True, key)

    url = cfg.media.public_base_url.rstrip("/") + "/" + key
    r = getter(url)
    check("CloudFront GET 200", r.status_code == 200, str(r.status_code))
    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
    check("CloudFront content-type", ctype == "image/jpeg", ctype)
    check("bytes match the upload",
          hashlib.sha256(r.content).hexdigest() == hashlib.sha256(jpg_bytes).hexdigest(),
          url)
    return 1 if failures else 0


def main() -> None:
    cfg = load_config()
    code = run(cfg, FluxClient(cfg), S3ObjectStore(cfg))
    print("art-smoke:", "PASS" if code == 0 else "FAIL")
    raise SystemExit(code)


if __name__ == "__main__":
    main()

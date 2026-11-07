"""Render operator tagline lines as evergreen liner items (bible/taglines.md).

Feed one on-air tagline per line (stdin or as argv) and each line is:
  1. rendered verbatim through the mandatory TTS cleanup + Kokoro
     (`liners` voice, `render_literal` — never LLM-rewritten, §11 operator
     phrases),
  2. QC'd + normalized to a 48 kHz stereo MP3 in the library,
  3. stored as a recyclable, evergreen `liner` item (kind `station_id`,
     same shape as the 2026-11-02 tagline batch 7771-7790),
  4. published to S3/CloudFront (RADIO §8.4).

On-air copy, not canon prose, goes in here: the TTS-safe variants from
prompts/voice.md ("ninety degrees Celsius", "nonsense"). Lines whose on-air
text is already in the library are skipped, so the script is idempotent —
safe to re-run on partial failure.
"""
from __future__ import annotations

import asyncio
import json
import sys

from pilgrim.config import load_config
from pilgrim.db import open_store
from pilgrim.media_publish import MediaPublisher, S3ObjectStore
from pilgrim.pipelines.voice import KokoroClient, VoicePipeline
from pilgrim.store import Store


def read_lines() -> list[str]:
    if len(sys.argv) > 1:
        return [a.strip() for a in sys.argv[1:] if a.strip()]
    raw = sys.stdin.read()
    return [ln.strip() for ln in raw.splitlines() if ln.strip()]


def existing_liner_texts(store: Store) -> set[str]:
    rows = store._conn.execute(  # noqa: SLF001 — tool, single query
        "SELECT meta_json FROM items WHERE type='liner'").fetchall()
    out: set[str] = set()
    for r in rows:
        m = r["meta_json"] if "meta_json" in r.keys() else r[0]
        if m:
            t = json.loads(m).get("text")
            if t:
                out.add(t)
    return out


async def main() -> int:
    lines = read_lines()
    if not lines:
        print("usage: tagline_render.py <line> ...  (or pipe lines on stdin)")
        return 2
    cfg = load_config()
    store = open_store(cfg)  # live engine: RDS when DATABASE_URL is set, else SQLite
    voice = cfg.voices.liners
    vp = VoicePipeline(cfg, None, KokoroClient(cfg))  # type: ignore[arg-type]  # render-only, no LLM
    publisher = MediaPublisher(cfg, store, S3ObjectStore(cfg))
    have = existing_liner_texts(store)
    done, skipped, failed = 0, 0, 0
    try:
        for i, text in enumerate(lines, 1):
            if text in have:
                print(f"[{i}/{len(lines)}] skip (already in library): {text[:60]}...")
                skipped += 1
                continue
            try:
                rendered = await vp.render_literal(text, voice=voice)
                item = VoicePipeline.item_dict(
                    "liner", {"text": text, "evergreen": True}, rendered)
                item["meta"]["kind"] = "station_id"
                rid = store.add_item(
                    type_="liner", media_path=item["media_path"],
                    duration_s=item["duration_s"],
                    sample_rate=item["sample_rate"],
                    channels=item["channels"], role="liner",
                    evergreen=True, meta=item["meta"])
                row = store.get_item(rid)
                assert row is not None
                key = publisher.publish_item(row)
                print(f"[{i}/{len(lines)}] id={rid} {item['duration_s']:.1f}s "
                      f"{key}: {text[:60]}...")
                done += 1
            except Exception as e:  # keep going; re-run to finish
                print(f"[{i}/{len(lines)}] FAILED: {e} :: {text[:60]}...")
                failed += 1
    finally:
        await vp.kokoro.close()
    print(f"rendered={done} skipped={skipped} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

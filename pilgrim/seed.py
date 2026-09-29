"""Cold-start seeder (RADIO.md §13). Pre-populates voice inventory (liners,
commercials, DJ talk, one news bulletin) so the station has immediate air content.
Song generation is slow and is left to the background song worker during `run`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

log = logging.getLogger("radio.seed")

from pilgrim.config import RNG, ROOT, Clock, Config, ensure_dirs, load_config  # noqa: E402
from pilgrim.logging_setup import setup_logging  # noqa: E402
from pilgrim.pipelines.llm import LLM  # noqa: E402
from pilgrim.pipelines.news import NewsPipeline  # noqa: E402
from pilgrim.pipelines.songs import SongPipeline  # noqa: E402
from pilgrim.pipelines.voice import KokoroClient, VoicePipeline  # noqa: E402
from pilgrim.producer import Producer  # noqa: E402
from pilgrim.store import Store  # noqa: E402


def _prompts(cfg: Config) -> dict:
    pdir = ROOT / cfg.library.prompts_dir
    return {f.replace(".md", ""): (pdir / f).read_text()
            for f in ("voice.md", "song_brief.md", "news.md") if (pdir / f).exists()}


async def seed(cfg: Config, api_key: str) -> int:
    ensure_dirs(cfg)
    media_dir = ROOT / cfg.library.dir
    db = Store(ROOT / cfg.library.db)
    llm = LLM(cfg, api_key)
    kokoro = KokoroClient(cfg)
    voice = VoicePipeline(cfg, llm, kokoro, db, media_dir, prompts=_prompts(cfg))
    songs = SongPipeline(cfg, llm, media_dir, prompts=_prompts(cfg))
    news = NewsPipeline(cfg, llm, api_key=api_key, prompts=_prompts(cfg))
    prod = Producer(cfg, db, llm, kokoro, voice, songs, Clock(), prompts=_prompts(cfg),
                    media_dir=media_dir, api_key=api_key, rng=RNG(cfg.station.rng_seed),
                    news_pipeline=news)
    made = 0
    log.info("seeding commercials...")
    n0 = db.count_fresh_of_type("commercial")
    await prod.ensure_commercials()
    made += db.count_fresh_of_type("commercial") - n0
    log.info("seeding liners...")
    n0 = db.count_fresh_of_type("liner")
    await prod.ensure_liners()
    made += db.count_fresh_of_type("liner") - n0
    log.info("seeding dj talk...")
    n0 = db.count_fresh_of_type("dj_talk")
    await prod.ensure_dj()
    made += db.count_fresh_of_type("dj_talk") - n0
    log.info("seeding news bulletin...")
    n0 = db.count_fresh_of_type("news")
    await prod.ensure_news()
    made += db.count_fresh_of_type("news") - n0
    log.info("seeding songs...")
    # §13: songs are seeded too, so cold start isn't dependent on the slow
    # background song worker. Seed at the fresh-ready target (expensive on mlx).
    fresh_songs = db.count_fresh_of_type("song")
    target_songs = cfg.inventory.fresh_songs_ready - fresh_songs
    for _ in range(max(0, target_songs)):
        try:
            brief = await songs.brief(prod._recent_genres())
            item = await songs.produce_song(brief)
            db.add_item(
                type_="song", media_path=item["media_path"],
                duration_s=item["duration_s"], sample_rate=item.get("sample_rate"),
                channels=item.get("channels"), title=item.get("title"),
                artist=item.get("artist"), genre=item.get("genre"),
                evergreen=True, fresh=True, meta=item.get("meta"))
            made += 1
        except Exception as e:
            log.warning("seed song failed: %s", e)
            break
    await llm.close()
    await kokoro.close()
    await songs.close()
    return made


def main() -> int:
    cfg = load_config()
    setup_logging(cfg)
    api_key = os.environ.get("LITELLM_TOKEN", "")
    if not api_key:
        print("LITELLM_TOKEN not set in environment", file=sys.stderr)
        return 1
    n = asyncio.run(seed(cfg, api_key))
    print(f"seed complete: {n} new items in inventory")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

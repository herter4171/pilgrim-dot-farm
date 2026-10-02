"""Operator on-air announcement (stopgap until the TUI phrases API, TUI.md §6).

Renders a literal text line with the DJ voice (am_liam, no LLM rewrite) and
stores it as a fresh `dj_talk` item. The scheduler airs it at the next DJ
words slot, picked at random from the eligible fresh pool (RADIO.md §5.2).

`--sole` retires the other currently-eligible fresh dj_talk items via the
sanctioned `store.retire_item` (rows and media files stay; the producer
refills the pool), so this line is the only candidate and is guaranteed to
air at the next DJ slot.

Usage:
    python -m pilgrim.tools.dj_announce "This is the official DJ. ..."
    python -m pilgrim.tools.dj_announce --sole "This is the official DJ. ..."
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime

from pilgrim.config import (
    ROOT,
    Config,
    load_api_key,
    load_config,
)
from pilgrim.pipelines.llm import LLM
from pilgrim.pipelines.voice import KokoroClient, VoicePipeline
from pilgrim.store import Store

log = logging.getLogger("radio.announce")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


async def announce(text: str, sole: bool) -> int:
    text = " ".join(text.split())
    if not text:
        print("empty announcement")
        return 2
    cfg: Config = load_config()
    db = Store(ROOT / cfg.library.db)
    llm = LLM(cfg, load_api_key())
    kokoro = KokoroClient(cfg)
    voice = VoicePipeline(cfg, llm, kokoro, db, ROOT / cfg.library.dir)
    # literal path: cleanup -> TTS -> QC -> normalize. 17 words ~ 6 s at the
    # station's ~3.6 w/s; the render tolerance band is 0.4-2.2x of target.
    target_s = max(4.0, min(12.0, len(text.split()) / 3.0))
    r = await voice.render({"text": text}, "dj_talk", target_s)
    item_id = db.add_item(
        type_="dj_talk", media_path=str(r["path"]), duration_s=r["duration_s"],
        sample_rate=r["sample_rate"], channels=r["channels"], role="dj_talk",
        evergreen=True, fresh=True, expires_at=None,
        meta={"text": text, "announce": True, "words_per_s": r["words_per_s"]})
    print(f"announced item {item_id}: {r['duration_s']:.1f}s "
          f"({r['words']} words, {r['words_per_s']:.1f} w/s, voice {cfg.voices.dj})")
    if sole:
        now = datetime.now(UTC)
        retired: list[int] = []
        for i in db.list_items("dj_talk"):
            if i["id"] == item_id or i["retired"] or i["emergency"] or not i["fresh"]:
                continue
            exp = _parse_iso(i.get("expires_at"))
            if exp is not None and exp < now:
                continue  # already ineligible
            db.retire_item(i["id"], "operator: pool cleared for on-air announcement")
            retired.append(i["id"])
        print(f"retired eligible fresh dj_talk {retired} (pool refill is automatic)")
        print("sole candidate -> airs at the next DJ slot with certainty")
    else:
        print("candidate in the fresh pool -> airs at a random future DJ slot")
    await kokoro.close()
    await llm.close()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("text", help="the exact line to air")
    ap.add_argument("--sole", action="store_true",
                    help="retire other eligible fresh dj_talk so this line is "
                         "the only candidate at the next DJ slot")
    a = ap.parse_args()
    try:
        return asyncio.run(announce(a.text, a.sole))
    except Exception as e:  # QC / TTS / DB: one clear failure, no retry storm
        log.error("announce failed: %s", e)
        print(f"announce failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

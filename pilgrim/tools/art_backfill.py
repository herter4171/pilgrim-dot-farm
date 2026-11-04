"""Cover-art backfill (ALBUM_ART.md Step 9; RADIO §8.4).

Dry run by default: it lists what would be rendered. `--apply` renders
(resuming where it left off), newest first, reusing the live worker's
per-song path and memory gate. The live worker gets through the backlog on
its own; this tool is for doing it deliberately (overnight) or re-rendering
one song with `--item ID --force [--seed N]`.

`--scene` skips the scene LLM and renders an operator-supplied scene (same
validation as LLM output); it is for a targeted re-render that must change
the picture, e.g. "more skeletons". `--ignore-lease` is the operator override
for that case while the station worker is active: FLUX serializes the two
renders, so nothing corrupts, but the second waits.

Refuses to run with `--apply` while the station's art worker holds a fresh
lease, so two renderers never contend for the single-flight FLUX server.

    python -m pilgrim.tools.art_backfill                  # dry run
    python -m pilgrim.tools.art_backfill --apply --limit 20
    python -m pilgrim.tools.art_backfill --item 132 --force --seed 7
    python -m pilgrim.tools.art_backfill --item 132 --force --apply \
        --scene "..." --ignore-lease
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime

from pilgrim.album_art import LEASE_KEY, LEASE_STALE_S, ArtWorker, scene_texts
from pilgrim.config import ROOT, Config, load_config
from pilgrim.logging_setup import setup_logging
from pilgrim.pipelines.art import FluxClient
from pilgrim.pipelines.llm import LLM
from pilgrim.store import Store


def _load_prompts(cfg: Config) -> dict[str, str]:
    pdir = ROOT / cfg.library.prompts_dir
    return {name: (pdir / f"{name}.md").read_text()
            for name in ("album_art_scene", "album_art_flux")
            if (pdir / f"{name}.md").exists()}


def lease_is_fresh(store: Store, now: float | None = None) -> bool:
    """True when the live worker heartbeated recently (never run two)."""
    raw = store.meta_get(LEASE_KEY)
    if not raw:
        return False
    try:
        beat = datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return False
    now = time.time() if now is None else now
    return (now - beat) < LEASE_STALE_S


def _targets(store: Store, cfg: Config, args: argparse.Namespace) -> list[dict]:
    if args.item:
        item = store.get_item(int(args.item))
        if not item:
            print(f"no item {args.item}", file=sys.stderr)
            return []
        if item["type"] != "song" or item["retired"] or item["emergency"]:
            print(f"item {args.item} is not an eligible song", file=sys.stderr)
            return []
        title, artist = scene_texts(item)
        if not title or not artist:
            print(f"item {args.item} has no title/artist: no art", file=sys.stderr)
            return []
        if item["art_path"] and not args.force:
            print(f"item {args.item} already has art (use --force to re-render)")
            return []
        return [item]
    return store.songs_needing_art(args.limit, retry_after_s=0.0)


async def run(cfg: Config, store: Store, args: argparse.Namespace) -> int:
    targets = _targets(store, cfg, args)
    if not targets:
        print("nothing to render")
        return 0
    if not args.apply:
        for it in targets:
            title, artist = scene_texts(it)
            print(f"{it['id']}: {title} - {artist}"
                  + ("  (has art)" if it["art_path"] else ""))
        print(f"{len(targets)} song(s) would be rendered "
              f"(dry run; pass --apply to render)")
        return 0

    llm = LLM(cfg, api_key=_api_key(cfg))
    worker = ArtWorker(cfg, store, llm, _load_prompts(cfg), flux=FluxClient(cfg))
    ok = 0
    try:
        for it in targets:
            got = await worker.process(it, seed=args.seed, scene=args.scene)
            ok += 1 if got else 0
            print(f"{it['id']}: {'rendered' if got else 'skipped (back-off set)'}",
                  flush=True)
            if not got:
                break       # FLUX is busy/down: stop rather than hammer it
    finally:
        await worker.close()
        await llm.close()
    print(f"{ok}/{len(targets)} rendered")
    return 0 if ok else 1


def _api_key(cfg: Config) -> str:
    from pilgrim.config import load_secret
    return load_secret("LITELLM_TOKEN")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="render (default: dry run listing only)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--item", type=int, default=None,
                    help="render one song by id (with --force to re-render)")
    ap.add_argument("--force", action="store_true",
                    help="re-render a song that already has art")
    ap.add_argument("--seed", type=int, default=None,
                    help="override the deterministic seed")
    ap.add_argument("--scene", type=str, default=None,
                    help="operator-supplied scene (skips the scene LLM; "
                         "requires --item, e.g. to force a visual change)")
    ap.add_argument("--ignore-lease", action="store_true",
                    help="run even if the station's art worker is active "
                         "(single targeted re-render only; FLUX serializes)")
    args = ap.parse_args(argv)

    cfg = load_config()
    setup_logging(cfg)
    store = Store(ROOT / cfg.library.db)

    if args.scene and not args.item:
        print("--scene requires --item (targeted re-render only)", file=sys.stderr)
        raise SystemExit(2)
    if args.apply and lease_is_fresh(store) and not args.ignore_lease:
        print("the station's art worker is running (fresh lease): refusing to "
              "render, so two renderers never contend "
              "(use --ignore-lease for one targeted re-render)", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(run(cfg, store, args)))


if __name__ == "__main__":
    main()

"""Standalone 24-hour station simulation with assertions (RADIO.md §15).
Runs the real scheduler over a SimClock + fakes; identical seeds give identical
programs. Print PASS/FAIL and a summary. Use via `make sim`.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_TESTDIR = Path(__file__).resolve().parent        # pilgrim/tests
_REPO = Path(__file__).resolve().parents[2]      # repo root
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_TESTDIR))

from conftest import seed_pool  # noqa: E402
from pilgrim.config import RNG, SimClock, ensure_dirs, load_config  # noqa: E402
from pilgrim.scheduler import Scheduler  # noqa: E402
from pilgrim.selector import RandomSelector  # noqa: E402
from pilgrim.store import Store  # noqa: E402


def _seed_sfx(store) -> set[int]:
    """SFX stock + hosts that carry overlays: field reports with a bed and a
    stinger, DJ talk with a joke, commercials (recycled) with a joke and a
    burst of stingers that the rate budget must thin out. Returns the ids of
    hosts carrying a joke cue."""
    rim = store.add_item(type_="sfx", media_path="rim.flac", duration_s=1.5,
                         evergreen=True, meta={"cue": "rimshot"})
    moo = store.add_item(type_="sfx", media_path="moo.flac", duration_s=2.0,
                         meta={"cue": "cow"})
    bed = store.add_item(type_="sfx", media_path="bed.flac", duration_s=25.0,
                         meta={"cue": "wind_bed"})

    def cue(item_id, kind, off, dur):
        return {"item_id": item_id, "cue": "x", "kind": kind, "offset_s": off,
                "duration_s": dur}
    jokes: set[int] = set()
    for _ in range(40):
        store.add_item(type_="field_report", media_path="fr.flac", duration_s=22.0,
                       evergreen=False, meta={"sfx": [cue(bed, "bed", 0.0, 25.0),
                                                      cue(moo, "stinger", 6.0, 2.0)]})
    for _ in range(30):
        jokes.add(store.add_item(
            type_="dj_talk", media_path="dj.flac", duration_s=18.0,
            meta={"sfx": [cue(rim, "joke", 9.0, 1.5)]}))
    for _ in range(10):
        jokes.add(store.add_item(
            type_="commercial", media_path="c.flac", duration_s=25.0,
            meta={"sfx": [cue(moo, "stinger", 1.0 + k * 1.2, 1.0) for k in range(5)]
                  + [cue(rim, "joke", 20.0, 1.5)]}))
    # news that (wrongly) carries a cue: the scheduler must still air it dry
    store.add_item(type_="news", media_path="n.flac", duration_s=30.0,
                   meta={"sfx": [cue(moo, "stinger", 2.0, 2.0)]})
    return jokes


def run(hours=24):
    cfg = load_config()
    tmp = Path(tempfile.mkdtemp(prefix="radio_sim_"))
    cfg.library.dir = str(tmp / "library")
    cfg.library.db = str(tmp / "station.db")
    ensure_dirs(cfg)
    store = Store(tmp / "station.db")
    seed_pool(store, cfg, n_song=80, n_liner=50, n_com=30, n_dj=25)
    # glue intros to a few stock songs so the intro path is exercised (4.7)
    import json as _json
    for s in store.list_items("song")[:4]:
        store.add_item(type_="intro", media_path="intro.flac", duration_s=8.0,
                       evergreen=False, fresh=True,
                       meta={"song_item_id": s["id"]})
    joke_hosts = _seed_sfx(store)

    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(7), cfg), clock, RNG(7))
    seen = []
    last_seq = 0
    step = 30
    max_run_non_song = 0
    min_coverage = float("inf")
    overlays: dict[int, list[dict]] = {}  # seq -> aired SFX overlays
    sched.commit_lookahead()  # the station commits at startup (Station.startup)
    for _ in range(int(hours * 3600) // step):
        clock.advance(step)
        sched.commit_lookahead()
        min_coverage = min(min_coverage, sched.coverage())
        for r in store.program_after(last_seq):
            seen.append((r["seq"], r["type"], r["duration_s"], r["item_id"]))
            overlays[r["seq"]] = sched.overlays_for(r["seq"])
            last_seq = max(last_seq, r["seq"])

    # ---- assertions ----
    fails = []
    seqs = [s for s, _, _, _ in seen]
    types = [t for _, t, _, _ in seen]
    if seqs != list(range(1, len(seqs) + 1)):
        fails.append("seq missing, duplicated, or out of order")
    if min_coverage < cfg.playout.committed_lookahead_s:
        fails.append("committed coverage below lookahead target")
    if sum(d for _, _, d, _ in seen) < hours * 3600 + cfg.playout.committed_lookahead_s:
        fails.append("program does not cover the full simulation plus lookahead")
    if any(d <= 0 for _, _, d, _ in seen):
        fails.append("non-positive duration")

    runn = 0
    for t in types:
        if t == "intro":
            continue  # intros are part of their song: no non-song run (4.7 rule 6)
        runn = 0 if t == "song" else runn + 1
        max_run_non_song = max(max_run_non_song, runn)
        if runn > cfg.playout.max_consecutive_non_song:
            fails.append("too many consecutive non-song segments")
            break

    for i, t in enumerate(types):
        if t == "dj_talk":
            if i > 0 and types[i - 1] in ("dj_talk", "news"):
                fails.append("dj_talk adjacent to dj_talk/news")
            if i < len(types) - 1 and types[i + 1] in ("dj_talk", "news"):
                fails.append("dj_talk adjacent to dj_talk/news")
            # dj_talk must never sit directly before an intro (4.7 rule 6)
            if i < len(types) - 1 and types[i + 1] == "intro":
                fails.append("dj_talk directly before an intro")

    # every committed intro is immediately followed by the song it names (4.7)
    for i, t in enumerate(types):
        if t != "intro":
            continue
        nxt = seen[i + 1] if i + 1 < len(seen) else None
        if not nxt or nxt[1] != "song":
            fails.append("intro not immediately followed by a song")
            continue
        intro_item = store.get_item(seen[i][3])
        imeta = _json.loads(intro_item["meta_json"] or "{}") if intro_item else {}
        if imeta.get("song_item_id") != nxt[3]:
            fails.append("intro followed by a different song")

    # field reports are comic: never next to news (SFX.md §4.2)
    for a, b in zip(types, types[1:], strict=False):
        if {a, b} == {"field_report", "news"} or a == b == "field_report":
            fails.append("field_report adjacent to news/field_report")
            break

    # ---- SFX policy (SFX.md §0, §7.5) ----
    start = 0.0
    stinger_starts: list[float] = []
    joke_chances = jokes_aired = 0
    for seq, t, dur, item_id in seen:
        ovs = overlays.get(seq, [])
        if ovs and t not in ("dj_talk", "field_report", "commercial", "liner"):
            fails.append(f"sfx on a {t}")
        for o in ovs:
            if o["offset_s"] < 0 or o["offset_s"] + o["duration_s"] > dur + 1e-6:
                fails.append("sfx overlay runs outside its host")
            if o["kind"] != "bed":
                stinger_starts.append(start + o["offset_s"])
        if item_id in joke_hosts:
            joke_chances += 1
            jokes_aired += any(o["kind"] == "joke" for o in ovs)
        start += dur
    win, cap = cfg.sfx.window_s, cfg.sfx.max_per_window
    for i, t0 in enumerate(stinger_starts):
        if sum(1 for t1 in stinger_starts[i:] if t1 < t0 + win) > cap:
            fails.append(f"more than {cap} stingers in {win:.0f}s")
            break
    joke_rate = jokes_aired / joke_chances if joke_chances else 0.0
    if joke_chances < 50 or not 0.35 <= joke_rate <= 0.65:
        fails.append(f"joke rimshot rate {joke_rate:.2f} over {joke_chances} jokes, want ~0.5")

    from collections import Counter
    mix = Counter(types)
    if not seen:
        fails.append("no program produced")

    print("=" * 60)
    audio_s = sum(d for _, _, d, _ in seen)
    print(f"24h simulated run: {len(seen)} committed items, {audio_s:.0f}s audio")
    print("segment mix:", dict(mix))
    print(f"minimum committed coverage: {min_coverage:.0f}s")
    max_run_str = (f"max consecutive non-song: {max_run_non_song} "
                   f"(limit {cfg.playout.max_consecutive_non_song})")
    print(max_run_str)
    print(f"sfx: {len(stinger_starts)} stingers, {sum(len(v) for v in overlays.values())} "
          f"overlays; joke rimshot {jokes_aired}/{joke_chances} ({joke_rate:.2f})")
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    print("ALL SIM ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

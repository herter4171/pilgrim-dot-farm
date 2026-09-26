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

from pilgrim.config import RNG, SimClock, load_config, ensure_dirs  # noqa: E402
from pilgrim.store import Store  # noqa: E402
from pilgrim.scheduler import Scheduler  # noqa: E402
from pilgrim.selector import RandomSelector  # noqa: E402
from conftest import seed_pool  # noqa: E402


def run(hours=24):
    cfg = load_config()
    tmp = Path(tempfile.mkdtemp(prefix="radio_sim_"))
    cfg.library.dir = str(tmp / "library")
    cfg.library.db = str(tmp / "station.db")
    ensure_dirs(cfg)
    store = Store(tmp / "station.db")
    seed_pool(store, cfg, n_song=80, n_liner=50, n_com=30, n_dj=25)

    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(7), cfg), clock, RNG(7))
    seen = []
    last_seq = 0
    step = 30
    max_run_non_song = 0
    min_coverage = float("inf")
    for _ in range(int(hours * 3600) // step):
        clock.advance(step)
        sched.commit_lookahead()
        min_coverage = min(min_coverage, sched.coverage())
        for r in store.program_after(last_seq):
            seen.append((r["seq"], r["type"], r["duration_s"]))
            last_seq = max(last_seq, r["seq"])

    # ---- assertions ----
    fails = []
    seqs = [s for s, _, _ in seen]
    types = [t for _, t, _ in seen]
    if seqs != list(range(1, len(seqs) + 1)):
        fails.append("seq missing, duplicated, or out of order")
    if min_coverage < cfg.playout.committed_lookahead_s:
        fails.append("committed coverage below lookahead target")
    if sum(d for _, _, d in seen) < hours * 3600 + cfg.playout.committed_lookahead_s:
        fails.append("program does not cover the full simulation plus lookahead")
    if any(d <= 0 for _, _, d in seen):
        fails.append("non-positive duration")

    runn = 0
    for t in types:
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

    from collections import Counter
    mix = Counter(types)
    if not seen:
        fails.append("no program produced")

    print("=" * 60)
    print(f"24h simulated run: {len(seen)} committed items, {sum(d for _,_,d in seen):.0f}s audio")
    print("segment mix:", dict(mix))
    print(f"minimum committed coverage: {min_coverage:.0f}s")
    print(f"max consecutive non-song: {max_run_non_song} (limit {cfg.playout.max_consecutive_non_song})")
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    print("ALL SIM ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

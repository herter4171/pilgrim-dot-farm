"""Real-backend smoke probe (AGENTS §6 `make smoke`).

Explicitly NOT part of the unit suite: contacts LiteLLM/Kokoro/song
generators/searxng. Only run deliberately. Verifies /health on every host and
reports a summary.

`--gen` (only when asked; hard rule 6) also renders one 10 s instrumental per
song-pool slot, all submitted through the pool (staggered, so LiteLLM fans
them out), QC-grades each and prints wall time / RTF / generator.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pilgrim.config import load_config  # noqa: E402

_TIMEOUT = httpx.Timeout(8.0, connect=5.0)


def _ok(status: int) -> bool:
    return 200 <= status < 500


def _mcp_probe(endpoint: str, key: str) -> tuple[bool, str]:
    """Minimal MCP initialize probe: proves the endpoint + auth handshake."""
    try:
        # synchronous reimplementation of the handshake via httpx sync
        with httpx.Client(timeout=_TIMEOUT, headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **({"Authorization": f"Bearer {key}"} if key else {})}) as c:
            r = c.post(endpoint.rstrip("/"), json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "smoke", "version": "0"}}})
            return r.status_code == 200, f"http {r.status_code}"
    except Exception as e:  # noqa: BLE001
        return False, e.__class__.__name__


def probe() -> int:
    cfg = load_config()
    key = os.environ.get("LITELLM_TOKEN", "")
    failures = []
    with httpx.Client(timeout=_TIMEOUT) as c:
        checks = [
            ("litellm", cfg.hosts.litellm.rstrip("/") + "/models",
             {"Authorization": f"Bearer {key}"} if key else None),
            ("kokoro", cfg.hosts.kokoro.rstrip("/") + "/health", None),
            ("sfx", cfg.hosts.sfx.rstrip("/") + "/health", None),
        ]
        for name, url, headers in checks:
            try:
                r = c.get(url, headers=headers or {})
                ok = _ok(r.status_code)
                print(f"{name:10s} {url:45s} {'OK' if ok else 'FAIL'} ({r.status_code})")
                if not ok:
                    failures.append(f"{name} http {r.status_code}")
            except Exception as e:  # noqa: BLE001 - probe reports any reachability failure
                print(f"{name:10s} {url:45s} FAIL ({e.__class__.__name__})")
                failures.append(f"{name} {e.__class__.__name__}")
    import asyncio

    from pilgrim.pipelines.songs import song_backend_health

    async def _music() -> dict[str, bool]:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as ac:
            return await song_backend_health(cfg, ac, key)
    for name, ok in asyncio.run(_music()).items():
        print(f"{'music:' + name:10s} {'':45s} {'OK' if ok else 'FAIL'}")
        if not ok:
            failures.append(f"music backend {name} unreachable")
    # searxng goes through the LiteLLM MCP endpoint (not a /health route)
    ok, why = _mcp_probe(cfg.hosts.searxng, key)
    print(f"{'searxng':10s} {cfg.hosts.searxng:45s} {'OK' if ok else 'FAIL'} ({why})")
    if not ok:
        failures.append(f"searxng {why}")
    if failures:
        print("\nSMOKE FAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nSMOKE OK: all backends reachable.")
    return 0


GEN_SECONDS = 10.0  # hard rule 6: real smoke renders are <= 10 s


def gen_probe() -> int:
    """One 10 s instrumental per pool slot, concurrently through the pool."""
    import asyncio

    import numpy as np
    import soundfile as sf
    from pilgrim.audio.qc import grade_audio
    from pilgrim.pipelines.songs import SongPipeline

    cfg = load_config()
    cfg.songs.max_duration_s = GEN_SECONDS  # generate() sends this as duration_seconds
    key = os.environ.get("LITELLM_TOKEN", "")
    out = Path(cfg.library.dir if Path(cfg.library.dir).is_absolute()
               else Path(__file__).resolve().parents[1] / cfg.library.dir) / "_smoke_gen"
    out.mkdir(parents=True, exist_ok=True)
    sp = SongPipeline(cfg, llm=None, media_dir=out, api_key=key)  # type: ignore[arg-type]
    n = sp.pool.capacity()

    async def one(i: int) -> tuple[int, str]:
        brief = {"style_prompt": "mellow lo-fi piano, soft brushed drums",
                 "lyrics": "", "seed": 1000 + i}
        async with sp.pool.lease("slow") as b:
            gen = await sp.generate(brief, b)
        data, sr = sf.read(str(gen["path"]))
        x = np.asarray(data, dtype=np.float32)
        dur = len(x) / sr
        v = grade_audio(x, sr, duration_s=dur, min_dur=5.0, max_dur=GEN_SECONDS + 2,
                        kind="song", max_gap_s=2.0, silence_db=cfg.audio.silence_db)
        gen["path"].unlink(missing_ok=True)
        return i, (f"{'OK' if v.ok else 'QC FAIL ' + str(v.reasons)}  {dur:.1f}s audio "
                   f"in {gen['wall_s']:.1f}s (rtf {dur / gen['wall_s']:.2f}) "
                   f"via {gen['backend']} / {gen['generator']}")

    async def run() -> list:
        try:
            return await asyncio.gather(*(one(i) for i in range(n)),
                                        return_exceptions=True)
        finally:
            await sp.close()
    results = asyncio.run(run())
    bad = 0
    for r in results:
        if isinstance(r, BaseException):
            bad += 1
            print(f"gen FAIL ({r.__class__.__name__}: {r})")
        else:
            print(f"gen {r[0]}: {r[1]}")
            bad += "OK" not in r[1][:2]
    print(f"\nGEN {'OK' if not bad else 'FAILURES'}: {n - bad}/{n} slots rendered + QC passed")
    return 1 if bad else 0


if __name__ == "__main__":
    rc = probe()
    if "--gen" in sys.argv[1:]:
        rc = gen_probe() or rc
    raise SystemExit(rc)

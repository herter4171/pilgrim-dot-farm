"""Live MiniMax Music 3 song-generation probe (manual, real backend).

NOT part of the unit suite (deliberate real-backend smoke, cf. `make smoke`).
Sends a MiniMax Music 3 prompt crafted per the minimax-music3-prompting skill
(tagged lyrics + structured three-part caption) to the mlx-serve endpoint and
reports wall-clock time + output properties.

Run:  LITELLM_TOKEN=... .venv/bin/python -m pilgrim.tests.gen_song_test [outdir]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pilgrim.config import load_config  # noqa: E402

LYRICS = """[intro]
[verse]
Dawn breaks over the rows of green
Steam rising from the pressure canner
Jars lined up like little soldiers
Labeled for the coming winter

[chorus]
Put it in a jar, keep it on the shelf
Every summer's promise locked away
When the snow comes down we'll still be here
Singing on the pilgrim dot farm

[verse]
Tomatoes blushing on the vine
Basil sweet and bees all buzzing
Grandma's recipe, a family line
Salt and sugar, nothin' fussing

[chorus]
Put it in a jar, keep it on the shelf
Every summer's promise locked away
When the snow comes down we'll still be here
Singing on the pilgrim dot farm

[bridge]
And when the pantry's full of gold
We'll sit and tell the harvest story
No money, honey, never sold
Just the light, and the land's slow glory

[chorus]
Put it in a jar, keep it on the shelf
Every summer's promise locked away
When the snow comes down we'll still be here
Singing on the pilgrim dot farm

[outro]
Singing on the pilgrim dot farm"""

CAPTION = """Global Metadata
Basic Attributes: unhurried mid-tempo ballpark of 100 bpm. key is G, and scale is major. Bluegrass / Folk.
Global Emotional Progression: opens with a gentle, sunlit reverence for the morning farm; swells into warm, communal joy on each chorus; the bridge turns reflective and tender; resolves into a peaceful, contented outro that feels like closing the pantry door at dusk.
Application Scenarios & Imagery: an early summer morning on a small rural farm, canning season, jars steaming on the counter, the kitchen filled with light, a couple singing while they work.
Sonics & Production Profile: natural and lightly compressed, a wide-open acoustic arrangement with air between the instruments; warm woody low-mids, crisp acoustic highs, intimate close-miked vocals.

Vocal Details
Vocal Gender & Timbre: Singer A (Male). A warm, slightly reedy, friendly tenor with a genuine folk delivery and a subtle Appalachian drawl.
Vocal Style: soft, close-miked and conversational in the verses; opens up with easy, joyful projection in the choruses; the bridge drops to a hushed, tender reflection before building back; the outro is a gentle, relaxed hum-along.
Harmony/Backing Vocals: light close harmony, a secondary part joining on each chorus and the final outro, sung in thirds, giving a family-around-the-porch feel.
Vocal FX: restrained, natural room reverb across the whole track; a touch more warmth on the verses and a slight lift of presence in the choruses; no heavy delay or saturation.

Arrangement
Instrument Lifecycle (Primary/Secondary): Primary: acoustic guitar, fingerpicked from the intro through the outro, providing the rhythmic and harmonic anchor. Secondary: the banjo enters at the first chorus with a bright rolling part, exits during the bridge for an intimate guitar-and-voice moment, and returns doubled for the final chorus; a double bass holds the root-notes throughout and becomes more present during the bridge.
Groove & Foundation Progression: a relaxed, loping feel in the verses built on the guitar's bass notes; the choruses lift with a driving, energetic banjo-and-bass stomp; the bridge strips back to a sparser, almost ballad-like pulse; the outro settles into a calm, swaying closure.
Embellishments, Textures & Spatial FX: a warm room-reverb tail behind every section; a subtle mandolin fill at the end of each verse; soft finger-snaps and a light wood-block keeping time in the choruses; the final bar closes with a gentle harmonic climb and a quiet, ringing ending."""


def main() -> int:
    cfg = load_config()
    outdir = Path(sys.argv[1] if len(sys.argv) > 1 else "library/_gen_probe")
    outdir.mkdir(parents=True, exist_ok=True)
    url = cfg.hosts.mlx_serve.rstrip("/") + "/v1/audio/music-generations"
    payload = {"prompt": CAPTION, "lyrics": LYRICS}
    print(f"POST {url}")
    print(f"  lyrics tags: {[t for t in LYRICS.splitlines() if t.startswith('[')]}")
    print(f"  caption words: {len(CAPTION.split())}")
    t0 = time.monotonic()
    with httpx.Client(timeout=httpx.Timeout(1800.0, connect=10.0)) as c:
        r = c.post(url, json=payload)
    wall = time.monotonic() - t0
    print(f"HTTP {r.status_code} in {wall:.1f}s")
    if r.status_code != 200:
        print("BODY:", r.text[:800])
        return 1
    out = outdir / "probe.wav"
    out.write_bytes(r.content)
    data, sr = sf.read(str(out))
    x = np.asarray(data, dtype=np.float32)
    dur = float(len(x)) / sr
    peak = float(np.max(np.abs(x)))
    print(f"wrote {out} ({out.stat().st_size/1e6:.1f} MB)")
    print(f"  shape {x.shape}  sr {sr}  dur {dur:.1f}s  peak {peak:.2f}")
    print(f"  realtime factor: audio {dur:.1f}s / wall {wall:.1f}s = {dur/wall:.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

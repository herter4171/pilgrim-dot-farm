"""Air-time SFX policy (SFX.md §0, §7.2). Pure: a function of its inputs and
the injected RNG, so `make sim` stays reproducible (AGENTS rule 3).

A host clip's `meta.sfx` lists the overlays the producer rendered for it:
`{item_id, cue, kind, offset_s, duration_s}` with kind in
`stinger` (a topical hit), `joke` (the da-dum-tiss), `bed` (ambience under the
whole clip). At commit time this decides which of them actually air:

- never on a type outside SFX_HOST_TYPES (news is structurally excluded);
- a `joke` airs on a coin flip (`sfx.joke_p`);
- a stinger never runs past its host's end (it would push the join);
- at most `sfx.max_per_window` stingers start in any `sfx.window_s` of air.
  Beds are ambience, not stingers, and don't count.
"""
from __future__ import annotations

from pilgrim.config import RNG, Sfx
from pilgrim.pipelines.sfx import SFX_HOST_TYPES


def plan_overlays(host_type: str, host_start: float, host_dur: float,
                  cues: list[dict], recent_starts: list[float], rng: RNG,
                  cfg: Sfx) -> tuple[list[dict], list[float]]:
    """Return (overlays to air, absolute start times of the stingers added).

    `recent_starts` are absolute program times of stingers already committed;
    `host_start` is the host's absolute program start time."""
    if host_type not in SFX_HOST_TYPES or not cfg.enabled:
        return [], []
    out: list[dict] = []
    added: list[float] = []
    for c in sorted(cues, key=lambda c: float(c.get("offset_s", 0.0))):
        kind = c.get("kind", "stinger")
        off = float(c.get("offset_s", 0.0))
        dur = float(c.get("duration_s", 0.0))
        if dur <= 0 or off < 0 or off >= host_dur:
            continue
        if kind == "bed":
            out.append(_overlay(c, off, min(dur, host_dur - off), cfg.bed_gain, 1.0))
            continue
        if kind == "joke" and rng.random() >= cfg.joke_p:
            continue  # the dry half of the 50/50
        if dur > cfg.max_stinger_s or off + dur > host_dur:
            continue  # never push a join
        t = host_start + off
        window = [s for s in (*recent_starts, *added) if t - cfg.window_s < s <= t]
        if len(window) >= cfg.max_per_window:
            continue
        added.append(t)
        out.append(_overlay(c, off, dur, cfg.stinger_gain, cfg.host_duck))
    return out, added


def _overlay(c: dict, offset_s: float, duration_s: float, gain: float,
             duck: float) -> dict:
    return {"media_id": int(c["item_id"]), "cue": c.get("cue"),
            "kind": c.get("kind", "stinger"), "offset_s": round(offset_s, 3),
            "duration_s": round(duration_s, 3), "gain": gain, "duck": duck}

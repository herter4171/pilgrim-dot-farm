# Emergency Pack (spec — RADIO.md §8.3)

The fallback-of-last-resort content set. These items are flagged `emergency`:
**never deleted, never subject to repeat rules**, stored locally on the M5,
and only aired when nothing else is available (RADIO.md §5.4). Silence is
never an option.

## Contents (to be rendered at deploy time, not by the producers)
- **3–5 short station IDs / liners** (3–15 s) in Rosie's voice: "You're on
  Pilgrim Dot Farm." "Keep the dial where it is." "Weather in a minute —
  the farmhouse kind."
- **1–2 emergency songs** (any genre) generated once and cached.
- **1 emergency news-style filler** liners are enough; the emergency pack
  carries no news, which is never recycled and never repurposed.

## Rules
- Rendered once, shipped with the station deploy, never flagged for recycling.
- Airing an emergency item should be logged distinctly so operators can tell
  production recovered.
- If the emergency pack itself is exhausted, the station logs critical and
  plays the last-known-good repeat rather than silence.

> **Status:** canon defined here; rendering is a deploy-time step (M10) and
> is not implemented in code yet.

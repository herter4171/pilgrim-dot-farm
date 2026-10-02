# CGM — one sound per animal, code-enforced, no re-generation

> **2026-10-02.** Work plan for fixing SFX variety and guaranteeing that a
> mentioned farm animal is heard. Supersedes the earlier "multi-variant /
> palette expansion" ideas. RADIO.md §4/§5.2/§6.1/§11/§12/§15 stay the
> normative spec; this file is the plan + rationale.

---

## 0. The policy (settled with the operator)

- **No continuous SFX generation.** The palette is fixed. What is already
  rendered is what airs. A cue is rendered **once**, then recycled forever.
- **One clip per animal.** Every word that means the animal maps to that
  animal's single stock clip. All cow words — "cow", "heifer", "calf",
  "mooing" — map to the one moo. **It's one moo.** No fresh take per mention,
  no variants, no re-rolls.
- **Mentioned ⇒ heard, enforced in code.** If the DJ or field reporter says
  a farm animal, the matching stinger airs. That is a code guarantee, not a
  prompt hope (AGENTS hard rule 10: enforce in code, not just in prompts).
- SFX stay optional at air time (rule 1): the backstop decides at *production*
  time, from stock clips; playout never waits on anything.

## 1. Why variety was lacking (diagnosis, 2026-10-02, with evidence)

1. **The SFX model is unloaded while DeepSeek runs.** `:8000` refuses
   connections (exit 56); `server.log` 23:48–23:56 shows `sfx.backend_retry …
   ReadError` → `sfx.failed` for chicken/horse/cow/wind_bed. Per MODELS.md the
   MOSS-SoundEffect CUDA box and the DeepSeek dev model don't share VRAM,
   so a downed `:8000` while DeepSeek is busy is **expected, not a fault** —
   everything produced meanwhile airs **dry** (by design, rule 1). The retry
   storm while it's down is still a wart (see C3).
2. **Failed cues are never retried (bug).** `producer.sfx_step` pops
   `meta.sfx_pending` unconditionally after `_attach_sfx`, so a render that
   failed during an outage is dropped forever — the item stays dry for its
   whole life.
3. **Animal hits are prompt-only, and the LLM misses 44%.** Library audit:
   **9 of 16** dj_talk/field_report items mentioning a farm animal carried the
   matching animal cue; the other 7 carried generic stingers instead
   (e.g. a chicken item with `barn_door + rimshot + tractor`).
4. **Animal cues are configured as "contextual"** — a *fresh render per
   mention*. That is exactly the continuous generation the policy forbids,
   and it is why the stock pool holds 20 near-identical takes (cow×8,
   rooster×6, chicken×3, goat×3) while five animals (horse, pig, duck,
   turkey, donkey) have **zero** stock because their renders kept failing.
5. Evergreen stingers are single renders (fine, keep), and there is no
   anti-repeat spacing — the scheduler only enforces the 3-per-10 s budget.

## 2. What we will do

Ordered; each step is small and independently testable against fakes
(AGENTS rule 2).

### C1 — Stock fill (lazy; the only generation we will ever do)

- Flip the 10 animal cues in `config.yaml` from contextual to
  `evergreen: true` with fixed seeds.
- **Nothing is rendered now.** The MOSS model is deliberately unloaded while
  DeepSeek is busy (shared box, MODELS.md). The existing `sfx_loop` already
  tops up one missing stock cue per idle step, so the five absent animals
  (horse, pig, duck, turkey, donkey) fill themselves in ~2 min of backend
  uptime once MOSS is loaded again — no scheduled job, no operator step.
- The ~20 surplus contextual animal takes from the old per-mention era are
  left **in place, untouched**: the picker always takes the first matching
  stock item, so each cue effectively airs as one fixed clip already. No DB
  writes, no retirements.
- After the fill, animal SFX are **never rendered again**.
- Done when: `store.list_items("sfx")` has at least one stock item for every
  cue (27 cues); from then on the pool is static.

### C2 — Word→cue map (the core of this plan)

- New pure table in `pilgrim/sfx_plan.py` (no `random`/`time`, sim-stable):

  ```python
  ANIMAL_WORDS: dict[str, tuple[str, ...]] = {
      "chicken": ("chicken", "chickens", "hen", "hens", "chick", "chicks",
                  "cluck", "clucking", "fowl"),
      "cow":     ("cow", "cows", "heifer", "heifers", "calf", "calves",
                  "moo", "mooing"),
      "horse":   ("horse", "horses", "colt", "colts", "foal", "foals",
                  "stallion", "mare", "nag"),
      "pig":     ("pig", "pigs", "piglet", "piglets", "hog", "hogs",
                  "swine", "porker"),
      "rooster": ("rooster", "roosters", "cock-a-doodle"),
      "sheep":   ("sheep", "lamb", "lambs"),
      "goat":    ("goat", "goats", "billy"),
      "duck":    ("duck", "ducks", "drake", "drakes", "gander",
                  "quack", "quacking"),
      "turkey":  ("turkey", "turkeys", "gobble", "gobbling"),
      "donkey":  ("donkey", "donkeys", "mule", "mules", "burro"),
  }
  ```

- Pure `animal_mentions(text) -> list[{cue, after_sentence}]`: word-boundary
  scan over the same sentence split the cue parser uses; first mention of each
  animal wins; multiple mentions of the same animal in one clip = **one**
  stinger (sparse reads funnier — SFX.md §0).
- **Backstop in `producer._attach_sfx`** (post-render, where text + duration
  are final): for every mentioned animal with no cue of its own, add
  `{cue, after_sentence}`; animals outrank non-animal cues at the 3-cue cap
  (animals win the slot). Log `sfx.animal_backstop` when it fires.
- The LLM keeps requesting cues as today; the map is a **floor**, never a
  replacement. Prompts are not touched.
- Done when: unit tests on fakes show 100% of scripts mentioning an animal
  get that animal's cue attached, for every alias in the table.

### C3 — Retry fix + health gate (reliability)

- `sfx_step` clears `sfx_pending` only when **all** wanted cues resolved;
  otherwise bump `meta.sfx_attempts` (cap 5) and leave it pending for the
  next loop. Pending items carrying animal cues go to the front of the
  queue. After the cap, the item airs dry and the miss is logged.
- **Health gate:** before any render attempt the worker probes
  `GET /health` (short timeout, result cached ~60 s). Down ⇒ skip renders
  entirely, log once, retry next cycle. No retry storms against a
  deliberately unloaded model; no wasted 180 s timeouts.
- A backend blip can delay a stinger, never delete it.
- Done when: fake-backend tests — backend down ⇒ item stays pending and
  **zero** generate calls are made; backend back ⇒ overlay resolves.

### C4 — Anti-repeat (variety without new bytes)

- New config knob `sfx.cue_repeat_gap_s` (default 180): `plan_overlays`
  (pure, sim-stable) skips any cue that aired inside the window. The
  scheduler passes its rolling recent-cue list alongside the existing
  `_stinger_starts` budget list.
- Done when: sim assertion — the same cue never airs twice within the gap.

### C5 — Ops: nothing to do now

- The `:8000` model is unloaded while DeepSeek is busy — that is expected.
  No restart, no render scheduled. When DeepSeek is done and MOSS is loaded
  again: verify `GET /health` (shapes in `docs/backends.md` §7), and the
  lazy C1 fill + C3 pending retries pick up automatically.

## 3. What we will NOT do

- **No multi-variant stock, no fresh render per mention, no re-rolls.**
- **No palette expansion, no audition tool, no new beds.** The palette is the
  current 27 cues (17 silly/prop evergreens + 10 animals), one clip each.
- **No prompt rework for cue choice** — the code map decides.
- **No new DB schema, no HTTP API change** (no §11/§12 changes beyond the
  config knobs noted in C4; noted here per AGENTS rule 8).

## 4. Verification (AGENTS §7 order)

1. `make lint` — ruff + mypy clean.
2. `make test` — new unit tests: alias table (every word maps), backstop
   precedence at the 3-cue cap, pending-retry lifecycle, gap logic.
3. `make sim` — new assertions: (a) 100% animal-mention hit rate with a
   healthy fake backend; (b) no same-cue air within `cue_repeat_gap_s`;
   (c) existing asserts hold (≤3 stingers/10 s, no SFX on news, gapless with
   missing stingers).
4. `tools/sfx_audit.py` (new, small): reports animal-mention hit rate and
   per-cue usage spread from a seeded temp library; output lands in the
   milestone report.

## 5. Open questions (need the operator)

1. **C5:** when DeepSeek is done, what is the procedure for loading MOSS
   back onto the box — manual, or automatic? (The plan only assumes someone
   does it; nothing here blocks on it.)
2. **C2 alias table:** anything to add or cut (is "billy" goat? should "beef"
   count as cow?)?
3. **C4 gap:** 180 s sensible, tighter or looser?

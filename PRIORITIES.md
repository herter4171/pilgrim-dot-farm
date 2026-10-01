# PRIORITIES.md — prioritizing fresher songs

Operator, 2026-10-01: "It always starts with the first song it generated. I
don't like how new songs are not prioritized."

This is the plan only. **No code changes have been made yet.** Work it top to
bottom. Each step updates its own RADIO.md sections (§5.2, §7, §12, §15) in the
same change, because steps 2 and 4 change the database and config schemas
(AGENTS.md hard rule 8).

---

## 1. What is actually happening

### Evidence

- **Live `station.db`:** 41 songs, all already aired at least once
  (`fresh=0`). The airplay ledger shows only **12 distinct songs** ever heard.
  The most-played song is **#84, "Cornfield Polka for the County Fair"**: the
  first song generated (2026-09-30 22:44), with 32 plays.
- **Simulation** (41 recycled songs, real `Scheduler` + `RandomSelector`,
  6 h, restarted 3×): every run **opens with song #1**, and only songs
  **#1–#18 ever air**. The newest 23 songs never play.

### Root causes (all in `pilgrim/scheduler.py`)

1. **The "oldest air" sort is inverted for unplayed songs.** `_pick_song`
   sorts recycled songs by
   `self._last_song_air.get(id, float("inf"))` and takes the **min**. A song
   not yet played this run scores `inf`, so it ranks *last*, not first. Once
   the first hour's songs clear the spacing floor, their finite timestamps
   always beat the `inf` of every song that hasn't aired. The rotation locks
   onto whichever ~18 songs aired first and never reaches the rest.
2. **Airplay history is memory-only.** `_last_song_air` is an in-process
   dict, and `clear_program()` wipes the program table on start. After a
   restart every song ties, and the final tiebreaker is `it["id"]`, so the
   **lowest id (the oldest song) airs first, every time**.
3. **Song age is ignored entirely.** Once a song has aired once, a song made
   ten minutes ago and one made yesterday are treated the same. Nothing
   favors recent work.
4. **Fresh songs are FIFO.** When more than one unaired song is waiting
   (`fresh_songs_ready: 2`, or a burst after a drought), the tiebreak is
   `id` ascending, so the *older* fresh song goes first.
5. **The spacing settings don't match.** RADIO.md §5.2 says 4 h → 2 h → 1 h.
   `config.yaml` says `[3600, 1800, 900]` ("hard floor 1h per operator"). The
   code uses `[0]` = 1 h as a preference and `min` = **15 min** as the real
   floor (`_eligible_songs`). See open question Q1.

What already works and must stay: a brand-new song airs at the next song slot
(drought rule, memory `new-songs-air-immediately`), and listener requests
jump the line.

---

## 2. Target behavior

Song selection, in priority order:

| Tier | What | Rule |
|------|------|------|
| 0 | Ready listener-request song | Unchanged: jumps the line (OVERHAUL 4.7). |
| 1 | Fresh (never aired) song | **Newest first** (LIFO). Airs at the next song slot; the drought rule is unchanged. |
| 2 | Recycled song | **Weighted random** over songs that clear spacing. Weight favors young songs and songs not heard for a long time (§3). |

Observable outcomes:

- After a restart, the first song is drawn from the weighted pool, which
  leans toward recent songs, never "lowest id".
- Songs made in the last few hours air noticeably more often than old ones.
- Every song in the pool still airs eventually (no starvation). Old songs
  thin out; they don't disappear.

---

## 3. Recycled-song weighting (tier 2)

For each eligible recycled song (clears spacing, not emergency, not retired):

```
age_h    = (now_wall - created_at) / 3600
since_h  = (now_wall - last_aired_at) / 3600        # persisted, see §4
youth    = max(youth_floor, 0.5 ** (age_h / half_life_h))
wait     = min(1.0, since_h / wait_target_h)        # 0..1, grows while unheard
weight   = youth * (wait_floor + (1 - wait_floor) * wait)
```

Pick with `rng.weighted_choice` (the injected RNG, so this stays pure and
seedable; hard rule 3).

Proposed defaults (new `playout.song_rotation` block in config, §12):

| Key | Default | Meaning |
|-----|---------|---------|
| `half_life_h` | 12 | A song's youth boost halves every 12 h. |
| `youth_floor` | 0.1 | Old songs keep ≥10 % of a new song's pull, so nothing starves. |
| `wait_target_h` | 3 | After 3 h unheard, a song's wait factor maxes out. |
| `wait_floor` | 0.2 | A song just past spacing still has some chance. |

Rough effect: a 1 h-old song outweighs a day-old song about 3.8× (≈0.94 vs.
0.25) and a 2-day-old song about 9× (vs. the 0.1 floor).

Kept as-is:
- **Genre:** filter to songs whose genre isn't in the last 3 (if any remain)
  *before* weighting, the same soft rule as today.
- **Spacing:** the floor/relaxation steps (Q1 decides the numbers). Weighting
  only orders songs that already pass spacing; it never breaks it.
- **Drought rule and request line:** untouched.

Rejected alternative: a strict "newest eligible first" sort. It's
deterministic and simple, but it would play the newest ~5 songs on a tight
loop at exactly the spacing floor. Weighted random gives the same lean
without the loop.

---

## 4. Persist airplay so restarts don't reset rotation

Schema change (RADIO.md §7 + §14 data model): two columns on `items`, added by
the same idempotent `ALTER TABLE` migration pattern already in `store.py`:

```sql
ALTER TABLE items ADD COLUMN last_aired_at REAL;     -- wall-clock epoch, set at commit
ALTER TABLE items ADD COLUMN play_count INTEGER DEFAULT 0;
```

- Set both in `Scheduler._append` for `song` entries, using `clock.wall()` (not
  `time.time()`; hard rule 3).
- Spacing and weighting read `last_aired_at` (wall clock) instead of the
  in-memory `_last_song_air` air-clock. The station is always on air, so wall
  time and program time advance together; the only difference is the ≤10 min
  of committed lookahead, which is within spacing tolerance. Drop
  `_last_song_air` for songs (commercials can follow later the same way).
- **One-time backfill:** set `last_aired_at` from
  `MAX(airplay.recorded_at)` per item and `play_count` from the airplay rows,
  so the current pool starts with real history rather than all-NULL.
- `NULL` `last_aired_at` (never aired, or aired before the ledger existed)
  counts as "unheard forever" → `wait = 1.0`. This is the fix for root cause 1.

`play_count` isn't used by the formula above. It's there for the stats
endpoint / tuning and for Q3.

---

## 5. Implementation steps

1. **Bug fix (small, ship first):** in `_pick_song`, treat a missing
   last-air as *longest ago* (`-inf`), not `inf`, and break ties by **newest
   id first**. Make fresh songs LIFO. This alone fixes "always starts with the
   first song" and the 18-song lockup, with no schema change.
2. **Persistence:** §4 columns, migration, backfill, scheduler reads/writes
   `last_aired_at`.
3. **Weighting:** §3 formula behind `playout.song_rotation` (config.py
   pydantic model + config.yaml + RADIO.md §12).
4. **Docs:** RADIO.md §5.2 song-selection bullet rewritten to the tier
   table; fix the 4 h/1 h spacing text once Q1 is answered.

---

## 6. Tests

Unit (`pilgrim/tests/test_scheduler.py`, fakes only):
- **Restart regression:** pool of N recycled songs with no history → the
  first pick is not the lowest id; across seeds the first pick favors the
  newest third.
- **No lockup regression:** 41 recycled songs, 6 h → every song airs at
  least once (today: 18/41).
- **Fresh LIFO:** two fresh songs → the newer airs first; both air before
  any recycled song.
- **Persistence:** commit a song, build a new `Scheduler` on the same db →
  the song is still inside its spacing window.
- **Spacing never broken by weighting:** a heavily weighted young song inside
  its floor is not picked.
- Keep `test_new_song_airs_promptly_after_a_drought` and
  `test_thin_liner_pool_does_not_pad_the_lookahead` passing unchanged.

Simulation (`make sim`, new §15 assertions):
- With songs added at the production rate over 24 h, songs < 12 h old get
  more than their pool share of airtime.
- No eligible song goes unaired longer than `max(6 h, pool_size × mean gap)`
  (starvation guard).
- Existing assertions (spacing, genre, max 2 non-song, drought) still pass.

---

## 7. Open questions (need an operator decision)

- **Q1. Song spacing:** RADIO.md says 4 h → 2 h → 1 h; config says
  `[3600, 1800, 900]` with a comment "hard floor 1h per operator"; the code's
  real floor is 15 min. Which is the rule: a 1 h hard floor, or 15 min as the
  last relaxation step?
- **Q2. Half-life:** is 12 h the right "new" window? Shorter (6 h) makes the
  last few songs dominate; longer (24 h) is gentler.
- **Q3. Old songs:** should very old / heavily played songs eventually stop
  rotating (e.g. retire after N plays or past `storage.soft_cap_gb`), or just
  sit at the `youth_floor`? Retiring is a flag only; library files are never
  deleted (hard rule 5).

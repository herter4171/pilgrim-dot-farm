# FIX_RADIO.md — Option A: request priority without starving the song rotation

Operator decision (2026-10-31): take **Option A** — stop blocking stock songs
while a listener request is pending. The splice already makes a ready request
air at the next slot; the window-hold adds nothing to that and costs 3–18 min
of voice-only air per request.

Work it top to bottom. Steps 1–6 land as **one change** (hard rule 8: the
config-schema change ships with the RADIO.md update). Step 7 lands with the
same change (tests). Steps 8–9 are a separate follow-up commit.

---

## 0. Evidence (2026-10-31)

- 18:20:52 request 483 ("A squirrel took my keys") approved → `request.producing`.
- 18:26:16 → 18:31:22: **5 min 06 s of pure voice** (7 dj_talk, ~14 liners,
  2 news, 2 commercials, zero songs) while the M5 render ran (175.8 s song,
  427 s wall). Request aired 18:31:22.
- Every request today caused the same pattern: 9 song-free gaps with a client
  connected, **~69 min of ~402 min client-connected air (17%)** voice-only.
  Baseline song share with no pending request: **85–100%** (e.g. 01:57–03:42: 97%).
- Real production times observed: 10–26 min (qwen38 brief ~3–170 s + mlx render
  208–427 s + QC/intro). The hold cap (`request_hold_max_s`) is 900 s — the
  design was validated on fakes where production takes seconds.

## 1. Root cause

`Scheduler.commit_lookahead()` (`pilgrim/scheduler.py`): while
`_request_hold()` is true (any request queued/producing/ready-and-uncommitted,
capped per request at `playout.request_hold_max_s` = 900 s),

1. the fill target drops from `committed_lookahead_s` (300 s) to
   `filler_horizon_s` (60 s), and
2. **stock songs are blocked** (`st.available["song"] = False`), with a
   song fallback only when coverage < 60 s (dead air).

That is the voice diet. The hold is **redundant**: `place_request_next()`
splices a ready request at the *first future item* (right after what is
playing), inserts the liner/callout bridge the no-song-after-song and
words-between-songs rules require, renumbers future seqs with fresh high-water
seqs, and bumps the programme revision; revision-aware clients cancel obsolete
future sources and refetch the snapshot. A committed stock song ahead of the
splice point does **not** delay the request — it is pushed back by the
renumber. The hold's only effect is what the stock songs would have filled.

## 2. Target behavior

| When | Committed window | Request priority |
|------|------------------|------------------|
| Request queued / producing | Fills normally to `committed_lookahead_s` **including stock songs** (normal rotation, spacing, weighting — unchanged) | — |
| Request ready (song + intro rendered) | Splice at next song slot, **ahead of committed stock songs**, intro glued in front, liner bridge where required, revision bump | Unchanged — same as today |
| Request stuck (backend down) | Fills normally; nothing to release | The 900 s cap and its release path disappear with the hold |

Listener-facing delta: while a request renders, the station sounds like the
station (music rotation) instead of a voice diet. The request still cuts in at
the next boundary the moment it is rendered. Nothing else changes.

## 3. Steps

### Step 1 — `pilgrim/scheduler.py`: remove the hold from `commit_lookahead()`

- Delete `request_waiting = self._request_hold()` and the whole
  `if request_waiting:` block inside the fill loop (the
  `st.available["song"] = False` branch and the `coverage() >= filler_horizon_s`
  break). The loop becomes the plain fill-until-lookahead loop.
- Keep `self._splice_ready_requests()` as the first step of
  `commit_lookahead()` (ready requests still splice before the window fills).
- Rewrite the `commit_lookahead()` docstring: no more "held at
  filler_horizon_s"; state that ready requests splice into the next slot and
  pending requests do not affect window depth (§5.2).
- `filler_horizon_s` stays in use via `_song_drought()` and `_may_repeat()` —
  do not touch those.

### Step 2 — `pilgrim/scheduler.py`: delete the hold state

- Delete `_request_hold()` (the per-request clock + `request_hold_max_s` cap).
- Delete the `_hold_request_id` / `_hold_since` fields and the comment block
  above them ("While a listener request is still waiting for its song slot…").
- Nothing else references the hold (verified: only `commit_lookahead`).

### Step 3 — `pilgrim/store.py`: docstring only

- `pending_requests()`: drop the sentence "The scheduler holds the committed
  window short while one of these is uncommitted…" — the method is still used
  for queue order, `next_request_to_produce()`, and the request board.

### Step 4 — `pilgrim/config.py`: remove the knob

- Delete `request_hold_max_s: int = 900` from the playout model (line ~149).

### Step 5 — `pilgrim/config.yaml`: remove the key

- Delete the `request_hold_max_s: 900` line. Keep `filler_horizon_s` and
  `committed_lookahead_s`. (Deploy templates do not carry this key — verified
  in `pilgrim/deploy/`.)

### Step 6 — `RADIO.md` (same change, hard rule 8)

- **§5.2** (song-priority / request-splice paragraph, ~line 265–273): replace
  the hold sentence. New wording, substance: "A ready request is spliced into
  the next song slot **ahead of already-committed stock songs** — the splice
  renumbers future occurrences with fresh seqs and bumps the programme
  revision, and revision-aware clients cancel their obsolete future sources
  and refetch. While a request is still queued/producing the committed window
  fills normally (stock songs included); the splice — not a window hold —
  gives the request its next-slot priority."
- **§5.3** (~line 282): delete the clause "the window is held at
  `filler_horizon_s` while a request renders"; keep "a ready request is
  spliced into the next song slot with a revision bump".
- **§12** (~line 865): remove the `request_hold_max_s` config line. Note in
  the report that this is a config-schema change per hard rule 8.

### Step 7 — `pilgrim/tests/test_scheduler.py`

- **Rewrite** `test_pending_request_holds_window_and_takes_next_slot` →
  `test_pending_request_keeps_rotation_and_takes_next_slot`:
  - with a queued request, `commit_lookahead()` keeps
    `coverage() >= committed_lookahead_s` **and** newly committed rows include
    stock songs (assert `"song" in new_types`);
  - once `mark_request_ready`, the request is the first future song, its intro
    immediately before it, revision bumped;
  - request marked `aired` only when the playhead reaches its song.
- **Rewrite** `test_request_hold_releases_after_limit` →
  `test_stuck_request_keeps_full_lookahead`: a request stuck in `queued`
  (never ready) leaves the window at full lookahead at every cycle — the old
  "release after the cap" behavior is simply the normal behavior now.
- **Keep** `test_ready_request_splices_ahead_of_committed_songs` unchanged —
  it asserts the crux (splice ahead of committed stock, intro glued, revision
  bump, unique ordered seqs, airs at the next boundary).
- **Add** `test_splice_preserves_words_between_songs`: ready request spliced
  directly before a committed stock song gets a liner/callout after it (or,
  with no liner available, lands after the next non-song) — no song-after-song
  and every song pair keeps a voice item.
- **Check** `tests/sim_run.py` + `tests/test_integration_sim.py`: assertions
  (requests air in submission order, each with its intro) must pass unchanged;
  requests will now air *earlier* — update only if a hard-coded latency bound
  assumed the diet (none known; verify).
- **Check** e2e (`tests/e2e/gap_test.spec.ts` + `/api/test/splice`): the splice
  path is unchanged; the gap test should pass without edits.

### Step 8 — verify (AGENTS.md §7, in order)

1. `make lint`
2. `make test`
3. `make sim` (24-h simulation, §15 assertions)
4. `make e2e`
- No deploy/split/payment surface touched, so `deploy-check` / `split-smoke` /
  `e2e-split` / `pay-smoke` are not required for this change.

### Step 9 — optional hardening (separate commit, after 3–8 are green)

a. **Fix stale `last_aired_at` on pushed-back stock songs.**
   `commit_song_air()` persists the *scheduled* air time and `play_count` at
   commit; a splice that renumbers a committed stock song to a later slot
   leaves the persisted time a few minutes early, so that song becomes
   re-eligible / weighted slightly earlier than it should. Add
   `Store.reschedule_song_air(item_id, wall)` (UPDATE `last_aired_at` only,
   **no** `play_count` increment) and call it from the splice for every
   shifted future `song` row. Unit test: pushed-back song's
   `last_aired_at` equals its new slot wall; `play_count` unchanged.

b. **FIFO splice guard.** If a committed pending request song is still in the
   future when another ready request splices, splice **after** the first one's
   block instead of at the first future item (narrow inversion window: playhead
   stall longer than the next request's production time while the first request
   is committed-but-unheard). Unit test: two ready requests splice in queue
   order, first stays first.

## 4. Known deltas / risks

- **Client cutovers become more frequent.** Splices now routinely cut in front
  of committed stock songs the client already fetched. Same mechanism as today
  (revision bump → cancel future sources → refetch snapshot, TUI.md §5/§9.2,
  verified in the 2026-10-28 work); no API change.
- **Pushed-back stock carries a stale `commit_song_air`** until step 9a lands:
  bounded skew of a few minutes (a splice inserts ~3–4 min of audio).
  Acceptable; step 9a removes it.
- **FIFO inversion window** (step 9b): theoretical today, stays theoretical
  unless the guard lands — production is strictly sequential, so two requests
  are never ready-uncommitted simultaneously in normal operation.
- **Out of scope** (from the incident review, tracked separately): identical-
  text request dedupe (482/483 "A squirrel took my keys"), the 00:49:05 server
  hard death (no traceback, no OOM in journal), and qwen38 empty-content retry
  tuning (170 s brief, 3× empty moderation calls that rejected a harmless request).

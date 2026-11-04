# IDLE.md — The farm sleeps when nobody is listening

Work plan for a coding agent. When the station has **no active stream**
(no player heartbeats for a grace period), playout **freezes** with a full
committed window already rendered, and production **pauses** (except listener
requests). The first `GET /api/station/program` (a PLAY click) or heartbeat
**wakes** it: the frozen window resumes from where it stopped, stale
time-bound items are dropped, and the producers pick up again. No client
change is needed.

Read this whole file first. Then read, in order: `AGENTS.md`, `RADIO.md`
§3, §5.2, §5.3, §6.1, §9.2, §11, §12, §14, §15. `RADIO.md` is the source of
truth; if this file and RADIO.md disagree, stop and report.

Do the steps in order. Each step ends with the AGENTS.md §7 checks, a commit
citing the RADIO/IDLE sections, and the AGENTS.md §8 report. All code is
written on the Mac checkout (ALBUM_ART.md "Where the work happens"); the edge
needs no deploy for this plan (no web or nginx change).

---

## 0. Read this first

### 0.1 Facts observed in the code (2026-11-03)

- **The playhead runs on the origin clock, listeners or not.**
  `Scheduler.position()` = `clock.now() - _offset` (`pilgrim/scheduler.py`
  ~line 140). `Scheduler.run()` calls `commit_lookahead()` every
  `REFILL_INTERVAL_S` (2 s) and keeps `playout.committed_lookahead_s`
  (300 s) of audio committed ahead of that playhead. Committing **consumes**
  one-shot inventory (`mark_aired` at commit for liners, DJ talk, news, field
  reports, promos, time checks; `commit_song_air` for songs). So with nobody
  listening the station burns through DJ talk, news and fresh songs, and the
  producers refill them: LLM, Kokoro and MiniMax calls around the clock for
  no one.
- **Live listeners** are an in-memory map in `Station` (`pilgrim/server.py`
  `touch_listener` / `active_listeners`), fed only by
  `POST /api/station/heartbeat` `player_id`, pruned after
  `visitors.listener_timeout_s` (15 s). The browser sends a heartbeat every
  5 s **only while playing** (`player.js` `hbTimer`). An open page that is
  not playing sends none.
- **What a non-playing page polls**, none of which may wake the station:
  `GET /api/health` every 8 s (`player.js`), `GET /api/station/state` every
  15 s (`visitors.js`, listener count), `GET /api/requests` every 6 s
  (`requests.js`). While playing the page also polls `/api/station/state`
  every 2 s and `/api/station/program` as its decode window drains.
- **PLAY** = `fetchProgram(null)` → `GET /api/station/program` with no
  cursor → full snapshot from the on-air item with `start_offset_s`. Then
  heartbeats. So the program route is the wake signal, and the response to
  the *waking* request must already be playable.
- **Producers** (`pilgrim/producer.py`): `run()` (voice refill every 6 s),
  `news_loop()` (5 s), `song_loop()` (decider: queued requests first, then
  stock when fresh < target), `sfx_loop()`. `song_loop` already has an
  injectable `_sleep`. `Producer.counts()` is THE inventory count.
- **Other background tasks** in `Station.startup()`: `phrases.run()`,
  `publisher.run()` (S3), `art_worker.run()` (FLUX). These are cheap or
  self-limiting and stay running.
- **Time-bound committed items.** `time_check` is rendered per 5-minute
  wall bucket (`meta.bucket`) and chosen for the slot's wall time at commit
  (§5.3). `news`, `promo` (campaign), `dj_talk`, `field_report` carry
  `expires_at` and are filtered against the slot's wall time at commit
  (`_slot_wall`). After a long freeze these commitments are wrong for the new
  wall time.
- **Programme identity:** `epoch` (restart) and `revision` (future
  changed). Any client that connects after a wake fetches a full snapshot
  anyway (no cursor), so a wake may edit the future freely as long as it
  bumps the revision.
- **The simulation** (`pilgrim/tests/sim_run.py`) and the e2e server
  (`pilgrim/tests/e2e/e2e_server.py`) build `Scheduler` directly with no
  `Station`, and drive `commit_lookahead()` themselves. They must keep
  working unchanged: idle **policy** lives in `Station`, idle **mechanics**
  live in `Scheduler` behind explicit calls.
- Hard rule 3: no `time.time()` / `asyncio.sleep` for scheduling in
  `scheduler.py` / `producer.py`. Use the injected `Clock` (`clock.now()`
  monotonic, `clock.wall()` epoch) and the existing `_sleep` hook.

### 0.2 Decisions (defaults chosen; the operator may override)

| # | Decision | Why |
|---|----------|-----|
| D1 | Idle = `active_listeners() == 0` for `idle.after_s` (default **120 s**) | A reload or a short network blip must not freeze the station; 120 s is eight missed heartbeats past the 15 s timeout |
| D2 | Wake = `GET /api/station/program` or `POST /api/station/heartbeat` (and any `/api/admin/dj/*` mutation). **Never** `/api/health`, `/api/station/state`, `/api/requests`, `/api/visitors`, media | Those are polled by idle pages, bots and smoke checks |
| D3 | While idle the **playhead freezes** and the committed window stays as it is (≤ 300 s, already rendered: "a song queued up") | Instant resume; nothing to render on wake |
| D4 | On wake, future rows whose `expires_at` has passed for their new slot, and every future `time_check`, are **dropped**; a song-after-song gap that this opens is bridged with `_pick_callout_fallback()`; the revision bumps | No stale news or wrong clock read; songs/liners/commercials are evergreen and keep |
| D5 | Dropped one-shots (news, DJ talk, promos, time checks) are **not** returned to the pool | Same as cancelled cutovers, RADIO §5.5 |
| D6 | Producers pause in idle: voice refill, news, SFX pool, **stock** songs. **Listener requests (free and paid) still produce** in idle | A requester is waiting on it; a paid one has paid. Stock/talk only serves airtime nobody hears |
| D7 | S3 publisher, art worker, phrase manager keep running | Self-limiting; art backfill in quiet hours is a feature |
| D8 | Ready requests still **splice** into the frozen window | They take the next slot the moment someone wakes it |
| D9 | `idle.enabled: true` by default; `false` restores today's behaviour exactly | Rollback switch |
| D10 | Idle state is visible: admin `/api/health` `idle: {idle, since, wakes}`, public `/api/station/state` `idle: bool`, log events `station.idle` / `station.wake` | Operator can see it; TUI can show it. `on_air` stays `true` while frozen (coverage > 0), so PLAY is never disabled by idle |

### 0.3 Rules

1. Idle must never produce dead air. A wake returns a playable program in
   the same request; `wake()` is synchronous and only touches already
   rendered inventory (hard rule 1).
2. `/api/health`, `/api/station/state`, `/api/requests`, `/api/visitors`,
   `/api/media/*` never change idle state (D2). Add a test for each.
3. The sim and e2e harnesses never freeze (they call no `freeze()`).
4. No new dependency. No schema change. No HTTP shape removed; only fields
   added (update RADIO §11, §12 in the same commit, AGENTS rule 8).

---

## Step 1 — Config (`pilgrim/config.py`, `pilgrim/config.yaml`; RADIO §12)

New top-level section, pydantic model `Idle`, field `idle: Idle` on `Config`:

```yaml
idle:                      # IDLE.md: freeze playout + pause production with no listeners
  enabled: true
  after_s: 120             # no heartbeat for this long -> idle (>= visitors.listener_timeout_s)
  check_s: 5               # how often the station checks (wake is immediate, on request)
  producer_poll_s: 30      # paused producer loops look again this often
```

Validator: `after_s >= visitors.listener_timeout_s` (otherwise a healthy
listener could be declared absent before its map entry expires). Document
the section in RADIO §12 and its line in the `config.yaml` sketch.

**Tests** (new `pilgrim/tests/test_config.py`; there is no config test
module today — `test_trusted_proxies.py` covers only `Visitors`):
defaults load; `after_s < listener_timeout_s` rejected.

---

## Step 2 — Scheduler mechanics (`pilgrim/scheduler.py`; RADIO §5.3)

Add to `Scheduler`:

```python
self._frozen_at: float | None = None   # clock.now() when the playhead froze
self.idle_wakes = 0

def position(self) -> float:
    now = self._frozen_at if self._frozen_at is not None else self.clock.now()
    return min(now - self._offset, self._total)

@property
def frozen(self) -> bool: ...

def freeze(self) -> None:
    """Stop the playhead where it is (IDLE.md D3). Idempotent."""

def thaw(self) -> int:
    """Resume the playhead (shift _offset by the frozen span), revalidate the
    committed future for the new wall time (D4), bump the revision if
    anything changed, top the window up, and return the number of rows
    dropped. Idempotent when not frozen (returns 0, no revision bump)."""
```

`thaw()` revalidation, in order:

1. `self._offset += self.clock.now() - self._frozen_at; self._frozen_at = None`.
2. Walk `self._items` **after** the on-air item (index from `on_air()`),
   computing each row's new slot wall via `_slot_wall(self._cum[i])`.
   Mark for drop: `type == "time_check"`, or the item's `expires_at`
   (from `store.get_item`) `< slot_wall`.
3. Delete the dropped rows (`store.delete_program_rows(seqs)`, new store
   method; **never** reuse a seq), remove them from `_sfx_by_seq` and from
   `_pending_aired` (a dropped request song cannot happen: songs are never
   dropped; assert).
4. `_rebuild_program()`. Then walk the future again: where a `song` row
   directly follows a `song` row (ignoring `intro`), insert
   `_pick_callout_fallback()` entries with fresh seqs (same splice technique
   as `place_request_next`: fresh seqs above `max_seq`, renumber the tail,
   `set_program_seq_high`). If no callout exists at all, leave it (§5.2:
   "never waits: with no voice item at all, the song airs").
5. If anything was dropped or inserted: `advance_revision()` and log
   `program.thaw_revalidated {dropped, bridged, frozen_s, revision}`.
6. `commit_lookahead()` (sync; only rendered inventory) so the waking
   request sees a full window.

`commit_lookahead()` while frozen: run `_splice_ready_requests()` (D8) and
return **before** the fill loop and `_trim()` (position is frozen, so
coverage and trim are static anyway; skipping the fill avoids consuming
inventory into a window nobody hears). `run()` is unchanged.

`_slot_wall` needs no change: while frozen, `clock.wall()` advances and
`position()` does not, so the tail's slot wall is "now + coverage", which is
exactly when it would air if woken now.

**Store** (`pilgrim/store.py`): `delete_program_rows(seqs: list[int]) -> int`.
No schema change.

**Tests** (`pilgrim/tests/test_scheduler.py`, SimClock, temp DB, fakes):

- `freeze()` holds `position()` and `coverage()` while the clock advances
  1 h; `on_air()` is unchanged; `thaw()` resumes from the same offset into
  the same item (no jump).
- Frozen window with a `time_check` and an expiring `news` row in the
  future: after a 2 h freeze, `thaw()` drops both, keeps every song/liner/
  commercial, bumps the revision once, and the dropped items are not
  `fresh` (D5).
- Dropping a news row between two songs inserts a liner/station ID (words
  between songs, §5.2); with no voice inventory at all, the songs stay
  adjacent and nothing crashes.
- A ready request submitted during a freeze is spliced into the frozen
  window (D8) and is the next song slot after `thaw()`.
- `thaw()` on a non-frozen scheduler is a no-op (revision unchanged).
- Seeded determinism: freeze/thaw does not change which items are picked
  for a given seed when nothing is dropped.
- `make sim` unchanged (it never freezes).

---

## Step 3 — Producer pause (`pilgrim/producer.py`; RADIO §6.1)

Inject a predicate, default never idle:

```python
def __init__(..., idle: Callable[[], bool] | None = None):
    self.idle = idle or (lambda: False)
```

- `run()`: at the top of each cycle, `if self.idle(): await self._sleep(self.cfg.idle.producer_poll_s); continue`.
- `news_loop()`: same.
- `sfx_loop()`: same (pending overlays on already-stored clips may still be
  attached; only the evergreen pool refill pauses. Simplest: pause the whole
  loop; a clip committed before its cues are ready airs dry, which is
  already the documented behaviour).
- `song_loop()` / `_next_job()`: in idle, `_stock_due()` returns `False`
  (log `song.stock_paused_idle` once, like `_stock_cap_logged`); queued
  requests are picked exactly as today (D6).
- `ensure_time_checks()` is inside `run()`, so it pauses with it; on wake
  the next cycle (≤ `producer_poll_s`) renders the missing buckets
  (Kokoro ~0.2 s each). Acceptable: a wake inside a quarter-hour window may
  miss one clock read.

Use `self._sleep` (already injectable) for the idle waits in `run`,
`news_loop`, `sfx_loop` too (replace their bare `asyncio.sleep` with
`self._sleep` so tests can drive them; same value).

**Tests** (`pilgrim/tests/test_producer.py`, fakes): with `idle=lambda: True`,
one `song_loop` tick spawns a request job but no stock job; `run()`'s cycle
makes nothing (fake LLM/Kokoro call counters stay 0); with
`idle=lambda: False` behaviour is today's. Existing producer tests pass
unchanged (default predicate).

---

## Step 4 — Station policy (`pilgrim/server.py`; RADIO §3, §11, §14)

In `Station`:

```python
self._idle_since: float | None = None      # clock.now() when idle began
self._last_listener_at: float = self.clock.now()
self.idle_wakes = 0

def is_idle(self) -> bool: return self.scheduler.frozen

def wake(self, reason: str) -> None:
    """Synchronous. Called from the program and heartbeat handlers before
    they read the programme. No-op when not idle."""
    if not self.scheduler.frozen: return
    frozen_s = self.clock.now() - self._idle_since
    dropped = self.scheduler.thaw()
    self._idle_since = None; self.idle_wakes += 1
    self._last_listener_at = self.clock.now()
    log.info("station.wake", extra={"reason": reason, "idle_s": round(frozen_s), "dropped": dropped})

async def idle_loop(self) -> None:
    """Every idle.check_s: with idle.enabled, no listeners for idle.after_s
    and not yet frozen -> scheduler.freeze(); log station.idle."""
```

- `touch_listener()` sets `_last_listener_at` and calls `wake("heartbeat")`.
- The `GET /api/station/program` handler calls `station.wake("program")`
  **first**, then `program_response(...)`.
- `POST /api/admin/dj/play` calls `wake("dj")` first.
- Pass `idle=station.is_idle` into `Producer(...)`.
- `startup()`: add `self._tasks.append(asyncio.create_task(self.idle_loop()))`
  after the warm-up commit. The station starts **awake** and idles after
  `after_s` with no listener, like any other quiet period.
- `health()` (admin) adds `"idle": {"idle": bool, "since": epoch|None, "wakes": n}`.
- `public_state()` adds `"idle": bool`. (`_public_health` unchanged: the
  browser's OFF AIR logic must not see idle.)

`idle_loop` uses `self.clock.now()` and `asyncio.sleep(cfg.idle.check_s)`
(server.py is allowed `asyncio.sleep`; the scheduler is not).

**Tests** (`pilgrim/tests/test_server.py`, `_listener_app` pattern: real
`create_app`, no lifespan, `SimClock` swapped in, temp library seeded with a
few rendered items so the window is non-empty):

- No heartbeat for `after_s` → one `idle_loop` iteration (call the body
  directly, or expose `_idle_check()` and call it) freezes the scheduler;
  `GET /api/station/state` shows `idle: true`, `listeners: 0`, and
  `/api/health` (admin) `on_air: true`.
- While frozen, `/api/health`, `/api/station/state`, `/api/requests`,
  `/api/visitors`, `/api/media/{id}` leave it frozen (assert after each).
- `GET /api/station/program` wakes it: response has items and
  `start_offset_s` equal to the frozen offset; `state.idle` is `false`;
  `idle.wakes == 1` in admin health.
- A heartbeat with a `player_id` wakes it too.
- `idle.enabled: false` → never freezes.
- A `time_check` row committed before the freeze is gone after a 2 h frozen
  wake (uses the Step 2 path end-to-end through the handler).

---

## Step 5 — Docs, checks, report

- `RADIO.md`: §3 (architecture paragraph: idle/wake), §5.3 (frozen playhead
  and wake revalidation, D4/D5), §6.1 (producers pause; requests continue),
  §9.2 (no client change; PLAY wakes the station through the program fetch),
  §11 (`state.idle`, admin health `idle`), §12 (`idle:` section), §14
  (nothing persisted), §15 (the tests above). `docs/` unchanged.
- `README.md`: one paragraph under running the station: what idle does, how
  to turn it off.
- `AGENTS.md` §2 table: no change. Add to §10 pitfalls: "a probe of
  `/api/station/program` wakes the station; use `/api/station/state` or
  `/api/health` for monitoring."
- Checks: `make lint`, `make test`, `make sim`, `make e2e`. The split
  checks are not required (no nginx or web change) but `make split-smoke`
  must still pass if run; its program probe will wake a frozen origin, which
  is fine.

---

## Agent delegation

One agent, sequential (Steps 1→5); roughly a day of work. It can run
**concurrently with VOTING.md Agent A** in separate worktrees: the only
shared files are `server.py` (distinct regions: idle in `Station`/the
program+heartbeat handlers vs. new routes at the end of
`_add_public_routes`), `config.py`/`config.yaml` (distinct sections) and
`RADIO.md`. The integrator (VOTING.md Agent E) rebases and resolves.

## Rollback

`idle.enabled: false` in `config.yaml` restores today's behaviour without a
code change. Full rollback is `git revert` of the Step 1–5 commits; there is
no schema change and no persisted idle state.

## Open questions for the operator

1. `after_s` = 120 s. Shorter saves more compute; longer avoids a freeze
   during a listener's page reload. Decide after watching the logs for a day.
2. Should **free** requests also pause in idle (only paid produce)? This plan
   says no (D6): a free requester is on the page waiting for "up next".
3. Should the station also stop the **S3 publisher / art worker** in idle to
   keep the M5 fully quiet? This plan says no (D7).

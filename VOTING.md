# VOTING.md — Community voting: turkey ratings and request votes

> Amended by FIX_RATINGS.md (2026-11-06): programme rows carry the aggregate, the client shows it for the whole song, the community average is drawn as magenta turkeys until the listener rates, and the web tier's grace window is fixed. Where this file and FIX_RATINGS.md differ, FIX_RATINGS.md wins.

Work plan for coding agents. Two listener controls, no accounts, no new
infrastructure:

1. **RATE THIS SONG** — five turkeys (1–5) above the cover art, open only
   while that song is audible. *You had to be there.*
2. **↑ score ↓** on each free request on the NOW QUEUED board. Votes decide
   which free request is **written next**; paid requests stay ahead of every
   free one; work already generating is never reordered; a request voted
   below a threshold is **buried** (not produced until it recovers).

Plus a mobile pass on the queue: scrollable, obviously so, wrapping prompts,
44 px touch targets, no horizontal scroll.

Read this whole file first. Then read, in order: `AGENTS.md`, `RADIO.md`
§5.2, §6.1, §9.1, §9.2, §11, §12, §14, §15, `PAYMENT.md` §3.2–3.4 (queue
order and board projection) and Step 10 (edge/gateway allowlists),
`SPLIT_PLAN.md` §1–2 (route allowlist, cache headers). `RADIO.md` is the
source of truth; if this file and RADIO.md disagree, stop and report.

Each step ends with the AGENTS.md §7 checks, a commit citing the
RADIO/VOTING sections, and the AGENTS.md §8 report. Code is written on the
Mac checkout; the edge gets `git pull` only (ALBUM_ART.md "Where the work
happens") — this plan changes `pilgrim/web/` **and** the nginx templates, so
Step 7 deploys the edge.

---

## 0. Read this first

### 0.1 Facts observed in the code (2026-11-03)

**Identity.** There are no accounts. The server knows (a) the **canonical
client IP** resolved through the trusted-proxy chain (`Station._client_ip`,
RADIO §14) — it keys the request-line rate limiter and the visitor counter —
and (b) a **`player_id`**, a per-tab random number the browser sends in
heartbeats (client-supplied, indicative only). Nothing survives a reload
except the IP. Raw IPs are stored (`visitor_ips`) by operator decision.

**Requests.** `requests` rows (`pilgrim/store.py`): statuses
`queued -> producing -> ready -> aired` plus `rejected / evicted / failed /
checking / awaiting_payment / abandoned`; `priority` 1 = paid. **One queue
order everywhere** (`QUEUE_ORDER`, PAYMENT §3.2):
`ORDER BY priority DESC, COALESCE(paid_at, created_at) ASC, id ASC`, used by
`next_request_to_produce`, `queued_requests`, `pending_requests`,
`ready_request_songs`, `request_board`, `request_position`. The producer
(`Producer._next_job`) scans `queued_requests()` for the next job; once
claimed, `mark_request_producing` and the in-memory `SongJob.queue_key()`
(`(-priority, paid_at or created_at, id)`) decide the brief-LLM turn order
(`PriorityGate`) and the **ready-order hold** (`_wait_turn`: a finished song
waits for every request ahead of it still in flight, because ready order is
air order — the scheduler splices ready requests in `ready_request_songs()`
order). The public board (`request_board`) projects rows to
`{text, status, paid, stage, song_title}` — **no id** today (PAYMENT §3.4
leak fix); `GET /api/requests` is `public, max-age=2`, so it is shared at
the edge and must stay voter-independent.

**Now playing.** The scheduler's playhead is server-side (`on_air()` →
`(seq, offset)`); `GET /api/station/state` (public, `max-age=1`, polled
every 2 s by a playing page) carries `current_seq`, `current_type`,
`current_offset_s`, `epoch`, `revision`. The browser decides "audible song"
from its audio clock (`player.js` `advancePlayhead` → `setSongLabel(it)`),
which lags the server playhead by well under a second in practice (it joined
at the server's `start_offset_s`). `items` rows hold `title`, `artist`,
`last_aired_at`, `play_count`; nothing rating-like exists.

**Web.** Plain HTML/CSS/JS, no build. `index.html` radio pane:
`.art-frame` → `.play-row` → `#level` → `#song-label` → `.dl-row`. Request
pane: form, pay panel, `NOW QUEUED` table (`.req-table-wrap.req-scroll`,
`max-height: 260px; overflow-y: auto`, sticky header, columns
Request 54% / Title 28% / Status 18%, `table-layout: fixed`,
`overflow-wrap: anywhere`), `AIRED REQUESTS` table. `requests.js` renders
rows with `textContent` only (never HTML), polls every 6 s, labels statuses
("in line", "being written", "being generated", "generated, in line",
"up next", "ready to air"). Theme tokens: `--ok` green `#3affa3`,
`--magenta` `#ff2fd6`, `--cyan`, `--violet`, `--warn` gold `#ffd24a`,
`--dim`. Mobile breakpoint `@media (max-width: 520px)`.

**Edges.** The edge and gateway nginx templates **allowlist exact routes**
(`location = /api/requests`, regex for `/api/paid-requests/<token>`; any
other `/api/*` is 404 at the edge). The gateway rate-limits POST routes with
`limit_req` zones. A new route is invisible to listeners until both
templates list it (PAYMENT Step 10, `make deploy-check`, `make split-smoke`).
Every POST is `no-store` (origin middleware); there is **no** `limit_req`
for PUT/DELETE, so **use POST for all mutations**.

**Tests.** `test_requests.py` (temp `Store`, fake moderation LLM),
`test_server.py` (`create_app` + `TestClient` without lifespan: no
background tasks, no backends), `test_scheduler.py` (SimClock),
Playwright under `pilgrim/tests/e2e/` against `e2e_server.py` (real
`Scheduler` + real `Store` over a temp library of 2 s MP3 tones; its
`/api/requests` is a static stub). `cosmetic.spec.ts` already checks the
360 px viewport.

### 0.2 Decisions (defaults chosen; the operator may override, §9)

| # | Decision | Why |
|---|----------|-----|
| V1 | **Voter identity** = `voter_id`, a 32-hex random string the page generates once and keeps in `localStorage` (`pd_voter`); sent with every vote. Server stores it with the canonical IP | No accounts; the IP alone would merge a household/NAT; the id alone is trivially reset. Together with V3 this is "basic abuse prevention", not security |
| V2 | **Idempotent upserts.** Request vote: `UNIQUE(request_id, voter_id)`, value ∈ {−1, 0, +1} (0 = retract). Rating: `UNIQUE(epoch, seq, voter_id)` (one per **airing**), stars 1–5, a re-vote while still open replaces it | Change/retract supported; a retry is harmless |
| V3 | **Abuse limits:** (a) the existing per-IP 10-minute limiter, `voting.per_client_per_10min` (60); (b) at most `voting.max_voters_per_ip` (3) distinct `voter_id`s per IP per target; beyond that, 429 | Clearing `localStorage` in a loop buys at most 3 votes per target per address |
| V4 | **Request voting window** = status `queued`, `producing`, `ready`; closes at `aired` (or any terminal status). Votes on `producing`/`ready` rows are display-only (the row's place is fixed, V6) | Paste: "close voting when the request begins airing; never retrospective" |
| V5 | **Paid rows are not votable** (no control shown, server 409 `not_votable`) | They are FIFO by payment time, not in the contest (paste: "preserve paid priority") |
| V6 | **Ordering.** Paid first (unchanged). Then rows already in flight (`producing`/`ready`) by the time they were **claimed**. Then free `queued` rows by **score DESC, created_at ASC**. Ties = today's FIFO, so with no votes nothing changes | Votes pick what is *written next*; a job that started is never reordered; air order of ready requests = claim order (`_wait_turn` and the splice agree) |
| V7 | **Bury:** a free `queued` row with score ≤ `voting.bury_threshold` (**−3**) is skipped by the producer while buried; it stays on the board ("buried") and revives when upvoted. Never auto-evicted (open question Q2) | The point of the feature: garbage is not generated. Reversible; operator-tunable; `null` disables |
| V8 | **Rating window** = the song on air now, by `(epoch, seq)`, with `ratings.grace_s` (**5 s**) after it ends for a click in flight. Not a song, or any other seq → 409 `rating_closed` | Server-authoritative; the client only hides the control. Previously aired songs can't be rated |
| V9 | **Aggregates** are computed from the vote tables (SUM/COUNT/AVG), never cached on `requests`/`items` | Always accurate; retractions and changes need no bookkeeping |
| V10 | **Shown aggregates:** request rows show `score`; the state carries the on-air song's lifetime `{count, avg}` (all airings). The page shows `avg · n` once `count > 0` | Per-airing uniqueness, lifetime display |
| V11 | **Own votes** are remembered client-side (`localStorage`) and confirmed by each POST response; shared GETs never carry per-voter data | Keeps `/api/requests` and `/api/station/state` cacheable at the edge |
| V12 | **Ratings do not change playout in this plan.** An optional, config-gated Step 8 lets the rating weight the recycled-song draw; it ships **off** | Agency first, policy later, by the operator |
| V13 | `voting.enabled` / `ratings.enabled` default **true**; `false` = routes 404, UI hidden | Rollback switch |

### 0.3 Rules

1. Hard rules unchanged: nothing here imports payments; playout never
   waits on anything; the browser is never trusted about status or window
   (server decides open/closed); LLM untouched.
2. `scheduler.py` / `selector.py` / `producer.py` read votes only through
   `Store` queries (pure of time/random). Step 8 is the only playout touch
   and is off by default.
3. Listener text and anything from the vote tables is rendered with
   `textContent`. Request ids are plain integers (they are not secrets; a
   vote needs an open window, not knowledge of the id).
4. Every GET keeps its cache header and stays voter-independent (V11).
5. No new dependency. Schema changes are additive and documented in
   RADIO §14 in the same commit (AGENTS rule 8).

---

## 1. Data model (`pilgrim/store.py`; RADIO §14)

Idempotent migration in `_init_schema`, same pattern as the paid columns:

```sql
CREATE TABLE IF NOT EXISTS request_votes (
    request_id INTEGER NOT NULL,
    voter_id   TEXT    NOT NULL,
    ip         TEXT,                        -- canonical client IP (abuse cap, V3)
    value      INTEGER NOT NULL,            -- -1 | 1  (0 = retracted -> row deleted)
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (request_id, voter_id)
);
CREATE INDEX IF NOT EXISTS idx_request_votes_ip ON request_votes(request_id, ip);

CREATE TABLE IF NOT EXISTS song_ratings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL,            -- the song (items.id)
    epoch      TEXT    NOT NULL,            -- airing identity: station epoch + program seq
    seq        INTEGER NOT NULL,
    voter_id   TEXT    NOT NULL,
    ip         TEXT,
    stars      INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL,
    UNIQUE (epoch, seq, voter_id)
);
CREATE INDEX IF NOT EXISTS idx_song_ratings_item ON song_ratings(item_id);
CREATE INDEX IF NOT EXISTS idx_song_ratings_ip ON song_ratings(epoch, seq, ip);
```

`requests` gains `claimed_at TEXT` (set by `mark_request_producing` the
first time only: `COALESCE(claimed_at, ?)`), so in-flight order is the claim
order (V6). `mark_request_producing` returns the row's `claimed_at`.

**Queue order** (replaces `QUEUE_ORDER` / `QUEUE_ORDER_R`; one helper
`queue_order(alias: str = "") -> str` that renders both):

```sql
ORDER BY priority DESC,
         CASE WHEN status = 'queued' THEN 1 ELSE 0 END ASC,        -- in flight first
         CASE WHEN status = 'queued' AND priority = 0
              THEN -(SELECT COALESCE(SUM(v.value), 0)
                     FROM request_votes v WHERE v.request_id = <alias>id)
              ELSE 0 END ASC,                                       -- score DESC
         COALESCE(paid_at, claimed_at, created_at) ASC,
         id ASC
```

Apply it in every place PAYMENT §3.2 lists. With no votes and no
`claimed_at` the order is today's (prove it: the existing ordering tests
pass unchanged).

New/changed store methods:

```python
def cast_request_vote(self, request_id: int, voter_id: str, ip: str | None,
                      value: int, *, max_voters_per_ip: int) -> dict
    # one locked transaction:
    #   row must be status IN ('queued','producing','ready') AND priority = 0
    #     -> else {"ok": False, "code": "voting_closed" | "not_votable" | "not_found"}
    #   distinct voter_ids for (request_id, ip) excluding this voter >= cap
    #     -> {"ok": False, "code": "too_many_voters"}
    #   value == 0: DELETE; else INSERT ... ON CONFLICT(request_id, voter_id)
    #     DO UPDATE SET value=excluded.value, updated_at=...
    #   returns {"ok": True, "score": SUM, "mine": value, "open": True}
def request_score(self, request_id: int) -> int
def queued_requests(self, cap=None, *, bury_threshold: int | None = None)
    # bury_threshold given: free rows with score <= threshold are excluded
def next_request_to_produce(self, *, bury_threshold: int | None = None)
def request_board(...)  # queue rows add: id, score, votable, buried
def cast_song_rating(self, *, item_id: int, epoch: str, seq: int, voter_id: str,
                     ip: str | None, stars: int, max_voters_per_ip: int) -> dict
    # same shape: upsert on (epoch, seq, voter_id); per-IP cap per (epoch, seq)
    # returns {"ok": True, "count": n, "avg": float, "mine": stars}
def song_rating(self, item_id: int) -> dict   # {"count": n, "avg": float|None}
def recent_votes(self, limit=100) / recent_ratings(self, limit=100)   # admin audit
```

Window checks for ratings are **not** in the store (the window is scheduler
state, §3). The store only enforces uniqueness and the IP cap.

**Board projection** (`_project_queue_row`): add `id` (int), `score` (int),
`votable` (bool: free and status in the window and `voting.enabled`),
`buried` (bool). Recent rows unchanged (no retrospective voting, no score).
Update the PAYMENT §3.5 "exact key set" test in a clearly described change.

**Tests** (`pilgrim/tests/test_requests.py`, temp DB):

- Migration idempotent on an old-schema copy (`claimed_at`, both tables).
- Upsert: vote +1 then −1 → score −1, one row; 0 deletes; a second voter
  → score −2; `request_score` matches `SUM`.
- Window: `aired`/`rejected`/`evicted`/`failed`/`awaiting_payment` →
  `voting_closed`; paid → `not_votable`; unknown → `not_found`.
- IP cap: 3 voter_ids from one IP ok, the 4th → `too_many_voters`; the
  same voter re-voting is not a new voter.
- Order: free A, B (B voted +2) → produce B first; A, B both +2 → A (FIFO);
  paid C after both → C, B, A; B claimed (`mark_request_producing`), then A
  voted +5 → board/`request_position` is C, B, A (in flight first) and
  `next_request_to_produce` is A. PAYMENT §3.5 ordering tests unchanged.
- Bury: score −3 excluded from `queued_requests(bury_threshold=-3)` and
  `next_request_to_produce`; present on the board with `buried: true`; +1
  later revives it; `bury_threshold=None` disables.
- Ratings: upsert per `(epoch, seq, voter)`; the same voter can rate the
  same `item_id` again under a new seq; `song_rating` count/avg over both
  airings; stars 0 or 6 rejected (CHECK); IP cap.
- Board queue rows carry exactly
  `{id, text, status, paid, stage, song_title, score, votable, buried}`.

---

## 2. API contracts (`pilgrim/voting.py` new, `pilgrim/server.py`; RADIO §11)

Put the payload models, the window check and the two service functions in
**`pilgrim/voting.py`** so `server.py` and the e2e server share them:

```python
VOTER_RE = re.compile(r"^[0-9a-f]{32}$")

class VoteIn(BaseModel):   voter_id: str; value: Literal[-1, 0, 1]
class RatingIn(BaseModel): voter_id: str; epoch: str; seq: int; stars: int (1..5)

def rating_window(scheduler, epoch: str, seq: int, grace_s: float) -> tuple[bool, int | None]:
    """(open, item_id). Open when epoch matches and seq is the on-air row of
    type 'song', or the row immediately before the on-air one is that song
    and the on-air offset <= grace_s (V8). Pure over scheduler state."""
```

Routes, both apps (`_add_public_routes`), **POST** only, `no-store`:

| Method | Path | Body | 200 | Errors |
|---|---|---|---|---|
| POST | `/api/requests/{id}/vote` | `{voter_id, value}` | `{ok: true, id, score, mine, open: true}` | 404 `not_found`; 409 `{code: voting_closed \| not_votable}` with the current `score`; 422 bad body / voter_id; 429 rate limit or `too_many_voters`; 404 when `voting.enabled` is false |
| POST | `/api/station/rating` | `{voter_id, epoch, seq, stars}` | `{ok: true, item_id, count, avg, mine}` | 409 `{code: rating_closed}`; 422; 429; 404 when `ratings.enabled` is false |

Reads (fields **added**, nothing removed):

- `GET /api/requests`: queue rows add `id, score, votable, buried`; top
  level adds `voting_enabled: bool`.
- `GET /api/station/state`: adds `rating: {open: bool, count: int, avg: float|null}`
  for the on-air item (`open` = `rating_window(...)[0]`; `count/avg` =
  `song_rating(item_id)` of the on-air song, zeros otherwise), or
  `rating: null` when `ratings.enabled` is false. Cached `max-age=1` as
  today (voter-independent).
- Admin: `GET /api/admin/voting?limit=` (behind `require_dj`) →
  `{votes: [...], ratings: [...], buried: [...]}` from `recent_votes`,
  `recent_ratings`, and the buried board rows. Audit only.

Handler order for a vote: `voting.enabled` → rate limit (`_make_rate_limiter(station, cfg.voting.per_client_per_10min)`, same IP resolution as the request line) → body validation → `store.cast_request_vote(...)` → map `code` to status. For a rating: enabled → rate limit → body → `rating_window(station.scheduler, ...)` → `store.cast_song_rating(...)`. The window check and the insert run in the same event-loop turn with no `await` between them, and the scheduler loop runs on the same loop, so the window cannot move between check and write ("atomic closing").

Logging: `vote.cast {request_id, value, score}` and `rating.cast {item_id, seq, stars, count}` with the voter's IP **omitted** (the DB has it; logs don't need it). Never log `voter_id` either.

**Config** (`config.py` models `Voting`, `Ratings`; `config.yaml`; RADIO §12):

```yaml
voting:                      # VOTING.md: request-queue votes
  enabled: true
  per_client_per_10min: 60   # vote POSTs per client IP (same limiter as the request line)
  max_voters_per_ip: 3       # distinct voter ids per IP per request
  bury_threshold: -3         # free queued request at/below this score is not produced; null = off
ratings:                     # VOTING.md: five-turkey song ratings
  enabled: true
  per_client_per_10min: 60
  max_voters_per_ip: 3
  grace_s: 5                 # a rating may land this long after the song ended
  rotation_weight: false     # Step 8 (off): let ratings weight the recycled-song draw
  rotation_min_votes: 5
```

**Producer** (`pilgrim/producer.py`, two lines): `_next_job` iterates
`self.store.queued_requests(bury_threshold=self.cfg.voting.bury_threshold)`;
`_spawn` sets `job.req["claimed_at"]` from `mark_request_producing`'s return
and `SongJob.queue_key()` becomes
`(-priority, paid_at or claimed_at or created_at, id)`. The backlog count in
`_produce_request_song` uses the same filtered call. Nothing else changes.

**Edge + gateway** (`pilgrim/deploy/edge/nginx.conf.template`,
`pilgrim/deploy/gateway/nginx.conf.template`): add, mirroring the
paid-request blocks exactly (headers, timeouts, `@offair` fallback):

- `location ~ "^/api/requests/[0-9]+/vote$" { ... }` — POST; gateway
  `limit_req zone=votes burst=50 nodelay` with a new
  `limit_req_zone $req_line_key zone=votes:10m rate=5r/s;`
- `location = /api/station/rating { ... }` — POST; same zone.

Note the gateway's `$edge_key` is the **edge's** tailnet address, not the
listener's (see the `map` at the top of the gateway template), so this zone
is a per-edge ceiling shared by every listener behind it, like `req_line`.
Per-listener fairness is the origin's own limiter (`voting.per_client_per_10min`).

Non-POST methods on those paths return 404/405 like the paid routes. Add
checks to `pilgrim/deploy/local/smoke.sh` and
`pilgrim/deploy/edge/cutover_check.sh` (the next free numbers): a vote on an
unknown id through the edge is **404 from the origin** (proves the route is
proxied, not edge-404), a rating with a bad body is **422**, and both are
`no-store`.

**Tests** (`pilgrim/tests/test_server.py` style, `TestClient`, temp library):

- Vote on a queued free request → 200 with score; same voter, value 0 →
  retract; paid row → 409 `not_votable`; aired → 409 `voting_closed`; bad
  `voter_id` → 422; 61st POST in 10 min from one IP → 429; `voting.enabled`
  false → 404 and `voting_enabled: false` on the board.
- Rating: seed one song in the committed program, `SimClock` at its start →
  200; advance past its end + `grace_s` → 409 `rating_closed`; wrong epoch →
  409; non-song on air → 409; state shows `rating.open` flipping and
  `count/avg` after a vote; `ratings.enabled` false → 404 and
  `rating: null`.
- `GET /api/requests` and `GET /api/station/state` keep their
  `Cache-Control` headers; every vote/rating response is `no-store`.
- Public app registers both routes; `/api/admin/voting` is 404 on the
  public app and 401/403 without the DJ token on the admin app
  (`test_server_split.py` pattern).
- `make deploy-check` renders both templates; `make split-smoke` passes with
  the new checks; `make e2e-split` after Step 7.

---

## 3. Rating window on the server (`pilgrim/voting.py`; RADIO §5.3, §9.1)

`rating_window` reads `scheduler.on_air()` and `scheduler._items`
(`seq`, `type`, `item_id`, `duration_s`). Cases:

- epoch mismatch → closed;
- `seq == on_air_seq` and that row is `song` → open;
- row before the on-air row is `song` with `seq` given, and
  `on_air_offset <= grace_s` → open (the click was in flight when the song
  ended);
- anything else (future seq, older seq, intro, non-song) → closed.

Note IDLE.md: a frozen station still has an on-air row; a rating then is
legal (someone could be hearing the tail of the buffered song) and harmless.

**Tests** (`pilgrim/tests/test_voting.py`, SimClock scheduler over a temp
store): the four cases above plus grace boundary (`offset == grace_s` open,
`+ 0.1` closed) and a revision bump that renumbers the future (a splice:
the on-air seq is stable, the old future seqs are closed).

---

## 4. Client contract shared by Steps 5 and 6 (`pilgrim/web/`)

- **`pilgrim/web/voter.js`** (tiny, loads first): `window.PD_VOTER = {id()}`
  — returns the 32-hex id from `localStorage.pd_voter`, generating it with
  `crypto.getRandomValues` on first use; falls back to an in-memory id when
  storage throws (private mode). Also `remember(key, value)` /
  `recall(key)` helpers over `localStorage` (`pd_vote:<request id>`,
  `pd_rating:<epoch>:<seq>`), each wrapped in try/catch.
- **`player.js`** (one hook, Agent C owns the edit): in `setSongLabel(it)`
  and in `stop()`, dispatch
  `document.dispatchEvent(new CustomEvent("pd:nowplaying", {detail}))` with
  `detail = named ? {epoch, seq: it.seq, media_id: it.media_id, title, artist} : null`.
  Nothing else in `player.js` changes; playback, OFF AIR and the label are
  untouched (RADIO §9.2).
- Script order in `index.html`: `titlebar.js, voter.js, requests.js,
  votes.js, pay.js, visitors.js, rating.js, player.js` with new
  `?v=` stamps.

---

## 5. Live song ratings — frontend (Agent C; RADIO §9.1)

**Markup** (`index.html`, inside `.win-body.radio`, **before** `.art-frame`):

```html
<div class="rate-row" id="rate-row" role="radiogroup" aria-label="Rate this song" aria-disabled="true">
  <span class="rate-label">RATE THIS SONG</span>
  <span class="turkeys">
    <button type="button" class="turkey" role="radio" aria-checked="false" aria-label="1 turkey" data-stars="1"><svg aria-hidden="true"><use href="#turkey"/></svg></button>
    … ×5 …
  </span>
  <span class="rate-agg" id="rate-agg" aria-live="polite"></span>
</div>
```

The turkey symbol is inlined once at the top of `<body>` as
`<svg width="0" height="0" style="position:absolute" aria-hidden="true"><symbol id="turkey" viewBox="0 0 24 24">…</symbol></svg>`:
a simple hand-drawn path set (fan of 5–7 tail feathers as arcs behind a
round body, a small head with a wattle), stroke on `currentColor`, fill on
`currentColor` only when `.filled`. Keep the source copy in
`pilgrim/web/img/turkey.svg` (same `<symbol>`), so reels and future pages
can reuse it. No external graphics; no font icons.

**Style** (`style.css`, a new commented block after the `.dl-row` block):
`.rate-row` is as wide as the art (`width: var(--art-w)`), one line on
desktop (`display: flex; align-items: center; justify-content: space-between;
gap: 8px; min-height: 44px`), label in `--dim` with the pane's letter-spacing
(13 px, like `.dl-mp3`), five `.turkey` buttons each **44×44 px** hit area
(`width:44px;height:44px;padding:0;background:none;border:0`), svg 28×28 in
`--warn` gold, outlined by default, `.filled` (hovered/selected level and
below) filled; disabled state (`aria-disabled="true"`) at 45 % opacity and
`cursor: default`. `.rate-agg` is `--dim` 12 px (`4.2 · 17`). On
`max-width: 520px` the row keeps one line (label 12 px, turkeys 40×40,
aggregate under the label if it does not fit: `flex-wrap: wrap` with the
label+agg stacked in a column). The row is **always present with a fixed
min-height**, so the cover art, PLAY row and meter never move when it
enables or disables (the same rule as `.dl-row`, RADIO §9.1).

**Behaviour** (`rating.js`):

- Listens for `pd:nowplaying`. `detail === null` → disabled, all outlined,
  aggregate empty. `detail` set → enabled for `(epoch, seq)`; restore
  `recall("pd_rating:<epoch>:<seq>")` as the filled level if present.
- Hover/focus previews a level; click/Enter/Space posts
  `{voter_id, epoch, seq, stars}` to `/api/station/rating`. On 200: fill to
  `mine`, `remember(...)`, show `avg · count`. On 409 `rating_closed`:
  disable with the label text `VOTING CLOSED` for 3 s, then back to the
  normal label (the song is over; the next `pd:nowplaying` re-enables). On
  429/network: a 3 s `TRY AGAIN` on the label; never a modal, never
  `alert()`.
- The aggregate also refreshes from the playing page's 2 s
  `/api/station/state` poll: `rating.js` runs its own light poll of state
  **only while enabled** (every 5 s; `rating.count/avg`, and `rating.open`
  false → disable). Playback code stays the only consumer of
  `epoch/revision` (§9.2); `rating.js` never touches audio.
- Keyboard: arrow keys move between turkeys (radio-group semantics), Enter
  selects. `aria-checked` on the chosen one.

**Tests** (Playwright, `pilgrim/tests/e2e/voting.spec.ts`, after Step 7's
e2e server support): the row is disabled before PLAY; enabled once
`#song-label` is non-empty; clicking turkey 4 posts and fills four; the
aggregate reads `4.0 · 1`; after the song ends the row disables and a forced
POST for the old seq returns 409; STOP disables; reload during the same song
restores the filled level from storage; at 360 px nothing overflows and each
turkey's bounding box is ≥ 40×40.

---

## 6. Request-queue voting + mobile scrolling — frontend (Agent D; RADIO §9.1)

**Markup** (`index.html`, NOW QUEUED table): add a fourth column
`<th class="c-vote"><span class="sr-only">Votes</span></th>`; colgroup
widths become Request 48 % / Title 22 % / Status 14 % / Vote 96 px (set
`table-layout: fixed` widths on `.c-*`). The AIRED REQUESTS table is
unchanged (no retrospective voting).

**Vote cell** (`votes.js` exports `window.PD_VOTES = {cell(row), refresh(row, td)}`;
`requests.js` calls `cell(r)` when building each queue row, right after the
status cell):

```
<td class="req-vote"><div class="vote" data-id="12">
  <button type="button" class="vote-up"   aria-label="Upvote request #3"   aria-pressed="false">↑</button>
  <span class="vote-score" aria-live="polite">0</span>
  <button type="button" class="vote-down" aria-label="Downvote request #3" aria-pressed="false">↓</button>
</div></td>
```

Horizontal `↑ [score] ↓`, score centered. Up = `--ok` green, down =
`--magenta`; pressed state = filled background of the same color with
`#0a0118` text (the pane's button convention), un-pressed = outlined
(`1px solid`) on the panel background. Each arrow is a **40×40 px** box with
a 44 px hit area (`padding` + negative margin, or `min-width/height: 44px`
with the glyph centered). Score is 15 px, `--text`, `min-width: 2ch`.
`votable: false` rows (paid, or voting off) render an empty cell; `buried`
rows get status label `buried` and the score in `--magenta`.

**Behaviour:**

- Click ↑: `value = mine === 1 ? 0 : 1`; click ↓: `value = mine === -1 ? 0 : -1`
  (second click retracts). POST `/api/requests/{id}/vote`. On 200 set the
  score and pressed states, `remember("pd_vote:<id>", mine)`. On 409
  `voting_closed` show the returned `score`, disable the pair, and let the
  next poll relabel the row. On 429/network: brief `.vote.err` shake/tint,
  no text. Buttons are `disabled` while a POST is in flight (no double
  submit).
- Render restores `recall("pd_vote:<id>")` as the pressed state; a score
  returned by a POST is **sticky for 4 s** against the 6 s board poll (the
  edge may serve a 2 s-old board), so a vote never visibly flickers back.
- `requests.js` keeps rendering everything with `textContent`; the vote
  cell is built with `createElement`, never from strings.

**Mobile + scrolling** (`style.css`, the request block):

- `.req-scroll`: keep `max-height: 260px; overflow-y: auto`; add
  `overscroll-behavior: contain`, `scrollbar-width: thin;
  scrollbar-color: var(--violet) transparent` (+ the `::-webkit-scrollbar`
  pair, 6 px, violet thumb) so the scrollbar is visible on touch devices
  without taking width.
- Scroll cue: `.req-table-wrap.req-scroll` gets `position: relative` and a
  `::after` 24 px bottom gradient (`transparent → var(--panel)`) shown only
  while `.has-more` is on (there is content below the fold:
  `scrollHeight - scrollTop - clientHeight > 4`), toggled by `requests.js`
  on `scroll`, on render, and on `resize`. Costs zero height; disappears at
  the bottom.
- Rows: `td { padding: 6px 4px }` on mobile; the prompt cell keeps
  `overflow-wrap: anywhere` and gets `hyphens: auto`; `.req-vote` never
  wraps (`white-space: nowrap`).
- `@media (max-width: 520px)`: hide the **Status** column header and cell
  and render the status label as a second line **under the title** in the
  Title cell (`.req-title .req-status-inline`, 10 px `--dim`), so the
  table has three visual columns: Request / Title+status / Vote
  (Request 50 %, Title 50 %-96px, Vote 96 px). Same data, no new concept.
- Nothing may introduce horizontal scroll at 320 px:
  `#desktop`, `.window`, `.req-table` stay `max-width: 100%`.

**Tests** (Playwright, `pilgrim/tests/e2e/voting.spec.ts`, after Step 7):
with 12 queued rows seeded, the queue box scrolls, `.has-more` is on at the
top and off at the bottom; ↑ on row 3 posts and shows `1` pressed; ↑ again
retracts to `0`; ↓ shows `-1`; a paid row has no buttons; a row whose
request is marked aired by the test endpoint relabels and loses its buttons
on the next poll; reload keeps the pressed state; at 360 px and 320 px
`document.documentElement.scrollWidth <= innerWidth`, the vote buttons'
bounding boxes are ≥ 40×40 and the status text appears under the title;
`cosmetic.spec.ts` still passes.

---

## 7. Integration, e2e support, docs, deploy (Agent E)

1. **e2e server** (`pilgrim/tests/e2e/e2e_server.py`): replace the static
   `/api/requests` stub with the real `store.request_board(...)` over
   12 seeded free requests + 1 paid (`priority=1, paid_at` set); add
   `POST /api/requests/{id}/vote` and `POST /api/station/rating` by calling
   the shared `pilgrim/voting.py` service functions with the e2e
   scheduler/store; add `rating` to `/api/station/state`; add test-only
   `POST /api/test/air/{id}` (marks a request aired) and
   `POST /api/test/advance` (nothing to do: real clock, 2 s clips) as the
   specs need. The split e2e render (`make e2e-split`) already allowlists
   `^~ /api/test/`.
2. Run `make lint`, `make test`, `make sim`, `make e2e`, `make deploy-check`,
   `make split-smoke`, `make e2e-split`. Fix integration fallout only;
   anything deeper goes back to the owning agent.
3. **Docs** in the same commits: `RADIO.md` §9.1 (rating row, vote column,
   scroll cue, mobile status-under-title), §9.2 (the `pd:nowplaying` hook;
   `rating.js` never touches audio), §11 (two routes, `rating` on state,
   board fields, admin voting), §12 (`voting:`/`ratings:`), §14 (tables,
   `claimed_at`, the new queue order — note that PAYMENT.md §3.2 is
   superseded by it), §15 (tests). `PAYMENT.md` §3.2: one line pointing to
   RADIO §14. `README.md`: a short "Community voting" paragraph (identity =
   browser id + IP, what is stored, how to disable). `docs/backends.md`:
   nothing (no backend).
4. **Deploy:** both nginx hops render from the templates, so both must be
   re-rendered. On the M5: restart the origin stack with
   `pilgrim/deploy/mac/origin.sh` (it renders the gateway template and
   reloads the Homebrew nginx). On the edge:
   `ssh pf 'cd ~/radio && git pull && make edge-up'` (renders the edge
   template and restarts it), then
   `ssh pf 'cd ~/radio && bash pilgrim/deploy/edge/cutover_check.sh'` with
   the new checks. Verify in a phone browser: vote, rate, scroll.

---

## 8. Optional — ratings weight the recycled-song draw (off by default; RADIO §5.2)

Only after Steps 1–7 ship and only as its own commit. In
`Scheduler._song_weight`, when `cfg.ratings.rotation_weight` is true and
`store.song_rating(item_id).count >= rotation_min_votes`, multiply the
weight by `clamp(avg / 3.0, 0.25, 1.5)` (a 3-turkey song is neutral, a
1-turkey song keeps a quarter of its pull, a 5-turkey song gets half again).
It reads the store only (pure of time/random), never breaks spacing (it
orders songs that already pass it) and never drops a song below the
starvation guard. Sim assertion: with ratings seeded, every §15 invariant
still holds and a 5-turkey song airs more often than a 1-turkey one of the
same age. `config.yaml` ships `rotation_weight: false`; flipping it is the
operator's call (§9 Q3).

---

## Agent delegation

| Agent | Owns (files) | Depends on | Deliverable |
|---|---|---|---|
| **A — Backend** | `pilgrim/store.py`, `pilgrim/voting.py`, `pilgrim/server.py` (new routes at the end of `_add_public_routes`, admin route), `pilgrim/config.py` + `config.yaml` (`voting`, `ratings`), `pilgrim/producer.py` (the two lines in §2), `pilgrim/deploy/edge|gateway/nginx.conf.template`, `smoke.sh`, `cutover_check.sh`, tests in `test_requests.py`, `test_server.py`, `test_voting.py`, `test_render_deploy.py`; RADIO §11/§12/§14 | — | Steps 1–3 + the backend half of §2's deploy checks. Commit per step |
| **B — Idle** | see `IDLE.md` | — | Runs concurrently with A in another worktree; only `server.py` (different regions), `config.*` (different sections) and RADIO.md overlap |
| **C — Ratings UI** | `pilgrim/web/rating.js`, `pilgrim/web/voter.js`, `pilgrim/web/img/turkey.svg`, the `<symbol>` + `.rate-row` markup in `index.html` (radio pane only), the rating CSS block in `style.css`, the one-hook edit in `player.js` | contract in §2/§4 (can start at once; e2e needs Step 7) | Step 5 |
| **D — Queue UI** | `pilgrim/web/votes.js`, `pilgrim/web/requests.js`, the NOW QUEUED markup in `index.html` (request pane only), the request/vote/scroll CSS blocks in `style.css` | contract in §2/§4 (can start at once; e2e needs Step 7) | Step 6 |
| **E — Integration** | `pilgrim/tests/e2e/*` (server + specs), docs reconciliation, rebase of A/B/C/D, edge deploy, Step 8 if approved | A, B, C, D merged | Step 7 (+ 8) |

**Concurrency.** A ∥ B from the start. C ∥ D from the start against the
written contract (both edit `index.html` and `style.css` in disjoint,
commented regions; each appends its CSS block under its own header to avoid
merge conflicts). E starts when A has landed and C/D have PRs, and owns the
final merge order A → B → C → D → E. The one cross-agent interface in the
browser is `pd:nowplaying` (C) and `PD_VOTES.cell` (D ↔ `requests.js`, both
D's).

**Acceptance (all):** AGENTS.md §7 checks green; new tests listed above
exist and pass; no removed HTTP field; GET cache headers unchanged;
`textContent`-only rendering; 44 px targets; no horizontal scroll at 320 px;
report per AGENTS.md §8.

## Verification commands

```
make lint && make test && make sim && make e2e
make deploy-check && make split-smoke && make e2e-split      # nginx templates changed
ssh pf 'cd ~/radio && git pull && bash pilgrim/deploy/edge/cutover_check.sh'
```

## Rollback

- Config: `voting.enabled: false` and/or `ratings.enabled: false` hide the
  UI and 404 the routes with no code change; `bury_threshold: null` keeps
  votes but stops burying.
- Code: `git revert` per step. The schema changes are additive
  (`request_votes`, `song_ratings`, `requests.claimed_at`); old code ignores
  them, nothing is dropped, `station.db` is never deleted or rewritten
  (hard rule 5). A reverted queue order falls back to pure FIFO; nothing in
  flight is affected.
- Edge: the nginx locations are harmless once the origin 404s the routes;
  revert the templates in the same revert and `make edge-up`.

## 9. Open questions for the operator (defaults in force until answered)

1. **Identity strength.** V1/V3 stops casual abuse only. If vote brigading
   appears, the next step is a signed cookie minted by the origin (no
   accounts, still no PII) — not in this plan.
2. **Buried requests** stay on the board and keep a slot in the free line
   (`free_queue_max`). Should a request buried for more than N hours be
   `evicted` (reason `voted down`)? Default: no automatic eviction.
3. **Ratings → rotation** (Step 8) ships off. Turn it on after a week of
   ratings, or leave ratings as display only?
4. **Paid rows**: not votable (V5). Flip to "votable, display-only" if the
   community should at least be able to react to a paid prompt.
5. **Grace** of 5 s after a song (V8): too generous, or fine?
6. **Retention**: `request_votes`/`song_ratings` store the canonical IP like
   `visitor_ips` does; no pruning policy exists for either (RADIO §14).

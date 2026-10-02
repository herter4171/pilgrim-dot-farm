# DJ console — Textual frontend and station controls

Design proposal, 2026-10-01. This document proposes implementation work; it
does not change the running station, API, configuration, database, or voice
assignments. RADIO.md remains authoritative. Adopt the proposed changes there
before implementing new contracts (RADIO §§11–12, 14).

Build a Textual console with three jobs: skip the current segment, choose an
existing song from a sortable **Name / Artist** table, and submit a phrase
spoken by a new, manually operated character with a distinct Kokoro voice.
The console controls the station over HTTP. Audio continues through the
browser's Web Audio engine (RADIO §§3, 9.2).

## 1. What exists today

### Discovery and limits

The running station returned HTTP 200 for **GET `/openapi.json`** on
2026-10-01, identifying itself as `Pilgrim Dot Farm Radio`, version `0.2.0`.
The address was read from `pilgrim/config.yaml` (`station.host` and
`station.port`). This was the only live station request made for this plan.
No playback, health fan-out, voice sampling, generation, or administration
endpoint was exercised.

The schema confirms these routes. Response details below come from the
current `pilgrim/server.py`, not sampled live payloads: almost all response
schemas are `{}`, so OpenAPI alone cannot generate a useful typed client.

| Existing route | Observed schema / behavior in source | Use for this feature |
|---|---|---|
| `GET /api/station/program?after_seq=N` | Optional nonnegative integer cursor. Source returns `{items, start_offset_s}`; each item has `seq, media_id, type, duration_s, sfx, title, artist`. | Initial read-only programme view; later add revision handling. |
| `POST /api/station/heartbeat` | Open-ended JSON object. Source records airplay; it does not move the scheduler playhead. | Browser playback telemetry, never a TUI skip mechanism. |
| `POST /api/station/start`, `/stop` | Both are no-ops in current source; the station runs continuously. | Neither implements skipping. |
| `GET /api/media/{item_id}` | Integer inventory ID. Source serves FLAC. | Existing rendered media delivery. |
| `GET /api/health` | Source queries backend health and inventory. | Do not poll it rapidly for console state. |
| `GET /api/admin/voices` | Source wraps Kokoro's voice list as `{voices: [...]}`, returning an empty list on failure. | Future character setup; distinguish outage from no available voices in the new API. |
| `GET /api/requests`, `POST /api/requests` | POST accepts `{text}` and enters moderated song **generation**. | Keep separate from selecting an existing song or reading an operator phrase. |
| `POST /api/visitors` | Visitor registration. | No console use. |

There is no song catalogue, skip, queue-edit, phrase-job, or programme revision
endpoint in the fetched schema. It declares no security schemes; the current
station routes also contain no operator authentication.

### Constraints and discrepancies to resolve

- RADIO §3 describes a heartbeat-driven, single-listener programme. Current
  `Scheduler.position()` advances with the injected clock independently of
  listeners; `server.py` treats the station as always on air. RADIO §11's
  start/stop descriptions also differ from the no-op handlers. **Proposal:**
  document and retain the existing shared station timeline for DJ controls;
  this needs an explicit RADIO §§3, 11 amendment before implementation.
- The browser already schedules current + next two decoded items. Changing
  SQLite rows cannot stop those sources. It also fetches programme metadata
  only when its local list runs low, so skips require a separate revision
  check (RADIO §§5.3, 9.2).
- `Scheduler._append()` consumes one-shot inventory and records song rotation
  information at commit time. Editing the committed tail requires separating
  reservations from actual starts and superseded entries (RADIO §§5.2, 7, 14).
- `OVERHAUL.md`, named as the active plan in AGENTS.md, is absent from this
  checkout. Reconcile this feature's implementation order with it when
  available. AGENTS.md's minimal-web-UI milestone also predates the richer UI
  described by current RADIO §9.1; RADIO takes precedence.

## 2. Operator experience

Proposed wide-terminal layout; narrow terminals use tabs for the same panes:

```text
 PILGRIM DJ                    CONNECTED       Updated just now
 ON AIR   Song name — Artist                  01:12 / 03:24
 [Skip current]     Command: ready

 SONGS                                  UPCOMING
 Search: [                         ]    #     Type       Name
 Name ↑                 Artist          ...   liner      ...
 Amber Fields           Barn Choir      ...   song       ...
 Night Tractor          The Furrows
 [Queue next]                           Pending DJ actions: ...

 CHARACTER: <name chosen by operator>    VOICE: <unused voice>
 Phrase: [                                                   ]
 Will say: <cleaned spoken text>
 [Render phrase]       Status: ready     [Queue phrase]

 Tab focus  / search  n name sort  a artist sort  Enter queue song
 Ctrl+K skip current  Ctrl+P phrase  r refresh  Ctrl+Q quit console
```

**Song selection.** Show all non-retired, non-emergency library songs with
finished, QC-passed media, including previously aired songs. Map the UI's
**Name** column to the existing `title` field; do not rename database fields.
Use item IDs as row keys so sorting never changes which song gets queued.
Both header clicks and keyboard bindings toggle ascending/descending order.
Sort case-insensitively, break ties by the other column and then ID, and keep
missing metadata visible with a display-only placeholder. Preserve selection,
search, and sort after refresh. Sorting/filtering has no playback side effects.

**Queue next.** Enter on a song row adds it to the ordered DJ queue, targeting
the earliest eligible boundary after the current segment. Show the actual
planned position and any required bridge; it must not silently wait behind
the existing ten-minute committed window. Further selections are FIFO.
Return a clear reason when spacing or other rules make a song ineligible;
v1 has no implicit repeat override. Existing ready listener requests retain
their relative order after the manual queue. This proposed manual priority
must be added to RADIO §5.2.

**Skip current.** A dedicated button/key skips the item identified in the
displayed state, with the server checking that it is still current. A stale
action refreshes the display instead of skipping the next item by accident.
Treat an intro and its song as one selectable unit: skip during the intro
skips both; skip during the song ends the song. A multi-spot commercial break
skips the current spot in v1. Display the action's scope before execution.
Dropping arbitrary future segments and “play selected song immediately” are
later additions; queue-next plus skip covers the first release.

**Phrase.** Type the literal words, inspect the cleaned text, render, then
queue the finished clip. Rendering alone does not put it on air. Show
queued/rendering/ready/failed/expired states with a useful failure reason.
Submitting a phrase never asks an LLM to rewrite it or invent additional copy.
The new character appears only through these manual submissions.

Keyboard actions are focus-aware: letters and Enter inside search/phrase
fields edit text rather than queue or skip. Disable a mutation while that
action is pending; a timeout shows “checking outcome,” not “failed, retry.”
Disconnects preserve typed text and table state, show the last successful
update, and disable live controls until state is fresh. Quitting the TUI
only closes its HTTP client.

## 3. Frontend structure

Use `pilgrim/tui/` with `__main__.py`, `app.py`, `client.py`, `models.py`, and
`console.tcss`; start it with a proposed `make tui` /
`python -m pilgrim.tui`. Keep all tests under `pilgrim/tests/` (RADIO §18).
Pin a tested Textual version in `requirements.txt` during implementation and
declare it consistently in `pyproject.toml`; no dependency change is made here.

Use a row-cursor `DataTable`, stable `add_row(..., key=str(item_id))` keys,
`HeaderSelected`, and `RowSelected` for the catalogue. Textual supports
sorting with `DataTable.sort()`; sorting must cover the complete result set,
not just a visible page. Use server sorting for paginated results and preserve
the same ordering rules in the widget. See the official
[DataTable documentation](https://textual.textualize.io/widgets/data_table/).

A single `httpx.AsyncClient` handles typed Pydantic request/response models,
explicit timeouts, authentication, and bounded retries. Run reads in Textual
async workers so network waits do not freeze the interface. Use exclusive
workers for obsolete searches; serialize mutations separately so a refresh
cannot cancel an in-flight command. See Textual's
[worker guide](https://textual.textualize.io/guide/workers/).

Poll cheap DJ state initially once per second, with backoff on disconnection;
make intervals/timeouts configurable. Refresh the catalogue on demand and
when its revision changes. Fetch job status only while jobs are active.
The TUI never imports `Station`, opens `station.db`, accesses library paths,
or calls Kokoro directly. The backend owns policy and the browser owns audio.

## 4. Proposed HTTP contracts

These are **new, draft contracts**, not available routes. Implement Pydantic
models and concrete OpenAPI responses for all of them. Keep station listeners
separate from privileged operators: require a dedicated bearer token for all
`/api/admin/dj/*` routes. Store it in `PILGRIM_DJ_TOKEN`, document that name in
README.md, and never reuse `LITELLM_TOKEN`. Use the deployment's encrypted
connection or SSH tunnel for remote operation. Missing operator credentials
disable DJ mutations; public request-line behavior remains separate.

| Method / proposed path | Purpose / principal fields |
|---|---|
| `GET /api/admin/dj/state` | `{epoch, revision, server_time, on_air, upcoming, pending_commands, catalogue_revision, player_status}`. Current item includes sequence, media ID, type, title, artist, position and remaining duration. No backend probes. |
| `GET /api/admin/dj/songs` | Parameters `q, sort=title\|artist, direction=asc\|desc, cursor, limit`. Returns `{items, next_cursor, catalogue_revision}`; rows include ID, title, artist, duration and advisory eligibility/reason. |
| `POST /api/admin/dj/skip` | `{command_id, expected_epoch, expected_revision, expected_seq}`. Returns `202` and a command receipt. |
| `POST /api/admin/dj/queue` | `{command_id, expected_epoch, expected_revision, media_id}`. Queues a ready song or this character's ready phrase; returns placement or a policy conflict. |
| `GET /api/admin/dj/commands/{command_id}` | Durable command result: `accepted`, `preparing`, `scheduled`, `applied`, `rejected`, or `expired`; includes affected sequences and cutover information where applicable. |
| `GET /api/admin/dj/character` | Configured character, setup readiness, and available unused voice IDs; explicit error when voice discovery fails. |
| `POST /api/admin/dj/phrases` | `{command_id, character_id, text, cleaned_text_hash}`. Validate the confirmed text, persist a job, return `202 {job_id, status, cleaned_text}`. |
| `GET /api/admin/dj/phrases/{job_id}` | Render stage, character/voice snapshot, cleaned text, expiry, failure or `{media_id, duration_s}` when ready. |
| `POST /api/admin/dj/phrases/validate` | `{character_id, text}` → cleaned text, changes, bounds validation and hash. Pure validation; no synthesis or scheduling. |

The catalogue uses allowlisted sort expressions, bound SQL parameters and a
stable tie-breaker. Cursors bind to the filter/sort and catalogue revision;
if inventory changes, return a refresh response rather than silently omitting
or duplicating rows. Eligibility shown in the table is advisory; recheck it
at the proposed insertion time when processing the command.

Example, with illustrative identifiers:

```json
{
  "command_id": "eb80a30d-53f8-4e57-8323-d1497bdbbcbe",
  "expected_epoch": "station-start-id",
  "expected_revision": 18,
  "media_id": 412
}
```

Each mutation is idempotent by command ID and payload: identical retries
return the existing result, and reuse with different content returns `409`.
Validate this before stale-state checks so a lost response can be recovered.
Use `401/403` for authentication/authorization, `404` for missing IDs,
`409` for stale state, ineligible items or conflicting commands, `422` for
invalid input, `429` for bounded queues, and `503` for unavailable required
services. Errors include a stable code and readable explanation. An accepted
command is not a claim that a listener has heard it.

## 5. Scheduling and browser coordination

Proposed scope: DJ actions change the **shared station programme** for
connected, compatible listeners. This is a decision to adopt in RADIO §§3,
5.3, 9.2, 11, not a behavior to infer from current heartbeat schemas.

### Revision protocol

Add a station `epoch` (changes on restart) and a programme `revision` (changes
when already-published future playback changes). Ordinary append-only
lookahead may retain its revision. Extend the public programme response with
these fields and support `GET /api/station/program?epoch=E&revision=R&after_seq=N`.
When E/R do not match, ignore the obsolete cursor and return a full current
snapshot with `reset: true`, start offset, and server time. Sequence numbers
identify occurrences; never reuse an old sequence for a different item.

Provide a cheap public `GET /api/station/state` returning the epoch/revision,
current sequence and offset, server time and any prepared cutover. Browser
polling must run independently of its buffered programme length. Specify both
new/extended public contracts in RADIO §11 with typed models.

### Applying a command

1. Serialize commands with the scheduler's normal commit loop. Validate the
   epoch, revision, current occurrence, item availability, and all constraints.
   Build a replacement tail from already rendered inventory; do not make
   production calls while holding the scheduler lock.
2. Preserve the current item for queue-next; for skip, propose a near-future
   cutover to an eligible ready successor. Publish a **prepared** replacement
   with a cutover ID, target revision, media list and proposed effective time.
   Keep the active programme valid while preparation is pending.
3. Compatible browsers fetch/decode the replacement within their current +
   next-two budget. Extend heartbeats with player ID, protocol version,
   observed epoch/revision, and `prepared_cutover_id`. Readiness reports
   describe buffers; they never control production.
4. After a bounded preparation window, finalize a cutover time with enough
   configured lead for notification. Players map server time to the existing
   AudioContext clock, schedule the successor, apply a brief configured
   anti-click fade and stop the skipped source at that same audio boundary.
   For queue-next, keep the natural current-item end. If preparation misses
   that boundary, report a deferred placement instead of interrupting audio.
5. Atomically activate the revised programme and command receipt at the
   cutover. Keep history of superseded occurrences; replace affected future
   occurrences with new sequence IDs. Reset the timeline anchor and recompute
   coverage, context, reservations and SFX windows from the retained history.
   Refill lookahead from ready inventory only.

When an unstarted intro/song block moves intact and its context is still
valid, transfer its one-shot reservation to the replacement occurrence in
the same transaction. It remains one planned airing. Cancelled blocks and
contextually invalid speech lose that reservation permanently; do not put
their intros back in inventory. Preserve request-to-song linkage across a
valid move so the request completes only when its song actually starts.

Only one cutover is prepared at a time; later edits refresh/rebase after it.
If the current segment ends during skip preparation, expire the skip as stale
instead of skipping its successor. The deadline and all scheduling arithmetic
use injected Clock/RNG (RADIO §§0, 5.1, 15).

No remote client can guarantee instantaneous, sample-synchronous changes
across a network. Target an acknowledged skip within a configurable two-second
budget for healthy clients, verified with the actual FLAC fetch/decode path.
If required audio is unavailable, leave current audio running and report the
delay/failure. A lagging or disconnected listener may temporarily remain on
old buffered audio and rejoin at the latest offset after recovery; never
silence everyone waiting for its acknowledgement. With no active listeners,
the server can apply a validated edit without waiting for a browser.

On a revision change the browser must cancel obsolete scheduled sources,
stop their SFX, release obsolete buffers, and ignore stale fetch/decode
completions. Hold explicit source references by occurrence; current
`scheduleOne()` does not retain them for later cancellation. Keep playback
labels and heartbeat events tied to the audio-clock start, not command
acceptance. Normal joins remain sample-contiguous; intentional skip fades
are measured separately from underruns (RADIO §9.2).

### Rules that manual controls still enforce

- Revalidate spacing, genre, maximum non-song runs, news expiry, serious-news
  adjacency and words between songs against the **new** timeline (RADIO §5.2).
  Shortening playback can make otherwise valid future repeats too close.
- Keep each unused intro attached to its song. Invalidate contextual speech
  whose neighbours or time reference changed; bridge with an existing ready
  liner/station ID when appropriate (RADIO §§5.3–5.4). No synchronous rewrite.
- News, DJ talk, intros, field reports and operator phrases remain one-shot.
  Cancelled or skipped one-shot occurrences cannot return to the random
  pool; invalidate them instead of resetting `fresh` to make them reusable.
  A reservation transferred within the same edit is not a second airing.
- Do not mark bypassed songs/requests as heard. Record started-then-skipped
  separately from never-started/superseded occurrences, and keep actual
  listener heartbeat records distinct from scheduled rotation reservations.
  Release/recompute reservations without erasing prior real airplay. A
  cancelled request occurrence must not become `aired` merely because the
  playhead moved past its old sequence.
- SFX stop with their host; recalculate the stinger budget at new start
  times. Never air a sidecar as a standalone segment (RADIO §§4, 5.2, 9.2).
- Skipping creates control/history records; it never retires, deletes or
  modifies library media or emergency inventory.

## 6. New character and phrase production

Create one configured operator-only character, with a stable ID, display
name and reserved voice. No random selector weight or inventory refill target
is assigned to it. The operator must choose its name and **listen before
choosing the voice**, per RADIO §10 and AGENTS §9. This plan assigns neither.

An “unused” voice means one present in Kokoro's voice catalogue and absent
from every existing configured role and other character reservation. Current
distinct role voices are `am_liam`, `am_michael`, `af_aoede`, and `bm_lewis`;
compute the exclusion set from config rather than hardcoding those IDs.
Use documented `GET /voices` during future setup, with bounded timeouts and
an explicit unavailable state. This plan did not call it. If no voice remains,
disable submission and explain why. Reject configuration collisions and
revalidate reservations before rendering; never silently substitute a voice.

Add a dedicated `operator_phrase` content type, role and manual insertion
path (RADIO §§4, 5.2, 6.3, 10). It counts as a voice bridge and non-song
segment. Conservatively give it DJ-like news adjacency restrictions and hold
it after serious news until a song separates them. No automatic SFX in v1.

Production sequence:

1. Validate character, input limits and raw text. Reject URLs, empty text
   after cleanup and terminal/control escape sequences. Run the mandatory
   TTS cleanup and show exactly the text to be spoken. Hash that result with
   the character/config version; rendering requires the operator-confirmed
   version so a validation race cannot change the spoken words.
2. Persist a bounded job and return immediately. A background worker freezes
   the voice/speed/cleaned-text snapshot and renders using the existing
   Kokoro client. `docs/backends.md` §3 records **GET** `/tts` with query
   parameters `text`, `voice`, `speed`, `format=wav`; do not replace it with
   an assumed POST API. Existing chunking handles long sentences.
3. Reuse voice QC, calibrated duration bounds, two-pass normalization to
   −16 LUFS / −1 dBTP and FLAC delivery (RADIO §§6.3, 8). Validate both before
   and after normalization. Calibration and tolerance discrepancies in the
   existing renderer must be resolved against RADIO §6.3, not copied blindly.
4. Render in a per-job staging directory outside `pilgrim/library/`, using
   unique filenames. Current `VoicePipeline.render()` shares `_render_tmp.wav`
   and cleans files inside its media directory; factor out a staging-safe
   literal-text render path before introducing concurrent jobs. Publish a
   **new** immutable FLAC only after QC succeeds, then expose its inventory
   row/job as ready. Never overwrite or remove existing library files.
5. Let **Queue phrase** invoke the same scheduling service as song selection.
   Recheck readiness/expiry, reserve it once, and place it at the earliest
   legal boundary. Failed or unfinished jobs never enter the committed
   programme and never block existing production or playout.

Proposed lifecycle: `queued → rendering → ready → scheduled → aired`, with
`failed`, `expired`, and `skipped` terminal outcomes. A cancelled scheduled
phrase becomes terminal; it is not replayed automatically. Keep unqueued
ready phrases for a configurable short TTL; recheck expiry at predicted air
time. Record original text, cleaned text, character, voice, QC result, media
ID and failure reason. Display backend errors without credentials or raw
request URLs containing the full phrase.

Use bounded Kokoro concurrency and retries. Preserve existing deadline-bound
contextual/news work, then service operator jobs ahead of routine stock
refills without starving them. On restart, recover unfinished render jobs
without duplicate publication; previously scheduled one-shot phrases whose
airing cannot be established become expired/uncertain rather than auto-replayed.

## 7. Configuration and storage changes to review

Keep endpoint addresses and operating limits in `pilgrim/config.yaml` and
validate them in `pilgrim/config.py` (RADIO §12). Proposed `dj` configuration
covers enabled state, console API base URL, polling and request timeouts,
cutover lead/deadline/fade, queue and phrase bounds, phrase TTL, worker limits,
and character ID/name/voice/speed/calibration. Ship control mutations disabled
until access setup is complete; enable phrases only after character setup.
The token itself stays in the environment. A bind address is not necessarily a usable remote client URL.

Draft persistence additions, to specify in RADIO §14 before migration:

| Area | Required record |
|---|---|
| Operator commands | Unique command ID, payload hash, accepted epoch/revision, operation, outcome, timestamps, applied revision and affected occurrences. |
| Phrase jobs | Original/cleaned text, config/voice snapshot, attempts, status, media link, expiry and failure. Unique publication per job. |
| Programme edits | Revision/cutover metadata and superseded occurrence history; stable occurrence identities and optional intro/song grouping. |
| Rotation accounting | Separate future reservations from actual start/partial-play history, including cancellation and restart recovery. |

Keep existing `items.id` as media identity. The new type can use existing
metadata fields for character/job references, but lifecycle uniqueness needs
database constraints, not only JSON flags. Migrations are additive and tested
against temporary databases; the TUI never runs them. Before changing the
live database, prepare and review the migration and recovery procedure as
part of implementation. Do not delete `station.db` or modify the emergency
pack. No schema migration is part of writing this document.

## 8. Delivery and acceptance

Proposed implementation ownership (all application code under `pilgrim/`):

| Files / area | Change |
|---|---|
| `server.py`, new `dj_api.py` | Authenticated routes and typed contracts; delegate commands rather than mutate the scheduler inside handlers. |
| New `dj_control.py`, `scheduler.py` | Serialized command handling, prepared cutovers, policy checks and revision-aware lookahead. |
| `store.py` | Additive migrations, command/job persistence, occurrence history and rotation reservations. |
| New `pipelines/operator_phrase.py`, `pipelines/voice.py`, `producer.py` | Literal-text jobs, reusable staging-safe audio processing and bounded background work. |
| `web/player.js` | Independent revision polling, source cancellation, readiness reporting and audio-clock cutovers. |
| `tui/`, `config.py`, `config.yaml` | Operator interface, shared HTTP client, validated settings and character reservation. |
| `tests/`, Makefile, dependency files, README.md, RADIO.md | Fake-backed coverage, launch command, pinned Textual dependency and updated contracts. |

Work in this order, with a reviewable contract and tests at each stage:

1. **Contract reconciliation:** resolve the clock/heartbeat discrepancy and
   manual priority; update RADIO §§3–7, 9–12, 14, 18 where needed. Add typed
   API models, operator authentication and the migration design.
2. **Read-only console:** catalogue/state endpoints, Textual layout, search,
   sorting, reconnect behavior and configuration. Validate against a fake
   station first; no live control feature is enabled yet.
3. **Playback control:** revision-aware browser, scheduler command service,
   skip and queue-next, accurate reservation/accounting changes. Verify both
   backend and browser before enabling mutation routes. Old browser tabs do
   not understand revisions: deploy client support first and report old
   clients as unsupported rather than claiming they followed a skip.
4. **Character phrases:** human voice selection, literal-text renderer,
   background jobs, QC/normalization, one-shot scheduling and TUI composer.
5. **Integration:** fake-backed rehearsal and reviewed deployment. A real
   listening/smoke session is separate, explicitly scheduled work; this plan
   authorizes no real generation, station restart or live skip.

Required tests (RADIO §15):

- **API/DB:** typed schemas, auth, pagination/sort consistency, ineligible or
  retired media, optimistic concurrency, duplicate clicks and lost responses,
  restart recovery, and no duplicate command or phrase publication.
- **Scheduler simulation:** deterministic skip/queue at boundaries, intro/song
  grouping, contextual invalidation, emergency fallback, all spacing/gravity
  constraints after compression, correct request outcomes, no one-shot
  recycling, and uninterrupted coverage during Kokoro failures.
- **Voice:** literal copy bypasses LLM, cleaned text confirmation, unused-voice
  enforcement, chunk order, concurrent staging, failed QC, normalized FLAC,
  expiry and a worker outage. Use fake Kokoro exclusively.
- **TUI:** correct song after sorting, keyboard/header sorting, Unicode and
  duplicate/missing names, input focus, small terminals, reconnect/stale state,
  repeated actions and job progress. Use Textual's headless `App.run_test()`
  and Pilot with fake HTTP transport; see the official
  [testing guide](https://textual.textualize.io/guide/testing/).
- **Browser:** skip while two items are already scheduled; cancel SFX and
  stale decode completions; apply a tail revision and restart epoch; recover
  delayed/missed polls; test multiple healthy/slow listeners and unsupported
  clients. Assert normal joins within one sample and zero unexpected
  underruns. Test the deliberate skip fade separately.

Run the repository gates in order: `make lint`, `make test`, `make sim`,
`make e2e`. Tests use temporary storage and fake backends; no new tests are
needed merely for this documentation change. Record implementation results
and RADIO section references at each milestone.

## 9. Decisions still needing the operator

| Decision | Recommended proposal |
|---|---|
| Shared versus individual playback control | Shared station controls, matching current code; document the change to RADIO §3. |
| Manual selection priority and repeat limits | FIFO manual queue before automatic selection; retain spacing, gravity and one-shot rules; show rejected selections clearly. |
| Character identity and sound | Operator chooses a new name and an unused Kokoro voice after listening. No default assigned by this plan. |
| API/config/database contracts | Review the drafts in §§4–7 before implementation and amend RADIO.md in the same implementation change. |

The only repository addition for this planning task is `TUI.md`, at the
user-requested root location. Existing uncommitted work is outside this change.


## 10. Planning-time validation baseline

Ran the required gates in order on 2026-10-01. These results describe the
existing checkout, not an implemented TUI. Establish a green baseline before
starting feature milestones; do not weaken the failing checks.

| Check | Result |
|---|---|
| `make lint` | Ruff passed; mypy failed with 3 undefined-`CFG` errors in `sfx_test/generate_suite.py` at lines 123, 141 and 145 (50 source files checked). |
| `make test` | 183 passed, 0 failed; 41 existing deprecation warnings. |
| `make sim` | One 24-hour run failed 1 reported assertion: song 114 went 6.9 hours between airs against a 6.0-hour starvation guard. Minimum coverage remained 600 seconds; 0 song gaps without words. |
| `make e2e` | 9 passed, 0 failed, against an isolated fake station on an OS-assigned port. |

The lint and simulation failures were present with no application changes
from this task. Only the live OpenAPI schema was requested; no production
backend request, live control, media mutation, or restart was performed.

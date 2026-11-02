# ANNOUNCER_OVERHAUL_PROPOSITION — discourse, clock accuracy, self-promotion

> **2026-11-01.** Diagnosis of the station's between-song discourse plus a
> proposed overhaul. **No production behaviour is changed by this document.**
> RADIO.md stays the normative spec (§4, §5.2, §5.3, §6.1, §6.3, §10, §11,
> §12, §15); this file is the plan and the rationale. Work it top to bottom;
> each step is small and independently testable against fakes (AGENTS §2).

---

## Review revisions (2026-11-01, operator + review) — supersede the body

Where this section and the body below disagree, this section wins.

1. **Greetings.** Drop a greeting only when the *same voice* spoke in the
   preceding non-song item. A new voice (news reader, field reporter) keeps
   its greeting, filled from the air time so it is never wrong. Station-name
   mentions are normal radio; never suppress them.
2. **Clock: per-bucket variants picked at commit (replaces A3's predicted
   tail).** The LLM writes the copy once with `[[clock]]` and no time. The
   producer renders 2–3 variants, one per upcoming 5-minute bucket ("about
   ten past six", "about quarter past six", …; Kokoro ~0.2 s each). At
   commit the scheduler picks the variant whose bucket matches
   `_slot_wall()` of the slot — known exactly then. On a request splice that
   moves the slot, swap to the matching variant (the splice already bumps the
   revision). Error = the 5-minute rounding only. The time MCP's "now" is the
   wrong input: audio is committed 300 s ahead and sits in stock first.
   The template stays on the shelf until a variant is committed; only the
   cheap renders are discarded.
3. **A1 stands** (expiry against the air slot; expiry-aware `build_state`
   count). Stamp `expires_at` after the LLM call, not before.
4. **Newest first.** `_pick_fresh_talk` (and promos) take the newest
   unexpired clip, not `rng.choice`. Consider newest-first for new
   commercials too.
5. **Word bans scoped.** Relative day words and spelled-out clock times are
   banned only in DJ talk, time checks and promos, and only when they are not
   the code-supplied phrase. News and commercials are exempt (news has its own
   TTL); `tts_cleanup` keeps rewriting times for commercials.
6. **Day-level facts** ("Sunday night", "the first of November", campaign
   timing) are computed in code from the station timezone, used verbatim, and
   the clip expires at the next local midnight.
7. **`time_check` text is code-written** (a few variants around `[[clock]]`),
   no LLM.
8. **Requests are already announced** (intro spliced before every request
   song; 327/327 aired requests consumed theirs). The 0-of-1,881 figure counts
   DJ breaks only. Add: a just-aired request is required context for the next
   DJ break (back-announce); a ready-but-waiting request gets a "coming up"
   tease; the request-line plug may cite real recent counts. Never a name or
   email.
9. **B3 airtime-weight conversion: dropped.** Reading the current weights as
   airtime shares would cut songs to ~55% and triple commercials. Keep the
   soft talk floor and nudge `dj_talk` by hand.
10. **B2 motifs:** a config list of rationed motifs (sprinkler, barn, "we
    just", "welcome back") with a minimum gap, string-matched. No NLP.
11. **B5 intros:** per-song named intros from code-written variants ("Here's
    [[title]] from [[artist]]"), rendered once per song, no LLM, recycled like
    liners (RADIO §4 must class them evergreen).
12. **C6 admin promo endpoints: deferred.**
13. **Target spread per hour:** songs ~49 min (~24); commercials ~3.5 min;
    DJ breaks 6 × 18 s; named song intros ~14 × 5 s; request intros as
    fulfilled; time checks 4 × 4 s (one at :00); IDs/liners 6 × 5 s; news
    2 × 19 s; field 2 × 18 s; promos 1–2 × 12 s. ≈ 6 min voice/h, songs ~80%.

    > **Amended 2026-11-01 (reviewer, same day):** the arithmetic above is
    > wrong in two places. **Songs are the remainder, not a target:**
    > `60 − ~6.1 voice − 3.5 spots = 50.4 min ≈ 27 songs` at the measured
    > 110.3 s. And **DJ breaks should not drop:** 6 × 18 s is 1.8 min/h
    > against a measured baseline of 2.6 min/h (11.7 breaks/h × 13.4 s).
    > Start at **8 × 20 s** (parity, tunable), with 10 × 20 s (+27%) the
    > option if more Liam is wanted. See B0.

### Operator decisions (2026-11-01, Justin) — final

1. **DJ spot count: unchanged.** No count target in `hour_budget`; the
   selector's existing weight keeps producing ~11.7 breaks/h. `target_s` stays
   18. (Airtime parity with today.)
2. **Second host: approved.** American, woman. Voice id is still a listening
   decision (AGENTS §9) — candidates rendered for the operator's ear; config
   ships the chosen id, with a provisional default clearly marked.
3. **Liners: `liners_per_bucket: 4`** (20 liners), not 5.
4. **Thanksgiving angles: approved.** We are pandering; lean in.
5. **No Thanksgiving song steering.** Stock songs stay general; listener
   requests for Thanksgiving songs are fulfilled normally.
6. **Time checks: 4/h at :00, :15, :30, :45.** Not "roughly hourly" — the
   draw targets those quarter marks within a window.
7. **Go.** Implement in the revision block's order.

### Thanksgiving (replaces C1–C5 where they differ)

- Light touch: the audience is small. Timing is **optional**; most promos
  just say Thanksgiving is coming to Pilgrim Dot Farm.
- When timing is mentioned, code supplies the colloquial phrase from days to
  2026-11-26 (station tz), used verbatim; the clip expires at local midnight:

  | Days out | Phrase |
  |---|---|
  | 22+ | "coming up" |
  | 15–21 | "a few weeks away" |
  | 8–14 | "a couple weeks away" |
  | 5–7 | "about a week away" |
  | 3–4 | "a few days away" |
  | 2 | "a couple days away" |
  | 1 | "tomorrow" |
  | 0 | "Happy Thanksgiving" |

- No spoken calendar date; no `require_date_every_n`.
- Ramp: ~1 promo/h, 2/h in the final week, a few extra on the day (gravity
  rule respected after news). Every promo names "Pilgrim Dot Farm" and
  "Thanksgiving" (code-checked).
- On the day: turkey-census field report, Thanksgiving DJ topics, "a song for
  the table" request plug. Campaign off after 11/26; leftovers retired.
- Decoupled from the clock templates: ships right after A1 so the ramp starts
  on time.
- Open: optionally steer 6–10 station songs (`prompts/station_song.md`)
  toward Thanksgiving — operator decision.

### Order

1. A1 + newest-first + expiry stamped after the LLM call.
2. Campaign config, `promo` type, promo prompt + validator, request-line plug.
3. `time_check` (code text); stop passing the clock to DJ talk.
4. Request back-announce / tease context; B1 rotation; B4 liners; B2 motifs.
5. B5 named intros.
6. Clock variants for any DJ copy that still wants `[[clock]]`.

---

## Notes on the revisions (agent, 2026-11-01)

These revisions were added after the body was drafted. **They win**; this
section is the implementation caveats plus the arithmetic I would still check.

### Agreed, with reasons

- **#2 per-bucket variants beat A3's predicted tail.** The slot is only known
  exactly at commit, so guessing it was the weak point in my draft. Rendering
  2–3 variants at ~0.2 s each is cheaper than the drift it removes, and a
  splice already bumps the revision, so the swap has a hook.
- **#4 newest-first** fixes a real defect: `_pick_fresh_talk` draws uniformly
  from a shelf where the producer makes ~15 clips/hour and the station airs
  ~8.6, so a 14-minute-old clip is as likely as a fresh one.
- **#5 scoped bans.** Commercials legitimately say "open till six", and
  `tts_cleanup` rewriting that is correct. Only roles whose copy must be
  re-filled need the ban.
- **#6 midnight expiry** is the right granularity for day-level facts; a
  `[[day]]` token filled at commit would need re-rendering at every midnight
  anyway.
- **#7 code-written time checks** removes an LLM failure mode from a 4-word
  clip.
- **#8 requests are already announced.** Verified: 329 request intros exist,
  all 329 consumed (`fresh=0`). My "0 forward-looking" framing was too broad
  (§2.5 is corrected).
- **#9 dropping the airtime conversion.** The arithmetic agrees (B0): the
  configured weights are unreachable, so converting them would only move the
  problem, not fix it.
- **#10 config motif list, string-matched.** Right call — my motif gate was
  over-engineered for a soft signal.
- **#11 named intros from code-written variants** is strictly better than
  genre-generic intros: every song gets its own name said, and no LLM.
- **Thanksgiving light touch.** `require_date_every_n` would have produced
  clunky date reads; the days-out table is better radio.

### Arithmetic (corrected)

1. **DJ baseline.** My earlier "~16/h, 3.6 min/h" was a double-count: I scaled
   the 55 airings up to the 6.4 h wall run and then divided by the 4.69 h of
   *observed program* again. The correct rate is what §2.1 reports:
   **55 / 4.69 h = 11.7 breaks/h × 13.4 s = 2.6 min/h**. Unobserved time
   changes the count, not the rate.

   | Allocation | Airtime | vs today (2.6 min/h) |
   |---|---|---|
   | 6 × 18 s (revision 13) | 1.8 min/h | **−31%** (a cut) |
   | 8 × 20 s | 2.7 min/h | parity |
   | 10 × 20 s | 3.3 min/h | +27% |

   So 8 × 20 s is a *floor*, not a win. If the operator wants more Liam, 10 ×
   20 s is the honest number. All tunable.
2. **Songs are not a target — they fill the remainder.** At 110.3 s average:
   `60 − ~6.1 voice − 3.5 commercials = 50.4 min ≈ 27 songs`. Revision 13's
   "~49 min (~24)" was wrong on both the minutes and the count.

### Implementation caveats

- **Bucket variants need a shelf-life rule.** A template can sit for its full
  TTL; variants rendered for buckets that have passed are worthless. Render the
  current bucket + next 2 (15 min, matching `time_mention_ttl_s`) and re-render
  on bucket roll; cap at 3 variants per template.
- **Skips move slots too, not just splices.** An operator skip renumbers the
  tail exactly like a request splice, so the variant swap must hang off the
  revision bump, not off the splice code path.
- **Newest-first for commercials: I'd keep random.** Commercials recycle on
  purpose and carry a 30-min spacing; newest-first would starve the 20-spot
  library and fight the spacing rule. "Consider" is right; the answer is no.
- **Thanksgiving date.** Decided: the 26th stays unspoken; timing comes from
  the code-supplied days-out phrase. A `date_label` override exists but is
  **off by default**.
- **#2 RADIO §5.3.** Decided: amend §5.3 to the bucket-variant pull model. No
  plan table, no schema change.
- **#4 `max_consecutive_non_song`.** Decided: leave at 2.
- **A1 "stamp `expires_at` after the LLM call"** applies only to copy that
  still carries a day-level fact (#6). Time-free templates have no expiry.

### Body steps superseded by the revisions

| Body step | Superseded by |
|---|---|
| A2 word bans (all roles) | #5 (scoped to DJ talk / time checks / promos) |
| A3 predicted tail + `meta.air_at` | #2 (per-bucket variants at commit) |
| A4 `time_check` as a template | #7 (code-written text, no LLM) |
| A5 greetings (drop entirely) | #1 (same-voice rule; new voices keep theirs) |
| A6 `[[day_part]]` fill | #6 (day-level facts, midnight expiry) |
| B2 motif gate | #10 (config list, string-matched) |
| B3 `weights_by_airtime` | #9 (dropped; keep the soft talk floor) |
| B5 generic genre intros | #11 (per-song named intros) |
| C1–C5 Thanksgiving | The Thanksgiving block above |
| C6 admin promo endpoints | #12 (deferred) |

B0, B1, B4 and B6 still stand: B0 is the diagnosis behind #9 and #13; B1's
context rotation is the vehicle for #8; B4's liner pool is unchanged; B6's
`weather`/`town_notice` stay `liner` kinds (they are not in the hour budget, so
treat them as optional).

---

## 0. The three verdicts

1. **Variety is thin — but the problem is structural, not literal.** The LLM
   writes 1,880 unique texts, so nothing repeats word for word. What repeats is
   the *recipe*: every clip gets the same two ingredients (the last three songs
   + the clock), the same 18 s target, the same shape (station name → time →
   sprinkler/barn → back-announce), and the same motifs. 51% of the last run's
   DJ breaks mentioned the sprinkler; 37% said "we just"; 0 of 1,881 ever
   mentioned the request line; only 6.6% looked forward at all.
2. **The clock is right; the airing is wrong.** `spoken_time()` is
   timezone-correct (`America/Detroit`) and the phrasing is good. But the time
   is frozen at *production* and only checked at *commit*, so clips air a
   **median of 12 minutes late**, 66% more than 10 minutes late, worst 25
   minutes. Nearly half of all DJ airings landed after their own
   `expires_at`. Every single DJ break also reads the clock, which no real
   station does. News says "good morning" at noon in 29% of airings; field
   reports do the same in 30%. **The fix is cheap because Kokoro is fast:**
   let the LLM write a *template* with a `[[clock]]` token; the producer renders
   one cheap variant per upcoming 5-minute bucket (~0.2 s each) and the
   scheduler commits the variant whose bucket matches the actual air slot.
   Measured on the live box, Kokoro renders 16.25 s of DJ copy in **0.22 s** —
   so the slow LLM leaves the critical path and the clock becomes accurate to
   the rounding.
3. **There is no self-promotion at all.** The only station ID brief asks for
   the name plus "you're tuned in". There is no promo content type, no campaign
   calendar, no request-line plug, no "coming up" tease, and nothing in the
   code or config knows that **Thanksgiving, 2026-11-26** exists. Today the
   station airs **zero** promos per hour with 25 days of runway.

---

## 1. Method

Read: `pilgrim/scheduler.py`, `pilgrim/selector.py`, `pilgrim/producer.py`,
`pilgrim/pipelines/voice.py`, `pilgrim/pipelines/clocktime.py`,
`pilgrim/pipelines/news.py`, `pilgrim/config.py`, `pilgrim/config.yaml`,
`pilgrim/prompts/*.md`, `pilgrim/store.py`, RADIO.md §4/§5/§6/§10/§15.

Measured against `pilgrim/station.db` (the live DB; the repo-root `station.db`
is a 0-byte stub). Airplay rows are 5-second listener heartbeats, so every
count below is **deduplicated by `(item_id, seq)`** = one row per airing.

Caveat: airplay only records what a listener actually saw, so the 2026-11-01
run covers 4.69 h of program out of a 6.4 h wall-clock run. Percentages are
shares of *observed* airings.

Latency probed live (2026-11-01, `192.168.68.89:8001`, `am_liam`, speed 1.0):

| Text | Audio | Wall time | RTF |
|---|---|---|---|
| 14 words ("It is about ten past six…") | 4.10 s | **0.089 s** | ~46× real time |
| 68 words (a full DJ break) | 16.25 s | **0.22 s** | ~74× real time |

For contrast, the qwen38 DJ copy call is tens of seconds and the song model is
minutes. Kokoro is not the bottleneck; the LLM is. That asymmetry is what makes
the template design in A2/A3 viable.

---

## 2. Diagnosis A — variety

### 2.1 Airtime mix (2026-11-01 run, 4.69 h of observed program)

| Type | Airings | Airtime | Share |
|---|---|---|---|
| song | 125 | 241.1 min | **85.7%** |
| commercial | 45 | 15.6 min | 5.5% |
| dj_talk | 55 | 12.6 min | 4.5% |
| liner | 56 | 5.1 min | 1.8% |
| field_report | 13 | 3.8 min | 1.4% |
| news | 9 | 2.9 min | 1.0% |
| intro | 1 | 0.1 min | 0.0% |

DJ talk is **2.7 minutes per hour**. That is the whole of the announcer's
presence, and most of it is a clock reading.

One number matters more than the rest: with `max_consecutive_non_song: 2`,
110.3 s songs and 15.3 s interjections, non-song airtime cannot exceed **21.7%**
of the program. Observed is 14.3% — **66% of the structural ceiling**. The
station is not failing to air enough interjections; it is close to the maximum
its own run-limit rule allows (B0).

### 2.2 The discourse is one of five shapes

Gaps between consecutive songs in the same run:

| Gap pattern | Count |
|---|---|
| `dj_talk` | 29 |
| `liner` | 19 |
| `commercial → dj_talk` | 19 |
| `commercial → liner` | 17 |
| `news → liner` | 8 |
| `field_report` | 6 |
| everything else | 14 |

So ~70% of the talk between songs is one of: a lone DJ break, a lone liner, or
a spot followed by one of the two. That is not a bug in the selector; it is
the direct consequence of `playout.max_consecutive_non_song: 2` (at most two
non-song items between songs) plus a 15-item liner pool plus a DJ clip that
always says the same kind of thing.

### 2.3 The liner pool is 15 items and one of them aired 6 times

- Config: `liners_per_bucket: 3` × 5 buckets = **15** liners. RADIO.md §6.1
  says **≥ 5 per bucket = 25**. The config under-ships the spec (a deviation
  worth closing on its own).
- 56 liner airings in 4.69 h from 15 unique items → 3.7 plays each, max **6**
  plays of a single liner. The scheduler's no-repeat window is 10 committed
  items, so with 15 items in rotation the same ID returns within ~10 slots.
- Station IDs: **3** (emergency only, never deleted).

### 2.4 The DJ copy is templated by the context it is handed

`Producer.ensure_dj()` passes exactly two things: the last three committed
songs and the current clock. Nothing else. Motif shares:

| Motif | Live run (96 clips) | All time (1,880) |
|---|---|---|
| "sprinkler" | 49 (51%) | 1,058 (56%) |
| "we just …" | 36 (38%) | 694 (37%) |
| "barn" | — | 606 (32%) |
| "Welcome back" | 24 (25%) | 354 (19%) |
| "This is Pilgrim Dot Farm" | 8 (8%) | 103 (5%) |
| mentions the request line | **0** | **0** |
| mentions "coming up"/"next up" | — | 125 (6.6%) |
| mentions a weekday | — | 19 (1%) |
| mentions weather | — | 93 (5%) |

Texts are 100% unique (1,880 distinct of 1,880), so the ear is not hearing
repeats — it is hearing the *same paragraph* with different nouns. That is
what "a tiny bit lacking" sounds like.

### 2.5 Forward-looking talk exists, but not for recycled songs

Correction to an earlier draft of this section: request songs *are* announced.
329 request intros exist and all 329 are consumed (`fresh=0`), so every aired
request got its intro. The 0-of-1,881 figure in §2.4 counts **DJ breaks only**.

The real gap is narrower and still worth fixing: a song's *first* airing has an
intro, but once it recycles — which is 85% of song airtime — it airs bare. In
the 2026-11-01 run: 125 songs aired, **1 intro**, because the library was
recycling. No recycled song gets its name said, and `dj_talk` cannot point
forward (the producer cannot see the next pick).

### 2.6 Selector weights are per-item, not per-second

`playout.weights` gives `dj_talk: 0.12`, but a DJ clip is ~13 s and a song is
~110 s. A 12% draw rate produces 4.5% of airtime. Every weight in §5.2 has this
skew; commercials (0.15) are 21 s, liners (0.10) are 5 s. Tuning weights by
feel therefore never lands where the operator expects.

### 2.7 The producer overproduces and throws away 43%

96 `dj_talk` were produced in the 6.4 h run; 55 aired. 41 clips (43%) expired
unused — each one an LLM call plus a Kokoro render. `inventory.dj_talk_min: 2`
plus a 6-second producer cycle tops the shelf up faster than a 15-minute TTL
can drain it, so the shelf is permanently stale: **age at first air averaged
909 s** against a 900 s TTL.

### 2.8 The scheduler counts expired talk as available

`Scheduler.build_state()` uses `count_fresh_of_type("dj_talk")`, which ignores
`expires_at`, while `Producer._count_fresh_unexpired` filters it. So the
selector draws `dj_talk` because the state says it exists, `_pick_fresh_talk`
then returns `[]`, and the type is blocked for that pass. `/api/health`
(`inventory_levels` → `producer.counts()`) reports the *other* number. Two
counts of the same thing disagree.

---

## 3. Diagnosis B — the clock

### 3.1 What is already correct

- `config.station.timezone: America/Detroit`, used by both the MCP time tool
  and the `zoneinfo` fallback in `current_local_time()`.
- `spoken_time()` rounds to 5 minutes and phrases loosely ("about quarter past
  ten", "coming up on eleven"). Verified against the DB: item #7493 was
  produced at 22:33 UTC = 17:33 EST and said "about half past five in the
  evening" — **correct**.
- Time-bearing clips get an `expires_at`; `_pick_fresh_talk` refuses expired
  ones at commit time.

So the timezone, the rounding, and the phrasing are not the problem.

### 3.2 What is wrong: the time is frozen at production and only checked at commit

> The fix for all three defects below is **A2/A3**: the LLM writes a template
> with a `[[clock]]` token, and code fills that token with the real air time at
> commit, rendering with Kokoro only then (0.22 s for a full break). The
> diagnosis here is unchanged; the remedy is not "produce faster" but "stop
> baking the clock into the copy".

Three separate defects stack:

1. **The clock is read at production.** `ensure_dj()` calls
   `spoken_time(now_dt)` when the clip is *written*, not when it *airs*. The
   scheduler has no neighbour knowledge at that moment, and it produces
   **two clips per pass from one shared context and one shared `expires_at`**
   — so both say the same minute.
2. **Expiry is checked at commit, not at air.** `_pick_fresh_talk` compares
   `expires_at` with `clock.wall()` (now), never with `_slot_wall(host_start)`
   (the wall time the item will actually air). `_append` does not re-check at
   all. An item may be committed one second before expiry and air seven
   minutes later — the committed lookahead is 300 s plus up to one full item.
3. **The item sits in stock for most of its TTL before it is even picked.**
   Uniform `rng.choice` over fresh clips means a 14-minute-old clip is as
   likely to be drawn as a 1-minute-old one, and production outpaces
   consumption (2.7). Total observed age at air = stock sit time + lookahead.

### 3.3 Measured error (2026-11-01 run)

Parsing the spoken time out of each aired DJ clip and comparing it with the
listener's heartbeat timestamp (converted to EST):

| Metric | Value |
|---|---|
| clips with a parseable time | 47 of 56 |
| median absolute error | **12 min** |
| mean signed error | **+13.2 min (late)** |
| share > 5 min late | **100%** |
| share > 10 min late | **66%** |
| worst | **25 min** |
| age at first air (created → aired) | min 325 s, avg 909 s, max 1,942 s |
| airings after own `expires_at` | **480 of 976 (49%)**, max 1,088 s late |

Worked examples from the DB:

| Item | Produced (EST) | Said | Aired (EST) | Error |
|---|---|---|---|---|
| #7493 | 17:33 | "about half past five" | 17:52 | **22 min late** |
| #7495 | 17:42 | "about twenty to six" | 17:56 | 14 min late |
| #7501 | 17:51 | "about ten to six" | 18:06 | **16 min late** |
| #7505 | 17:58 | "coming up on six" | 18:09 | says "coming up on six" at 6:09 |
| #7513 | 18:18 | "about quarter past six" | 18:27 | 12 min late |

"Coming up on six" airing nine minutes *past* six is the audible failure mode.

### 3.4 Every DJ break reads the clock

1,877 of 1,880 stored `dj_talk` carry an `expires_at`, i.e. were produced with
time context. Real radio reads the clock maybe once every 10–15 minutes; this
station does it on 100% of its breaks. That is both monotonous and the source
of the accuracy problem — if a clip has no time in it, none of §3.2 applies.

Worse: the expiry is stamped **even when the copy never mentions the clock**.
In the live run, 9 of 56 clips had no time phrase and still expired, wasting
the render. The prompt says "only mention the time of day if the context gives
it" — the context always gives it.

### 3.5 News and field reports carry a greeting that contradicts the clock

| Type | Airings/items | Greeting contradicts air time |
|---|---|---|
| news | 193 airings | **56 (29%)** — "Good evening" at 11:00 |
| field_report | 458 items | **136 (30%)** — despite "never mention the clock time" |

News age at air: median **28.2 min**, max **53.9 min** (`refresh_s: 1800`, TTL
`refresh + 900`). The bulletin's "today" content and its greeting both age
inside that window. `_pick_news` also checks expiry only at commit.

### 3.6 Period boundaries compound the lateness

`_period()` flips to "at night" at 21:00. A clip produced at 20:55 saying "in
the evening" airs at 21:15 and is now wrong twice: wrong minute *and* wrong
period. `spoken_time()` computes the period from the hour, not from the air
slot.

---

## 4. Diagnosis C — self-promotion

What exists today:

- `Producer.STATION_ID_BRIEF` — "say the station name, Pilgrim Dot Farm, and
  that the listener is tuned in. Timeless." Rendered once into 3 emergency
  IDs. No promotion.
- `VoicePipeline.ROLE_INFO["liner"]` — "short station id or gag line".
- Nothing else. `grep -rn "promo|promote|marketing|campaign|event_date"` over
  `pilgrim/` and RADIO.md returns **zero** hits.

What is missing, measured on 1,880 DJ clips: request-line mentions **0**,
"coming up"/"next up" 125 (6.6%), weekday mentions 19 (1%). The station never
tells a listener what it is, what it does, or what is coming.

**Thanksgiving, 2026-11-26.** Today (2026-11-01) there are 25 days of runway
and the station airs **0** promos per hour. The hook is settled: it is a
Thanksgiving special, built from Thistledown station lore (a song for the
table, the turkey census, what we are thankful for), not real-world claims.
It needs a config-driven campaign calendar and a `promo` content type, not a
prompt tweak — and the timing has to be right on air, which is exactly the
days-out table (revision block) filled at render time.

---

## 5. The proposition

Ordered. A first (correctness), B next (variety), C last (promotion), because
promoting on top of a clock that is 12 minutes wrong would be worse than not
promoting.

### A. Clock correctness

#### A1 — Check expiry at the **air slot**, not the commit slot

- Add `Scheduler._airs_unexpired(it, host_start)`: `expires_at` must be later
  than `_slot_wall(host_start)`. No extra slack knob — the bucket variants
  already carry `expires_at = bucket_end`, and day-level clips expire at local
  midnight.
- Apply it in `_pick_fresh_talk`, `_pick_news`, and defensively in `_append`
  (log `program.expired_commit` — it should never fire).
- Fix `build_state()` to use an expiry-aware count
  (`Store.count_fresh_unexpired_of_type(type_, now)`), so the selector and
  `/api/health` agree with `Producer._count_fresh_unexpired`.
- *Why:* this is the single largest correctness bug. It converts "expiry is
  advisory" into "expiry is a hard guarantee", which RADIO.md §5.2 already
  promises.
- *Acceptance:* sim assertion — **no committed `dj_talk`/`news`/`time_check`
  airs after its `expires_at`** measured on the committed timeline (SimClock
  makes this deterministic). Unit test: an item expiring 60 s from now is not
  committed into a slot that airs in 90 s.

#### A2 — Copy carries time *tokens*, not times

The LLM never writes a clock time again. It writes a template with tokens;
the time is filled in later, by code, from the real air slot.

Token contract (`prompts/voice.md`, `prompts/news.md`, `prompts/field.md`,
`prompts/promo.md`):

| Token | Filled with | Used by |
|---|---|---|
| `[[clock]]` | `spoken_time(air_at)` (per-bucket variant) | `time_check`, `dj_talk` |
| `[[day_part]]` | "morning drive" / "midday" / "evening" / "overnight" | `dj_talk` |
| `[[day]]` | "Wednesday" / "the first of November" | `dj_talk` |
| `[[greeting]]` | "Good evening" — filled only when a *new voice* starts | news, field_report |
| `[[event_name]]` | campaign `label` ("Thanksgiving") | campaign promos |
| `[[days_until]]` | "a couple weeks away" / "tomorrow" / "Happy Thanksgiving" | campaign promos |

Revision 6 supplies `[[day_part]]`, `[[day]]` and `[[days_until]]` as
**code-computed phrases used verbatim** (station timezone, days-out table),
not LLM prose. Revision 1 means `[[greeting]]` is dropped only when the same
voice spoke in the preceding non-song item; a new voice keeps it. Revision 7
means `time_check` never sees the LLM at all — its text is a code-written
variant around `[[clock]]`.

Code-enforced, not prompt-hoped (**revision 5 scoping**):

- **Reject a bare clock time** in DJ talk, time checks and promos: `\b\d{1,2}:\d{2}\b`
  or a spelled-out clock ("six thirty", "half past six"). Today `tts_cleanup`
  silently *rewrites* `6:30` → "six thirty", which is how the baked clock gets
  through. **News and commercials are exempt** — a spot saying "open till six"
  is correct, and `tts_cleanup` keeps rewriting it.
- **Reject relative day words** ("tonight", "tomorrow", "this Thursday",
  "next week") in LLM-authored copy for the same three roles, unless they are
  the code-supplied phrase from `[[days_until]]`.
- **Require the token** where the role needs one: `dj_talk` needs at least one
  of `[[day_part]]`/`[[day]]`; a campaign promo must contain the campaign
  label.
- **`[[clock]]` must sit in the first sentence**, so the fill time is the item's
  air start (no mid-clip offset estimate needed).

Templates **do not expire** (`expires_at IS NULL`) — revision 3's "stamp
`expires_at` after the LLM call" applies only to copy carrying a day-level
fact, which expires at the next local midnight (revision 6). A template is
still one-shot — consumed when filled — so AGENTS rule 10 holds. This removes
the 43% waste: a template waits on the shelf until a slot opens instead of
dying after 15 minutes. Only the cheap bucket renders are discarded.

*Acceptance:* unit tests for the validator table (bare time → reject, token
missing → reject, relative day → reject); producer test — stored templates
have `expires_at IS NULL`.

#### A3 — Fill and render per-bucket variants, chosen at commit

*(Supersedes the predicted-tail design I first proposed; revision 2 is
better. Kept here as the rationale.)*

This is the fix for "what time they say it is versus when it airs", and it is
cheap because Kokoro is ~50–75× real time while the LLM is tens of seconds.

- **The LLM writes the copy once, with `[[clock]]` and no time.** The template
  is reusable and carries no clock, so nothing about it goes stale.
- **The producer renders 2–3 variants, one per upcoming 5-minute bucket**
  ("about ten past six", "about quarter past six", …; ~0.2 s each), stamped
  with `meta.bucket` (the bucket start) and `expires_at = bucket_end`.
  Variants cover the current bucket plus the next two (15 min, matching
  `time_mention_ttl_s`), so the shelf life is bounded even though the template
  is not.
- **The scheduler picks the variant whose bucket matches `_slot_wall()` at
  commit.** That is the exact air slot — no prediction, no `meta.air_at`
  heuristic. It commits only already-rendered items, so **nothing in the
  commit loop calls Kokoro or an LLM** (hard rule 1).
- **Moves are handled by the revision bump.** A request splice, a prepared
  cutover, or an operator skip renumbers the tail and changes the slot; the
  scheduler re-selects the variant for the new bucket, and re-renders if that
  bucket is not on the shelf. Because the swap hangs off the revision bump,
  skips get the same treatment as splices.
- **Error = the 5-minute rounding only.** `spoken_time` is loose by design, so
  a bucket match means the sentence is never more than ~2.5 min off. Compare
  with 12 min today.
- **Why the predicted tail was wrong:** audio is committed 300 s ahead and
  sits in stock first, so "now" (and even "now + coverage") is not the air
  time. The slot is only known exactly at commit.
- *Why not stitch pre-rendered audio around a fresh clock segment?* It would
  save one Kokoro call, but it buys complexity (three renders, join seams, QC
  per piece) for 0.2 s. Not worth it.
- *Acceptance:* sim assertion — every committed item containing `[[clock]]`
  speaks a time whose bucket equals its committed air slot's bucket; no item
  ever airs with an unfilled token (reject `[[` in the TTS text); a slot whose
  bucket has no rendered variant falls back to a liner and the program stays
  contiguous.

#### A4 — `time_check` as a cheap type, not a special mechanism

- New segment type `time_check`, 3–5 s, DJ voice. **Its text is code-written**
  (revision 7): a few variants around `[[clock]]` ("It's about [[clock]]." /
  "[[clock]] here at Pilgrim Dot Farm."), no LLM call at all. Never recycled.
- Because A3 does the bucketing, a `time_check` is **the cheapest item in the
  system** (~90 ms of Kokoro) and needs no separate JIT pipeline.
- Draw policy: ~4 per hour, one near :00, at most one per
  `talk.time_check_min_gap_s` (default 600). `PlayoutState` gains
  `secs_since_time_check`.
- `dj_talk` becomes **time-free by default**. That alone fixes the monotony:
  the clock stops being in every paragraph.
- *Acceptance:* sim assertion — at most one `time_check` per gap; the clock is
  read at least once per 15 min when the station is on air; zero LLM calls for
  `time_check`.

#### A5 — Greetings: same-voice rule, filled at air time

*(Superseded my "drop greetings entirely"; revision 1 is right — a greeting
from a *different* voice is normal radio.)*

- Drop `[[greeting]]` only when the **same voice** spoke in the preceding
  non-song item. A new voice (news anchor, field reporter) keeps its greeting,
  filled from the air time so it is never wrong.
- Station-name mentions are normal radio; never suppress them.
- Keep `strip_greeting()` as a deterministic fallback for copy that still
  opens with a bare "Good morning/afternoon/evening/night" with no token.
- *Why:* 29% of news airings and 30% of field reports currently contradict the
  clock. A greeting is a clock statement with no `expires_at`.
- *Acceptance:* unit test on the same-voice rule and `strip_greeting()`; sim/DB
  check — zero airings whose greeting contradicts the air time.

#### A6 — Day-level facts are code-computed, expiring at midnight

*(Revision 6 replaces my `[[day_part]]` fill-at-air idea: day-level facts are
computed once in code from the station timezone, used verbatim, and the clip
expires at the next local midnight. That is simpler than re-rendering at every
period boundary.)*

- `[[day_part]]`, `[[day]]` and `[[days_until]]` are **code-supplied phrases**,
  not LLM prose: `morning drive | midday | evening | overnight`, "Wednesday",
  "the first of November", and the days-out table for the campaign.
- The clip that carries one gets `expires_at` at the next local midnight
  (`America/Detroit`), so "Sunday night" can never air on Monday.
- `spoken_time` keeps its loose 5-minute phrasing; `talk.clock_precision`
  (`loose | minute`, default `loose`) is the only knob. Precision here means
  *staleness*, not exactness.
- *Acceptance:* unit test — a clip stamped "Sunday night" expires at local
  midnight and is not committed after it; `fill_template("It is [[clock]].",
  21:05)` never yields "in the evening".

### B. Variety

#### B0 — The airtime budget (do this arithmetic before tuning weights)

`playout.weights` are unreachable as configured, and that is worth stating
before any variety work. With `max_consecutive_non_song: 2`, an average song of
110.3 s and interjections averaging 15.3 s (measured, `station.db`):

```
max non-song airtime = 2 x 15.3 / (110.3 + 2 x 15.3) = 21.7%
observed non-song airtime                              = 14.3%   (66% of ceiling)
playout.weights sum to                                   50%
```

So the station is already two-thirds of the way to the structural ceiling, and
the configured weights ask for 2.3x more interjection airtime than the run
limit permits. Raising a weight only *displaces* another segment; it cannot add
non-song airtime. Three levers actually exist:

| Lever | Effect | Cost |
|---|---|---|
| `max_consecutive_non_song: 2 → 3` | ceiling 21.7% → 29.0% | a longer stop-set; less music. **Decided: leave at 2.** |
| longer talk-ups (13.4 s → 20 s) | same break count, more airtime | needs the copy target enforced |
| more talk *slots* (weight) | redistributes within the ceiling | squeezes spots/liners/news |

`dj_talk` currently averages **13.4 s against an 18 s target** — the pipeline
undershoots by 26% (the LLM writes short and QC only rejects, it does not
retry for length). Lengthening is the cheapest variety win.

This is why revision 9 drops `weights_by_airtime` (converting the current
weights would cut songs to ~55% and triple commercials) and revision 13 states
the hour budget directly instead. Encode that budget, not the weights — and
note **songs are the remainder, not a target**:

```yaml
playout:
  hour_budget:                    # NEW, per hour of program
    # voice ~6.1 min + commercials 3.5 min => songs fill the rest
    # 60 - 6.1 - 3.5 = 50.4 min = ~27 songs at the measured 110.3 s
    dj_talk: {count: 8, target_s: 20}    # tunable floor: 2.7 min/h = parity
    intro: {count: 14, target_s: 5}      # per-song, named (B5)
    time_check: {count: 4, target_s: 4}  # one near :00
    liner: {count: 6, target_s: 5}
    news: {count: 2}
    field_report: {count: 2}
    promo: {count: 1.5, target_s: 12}
    commercial: {minutes: 3.5}
    song: {remainder: true}
```

8 × 20 s is a **floor**, not a win: it is parity with today's 2.6 min/h. If
the operator wants more Liam, 10 × 20 s (3.3 min/h, +27%) is the honest
number.

The scheduler steers toward it the way `min_talk_per_hour_s` would: soft
nudges, never a forced draw past a constraint.

*Acceptance:* sim assertion — realized counts per hour are within tolerance of
`hour_budget`, songs fill the remainder to 60 min, and non-song airtime stays
under the ceiling; `Config` rejects a budget whose non-song share exceeds the
ceiling at the current `max_consecutive_non_song`.

#### B1 — Give the DJ something to talk about (context, rotated)

Replace the two-ingredient context in `ensure_dj()` with a rotating context
assembled from config + clock, no new backend:

- **Time**: only for `time_check` (A4). `dj_talk` gets the *day part*
  ("overnight", "morning drive", "midday", "evening") for energy, not a clock.
- **Day**: weekday and date from the injected clock → "Wednesday night",
  "the first of November". Never invented by the LLM.
- **Weather**: `talk.weather_phrases` — a small operator-written list
  ("cool and clear", "spitting rain", "warm for November") sampled with the
  injected RNG. No weather API, no new dependency.
- **Topic**: `talk.topic_bank` — a list of station-lore buckets
  (`town_notice`, `farm_lore`, `station_lore`, `listener_request`,
  `behind_the_mic`, `promo_tease`). The producer draws a topic with the
  injected RNG, records `meta.topic`, and the scheduler enforces
  `talk.topic_repeat_gap_s` (default 1,800) so the same bucket cannot return
  twice in 30 min.
- **Neighbours**: the last field report and the last news headline. **Not the
  next song** — the producer cannot see the next pick (the scheduler chooses it
  later), so a forward reference here would be a lie. Forward talk belongs to
  `intro` (B5), which is produced *after* its song is chosen. Pointing
  `dj_talk` forward needs RADIO.md §5.3's two-phase plan (scheduler fixes
  neighbours, then requests copy); that is Phase 2, not this change.
- **Request line (revision 8)**: three cases, all from real state, never a
  name or email (AGENTS rule 11 / PAYMENT §0.3):
  - a **just-aired request** is required context for the next DJ break
    (back-announce it by song title);
  - a **ready-but-waiting request** gets a "coming up" tease;
  - the request-line plug may cite real recent counts.
  Request songs already carry their own intro (329/329 consumed), so this is
  the DJ's *reaction*, not the announcement.

*Acceptance:* producer unit test — with a seeded RNG, 20 consecutive clips
cover ≥ 6 distinct topics and ≥ 3 distinct day parts; sim assertion — no topic
repeats inside `talk.topic_repeat_gap_s`.

#### B2 — Rotate structure in code; ration motifs from a config list

*(Revised: my first draft proposed a tokenizer-based motif gate; revision 10's
string-matched config list is simpler and enough.)*

- **Gate on topic and open style.** `Producer` assigns `meta.open_style` from a
  rotating list (`station_id | back_announce | scene | question | tease |
  promo_tease`) and a `meta.topic` from `talk.topic_bank`, both with the
  injected RNG; the scheduler refuses a topic that aired inside
  `talk.topic_repeat_gap_s`.
- **Ration motifs by string match.** `talk.motif_bank` (sprinkler, barn,
  "we just", "welcome back") with `talk.motif_gap_items` (default 8): a clip
  containing a banked motif that aired inside the gap is rejected and
  retried once. Plain substring match, no NLP, no stopword list.
- *Why:* AGENTS rule 10's spirit — enforce in code, not just in prompts. Today
  "sprinkler" is in 51% of breaks because nothing in code knows it was just
  said.
- *Acceptance:* unit test — no two consecutive clips share `open_style`; a
  banked motif inside its gap is rejected; sim assertion — motif reuse under
  20% and no topic repeat inside the gap.

#### B3 — Dropped (revision 9)

`weights_by_airtime` is **not implemented.** Converting the current weights
into airtime shares would cut songs to ~55% and roughly triple commercials —
a bigger change than the variety problem needs. B0's arithmetic explains why
(weights sum to 50% non-song against a 21.7% ceiling), and the fix is revision
13's explicit `hour_budget`, plus a soft `min_talk_per_hour_s` floor and
hand-nudged `dj_talk` weight. Keep the weights as per-item draw probabilities;
do not reinterpret them as airtime shares.

#### B4 — Bigger, typed, anti-repeat liner pool

- `liners_per_bucket: 3 → 5` (25 total, matching RADIO.md §6.1).
- Liner `meta.kind` ∈ `station_id | gag | promo | weather | town_notice`, with
  a per-kind target so the pool is not 25 interchangeable IDs.
- Replace the 10-item no-repeat window with `liners.repeat_gap_items: 20`, and
  forbid the same liner inside `liners.repeat_gap_s: 2700`.
- *Why:* 15 items, one aired 6 times in 4.7 h. Variety is cheapest here: five
  more renders per bucket.
- *Acceptance:* sim assertion — no liner repeats inside the gap; DB audit after
  rollout shows max plays per liner per 4 h ≤ 2.

#### B5 — Per-song named intros, code-written, recycled like liners

*(Revised: my first draft proposed generic genre/mood intros; revision 11's
named intros are strictly better — every song gets its own name said, and no
LLM.)*

- When a song is chosen, the scheduler/voice worker renders a 4–6 s intro from
  a code-written variant: "Here's [[title]] from [[artist]]." /
  "That was [[title]] — [[artist]]." Rendered **once per song**, `meta.song_item_id`
  set, **evergreen** and recycled like a liner (RADIO §4 must class intros
  evergreen; today they are one-shot).
- No LLM. The existing request-intro path stays as is (329/329 consumed).
- `_glue_intro` keeps the dj_talk-adjacency skip (no intro directly after
  `dj_talk`) and the request rule (a request's intro is its thank-you).
- *Why:* 125 songs aired with 1 intro in the last run. This is the biggest
  audible variety win per render, and it is nearly free: one Kokoro call per
  song, once, then it recycles.
- *Acceptance:* sim assertion — ≥ 14 intros per hour air, and every recycled
  song gets a named intro when its intro exists; unit test — the intro's
  `song_item_id` matches the song it precedes (the §15 assertion already
  checks this).

#### B6 — `promo` as a type; `weather`/`town_notice` as optional liner kinds

Adding segment types touches `SEGMENTS`, `INVENTORY_TYPES`,
`CALLOUT_SEGMENTS`, the sim assertions and the server rotation dicts, so keep
the count small:

- **`promo` is a real type.** It needs its own weight, its own campaign
  metadata (`meta.campaign_id`), and its own spacing rule (C2).
- **`time_check` stays a type** (A4) because it has its own draw policy
  (~4/h, one near :00) that does not belong inside `liner`.
- **`weather` and `town_notice` are `liner` items with `meta.kind`, and are
  optional.** They are not in revision 13's hour budget, so treat them as
  nice-to-have: a weather liner is 6–10 s, keyed to a day part, expiring at
  that part's end. No new segment type, no new refill loop.

### C. Station self-promotion + 2026-11-26

#### C1 — Campaign config (as revised)

```yaml
station:
  promotions:
    - id: "thanksgiving-2026"
      label: "Thanksgiving"            # must appear verbatim in every promo
      date: "2026-11-26"               # station timezone (America/Detroit)
      cta: "the request line"          # what listeners should do
      light_touch: true                # small audience: timing is optional
      schedule:                        # ~1/h, 2/h in the final week, extra on the day
        - {from: "2026-11-05", until: "2026-11-18", per_hour: 1}
        - {from: "2026-11-19", until: "2026-11-25", per_hour: 2}
        - {from: "2026-11-26", until: "2026-11-26", per_hour: 3, after_news: true}
      lines:                           # operator-approved angles, rephrased by the LLM
        - "a song for the table"
        - "the great Thistledown turkey census"
        - "what we are thankful for"
        - "the request line is open for Thanksgiving"
```

- No `require_date_every_n`, and **no spoken calendar date** by default (the
  revision block above). `[[days_until]]` supplies the colloquial phrase from
  the days-out table and the clip expires at local midnight (A6). A
  `date_label` override exists but is **off by default**.
- `PromoCalendar` (pure, over the injected clock): `target_per_hour(now)` from
  the `schedule` steps (0 outside every range), plus `after_news` on the day.
  The scheduler adds `promo` to the draw when the last hour is under target; no
  promo is ever forced past a constraint.
- *Why:* a date in code is a bug waiting for next year; a date in config is a
  campaign. Also makes the ramp sim-testable, and keeps 11/26 out of the
  prompts (AGENTS rule 4).

#### C2 — `prompts/promo.md` + a code-enforced contract

- 10–20 s, DJ voice, `evergreen=True`, `meta.campaign_id`, retired after the
  last `schedule` range ends + 1 day. `[[days_until]]` counts from `date`
  against the air date.
- Code-enforced (not prompt-hoped): every campaign promo **must contain**
  `station.name` and the campaign `label` ("Thanksgiving") verbatim.
  Case-insensitive, punctuation-normalised. Reject and retry once; on a second
  failure, drop the promo rather than air a wrong claim.
- Never let the LLM invent timing: `[[event_name]]` comes from config and
  `[[days_until]]` from the code table. Banned in promo copy: "this Thursday",
  "next week", "tonight" (A2's scoped ban).
- *Why:* the marketing beat is the whole point. A hallucinated date is worse
  than no promo.
- *Acceptance:* unit test — copy missing the label is rejected; sim assertion —
  every aired promo contains the station name and "Thanksgiving", and
  `[[days_until]]` matches the air date's row in the table.

#### C3 — Evergreen request-line plug (works with or without a campaign)

- A `promo` sub-kind `request_line`, 5–8 s, at most one per
  `promotions.request_line_gap_s` (default 2,700). Copy is a seed from
  config + the station name, and may cite real recent counts (revision 8) —
  never a name or email (AGENTS rule 11 / PAYMENT §0.3). No Stripe, no payment
  logic, no browser trust.
- *Why:* 0 of 1,881 DJ clips mention the request line. Free self-promotion that
  also feeds the paid-request funnel (PAYMENT.md).

#### C4 — Top-of-hour ID + tease

- Reuse `near_top_of_hour`: once per hour at :00 ± `top_of_hour_window_s`,
  commit a station ID followed by a 12–15 s tease of what is coming (next
  song if committed, next campaign beat, the request line). Never forced past
  a constraint; if no tease is ready, the ID alone airs.
- *Acceptance:* sim assertion — ≥ 20 of 24 hours contain a top-of-hour ID.

#### C5 — Thanksgiving day (2026-11-26)

- `schedule` gives `per_hour: 3` with `after_news: true`: a promo after every
  **non-serious** news bulletin (the §5.2 gravity rule bans any comic item
  right after a real tragedy). Config only — no code branch on the date.
- Day-specific programming, all fictional Thistledown lore:
  - the field reporter files the **turkey census** from the field (a campaign-
    tagged `field_report`, sign-off unchanged);
  - `dj_talk` draws from a Thanksgiving `topic_bank` entry ("what we are
    thankful for", "the table", "the parade that never happened");
  - the request-line plug becomes "a song for the table";
  - every top-of-hour ID names the day ("Thanksgiving on Pilgrim Dot Farm");
  - the day's first promo says "Happy Thanksgiving" (days-out 0 row).
- After 11/26 the campaign is `off`; its rendered promos are retired
  (`retired=1`) rather than deleted, so nothing recycles in 2027.
- *Open (operator):* optionally steer 6–10 station songs
  (`prompts/station_song.md`) toward Thanksgiving. Not planned here.
- *Acceptance:* sim run over 2026-11-26 — a few extra promos, one after each
  non-serious bulletin, and a campaign-tagged field report.

#### C6 — Operator surface (deferred, revision 12)

Not in this change. When it lands:

- `GET /api/admin/promotions` → campaigns, current `target_per_hour`, airings
  today, next scheduled promo. `POST /api/admin/promotions/{id}/air` → force
  one promo into the tail (same prepared-cutover machinery as an operator
  phrase).
- `/api/health` `inventory` gains `promo: {have, target, campaign_id}`.
- RADIO.md §11 + §12 updated in the same change (AGENTS rule 8).

### D. Testing

- **Sim (§15 additions):** no expired item airs (air-slot expiry); every
  committed item containing `[[clock]]` speaks a time whose bucket equals its
  air slot's bucket; ~4 `time_check` per hour and none inside
  `time_check_min_gap_s`; `dj_talk` carries no clock at all; topic repeat gap
  holds and motif reuse stays under 20%; no liner repeat inside
  `repeat_gap_s`; realized counts per hour are within tolerance of
  `hour_budget` and non-song airtime stays under the B0 ceiling; promo airings
  follow the ramp; every promo contains the station name and "Thanksgiving";
  a greeting contradicts the air time only when the same voice spoke before;
  no item ever airs with an unfilled `[[token]]`; a slot whose bucket has no
  rendered variant falls back to a liner and the program stays contiguous.
- **Fakes:** `fake_kokoro` gains a `delay_s` knob, so the "variant not ready →
  liner" path actually runs (the fake renders instantly today, which would
  leave the fallback untested until production).
- **Unit:** `_airs_unexpired`; the bucket picker (slot bucket → variant); the
  days-out table and midnight expiry (A6); the copy validator (bare clock →
  reject only in DJ talk/time checks/promos; relative day → reject; missing
  token → reject); the same-voice greeting rule; `strip_greeting`;
  `PromoCalendar.target_per_hour` at every `schedule` boundary; promo
  validator (label verbatim, no invented timing); `build_state` count parity
  with `Producer.counts()`; the `hour_budget` ceiling validator in `Config`.
- **Live:** after rollout, re-run §2/§3's queries and publish the before/after
  table (see §10). This is the acceptance test that matters.

### E. Docs

- RADIO.md §4: add `time_check` and `promo` to the type table; **class intros
  evergreen** (revision 11); document `weather`/`town_notice` as `liner`
  kinds.
- §5.2: `hour_budget`, `min_talk_per_hour_s`, time-check gap, promo campaign
  `target_per_hour`, motif rationing, liner repeat gap, newest-first for
  `dj_talk`/promos.
- §5.3: **decided — amend the doc** to the bucket-variant pull model (no plan
  table, no schema change). §5.3 currently describes plan-at-commit copy that
  the code does not implement.
- §6.1: liner target 25 (5 per bucket), named intros per song, time checks,
  promos.
- §6.3: token contract, the scoped copy ban, and the rule that `tts_cleanup`
  keeps rewriting times for commercials but rejects them for talk.
- §10: no voice changes (voice assignment is a human decision, AGENTS §9).
- §11: promotions routes (deferred); `/api/health` promo inventory.
- §12: the config blocks below.
- §15: the assertions above.

### F. Rollout

- **The first hour after deploy has no talk.** Every stored `dj_talk` (1,880
  rows) has a baked time and an expiry in the past, so A1 skips all of them.
  `seed.py` must pre-render `inventory.dj_talk_min` templates (with bucket
  variants) plus one `time_check` before the station opens, or the first hour
  is liner-only.
- **Order** (revision block): A1 + newest-first → campaign/promo → time_check
  → request context/B1/B4/B2 → named intros → clock variants for any DJ copy
  that still wants `[[clock]]`.
- **Rollback is one flag.** Leaving `talk.bucket_variants` at 0 keeps the
  legacy `time_mention_ttl_s` path, which still exists (A1 makes it safe).

---

## 6. Proposed config (additions only)

```yaml
talk:
  time_mention_ttl_s: 900       # legacy: only for copy that still bakes a time
  target_s: 20                  # 8 breaks x 20 s = 2.7 min/h (parity); tunable
  bucket_variants: 3            # NEW: current bucket + next 2 (15 min shelf life)
  topic_repeat_gap_s: 1800      # NEW
  motif_gap_items: 8            # NEW: revision 10, string-matched config list
  motif_bank: [sprinkler, barn, "we just", "welcome back"]   # NEW
  topic_bank: [town_notice, farm_lore, station_lore, listener_request,
               behind_the_mic, promo_tease]   # NEW
  weather_phrases: ["cool and clear", "spitting rain", "warm for November"]  # NEW
  field_reporter_name: Giles

playout:
  min_talk_per_hour_s: 300      # NEW soft floor (inside the B0 ceiling)
  hour_budget: { ... }          # NEW: per hour; songs fill the remainder (B0)
  # max_consecutive_non_song stays 2 (decided); it raises the ceiling from
  # 21.7% to 29.0% only if that is ever wanted (B0). Config validates the hour
  # budget against the ceiling.
  weights: {song: 0.55, dj_talk: 0.12, commercial_break: 0.15, liner: 0.10,
            news: 0.08, field_report: 0.05, time_check: 0.04, promo: 0.05}

inventory:
  liners_per_bucket: 5          # CHANGED: 3 -> 5 (RADIO §6.1 says >= 5 each)
  named_intros_min: 0           # revision 11: intros are per-song, not a pool
  promos_min: 4                 # NEW: ready promo clips outside a campaign

liners:
  repeat_gap_items: 20          # NEW (was hardcoded 10)
  repeat_gap_s: 2700            # NEW
  kinds: [station_id, gag, weather, town_notice, promo]   # NEW: meta.kind

promotions:
  request_line_gap_s: 2700      # NEW
  # campaigns live under station.promotions (C1); no date_label, no
  # require_date_every_n (revision block), timing from the days-out table
```

No database schema change is required: `items.type` is free text, and campaign
metadata rides in `meta_json` (`campaign_id`, `topic`, `open_style`, `motifs`,
`day_part`). If a plan table is chosen for §5.3 instead, that *is* a schema
change and needs RADIO.md §14 plus a migration.

---

## 7. Deviations this proposal fixes (already in the repo today)

1. `config.yaml liners_per_bucket: 3` vs RADIO.md §6.1 "≥ 5 each" — 15 vs 25.
2. RADIO.md §5.3's plan-at-commit DJ path is not implemented; the producer
   pre-renders. Per AGENTS, RADIO wins — resolve in E.
3. `Scheduler.build_state()` counts expired `dj_talk` as available while
   `Producer.counts()` does not — two inventory numbers.
4. `Clock.wall()`'s docstring says "only for expiry checks, never scheduling
   arithmetic", but `_slot_wall()` already uses it for song spacing and
   top-of-hour. Works on `SimClock`, but the comment is stale.

---

## 8. What I would NOT do

- **No audio splicing.** Pre-rendering the segments around the clock and
  stitching a fresh clock in would save one Kokoro call; Kokoro needs 0.22 s
  for a whole DJ break, so the stitching (seams, per-piece QC, three renders)
  is not worth it. Re-render the whole clip at commit.
- **No second DJ voice.** Voice assignment is a human listening decision
  (AGENTS §9). Variety comes from copy, topics, and structure, not from a
  second Kokoro voice — see Q2.
- **No new backend or API.** Weather comes from config phrases; topics from a
  config bank; dates from config. Nothing new to probe (AGENTS §2).
- **No LLM-authored dates.** The event date is a config value that the copy
  must reproduce verbatim.
- **No free-form real-world promotion.** The station's fiction stays intact;
  the campaign is station lore, not a real-world claim.
- **No change to the HTTP API or DB schema without the RADIO.md update in the
  same change** (AGENTS rule 8).
- **No touching `pilgrim/payments.py`.** Promo copy mentions the request line;
  it never touches money, never imports Stripe (AGENTS rule 11).

---

## 9. Decisions (all answered by the operator, 2026-11-01)

- **Q1 — DJ breaks: unchanged.** No count target; the selector's weight keeps
  ~11.7/h. `target_s` stays 18.
- **Q2 — Second host: shipped.** American, woman; shift-based
  (`talk.hosts`, midday/afternoon/evening). **The voice id is still yours by
  ear** — `af_bella` is provisional. Samples:
  `voice-samples/af_bella|af_nicole|af_sarah|af_river.wav`. Change one line in
  `config.yaml` to switch.
- **Q3 — Paid-priority copy: not requested.** The request-line plug is plain
  self-promotion; no payment language.
- **Q4 — Liners: `liners_per_bucket: 4`** (20 total).
- **Q5 — Thanksgiving angles: approved, pandering encouraged.**
- **Q6 — No Thanksgiving song steering.** Requests are fulfilled normally.
- **Q7 — Clock reads: 4/h at :00/:15/:30/:45.** Shipped; the sim shows
  97/day = 4.04/h, each speaking its own 5-minute bucket.

---

## 10. How we will know it worked

Re-run the §2/§3 queries after rollout and compare:

| Metric | Today | Target |
|---|---|---|
| DJ breaks per hour | 11.7 at 13.4 s (2.6 min/h) | 8 at 20 s (2.7 min/h) |
| Median clock error at air | 12 min | ≤ 2.5 min (5-min rounding only) |
| Share of DJ airings after `expires_at` | 49% | 0% |
| DJ clips produced but never aired | 43% | ~0% (templates don't expire) |
| Share of breaks mentioning "sprinkler" | 51% | < 15% |
| Max plays of one liner per 4 h | 6 | ≤ 2 |
| Songs aired with an intro | 1 of 125 | ≥ 14/h named (revision 11) |
| News greeting contradicts air time | 29% | 0% |
| Request-line mentions | 0 of 1,881 | ≥ 1 per 45 min |
| Promos per hour, final week | 0 | 2 (revision block) |
| Promos on 11/26 (Thanksgiving) | 0 | a few extra, after non-serious bulletins |

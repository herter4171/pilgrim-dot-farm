# FIX_RATINGS.md — Song ratings: community average as pink turkeys (fractional) under your cyan vote, `avg · n` always, two bug fixes

Work plan for a coding agent. Five steps, done **in order**, one commit per
step. Every code block in this file is the exact code to write unless the
step says "adapt". Do not improvise beyond what a step says; if a step
cannot be done as written, stop and report (AGENTS.md §9).

Read this whole file first. Then read, in order: `AGENTS.md`, `RADIO.md`
§9.1, §9.2, §11, §14; `VOTING.md` §1 (table V1–V13), §2, §3, §5. `RADIO.md`
is the source of truth; if this file and RADIO.md disagree, stop and report.

Each step ends with the AGENTS.md §7 checks (`make lint`, `make test`,
`make sim`, `make e2e`), a commit whose message cites the RADIO sections
named in the step, and an AGENTS.md §8 report. All work happens on the Mac
checkout at `/Users/justinherter/prod/pilgrim-dot-farm`. **Do not restart
the station, build or push an image, or touch AWS**: Step 5 ends by asking
the operator to deploy.

---

## 0. Read this first

### 0.1 What was found (investigation of 2026-11-06)

- **Ratings are retained.** Production Postgres holds 131 `song_ratings`
  rows: 99 migrated from the old `station.db` (per-day counts match exactly)
  and 32 cast on 2026-11-06 through the Fargate web tier. The lifetime
  aggregate (`Store.song_rating`) is keyed by `item_id` and is correct.
- **The UI forgets on purpose.** VOTING.md V2/V8 make a rating belong to one
  *airing* `(epoch, seq)`. `rating.js` remembers a rating under
  `pd_rating:<epoch>:<seq>`, so the next airing of the same song shows empty
  turkeys. The only trace of earlier ratings is the dim `avg · n` text, and
  it is shown only while the server says the window is open and is blanked
  the moment the window closes. Of 130 rated songs only one was ever rated
  on a second airing, so the operator has almost never seen an aggregate
  carry over. This is what reads as "not retaining".
- **Bug A (confirmed).** `pilgrim/web_app.py` `CachedScheduler` has no
  `_cum` and no `position()`, but `voting.rating_window` reads both in its
  grace branch (the on-air row has moved past the song). On the web tier a
  rating that lands after the published snapshot has moved on raises
  `AttributeError`, FastAPI answers 500, the client shows "TRY AGAIN". The
  browser plays 1–3 s behind the server (it joins after fetch + decode), so
  clicks in the last seconds of a song hit this. No test covers it.
- **Bug B (by reading).** `rating.js` `poll()` treats one state response
  whose `current_seq` differs from the audible song, or whose `rating.open`
  is false, as "the song is over" and disables the row until the next
  `pd:nowplaying`. The published snapshot is up to one publish tick old plus
  one second of `max-age=1` cache, so a client slightly ahead of the server
  loses the control for the whole song right at song start.

### 0.2 Decisions in force (operator request of 2026-11-06 + defaults)

| # | Decision | Why |
|---|----------|-----|
| D1 | **Always show the aggregate.** Every song item in `GET /api/station/program` carries `rating_count` (int) and `rating_avg` (float or null). The client paints `avg · n` the instant the song becomes audible, keeps it while the song is audible even after the server's window closes, and refreshes it from the 5 s state poll while open. With zero ratings it shows `NO RATINGS YET` | The aggregate must not depend on a poll racing the playhead |
| D2 | **Pink = the community average, always; cyan = your vote, on top.** The turkeys are filled in magenta ("pink") to the community average with **fractional fill**: turkey *i* (1-based) shows the fraction `clamp(avg - (i-1), 0, 1)` of its glyph, clipped left to right (3.4 = three full pink turkeys and 40 % of the fourth). Nothing is pink when `count` is 0. The pink is never hidden by the listener's own input. Once this browser rates the airing (or while hovering/focusing a turkey), its own level is drawn in cyan as whole turkeys **over** the pink; pink stays visible wherever the cyan does not reach. The exact `avg · n` text sits beside the turkeys. Cyan is restored per airing from `pd_rating:<epoch>:<seq>` as today | "Eyes on the community outlook"; "when the user votes, their cyan overshadows the pink" (operator, 2026-11-06). Voter-independent data, so no new API: `/api/station/state` stays a shared `max-age=1` cache (RADIO §11) |
| D3 | **The per-airing rule stays.** `UNIQUE(epoch, seq, voter_id)`, one rating per airing, window = the on-air song + `ratings.grace_s` (VOTING.md V2, V8). No schema change | Already shipped and correct |
| D4 | **Bug A fix**: `CachedScheduler` gets `_cum` and `position()` with the same meaning as `Scheduler`'s | Make `rating_window` correct over the snapshot instead of special-casing it |
| D5 | **Bug B fix**: the poll closes the row only when the server has moved **past** the song (`current_seq > seq`), the epoch changed, or ratings are off. A snapshot **behind** the playhead is ignored. A closed row keeps its paint and aggregate until the next `pd:nowplaying` | A one-second-old snapshot must never kill the control |
| D7 | **No per-song browser memory.** Nothing new goes into localStorage; the only key stays `pd_rating:<epoch>:<seq>` | The community average is the "existing rating"; a rating from an earlier airing is already inside it |
| D6 | No change to `POST /api/station/rating`, `song_ratings`, `/api/station/state` or the admin audit route | Scope |

### 0.3 Files you will touch

| File | Steps |
|------|-------|
| `pilgrim/web_app.py` | 1 |
| `pilgrim/tests/test_web_app.py` | 1 |
| `pilgrim/store.py` | 2 |
| `pilgrim/server.py` | 2 |
| `pilgrim/tests/e2e/e2e_server.py` | 2 |
| `pilgrim/tests/test_voting.py`, `pilgrim/tests/test_server.py` | 2 |
| `pilgrim/web/player.js`, `pilgrim/web/rating.js`, `pilgrim/web/style.css`, `pilgrim/web/index.html` (the five turkey buttons only) | 3 |
| `pilgrim/tests/e2e/voting.spec.ts` | 3 |
| `RADIO.md`, `VOTING.md`, `AGENTS.md` | 4 |

Nothing else. In particular: no change to `pilgrim/scheduler.py`,
`pilgrim/voting.py`, `pilgrim/config.yaml`, `pilgrim/web/img/turkey.svg`, the
`<symbol id="turkey">` in `index.html`, or any nginx file.

---

## 1. Bug A — the web tier's rating window on the grace path

**RADIO §11 (`POST /api/station/rating` over the web tier), §14; VOTING.md V8.**

### 1.1 Reproduce first

Run this from the repo root. It must print `grace path raised: AttributeError ...`
before the fix and `grace path: (True, 2928)` after it.

```
.venv/bin/python - <<'EOF'
from pilgrim.web_app import CachedScheduler
from pilgrim.voting import rating_window
program = {"items": [
    {"seq": 10, "type": "song",  "media_id": 2928, "duration_s": 180.0},
    {"seq": 11, "type": "liner", "media_id": 55,   "duration_s": 8.0},
]}
st = {"epoch": "e1", "revision": 1, "current_seq": 11, "current_offset_s": 2.0}
try:
    print("grace path:", rating_window(CachedScheduler(st, program), "e1", 10, 5.0))
except Exception as e:
    print("grace path raised:", type(e).__name__, e)
EOF
```

### 1.2 Change `pilgrim/web_app.py`

Replace the whole `CachedScheduler` class with this:

```python
class CachedScheduler:
    """The slice of `Scheduler` that `voting.rating_window` reads, rebuilt from
    the published `state` and `program` documents. `on_air()` is the station's
    snapshot (up to one publish tick old), which the rating grace already
    tolerates (VOTING.md V8). `_cum` and `position()` mean what they mean on
    `Scheduler`, in program-seconds counted from the first published row, so
    the grace branch (`position() - _cum[idx] <= grace_s`) is the published
    offset into the on-air row (FIX_RATINGS.md Step 1)."""

    def __init__(self, state: dict[str, Any], program: dict[str, Any]) -> None:
        self.epoch: str = str(state.get("epoch") or "")
        self.revision: int = int(state.get("revision") or 0)
        self.frozen: bool = bool(state.get("idle"))
        self._items: list[dict[str, Any]] = [
            {"seq": int(it["seq"]), "type": it.get("type"),
             "item_id": int(it.get("media_id") or 0),
             "duration_s": float(it.get("duration_s") or 0.0)}
            for it in program.get("items", [])]
        self._cum: list[float] = []
        t = 0.0
        for r in self._items:
            self._cum.append(t)
            t += r["duration_s"]
        seq = state.get("current_seq")
        self._on_air: tuple[int | None, float] = (
            int(seq) if seq is not None else None,
            float(state.get("current_offset_s") or 0.0))

    def on_air(self) -> tuple[int | None, float]:
        return self._on_air

    def position(self) -> float:
        """The snapshot's playhead: the on-air row's start plus the published
        offset into it. 0.0 when the on-air row is not in the window."""
        seq, offset = self._on_air
        idx = next((i for i, r in enumerate(self._items) if r["seq"] == seq), None)
        if idx is None:
            return 0.0
        return self._cum[idx] + offset
```

### 1.3 Add a test to `pilgrim/tests/test_web_app.py`

Append at the end of the file (it needs no fixtures):

```python
def test_cached_scheduler_rating_window_grace_and_song_start():
    """FIX_RATINGS.md Step 1 (VOTING.md V8 over the published snapshot): the
    grace branch reads `_cum` and `position()`, which the cached scheduler
    must provide; a rating for a song the snapshot has not reached is closed."""
    from pilgrim.voting import rating_window
    from pilgrim.web_app import CachedScheduler
    program = {"items": [
        {"seq": 10, "type": "song", "media_id": 2928, "duration_s": 180.0},
        {"seq": 11, "type": "liner", "media_id": 55, "duration_s": 8.0},
        {"seq": 12, "type": "song", "media_id": 777, "duration_s": 200.0},
    ]}

    def window(seq, offset, ask_seq):
        st = {"epoch": "e1", "revision": 1, "current_seq": seq,
              "current_offset_s": offset}
        return rating_window(CachedScheduler(st, program), "e1", ask_seq, 5.0)

    assert window(10, 100.0, 10) == (True, 2928)      # the song is on air
    assert window(11, 2.0, 10) == (True, 2928)        # liner on air, 2 s in: grace
    assert window(11, 6.0, 10) == (False, None)       # past the grace
    assert window(12, 1.0, 10) == (False, None)       # two rows later: closed
    assert window(10, 179.5, 12) == (False, None)     # a future song is closed
    assert window(11, 2.0, 11) == (False, None)       # a liner is never ratable
    sched = CachedScheduler({"epoch": "e1", "current_seq": 11,
                             "current_offset_s": 2.0}, program)
    assert sched._cum == [0.0, 180.0, 188.0]
    assert sched.position() == 182.0
    # a frozen station is closed (IDLE.md), whatever the row
    st = {"epoch": "e1", "current_seq": 10, "current_offset_s": 1.0, "idle": True}
    assert rating_window(CachedScheduler(st, program), "e1", 10, 5.0) == (False, None)
```

### 1.4 Checks and commit

Run `make lint`, `make test`, `make sim`, `make e2e`. All must pass. Then
rerun §1.1 and confirm `grace path: (True, 2928)`.

Commit: `Web tier: rating grace window over the published snapshot (FIX_RATINGS.md Step 1; RADIO §11, §14; VOTING.md V8)`.

---

## 2. Program items carry the song's lifetime aggregate

**RADIO §11 (`GET /api/station/program` fields), §14; VOTING.md V10.** This is
an HTTP API addition, so RADIO.md is updated in **this** commit (AGENTS.md
§1.8) — do §4.3 of this file as part of this step.

### 2.1 `pilgrim/store.py`: one batch query

Add this method directly after `song_rating` (the method that returns
`{"count": n, "avg": ...}` for one item):

```python
    def song_ratings_for(self, item_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Lifetime aggregates for several songs in one query (FIX_RATINGS.md
        Step 2; VOTING.md V10): {item_id: {"count": n, "avg": float|None}}.
        Ids with no ratings are absent; callers default to count 0 / avg None.
        One round trip per programme response, not one per song (the store
        may be Postgres across the tailnet)."""
        ids = sorted({int(i) for i in item_ids})
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_id, COUNT(*) AS n, AVG(stars) AS avg_stars "
                f"FROM song_ratings WHERE item_id IN ({marks}) GROUP BY item_id",
                ids).fetchall()
        return {int(r["item_id"]): {
                    "count": int(r["n"]),
                    "avg": round(float(r["avg_stars"]), 2)
                           if r["avg_stars"] is not None else None}
                for r in rows}
```

Rules: `?` placeholders only (the Postgres connection translates them,
`pilgrim/db.py`); never branch on the engine inside `store.py` (AGENTS.md §3).

### 2.2 `pilgrim/server.py`: `program_response`

In `Station.program_response`, the loop that builds `out` currently reads:

```python
        out = []
        for it in items:
            inv = self.db.get_item(it["item_id"])
            title = (inv.get("title") if inv else None) or ""
            artist = (inv.get("artist") if inv else None) or ""
            out.append({"seq": it["seq"], "media_id": it["item_id"],
                        "url": self.media_url(it["item_id"], inv),
                        # ALBUM_ART Step 7: "" until the JPEG is in S3; no
                        # origin fallback, art is cosmetic.
                        "art_url": art_url(self.cfg, inv),
                        "type": it["type"], "duration_s": it["duration_s"],
                        "sfx": self._with_urls(scheduler.overlays_for(it["seq"])),
                        "title": title, "artist": artist})
```

Replace it with:

```python
        # FIX_RATINGS.md Step 2 (VOTING.md V10): every song row carries its
        # lifetime aggregate so the client can show it the instant the song
        # is audible. One batch query; non-songs and disabled ratings send 0/null.
        song_ids = [it["item_id"] for it in items if it["type"] == "song"]
        aggs = (self.db.song_ratings_for(song_ids)
                if self.cfg.ratings.enabled and song_ids else {})
        out = []
        for it in items:
            inv = self.db.get_item(it["item_id"])
            title = (inv.get("title") if inv else None) or ""
            artist = (inv.get("artist") if inv else None) or ""
            agg = aggs.get(it["item_id"]) if it["type"] == "song" else None
            out.append({"seq": it["seq"], "media_id": it["item_id"],
                        "url": self.media_url(it["item_id"], inv),
                        # ALBUM_ART Step 7: "" until the JPEG is in S3; no
                        # origin fallback, art is cosmetic.
                        "art_url": art_url(self.cfg, inv),
                        "type": it["type"], "duration_s": it["duration_s"],
                        "sfx": self._with_urls(scheduler.overlays_for(it["seq"])),
                        "title": title, "artist": artist,
                        "rating_count": agg["count"] if agg else 0,
                        "rating_avg": agg["avg"] if agg else None})
```

The web tier (`pilgrim/web_app.py` `program_response`) passes the published
document through unchanged, so it picks the fields up automatically. Do not
edit it.

### 2.3 `pilgrim/tests/e2e/e2e_server.py`: mirror the fields

In the `program` route, the items list comprehension builds dicts with
`seq, media_id, type, duration_s, sfx, title, artist, art_url`. Change the
route so it first computes the aggregates, then adds the two fields to every
item. Replace the `return JSONResponse({"items": [...` statement with:

```python
        aggs = store.song_ratings_for(
            [it["item_id"] for it in items if it["type"] == "song"])
        out = []
        for it in items:
            agg = aggs.get(it["item_id"]) if it["type"] == "song" else None
            out.append({"seq": it["seq"], "media_id": it["item_id"],
                        "type": it["type"], "duration_s": it["duration_s"],
                        "sfx": sched.overlays_for(it["seq"]),
                        "title": (store.get_item(it["item_id"]) or {}).get("title") or "",
                        "artist": (store.get_item(it["item_id"]) or {}).get("artist") or "",
                        "art_url": _art_url(store, it["item_id"]),
                        "rating_count": agg["count"] if agg else 0,
                        "rating_avg": agg["avg"] if agg else None})
        return JSONResponse({"items": out, "start_offset_s": start_offset,
                "epoch": sched.epoch, "revision": sched.revision,
                "reset": stale, "server_time": time.time()},
                headers={"Cache-Control": "public, max-age=1"})
```

Keep everything above it in that route (the `stale` / `items` /
`start_offset` computation) exactly as it is.

### 2.4 Tests

**`pilgrim/tests/test_voting.py`** — append:

```python
def test_song_ratings_for_batches_lifetime_aggregates(cfg, tmp_env):
    """FIX_RATINGS.md Step 2: one query, keyed by item, over every airing."""
    _, store, _ = tmp_env
    a = store.add_item(type_="song", media_path="/tmp/a.flac", duration_s=60.0, fresh=True)
    b = store.add_item(type_="song", media_path="/tmp/b.flac", duration_s=60.0, fresh=True)
    c = store.add_item(type_="song", media_path="/tmp/c.flac", duration_s=60.0, fresh=True)
    cap = cfg.ratings.max_voters_per_ip
    store.cast_song_rating(item_id=a, epoch="e", seq=1, voter_id="1" * 32, ip=None,
                           stars=5, max_voters_per_ip=cap)
    store.cast_song_rating(item_id=a, epoch="e", seq=9, voter_id="1" * 32, ip=None,
                           stars=4, max_voters_per_ip=cap)   # a later airing
    store.cast_song_rating(item_id=b, epoch="e", seq=2, voter_id="2" * 32, ip=None,
                           stars=2, max_voters_per_ip=cap)
    aggs = store.song_ratings_for([a, b, c, a])
    assert aggs == {a: {"count": 2, "avg": 4.5}, b: {"count": 1, "avg": 2.0}}
    assert c not in aggs
    assert store.song_ratings_for([]) == {}
    assert aggs[a] == store.song_rating(a)
```

**`pilgrim/tests/test_server.py`** — in `test_rating_route_window_and_state`,
directly after the line

```python
    assert state["rating"] == {"open": True, "count": 1, "avg": 4.0}
```

insert:

```python
    # FIX_RATINGS.md Step 2: the programme carries the song's aggregate
    prog = c.get("/api/station/program").json()["items"]
    by_seq = {it["seq"]: it for it in prog}
    assert by_seq[seq]["rating_count"] == 1 and by_seq[seq]["rating_avg"] == 4.0
    liner_row = next(it for it in prog if it["type"] == "liner")
    assert liner_row["rating_count"] == 0 and liner_row["rating_avg"] is None
```

and directly after the existing line

```python
    assert c.get("/api/station/state").json()["rating"] is None
```

(the `ratings.enabled = False` branch) insert:

```python
    assert all(it["rating_count"] == 0 and it["rating_avg"] is None
               for it in c.get("/api/station/program").json()["items"])
```

**`pilgrim/tests/test_web_app.py`** — in
`test_program_and_state_come_from_the_cache`, after
`assert all(it["url"] for it in body["items"])` insert:

```python
    assert all("rating_count" in it and "rating_avg" in it for it in body["items"])
```

### 2.5 Docs in this commit

Do §4.3 (RADIO.md §11 program row) now, in this same commit.

### 2.6 Checks and commit

`make lint`, `make test`, `make sim`, `make e2e`; all pass. Also run the
Postgres flavour of the unit suite, which exercises the new `IN (...)`
query through the placeholder translation:

```
make pg-test PG_TEST_ARGS="pilgrim/tests/test_voting.py pilgrim/tests/test_server.py -x"
```

(needs Docker; see `pilgrim/deploy/local/pg_test.sh`. If Docker is not on
the tool shell's PATH use `DOCKER=/Applications/Docker.app/Contents/Resources/bin/docker`.)
If `pg-test` cannot run at all, say so in the report; do not skip it silently.

Commit: `Program items carry the song's rating aggregate (FIX_RATINGS.md Step 2; RADIO §11, §14; VOTING.md V10)`.

---

## 3. Client: aggregate always, fractional pink average under a cyan vote, no stale-poll close

**RADIO §9.1 (RATE THIS SONG), §9.2 (`pd:nowplaying`); VOTING.md §4, §5.**
Hard rule AGENTS.md §1.9: `rating.js` never touches audio; `player.js`
changes are one event detail only.

### 3.1 `pilgrim/web/player.js`: the event detail

In `setSongLabel`, the dispatch currently reads:

```js
    document.dispatchEvent(new CustomEvent("pd:nowplaying", {
      detail: named ? { epoch, seq: it.seq, media_id: it.media_id,
                        title: it.title, artist: it.artist } : null,
    }));
```

Replace with:

```js
    document.dispatchEvent(new CustomEvent("pd:nowplaying", {
      detail: named ? { epoch, seq: it.seq, media_id: it.media_id,
                        title: it.title, artist: it.artist,
                        // FIX_RATINGS.md Step 3: the lifetime aggregate rides
                        // on the programme row (RADIO §11), so the rating row
                        // can show it the instant the song is audible.
                        rating_count: it.rating_count || 0,
                        rating_avg: (typeof it.rating_avg === "number") ? it.rating_avg : null }
                    : null,
    }));
```

No other change in `player.js`.

### 3.2 `pilgrim/web/index.html`: three layers per turkey

Each of the five turkey buttons currently holds one `<svg>`. Give each
button three, in this order: the dim outline, the pink community layer, the
cyan "mine" layer. Replace the five buttons inside `<span class="turkeys">`
with exactly these (only the `aria-label` and `data-stars` differ):

```html
          <button type="button" class="turkey" role="radio" aria-checked="false"
                  aria-label="1 turkey" data-stars="1"><svg class="t-outline" aria-hidden="true"><use href="#turkey"/></svg><svg class="t-avg" aria-hidden="true"><use href="#turkey"/></svg><svg class="t-mine" aria-hidden="true"><use href="#turkey"/></svg></button>
          <button type="button" class="turkey" role="radio" aria-checked="false"
                  aria-label="2 turkeys" data-stars="2"><svg class="t-outline" aria-hidden="true"><use href="#turkey"/></svg><svg class="t-avg" aria-hidden="true"><use href="#turkey"/></svg><svg class="t-mine" aria-hidden="true"><use href="#turkey"/></svg></button>
          <button type="button" class="turkey" role="radio" aria-checked="false"
                  aria-label="3 turkeys" data-stars="3"><svg class="t-outline" aria-hidden="true"><use href="#turkey"/></svg><svg class="t-avg" aria-hidden="true"><use href="#turkey"/></svg><svg class="t-mine" aria-hidden="true"><use href="#turkey"/></svg></button>
          <button type="button" class="turkey" role="radio" aria-checked="false"
                  aria-label="4 turkeys" data-stars="4"><svg class="t-outline" aria-hidden="true"><use href="#turkey"/></svg><svg class="t-avg" aria-hidden="true"><use href="#turkey"/></svg><svg class="t-mine" aria-hidden="true"><use href="#turkey"/></svg></button>
          <button type="button" class="turkey" role="radio" aria-checked="false"
                  aria-label="5 turkeys" data-stars="5"><svg class="t-outline" aria-hidden="true"><use href="#turkey"/></svg><svg class="t-avg" aria-hidden="true"><use href="#turkey"/></svg><svg class="t-mine" aria-hidden="true"><use href="#turkey"/></svg></button>
```

Nothing else in `index.html` changes (the `<symbol id="turkey">` stays as is).

### 3.3 `pilgrim/web/style.css`: the layered turkey

In the RATE THIS SONG block, replace everything from the comment line
`/* Empty turkeys are a dim lavender outline; ...` down to and including the
rule `.turkey:hover:not(:disabled), .turkey:focus-visible { ... }` (that is:
the comment, `.turkey {...}`, `.turkey svg {...}`, the `<use>` comment,
`.turkey.filled {...}`, `.turkey.filled svg {...}`, and the hover rule) with:

```css
/* Three stacked layers per turkey (FIX_RATINGS.md Step 3, D2). `.t-outline`:
   the dim lavender outline, always. `.t-avg`: a magenta ("pink") fill for the
   COMMUNITY average, clipped left-to-right to the fraction this turkey holds
   (rating.js sets `clip-path` per turkey; 100 % clipped = nothing). `.t-mine`:
   a cyan fill for THIS listener's own rating (or the hover/focus preview),
   drawn on top of the pink wherever it reaches. <use> clones the symbol into
   a shadow tree, so colour and fill are set on each <svg> and inherited; the
   symbol's group carries no fill attribute of its own. */
.turkey {
  position: relative;
  width: 48px; height: 48px; padding: 0; background: none; border: 0;
  border-radius: 6px; cursor: pointer; color: var(--dim);
  display: inline-flex; align-items: center; justify-content: center;
}
.turkey svg { width: 34px; height: 34px; display: block; fill: none; }
.turkey .t-avg, .turkey .t-mine {
  position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%);
  fill: currentColor; pointer-events: none;
}
.turkey .t-avg { color: var(--magenta); clip-path: inset(0 100% 0 0);
  filter: drop-shadow(0 0 4px rgba(255, 47, 214, .45)); }
.turkey .t-mine { color: var(--cyan); display: none;
  filter: drop-shadow(0 0 4px rgba(36, 224, 255, .45)); }
.turkey.mine .t-mine { display: block; }
.turkey:hover:not(:disabled), .turkey:focus-visible {
  color: var(--cyan);
  filter: brightness(1.2); outline: 2px solid var(--cyan); outline-offset: -2px;
}
```

Keep `.turkey:disabled`, `.rate-row[aria-disabled="true"]`, `.rate-agg` and
the phone-width rules (`.turkey { width: 44px; ... }`, `.turkey svg { width: 30px; ... }`)
exactly as they are; they apply to all three layers. `--magenta` and `--cyan`
already exist in `:root`. The class `filled` is retired; nothing else in the
file references it.

### 3.4 `pilgrim/web/rating.js`: replace the whole file

```js
/* Pilgrim Dot Farm — five-turkey song ratings (VOTING.md Step 5; FIX_RATINGS.md Step 3).
   The control is open only while the song it belongs to is audible. The server
   decides the window (`rating_window`); this file only enables/disables the
   control and renders what the server says. It never touches audio (§9.2).

   Colours: pink (magenta) = the community average, always, with the last
   turkey partly filled left-to-right; cyan = the rating this browser gave
   THIS airing (or the hover/focus preview), drawn over the pink. The exact
   aggregate (`avg · n`) is always shown for the audible song: first from the
   programme row, then from the state poll (FIX_RATINGS.md D1/D2). */
(() => {
  "use strict";
  const VOTER = window.PD_VOTER;
  const row = document.getElementById("rate-row");
  if (!row || !VOTER) return;
  const label = row.querySelector(".rate-label");
  const agg = document.getElementById("rate-agg");
  const turkeys = Array.from(row.querySelectorAll(".turkey"));
  const avgLayers = turkeys.map((t) => t.querySelector(".t-avg"));
  const LABEL = "RATE THIS SONG";
  const NONE = "NO RATINGS YET";
  const POLL_MS = 5000;

  let current = null;   // {epoch, seq, media_id} of the audible song
  let savedStars = 0;   // this browser's rating for THIS airing (cyan, on top)
  let communityAvg = 0; // the community average (pink, fractional); 0 when unrated
  let preview = 0;      // hover/focus level (cyan, on top)
  let busy = false;
  let pollTimer = null;
  let labelTimer = null;

  function setEnabled(on) {
    row.setAttribute("aria-disabled", on ? "false" : "true");
    turkeys.forEach((t) => { t.disabled = !on; });
  }

  function setLabel(text) { label.textContent = text; }

  function flashLabel(text, ms) {
    if (labelTimer) clearTimeout(labelTimer);
    setLabel(text);
    labelTimer = setTimeout(() => { setLabel(LABEL); labelTimer = null; }, ms);
  }

  /* Two paints per turkey (D2). Pink underneath: the community average,
     turkey i holding clamp(avg - i, 0, 1) of its glyph, clipped from the
     right. Cyan on top: the hover preview, else this browser's own rating of
     this airing, as whole turkeys. The pink stays wherever the cyan does not
     reach. aria-checked follows the listener's own rating only. */
  function paint() {
    const mine = preview || savedStars;
    turkeys.forEach((t, i) => {
      const frac = Math.min(1, Math.max(0, communityAvg - i));
      avgLayers[i].style.clipPath = "inset(0 " + ((1 - frac) * 100).toFixed(1) + "% 0 0)";
      t.classList.toggle("has-avg", frac > 0);
      t.classList.toggle("mine", i < mine);
      t.setAttribute("aria-checked", i === savedStars - 1 ? "true" : "false");
      t.tabIndex = i === 0 ? 0 : -1;
    });
  }

  /* The community view: the exact text, and the fractional pink level (D2). */
  function showAggregate(count, avg) {
    const has = count > 0 && typeof avg === "number";
    agg.textContent = has ? avg.toFixed(1) + " · " + count : NONE;
    communityAvg = has ? Math.min(5, Math.max(0, avg)) : 0;
    paint();
  }
  function clearAggregate() { agg.textContent = ""; communityAvg = 0; }

  function airingKey(a) { return "pd_rating:" + a.epoch + ":" + a.seq; }
  function recallStars(key) {
    const n = parseInt(VOTER.recall(key) || "0", 10);
    return n >= 1 && n <= 5 ? n : 0;
  }

  function stopPoll() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }
  function startPoll() {
    if (pollTimer) return;
    pollTimer = setInterval(poll, POLL_MS);
  }

  /* Nothing audible (or STOP): empty row, no aggregate. */
  function reset() {
    current = null;
    savedStars = 0; preview = 0;
    clearAggregate();
    paint();
    setEnabled(false);
    stopPoll();
  }

  /* The window closed on the server while the song is still audible here:
     stop taking clicks but keep what the listener sees. */
  function closeWindow() {
    preview = 0;
    paint();
    setEnabled(false);
    stopPoll();
  }

  async function poll() {
    if (!current) return;
    const mine = current;
    try {
      const r = await fetch("/api/station/state");
      if (!r.ok) return;
      const st = await r.json();
      if (current !== mine) return;          // the song changed while we waited
      const rating = st.rating;
      if (!rating || st.epoch !== mine.epoch) { reset(); return; }  // ratings off / programme reset
      if (st.current_seq === mine.seq) {
        if (rating.open) {
          showAggregate(rating.count, rating.avg);
          if (!busy) setEnabled(true);
        }
        return;
      }
      if (typeof st.current_seq === "number" && st.current_seq > mine.seq) {
        closeWindow();                        // the server has moved past this song
      }
      // st.current_seq < mine.seq: the snapshot is a tick behind our playhead
      // (song start). Keep the control; the next poll will catch up.
    } catch (e) { /* keep the control; retry on the next poll */ }
  }

  async function submit(stars) {
    if (!current || busy) return;
    const airing = current;
    busy = true;
    turkeys.forEach((t) => { t.disabled = true; });
    try {
      const r = await fetch("/api/station/rating", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ voter_id: VOTER.id(), epoch: airing.epoch,
                               seq: airing.seq, stars }),
      });
      const d = await r.json().catch(() => ({}));
      if (current !== airing) return;        // the song changed while in flight
      if (r.status === 200) {
        savedStars = d.mine;                 // cyan goes on top of the pink
        preview = 0;
        VOTER.remember(airingKey(airing), d.mine);
        showAggregate(d.count, d.avg);       // repaints
        setEnabled(true);
      } else if (r.status === 409) {
        // the song ended while the click was in flight (V8 grace)
        closeWindow();
        flashLabel("VOTING CLOSED", 3000);
      } else {
        flashLabel("TRY AGAIN", 3000);
        preview = 0;
        paint();
        setEnabled(true);
      }
    } catch (e) {
      if (current === airing) {
        flashLabel("TRY AGAIN", 3000);
        preview = 0;
        paint();
        setEnabled(true);
      }
    } finally {
      busy = false;
    }
  }

  /* pd:nowplaying is dispatched by player.js from its audio-clock playhead:
     the detail is the song that is AUDIBLE now, or null. */
  document.addEventListener("pd:nowplaying", (e) => {
    const d = e.detail;
    if (!d) {
      reset();
      if (labelTimer) { clearTimeout(labelTimer); labelTimer = null; }
      setLabel(LABEL);
      return;
    }
    if (current && current.epoch === d.epoch && current.seq === d.seq) return;
    current = { epoch: d.epoch, seq: d.seq, media_id: d.media_id };
    savedStars = recallStars(airingKey(current));
    preview = 0;
    setEnabled(true);
    showAggregate(d.rating_count || 0,
                  (typeof d.rating_avg === "number") ? d.rating_avg : null);  // paints
    setLabel(LABEL);
    startPoll();
    void poll();
  });

  turkeys.forEach((t, i) => {
    const stars = i + 1;
    t.addEventListener("click", () => submit(stars));
    t.addEventListener("mouseenter", () => {
      if (!t.disabled) { preview = stars; paint(); }
    });
    t.addEventListener("focus", () => {
      if (!t.disabled) { preview = stars; paint(); }
    });
    t.addEventListener("keydown", (e) => {
      const step = (e.key === "ArrowRight" || e.key === "ArrowDown") ? 1
        : (e.key === "ArrowLeft" || e.key === "ArrowUp") ? -1 : 0;
      if (!step) return;
      e.preventDefault();
      const next = turkeys[(i + step + turkeys.length) % turkeys.length];
      next.focus();
    });
  });

  row.querySelector(".turkeys").addEventListener("mouseleave", () => {
    preview = 0;
    paint();
  });
  row.addEventListener("focusout", (e) => {
    if (!row.contains(e.relatedTarget)) { preview = 0; paint(); }
  });

  setEnabled(false);
  paint();
})();
```

### 3.5 `pilgrim/tests/e2e/voting.spec.ts`

(a) The `announce` helper passes a fake detail. Change its object from
`{ ...airing, media_id: 1, title: "T", artist: "A" }` to
`{ ...airing, media_id: 1, title: "T", artist: "A", rating_count: 0, rating_avg: null }`.
The reload test builds the same object inline in `page.evaluate(...)`; add
the same two fields there.

(b) The class for the listener's own level is now `mine` (it was `filled`).
In `voting.spec.ts` replace every `toHaveClass(/filled/)` with
`toHaveClass(/mine/)` and `classList.contains("filled")` with
`classList.contains("mine")` (the click test and the reload test).

In the test `"a turkey click posts, fills the row, and shows the aggregate"`,
directly after `await announce(page, airing);` insert:

```ts
  // FIX_RATINGS.md Step 3: the aggregate is never blank while a song is audible
  await expect(page.locator("#rate-agg")).not.toHaveText("");
```

and directly after the (now) `await expect(page.locator(".turkey[data-stars='4']")).toHaveClass(/mine/);`
insert:

```ts
  // D2: the listener's own level is four cyan turkeys on top; the fifth is not theirs
  await expect(page.locator(".turkey.mine")).toHaveCount(4);
  await expect(page.locator(".turkey[data-stars='5']")).not.toHaveClass(/mine/);
```

(c) Append this test before the `"queue votes: ..."` test. It is
synchronous on purpose: the paint happens inside the event handler, before
the state poll can replace the announced aggregate with the real one.

```ts
/* The pink layer's clip: rating.js writes `inset(0 X% 0 0)`; X is how much of
   the glyph is hidden from the right (0 = full turkey, 100 = none). */
function hiddenPct(el: Element | null): number {
  const m = /([\d.]+)%/.exec((el as HTMLElement)?.style.clipPath || "");
  return m ? parseFloat(m[1]) : NaN;
}

test("the community average is pink with a fractional turkey; a vote paints cyan over it", async ({ page }) => {
  // FIX_RATINGS.md Step 3 (D2): rating_avg 3.4 = three full pink turkeys, 40 %
  // of the fourth, none of the fifth, exact text beside them, no cyan yet.
  await page.goto("/");
  const airing = await waitForSong(page);
  const shown = await page.evaluate((detail) => {
    document.dispatchEvent(new CustomEvent("pd:nowplaying", { detail }));
    const t = (n: number) => document.querySelector(`.turkey[data-stars='${n}']`)!;
    const clip = (n: number) => (t(n).querySelector(".t-avg") as HTMLElement).style.clipPath;
    return { c1: clip(1), c3: clip(3), c4: clip(4), c5: clip(5),
             hasAvg: [1, 2, 3, 4, 5].map((n) => t(n).classList.contains("has-avg")),
             mine: document.querySelectorAll(".turkey.mine").length,
             agg: document.getElementById("rate-agg")!.textContent };
  }, { ...airing, media_id: 1, title: "T", artist: "A", rating_count: 2, rating_avg: 3.4 });
  const pct = (clip: string) => parseFloat(/([\d.]+)%/.exec(clip)![1]);
  expect(pct(shown.c1)).toBeCloseTo(0, 0);
  expect(pct(shown.c3)).toBeCloseTo(0, 0);
  expect(pct(shown.c4)).toBeCloseTo(60, 0);
  expect(pct(shown.c5)).toBeCloseTo(100, 0);
  expect(shown.hasAvg).toEqual([true, true, true, true, false]);
  expect(shown.mine).toBe(0);
  expect(shown.agg).toBe("3.4 · 2");
  // no ratings: nothing pink, NO RATINGS YET
  await announce(page, null);
  const empty = await page.evaluate((detail) => {
    document.dispatchEvent(new CustomEvent("pd:nowplaying", { detail }));
    return { hasAvg: document.querySelectorAll(".turkey.has-avg").length,
             agg: document.getElementById("rate-agg")!.textContent };
  }, { ...airing, seq: airing.seq + 1000, media_id: 1, title: "T", artist: "A",
       rating_count: 0, rating_avg: null });
  expect(empty.hasAvg).toBe(0);
  expect(empty.agg).toBe("NO RATINGS YET");
  await announce(page, null);
});

test("a vote keeps the pink average visible under the cyan", async ({ page }) => {
  // D2: after a 200, the listener's cyan sits on top and the pink layer is
  // still painted from the server's aggregate (count >= 1 after the vote).
  let rated = false;
  for (let attempt = 0; attempt < 6 && !rated; attempt++) {
    await page.goto("/");
    const airing = await waitForSong(page);
    await announce(page, airing);
    const [resp] = await Promise.all([
      page.waitForResponse((r) => r.url().includes("/api/station/rating")),
      page.click(".turkey[data-stars='2']"),
    ]);
    rated = resp.status() === 200;
  }
  expect(rated, "no click landed inside a rating window").toBeTruthy();
  await expect(page.locator(".turkey.mine")).toHaveCount(2);
  await expect(page.locator(".turkey[data-stars='1']")).toHaveClass(/has-avg/);
  expect(hiddenPct(await page.$(".turkey[data-stars='1'] .t-avg"))).toBeCloseTo(0, 0);
});
```

(d) In the test `"the server closes the window after the song ends"`, keep
everything; the final `announce(page, null)` → `aria-disabled="true"`
assertion still holds (`reset()`).

### 3.6 Checks and commit

`make lint`, `make test`, `make sim`, `make e2e`. If `make e2e` fails with a
missing browser, run `cd pilgrim/tests/e2e && npx playwright install chromium`
once and rerun. All must pass.

Commit: `Rating row: fractional pink average under a cyan vote, aggregate always, no stale-poll close (FIX_RATINGS.md Step 3; RADIO §9.1, §9.2; VOTING.md §4, §5)`.

---

## 4. Docs

Make these edits. §4.3 belongs to the Step 2 commit; §4.1, §4.2 and §4.4 are
their own commit at the end of Step 3 or a separate docs commit — either is
fine, but RADIO.md must describe what the code does by the end of Step 4.

### 4.1 RADIO.md §9.1, the **RATE THIS SONG** bullet

Replace the bullet with:

```
- **RATE THIS SONG** (VOTING.md, FIX_RATINGS.md): a row above the cover art
  with five turkey buttons (44 px targets, outlined → filled) and the on-air
  song's lifetime aggregate (`avg · n`, or `NO RATINGS YET`). The aggregate is
  shown the whole time a song is audible: first from the programme row's
  `rating_count`/`rating_avg` (§11), then refreshed by the state poll while
  the window is open. Clicking is open only while the server says so
  (`POST /api/station/rating` 409s otherwise); the client enables/disables
  from the same audio-clock `pd:nowplaying` signal as the label (§9.2) and
  disables the turkeys, keeping the paint and the aggregate, once the state
  poll shows the server has moved **past** the song (a snapshot a tick behind
  the playhead never closes it). The row keeps its min-height when disabled,
  so the art, PLAY row and meter never move. Each turkey is three stacked
  `<svg>` layers of the one symbol: a dim outline; a **magenta** fill for the
  community average, clipped left-to-right so turkey *i* shows
  `clamp(avg − (i−1), 0, 1)` of its glyph (3.4 = three full turkeys and 40 %
  of the fourth; nothing when the song has no ratings), always visible; and a
  **cyan** fill for this browser's own rating of this airing (whole turkeys,
  restored per airing from `pd_rating:<epoch>:<seq>`), drawn over the magenta
  wherever it reaches. Hover/focus previews the listener's level in cyan;
  arrow keys move between turkeys.
```

### 4.2 RADIO.md §9.2, the **`pd:nowplaying`** bullet

Change `detail = {epoch, seq, media_id, title, artist}` to
`detail = {epoch, seq, media_id, title, artist, rating_count, rating_avg}`
and append the sentence: `The two rating fields are copied from the
programme row (§11) so the rating row can show the aggregate without a poll.`

### 4.3 RADIO.md §11, the `GET /api/station/program?after_seq=N` row

In the field list `{seq, media_id, url, art_url, type, duration_s, sfx, title, artist}`
append `, rating_count, rating_avg`, and append this sentence to the cell:
`rating_count`/`rating_avg` are the song's lifetime rating aggregate
(VOTING.md V10, over every airing of that `media_id`; `avg` rounded to 2
decimals); `0`/`null` for non-song rows and whenever `ratings.enabled` is
false. One batch query per response (`Store.song_ratings_for`).

### 4.4 VOTING.md and AGENTS.md

- `VOTING.md`: directly under the title line add:
  `> Amended by FIX_RATINGS.md (2026-11-06): programme rows carry the aggregate, the client shows it for the whole song, the community average is always drawn as magenta turkeys with fractional fill under the listener's cyan vote, and the web tier's grace window is fixed. Where this file and FIX_RATINGS.md differ, FIX_RATINGS.md wins.`
- `AGENTS.md`: in the first paragraph ("Active work plans: ..."), after the
  VOTING.md sentence add: `FIX_RATINGS.md (Steps 1–5) amends VOTING.md's
  rating UI and the web tier's rating window.`

Commit (if separate): `Docs: rating aggregate on programme rows, fractional pink average under the cyan vote (FIX_RATINGS.md Step 4; RADIO §9.1, §9.2, §11)`.

---

## 5. Hand-off: ask the operator to deploy

Do **not** do any of the following yourself. End the task with this list in
the report and wait:

1. **Station restart on the Mac** (Step 2 changes `server.py`; the running
   process is `launchd` `farm.pilgrim.station`). Restarting needs the
   operator's go: it drops the live programme window.
2. **Web tier image** (Steps 2–3 change `pilgrim/web/*` and
   `e2e_server.py` is test-only, but the container serves `pilgrim/web/`):
   `make image`, then `docker tag pilgrim:web pilgrimdotfarm/front-end:latest`
   and `docker push pilgrimdotfarm/front-end:latest`
   (`pilgrim/deploy/container/README.md`), then a new ECS deployment of the
   `pilgrim-web` service. The operator runs these.
3. Order matters: restart the station **first** (so the published programme
   carries `rating_count`/`rating_avg`), then roll the web tier. Old clients
   ignore the extra fields; the new client treats a missing field as `0`/`null`.
4. After both: open the site, PLAY, and check that `NO RATINGS YET` or
   `avg · n` appears the moment a song's title does, that a song with
   ratings shows magenta turkeys with the last one partly filled to match
   the average, and that rating it paints cyan turkeys over the magenta up to
   your level while the magenta beyond it stays (the e2e covers the same
   three things).

---

## 6. Open questions for the operator (defaults in force until answered)

1. **Your own earlier rating.** After rating a song, its next airing shows
   only the community pink (your vote is inside the average), not your
   earlier cyan (one rating per airing, VOTING.md V2). Showing *your* past
   rating across airings or devices would need a per-voter route and is not
   in this plan. Want it?
2. **Pink glow.** The magenta layer has the same soft drop-shadow glow as the
   cyan one. If the two glows blur together at phone size, drop the glow on
   the magenta layer (one CSS line in §3.3).
3. **Possibly lost ratings on the evening of 2026-11-05.** Between the
   Fargate web tier going live and the Mac's Postgres cutover (~22:14 EST),
   ratings posted through the web tier went to Postgres while the Mac's
   published state still read SQLite, and a `pg-migrate --truncate` would
   have wiped them. Postgres has no rows in that window; whether none were
   cast or they were truncated cannot be told from the data. Did you rate
   anything that evening before the 22:49 EST restart?
4. **`max_voters_per_ip: 3`.** Three distinct browsers behind one home IP
   can rate one airing; a fourth gets 429 "TRY AGAIN". A private window
   generates a new voter id every session. Raise it?

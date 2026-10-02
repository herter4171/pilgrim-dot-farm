# Fix song prompting and genre variety

Status: diagnosis and implementation plan. No production behavior is changed by
this document.

## Reference and observed behavior

Use [the MiniMax Music 3 prompting skill](.pi/skills/minimax-music3-prompting/SKILL.md)
as the caption and lyric contract, adapted to the two fields accepted by our
mlx-serve endpoint (`prompt` and `lyrics`, or `instrumental: true`). The skill's
two labelled code blocks are a presentation format for people; the station
must send the fields separately, without code fences. The skill file being
present in the repository does not cause the station's LLM to use it. Runtime
prompts come from `pilgrim/prompts/song_brief.md` through
`pilgrim/server.py::_load_prompts` and `pilgrim/seed.py::_prompts`.
The supplied `.pi` skill cites a `multimodalart` guide as its source and itself
points to MiniMax-AI's separate `music-caption-rewriter` as the official skill;
confirm provenance before treating either document as a vendor guarantee.

Per RADIO.md §6.2, genre should be drawn from the weighted configuration and
the brief should be validated before MiniMax generation. Per §5.2, adjacent
songs should respect the last-three-song genre rule, subject to an explicitly
documented priority when constraints conflict. The current pipeline does not
achieve either consistently:

- `pilgrim/pipelines/songs.py::brief` sends only the genre **names** to qwen38.
  It ignores their weights, leaves the choice to the LLM, and accepts the
  returned genre even if it was recently used or absent from config. An
  in-memory fake confirmed that it accepts a zero-weight, recently used genre.
- `pilgrim/producer.py::_recent_genres` uses the last five **created** songs,
  while the brief labels the last three of these as "recently aired." It does
  not read the committed air order. Any listener request, even one asking only
  for a subject, disables the brief's genre avoidance.
- `pilgrim/scheduler.py::_pick_stock_song` chooses the newest fresh song
  without applying the genre filter. The filter applies only to recycled
  songs, and it falls back to the unfiltered pool when empty. Request songs
  also bypass it. Fresh LIFO and request priority in §5.2 need an explicit
  tie-break with the genre rule before changing that behavior.
- `pilgrim/prompts/song_brief.md` asks for a 1–3 sentence `style_prompt`.
  MiniMax's skill calls for `Global Metadata`, `Vocal Details`, and
  `Arrangement`, with an emotional arc, explicit vocal configuration, and an
  arrangement that develops through the lyric sections. The brief checker
  requires only a nonempty title and style prompt. It does not validate the
  lyric tags, genre, caption structure, or requested vocal configuration.

Read-only snapshot of `pilgrim/station.db` on 2026-10-01: 73 active songs,
including seven listener requests. Genres were polka 19, bluegrass 19, yacht
rock 10, chiptune 9, bossa nova 9, synthwave 5, dungeon synth 2, and doom
metal 0. All eight config weights are 1. Excluding requests still leaves 18
polkas and 14 bluegrass songs in 66 stock songs. All 73 stored captions were
24–59 words (median 38), none had the three skill headings, and only eight
explicitly named a vocal gender. Forty-eight mention AM radio, and 40 use
crackle, hiss, static, lo-fi, dusty, or tinny texture. The saved lyrics have
section tags on their own lines; no inline-tag error was found in this sample.
The captions' shared texture may make otherwise different genres sound alike,
but listening is needed to confirm the audible effect. Existing songs ranged
from 47 to 166 seconds (median 110) despite the 360-second ceiling; the skill
notes that lyric and section length also affect duration.

## Proposed implementation order

1. **Choose the stock genre in code.** Use the injected RNG and the existing
   `songs.genres` weights. Ignore zero or negative weights, reject an empty
   usable pool, and exclude the previous `playout.genre_no_repeat` generated
   stock genres when another choice exists. Include recently committed genres
   so a long inventory backlog does not hide what listeners just heard. Pass
   the selected genre to the LLM as a requirement and reject a mismatched
   response. Keep the chosen genre and its reason in the stored brief metadata
   for diagnosis. Do not add a config or database field unless necessary; a
   schema change requires a matching RADIO.md update (§12 or §14).
2. **Separate request handling.** Preserve an explicit, supported genre wish
   even when recent. A subject-only request should use the normal stock genre
   chooser and its avoidance rule. Validate that the resulting caption and
   `genre` field agree. Decide the policy for requests naming a genre outside
   the configured list before implementing that case; the current prompt says
   to choose from the list but also says to fulfill genre requests.
3. **Rewrite the song brief prompt.** Keep the existing five JSON fields
   (`title`, `artist`, `genre`, `style_prompt`, `lyrics`) and put the three skill
   headings inside `style_prompt`. Target roughly 250–450 words of music
   direction. Require a concrete genre/subgenre, emotional progression, scene,
   production character, explicit lead voice gender/timbre (or instrumental
   declaration and lead instrument), and a section-by-section instrument and
   groove timeline. Avoid invented exact BPM/key. Keep lyric words out of the
   caption. Put the station's rural humor mainly in title/lyrics/imagery;
   vary production texture and vocal treatment by genre instead of routinely
   prescribing AM static. Keep every lyric section tag alone on its line and
   request enough sections for the intended song length. For an instrumental,
   use the backend's existing `instrumental: true` path and no lyric text;
   describe the lead instrument in `Vocal Details`.
4. **Validate before the expensive M5 call.** Replace the loose dictionary
   checks with a Pydantic brief model and focused validators for the selected
   genre, required headings in order, vocal or instrumental declaration,
   supported lyric tags and standalone tag lines, and a caption length range.
   Validate that the requested genre and major exclusions survive in the
   caption. Retry invalid LLM output a bounded number of times with a logged
   reason; never send an invalid brief to MiniMax. Preserve the existing
   `duration_seconds` ceiling and the backend's synchronous WAV path in
   docs/backends.md (§6.2, §8).
5. **Reconcile on-air genre spacing.** Specify in RADIO.md §5.2 what happens
   when fresh LIFO, a ready listener request, and no-repeat conflict. Suggested
   policy: ready requests retain priority; among ordinary fresh songs, choose
   the newest that clears the genre rule, falling back only if none clears it.
   The scheduler must continue choosing already rendered items and never wait
   on song production. This policy needs an explicit decision because it
   changes the currently stated fresh-song LIFO behavior.
6. **Measure both stages.** Log the selected genre, LLM-returned genre,
   validation failures, MiniMax rejections, and the resulting inventory and
   airplay mix. Compare stock and requested songs separately. Use existing
   stored captions and a human listening sample to assess audible variety;
   metadata alone cannot prove it. Expanding the eight configured genres is
   an editorial choice after the current weights actually work.

## Acceptance checks

- Fake LLM tests prove that a zero-weight genre is never offered as a stock
  choice; a recently generated stock genre is avoided when alternatives
  exist; fixed RNG seeds give repeatable weighted results; subject-only
  requests do not disable avoidance; and explicit supported genre requests
  remain possible.
- Fake output tests reject a mismatched or unknown genre, missing/reordered
  headings, unspecified vocals, malformed tags, contradictory instrumental
  lyrics, and captions too short to describe a timeline. A valid structured
  caption and standalone lyric tags reach separate mlx payload fields.
- Scheduler tests cover fresh songs, recycled songs, and request priority
  under the chosen §5.2 tie-break. Simulation checks the emitted genre
  sequence rather than relying only on a cyclically seeded fake library.
- Run `make lint`, `make test`, `make sim`, and `make e2e` in that order with
  fakes. On the pre-change snapshot: lint passed; 183 unit tests passed;
  `make sim` failed an existing song starvation assertion (6.9 h versus its
  6.0 h guard); nine browser tests passed. Treat that simulation failure
  separately from prompting. Do not run real MiniMax song generation as part
  of this change; an explicitly requested smoke probe must stay at or below
  ten seconds (AGENTS.md §1.6).

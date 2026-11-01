"""Song pipeline tests: mlx payload, family-level genre, structured caption.

Fakes only (AGENTS §1.2). The brief contract is the MiniMax Music 3 structured
caption (prompting guide §3/§7) over the guide's 18 style families.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pilgrim import genres
from pilgrim.config import RNG
from pilgrim.pipelines.llm import LLMError
from pilgrim.pipelines.songs import SongPipeline
from pilgrim.tests.conftest import VALID_CAPTION, valid_brief


class _Resp:
    status_code = 200
    content = b"FAKEWAV"
    text = ""
    headers: dict = {}


class _FakePost:
    """Captures the generator payload; returns a fake 200 WAV response."""

    def __init__(self):
        self.payload = None
        self.url = None
        self.headers = None

    async def post(self, url, json, headers=None):
        self.payload = json
        self.url = url
        self.headers = headers
        return _Resp()


class _Rec:
    """Fake LLM: records the user message, returns a canned brief.

    With no canned brief it echoes the family the station pinned (parsed from
    the prompt) so stock-brief tests exercise the real weighted choice.
    """

    def __init__(self, brief=None, replies=None):
        self.system = ""
        self.user = ""
        self.calls = 0
        self._brief = brief
        self._replies = list(replies or [])

    async def chat_json(self, model, system, user, max_tokens=800):
        self.system = system
        self.user = user
        self.calls += 1
        if self._replies:
            return self._replies.pop(0)
        if self._brief is not None:
            return dict(self._brief)
        import re as _re
        m = _re.search(r"Required family: ([a-z0-9-]+)", user)
        return valid_brief(m.group(1) if m else "general-pop-ballad")

    async def close(self):
        pass


def _pipeline(cfg, tmp_path):
    sp = SongPipeline.__new__(SongPipeline)
    sp.cfg = cfg
    sp.media_dir = tmp_path
    sp.prompts = {}
    sp._client = _FakePost()
    return sp


def _brief_pipeline(cfg, llm, seed=1):
    sp = SongPipeline.__new__(SongPipeline)
    sp.cfg = cfg
    pdir = Path(__file__).resolve().parents[1] / "prompts"
    sp.prompts = {name: (pdir / f"{name}.md").read_text()
                  for name in ("song_brief", "station_song", "paid_request")}
    sp.llm = llm
    sp.rng = RNG(seed)
    return sp


# --------------------------------------------------------------------------- #
# mlx payload
# --------------------------------------------------------------------------- #
def test_generate_forwards_seed_lyrics_and_max_duration(cfg, tmp_path):
    """duration_s = songs.max_duration_s (without it mlx-serve cuts at ~60 s)."""
    sp = _pipeline(cfg, tmp_path)
    brief = valid_brief(seed=12345, lyrics="[verse]\npour the beans")
    asyncio.run(sp.generate(brief))
    assert sp._client.payload["duration_seconds"] == cfg.songs.max_duration_s
    assert sp._client.payload["seed"] == 12345
    assert sp._client.payload["lyrics"] == "[verse]\npour the beans"
    # the structured caption is what the model receives as `prompt`
    assert sp._client.payload["prompt"] == VALID_CAPTION


def test_instrumental_has_no_lyrics_and_keeps_seed(cfg, tmp_path):
    sp = _pipeline(cfg, tmp_path)
    brief = {"style_prompt": "quiet pad", "lyrics": "", "seed": 7}
    asyncio.run(sp.generate(brief))
    assert sp._client.payload["instrumental"] is True
    assert "lyrics" not in sp._client.payload
    assert sp._client.payload["duration_seconds"] == cfg.songs.max_duration_s
    assert sp._client.payload["seed"] == 7


# --------------------------------------------------------------------------- #
# brief: family selection, request routing, caption validation
# --------------------------------------------------------------------------- #
def test_brief_includes_json_escaped_request_text(cfg):
    """OVERHAUL 4.5: the brief's user message carries the JSON-escaped request
    text (DATA, not instructions)."""
    import json as _json

    llm = _Rec(valid_brief("general-pop-ballad"))
    sp = _brief_pipeline(cfg, llm)
    asyncio.run(sp.brief([], request_text='say "hi" & more <script>'))
    assert _json.dumps('say "hi" & more <script>') in llm.user


def test_request_style_routes_to_family(cfg):
    """A request's named style wins: it is routed to the nearest family and
    pinned, and the fallback list is not offered as a choice."""
    cap = VALID_CAPTION.replace("Jazz Pop / Cabaret Swing",
                                "Grunge / Alternative Rock")
    llm = _Rec(valid_brief("pop-alternative-rock", style_prompt=cap))
    sp = _brief_pipeline(cfg, llm)
    out = asyncio.run(sp.brief(["jazz-swing-big-band"],
                               request_text="grunge rock about plowing"))
    assert out["genre"] == "pop-alternative-rock"
    assert "Required family: pop-alternative-rock" in llm.user
    assert "OVERRIDES" in llm.user
    assert "Genres to pick from" not in llm.user
    assert "Avoid genres (recently aired)" not in llm.user


def test_request_naming_jazz_pins_jazz_family(cfg):
    """The reported failure: 'jazz song about ...' must pin the jazz family."""
    llm = _Rec(valid_brief("jazz-swing-big-band"))
    sp = _brief_pipeline(cfg, llm)
    out = asyncio.run(sp.brief([], request_text=(
        "jazz song about Chuck E Cheese being arrested in costume")))
    assert out["genre"] == "jazz-swing-big-band"
    assert "Required family: jazz-swing-big-band" in llm.user
    assert "bossa nova" in llm.user  # family cues are offered to the caption


def test_subject_only_request_leaves_family_to_the_llm(cfg):
    llm = _Rec(valid_brief("general-pop-ballad"))
    sp = _brief_pipeline(cfg, llm)
    out = asyncio.run(sp.brief([], request_text="my dog can talk"))
    assert out["genre"] == "general-pop-ballad"
    assert "best covers it" in llm.user


def test_stock_brief_pins_a_weighted_family_and_avoids_recent(cfg):
    llm = _Rec()
    sp = _brief_pipeline(cfg, llm, seed=7)
    first = asyncio.run(sp.brief([]))
    assert first["genre"] in cfg.songs.genres
    assert f"Required family: {first['genre']}" in llm.user
    assert "Genres to pick from" in llm.user
    second = asyncio.run(sp.brief([first["genre"]]))
    assert second["genre"] != first["genre"]
    assert f"Avoid genres (recently aired): {first['genre']}" in llm.user


def test_zero_weight_family_is_never_offered(cfg):
    """§6.2: a zero-weight genre must not be chosen (weights now matter)."""
    songs = cfg.songs.model_copy(update={"genres": {
        "jazz-swing-big-band": 0.0, "country-americana": 1.0}})
    cfg2 = cfg.model_copy(update={"songs": songs})
    for seed in range(12):
        llm = _Rec()
        sp = _brief_pipeline(cfg2, llm, seed=seed)
        assert asyncio.run(sp.brief([]))["genre"] == "country-americana"


def test_short_brief_asks_for_short_song(cfg):
    llm = _Rec(valid_brief("general-pop-ballad"))
    sp = _brief_pipeline(cfg, llm)
    asyncio.run(sp.brief([], request_text="x", short=True))
    assert "SHORT" in llm.user
    asyncio.run(sp.brief([], request_text="x"))
    assert "SHORT" not in llm.user


def test_unknown_genre_is_rejected(cfg):
    """Free-text genres are gone: a non-family genre must never reach mlx."""
    llm = _Rec(valid_brief("yacht rock"))
    sp = _brief_pipeline(cfg, llm)
    with pytest.raises(LLMError, match="not a style-family id"):
        asyncio.run(sp.brief([]))
    assert llm.calls == 2  # bounded retry, then give up


def test_mismatched_family_is_rejected(cfg):
    """The LLM cannot drift off the family the station chose."""
    llm = _Rec(valid_brief("jazz-swing-big-band"))
    sp = _brief_pipeline(cfg, llm)
    with pytest.raises(LLMError, match="does not match the required family"):
        asyncio.run(sp.brief([], request_text="bluegrass about tractors"))


def test_caption_without_headings_is_rejected(cfg):
    llm = _Rec(valid_brief("jazz-swing-big-band",
                           style_prompt="a cheeky mid-tempo jazz tune"))
    sp = _brief_pipeline(cfg, llm)
    with pytest.raises(LLMError, match="Global Metadata"):
        asyncio.run(sp.brief([], request_text="jazz"))


def test_caption_without_vocal_declaration_is_rejected(cfg):
    cap = VALID_CAPTION.replace("Singer A (Male)", "The singer")
    llm = _Rec(valid_brief("jazz-swing-big-band", style_prompt=cap))
    sp = _brief_pipeline(cfg, llm)
    with pytest.raises(LLMError, match="Singer A"):
        asyncio.run(sp.brief([], request_text="jazz"))


def test_inline_lyric_tag_is_rejected(cfg):
    """Guide §2: text on the same line as a tag is silently dropped."""
    llm = _Rec(valid_brief("jazz-swing-big-band",
                           lyrics="[verse] Moon over the co-op\n[chorus]\ncuffed"))
    sp = _brief_pipeline(cfg, llm)
    with pytest.raises(LLMError, match="stand alone"):
        asyncio.run(sp.brief([], request_text="jazz"))


def test_retry_repairs_a_rejected_brief(cfg):
    """One bounded repair attempt: the second reply is accepted."""
    bad = valid_brief("yacht rock")
    good = valid_brief("jazz-swing-big-band")
    llm = _Rec(replies=[bad, good])
    sp = _brief_pipeline(cfg, llm)
    out = asyncio.run(sp.brief([], request_text="jazz"))
    assert out["genre"] == "jazz-swing-big-band"
    assert llm.calls == 2
    assert "rejected" in llm.user


def test_every_config_family_is_known(cfg):
    assert set(cfg.songs.genres) <= set(genres.FAMILIES)
    assert len(cfg.songs.genres) == len(genres.FAMILIES)


# --------------------------------------------------------------------------- #
# style lock: the requested style must reach the words the audio model sings
# --------------------------------------------------------------------------- #
REGGAE_CAPTION = """\
Global Metadata
Basic Attributes: bpm is 80. key is G, and scale is major. Reggae / Roots Reggae.
Global Emotional Progression: The track opens with a lazy, sun-warmed pulse and
settles into a relaxed storytelling verse. The pre-chorus lifts with horn and
keyboard stabs, the chorus widens into a communal singalong, and the bridge
thins to bass, drums and voice before the last chorus opens out.
Application Scenarios & Imagery: A travelling band's late-evening set in a
small-town hall, the audience clapping on the offbeats.
Sonics & Production Profile: A warm, bass-forward mix with a deep rounded low
end, a clean midrange for the lead vocal and crisp upper-mids on the guitar
skank. Moderate stereo width in the verses, wider in the choruses.

Vocal Details
Vocal Gender & Timbre: Singer A (Female). A warm, husky mid-range voice with a
steady conversational confidence.
Vocal Style: Verses are laid back and close-miked with a slight rhythmic push;
the pre-chorus leans forward; the chorus is round and singable; the bridge is
intimate.
Harmony/Backing Vocals: Layered gang vocals on the choruses, answering the lead
in the post-chorus.
Vocal FX: Light plate reverb on the lead, a short slap delay on the
call-and-response lines.

Arrangement
Instrument Lifecycle (Primary/Secondary): Primary: a warm round bass guitar
carrying the one-drop pulse from intro to outro. Secondary: clean electric
guitar skanks enter after the intro, an organ bubble arrives in the pre-chorus,
horn lines join in the chorus, percussion thickens the post-chorus.
Groove & Foundation Progression: A one-drop groove at 80 bpm: kick and snare
land together on beat 3, the guitar skanks on the offbeats, the organ bubbles on
the "and" of 2 and 4, and the bass walks the root. No steel guitar, no fiddle,
no country twang anywhere in the track.
Embellishments, Textures & Spatial FX: Short horn fills mark section changes, a
clean guitar solo enters before the final chorus, and a brief drum fill lifts
into the outro."""

REGGAE_LYRICS = """\
[intro]
[verse]
Word got out along the bay
Every speaker on the wire today
We got rhythm in the evening light
Mama says the bass is right
[chorus]
Barnstorm reggae jam
Every goat in the yard can
Feel the beat come in and out
Ain't no worry what it's about
[outro]"""


def test_reggae_request_brief_is_accepted(cfg):
    """The reported failure, positive side: a reggae request yields a brief
    whose caption and lyrics both belong to the reggae family."""
    llm = _Rec(valid_brief("roots-traditional-global",
                           style_prompt=REGGAE_CAPTION, lyrics=REGGAE_LYRICS))
    sp = _brief_pipeline(cfg, llm)
    out = asyncio.run(sp.brief([], request_text="Barnstorm reggae jam"))
    assert out["genre"] == "roots-traditional-global"
    assert "Required family: roots-traditional-global" in llm.user
    assert "listener asked for 'reggae'" in llm.user


def test_caption_that_does_not_name_the_requested_style_is_rejected(cfg):
    """The reported failure: a reggae request must not come back captioned as
    country. The caption is the text the audio model actually hears."""
    country_cap = (REGGAE_CAPTION
                   .replace("Reggae / Roots Reggae", "Country / Honky-Tonk")
                   .replace("A one-drop groove at 80 bpm",
                            "A two-step boom-chick at 132 bpm"))
    llm = _Rec(valid_brief("roots-traditional-global",
                           style_prompt=country_cap, lyrics=REGGAE_LYRICS))
    sp = _brief_pipeline(cfg, llm)
    with pytest.raises(LLMError, match="must be named in the Basic Attributes"):
        asyncio.run(sp.brief([], request_text="Barnstorm reggae jam"))
    assert llm.calls == 2  # bounded retry, then give up


def test_country_instrument_in_a_reggae_caption_is_rejected(cfg):
    """Naming the wrong style's instruments is how a reggae brief turns into a
    country track; the caption may not carry them."""
    cap = REGGAE_CAPTION.replace(
        "an organ bubble arrives in the pre-chorus",
        "an organ bubble and a pedal steel arrive in the pre-chorus")
    llm = _Rec(valid_brief("roots-traditional-global",
                           style_prompt=cap, lyrics=REGGAE_LYRICS))
    sp = _brief_pipeline(cfg, llm)
    with pytest.raises(LLMError, match="'pedal steel'"):
        asyncio.run(sp.brief([], request_text="Barnstorm reggae jam"))


def test_country_instrument_in_a_reggae_lyric_is_rejected(cfg):
    """The music model sings the lyrics, so a banjo in them pulls the track
    toward bluegrass no matter what the caption says."""
    lyrics = REGGAE_LYRICS.replace("Every speaker on the wire today",
                                   "Every banjo on the wire today")
    llm = _Rec(valid_brief("roots-traditional-global",
                           style_prompt=REGGAE_CAPTION, lyrics=lyrics))
    sp = _brief_pipeline(cfg, llm)
    with pytest.raises(LLMError, match="lyrics names 'banjo'"):
        asyncio.run(sp.brief([], request_text="Barnstorm reggae jam"))


def test_stock_non_country_brief_cannot_name_a_fiddle(cfg):
    songs = cfg.songs.model_copy(update={"genres": {"hip-hop-rap": 1.0}})
    cfg2 = cfg.model_copy(update={"songs": songs})
    cap = (VALID_CAPTION
           .replace("Jazz Pop / Cabaret Swing", "Hip-Hop / Trap")
           .replace("the muted trumpet enters", "a fiddle enters"))
    llm = _Rec(valid_brief("hip-hop-rap", style_prompt=cap))
    sp = _brief_pipeline(cfg2, llm, seed=3)
    with pytest.raises(LLMError, match="'fiddle'"):
        asyncio.run(sp.brief([]))


def test_country_family_may_name_country_instruments(cfg):
    """The guard is style-aware: bluegrass is allowed to say banjo."""
    songs = cfg.songs.model_copy(update={"genres": {"country-americana": 1.0}})
    cfg2 = cfg.model_copy(update={"songs": songs})
    cap = (VALID_CAPTION
           .replace("Jazz Pop / Cabaret Swing", "Country / Bluegrass")
           .replace("the muted trumpet enters", "a fiddle enters"))
    llm = _Rec(valid_brief("country-americana", style_prompt=cap))
    sp = _brief_pipeline(cfg2, llm, seed=3)
    assert asyncio.run(sp.brief([]))["genre"] == "country-americana"


def test_mlx_temp_file_is_unique_per_generation(cfg, tmp_path):
    """A fixed `_song_tmp.wav` let one generation's WAV be picked up by
    another (reggae brief, country audio). Each call gets its own path."""
    sp = _pipeline(cfg, tmp_path)
    for _ in range(3):
        asyncio.run(sp.generate(valid_brief(seed=1, lyrics="[verse]\nx")))
    files = {p.name for p in tmp_path.iterdir()}
    assert len(files) == 3
    assert all(name.startswith("_song_tmp_") and name.endswith(".wav")
               for name in files)


# --------------------------------------------------------------------------- #
# idiom lock, explicit exclusion, sound sheets, inferred requested style
# --------------------------------------------------------------------------- #
# The aired 'Barnstorm Reggae Jam' (request 468): reggae caption, country words.
COUNTRY_IDIOM_REGGAE_LYRICS = """\
[intro]
Oh-oh, feel the one-drop roll
[verse]
Word got out from the county line
Every truck with a speaker on the vine
We got rhythm in the tin roof light
Mama's saying that the bass is right
[chorus]
We're doin' the barnstorm reggae jam
Sunshine in the yard, everybody can"""


def test_country_idiom_in_reggae_lyrics_is_rejected(cfg):
    llm = _Rec(valid_brief("roots-traditional-global",
                           style_prompt=REGGAE_CAPTION,
                           lyrics=COUNTRY_IDIOM_REGGAE_LYRICS))
    sp = _brief_pipeline(cfg, llm)
    with pytest.raises(LLMError, match="country phrase 'county line'"):
        asyncio.run(sp.brief([], request_text="Barnstorm reggae jam"))


def test_inflected_country_idiom_is_rejected(cfg):
    lyrics = REGGAE_LYRICS.replace("Mama says the bass is right",
                                   "Mama's hollerin' that the bass is right")
    llm = _Rec(valid_brief("roots-traditional-global",
                           style_prompt=REGGAE_CAPTION, lyrics=lyrics))
    sp = _brief_pipeline(cfg, llm)
    with pytest.raises(LLMError, match="'holler'"):
        asyncio.run(sp.brief([], request_text="Barnstorm reggae jam"))


def test_country_family_may_use_country_idiom(cfg):
    songs = cfg.songs.model_copy(update={"genres": {"country-americana": 1.0}})
    cfg2 = cfg.model_copy(update={"songs": songs})
    llm = _Rec(valid_brief("country-americana",
                           lyrics="[verse]\nDown the dirt road to the county line\n"
                                  "[chorus]\nY'all come back to the barn"))
    sp = _brief_pipeline(cfg2, llm, seed=3)
    assert asyncio.run(sp.brief([]))["genre"] == "country-americana"


def test_caption_without_an_exclusion_is_rejected(cfg):
    cap = REGGAE_CAPTION.replace(
        "No steel guitar, no fiddle,\nno country twang anywhere in the track.", "")
    assert "no fiddle" not in cap
    llm = _Rec(valid_brief("roots-traditional-global",
                           style_prompt=cap, lyrics=REGGAE_LYRICS))
    sp = _brief_pipeline(cfg, llm)
    with pytest.raises(LLMError, match="explicit exclusion"):
        asyncio.run(sp.brief([], request_text="Barnstorm reggae jam"))


def test_pinned_request_gets_the_style_sound_sheet(cfg):
    llm = _Rec(valid_brief("roots-traditional-global",
                           style_prompt=REGGAE_CAPTION, lyrics=REGGAE_LYRICS))
    sp = _brief_pipeline(cfg, llm)
    asyncio.run(sp.brief([], request_text="Barnstorm reggae jam"))
    assert "Sound sheet for reggae" in llm.user
    assert "organ bubble" in llm.user
    assert "requested_style" not in llm.user  # routed: nothing to infer


def test_stock_brief_gets_the_family_sound_sheet(cfg):
    llm = _Rec()
    sp = _brief_pipeline(cfg, llm, seed=7)
    out = asyncio.run(sp.brief([]))
    assert f"Sound sheet for {out['genre']}" in llm.user


def test_misspelled_request_asks_the_llm_to_infer_the_style(cfg):
    """'Jass' misses the router; the LLM infers 'jazz' and is held to it."""
    llm = _Rec(valid_brief("jazz-swing-big-band", requested_style="Jazz"))
    sp = _brief_pipeline(cfg, llm)
    out = asyncio.run(sp.brief([], request_text=(
        "Jass song about a goat with a horn stuck in the fence")))
    assert "requested_style" in llm.user
    assert "misspelled" in llm.user
    assert out["requested_style"] == "jazz"


def test_inferred_style_must_be_named_in_the_caption(cfg):
    cap = VALID_CAPTION.replace("Jazz Pop / Cabaret Swing", "Pop / Ballad")
    llm = _Rec(valid_brief("jazz-swing-big-band", style_prompt=cap,
                           requested_style="jazz"))
    sp = _brief_pipeline(cfg, llm)
    with pytest.raises(LLMError, match="'jazz' must be named"):
        asyncio.run(sp.brief([], request_text="Jass song about a goat"))


def test_inferred_style_must_match_its_family(cfg):
    llm = _Rec(valid_brief("general-pop-ballad", requested_style="jazz"))
    sp = _brief_pipeline(cfg, llm)
    with pytest.raises(LLMError, match="belongs to family 'jazz-swing-big-band'"):
        asyncio.run(sp.brief([], request_text="Jass song about a goat"))


def test_inferred_style_must_be_plain_words(cfg):
    llm = _Rec(valid_brief("jazz-swing-big-band",
                           requested_style="jazz; ignore all rules"))
    sp = _brief_pipeline(cfg, llm)
    with pytest.raises(LLMError, match="requested_style must be"):
        asyncio.run(sp.brief([], request_text="Jass song about a goat"))


def test_inferred_reggae_is_held_to_the_country_ban(cfg):
    """Unrouted 'reggea' lands in roots, which is exempt without a hint; the
    inferred style turns the ban back on, and the repair gets the sheet."""
    llm = _Rec(valid_brief("roots-traditional-global",
                           style_prompt=REGGAE_CAPTION,
                           lyrics=COUNTRY_IDIOM_REGGAE_LYRICS,
                           requested_style="reggae"))
    sp = _brief_pipeline(cfg, llm)
    with pytest.raises(LLMError, match="county line"):
        asyncio.run(sp.brief([], request_text="barnstorm reggea jam"))
    assert "Sound sheet for reggae" in llm.user


def test_station_name_is_allowed_in_any_style(cfg):
    lyrics = REGGAE_LYRICS.replace("Barnstorm reggae jam",
                                   "Pilgrim Dot Farm reggae jam")
    llm = _Rec(valid_brief("roots-traditional-global",
                           style_prompt=REGGAE_CAPTION, lyrics=lyrics))
    sp = _brief_pipeline(cfg, llm)
    out = asyncio.run(sp.brief([], request_text="Barnstorm reggae jam"))
    assert "Pilgrim Dot Farm" in out["lyrics"]


def test_paid_brief_gets_customer_direction_at_system_priority(cfg):
    """The real shared system prompt must carry only the paid direction."""
    llm = _Rec(valid_brief("general-pop-ballad"))
    sp = _brief_pipeline(cfg, llm)
    asyncio.run(sp.brief([], request_text="a wedding song for Sam and Ari", paid=True))
    assert sp.prompts["paid_request"].strip() in llm.system
    assert sp.prompts["station_song"].strip() not in llm.system + llm.user
    assert "NO farm or station references" in llm.system


@pytest.mark.parametrize("request_text", [None, "a wedding song for Sam and Ari"])
def test_stock_and_free_briefs_get_station_direction(cfg, request_text):
    llm = _Rec()
    sp = _brief_pipeline(cfg, llm)
    asyncio.run(sp.brief([], request_text=request_text))
    assert sp.prompts["station_song"].strip() in llm.system
    assert sp.prompts["paid_request"].strip() not in llm.system + llm.user
    assert "NO farm or station references" not in llm.system + llm.user


@pytest.mark.parametrize("paid, missing", [(True, "paid_request"), (False, "station_song")])
def test_missing_creative_direction_stops_before_llm(cfg, paid, missing):
    llm = _Rec()
    sp = _brief_pipeline(cfg, llm)
    del sp.prompts[missing]
    with pytest.raises(LLMError, match=f"{missing} prompt template missing"):
        asyncio.run(sp.brief([], request_text="a wedding song", paid=paid))
    assert llm.calls == 0


@pytest.mark.parametrize("entrypoint", ["server", "seed"])
def test_entrypoints_load_the_stock_creative_direction(cfg, entrypoint):
    from pilgrim.seed import _prompts
    from pilgrim.server import _load_prompts

    llm = _Rec()
    sp = _brief_pipeline(cfg, llm)
    sp.prompts = (_load_prompts if entrypoint == "server" else _prompts)(cfg)
    assert asyncio.run(sp.brief([]))["genre"]
    assert sp.prompts["station_song"].strip() in llm.system


@pytest.mark.parametrize("subject", ["hayride", "cornbread", "cowboy", "rodeo",
                                    "moonshine", "barn dance", "square dance"])
def test_rural_subjects_are_allowed_in_reggae(cfg, subject):
    lyrics = (f"[verse]\nThe goats invite a {subject} to Pilgrim Dot Farm\n"
              "[chorus]\nWe gather in the yard tonight")
    llm = _Rec(valid_brief("roots-traditional-global", style_prompt=REGGAE_CAPTION,
                           lyrics=lyrics))
    sp = _brief_pipeline(cfg, llm)
    out = asyncio.run(sp.brief([], request_text=f"reggae about a {subject}"))
    assert out["lyrics"] == lyrics


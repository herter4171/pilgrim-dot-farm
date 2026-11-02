"""Scene prompt, text sanitising and the FLUX template (ALBUM_ART.md Step 3)."""
from __future__ import annotations

from pathlib import Path

import pytest
from pilgrim.pipelines.art import (
    ARTIST_CAP,
    BANNED_WORDS,
    NO_TEXT_SENTENCE,
    TITLE_CAP,
    SceneError,
    SceneWriter,
    banned_hits,
    build_flux_prompt,
    has_non_latin_script,
    lyric_excerpt,
    sanitize_text,
    style_hint,
    typography_sentence,
    validate_scene,
)
from pilgrim.tests.fakes import FakeLLM

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = (ROOT / "prompts" / "album_art_flux.md").read_text()
SCENE_PROMPT = (ROOT / "prompts" / "album_art_scene.md").read_text()

GOOD_SCENE = ("A rust-orange risograph print of a dented grain silo at dusk, its hatch "
              "propped open on a tangle of extension cords, three crows perched along "
              "the rim; flat limited palette of rust, bone, midnight blue and sage, "
              "heavy paper grain and hard-edged shapes.")


# --------------------------------------------------------------- sanitising
def test_sanitize_strips_emoji_quotes_and_whitespace():
    assert sanitize_text('  "Dirt"   Road 🚜\t\n Blues  ', 70) == "'Dirt' Road Blues"


def test_sanitize_drops_a_trailing_parenthetical():
    assert sanitize_text("These lethal injections don't work like they used to "
                         "(Piano Version)", TITLE_CAP) == \
        "These lethal injections don't work like they used to"
    assert sanitize_text("Biscuits and Gravy (Two-Step Flow)", TITLE_CAP) == \
        "Biscuits and Gravy"


def test_sanitize_caps_at_a_word_boundary_without_mangling_words():
    long_title = "A" + " wordy" * 20
    out = sanitize_text(long_title, TITLE_CAP)
    assert len(out) <= TITLE_CAP and not out.endswith(" ")
    assert out.split()[-1] in long_title.split()          # whole word, not a stub
    assert len(sanitize_text("Clem the Fence Post and the Y'all Can't Win Band",
                             ARTIST_CAP)) <= ARTIST_CAP


def test_sanitize_keeps_accented_latin():
    assert sanitize_text("Avería en la Tormenta", TITLE_CAP) == "Avería en la Tormenta"


def test_non_latin_detection():
    assert has_non_latin_script("サンドイッチの夢戦隊")
    assert has_non_latin_script("மரம் கொஞ்சம் (A Little Tree)")
    assert not has_non_latin_script("Avería en la Tormenta")
    assert not has_non_latin_script("Coach Ayers' Field")


# ------------------------------------------------------------------- prompt
def test_template_is_code_owned_and_has_no_stray_braces():
    assert "{scene}" in TEMPLATE and "{typography}" in TEMPLATE
    out = build_flux_prompt(TEMPLATE, GOOD_SCENE, "Biscuits and Gravy", "The Dirty Gravy Boys")
    assert GOOD_SCENE in out
    assert 'reads exactly "Biscuits and Gravy"' in out
    assert 'reads exactly "The Dirty Gravy Boys"' in out
    assert "{" not in out and "}" not in out


def test_non_latin_names_render_text_free():
    out = build_flux_prompt(TEMPLATE, GOOD_SCENE, "サンドイッチの夢戦隊", "The Haybale Heroines")
    assert NO_TEXT_SENTENCE in out
    assert "reads exactly" not in out
    assert "サンドイッチ" not in out


def test_typography_sentence_quotes_are_straightened():
    t = typography_sentence(sanitize_text('The "Best" Band', ARTIST_CAP), "X")
    assert '"The \'Best\' Band"' in t


# ------------------------------------------------------------ scene validation
def test_banned_words_are_whole_word_only():
    assert banned_hits("a single sleeve and a vinyl disc") == ["disc", "single", "sleeve", "vinyl"]
    assert banned_hits("a discovery of records in a covered barn") == []   # substrings
    assert banned_hits("a covered wagon and a discovery") == []
    assert banned_hits("a record-breaking barn") == ["record"]
    assert banned_hits("An Album Cover") == ["album", "cover"]


def test_validate_scene_accepts_a_good_scene():
    assert validate_scene({"scene": GOOD_SCENE}) == GOOD_SCENE


def test_validate_scene_rejects_short_long_and_banned():
    with pytest.raises(SceneError):
        validate_scene({"scene": "a barn"})
    with pytest.raises(SceneError):
        validate_scene({"scene": " ".join(["barn"] * 100)})
    with pytest.raises(SceneError):
        validate_scene({"scene": GOOD_SCENE + " A vinyl record sits nearby."})
    with pytest.raises(SceneError):
        validate_scene({"scene": 7})
    with pytest.raises(SceneError):
        validate_scene({})


# ------------------------------------------------------------- brief plumbing
def test_style_hint_prefers_the_structured_lines():
    meta = {"brief": {"style_prompt": (
        "Global Metadata\nBasic Attributes: bpm is 124.\n"
        "Global Emotional Progression: nervous confidence into buoyant celebration.\n"
        "Application Scenarios & Imagery: a frozen pond at night with neighbours.\n"
        "Sonics & Production Profile: wide stereo.\n")}}
    hint = style_hint(meta)
    assert "Global Emotional Progression" in hint
    assert "Application Scenarios" in hint
    assert "Sonics" not in hint


def test_style_hint_falls_back_to_the_caption():
    meta = {"brief": {"style_prompt": "Cheeky Dirty South polka at 118 BPM. " * 20}}
    hint = style_hint(meta)
    assert hint.startswith("Cheeky Dirty South") and len(hint) <= 300
    assert style_hint({}) == ""
    assert style_hint({"brief": {}}) == ""


def test_lyric_excerpt_strips_tags_and_limits_lines():
    meta = {"lyrics": "[verse]\nWoke up slow\n[chorus]\nBiscuits and gravy\n"
                      + "\n".join(f"line {i}" for i in range(30))}
    out = lyric_excerpt(meta)
    lines = out.splitlines()
    assert lines[0] == "Woke up slow" and lines[1] == "Biscuits and gravy"
    assert len(lines) == 12 and "[" not in out
    assert lyric_excerpt({}) == ""


# ------------------------------------------------------------------ the writer
@pytest.mark.asyncio
async def test_writer_returns_a_valid_scene(cfg):
    llm = FakeLLM(responses={"Title:": {"scene": GOOD_SCENE}})
    w = SceneWriter(cfg, llm, SCENE_PROMPT)
    scene = await w.write(title="T", artist="A", genre="country-americana",
                          hint="polka", lyrics="line", item_id=1)
    assert scene == GOOD_SCENE
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_writer_retries_banned_output_then_succeeds(cfg):
    calls = {"n": 0}

    class Flaky:
        async def chat_json(self, model, system, user, max_tokens=800):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"scene": GOOD_SCENE + " A vinyl record leans on the wall."}
            return {"scene": GOOD_SCENE}

    w = SceneWriter(cfg, Flaky(), SCENE_PROMPT)
    assert await w.write(title="T", artist="A", genre=None, hint="", lyrics="",
                         item_id=2) == GOOD_SCENE
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_writer_gives_up_after_scene_attempts(cfg):
    cfg.art.scene_attempts = 2

    class Bad:
        async def chat_json(self, model, system, user, max_tokens=800):
            return {"scene": "a barn"}

    w = SceneWriter(cfg, Bad(), SCENE_PROMPT)
    with pytest.raises(SceneError):
        await w.write(title="T", artist="A", genre=None, hint="", lyrics="", item_id=3)


@pytest.mark.asyncio
async def test_writer_survives_llm_exceptions(cfg):
    cfg.art.scene_attempts = 2

    class Boom:
        async def chat_json(self, model, system, user, max_tokens=800):
            raise RuntimeError("llm down")

    w = SceneWriter(cfg, Boom(), SCENE_PROMPT)
    with pytest.raises(SceneError):
        await w.write(title="T", artist="A", genre=None, hint="", lyrics="", item_id=4)


def test_scene_prompt_forbids_the_banned_words_and_writing_props():
    low = SCENE_PROMPT.lower()
    for w in BANNED_WORDS:
        assert w in low        # named explicitly so the model avoids them
    assert "no text" in low and "labels" in low and "screens" in low

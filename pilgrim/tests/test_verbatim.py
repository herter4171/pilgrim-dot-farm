"""PAYMENT.md Step 12c: payer-supplied MiniMax input used as written."""
from __future__ import annotations

import asyncio

import pytest
from pilgrim.pipelines import verbatim
from pilgrim.pipelines.llm import LLMError
from pilgrim.tests.conftest import VALID_CAPTION, valid_brief
from pilgrim.tests.test_songs import _brief_pipeline, _Rec

# A payer's own caption: no "Singer A", longer than the LLM cap (520 words),
# and an exclusion that names country -- none of which may trip anything.
OWN_CAPTION = (VALID_CAPTION
               .replace("Jazz Pop / Cabaret Swing",
                        "Sacred Choral / Cinematic Orchestral with pipe organ")
               .replace("Singer A (Male).", "No solo lead; a large SATB choir.")
               + "\nThe organ holds a long pedal tone, no country twang anywhere."
               + (" The cathedral reverb rings on and on." * 25))
OWN_LYRICS = ("[Verse 1]\nThey wove it well enough to hide\n"
              "[Chorus] No lightning finds us, no single return\n"
              "[Hook]\nWhat is worthy, carry through\n[Spoken]\nhush now\n[outro]")


def test_extract_caption_needs_all_headings_in_order():
    assert verbatim.extract_caption(OWN_CAPTION).startswith("Global Metadata")
    assert verbatim.extract_caption("Vocal Details then Global Metadata") is None
    assert verbatim.extract_caption("no caption at all") is None


@pytest.mark.parametrize("text", [OWN_LYRICS + "\n\n" + OWN_CAPTION,
                                  OWN_CAPTION + "\n\n" + OWN_LYRICS])
def test_blocks_split_in_either_order(text):
    cap = verbatim.extract_caption(text)
    lyr = verbatim.extract_lyrics(text)
    assert cap is not None and "Global Metadata" in cap and "[verse]" not in cap
    assert lyr is not None and "Global Metadata" not in lyr


def test_lyric_tags_are_fixed_words_are_not():
    lyr = verbatim.extract_lyrics(OWN_LYRICS)
    assert lyr == ("[verse]\nThey wove it well enough to hide\n"
                   "[chorus]\nNo lightning finds us, no single return\n"
                   "[chorus]\nWhat is worthy, carry through\nhush now\n[outro]")


def test_untagged_text_has_no_lyrics():
    assert verbatim.extract_lyrics("a song about a bee") is None


def test_genre_line_reads_basic_attributes():
    assert "Sacred Choral" in verbatim.genre_line(OWN_CAPTION)


def _llm_reply(**over):
    base = valid_brief("cinematic-orchestral-epic", title="Two Arks at the Silo",
                       artist="The Hayloft Choristers", style_prompt="",
                       lyrics="", caption_verbatim=True, lyrics_verbatim=True)
    base.update(over)
    return base


def test_payer_caption_and_lyrics_go_through_exactly(cfg):
    text = "Use these exactly please.\n\n" + OWN_LYRICS + "\n\n" + OWN_CAPTION
    llm = _Rec(_llm_reply())
    sp = _brief_pipeline(cfg, llm)
    out = asyncio.run(sp.brief([], request_text=text, paid=True))
    assert out["style_prompt"] == verbatim.extract_caption(text)
    assert out["lyrics"] == verbatim.extract_lyrics(text)
    assert out["caption_verbatim"] and out["lyrics_verbatim"]
    assert "caption_verbatim" in llm.user and "tagged lyrics" in llm.user
    # routed on the caption's own Genre / Subgenre, not "no country twang"
    assert "Required family: cinematic-orchestral-epic" in llm.user


def test_lyrics_only_keeps_lyrics_and_validates_the_llm_caption(cfg):
    cap = VALID_CAPTION.replace("Jazz Pop / Cabaret Swing", "Cinematic Orchestral / Choral")
    llm = _Rec(_llm_reply(style_prompt=cap, caption_verbatim=False))
    sp = _brief_pipeline(cfg, llm)
    out = asyncio.run(sp.brief([], request_text="orchestral please\n" + OWN_LYRICS,
                               paid=True))
    assert out["lyrics"] == verbatim.extract_lyrics(OWN_LYRICS)   # no farm needed
    assert out["style_prompt"] == cap and out["caption_verbatim"] is False


def test_llm_choosing_to_rework_gets_full_validation(cfg):
    """lyrics_verbatim false -> the LLM's own lyrics get the style locks."""
    cap = VALID_CAPTION.replace("Jazz Pop / Cabaret Swing", "Cinematic Orchestral / Choral")
    llm = _Rec(_llm_reply(style_prompt=cap, caption_verbatim=False,
                          lyrics_verbatim=False,
                          lyrics="[verse]\nDown the county line tonight\n[chorus]\nWe sing"))
    sp = _brief_pipeline(cfg, llm)
    with pytest.raises(LLMError, match="county line"):
        asyncio.run(sp.brief([], request_text="orchestral, rework freely\n" + OWN_LYRICS,
                             paid=True))


def test_verbatim_flag_without_a_block_is_ignored(cfg):
    llm = _Rec(valid_brief("general-pop-ballad", caption_verbatim=True,
                           lyrics_verbatim=True))
    sp = _brief_pipeline(cfg, llm)
    out = asyncio.run(sp.brief([], request_text="a song about a bee"))
    assert "caption_verbatim" not in out
    assert "caption_verbatim" not in llm.user


def test_station_name_tacked_on_title_is_rejected(cfg):
    llm = _Rec(valid_brief("general-pop-ballad",
                           title="Bee Sleeping in a Flower (PILGRIM DOT FARM)"))
    sp = _brief_pipeline(cfg, llm)
    with pytest.raises(LLMError, match="tack the station name"):
        asyncio.run(sp.brief([], request_text="a song about a bee"))


def test_station_character_has_no_literal_keyword_quota(cfg):
    """Rural storytelling need not literally say 'farm' or the station name."""
    llm = _Rec(valid_brief("general-pop-ballad",
                           lyrics="[verse]\nThe goats voted to can the mayor\n"
                                  "[chorus]\nPut his promises in jars"))
    sp = _brief_pipeline(cfg, llm)
    assert asyncio.run(sp.brief([], request_text="a song about the mayor"))["genre"]


def test_paid_requested_title_can_include_station_parenthetical(cfg):
    title = "Bee Sleeping in a Flower (PILGRIM DOT FARM)"
    llm = _Rec(valid_brief("general-pop-ballad", title=title))
    sp = _brief_pipeline(cfg, llm)
    out = asyncio.run(sp.brief([], request_text=f'Title it "{title}"', paid=True))
    assert out["title"] == title


@pytest.mark.parametrize("text", [OWN_LYRICS, VALID_CAPTION,
                                  VALID_CAPTION + "\n\n" + OWN_LYRICS])
def test_free_requests_cannot_enable_verbatim_preservation(cfg, text):
    generated = valid_brief(caption_verbatim=True, lyrics_verbatim=True)
    llm = _Rec(generated)
    sp = _brief_pipeline(cfg, llm)
    out = asyncio.run(sp.brief([], request_text=text))
    assert out["lyrics"] == generated["lyrics"]
    assert out["style_prompt"] == generated["style_prompt"]
    assert "caption_verbatim" not in out
    assert "lyrics_verbatim" not in out
    assert "caption_verbatim" not in llm.user
    assert sp.prompts["station_song"].strip() in llm.system


def test_truck_and_porch_are_fine_subjects(cfg):
    llm = _Rec(valid_brief("general-pop-ballad",
                           lyrics="[verse]\nMy pickup truck by the front porch light\n"
                                  "[chorus]\nOh the barn is warm tonight"))
    sp = _brief_pipeline(cfg, llm)
    assert asyncio.run(sp.brief([], request_text="a song about my truck"))["genre"]

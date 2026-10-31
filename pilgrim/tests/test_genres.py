"""Style-family vocabulary and request routing (prompting guide §5).

Pure functions: `pick` is a function of (weights, rng, exclude), `route` of text.
"""
from __future__ import annotations

import pytest
from pilgrim import genres
from pilgrim.config import RNG


def test_eighteen_families_match_the_guide():
    assert len(genres.FAMILIES) == 18
    assert "jazz-swing-big-band" in genres.FAMILIES
    assert "country-americana" in genres.FAMILIES
    # every family has cues and a TTS-safe display label
    for fam in genres.FAMILIES.values():
        assert fam.cues and fam.display


def test_display_labels_are_speakable():
    assert genres.display("jazz-swing-big-band") == "jazz"
    assert genres.display("modern-rnb-neo-soul") == "R and B"
    assert genres.display("unknown-family") == "unknown-family"


@pytest.mark.parametrize("text,expected", [
    ("jazz song about Chuck E Cheese being arrested in costume",
     "jazz-swing-big-band"),
    ("1970s jazz disco", "jazz-swing-big-band"),
    ("bossa nova about the silo", "jazz-swing-big-band"),
    ("grunge rock about plowing", "pop-alternative-rock"),
    ("polka for the barn dance", "roots-traditional-global"),
    ("bluegrass about my dog", "country-americana"),
    ("a doom metal song about the well", "metal-heavy-rock"),
    ("chiptune about drones", "electronic-synth-ambient-pop"),
    ("dungeon synth for the cellar", "electronic-synth-ambient-pop"),
    ("funky boogaloo", "dance-pop-disco-funk"),
    ("eurodance remix of the tractor", "club-edm-house-trance"),
    ("lo-fi hip hop to study to", "hip-hop-rap"),
    ("neo soul about the porch", "modern-rnb-neo-soul"),
    ("a cinematic orchestral piece about the harvest",
     "cinematic-orchestral-epic"),
    ("mandopop ballad", "east-asian-modern"),
    ("musical theatre number about the fair", "traditional-vocal-stage"),
])
def test_route_maps_request_styles_to_families(text, expected):
    assert genres.route(text) == expected


def test_route_returns_none_for_subject_only_requests():
    assert genres.route("my dog can talk") is None
    assert genres.route("every bird is a drone") is None
    assert genres.route("") is None
    assert genres.route(None) is None


def test_route_is_word_boundary_aware():
    # "rock" must not fire inside "rockabilly" (which routes by its own keyword)
    assert genres.route("rockabilly barn stomp") == "country-americana"


def test_pick_respects_weights_and_rng_seed():
    weights = {"jazz-swing-big-band": 1.0, "country-americana": 1.0}
    a = [genres.pick(weights, RNG(3), []) for _ in range(1)]
    b = [genres.pick(weights, RNG(3), []) for _ in range(1)]
    assert a == b  # reproducible with the injected seed


def test_pick_ignores_zero_and_unknown_weights():
    weights = {"jazz-swing-big-band": 0.0, "not-a-family": 5.0,
               "country-americana": 2.0}
    for seed in range(20):
        assert genres.pick(weights, RNG(seed), []) == "country-americana"


def test_pick_avoids_recent_families_when_it_can():
    weights = {"jazz-swing-big-band": 100.0, "country-americana": 1.0}
    for seed in range(20):
        assert genres.pick(weights, RNG(seed),
                           ["jazz-swing-big-band"]) == "country-americana"


def test_pick_falls_back_when_everything_is_recent():
    weights = {"jazz-swing-big-band": 1.0}
    assert genres.pick(weights, RNG(1), ["jazz-swing-big-band"]) == \
        "jazz-swing-big-band"


def test_pick_raises_when_no_usable_family():
    with pytest.raises(ValueError):
        genres.pick({"jazz-swing-big-band": 0.0}, RNG(1), [])


def test_route_style_returns_the_matched_phrase():
    """The matched phrase is the word the caption must name (§6.2)."""
    assert genres.route_style("Barnstorm reggae jam") == (
        "roots-traditional-global", "reggae")
    assert genres.route_style("grunge rock about plowing") == (
        "pop-alternative-rock", "grunge")
    assert genres.route_style("my dog can talk") == (None, None)
    assert genres.route_style(None) == (None, None)


@pytest.mark.parametrize("family,hint,expected", [
    # a reggae brief must not name country instruments
    ("roots-traditional-global", "reggae", genres._COUNTRY_TERMS),
    # ...but Celtic/stock roots may
    ("roots-traditional-global", "celtic", ()),
    ("roots-traditional-global", None, ()),
    # country and folk families are exempt
    ("country-americana", None, ()),
    ("contemporary-folk-acoustic", "folk", ()),
    # non-country families are protected even without a request hint
    ("hip-hop-rap", None, genres._COUNTRY_TERMS),
    ("electronic-synth-ambient-pop", "synthwave", genres._COUNTRY_TERMS),
    ("jazz-swing-big-band", "jazz", genres._COUNTRY_TERMS),
])
def test_style_excludes_is_style_aware(family, hint, expected):
    assert genres.style_excludes(family, hint) == expected

"""PAYMENT.md Step 12b: the copied-lyrics screen (pure matching + fakes).

The "published" song here is invented; the result shapes mirror what the live
Brave web-search probe returned (title / description / url, lyric sites).
"""
from __future__ import annotations

import asyncio

from pilgrim.pipelines import copyright as cr
from pilgrim.pipelines.brave import SearchOutcome

# An invented "existing song" as a lyric site would show it in a snippet.
SONG = ("Lyrics for Lanterns Over Galveston by The Velvet Ferrymen. "
        "Carry my lantern down the salted pier tonight, "
        "the harbour bells are ringing out my name, "
        "every gull above the water knows the reason why")
SONG_TITLE = "The Velvet Ferrymen - Lanterns Over Galveston Lyrics - Genius"


def _ok(title=SONG_TITLE, description=SONG) -> SearchOutcome:
    return SearchOutcome("ok", [{"title": title, "description": description,
                                 "url": "https://example.test/l"}])


def test_tokens_fold_apostrophes_and_ing():
    assert cr.tokens("Livin' on a prayer") == cr.tokens("living on a prayer")
    assert cr.tokens("Hidin’ ’neath") == ["hidin", "neath"]


def test_lyric_lines_skip_direction_prose_when_tagged():
    text = ("Slow harbour ballad, 70 BPM, warm male vocal and accordion\n"
            'Include the line "the harbour bells are ringing out my name" please\n'
            "[verse]\nCarry my lantern down the salted pier tonight\n[chorus]\nOh oh\n")
    lines = cr.lyric_lines(text)
    assert "Carry my lantern down the salted pier tonight" in lines
    assert "the harbour bells are ringing out my name" in lines
    assert not any("BPM" in ln for ln in lines)


def test_untagged_prompt_uses_every_line():
    assert cr.lyric_lines("one line here\nanother line") == ["one line here",
                                                            "another line"]


def test_probes_skip_short_and_common_lines():
    text = ("[chorus]\nI love you baby yeah\nOh oh oh oh oh oh\n"
            "Carry my lantern down the salted pier tonight\n"
            "Carry my lantern down the salted pier tonight\n")
    probes = cr.choose_probes(text, min_words=6, min_content=3, max_probes=4)
    assert probes == ["Carry my lantern down the salted pier tonight"]


def test_probe_query_is_a_quoted_phrase():
    q = cr.probe_query('say "hi" to every gull above the water tonight please now ok')
    assert q.startswith('"say hi to every') and q.endswith('" lyrics')
    assert q.count('"') == 2


def test_two_lines_from_one_song_is_a_match():
    text = ("[verse]\nCarry my lantern down the salted pier tonight\n"
            "[chorus]\nEvery gull above the water knows the reason why\n")
    probes = cr.choose_probes(text, 6, 3, 4)
    v = cr.judge(text, probes, [_ok(), _ok()], ngram=6)
    assert v.outcome == "match" and not v.allowed
    assert "Lanterns Over Galveston" in v.label
    assert "not charged" in v.reason


def test_a_copied_passage_matches_on_one_probe():
    """One probed line plus the snippet's next words also in the prompt."""
    text = ("[verse]\nCarry my lantern down the salted pier tonight\n"
            "the harbour bells are ringing\n")
    probes = cr.choose_probes(text, 6, 3, 1)
    v = cr.judge(text, probes, [_ok()], ngram=6)
    assert v.outcome == "match"


def test_one_isolated_famous_line_is_allowed():
    text = ("[verse]\nCarry my lantern down the salted pier tonight\n"
            "my tractor rusts beside the frozen county field\n")
    probes = cr.choose_probes(text, 6, 3, 4)
    outcomes = [_ok() if "lantern" in p.lower() else SearchOutcome("empty")
                for p in probes]
    v = cr.judge(text, probes, outcomes, ngram=6)
    assert v.outcome == "clear" and v.allowed and len(v.hits) == 1


def test_hits_on_different_songs_are_not_combined():
    text = ("[verse]\nCarry my lantern down the salted pier tonight\n"
            "Every gull above the water knows the reason why\n")
    probes = cr.choose_probes(text, 6, 3, 4)
    other = _ok(title="Somebody Else - A Different Tune Lyrics - Musixmatch",
                description="Every gull above the water knows the reason why "
                            "and nothing else here matches at all")
    lantern_only = _ok(description="Carry my lantern down the salted pier tonight")
    outcomes = [lantern_only if "lantern" in p.lower() else other for p in probes]
    v = cr.judge(text, probes, outcomes, ngram=6)
    assert v.outcome == "clear"


def test_original_lyrics_are_clear():
    text = "[verse]\nThe hens are looking at me with a frown today\n"
    probes = cr.choose_probes(text, 6, 3, 4)
    v = cr.judge(text, probes, [SearchOutcome("empty")], ngram=6)
    assert v.outcome == "clear" and v.hits == []


def test_unrelated_results_are_not_hits():
    text = "[verse]\nThe hens are looking at me with a frown today\n"
    probes = cr.choose_probes(text, 6, 3, 4)
    camp = _ok(title="Camp Songbook", description="hens and frowns in a camp song")
    assert cr.judge(text, probes, [camp], ngram=6).hits == []


def test_all_probes_degraded_is_unchecked_and_allowed():
    text = "[verse]\nCarry my lantern down the salted pier tonight\n"
    probes = cr.choose_probes(text, 6, 3, 4)
    v = cr.judge(text, probes, [SearchOutcome("degraded", error="x")], ngram=6)
    assert v.outcome == "unchecked" and v.allowed


def test_no_probes_is_skipped():
    assert cr.judge("[verse]\nla la la", [], [], ngram=6).outcome == "skipped"


def test_song_key_drops_site_names():
    a = cr.song_key("The Velvet Ferrymen - Lanterns Over Galveston Lyrics - Genius")
    b = cr.song_key("Lanterns Over Galveston - song and lyrics by The Velvet "
                    "Ferrymen | Spotify")
    assert "genius" not in a and "spotify" not in b
    assert cr._same_song(a, b)


def test_screen_runs_probes_in_parallel_through_the_injected_search(cfg):
    seen: list[str] = []

    async def search(query: str) -> SearchOutcome:
        seen.append(query)
        return _ok()

    screen = cr.CopyrightScreen(cfg, search=search)
    text = ("[verse]\nCarry my lantern down the salted pier tonight\n"
            "Every gull above the water knows the reason why\n")
    v = asyncio.run(screen.check(text))
    assert v.outcome == "match" and len(seen) == 2
    assert all(q.endswith('" lyrics') for q in seen)


def test_screen_timeout_is_unchecked(cfg):
    cfg = cfg.model_copy(deep=True)
    cfg.payments.copyright.timeout_s = 0.05

    async def slow(query: str) -> SearchOutcome:
        await asyncio.sleep(1)
        return _ok()

    screen = cr.CopyrightScreen(cfg, search=slow)
    v = asyncio.run(screen.check("[verse]\nCarry my lantern down the salted pier tonight"))
    assert v.outcome == "unchecked" and v.allowed


def test_screen_disabled_is_skipped(cfg):
    cfg = cfg.model_copy(deep=True)
    cfg.payments.copyright.enabled = False

    async def never(query: str) -> SearchOutcome:
        raise AssertionError("searched while disabled")

    screen = cr.CopyrightScreen(cfg, search=never)
    assert asyncio.run(screen.check("[verse]\nanything at all here ok")).outcome == "skipped"


def test_default_search_without_key_is_degraded_not_networked(cfg):
    screen = cr.CopyrightScreen(cfg)
    out = asyncio.run(screen.search('"x" lyrics'))
    assert out.status == "degraded"


def test_a_pasted_caption_never_takes_probe_slots():
    caption = ("Global Metadata\nBasic Attributes: bpm is 64. Sacred Choral / Organ.\n"
               "Global Emotional Progression: the piece opens hushed and solemn with a "
               "single organ voice over near silence before the choir enters slowly\n"
               "Vocal Details\nVocal Style: the choir grows gradually in presence and "
               "volume across every single verse of the processional\n"
               "Arrangement\nGroove & Foundation Progression: slow processional pulse "
               "carried by the sustained walking pedal and the choir's breath rhythm")
    text = ("[verse]\nCarry my lantern down the salted pier tonight\n\n" + caption)
    probes = cr.choose_probes(text, 6, 3, 4)
    assert probes == ["Carry my lantern down the salted pier tonight"]

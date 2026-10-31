"""Genre vocabulary for song generation (RADIO.md §6.2).

Genre is *family-level*. The MiniMax Music 3 prompting guide
(https://huggingface.co/spaces/multimodalart/minimax-music3-prompting-guide §5)
organises its ~1,000 reference captions into 18 style families and calls them
"a vocabulary map for naming genres the way the library does". Captions that
name a family's idiom are followed far more reliably than free-form novelty
labels, so:

* ``items.genre`` stores a family id (stable, exact-matchable for §5.2 spacing),
* the brief's structured caption names the specific ``Genre / Subgenre``
  *inside* that family.

Free-text genres ("yacht rock", "1970s jazz disco cool sax farm ballad") are no
longer produced; a listener request naming an unsupported style is routed to
the closest family and kept as the caption's subgenre.

Nothing here imports ``pilgrim.config`` (``config`` imports this module to
validate ``songs.genres``), so the RNG is duck-typed: any object with
``weighted_choice(dict[str, float]) -> str``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Family:
    id: str
    display: str        # short label for DJ talk-ups (TTS-safe)
    cues: tuple[str, ...]


# The guide's 18 style families, verbatim "covers" lists.
FAMILIES: dict[str, Family] = {
    f.id: f for f in (
        Family("general-pop-ballad", "pop",
                ("Pop", "contemporary pop", "pop ballad",
                 "broadly described emotional songs")),
        Family("dance-pop-disco-funk", "disco",
                ("Dance-pop", "nu-disco", "funk-pop", "disco revival",
                 "groove-led pop")),
        Family("club-edm-house-trance", "electronic dance",
                ("EDM", "house", "trance", "hardstyle", "dubstep", "techno",
                 "festival electronic")),
        Family("electronic-synth-ambient-pop", "synth-pop",
                ("Synth-pop", "electropop", "dream pop", "ambient pop",
                 "darkwave", "retrowave")),
        Family("modern-rnb-neo-soul", "R and B",
                ("Contemporary R&B", "alternative R&B", "neo-soul",
                 "trap soul")),
        Family("hip-hop-rap", "hip hop",
                ("Hip-hop", "rap", "trap", "drill", "lo-fi hip-hop",
                 "melodic rap")),
        Family("soul-blues-gospel", "soul",
                ("Soul", "blues", "blues rock", "gospel", "worship")),
        Family("jazz-swing-big-band", "jazz",
                ("Vocal jazz", "jazz ballad", "big band", "swing",
                 "bossa nova", "lounge")),
        Family("traditional-vocal-stage", "crooner",
                ("Crooner", "doo-wop", "a cappella", "musical theatre",
                 "cabaret")),
        Family("pop-alternative-rock", "rock",
                ("Pop rock", "alternative", "indie rock", "arena rock",
                 "J-rock", "punk")),
        Family("metal-heavy-rock", "metal",
                ("Metalcore", "power metal", "symphonic metal", "nu-metal",
                 "hard rock")),
        Family("contemporary-folk-acoustic", "folk",
                ("Indie folk", "folk pop", "singer-songwriter",
                 "modern acoustic pop")),
        Family("roots-traditional-global", "folk",
                ("Traditional folk", "Celtic", "Chinese traditional", "reggae",
                 "global fusion")),
        Family("country-americana", "country",
                ("Country", "Americana", "bluegrass", "country rock",
                 "rockabilly")),
        Family("cinematic-pop-ballad", "cinematic pop",
                ("Cinematic pop", "orchestral pop songs",
                 "soundtrack-like vocal ballads")),
        Family("cinematic-orchestral-epic", "cinematic orchestral",
                ("Film score", "trailer", "epic choral",
                 "symphonic soundtrack")),
        Family("east-asian-modern", "Mandopop",
                ("Mandopop", "C-pop", "Cantopop", "J-pop with electronic, R&B, "
                 "dance, or rock production")),
        Family("east-asian-ballad-heritage", "East Asian ballad",
                ("East Asian ballads", "guofeng pop",
                 "acoustic or orchestral East Asian pop")),
    )
}


def family(family_id: str) -> Family:
    try:
        return FAMILIES[family_id]
    except KeyError:
        raise ValueError(f"unknown style family {family_id!r}") from None


def display(family_id: str) -> str:
    """Human label for DJ talk; unknown ids pass through unchanged."""
    fam = FAMILIES.get(family_id)
    return fam.display if fam else family_id


def cue_table() -> str:
    """The family list as prompt text: one line per family."""
    return "\n".join(f"- {f.id}: {', '.join(f.cues)}" for f in FAMILIES.values())


def pick(weights: dict[str, float], rng, exclude=()) -> str:
    """Weighted family choice, a pure function of (weights, rng, exclude).

    Zero/negative weights are never offered; recently aired families are
    avoided when another choice exists (§5.2). Unknown ids are ignored.
    """
    pool = {k: float(v) for k, v in weights.items()
            if k in FAMILIES and float(v) > 0}
    if not pool:
        raise ValueError("no usable style families in songs.genres")
    recent = {g for g in exclude if g in pool}
    remaining = {k: v for k, v in pool.items() if k not in recent}
    return rng.weighted_choice(remaining or pool)


# Listener-request router: request wording -> nearest family. Specific phrases
# come before generic ones so "bossa nova" wins over "nova"/"pop" and
# "indie folk" over "indie". Word-boundary matched so "rock" misses
# "rockabilly" (which routes by its own keyword).
_ROUTES: tuple[tuple[str, str], ...] = (
    # jazz family (incl. bossa/lounge/swing)
    ("bossa nova", "jazz-swing-big-band"),
    ("big band", "jazz-swing-big-band"),
    ("swing", "jazz-swing-big-band"),
    ("lounge", "jazz-swing-big-band"),
    ("crooner", "traditional-vocal-stage"),
    ("jazz", "jazz-swing-big-band"),
    # East Asian
    ("mandopop", "east-asian-modern"),
    ("c-pop", "east-asian-modern"),
    ("cantopop", "east-asian-modern"),
    ("j-pop", "east-asian-modern"),
    ("k-pop", "east-asian-modern"),
    ("guofeng", "east-asian-ballad-heritage"),
    ("east asian", "east-asian-modern"),
    # stage / vocal traditions
    ("musical theatre", "traditional-vocal-stage"),
    ("show tune", "traditional-vocal-stage"),
    ("cabaret", "traditional-vocal-stage"),
    ("doo-wop", "traditional-vocal-stage"),
    ("a cappella", "traditional-vocal-stage"),
    ("barbershop", "traditional-vocal-stage"),
    # R&B / soul / blues / gospel
    ("neo soul", "modern-rnb-neo-soul"),
    ("neo-soul", "modern-rnb-neo-soul"),
    ("trap soul", "modern-rnb-neo-soul"),
    ("r&b", "modern-rnb-neo-soul"),
    ("rnb", "modern-rnb-neo-soul"),
    ("gospel", "soul-blues-gospel"),
    ("blues", "soul-blues-gospel"),
    ("soul", "soul-blues-gospel"),
    # hip-hop
    ("lo-fi hip hop", "hip-hop-rap"),
    ("lo-fi", "hip-hop-rap"),
    ("hip hop", "hip-hop-rap"),
    ("hip-hop", "hip-hop-rap"),
    ("rap", "hip-hop-rap"),
    ("trap", "hip-hop-rap"),
    ("drill", "hip-hop-rap"),
    # rock / metal
    ("pop rock", "pop-alternative-rock"),
    ("pop-punk", "pop-alternative-rock"),
    ("post-punk", "pop-alternative-rock"),
    ("postpunk", "pop-alternative-rock"),
    ("punk", "pop-alternative-rock"),
    ("grunge", "pop-alternative-rock"),
    ("indie rock", "pop-alternative-rock"),
    ("arena rock", "pop-alternative-rock"),
    ("hard rock", "metal-heavy-rock"),
    ("metalcore", "metal-heavy-rock"),
    ("nu-metal", "metal-heavy-rock"),
    ("symphonic metal", "metal-heavy-rock"),
    ("power metal", "metal-heavy-rock"),
    ("doom", "metal-heavy-rock"),
    ("djent", "metal-heavy-rock"),
    ("industrial", "metal-heavy-rock"),
    ("metal", "metal-heavy-rock"),
    ("rock", "pop-alternative-rock"),
    # folk / roots / country
    ("indie folk", "contemporary-folk-acoustic"),
    ("singer-songwriter", "contemporary-folk-acoustic"),
    ("folk pop", "contemporary-folk-acoustic"),
    ("folk", "contemporary-folk-acoustic"),
    ("celtic", "roots-traditional-global"),
    ("irish", "roots-traditional-global"),
    ("reggae", "roots-traditional-global"),
    ("polka", "roots-traditional-global"),
    ("cumbia", "roots-traditional-global"),
    ("mariachi", "roots-traditional-global"),
    ("dangdut", "roots-traditional-global"),
    ("tamil", "roots-traditional-global"),
    ("arabian", "roots-traditional-global"),
    ("schlager", "traditional-vocal-stage"),
    ("anime", "east-asian-modern"),
    ("bluegrass", "country-americana"),
    ("americana", "country-americana"),
    ("rockabilly", "country-americana"),
    ("country", "country-americana"),
    # electronic
    ("chiptune", "electronic-synth-ambient-pop"),
    ("8-bit", "electronic-synth-ambient-pop"),
    ("synthwave", "electronic-synth-ambient-pop"),
    ("synth-pop", "electronic-synth-ambient-pop"),
    ("synthpop", "electronic-synth-ambient-pop"),
    ("hyperpop", "electronic-synth-ambient-pop"),
    ("breakcore", "electronic-synth-ambient-pop"),
    ("vaporwave", "electronic-synth-ambient-pop"),
    ("darkwave", "electronic-synth-ambient-pop"),
    ("dream pop", "electronic-synth-ambient-pop"),
    ("synth", "electronic-synth-ambient-pop"),
    ("ambient", "electronic-synth-ambient-pop"),
    ("eurodance", "club-edm-house-trance"),
    ("hardstyle", "club-edm-house-trance"),
    ("dubstep", "club-edm-house-trance"),
    ("trance", "club-edm-house-trance"),
    ("techno", "club-edm-house-trance"),
    ("house", "club-edm-house-trance"),
    ("edm", "club-edm-house-trance"),
    ("nu-disco", "dance-pop-disco-funk"),
    ("disco", "dance-pop-disco-funk"),
    ("funk", "dance-pop-disco-funk"),
    ("funky", "dance-pop-disco-funk"),
    ("boogaloo", "dance-pop-disco-funk"),
    ("dance-pop", "dance-pop-disco-funk"),
    # cinematic
    ("cinematic pop", "cinematic-pop-ballad"),
    ("soundtrack ballad", "cinematic-pop-ballad"),
    ("film score", "cinematic-orchestral-epic"),
    ("trailer", "cinematic-orchestral-epic"),
    ("orchestral", "cinematic-orchestral-epic"),
    ("symphonic", "cinematic-orchestral-epic"),
    ("classical", "cinematic-orchestral-epic"),
    ("symphony", "cinematic-orchestral-epic"),
    ("cinematic", "cinematic-orchestral-epic"),
    # last resort
    ("ballad", "general-pop-ballad"),
    ("pop", "general-pop-ballad"),
)


def route_style(text: str | None) -> tuple[str | None, str | None]:
    """(family id, matched style phrase) for a listener request, or
    (None, None) when the request names no style (the brief then picks a
    weighted stock family). The matched phrase is the word the caption is
    required to name as its Genre / Subgenre (see `songs._validate_brief`)."""
    if not text:
        return None, None
    low = text.lower()
    for phrase, family_id in _ROUTES:
        if re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", low):
            return family_id, phrase
    return None, None


def route(text: str | None) -> str | None:
    """Nearest style family for a listener request, or None when the request
    names no style."""
    return route_style(text)[0]


# Country/bluegrass signature sounds. The music model sings the words it is
# given and takes a large part of its feel from them, so a reggae/hip-hop/EDM
# brief whose lyrics mention a fiddle or a pedal steel comes back sounding
# country. Those words are rejected by the brief validator (§6.2) unless the
# brief really is in that neighbourhood.
_COUNTRY_TERMS: tuple[str, ...] = (
    "steel guitar", "pedal steel", "dobro", "fiddle", "banjo", "bluegrass",
    "honky-tonk", "honky tonk", "yodel", "twang",
)

# Families and requested styles where those sounds belong.
_COUNTRY_FAMILIES = frozenset({
    "country-americana", "contemporary-folk-acoustic", "traditional-vocal-stage",
    "soul-blues-gospel",
})
_COUNTRY_HINTS = frozenset({
    "bluegrass", "americana", "country", "rockabilly", "celtic", "irish",
    "folk", "indie folk", "folk pop", "singer-songwriter", "polka",
    "mariachi", "cumbia", "dangdut", "tamil", "arabian", "schlager",
    "gospel", "blues", "soul", "bossa nova", "swing", "lounge", "big band",
})


def style_excludes(family_id: str, style_hint: str | None = None) -> tuple[str, ...]:
    """Signature sounds this brief's caption and lyrics must not name.

    Pure function of (family, requested style). Country/folk families and
    country-adjacent requested styles are exempt; a stock roots song (no style
    hint) is exempt too, because polka and Celtic are legitimate there.
    """
    if family_id in _COUNTRY_FAMILIES or style_hint in _COUNTRY_HINTS:
        return ()
    if family_id == "roots-traditional-global" and style_hint is None:
        return ()
    return _COUNTRY_TERMS

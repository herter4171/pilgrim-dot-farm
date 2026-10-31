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
                ("Mandopop/C-pop/Cantopop/J-pop with electronic, R&B, dance, "
                 "or rock production",)),
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
    ("western swing", "country-americana"),
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
    "gospel", "blues", "soul", "western swing",
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


# Americana lyric idiom. The instrument ban above is not enough: a reggae brief
# whose lyrics say "county line", "pickup truck" and "tin roof" is a country
# song with "reggae" in the chorus, and the audio model sings it that way.
# Applied wherever `style_excludes` applies; matched with simple inflections.
_AMERICANA_IDIOM: tuple[str, ...] = (
    "county line", "dirt road", "gravel road", "back road", "backroad",
    "pickup truck", "tin roof", "front porch", "porch light", "porch swing",
    "holler", "y'all", "yall", "hoedown", "barn dance", "square dance",
    "two-step", "hayride", "moonshine", "cornbread", "rodeo", "cowboy",
)


def idiom_excludes(family_id: str, style_hint: str | None = None) -> tuple[str, ...]:
    """Americana lyric phrases this brief must not use (same exemptions as
    `style_excludes`)."""
    return _AMERICANA_IDIOM if style_excludes(family_id, style_hint) else ()


@dataclass(frozen=True)
class Sheet:
    """What a style sounds and talks like, sent with the pinned family so the
    caption describes the real idiom instead of the model's house style."""
    groove: str     # the defining rhythm, concretely
    core: str       # signature instruments
    scene: str      # where the music natively lives (Application Scenarios)
    exclude: str    # the nearest wrong style to block, as caption wording
    idiom: str      # how the lyrics should talk


_NO_COUNTRY = "no fiddle, no banjo, no pedal steel, no country twang"

SHEETS: dict[str, Sheet] = {
    "general-pop-ballad": Sheet(
        "steady 4/4 pop pulse, piano or programmed drums, half-time feel in "
        "verses opening to a full backbeat in the chorus",
        "piano, clean electric guitar, bass, polished drums, light pads",
        "a city bedroom at night, a car on a highway, a crowded pop arena",
        f"{_NO_COUNTRY}, no acoustic strumming lead",
        "direct, universal, emotional; contemporary conversational English"),
    "dance-pop-disco-funk": Sheet(
        "four-on-the-floor kick, open hi-hat on the offbeats, syncopated "
        "octave bass line, guitar chicken-scratch on the sixteenths",
        "slap or octave bass, funk rhythm guitar, string stabs, brass hits, "
        "clavinet",
        "a mirrorball dance floor, a roller rink, a late-night club",
        f"{_NO_COUNTRY}, no acoustic folk instruments",
        "dance-floor slang and call-and-response hooks ('get up', 'move')"),
    "club-edm-house-trance": Sheet(
        "four-on-the-floor kick, sidechained pads, build-up with snare roll "
        "and riser into a drop",
        "synth lead, supersaw pads, sub bass, drum machine, vocal chops",
        "a festival main stage, a warehouse rave, a club at 3 a.m.",
        f"{_NO_COUNTRY}, no live acoustic instruments",
        "short repeated hook phrases built for chanting over the drop"),
    "electronic-synth-ambient-pop": Sheet(
        "programmed drum machine beat, arpeggiated synth bass, gated snare",
        "analog synths, arpeggiators, drum machine, vocoder or airy vocal",
        "neon city streets, a night drive, a glowing screen in a dark room",
        f"{_NO_COUNTRY}, no acoustic instruments",
        "dreamy, nocturnal, technological imagery"),
    "modern-rnb-neo-soul": Sheet(
        "laid-back swung hip-hop drums behind the beat, deep sub bass, "
        "finger snaps",
        "Rhodes, warm sub bass, programmed drums, lush vocal stacks",
        "a dim lounge, a late-night apartment, a rooftop after midnight",
        f"{_NO_COUNTRY}, no strummed acoustic guitar",
        "intimate, smooth, sensual R&B phrasing with melisma"),
    "hip-hop-rap": Sheet(
        "boom-bap or trap drums: rolling hi-hats, hard snare on 3, 808 glides",
        "808 bass, drum machine, sampled loop or dark synth, ad-libs",
        "a city block, a car with the bass up, a studio booth",
        f"{_NO_COUNTRY}, no live drum kit",
        "rhythmic rap verses with internal rhyme and ad-libs; urban slang"),
    "soul-blues-gospel": Sheet(
        "12/8 shuffle or slow backbeat with heavy snare on 2 and 4",
        "Hammond organ, electric guitar, piano, horns, choir",
        "a juke joint, a Sunday church, a smoky soul revue",
        "no synth pads, no 808s",
        "testifying, call-and-response, soulful repetition"),
    "jazz-swing-big-band": Sheet(
        "swung eighths, ride cymbal pattern, walking upright bass, brushes "
        "or sticks",
        "upright bass, piano, ride cymbal, trumpet, saxophone, trombone",
        "a smoky jazz club, a ballroom, a hotel lounge",
        f"{_NO_COUNTRY}, no rock backbeat",
        "witty, urbane, playful wordplay; jazz-club cool, not rural drawl"),
    "traditional-vocal-stage": Sheet(
        "theatrical tempo changes, swing or ballad feel led by the vocal",
        "orchestra or piano, close-harmony vocals, brass",
        "a Broadway stage, a cabaret, a vintage variety show",
        f"{_NO_COUNTRY}, no modern electronic drums",
        "theatrical storytelling, crisp diction, punchlines"),
    "pop-alternative-rock": Sheet(
        "driving straight eighths, snare backbeat on 2 and 4, power chords",
        "distorted electric guitars, bass, live drums",
        "a sweaty club gig, a garage, a festival stage",
        f"{_NO_COUNTRY}, no synth-pop drum machine",
        "direct, energetic rock lyrics"),
    "metal-heavy-rock": Sheet(
        "palm-muted chugging riffs, double-kick drums, blast beats or "
        "half-time breakdowns",
        "high-gain guitars, bass, double-kick drums",
        "a mosh pit, a dark arena, a storm",
        f"{_NO_COUNTRY}, no clean pop production",
        "intense, dramatic imagery, shouted or belted hooks"),
    "contemporary-folk-acoustic": Sheet(
        "fingerpicked or strummed acoustic guitar, light percussion",
        "acoustic guitar, piano, soft percussion, harmony vocals",
        "a coffeehouse, a small bedroom recording, an indie festival tent",
        "no pedal steel, no country twang, no programmed beats",
        "personal, observational singer-songwriter lyrics"),
    "roots-traditional-global": Sheet(
        "choose ONE living tradition (reggae, Celtic, polka, cumbia, Chinese "
        "traditional, mariachi) and use that tradition's own rhythm",
        "the chosen tradition's own instruments",
        "the place where that tradition is played, not an American barn",
        "not American country or bluegrass: no pedal steel, no country twang",
        "the chosen tradition's own idiom"),
    "country-americana": Sheet(
        "boom-chick two-beat or train beat, snare on 2 and 4",
        "acoustic guitar, fiddle, banjo, pedal steel, upright bass",
        "a honky-tonk, a dirt road, a front porch",
        "no 808s, no synth pads",
        "plainspoken country storytelling"),
    "cinematic-pop-ballad": Sheet(
        "slow ballad pulse building to a full orchestral chorus",
        "piano, strings, timpani, soaring lead vocal",
        "a film's closing credits, a rain-streaked window, a grand finale",
        f"{_NO_COUNTRY}, no hard dance drop",
        "grand, emotional, universal imagery"),
    "cinematic-orchestral-epic": Sheet(
        "orchestral ostinato, taiko and timpani hits, building crescendos",
        "full orchestra, choir, brass, percussion",
        "a film trailer, a battlefield, a mountain summit",
        f"{_NO_COUNTRY}, no pop drum kit",
        "epic, mythic, choral phrases"),
    "east-asian-modern": Sheet(
        "polished pop production: electronic, R&B, dance or rock drums",
        "synths, programmed drums, guitar, layered vocals",
        "a Taipei or Tokyo night skyline, a music show stage",
        f"{_NO_COUNTRY}",
        "Mandopop/J-pop lyric idiom; English or Mandarin phrasing"),
    "east-asian-ballad-heritage": Sheet(
        "slow ballad pulse with traditional ornaments",
        "guzheng, erhu, dizi or piano with strings",
        "a moonlit pavilion, misty mountains, a lantern-lit courtyard",
        f"{_NO_COUNTRY}",
        "poetic, nature-rich East Asian ballad imagery"),
}

# Requested styles whose sound differs from their family's generic sheet.
STYLE_SHEETS: dict[str, Sheet] = {
    "reggae": Sheet(
        "one-drop: kick and snare together on beat 3, guitar skank on the "
        "offbeats, organ bubble on the 'and' of 2 and 4, heavy melodic bass",
        "bass guitar up front, clean skank guitar, Hammond organ bubble, "
        "rimshot drums, horns",
        "a Kingston yard, a beach sound system, a sunset dancehall",
        f"{_NO_COUNTRY}, no snare backbeat on 2 and 4, no acoustic strumming",
        "island patois-light phrasing, sunshine, unity, 'irie'; no rural "
        "Americana"),
    "celtic": Sheet(
        "jig in 6/8 or reel in fast 4/4, bodhran pulse",
        "tin whistle, fiddle, uilleann pipes, bodhran, bouzouki",
        "a crowded Irish pub session, green coastal hills",
        "no pedal steel, no country twang, no banjo rolls",
        "Irish/Scottish turns of phrase"),
    "polka": Sheet(
        "fast 2/4 oom-pah: tuba on the downbeat, accordion on the offbeat",
        "accordion, tuba, clarinet, snare",
        "a beer hall, a church festival dance floor",
        "no pedal steel, no country twang",
        "jolly sing-along, beer-hall humour"),
    "cumbia": Sheet(
        "cumbia rhythm: guacharaca scrape on every beat, bass on 1 and 3",
        "accordion, guacharaca, congas, bass",
        "a Colombian street party, a Mexican dance hall",
        _NO_COUNTRY,
        "Latin dance phrases, Spanish words welcome"),
    "mariachi": Sheet(
        "son or ranchera rhythm, guitarron bass, strummed vihuela",
        "trumpets, violins, vihuela, guitarron",
        "a Mexican plaza, a fiesta",
        "no pedal steel, no country twang",
        "Spanish exclamations ('ay ay ay'), heartfelt ranchera drama"),
    "bossa nova": Sheet(
        "bossa clave on nylon guitar, brushed snare cross-stick, soft bass "
        "on 1 and the 'and' of 2",
        "nylon-string guitar, brushes, upright bass, soft flute or piano",
        "an Ipanema beach bar at dusk, a quiet lounge",
        f"{_NO_COUNTRY}, no rock backbeat",
        "soft, breezy, whispered phrasing; saudade"),
}


def sheet(family_id: str, style_hint: str | None = None) -> Sheet | None:
    """The sound sheet for a requested style, falling back to its family."""
    if style_hint and style_hint in STYLE_SHEETS:
        return STYLE_SHEETS[style_hint]
    return SHEETS.get(family_id)


def sheet_text(family_id: str, style_hint: str | None = None) -> str:
    """The sound sheet as prompt text, or '' for an unknown family."""
    sh = sheet(family_id, style_hint)
    if sh is None:
        return ""
    name = style_hint or family_id
    return (f"Sound sheet for {name} (use it; the caption must sound like "
            f"this):\n"
            f"- Groove: {sh.groove}\n"
            f"- Core instruments: {sh.core}\n"
            f"- Native scene: {sh.scene}\n"
            f"- Exclusion to state in the caption: {sh.exclude}\n"
            f"- Lyric idiom: {sh.idiom}\n")


def clean_style(text: str) -> str | None:
    """Validate an LLM-inferred requested style: 1-4 plain words, lowercased.
    Returns None for empty; raises ValueError for anything else."""
    s = (text or "").strip().lower()
    if not s:
        return None
    if not re.fullmatch(r"[a-z0-9][a-z0-9&' -]{0,39}", s) or len(s.split()) > 4:
        raise ValueError(
            f"requested_style must be 1-4 plain words or empty (got {s[:40]!r})")
    return s

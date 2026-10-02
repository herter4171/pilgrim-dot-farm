# Prompting Stable Audio 3 Small SFX

Treat `stable-audio-3-small-sfx` as a sound-effect generator, not a general
language model and not an image generator.

## Prompt structure

Describe, in roughly this order:

1. **Sound source**
   - What physically makes the sound?
   - Examples: wooden door, gravel footsteps, electric motor, thunder,
     pneumatic valve, glass bottle, sci-fi energy weapon.

2. **Action**
   - What is the source doing?
   - Use concrete verbs: slams, scrapes, rattles, accelerates, impacts,
     hisses, cracks, powers down, rolls away.

3. **Temporal behavior**
   - Describe how the sound unfolds when useful.
   - Examples:
     - sharp attack and fast decay
     - slow mechanical startup followed by steady hum
     - three heavy impacts
     - rising whine followed by an abrupt stop
     - distant approach, pass-by, then fade into the distance

4. **Acoustic character**
   - Describe qualities that can actually be heard.
   - Examples:
     - heavy low-frequency body
     - metallic resonance
     - dry and crisp
     - muffled
     - distorted
     - deep sub-bass
     - bright transient
     - long reverberant tail

5. **Recording/environment**
   - Add this when it matters.
   - Examples:
     - close-miked
     - recorded outdoors
     - large concrete warehouse
     - small dead room
     - distant microphone perspective
     - cavernous reverb
     - vintage microphone character

A useful generic template is:

    TrackType: SFX, [source] [action], [temporal behavior],
    [acoustic character], [recording/environment].

`TrackType: SFX` is optional, but use it by default because it helps explicitly
tell the model that the requested output is a sound effect.

## Examples

    TrackType: SFX, heavy wooden warehouse door slamming shut,
    hard initial impact with a short rattling decay, deep low-mid body,
    close-miked in a large concrete room.

    TrackType: SFX, footsteps walking slowly across loose gravel,
    individual crunchy footfalls with small stones shifting after each step,
    natural outdoor recording, close microphone perspective.

    TrackType: SFX, large industrial electric motor powering on,
    initial relay click followed by a rising mechanical whine that settles
    into a deep steady hum, subtle metallic vibration, recorded indoors.

    TrackType: SFX, futuristic plasma weapon firing once,
    rapid electrical charge-up followed by a sharp energetic blast,
    deep bass impact, bright electrical crackle and a short synthetic echo.

    TrackType: SFX, ceramic coffee mug dropped onto a tile floor,
    single hard impact followed by several small broken pieces bouncing
    and settling, dry close-miked recording.

## Prefer literal audible descriptions

Describe what should be HEARD.

Good:

    heavy steel hatch slams shut with a resonant metallic boom

Weak:

    terrifying hatch closing on an abandoned spaceship

The second prompt mostly describes visual/story information. Translate the
idea into audible properties instead:

    TrackType: SFX, massive steel hatch slamming shut,
    heavy metallic impact followed by a deep resonant ring,
    mechanical latch clunks into place, large reverberant interior.

## Think like a sound-effects library

The model was trained substantially from AudioSparx and Freesound audio and
metadata. Prompts resembling useful descriptions of recorded sounds are
therefore a good default.

Prefer:

    diesel truck engine idling unevenly, close exterior recording,
    deep mechanical rumble and occasional metallic vibration

over:

    I need the sound of an old truck that a character has just discovered
    after surviving for twenty years in a post-apocalyptic wasteland.

Convert narrative requirements into acoustic requirements.

## Duration

Choose duration independently from the text prompt and make it appropriate
for the sound.

Examples:

- gunshot / impact / UI sound: ~1-3 seconds
- door, mechanical action, short sequence: ~2-6 seconds
- engine start or pass-by: ~5-10 seconds
- ambience: longer as necessary

Do not request ten seconds for a naturally half-second transient unless the
desired result actually includes a tail, aftermath, repetition, or ambience.

The model's generation API takes duration separately, e.g.:

    prompt="chugging train coming into station with horn"
    duration=7

## Complexity

Prefer one coherent acoustic event per generation.

If several events belong naturally together, describe their order explicitly:

    car approaches rapidly on wet pavement, passes close to the microphone,
    tire spray and engine roar peak during the pass, then fade into the distance

Avoid stuffing unrelated events into one prompt. Generate separate effects
and combine them later when precise timing or layering matters.

## Prompt refinement

If the result is wrong, do not merely make the prompt longer.

Identify what is wrong and strengthen the corresponding audible property.

Too soft:
    "hard violent impact, strong low-frequency transient"

Too reverberant:
    "dry close-miked recording in a dead room"

Too synthetic:
    "natural field recording, realistic acoustic sound"

Missing sequence:
    explicitly state the order:
    "short hiss, followed by a metallic click, then a heavy mechanical clunk"

Too busy:
    remove secondary sounds and request only the primary event.

## Rule of thumb

Build prompts as:

    WHAT MAKES THE SOUND
    + WHAT IT DOES
    + HOW IT CHANGES OVER TIME
    + WHAT IT SOUNDS LIKE
    + HOW/WHERE IT IS RECORDED

Be concrete, acoustic, and concise. Add detail when the detail corresponds
to something that could actually be heard.

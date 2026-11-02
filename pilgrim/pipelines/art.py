"""FLUX 3 Image Klein client (ALBUM_ART.md Step 2; docs/backends.md §10).

The server is the M5's own FLUX server on `hosts.flux` (loopback). Two facts
shape this client:

* **Always `response_format: "b64_json"`.** The `url` form points at
  `/download/{nbytes}`, which serves whatever render finished *last* — any
  other caller between our render and our download would hand us their image.
* **The server is single-flight and blocks.** Generation runs synchronously on
  its event loop, so `/health` can take ~40 s during a render. A slow health
  check is not "down", and a 503 `memory_pressure` is a normal back-off.

Nothing here ever touches playout (hard rule 1): only `album_art.py` (its own
background task) and `tools/art_backfill.py` call this.
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
import struct
import time
import unicodedata
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from pilgrim.config import Config

log = logging.getLogger("radio.art")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# The 503 body the server's RuntimeError handler returns (docs/backends.md §10).
MEMORY_PRESSURE_TYPE = "memory_pressure"


class ArtError(Exception):
    """Base for every art failure. Callers degrade (skip the song, retry later)."""


class FluxBusy(ArtError):
    """503 `memory_pressure`: the server refused the render. Back off, retry."""


class FluxTimeout(ArtError):
    """The generation or health call timed out."""


class FluxUnavailable(ArtError):
    """Connection refused/reset: the server is not up."""


class FluxMalformed(ArtError):
    """A 200 whose body is not the documented shape, or not the PNG we asked for."""


# --------------------------------------------------------------------------- #
# Response models (docs/backends.md §10: the PNG is double-nested)
# --------------------------------------------------------------------------- #
class FluxImage(BaseModel):
    b64_json: str
    mime_type: str | None = None


class FluxItem(BaseModel):
    created: int | None = None
    data: list[FluxImage]
    model: str | None = None
    bfx_revised_prompt: str | None = None
    bfx_record: dict[str, Any] | None = None


class FluxResponse(BaseModel):
    object: str | None = None
    data: list[FluxItem] = Field(default_factory=list)


def parse_size(size: str) -> tuple[int, int]:
    """`"1024x1024"` -> `(1024, 1024)`."""
    try:
        w, h = size.lower().split("x", 1)
        return int(w), int(h)
    except (ValueError, AttributeError) as e:
        raise ValueError(f"bad art.size {size!r} (want WxH)") from e


def png_dimensions(data: bytes) -> tuple[int, int]:
    """Read the IHDR width/height without Pillow. Raises FluxMalformed."""
    if not data.startswith(PNG_SIGNATURE):
        raise FluxMalformed("not a PNG (bad signature)")
    if len(data) < 24 or data[12:16] != b"IHDR":
        raise FluxMalformed("PNG has no IHDR chunk")
    w, h = struct.unpack(">II", data[16:24])
    return int(w), int(h)


def validate_png(data: bytes, expected: tuple[int, int] | None = None) -> tuple[int, int]:
    """Signature + IHDR, and the dimensions we asked for (never trust a size)."""
    dims = png_dimensions(data)
    if expected is not None and dims != expected:
        raise FluxMalformed(f"PNG is {dims[0]}x{dims[1]}, wanted {expected[0]}x{expected[1]}")
    return dims


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class FluxClient:
    """Two calls: `health()` and `generate(prompt, seed) -> PNG bytes`."""

    def __init__(self, cfg: Config, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self._base = cfg.hosts.flux.rstrip("/")
        self._size = parse_size(cfg.art.size)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.art.timeout_s, connect=10.0),
            transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def health(self) -> bool:
        """Cached-by-the-caller probe. A slow answer is still an answer: the
        server blocks for the whole render, so the timeout is generous."""
        try:
            r = await self._client.get(self._base + "/health",
                                       timeout=httpx.Timeout(self.cfg.art.health_timeout_s,
                                                             connect=10.0))
        except Exception as e:  # noqa: BLE001 - any failure means "not usable now"
            log.info("art.health", extra={"ok": False, "error": str(e)[:200]})
            return False
        if r.status_code != 200:
            log.info("art.health", extra={"ok": False, "status": r.status_code})
            return False
        try:
            body = r.json()
        except ValueError:
            return False
        ok = bool(body.get("status") == "ok")
        log.info("art.health", extra={"ok": ok, "model": body.get("model"),
                                      "rss_mb": body.get("rss_mb")})
        return ok

    async def generate(self, prompt: str, seed: int) -> bytes:
        """One 1024x1024 render. Returns validated PNG bytes or raises ArtError.

        The caller guarantees one in-flight request station-wide
        (ALBUM_ART.md §0.2 rule 2); this client adds no retries of its own so a
        503 back-off stays the caller's decision.
        """
        payload = {
            "prompt": prompt,
            "size": f"{self._size[0]}x{self._size[1]}",
            "n": 1,
            "steps": self.cfg.art.steps,
            "seed": int(seed),
            "response_format": "b64_json",   # never `url` (§0.2 rule 1)
        }
        try:
            r = await self._client.post(self._base + "/v1/images/generations",
                                        json=payload)
        except httpx.TimeoutException as e:
            raise FluxTimeout(f"flux render timed out after {self.cfg.art.timeout_s}s") from e
        except httpx.HTTPError as e:
            raise FluxUnavailable(f"flux unreachable: {type(e).__name__}: {e}") from e

        if r.status_code == 503:
            raise FluxBusy(_busy_detail(r))
        if r.status_code != 200:
            raise FluxUnavailable(
                f"flux http {r.status_code}: {r.text[:200]!r}")

        try:
            body = FluxResponse.model_validate(r.json())
        except (ValueError, ValidationError) as e:
            raise FluxMalformed(f"flux response shape: {e}") from e
        if not body.data:
            raise FluxMalformed("flux response has no items")
        item = body.data[0]
        if not item.data:
            raise FluxMalformed("flux item has no images")
        try:
            png = base64.b64decode(item.data[0].b64_json, validate=True)
        except (binascii.Error, ValueError) as e:
            raise FluxMalformed(f"flux b64_json decode: {e}") from e
        validate_png(png, self._size)
        return png


def _busy_detail(r: httpx.Response) -> str:
    """Keep the server's reason (useful in logs) without trusting the body."""
    try:
        body = r.json()
    except ValueError:
        return r.text[:200]
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("type") or "")[:200]
    return r.text[:200]


# --------------------------------------------------------------------------- #
# Scene writing + the fixed FLUX prompt (ALBUM_ART.md §0.3, Step 3)
# --------------------------------------------------------------------------- #
# These words make FLUX draw a vinyl disc instead of square artwork (§0.3).
BANNED_WORDS = ("album", "cover", "record", "vinyl", "lp", "disc", "cd",
                "single", "sleeve", "cassette")
_BANNED_RE = re.compile(
    r"\b(?:" + "|".join(BANNED_WORDS) + r")\b", re.I)

TITLE_CAP = 70          # art text only; the DB title is never changed
ARTIST_CAP = 48
STYLE_HINT_CAP = 300
LYRIC_LINES = 12
MIN_SCENE_WORDS = 20    # asked for 30-70, accepted a little wider
MAX_SCENE_WORDS = 80
NO_TEXT_SENTENCE = "No text, no letters, no typography."

_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\uFE0F\u200D\u20E3]+")
_TRAILING_PAREN = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")
_SECTION_TAG = re.compile(r"^\s*\[[^\]]*\]\s*")


class SceneError(ArtError):
    """No usable scene after `art.scene_attempts` (skip the song, retry later)."""


def banned_hits(text: str) -> list[str]:
    """Whole-word, case-insensitive banned-word matches (§0.3)."""
    return sorted({m.group(0).lower() for m in _BANNED_RE.finditer(text)})


def has_non_latin_script(text: str) -> bool:
    """True when a letter is not Latin script (Japanese, Tamil, Cyrillic, …).
    Accented Latin (Spanish, French) is fine: those render correctly."""
    for ch in text:
        if not ch.isalpha():
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            return True     # unnamed letter: assume the model can't render it
        if not name.startswith("LATIN"):
            return True
    return False


def sanitize_text(text: str, cap: int) -> str:
    """Art-text sanitiser (§0.3): strip emoji/control chars, collapse
    whitespace, straighten quotes, drop a trailing parenthetical, cap at a word
    boundary. Never touches the stored title/artist."""
    t = unicodedata.normalize("NFKC", text or "")
    t = "".join(ch for ch in t if unicodedata.category(ch)[0] != "C")
    t = _EMOJI.sub("", t)
    for q in ('"', "\u201c", "\u201d", "\u201e"):
        t = t.replace(q, "'")
    t = re.sub(r"\s+", " ", t).strip()
    stripped = _TRAILING_PAREN.sub("", t).strip()
    if stripped:
        t = stripped
    if len(t) > cap:
        cut = t[:cap]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        t = cut.rstrip(" ,;:-–—'")
    return t.strip()


def typography_sentence(title: str, artist: str) -> str:
    """The two `reads exactly` sentences, or the text-free replacement for a
    non-Latin title/artist (§0.3, answer 5)."""
    if has_non_latin_script(title + artist):
        return NO_TEXT_SENTENCE
    return (f'Large clear typography at the top reads exactly "{title}". '
            f'Smaller text at the bottom reads exactly "{artist}".')


def build_flux_prompt(template: str, scene: str, title: str, artist: str) -> str:
    """Fixed code-owned template + the LLM scene + code-inserted names."""
    return template.format(scene=re.sub(r"\s+", " ", scene).strip(),
                           typography=typography_sentence(title, artist))


def validate_scene(obj: dict[str, Any]) -> str:
    """Untrusted LLM output -> a scene, or SceneError (AGENTS §5)."""
    scene = obj.get("scene")
    if not isinstance(scene, str) or not scene.strip():
        raise SceneError("scene missing or empty")
    scene = re.sub(r"\s+", " ", scene).strip()
    words = len(scene.split())
    if words < MIN_SCENE_WORDS or words > MAX_SCENE_WORDS:
        raise SceneError(f"scene is {words} words (want {MIN_SCENE_WORDS}-{MAX_SCENE_WORDS})")
    hits = banned_hits(scene)
    if hits:
        raise SceneError(f"banned words in scene: {', '.join(hits)}")
    return scene


def style_hint(meta: dict[str, Any] | None) -> str:
    """The brief's scene-bearing lines when present (5 of 413 songs), else the
    one-line caption (ALBUM_ART Step 3, answer 3)."""
    meta = meta or {}
    raw_brief = meta.get("brief")
    brief: dict[str, Any] = raw_brief if isinstance(raw_brief, dict) else {}
    sp = str(brief.get("style_prompt") or meta.get("style_prompt") or "").strip()
    if not sp:
        return ""
    lines = [ln.strip() for ln in sp.splitlines()]
    picked = [ln for ln in lines
              if ln.startswith("Application Scenarios")
              or ln.startswith("Global Emotional Progression")]
    if picked:
        return " ".join(picked)[:600]
    return sp[:STYLE_HINT_CAP]


def lyric_excerpt(meta: dict[str, Any] | None, lines: int = LYRIC_LINES) -> str:
    """First ~12 non-empty lyric lines with `[section]` tags stripped."""
    raw = str((meta or {}).get("lyrics") or "")
    out = []
    for line in raw.splitlines():
        line = _SECTION_TAG.sub("", line).strip()
        if line:
            out.append(line)
        if len(out) >= lines:
            break
    return "\n".join(out)


class SceneWriter:
    """Asks `models.art_scene` for a scene, retrying untrusted output."""

    def __init__(self, cfg: Config, llm: Any, prompt: str) -> None:
        self.cfg = cfg
        self.llm = llm
        self.prompt = prompt

    async def write(self, *, title: str, artist: str, genre: str | None,
                    hint: str, lyrics: str, item_id: int | None = None) -> str:
        user = (f"Title: {title}\nArtist: {artist}\n"
                f"Genre: {genre or 'unknown'}\nStyle hint: {hint or 'none'}\n")
        if lyrics:
            user += f"Lyrics excerpt:\n{lyrics}\n"
        last: Exception | None = None
        for attempt in range(1, max(1, self.cfg.art.scene_attempts) + 1):
            t0 = time.monotonic()
            try:
                obj = await self.llm.chat_json(self.cfg.models.art_scene,
                                               self.prompt, user)
                scene = validate_scene(obj)
                log.info("art.scene", extra={
                    "item_id": item_id, "stage": "scene", "attempt": attempt,
                    "duration_ms": round((time.monotonic() - t0) * 1000)})
                return scene
            except Exception as e:  # noqa: BLE001 - untrusted output, retry
                last = e
                log.warning("art.scene_rejected", extra={
                    "item_id": item_id, "stage": "scene", "attempt": attempt,
                    "error": f"{type(e).__name__}: {e}"[:300],
                    "duration_ms": round((time.monotonic() - t0) * 1000)})
        raise SceneError(f"no valid scene after {self.cfg.art.scene_attempts} "
                         f"attempts: {last}")

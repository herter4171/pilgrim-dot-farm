"""Payer-supplied MiniMax input, used as written (PAYMENT.md Step 12c).

The closer a submission already is to MiniMax Music 3 input, the less the
station should touch it. The brief LLM reads the request in whatever English
it is written and decides whether the listener wants their caption and/or
lyrics used exactly (`caption_verbatim`, `lyrics_verbatim`); this module then
copies those blocks from the submission itself, byte for byte, because a
model retyping them can drift.

The only edits are to section tags, so MiniMax does not drop lines: a tag
sharing a line with text is split onto its own line (the model drops text on
a tag's line), tags are lowercased, and common synonyms map to the allowed
set. Words are never changed.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("radio.verbatim")

HEADINGS = ("Global Metadata", "Vocal Details", "Arrangement")
ALLOWED_TAGS = ("intro", "verse", "pre-chorus", "chorus", "post-chorus",
                "bridge", "instrumental", "solo", "outro")
_SYNONYMS = {
    "hook": "chorus", "refrain": "chorus", "final chorus": "chorus",
    "pre chorus": "pre-chorus", "prechorus": "pre-chorus",
    "post chorus": "post-chorus", "postchorus": "post-chorus",
    "interlude": "instrumental", "break": "instrumental",
    "instrumental break": "instrumental", "guitar solo": "solo",
    "end": "outro", "ending": "outro", "coda": "outro",
}
_TAG_LINE = re.compile(r"^\s*\[([^\]\n]{1,40})\]\s*(.*)$")


def _canon_tag(raw: str) -> str | None:
    name = re.sub(r"\s*\d+\s*$", "", raw.strip().lower())   # "Verse 2" -> verse
    name = _SYNONYMS.get(name, name)
    return name if name in ALLOWED_TAGS else None


def extract_caption(text: str) -> str | None:
    """The structured caption block: from `Global Metadata` through the
    Arrangement section, stopping at the first section tag after it (lyrics
    pasted below the caption). None unless all three headings appear in order."""
    start = text.find(HEADINGS[0])
    if start < 0:
        return None
    pos = [text.find(h, start) for h in HEADINGS]
    if any(p < 0 for p in pos) or pos != sorted(pos):
        return None
    lines = text[start:].split("\n")
    out: list[str] = []
    seen_arrangement = False
    for ln in lines:
        if ln.strip().startswith(HEADINGS[2]):
            seen_arrangement = True
        elif seen_arrangement and _TAG_LINE.match(ln):
            break
        out.append(ln.rstrip())
    block = "\n".join(out).strip()
    return block or None


def extract_lyrics(text: str) -> str | None:
    """Tagged lyrics: from the first section tag to the end, with any caption
    block removed. Tags are fixed (own line, lowercase, allowed set); an
    unknown tag line is dropped and its lyric lines kept. None without tags."""
    cap = extract_caption(text)
    body = text.replace(cap, "\n") if cap else text
    lines = body.split("\n")
    first = next((i for i, ln in enumerate(lines) if _TAG_LINE.match(ln)), None)
    if first is None:
        return None
    out: list[str] = []
    for ln in lines[first:]:
        m = _TAG_LINE.match(ln)
        if not m:
            out.append(ln.rstrip())
            continue
        tag, rest = _canon_tag(m.group(1)), m.group(2).strip()
        if tag is None:
            log.info("verbatim.tag_dropped", extra={"tag": m.group(1)[:40]})
        else:
            out.append(f"[{tag}]")
        if rest:
            out.append(rest)
    block = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    if not re.search(r"^\[(verse|chorus|bridge|solo|instrumental)\]$", block, re.M):
        return None
    return block


def genre_line(text: str) -> str | None:
    """The caption's Basic Attributes line, used for style routing so words
    elsewhere in a long caption (e.g. an exclusion "no country twang") cannot
    pin the wrong family."""
    cap = extract_caption(text)
    if not cap:
        return None
    m = re.search(r"Basic Attributes:\s*(.+)", cap)
    return m.group(1) if m else None

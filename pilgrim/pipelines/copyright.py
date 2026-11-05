"""Copied-lyrics screen for paid requests (PAYMENT.md Step 12b).

A paid prompt can carry many lines of lyrics. Before a Checkout Session is
created, a few of its most distinctive lines are searched as exact phrases
through Brave web search (BRAVE.md), and the result snippets are compared
word by word. The
decision is deterministic text matching; no LLM sees the snippets (web text
can carry prompt injection) and none makes the call.

Decision (operator policy, Oct 31):

* two or more probed lines hit the same song                 -> match (reject)
* one line hits and the snippet's *next* words also appear
  in the submission (a copied passage, not a quoted line)    -> match (reject)
* one isolated famous line                                   -> clear (allowed)
* search degraded for every probe                            -> unchecked
  (the request proceeds; logged as payment.copyright_unchecked)

"No match found" is what this establishes, never "copyright-free".
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pilgrim.config import Config
from pilgrim.pipelines.brave import BraveSearch, SearchOutcome
from pilgrim.pipelines.verbatim import extract_caption

log = logging.getLogger("radio.copyright")

Search = Callable[[str], Awaitable[SearchOutcome]]

_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "had", "has", "have", "he", "her", "his", "i", "im", "in", "is", "it",
    "its", "just", "me", "my", "no", "not", "of", "oh", "on", "or", "our",
    "she", "so", "that", "the", "their", "them", "then", "there", "they",
    "this", "to", "up", "us", "was", "we", "were", "what", "when", "where",
    "who", "will", "with", "you", "your", "yeah", "ooh", "baby",
})

# Result-title parts that name the site, not the song.
_SITES = frozenset({
    "youtube", "genius", "musixmatch", "spotify", "azlyrics", "azlyricscom",
    "lyricscom", "songfacts", "apple music", "shazam", "lyricfind", "letras",
    "songlyrics", "metrolyrics", "lyricstranslate", "deezer", "amazon music",
    "pandora", "soundcloud", "tiktok", "facebook", "instagram", "reddit",
})
_TITLE_FILLER = frozenset({
    "lyrics", "lyric", "song", "songs", "and", "by", "official", "video",
    "audio", "meaning", "live", "the", "a", "with", "hd", "remastered",
})
_SECTION_TAG = re.compile(r"^\[[^\]]{1,40}\]$")
_QUOTED = re.compile(r"[\"“]([^\"“”]{12,200})[\"”]")


def tokens(text: str) -> list[str]:
    """Lowercased words with apostrophes dropped and -ing folded to -in, so
    "livin'" == "living" and "hidin'" == "hiding"."""
    low = text.lower().replace("’", "'").replace("‘", "'")
    out: list[str] = []
    for w in re.findall(r"[a-z0-9']+", low):
        w = w.replace("'", "")
        if not w:
            continue
        if len(w) > 4 and w.endswith("ing"):
            w = w[:-1]
        out.append(w)
    return out


def lyric_lines(text: str) -> list[str]:
    """Candidate lyric lines: lines under section tags when the prompt has
    tags (the direction prose above them is skipped), else every line; plus
    any long quoted passage in the prose. A pasted MiniMax caption is removed
    first: its long sentences would otherwise crowd real lyrics out of the
    probe slots."""
    cap = extract_caption(text)
    if cap:
        text = text.replace(cap, "\n")
    lines = [ln.strip() for ln in text.split("\n")]
    tagged = any(_SECTION_TAG.match(ln) for ln in lines)
    out: list[str] = []
    inside = not tagged
    for ln in lines:
        if not ln:
            continue
        if _SECTION_TAG.match(ln):
            inside = True
            continue
        if inside:
            out.append(ln.strip("\"“”'"))
        else:
            out.extend(m.group(1) for m in _QUOTED.finditer(ln))
    return out


def choose_probes(text: str, min_words: int, min_content: int,
                  max_probes: int) -> list[str]:
    """The most distinctive lyric lines: enough words, enough non-stopwords,
    most content words first. Common short lines are never searched."""
    seen: set[tuple[str, ...]] = set()
    scored: list[tuple[int, int, str]] = []
    for line in lyric_lines(text):
        toks = tokens(line)
        content = sum(1 for t in toks if t not in _STOPWORDS)
        key = tuple(toks)
        if len(toks) < min_words or content < min_content or key in seen:
            continue
        seen.add(key)
        scored.append((content, len(toks), line))
    scored.sort(key=lambda s: (-s[0], -s[1]))
    return [line for _, _, line in scored[:max_probes]]


def probe_query(line: str, max_words: int = 12) -> str:
    words = line.replace('"', " ").replace("“", " ").replace("”", " ").split()
    return '"' + " ".join(words[:max_words]) + '" lyrics'


def _longest_run(a: list[str], b: list[str]) -> tuple[int, int]:
    """(length, end index in b) of the longest common contiguous run."""
    best, end = 0, 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            if x == y:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best, end = cur[j], j
        prev = cur
    return best, end


def _contains(hay: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return n > 0 and any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


def song_key(title: str) -> frozenset[str]:
    """Words naming the song/artist in a result title (site names dropped)."""
    parts = re.split(r"\s[-–|:]\s", re.sub(r"[(\[][^)\]]*[)\]]", " ", title))
    words: set[str] = set()
    for part in parts:
        flat = re.sub(r"[^a-z0-9 ]", "", part.lower()).strip()
        if not flat or flat in _SITES or flat.replace(" ", "") in _SITES:
            continue
        words.update(t for t in tokens(part) if t not in _TITLE_FILLER)
    return frozenset(words)


def song_label(title: str) -> str:
    parts = re.split(r"\s[-–|]\s", re.sub(r"[(\[][^)\]]*[)\]]", " ", title))
    keep = [p.strip() for p in parts
            if p.strip() and re.sub(r"[^a-z0-9]", "", p.lower()) not in _SITES]
    label = " - ".join(keep) or title
    label = re.sub(r"\b(song and )?lyrics( by)?\b", "", label, flags=re.I)
    return " ".join(label.split()).strip(" -–")[:80]


def _same_song(a: frozenset[str], b: frozenset[str]) -> bool:
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= 0.5


@dataclass
class Hit:
    probe: int
    key: frozenset[str]
    label: str
    strong: bool          # the snippet continues with more of the submission


@dataclass
class Verdict:
    outcome: str          # clear | match | unchecked | skipped
    reason: str = ""
    label: str = ""
    probes: int = 0
    degraded: int = 0
    hits: list[Hit] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.outcome != "match"


def judge(text: str, probes: list[str], outcomes: list[SearchOutcome],
          ngram: int) -> Verdict:
    """Pure decision from probes and their search outcomes (policy above)."""
    if not probes:
        return Verdict("skipped")
    degraded = sum(1 for o in outcomes if o.status == "degraded")
    if degraded == len(probes):
        return Verdict("unchecked", probes=len(probes), degraded=degraded)
    sub = tokens(text)
    hits: list[Hit] = []
    for i, (line, outcome) in enumerate(zip(probes, outcomes, strict=True)):
        lt = tokens(line)
        need = min(ngram, len(lt))
        for res in outcome.results:
            desc = tokens(res.get("description", ""))
            run, end = _longest_run(lt, desc)
            if run < need:
                continue
            nxt = desc[end:end + 5]
            strong = (len(nxt) >= 4 and not _contains(lt, nxt)
                      and _contains(sub, nxt))
            title = res.get("title", "")
            hits.append(Hit(i, song_key(title), song_label(title), strong))
    v = Verdict("clear", probes=len(probes), degraded=degraded, hits=hits)
    passage = next((h for h in hits if h.strong), None)
    if passage is not None:
        v.outcome, v.label = "match", passage.label
    else:
        for a in hits:
            if any(b.probe != a.probe and _same_song(a.key, b.key) for b in hits):
                v.outcome, v.label = "match", a.label
                break
    if v.outcome == "match":
        v.reason = (f"Parts of your lyrics match an existing song ({v.label}). "
                    "Rewrite them in your own words and try again. You were "
                    "not charged.")
    return v


class CopyrightScreen:
    """Bounded, parallel probe searches + `judge`. `search` is injectable so
    tests never reach the network (AGENTS §1.2)."""

    def __init__(self, cfg: Config, client: BraveSearch | None = None,
                 search: Search | None = None):
        self.cfg = cfg
        # No client = no key = degraded without the network (tests).
        self.client = client or BraveSearch(cfg, "")
        self.search: Search = search or self._brave

    async def _brave(self, query: str) -> SearchOutcome:
        return await self.client.web(query, count=self.cfg.payments.copyright.results)

    async def check(self, text: str, request_id: int | None = None) -> Verdict:
        cc = self.cfg.payments.copyright
        if not cc.enabled:
            return Verdict("skipped")
        t0 = time.monotonic()
        probes = choose_probes(text, cc.min_words, cc.min_content_words,
                               cc.max_probes)
        outcomes: list[SearchOutcome] = []
        if probes:
            try:
                outcomes = list(await asyncio.wait_for(
                    asyncio.gather(*(self.search(probe_query(p)) for p in probes)),
                    timeout=cc.timeout_s))
            except TimeoutError:
                outcomes = [SearchOutcome("degraded", error="timeout")] * len(probes)
        verdict = judge(text, probes, outcomes, cc.ngram)
        extra = {"request_id": request_id, "outcome": verdict.outcome,
                 "probes": verdict.probes, "hits": len(verdict.hits),
                 "degraded": verdict.degraded, "label": verdict.label,
                 "duration_ms": round((time.monotonic() - t0) * 1000, 1)}
        if verdict.outcome == "unchecked":
            errors = sorted({o.error for o in outcomes if o.error})
            log.warning("payment.copyright_unchecked",
                        extra={**extra, "errors": errors[:3]})
        else:
            log.info("payment.copyright", extra=extra)
        return verdict

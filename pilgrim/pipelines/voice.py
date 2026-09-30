"""Voice pipeline (RADIO.md §6.3, §5.3): copy -> TTS cleanup -> Kokoro -> QC -> normalize."""
from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import cast

import httpx
import numpy as np
import soundfile as sf

from pilgrim.audio import normalize
from pilgrim.audio.qc import grade_audio
from pilgrim.config import Config
from pilgrim.pipelines.llm import LLM, LLMError

log = logging.getLogger("radio.voice")

# Kokoro reads EVERYTHING literally: markdown, emoji, stage directions, URLs.
_MD = re.compile(r"[*_`#~>{}\[\]()]")
_EMOJI = re.compile(
    "[" "\U0001F600-\U0001F64F" "\U0001F300-\U0001F5FF" "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF" "\U00002702-\U000027B0" "\U000024C2-\U0001F251"
    "\U0001f900-\U0001f9ff" "\u2600-\u27BF" "\u2B00-\u2BFF" "]+")
_STAGE = re.compile(r"\([^)]*\)", re.IGNORECASE)
_URL = re.compile(r"https?://\S+|www\.\S+")
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def tts_cleanup(text: str) -> str:
    """Make copy TTS-safe (AGENTS §5, RADIO §6.3). Raises if it contains a URL."""
    if _URL.search(text):
        raise ValueError("copy contains URL; refusing to read it aloud")
    out = text
    out = _EMOJI.sub("", out)
    out = _MD.sub("", out)
    out = _STAGE.sub("", out)
    out = re.sub(r"\s+", " ", out)
    out = _TIME.sub(lambda m: _spoken_time(int(m.group(1)), int(m.group(2))), out)
    # basic number words for standalone small integers
    out = _numbers(out)
    return out.strip()


def _spoken_time(h: int, m: int) -> str:
    if m == 0:
        return f"{h} o'clock" if h <= 12 else "the hour"
    return f"{h} {m:02d}"


_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
          "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
          "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
          "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _numbers(text: str) -> str:
    def repl(m):
        n = m.group(0)
        if len(n) > 2:
            return n  # leave big numbers alone
        v = int(n)
        if 0 <= v < 20:
            return _WORDS[v]
        if 20 <= v < 100:
            t, o = divmod(v, 10)
            if o:
                return _WORDS[18 + t - 2] + " " + _WORDS[o]
            return _WORDS[18 + t - 2]
        return n
    return re.sub(r"(?<!\d)\d{1,2}(?!\d)", repl, text)


class KokoroClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))

    async def close(self) -> None:
        await self._client.aclose()

    async def voices(self) -> list:
        r = await self._client.get(self.cfg.hosts.kokoro.rstrip("/") + "/voices")
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _chunk_text(text: str, max_chars: int = 200) -> list[str]:
        """Split on sentence boundaries into chunks of <= max_chars (OVERHAUL
        2.6): Kokoro truncates long text (probe: 120 words @ 3.51 w/s vs a
        natural ~2.6-3.2), so long copy is synthesized per-chunk and joined."""
        sentences = re.split(r"(?<=[.!?])\s+", text)
        chunks: list[str] = []
        cur = ""
        for s in sentences:
            if not s:
                continue
            if len(s) > max_chars:
                # hard-split an over-long sentence on word boundaries
                piece, plen = "", 0
                for w in s.split():
                    nlen = len(w) if not piece else plen + 1 + len(w)
                    if piece and nlen > max_chars:
                        chunks.append(piece)
                        piece, plen = w, len(w)
                    else:
                        piece = w if not piece else f"{piece} {w}"
                        plen = len(piece)
                if piece:
                    chunks.append(piece)
                continue
            if cur and len(cur) + 1 + len(s) > max_chars:
                chunks.append(cur)
                cur = s
            else:
                cur = s if not cur else f"{cur} {s}"
        if cur:
            chunks.append(cur)
        return chunks

    async def synth(self, text: str, voice: str, speed: float = 1.0) -> bytes:
        """Synthesize text to a WAV. Long text is chunked by sentence (<=200
        chars) and the per-chunk WAVs are concatenated so nothing is truncated."""
        chunks = self._chunk_text(text)
        wavs: list[bytes] = []
        for chunk in chunks:
            r = await self._client.get(
                self.cfg.hosts.kokoro.rstrip("/") + "/tts",
                params={"text": chunk, "voice": voice, "speed": str(speed),
                        "format": "wav"})
            r.raise_for_status()
            wavs.append(r.content)
        if len(wavs) == 1:
            return wavs[0]
        # Trim each chunk's silent edges and join with one fixed pause, so a
        # sentence boundary never stacks Kokoro's lead-in + tail silence into
        # something QC reads as a dropout.
        pieces: list[np.ndarray] = []
        sr = 0
        floor = 10 ** (self.cfg.audio.silence_db / 20.0)
        for w in wavs:
            d, s = sf.read(io.BytesIO(w))
            x = np.asarray(d, dtype=np.float32)
            sr = int(s)
            loud = np.flatnonzero(np.abs(x if x.ndim == 1 else x.max(axis=-1)) >= floor)
            if loud.size:
                x = x[loud[0]: loud[-1] + 1]
            pieces.append(x)
        gap = np.zeros((int(sr * self.cfg.audio.tts_join_gap_ms / 1000),)
                       + pieces[0].shape[1:], dtype=np.float32)
        joined: list[np.ndarray] = []
        for i, p in enumerate(pieces):
            if i:
                joined.append(gap)
            joined.append(p)
        buf = io.BytesIO()
        sf.write(buf, np.concatenate(joined), sr, format="WAV")
        return buf.getvalue()


class VoicePipeline:
    """Produce a single voiced item: copy -> cleanup -> render -> QC -> normalize -> store."""

    ROLE_INFO = {
        "liner": ("liners", "short station id or gag line"),
        "commercial": ("commercials", "satirical 15-30s commercial spot"),
        "dj_talk": ("dj_talk", "conversational DJ filler: station life, the town, the "
                    "weather in Thistledown, recent songs, the time of day"),
        "news": ("news", "news bulletin headline"),
        "intro": ("dj_talk", "short DJ intro for the very next song; name the title and artist"),
    }

    def __init__(self, cfg: Config, llm: LLM, kokoro: KokoroClient,
                 db=None, media_dir: Path | None = None, prompts=None):
        self.cfg = cfg
        self.llm = llm
        self.kokoro = kokoro
        self.db = db
        self.media_dir = media_dir or (Path(__file__).resolve().parent.parent / cfg.library.dir)
        self.prompts = prompts or {}

    def _model_for(self, role: str) -> str:
        return getattr(self.cfg.models, self.ROLE_INFO[role][0], "qwen38")

    def _voice_for(self, role: str) -> str:
        if role == "commercial":
            return self.cfg.voices.commercials
        if role == "news":
            return self.cfg.voices.news
        return self.cfg.voices.dj

    async def write_copy(self, role: str, target_s: float,
                         context: str | None = None) -> dict:
        prompt = self.prompts.get("voice")
        if not prompt:
            raise LLMError("voice prompt template missing")
        extra = context or ""
        schema = ('Return JSON only: {"text": "<copy>", "est_duration_s": <number>, '
                  '"evergreen": true}') if role != "news" else (
            'Return JSON only: {"text": "<copy>", "est_duration_s": <number>, '
            '"gravity": "serious"|"normal"}')
        sys_prompt = f"{prompt}\n\nRole: {self.ROLE_INFO[role][1]}. Target {target_s:.0f}s."
        user = (f"Write the copy. {schema}\n\nContext:\n{extra}"
                if extra else f"Write the copy. {schema}")
        obj = await self.llm.chat_json(self._model_for(role), sys_prompt, user)
        text = str(obj.get("text", "")).strip()
        if not text:
            raise LLMError("copy empty")
        est = float(obj.get("est_duration_s", target_s) or target_s)
        return {"text": text, "est_duration_s": est, **obj}

    async def render(self, copy: dict, role: str, target_s: float) -> dict:
        clean = tts_cleanup(copy["text"])
        voice = self._voice_for(role)
        speed = self.cfg.voices.speed
        wav = await self.kokoro.synth(clean, voice, speed)
        tmp = self.media_dir / "_render_tmp.wav"
        tmp.write_bytes(wav)
        data, sr = sf.read(str(tmp))
        x = np.asarray(data, dtype=np.float32)
        duration = float(len(x)) / sr
        verdict = grade_audio(
            x, sr, duration_s=duration, min_dur=0.5, max_dur=float("inf"),
            kind="voice", max_gap_s=1.0, silence_db=self.cfg.audio.silence_db)
        if not verdict.ok:
            raise ValueError(f"voice QC failed: {verdict.reasons}")
        # rough duration targeting: accept within ±40% since voice length is an estimate
        if duration < 0.4 * target_s or duration > 2.2 * target_s:
            raise ValueError(f"voice duration {duration}s outside tolerance of {target_s}s")
        # normalize -> flac
        dst = self.media_dir / f"{role}_{int(__import__('time').time()*1000)}.flac"
        meta = normalize.normalize(tmp, dst, self.cfg)
        tmp.unlink(missing_ok=True)
        # speech-rate gate: re-run the duration tolerance on the NORMALIZED audio
        # too (post-processing can shorten it), and reject truncated TTS — too many
        # words for the audio length (OVERHAUL 2.1). dst is a file we just made.
        norm_dur = cast(float, meta["duration_s"])
        if norm_dur < 0.4 * target_s or norm_dur > 2.2 * target_s:
            dst.unlink(missing_ok=True)
            raise ValueError(f"voice duration {norm_dur}s outside tolerance of {target_s}s")
        words = len(clean.split())
        speech_s = max(norm_dur - self.cfg.audio.edge_pad_ms / 1000.0, 0.1)
        words_per_s = words / speech_s
        lo, hi = self.cfg.audio.min_words_per_s, self.cfg.audio.max_words_per_s
        if not (lo <= words_per_s <= hi):
            dst.unlink(missing_ok=True)
            raise ValueError(
                f"voice speech rate {words_per_s:.2f} w/s outside [{lo},{hi}] — truncated?")
        return {"path": dst, "duration_s": norm_dur,
                "sample_rate": meta["sample_rate"], "channels": meta["channels"],
                "words": words, "words_per_s": words_per_s}

    async def produce_item(self, role: str, target_s: float,
                           context: str | None = None) -> dict:
        base = {"item_type": role, "target_s": target_s}
        try:
            copy = await self.write_copy(role, target_s, context)
        except Exception as e:
            log.warning("voice.rejected", extra={**base, "stage": "copy", "error": str(e)})
            raise
        try:
            rendered = await self.render(copy, role, target_s)
        except Exception as e:
            log.warning("voice.rejected", extra={**base, "stage": "render", "error": str(e)})
            raise
        item = {
            "type": role, "media_path": str(rendered["path"]), "duration_s": rendered["duration_s"],
            "sample_rate": rendered["sample_rate"], "channels": rendered["channels"],
            "role": role, "evergreen": bool(copy.get("evergreen", True)),
            "gravity": copy.get("gravity"),
            "meta": {"text": copy.get("text"), "words": rendered.get("words"),
                      "words_per_s": rendered.get("words_per_s")},
        }
        log.info("voice.produced", extra={
            **base, "words": len(copy.get("text", "").split()),
            "duration_s": rendered["duration_s"]})
        return item

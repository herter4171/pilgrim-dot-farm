"""Voice pipeline (RADIO.md §6.3, §5.3): copy -> TTS cleanup -> Kokoro -> QC -> normalize."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import IO, Optional

import httpx
import numpy as np
import soundfile as sf

from audio import normalize
from audio.qc import grade_audio
from config import Config
from pipelines.llm import LLM, LLMError

log = logging.getLogger("radio.voice")

# Kokoro reads EVERYTHING literally: markdown, emoji, stage directions, URLs.
_MD = re.compile(r"[*_`#~>{}\[\]()]")
_EMOJI = re.compile(
    "[" u"\U0001F600-\U0001F64F" u"\U0001F300-\U0001F5FF" u"\U0001F680-\U0001F6FF"
    u"\U0001F1E0-\U0001F1FF" u"\U00002702-\U000027B0" u"\U000024C2-\U0001F251"
    u"\U0001f900-\U0001f9ff" u"\u2600-\u27BF" u"\u2B00-\u2BFF" "]+")
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

    async def synth(self, text: str, voice: str, speed: float = 1.0) -> bytes:
        r = await self._client.get(
            self.cfg.hosts.kokoro.rstrip("/") + "/tts",
            params={"text": text, "voice": voice, "speed": str(speed), "format": "wav"})
        r.raise_for_status()
        return r.content


class VoicePipeline:
    """Produce a single voiced item: copy -> cleanup -> render -> QC -> normalize -> store."""

    ROLE_INFO = {
        "liner": ("liners", "short station id or gag line"),
        "commercial": ("commercials", "satirical 15-30s commercial spot"),
        "dj_talk": ("dj_talk", "conversational DJ talk-up referencing previous and next"),
        "news": ("news", "news bulletin headline"),
    }

    def __init__(self, cfg: Config, llm: LLM, kokoro: KokoroClient,
                 db=None, media_dir: Optional[Path] = None, prompts=None):
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
                         context: Optional[str] = None) -> dict:
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
        obj = await self.llm.chat_json(self._model_for(role), sys_prompt, user, max_tokens=600)
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
        return {"path": dst, "duration_s": meta["duration_s"],
                "sample_rate": meta["sample_rate"], "channels": meta["channels"]}

    async def produce_item(self, role: str, target_s: float,
                           context: Optional[str] = None) -> dict:
        copy = await self.write_copy(role, target_s, context)
        rendered = await self.render(copy, role, target_s)
        item = {
            "type": role, "media_path": str(rendered["path"]), "duration_s": rendered["duration_s"],
            "sample_rate": rendered["sample_rate"], "channels": rendered["channels"],
            "role": role, "evergreen": bool(copy.get("evergreen", True)),
            "gravity": copy.get("gravity"), "meta": {"text": copy.get("text")},
        }
        return item

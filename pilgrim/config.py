"""Configuration loading, Clock and RNG abstractions (RADIO.md §12, AGENTS §4)."""
from __future__ import annotations

import os
import random
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Clock + RNG — the injected sources of time/randomness (AGENTS §1.3)
# --------------------------------------------------------------------------- #
class Clock:
    """Injected wall/sim clock. All scheduling reads elapsed seconds from here."""
    def __init__(self, start: float | None = None) -> None:
        self._start = start if start is not None else time.monotonic()

    def now(self) -> float:
        """Monotonic seconds since the clock started. Saturates internally."""
        return time.monotonic() - self._start

    def wall(self) -> float:
        """Unix-ish wall time. Only for expiry checks (news/dj ttl), never
        scheduling arithmetic — keeps tests on SimClock (AGENTS §1.3, OVERHAUL 2.4)."""
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class SimClock(Clock):
    """A clock that can be advanced by hand / by the simulation driver."""
    def __init__(self, epoch: float = 1_800_000_000.0) -> None:
        self._t = 0.0
        self._epoch = epoch

    def now(self) -> float:
        return self._t

    def wall(self) -> float:
        return self._epoch + self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds

    def sleep(self, seconds: float) -> None:
        # Simulation does not actually sleep; it jumps.
        self.advance(seconds)


class RNG:
    """Injected random source so runs are reproducible with a seed."""
    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    def random(self) -> float:
        return self._rng.random()

    def choice(self, seq: list[Any]) -> Any:
        return self._rng.choice(seq)

    def choices(self, seq: list[Any], weights: list[float] | None = None, k: int = 1) -> list[Any]:
        return self._rng.choices(seq, weights=weights, k=k)

    def randint(self, a: int, b: int) -> int:
        return self._rng.randint(a, b)

    def shuffle(self, seq: list[Any]) -> None:
        self._rng.shuffle(seq)

    def sample(self, seq: list[Any], k: int = 1) -> list[Any]:
        return self._rng.sample(seq, k)

    def weighted_choice(self, pairs: dict[str, float]) -> str:
        keys = list(pairs.keys())
        if not keys:
            raise ValueError("empty weighted choice")
        return self._rng.choices(keys, weights=list(pairs.values()), k=1)[0]


# --------------------------------------------------------------------------- #
# Config models
# --------------------------------------------------------------------------- #
class Hosts(BaseModel):
    mlx_serve: str
    kokoro: str
    litellm: str
    searxng: str
    sfx: str = "http://127.0.0.1:8500"  # Stable Audio SFX wrapper (SFX.md §1)


class Models(BaseModel):
    news: str
    briefs: str
    dj_talk: str
    commercials: str
    liners: str
    field_report: str = "qwen38"  # field reporter copy (SFX.md §4.2)


class Voices(BaseModel):
    dj: str
    news: str
    commercials: str
    liners: str
    field_reporter: str = "bm_lewis"  # British field reporter (SFX.md §4.2, user pick)
    speed: float = 1.0


class SongRotation(BaseModel):
    """Recycled-song weighting (PRIORITIES §3, RADIO.md §5.2 Tier 2).

    weight = youth * (wait_floor + (1 - wait_floor) * wait):
    - youth  halves every `half_life_h` hours, floored at `youth_floor` so no
      song ever starves; recent songs pull more airtime than old ones.
    - wait  grows 0..1 while a song goes unheard, saturating at
      `wait_target_h`; a never-aired song counts as unheard forever.
    """
    half_life_h: float = 12.0
    youth_floor: float = 0.1
    wait_target_h: float = 3.0
    wait_floor: float = 0.2


class Playout(BaseModel):
    committed_lookahead_s: int = 600
    filler_horizon_s: int = 60  # spacing-breaking repeats only below this coverage
    window_trim_keep_s: int = 120
    weights: dict[str, float] = Field(
        default_factory=lambda: {"song": .55, "dj_talk": .12,
                                 "commercial_break": .15, "liner": .10, "news": .08,
                                 "field_report": .05})
    max_consecutive_non_song: int = 2
    news_min_spacing_s: int = 1200
    news_ttl_s: int = 2700
    commercial_min_spacing_s: int = 1800
    song_min_spacing_s: list[int] = Field(default_factory=lambda: [14400, 7200, 3600])
    genre_no_repeat: int = 3
    song_rotation: SongRotation = Field(default_factory=SongRotation)


class Inventory(BaseModel):
    fresh_songs_ready: int = 2
    briefs_queued: int = 3
    commercials_min: int = 6
    liners_per_bucket: int = 3
    liner_buckets_s: list[int] = Field(default_factory=lambda: [3, 5, 10, 15, 30])
    dj_talk_min: int = 2
    field_reports_min: int = 1  # unaired field reports kept ready (SFX.md §4.2)


class Songs(BaseModel):
    # max_duration_s is sent to mlx-serve as duration_seconds (the model's ceiling).
    genres: dict[str, float] = Field(default_factory=dict)
    min_duration_s: float = 20.0  # sanity floor (OVERHAUL 3.2)
    max_duration_s: float = 360.0  # sent as duration_seconds; also the song QC ceiling
    abrupt_fade_s: float = 2.5  # fade-out applied to songs with hard endings (3.2)


class News(BaseModel):
    enabled: bool = True
    refresh_s: int = 1800
    max_searches: int = 3
    budget_s: int = 60


class Visitors(BaseModel):
    """Persistent unique-visitor counter (COSMETIC_PATCHING §6, RADIO §14).

    One distinct canonical public client IP counts once for the lifetime of the
    counter. `trusted_proxies` names peers whose `X-Real-IP` / first
    `X-Forwarded-For` entry we trust as the real client address (the verified
    deployment path is nginx setting `X-Real-IP` on loopback). The raw
    canonical IP is stored for dedupe."""
    enabled: bool = True
    trusted_proxies: list[str] = Field(default_factory=lambda: ["127.0.0.1", "::1"])


class Requests(BaseModel):
    """Listener request line (FIFO + LLM moderation)."""
    queue_cap: int = 10
    max_length: int = 160
    moderation_model: str = "qwen38"
    moderation_max_tokens: int = 4096  # replaces the hardcoded value (OVERHAUL 4.2)
    per_client_per_10min: int = 3  # rate limit on the request line (OVERHAUL 4.3)


class Audio(BaseModel):
    lufs: float = -16.0
    true_peak_db: float = -1.0
    lra: float = 7.0
    edge_pad_ms: int = 150
    tts_join_gap_ms: int = 250  # pause between Kokoro chunks after edge trim
    delivery: str = "flac"
    silence_db: int = -50
    max_words_per_s: float = 3.6  # speech-rate QC ceiling (truncation smell)
    min_words_per_s: float = 1.2  # speech-rate QC floor


class Logging(BaseModel):
    """Human-readable logging to stdout + a rotating file (RADIO.md §12).

    Each record is one greppable line: ``<ts> <LEVEL> <logger> <message>
    key=value ...``. Color is added only on the console when writing to a
    terminal (or no NO_COLOR override); the file is always plain text.
    OVERHAUL 1.1 (was JSON-lines)."""
    level: str = "INFO"
    dir: str = "logs"
    file: str = "station.log"
    max_bytes: int = 10485760  # 10 MB
    backups: int = 5


class Library(BaseModel):
    soft_cap_gb: int = 100
    dir: str = "library"
    web_dir: str = "web"
    db: str = "station.db"
    bible_dir: str = "bible"
    prompts_dir: str = "prompts"


class Station(BaseModel):
    name: str = "Pilgrim Dot Farm"
    port: int = 5000
    rng_seed: int | None = None
    host: str = "0.0.0.0"
    timezone: str = "America/Detroit"  # DJ clock time (OVERHAUL 5.3)


class Talk(BaseModel):
    """Contextual talk settings (OVERHAUL 5.3)."""
    time_mention_ttl_s: int = 900  # DJ clips that mention the time expire after this
    field_reporter_name: str = "Giles"  # placeholder until the user names him (SFX.md §10)


class SfxCue(BaseModel):
    """One curated sound effect (SFX.md §6). `evergreen` cues are rendered once
    into a recycled stock pool; contextual cues are rendered fresh for the host
    clip that calls for them. Prompts are a lottery, so only `approved` cues
    (picked by ear) are ever rendered or aired."""
    prompt: str
    duration_s: float = 2.0
    seed: int | None = None  # fixed seed = byte-identical clip; None = injected RNG
    evergreen: bool = False
    approved: bool = True


class Sfx(BaseModel):
    """Sound-effect overlays on non-news talk (SFX.md §0, §7)."""
    enabled: bool = True
    model: str = "stable-audio-3-small-sfx"
    steps: int = 8
    cfg_scale: float = 4.0
    timeout_s: float = 60.0
    stinger_gain: float = 0.5  # undecided by ear (SFX.md §10.3): a knob, not baked in
    bed_gain: float = 0.15
    host_duck: float = 0.7  # host gain under a stinger, so the sum doesn't clip
    bed_cue: str = "wind_bed"  # ambience under every field report
    bed_max_s: float = 30.0
    joke_cue: str = "rimshot"  # the 50/50 da-dum-tiss after a marked joke
    joke_p: float = 0.5
    max_per_window: int = 3  # rolling stinger budget ...
    window_s: float = 10.0  # ... per this many seconds of air
    max_stinger_s: float = 2.5
    cues: dict[str, SfxCue] = Field(default_factory=dict)


class Config(BaseModel):
    station: Station = Field(default_factory=Station)
    hosts: Hosts = Field(default_factory=Hosts)  # type: ignore[arg-type]
    models: Models = Field(default_factory=Models)  # type: ignore[arg-type]
    voices: Voices = Field(default_factory=Voices)  # type: ignore[arg-type]
    playout: Playout = Field(default_factory=Playout)
    inventory: Inventory = Field(default_factory=Inventory)
    songs: Songs = Field(default_factory=Songs)
    news: News = Field(default_factory=News)
    requests: Requests = Field(default_factory=Requests)
    visitors: Visitors = Field(default_factory=Visitors)
    audio: Audio = Field(default_factory=Audio)
    logging: Logging = Field(default_factory=Logging)
    talk: Talk = Field(default_factory=Talk)
    sfx: Sfx = Field(default_factory=Sfx)
    library: Library = Field(default_factory=Library)


def load_config(path: Path | None = None) -> Config:
    path = path or (ROOT / "config.yaml")
    raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
    return Config(**raw)


def load_api_key(env_file: Path | None = None) -> str:
    """LiteLLM bearer token: $LITELLM_TOKEN, else LITELLM_TOKEN= in the repo-root .env.

    `make run` injects the variable, but `python -m pilgrim.server` does not, so
    read .env as a fallback rather than sending an empty `Bearer ` header.
    """
    token = os.environ.get("LITELLM_TOKEN", "").strip()
    if token:
        return token
    env_file = env_file or (ROOT.parent / ".env")
    try:
        lines = env_file.read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and key.strip() == "LITELLM_TOKEN":
            return value.strip().strip("'\"")
    return ""


def ensure_dirs(cfg: Config) -> None:
    for d in [cfg.library.dir, cfg.library.web_dir, cfg.library.bible_dir, cfg.library.prompts_dir]:
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    Path(str(ROOT / cfg.library.db)).parent.mkdir(parents=True, exist_ok=True)

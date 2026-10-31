"""Configuration loading, Clock and RNG abstractions (RADIO.md §12, AGENTS §4)."""
from __future__ import annotations

import ipaddress
import os
import random
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, PrivateAttr, model_validator

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
    sfx: str = "http://127.0.0.1:8000"  # MOSS-SoundEffect v2.0 (docs/backends.md §7)


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
    # 300 s (5 min) of committed audio ahead: enough to absorb production
    # latency without queueing a listener request behind ten minutes of stock.
    committed_lookahead_s: int = 300
    filler_horizon_s: int = 60  # spacing-breaking repeats only below this coverage
    # While a listener request is pending (queued/producing/ready, not yet
    # committed), hold the window at filler_horizon_s so it takes the next slot.
    # Bounded per request so a stuck request cannot pin a shallow lookahead.
    request_hold_max_s: int = 900
    window_trim_keep_s: int = 120
    weights: dict[str, float] = Field(
        default_factory=lambda: {"song": .55, "dj_talk": .12,
                                 "commercial_break": .15, "liner": .10, "news": .08,
                                 "field_report": .05})
    max_consecutive_non_song: int = 2
    top_of_hour_window_s: float = 240.0  # a song gap this close to :00 leads with words
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
    station_ids_min: int = 3  # emergency station IDs: words between songs from cold start


class Songs(BaseModel):
    # max_duration_s is sent to mlx-serve as duration_seconds (the model's ceiling).
    # Keys are style-family ids from pilgrim/genres.py (the MiniMax Music 3
    # prompting guide §5 vocabulary); values are selection weights.
    genres: dict[str, float] = Field(default_factory=dict)
    min_duration_s: float = 20.0  # sanity floor (OVERHAUL 3.2)
    max_duration_s: float = 360.0  # sent as duration_seconds; also the song QC ceiling
    abrupt_fade_s: float = 2.5  # fade-out applied to songs with hard endings (3.2)
    # idle gap between STOCK song generations (finish -> next start); queued
    # listener requests skip it. 0 = back to back (§6.1)
    stock_gap_s: float = 600.0
    # Song generation is the expensive path: once the library holds this many
    # usable (non-retired) songs, stock generation pauses entirely and only
    # listener requests produce. 0 disables the cap (§6.1)
    stock_max_library: int = 100

    @model_validator(mode="after")
    def _known_families(self) -> Songs:
        from pilgrim.genres import FAMILIES
        unknown = sorted(set(self.genres) - set(FAMILIES))
        if unknown:
            raise ValueError(
                f"songs.genres has unknown style families {unknown}; "
                f"use ids from pilgrim/genres.py")
        return self


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
    canonical IP is stored for dedupe.

    `trusted_proxies` entries are single IPs or CIDRs, parsed into networks at
    validation. Assigning `trusted_proxies` on an existing instance does NOT
    re-parse them; build a new `Visitors(...)` instead.

    The live listener count is in-memory: a player counts while its heartbeats
    (every 5 s) arrive within `listener_timeout_s`; `max_listeners` bounds the
    tracked player IDs."""
    enabled: bool = True
    trusted_proxies: list[str] = Field(default_factory=lambda: ["127.0.0.1", "::1"])
    listener_timeout_s: float = 15.0
    max_listeners: int = 1000

    # Parsed at load time (SPLIT_PLAN §3.2). Private so it never leaks into
    # model_dump()/JSON (the e2e server dumps station config).
    _networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = PrivateAttr(
        default_factory=list)

    @model_validator(mode="after")
    def _parse_trusted_proxies(self) -> Visitors:
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for entry in self.trusted_proxies:
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                raise ValueError(
                    f"visitors.trusted_proxies entry {entry!r} is not a valid "
                    "IP address or CIDR network") from None
        self._networks = networks
        return self

    def is_trusted(self, canonical_ip: str | None) -> bool:
        """True when `canonical_ip` is inside any configured trusted network."""
        if not canonical_ip:
            return False
        try:
            addr = ipaddress.ip_address(canonical_ip)
        except ValueError:
            return False
        return any(addr in net for net in self._networks)


class Requests(BaseModel):
    """Listener request line (FIFO + LLM moderation)."""
    queue_cap: int = 20
    max_length: int = 160
    moderation_model: str = "qwen38"
    moderation_max_tokens: int = 4096  # replaces the hardcoded value (OVERHAUL 4.2)
    per_client_per_10min: int = 3  # rate limit on the request line (OVERHAUL 4.3)


class Payments(BaseModel):
    """Stripe paid priority requests (PAYMENT.md §2.1). Off by default so every
    existing test and deployment behaves exactly as before."""
    enabled: bool = False
    api_base: str = "https://api.stripe.com"
    api_version: str = "2026-10-28.endive"  # Stripe-Version on every call (Step 1)
    price_id_live: str = "price_1UWQHdKOiB5HDXLd4qNMoosI"
    price_id_test: str = ""
    currency: str = "usd"
    min_cents: int = 300
    max_cents: int = 6769
    timeout_s: float = 10.0
    retries: int = 2
    checkout_expiry_s: int = 1800  # Stripe's minimum is 30 min
    webhook_tolerance_s: int = 300
    reconcile_after_s: int = 10
    reconcile_min_interval_s: int = 5
    abandon_after_s: int = 86400
    paid_max_length: int = 3600
    paid_max_lines: int = 24
    per_client_per_10min: int = 6
    max_attempts: int = 5
    # Managed Payments is on by default for the account; the product tax code
    # is ineligible, so sessions send managed_payments[enabled]=false.
    managed_payments: bool = False

    @model_validator(mode="after")
    def _validate_limits(self) -> Payments:
        if self.min_cents <= 0:
            raise ValueError("payments.min_cents must be positive")
        if self.min_cents >= self.max_cents:
            raise ValueError("payments.min_cents must be below payments.max_cents")
        if self.checkout_expiry_s < 1800:
            raise ValueError("payments.checkout_expiry_s must be >= 1800 (Stripe minimum)")
        if self.webhook_tolerance_s <= 0 or self.timeout_s <= 0:
            raise ValueError("payments timeouts must be positive")
        if self.retries < 0:
            raise ValueError("payments.retries must be >= 0")
        if self.paid_max_length <= 0 or self.paid_max_lines <= 0:
            raise ValueError("payments paid limits must be positive")
        if self.per_client_per_10min <= 0 or self.max_attempts <= 0:
            raise ValueError("payments rate limit and attempts must be positive")
        return self

    def mode(self, secret_key: str) -> str:
        """`test` or `live` from the key prefix; `""` when unrecognised."""
        key = (secret_key or "").strip()
        if key.startswith(("sk_test_", "rk_test_")):
            return "test"
        if key.startswith(("sk_live_", "rk_live_")):
            return "live"
        return ""

    def price_id(self, secret_key: str) -> str:
        """Price for the key's mode. Live keys never see the test price."""
        return self.price_id_test if self.mode(secret_key) == "test" else self.price_id_live

    def active(self, env: Mapping[str, str]) -> bool:
        """True only when enabled and all three env vars are present."""
        return bool(
            self.enabled
            and env.get("STRIPE_SECRET_KEY", "").strip()
            and env.get("STRIPE_PUBLISHABLE_KEY", "").strip()
            and env.get("STRIPE_WEBHOOK_SECRET", "").strip()
        )


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


class DjCharacter(BaseModel):
    """Operator-only character (TUI.md §6, §10; RADIO.md §12).

    The operator picks the name and **listens before choosing the voice** (a
    human decision, AGENTS §9); empty until then. No random-selector weight and
    no inventory refill target."""
    id: str = "dj_operator"
    name: str = ""
    voice: str = ""
    speed: float = 1.0


class Dj(BaseModel):
    """DJ console (TUI.md §7, RADIO.md §12). `enabled` gates the control
    mutations; read-only console endpoints are available behind the operator
    token once access setup is complete."""
    enabled: bool = False
    state_poll_s: float = 1.0       # cheap state poll interval (console)
    backoff_max_s: float = 10.0     # reconnect backoff ceiling on disconnects
    request_timeout_s: float = 5.0
    cutover_deadline_s: float = 2.0  # acknowledged-skip budget for healthy clients
    cutover_lead_s: float = 1.0      # client-notification lead before activation
    cutover_fade_s: float = 0.02     # anti-click fade at the cutover boundary
    queue_max: int = 20
    phrase_max_chars: int = 280
    phrase_prefix: str = "This is the administrator,"  # sign-on prepended to
    # operator messages that do not already open with it (RADIO.md §4)
    phrase_ttl_s: int = 7200         # unqueued ready phrase lifetime
    job_timeout_s: int = 300
    character: DjCharacter = Field(default_factory=DjCharacter)


class Library(BaseModel):
    soft_cap_gb: int = 100
    dir: str = "library"
    web_dir: str = "web"
    db: str = "station.db"
    bible_dir: str = "bible"
    prompts_dir: str = "prompts"


class Station(BaseModel):
    """Station identity + the two listener sockets (SPLIT_PLAN Step 1).

    `host`/`port` are the PUBLIC listener: only the edge-facing routes (§11).
    `admin_host`/`admin_port` are the ADMIN listener: every route including
    `/api/admin/*` and `/api/station/start|stop`. The admin bind defaults to
    loopback; production must never expose it. `serve_web` mounts the static
    client on the public listener for single-host runs; on the M5 the edges
    serve the site instead and the operator sets it false.
    """
    name: str = "Pilgrim Dot Farm"
    port: int = 5000
    admin_host: str = "127.0.0.1"
    admin_port: int = 5001
    serve_web: bool = True
    rng_seed: int | None = None
    host: str = "0.0.0.0"
    timezone: str = "America/Detroit"  # DJ clock time (OVERHAUL 5.3)

    @model_validator(mode="after")
    def _distinct_listeners(self) -> Station:
        if self.admin_port == self.port:
            raise ValueError(
                f"station.admin_port ({self.admin_port}) must differ from "
                f"station.port ({self.port}); the public and admin listeners "
                "are separate sockets")
        return self


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
    model: str = "moss-soundeffect-v2.0"
    steps: int = 100  # flow-match solver steps (`num_inference_steps`)
    cfg_scale: float = 4.0
    sigma_shift: float = 5.0
    negative_prompt: str = ""
    max_seconds: float = 30.0  # backend hard cap per request
    timeout_s: float = 180.0
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
    payments: Payments = Field(default_factory=Payments)
    visitors: Visitors = Field(default_factory=Visitors)
    audio: Audio = Field(default_factory=Audio)
    logging: Logging = Field(default_factory=Logging)
    talk: Talk = Field(default_factory=Talk)
    sfx: Sfx = Field(default_factory=Sfx)
    dj: Dj = Field(default_factory=Dj)
    library: Library = Field(default_factory=Library)

    @model_validator(mode="after")
    def _validate_character_voice(self) -> Config:
        """The operator character must reserve an *unused* Kokoro voice
        (RADIO.md §10): reject a configured collision with any role voice at
        load time instead of discovering it when a phrase renders.

        An empty `dj.character.voice` is allowed — it means "not assigned yet"
        and keeps the console in setup state (TUI.md §6)."""
        voice = self.dj.character.voice
        if not voice:
            return self
        role_voices = {
            self.voices.dj,
            self.voices.news,
            self.voices.commercials,
            self.voices.liners,
            self.voices.field_reporter,
        }
        if voice in role_voices:
            raise ValueError(
                f"dj.character.voice {voice!r} collides with a configured role "
                "voice; the operator character must reserve an unused Kokoro "
                "voice (RADIO.md §10)")
        return self


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

"""Typed DJ-console contracts (TUI.md §4; RADIO.md §11).

Pydantic request/response models for the two public revision-aware endpoints
and the authenticated ``/api/admin/dj/*`` routes. These are pure data models —
no route, scheduler, or server behavior lives here — so the module is
importable anywhere, including by the Textual console client, which must never
import ``Station``, open ``station.db``, or touch library paths (RADIO §3,
TUI.md §3).

Response schemas used to be empty ``{}`` in OpenAPI (TUI.md §1); every route
these serve must return one of these concrete models so a generated client is
typed. Plain-string enums are spelled with ``Literal`` so OpenAPI stays
explicit and a wrong status fails static analysis.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CommandStatus = Literal[
    "accepted", "preparing", "scheduled", "applied", "rejected", "expired"
]
PhraseStatus = Literal[
    "queued", "rendering", "ready", "scheduled", "aired", "failed", "expired",
    "skipped",
]
SortField = Literal["title", "artist"]
SortDirection = Literal["asc", "desc"]


# --------------------------------------------------------------------------- #
# Public revision-aware endpoints (RADIO §5.3, §11)
# --------------------------------------------------------------------------- #
class SfxOverlay(BaseModel):
    """SFX sidecar riding on a host program item (RADIO §4, §9.2)."""

    media_id: int
    cue: str
    kind: Literal["stinger", "joke", "bed"]
    offset_s: float
    duration_s: float
    gain: float = 0.5
    duck: float = 1.0


class ProgramItem(BaseModel):
    """One committed program row as served by GET /api/station/program."""

    seq: int
    media_id: int
    type: str
    duration_s: float
    sfx: list[SfxOverlay] = Field(default_factory=list)
    title: str = ""
    artist: str = ""


class PreparedCutover(BaseModel):
    """A validated edit awaiting activation (§5.5). None when none is pending."""

    cutover_id: int
    target_revision: int
    effective_at: float  # server wall-clock epoch
    replaces_seqs: list[int] = Field(default_factory=list)


class StationState(BaseModel):
    """Public cheap snapshot for DJ-aware clients (§11 GET /api/station/state)."""

    epoch: str
    revision: int
    server_time: float
    current_seq: int | None = None
    current_type: str | None = None
    current_offset_s: float = 0.0
    prepared_cutover: PreparedCutover | None = None


class ProgramResponse(BaseModel):
    """Extended public programme response. ``reset=True`` means the client's
    cursor was stale (epoch/revision mismatch) — replace the whole list."""

    items: list[ProgramItem]
    start_offset_s: float = 0.0
    epoch: str
    revision: int
    reset: bool = False
    server_time: float = 0.0


class HeartbeatIn(BaseModel):
    """POST /api/station/heartbeat payload, extended for revision-aware
    clients (§9.2): player ID + protocol + observed epoch/revision + readiness.
    These report what the client sees; they never control production."""

    seq: int = 0
    media_id: int = 0
    position: float = 0.0
    started_at: float | None = None
    type: str | None = None
    underrun: bool = False
    player_id: str | None = None
    protocol_version: int = 1
    observed_epoch: str | None = None
    observed_revision: int | None = None
    prepared_cutover_id: int | None = None
    buffered_ahead_s: float | None = None


# --------------------------------------------------------------------------- #
# Operator command surface
# --------------------------------------------------------------------------- #
class DjCommandRequest(BaseModel):
    """Common optimistic-concurrency fields on every DJ mutation (TUI.md §4)."""

    command_id: str = Field(..., min_length=1, max_length=64)
    expected_epoch: str
    expected_revision: int


class SkipRequest(DjCommandRequest):
    """POST /api/admin/dj/skip — the occurrence the operator believes is on air."""

    expected_seq: int


class QueueRequest(DjCommandRequest):
    """POST /api/admin/dj/queue — a ready song, or this character's ready
    phrase. Only one media_id per command; repeats are separate commands."""

    media_id: int


class CommandReceipt(BaseModel):
    """Accepted-command acknowledgement (202). Not a claim a listener heard it."""

    command_id: str
    status: CommandStatus
    submitted_at: float


class CommandResult(BaseModel):
    """Durable command outcome (GET /api/admin/dj/commands/{id})."""

    command_id: str
    status: CommandStatus
    operation: Literal["skip", "queue", "phrase"]
    submitted_at: float
    resolved_at: float | None = None
    applied_revision: int | None = None
    affected_seqs: list[int] = Field(default_factory=list)
    cutover: PreparedCutover | None = None
    reject_reason: str | None = None


# --------------------------------------------------------------------------- #
# Console state + catalogue
# --------------------------------------------------------------------------- #
class DjCurrentItem(BaseModel):
    seq: int
    media_id: int
    type: str
    title: str | None = None
    artist: str | None = None
    position_s: float = 0.0
    remaining_s: float = 0.0


class DjUpcomingItem(BaseModel):
    seq: int
    media_id: int
    type: str
    title: str | None = None
    artist: str | None = None
    duration_s: float = 0.0


class DjState(BaseModel):
    """GET /api/admin/dj/state; computed without backend probes (TUI.md §4)."""

    epoch: str
    revision: int
    server_time: float
    on_air: bool
    current: DjCurrentItem | None = None
    upcoming: list[DjUpcomingItem] = Field(default_factory=list)
    pending_commands: list[str] = Field(default_factory=list)
    catalogue_revision: int = 0
    player_status: str = "unknown"


class DjSong(BaseModel):
    id: int
    title: str | None = None
    artist: str | None = None
    duration_s: float = 0.0
    eligible: bool = True
    reason: str | None = None  # why not eligible (advisory; rechecked at insert)


class DjSongPage(BaseModel):
    items: list[DjSong]
    next_cursor: str | None = None
    catalogue_revision: int


# --------------------------------------------------------------------------- #
# Character + phrases (TUI.md §4, §6)
# --------------------------------------------------------------------------- #
class CharacterInfo(BaseModel):
    character_id: str
    name: str = ""
    setup_complete: bool
    voice: str | None = None
    available_voices: list[str] = Field(default_factory=list)
    voice_discovery_error: str | None = None
    block_reason: str | None = None  # e.g. "no unused voice remains"


class CleanupChange(BaseModel):
    field: str  # e.g. "text"
    change: str  # short human-readable description of what changed


class ValidateResponse(BaseModel):
    """Pure cleaned-text validation (POST /api/admin/dj/phrases/validate)."""

    character_id: str
    cleaned_text: str
    changes: list[CleanupChange] = Field(default_factory=list)
    text_hash: str = ""
    ok: bool
    rejected_reason: str | None = None


class PhraseValidateRequest(BaseModel):
    """POST /api/admin/dj/phrases/validate — pure validation of raw operator
    text; returns the exact cleaned text, changes, text_hash and ok."""

    character_id: str
    text: str


class PhraseSubmitRequest(BaseModel):
    """POST /api/admin/dj/phrases — operator confirms the exact cleaned text via
    ``cleaned_text_hash`` so a validation race cannot change what renders."""

    command_id: str = Field(..., min_length=1, max_length=64)
    character_id: str
    text: str
    cleaned_text_hash: str


class PhraseSubmitResponse(BaseModel):
    command_id: str
    job_id: int
    status: PhraseStatus
    cleaned_text: str


class PhraseJob(BaseModel):
    """GET /api/admin/dj/phrases/{job_id} — render stage and finished clip."""

    job_id: int
    status: PhraseStatus
    character_id: str
    original_text: str
    cleaned_text: str
    voice: str
    expires_at: float | None = None
    media_id: int | None = None
    duration_s: float | None = None
    failure_reason: str | None = None


# --------------------------------------------------------------------------- #
# Structured errors (stable code + readable detail; §5.5)
# --------------------------------------------------------------------------- #
class ErrorResponse(BaseModel):
    code: str
    detail: str

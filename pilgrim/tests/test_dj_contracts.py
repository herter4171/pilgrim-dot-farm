"""Milestone 1 (TUI.md §8 step 1): typed DJ contracts + operator auth.

Pure model round-trips and the authorization helper — no routes or scheduler
behavior yet (those land with the read-only console, milestone 2).
"""
from __future__ import annotations

from typing import cast

import pytest
from fastapi import HTTPException
from pilgrim import dj_contracts as c
from pilgrim.dj_security import check_dj_authorized
from pydantic import ValidationError


def test_program_item_defaults() -> None:
    it = c.ProgramItem(seq=1, media_id=2, type="song", duration_s=180.0)
    assert it.title == "" and it.artist == "" and it.sfx == []


def test_program_item_with_overlay() -> None:
    ov = c.SfxOverlay(media_id=9, cue="cow", kind="stinger",
                      offset_s=1.5, duration_s=2.0)
    it = c.ProgramItem(seq=1, media_id=2, type="dj_talk", duration_s=9.0,
                       sfx=[ov])
    assert it.sfx[0].duck == 1.0  # default; host-duck applied at air time


def test_program_response_reset_flag() -> None:
    r = c.ProgramResponse(items=[], epoch="e1", revision=3,
                          reset=True, server_time=1.5)
    assert r.reset is True
    assert r.start_offset_s == 0.0
    assert r.epoch == "e1" and r.revision == 3


def test_station_state_current() -> None:
    s = c.StationState(epoch="e1", revision=5, server_time=100.0,
                       current_seq=42, current_type="song", current_offset_s=7.25)
    assert s.current_seq == 42
    assert s.prepared_cutover is None


def test_station_state_with_cutover() -> None:
    cut = c.PreparedCutover(cutover_id=1, target_revision=6, effective_at=105.0,
                            replaces_seqs=[42, 43, 44])
    s = c.StationState(epoch="e1", revision=6, server_time=102.0,
                       prepared_cutover=cut)
    assert s.prepared_cutover is not None
    assert s.prepared_cutover.replaces_seqs == [42, 43, 44]


def test_heartbeat_revision_fields() -> None:
    hb = c.HeartbeatIn(seq=9, media_id=4, position=12.5, observed_epoch="e1",
                       observed_revision=7, prepared_cutover_id=42, underrun=True)
    assert hb.observed_revision == 7
    assert hb.prepared_cutover_id == 42
    assert hb.protocol_version == 1  # default when the client sends none


def test_skip_request() -> None:
    req = c.SkipRequest(command_id="cmd-1", expected_epoch="e1",
                        expected_revision=18, expected_seq=99)
    assert req.expected_seq == 99
    assert req.expected_revision == 18


def test_queue_request() -> None:
    req = c.QueueRequest(command_id="cmd-2", expected_epoch="e1",
                         expected_revision=18, media_id=412)
    assert req.media_id == 412


def test_command_result_status_enum() -> None:
    for status in ("accepted", "preparing", "scheduled", "applied",
                   "rejected", "expired"):
        r = c.CommandResult(command_id="x", status=status, operation="skip",
                            submitted_at=0.0)
        assert r.status == status
    with pytest.raises(ValidationError):
        # deliberately invalid status; rejected at runtime by pydantic
        c.CommandResult(command_id="x", status="bogus",  # type: ignore[arg-type]
                        operation="skip", submitted_at=0.0)


def test_phrase_job_ready() -> None:
    job = c.PhraseJob(job_id=1, status="ready", character_id="op",
                      original_text="hi", cleaned_text="hi", voice="am_liam",
                      media_id=7, duration_s=2.0)
    assert job.media_id == 7
    assert job.failure_reason is None


def test_phrase_job_failed() -> None:
    job = c.PhraseJob(job_id=2, status="failed", character_id="op",
                      original_text="hi", cleaned_text="hi", voice="am_liam",
                      failure_reason="QC rejected: mostly silent")
    assert job.failure_reason is not None
    assert job.failure_reason.startswith("QC")


def test_validate_response_ok_and_rejected() -> None:
    ok = c.ValidateResponse(character_id="op", cleaned_text="hi", ok=True)
    assert ok.ok and ok.rejected_reason is None
    bad = c.ValidateResponse(character_id="op", cleaned_text="",
                             ok=False, rejected_reason="empty after cleanup")
    assert not bad.ok


def test_character_info_unused_voices() -> None:
    ch = c.CharacterInfo(character_id="op", setup_complete=False,
                         available_voices=["af_bella", "am_puck"])
    assert ch.available_voices == ["af_bella", "am_puck"]
    assert ch.block_reason is None


def test_error_response_shape() -> None:
    e = c.ErrorResponse(code="stale", detail="epoch/revision mismatch")
    assert e.code == "stale"


# --------------------------------------------------------------------------- #
# operator auth
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("headers,expected", [
    ({"Authorization": "Bearer sekrit"}, "ok"),
    ({"authorization": "Bearer sekrit"}, "ok"),          # case-insensitive
    ({"Authorization": "Bearer wrong"}, "401"),
    ({}, "401"),
    ({"Authorization": "Token sekrit"}, "401"),          # wrong scheme
])
def test_check_dj_authorized(monkeypatch: pytest.MonkeyPatch,
                             headers: dict[str, str], expected: str) -> None:
    monkeypatch.setenv("PILGRIM_DJ_TOKEN", "sekrit")
    try:
        check_dj_authorized(headers)
        assert expected == "ok"
    except HTTPException as e:
        assert str(e.status_code) == expected


def test_check_dj_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # isolate from any PILGRIM_DJ_TOKEN in the real repo .env; "no token" -> 403
    from pilgrim import dj_security
    monkeypatch.setattr(dj_security, "load_dj_token", lambda env_file=None: "")
    with pytest.raises(HTTPException) as ei:
        check_dj_authorized({"Authorization": "Bearer anything"})
    assert ei.value.status_code == 403
    detail = cast(dict, ei.value.detail)
    assert detail["code"] == "dj_disabled"


def test_check_dj_dotenv_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A PILGRIM_DJ_TOKEN= line in .env satisfies auth when the env var is unset."""
    env = tmp_path / ".env"
    env.write_text("PILGRIM_DJ_TOKEN=from-dotenv\nLITELLM_TOKEN=unrelated\n")
    monkeypatch.delenv("PILGRIM_DJ_TOKEN", raising=False)

    # load_dj_token is consulted inside check_dj_authorized -> monkeypatch path
    from pilgrim import dj_security

    monkeypatch.setattr(dj_security, "load_dj_token",
                        lambda env_file=None: _read_dotenv_token(str(env)))
    check_dj_authorized({"Authorization": "Bearer from-dotenv"})  # no raise


def _read_dotenv_token(path: str) -> str:
    with open(path) as f:
        for line in f:
            key, sep, value = line.strip().partition("=")
            if sep and key == "PILGRIM_DJ_TOKEN":
                return value.strip()
    return ""

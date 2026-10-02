"""Operator phrase jobs (RADIO.md §11, §6.3; TUI.md §6).

A phrase is submitted **verbatim** by the station operator through the DJ
console API: the typed words are the copy and are never rewritten or extended
by an LLM. The mandatory TTS cleanup (\u00a7voice.tts_cleanup) runs on
submission; the operator sees the exact cleaned text and confirms it via a
`cleaned_text_hash` over (character, cleaned text, config version) so a
submission/render race cannot change what is spoken.

Lifecycle: ``queued -> rendering -> ready -> scheduled -> aired`` with
``failed`` / ``expired`` / ``skipped`` terminal outcomes. One-shot by
construction: the ready item is consumed when it is placed (never recycled,
AGENTS rule 10). Idempotent by ``command_id``.

Jobs are held in memory (the process owning ``Station`` is the job owner; a
restart drains unplaced jobs, as they are live operator intent, not durable
inventory). No SFX in v1 (RADIO \u00a711 operator_phrase).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any

from pilgrim.logging_setup import err_text
from pilgrim.pipelines.voice import VoicePipeline, tts_cleanup

log = logging.getLogger("radio.operator_phrase")

# Conservative control/escape sequences that must never reach a TTS synth.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL = re.compile(r"https?://\S+|www\.\S+")

_STATUSES = ("queued", "rendering", "ready", "scheduled", "aired", "failed",
             "expired", "skipped")


def phrase_hash(character_id: str, cleaned_text: str, version: str) -> str:
    """Stable hash binding a phrase to its character + config version."""
    key = f"{character_id}\x00{cleaned_text}\x00{version}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def clean_and_diff(raw: str) -> tuple[str, list[dict]]:
    """Run the mandatory TTS cleanup and report what changed (TUI.md §6)."""
    base = raw.strip()
    cleaned = tts_cleanup(base)
    changes: list[dict] = []
    if base != cleaned:
        changes.append({"field": "text", "change": "TTS cleanup applied"})
    return cleaned, changes


class PhraseManager:
    """In-memory operator-phrase job registry + render loop (TUI.md §6)."""

    def __init__(self, cfg, store, voice: VoicePipeline, media_dir: Path,
                 scheduler: Any | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.voice = voice
        self.media_dir = media_dir
        self.scheduler = scheduler
        self._jobs: dict[int, dict] = {}
        self._by_command: dict[str, int] = {}   # command_id -> job_id
        self._by_item: dict[int, int] = {}      # item_id -> job_id
        self._next_id = 1
        self._version = self._config_version()
        self._failures: dict[str, int] = {}
        self._lock = asyncio.Lock()

    def _config_version(self) -> str:
        ch = self.cfg.dj.character
        return "|".join([ch.id, ch.name or "", ch.voice or "", str(ch.speed)])

    # ----------------------------------------------------------------- setup
    def character(self) -> dict:
        ch = self.cfg.dj.character
        return {
            "character_id": ch.id,
            "name": ch.name,
            "setup_complete": bool(ch.name and ch.voice),
            "voice": ch.voice or None,
            "speed": ch.speed,
        }

    # ----------------------------------------------------------------- validate
    def validate(self, character_id: str, text: str) -> dict:
        if character_id != self.cfg.dj.character.id:
            return self._reject("unknown_character", character_id, text)
        if not text or not text.strip():
            return self._reject("empty", character_id, text)
        if _URL.search(text):
            return self._reject("url", character_id, text)
        cleaned, changes = clean_and_diff(text)
        if not cleaned:
            return self._reject("empty_after_cleanup", character_id, text)
        if len(cleaned) > self.cfg.dj.phrase_max_chars:
            return self._reject("too_long", character_id, text, cleaned=cleaned,
                                changes=changes)
        return {
            "character_id": character_id,
            "cleaned_text": cleaned,
            "changes": changes,
            "text_hash": phrase_hash(character_id, cleaned, self._version),
            "ok": True,
            "rejected_reason": None,
        }

    def _reject(self, reason: str, character_id: str, text: str,
                cleaned: str | None = None, changes: list | None = None) -> dict:
        return {
            "character_id": character_id,
            "cleaned_text": cleaned or text.strip(),
            "changes": changes or [],
            "text_hash": phrase_hash(character_id, cleaned or text.strip(), self._version),
            "ok": False,
            "rejected_reason": reason,
        }

    # ----------------------------------------------------------------- submit
    def submit(self, command_id: str, character_id: str, text: str,
               cleaned_text_hash: str) -> dict:
        """Create a phrase job (fast, no I/O). Idempotent by command_id; returns
        202-style {command_id, job_id, status, cleaned_text}."""
        if not self.cfg.dj.enabled:
            raise ValueError("dj_disabled")
        if not self.cfg.dj.character.voice:
            raise ValueError("no_voice")
        v = self.validate(character_id, text)
        if not v["ok"]:
            raise ValueError(v["rejected_reason"])
        if v["text_hash"] != cleaned_text_hash:
            raise ValueError("hash_mismatch")
        existing = self._by_command.get(command_id)
        if existing is not None:
            if self._jobs[existing]["text_hash"] == v["text_hash"]:
                # identical retry after a lost response: return the existing job
                return {"command_id": command_id, "job_id": existing,
                        "status": self._jobs[existing]["status"],
                        "cleaned_text": self._jobs[existing]["cleaned_text"]}
            raise ValueError("conflict")
        ch = self.cfg.dj.character
        job_id = self._next_id
        self._next_id += 1
        now = time.time()
        job = {
            "job_id": job_id, "command_id": command_id,
            "character_id": character_id,
            "original_text": text,
            "cleaned_text": v["cleaned_text"],
            "text_hash": v["text_hash"],
            "voice": ch.voice, "speed": ch.speed,
            "status": "queued",
            "created_at": now,
            "expires_at": now + self.cfg.dj.phrase_ttl_s,
            "media_id": None, "duration_s": None, "failure_reason": None,
        }
        self._jobs[job_id] = job
        self._by_command[command_id] = job_id
        log.info("phrase.job_created", extra={
            "job_id": job_id, "character_id": character_id,
            "len": len(v["cleaned_text"])})
        return {"command_id": command_id, "job_id": job_id,
                "status": job["status"], "cleaned_text": v["cleaned_text"]}

    def get(self, job_id: int) -> dict | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        # reflect current scheduling state without a callback: the item is
        # 'scheduled' the moment it appears in the committed program.
        if (job["status"] == "ready" and job["media_id"] is not None
                and self._item_in_program(job["media_id"])):
            job["status"] = "scheduled"
        self._expire(job)
        return dict(job)

    def _item_in_program(self, media_id: int) -> bool:
        try:
            return any(r["item_id"] == media_id
                       for r in self.store.program_since(1))
        except Exception:
            return False

    def _expire(self, job: dict) -> None:
        if job["status"] in ("queued", "rendering") and self._expired(job):
            job["status"] = "expired"
            job["failure_reason"] = "phrase TTL expired before it aired"

    def _expired(self, job: dict) -> bool:
        return (job.get("expires_at") or float("inf")) < time.time()

    # ----------------------------------------------------------------- render
    async def process_ready(self) -> None:
        """Render the next queued job (one per call). Non-blocking: a failed
        backend degrades the job, never the station (AGENTS rule 6)."""
        jobs = [j for j in self._jobs.values() if j["status"] == "queued"]
        if not jobs:
            return
        # oldest first
        jobs.sort(key=lambda j: j["created_at"])
        job = jobs[0]
        if self._expired(job):
            job["status"] = "expired"
            job["failure_reason"] = "phrase TTL expired before it aired"
            return
        job["status"] = "rendering"
        try:
            r = await self.voice.render_literal(
                job["cleaned_text"], job["voice"], job["speed"],
                out_dir=self.media_dir)
            item_id = self.store.add_item(
                type_="operator_phrase", media_path=str(r["path"]),
                duration_s=r["duration_s"], sample_rate=r["sample_rate"],
                channels=r["channels"], role="operator_phrase",
                evergreen=False, fresh=True,
                meta={"text": r["clean"], "character_id": self.cfg.dj.character.id,
                      "voice": job["voice"], "words": r["words"],
                      "words_per_s": r["words_per_s"]})
            job["media_id"] = item_id
            self._by_item[item_id] = job["job_id"]
            job["duration_s"] = r["duration_s"]
            job["status"] = "ready"
            log.info("phrase.ready", extra={"job_id": job["job_id"],
                                            "item_id": item_id,
                                            "duration_s": round(r["duration_s"], 2)})
            # hand to the scheduler run loop for legal boundary placement.
            if self.scheduler is not None:
                self.scheduler.pending_phrases.append(
                    {"item_id": item_id, "duration_s": r["duration_s"]})
        except Exception as e:
            job["status"] = "failed"
            job["failure_reason"] = err_text(e)
            log.warning("phrase.rendering_failed", extra={
                "job_id": job["job_id"], "error": job["failure_reason"]})

    # ----------------------------------------------------------------- run loop
    async def run(self) -> None:
        log.info("phrase manager running (character=%s voice=%s)",
                 self.cfg.dj.character.id, self.cfg.dj.character.voice or "?")
        while True:
            try:
                async with self._lock:
                    await self.process_ready()
            except Exception as e:
                log.exception("phrase cycle failed: %s", e)
            await asyncio.sleep(1.0)

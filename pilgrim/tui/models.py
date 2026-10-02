"""Typed models for the console (TUI.md §3).

Thin re-exports of the shared DJ contracts (`pilgrim.dj_contracts`) so the TUI
has one stable boundary and never imports `Station`, opens `station.db`, or
touches library paths (RADIO §3, TUI.md §3). `load_dj_token` is the single
token source (`PILGRIM_DJ_TOKEN`, env or .env) shared with the server.
"""
from __future__ import annotations

from pilgrim.dj_contracts import (
    DjSong,
    DjSongPage,
    DjState,
    ProgramResponse,
    StationState,
)
from pilgrim.dj_security import load_dj_token

__all__ = [
    "DjSong",
    "DjSongPage",
    "DjState",
    "ProgramResponse",
    "StationState",
    "load_dj_token",
]

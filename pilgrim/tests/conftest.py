from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent   # pilgrim package dir
APP = ROOT.parent                               # repo root
sys.path.insert(0, str(APP))

from pilgrim.config import Config, SimClock, ensure_dirs, load_config  # noqa: E402
from pilgrim.store import Store  # noqa: E402


@pytest.fixture
def base_config() -> Config:
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def cfg(base_config):
    return base_config


@pytest.fixture
def tmp_env(tmp_path, base_config):
    """Config pointed at a temp library/db plus a fresh Store."""
    cfg = base_config.model_copy(deep=True)
    cfg.library.dir = str(tmp_path / "library")
    cfg.library.db = str(tmp_path / "station.db")
    ensure_dirs(cfg)
    store = Store(tmp_path / "station.db")
    yield cfg, store, tmp_path


def make_item(cfg, store, type_, duration, genre=None, fresh=True,
              expires_at=None, gravity=None):
    return store.add_item(
        type_=type_, media_path=f"/tmp/{type_}_{store.max_seq()}.flac",
        duration_s=duration, genre=genre, fresh=fresh, evergreen=True,
        expires_at=expires_at, gravity=gravity)


def seed_pool(store, cfg, n_song=40, n_liner=30, n_com=20, n_dj=15):
    genres = list(cfg.songs.genres.keys())
    for i in range(n_song):
        make_item(cfg, store, "song", 120.0 + (i % 60), genre=genres[i % len(genres)])
    for i in range(n_liner):
        make_item(cfg, store, "liner", 3 + (i % 12))
    for i in range(n_com):
        make_item(cfg, store, "commercial", 20 + (i % 10))
    for i in range(n_dj):
        make_item(cfg, store, "dj_talk", 15 + (i % 5))

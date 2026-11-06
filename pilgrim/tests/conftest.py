from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent   # pilgrim package dir
APP = ROOT.parent                               # repo root
sys.path.insert(0, str(APP))

from pilgrim import db as pilgrim_db  # noqa: E402
from pilgrim.config import Config, ensure_dirs, load_config  # noqa: E402
from pilgrim.store import Store  # noqa: E402

# `make pg-test` (SCALABILITY.md Step 2): the SAME suite against a throwaway
# Postgres. Unset (the default, `make test`) every store is the SQLite file in
# the test's temp directory. Set, tmp_env and every Station open the Postgres
# database instead, reset to an empty schema before each test.
PG_TEST_URL = os.environ.get("PILGRIM_TEST_DATABASE_URL", "").strip()
PG_TEST_PASSWORD = os.environ.get("PILGRIM_TEST_DATABASE_PASSWORD", "")


def _reset_pg_schema() -> None:
    import psycopg
    pilgrim_db.close_all_postgres()
    with psycopg.connect(PG_TEST_URL, password=PG_TEST_PASSWORD or None,
                         autocommit=True, connect_timeout=10) as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")


def iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


@pytest.fixture(autouse=True)
def _no_repo_secrets(monkeypatch):
    """Tests never read the repo-root .env or an exported Stripe key (AGENTS §1.2).

    Without this, a developer's live `.env` would make `create_app` start with
    payments active and a real Stripe client inside the unit/e2e suite. Tests
    that need the fallback pass `env_file=` or point PILGRIM_ENV_FILE at a
    temp file themselves.
    """
    monkeypatch.setenv("PILGRIM_ENV_FILE", "")
    for name in ("STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY",
                 "STRIPE_WEBHOOK_SECRET", "PAID_OVERRIDE_TOKEN", "BRAVE_SEARCH_KEY",
                 "DATABASE_URL", "DATABASE_PASSWORD", "DB_PASS_SECRET_ARN", "REDIS_URL"):
        monkeypatch.delenv(name, raising=False)
    if PG_TEST_URL:
        _reset_pg_schema()
        monkeypatch.setenv("DATABASE_URL", PG_TEST_URL)
        if PG_TEST_PASSWORD:
            monkeypatch.setenv("DATABASE_PASSWORD", PG_TEST_PASSWORD)


@pytest.fixture
def base_config() -> Config:
    cfg = load_config(ROOT / "config.yaml")
    # Tests never upload to S3 (AGENTS §1.2): publisher tests inject a fake.
    cfg.media.publish = False
    return cfg


@pytest.fixture
def cfg(base_config):
    return base_config


@pytest.fixture
def tmp_env(tmp_path, base_config):
    """Config pointed at a temp library/db plus a fresh Store."""
    cfg = base_config.model_copy(deep=True)
    cfg.library.dir = str(tmp_path / "library")
    cfg.library.db = str(tmp_path / "station.db")
    # keep test runs out of pilgrim/logs/ so the live JSON log stays clean (5.3)
    cfg.logging.dir = str(tmp_path / "logs")
    ensure_dirs(cfg)
    store = pilgrim_db.open_store(cfg, path=tmp_path / "station.db")
    yield cfg, store, tmp_path
    store.close()


@pytest.fixture
def sqlite_env(tmp_path, base_config):
    """Like tmp_env but ALWAYS the SQLite file, whatever `make pg-test` says:
    for the tools that work on the station.db file itself (mp3_migrate,
    purge_songs, pg_migrate) and tests that need a second, empty store."""
    cfg = base_config.model_copy(deep=True)
    cfg.library.dir = str(tmp_path / "library")
    cfg.library.db = str(tmp_path / "station.db")
    cfg.logging.dir = str(tmp_path / "logs")
    ensure_dirs(cfg)
    store = Store(tmp_path / "station.db")
    yield cfg, store, tmp_path
    store.close()


def make_item(cfg, store, type_, duration, genre=None, fresh=True,
              expires_at=None, gravity=None, meta=None):
    return store.add_item(
        type_=type_, media_path=f"/tmp/{type_}_{store.max_seq()}.flac",
        duration_s=duration, genre=genre, fresh=fresh, evergreen=True,
        expires_at=expires_at, gravity=gravity, meta=meta)


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


def seed_aged_songs(cfg, store, n, epoch, age_h_start, age_h_end=0.0, fresh=False):
    """Seed `n` songs whose created_at is spread (linearly) from `age_h_start`
    hours ago to `age_h_end` hours ago, in the sim-clock wall frame `epoch`.
    id 0 is the OLDEST (created furthest in the past); the newest is the highest
    id. `fresh=False` marks them already-aired-but-with-no-ledger-history
    (last_aired_at NULL => wait 1.0), so weighting is purely age-driven. Returns
    the item ids in creation order."""
    genres = list(cfg.songs.genres.keys())
    ids = []
    span = max(n - 1, 1)
    for i in range(n):
        age_h = age_h_start + (age_h_end - age_h_start) * i / span
        created = iso_from_epoch(epoch - age_h * 3600)
        ids.append(store.add_item(
            type_="song", media_path=f"/aged_{i}.flac",
            duration_s=120.0, genre=genres[i % len(genres)],
            fresh=fresh, evergreen=True, created_at=created))
    return ids


# A valid MiniMax Music 3 structured caption (prompting guide §3): three
# headings in order, Genre / Subgenre + tempo, explicit vocal, arrangement
# timeline, explicit exclusion. ~437 words, inside the 250-450 target. Shared
# by brief-validation tests so every fake LLM returns a brief the pipeline
# would accept.
VALID_CAPTION = """\
Global Metadata
Basic Attributes: bpm is 118. key is C, and scale is minor. Jazz Pop / Cabaret Swing.
Global Emotional Progression: The piece opens with a mysterious, slightly
melancholy night-shift atmosphere and holds a wry, conspiratorial tone through
the verses. The pre-chorus tightens into mock-serious tension, the chorus
releases into theatrical indignation and a swaggering brass celebration, and the
bridge drops to a lone piano confession before the final chorus resolves with a
knowing wink.
Application Scenarios & Imagery: A neon arcade at closing time, a small-town
sheriff's office lit by one flickering fluorescent tube, and a slow drive past a
county fairground after the lights go out.
Sonics & Production Profile: A warm, wide live-room soundstage with the rhythm
section centered and the horn section spread across the stereo field. The
frequency balance favors a rich mid-range for the brass and piano, with a round
upright bass anchoring the low end. Dynamics stay natural and breathable, with
only gentle bus compression so the swing breathes rather than pumps.

Vocal Details
Vocal Gender & Timbre: Singer A (Male). A wry, slightly raspy baritone with a
conversational mid-range and a bright, nasal edge when he pushes.
Vocal Style: Spoken-sung and behind the beat in the verses, leaning into the
punchlines; a belted, theatrical delivery in the choruses with clipped
consonants; a soft, almost whispered confession in the bridge.
Harmony/Backing Vocals: Tight three-part backing harmonies in the choruses, plus
call-and-response shouts from the horn players on the post-chorus.
Vocal FX: A short plate reverb for club intimacy, a slap delay on the punchline
lines, and no pitch correction, keeping the performance human and a little rough.

Arrangement
Instrument Lifecycle (Primary/Secondary): Primary: A walking upright bass
carries the harmony from the first bar to the last, locking with the ride cymbal
to define the swing. Secondary: Piano comps with rhythmic stabs in the verses;
the muted trumpet enters on the pre-chorus with a sly counter-line; the full
brass section lands on the first chorus and returns bigger each time; brushes
give way to sticks in the final chorus.
Groove & Foundation Progression: A classic swing feel at 118 bpm, ride cymbal
pattern throughout, brushes and light kick in the verses, a harder backbeat and
walking bass pushes in the choruses, and the bridge strips to piano and bass
before the last chorus rebuilds. No steel guitar, no fiddle, no country twang.
Embellishments, Textures & Spatial FX: Brass swells and cymbal chokes mark the
section changes, a piano glissando leads into the final chorus, and the outro
lets the bass walk out alone under a long reverb tail."""


def valid_brief(genre="jazz-swing-big-band", **over):
    """A brief the pipeline accepts; override any field for negative tests."""
    brief = {"title": "The Cheese Got Cuffed",
             "artist": "Sergeant Biscuit & the County Jive Department",
             "genre": genre, "style_prompt": VALID_CAPTION,
             "lyrics": "[verse]\nMoon over the hen coop\n[chorus]\nOh the cheese got cuffed"}
    brief.update(over)
    return brief

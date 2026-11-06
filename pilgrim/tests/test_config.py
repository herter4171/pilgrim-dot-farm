"""Config loading for the new sections (IDLE.md Step 1, VOTING.md §2).

`test_trusted_proxies.py` covers only `Visitors`; this module is the general
config test surface (defaults, validation, YAML wiring).
"""
from __future__ import annotations

import pytest
from pilgrim.config import Config, Idle, Ratings, Voting, load_config
from pydantic import ValidationError


def test_idle_defaults():
    cfg = load_config()
    assert cfg.idle.enabled is True
    assert cfg.idle.after_s == 120
    assert cfg.idle.check_s == 5
    assert cfg.idle.producer_poll_s == 30


def test_voting_and_ratings_defaults():
    cfg = load_config()
    assert cfg.voting.enabled is True
    assert cfg.voting.max_voters_per_ip == 3
    assert cfg.voting.bury_threshold == -3
    assert cfg.ratings.enabled is True
    assert cfg.ratings.grace_s == 5
    assert cfg.ratings.rotation_weight is False


def test_idle_after_s_must_cover_listener_timeout():
    """IDLE.md Step 1: a healthy listener must not be declared absent before
    its in-memory entry expires."""
    with pytest.raises(ValidationError):
        Config(idle=Idle(after_s=10), visitors={"listener_timeout_s": 15,
                                                "trusted_proxies": ["127.0.0.1"]})


def test_idle_non_positive_intervals_rejected():
    with pytest.raises(ValidationError):
        Idle(check_s=0)
    with pytest.raises(ValidationError):
        Idle(producer_poll_s=0)
    with pytest.raises(ValidationError):
        Idle(after_s=-1)


def test_voting_limits_positive():
    with pytest.raises(ValidationError):
        Voting(per_client_per_10min=0)
    with pytest.raises(ValidationError):
        Voting(max_voters_per_ip=0)
    assert Voting(bury_threshold=None).bury_threshold is None


def test_ratings_limits():
    with pytest.raises(ValidationError):
        Ratings(grace_s=-1)
    with pytest.raises(ValidationError):
        Ratings(rotation_min_votes=0)
    assert Ratings(rotation_weight=True).rotation_weight is True


# --- config overlay (SCALABILITY.md Step 1) -------------------------------

def test_overlay_deep_merges_and_keeps_the_rest(tmp_path, monkeypatch):
    monkeypatch.delenv("PILGRIM_CONFIG_OVERLAY", raising=False)
    base = load_config()
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("station:\n  host: 0.0.0.0\n  port: 8080\nlibrary:\n  db: data/station.db\n")
    cfg = load_config(overlay=overlay)
    assert cfg.station.host == "0.0.0.0"
    assert cfg.station.port == 8080
    assert cfg.library.db == "data/station.db"
    # untouched siblings keep config.yaml's values
    assert cfg.station.admin_port == base.station.admin_port
    assert cfg.station.name == base.station.name
    assert cfg.library.dir == base.library.dir
    assert cfg.hosts == base.hosts


def test_overlay_from_env_and_empty_env_means_none(tmp_path, monkeypatch):
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("station:\n  port: 8081\n")
    monkeypatch.setenv("PILGRIM_CONFIG_OVERLAY", str(overlay))
    assert load_config().station.port == 8081
    monkeypatch.setenv("PILGRIM_CONFIG_OVERLAY", "")
    assert load_config().station.port == load_config(overlay=None).station.port != 8081


def test_overlay_cannot_add_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("PILGRIM_CONFIG_OVERLAY", raising=False)
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("station:\n  bogus: 1\n")
    with pytest.raises(ValueError, match="unknown keys: station.bogus"):
        load_config(overlay=overlay)
    overlay.write_text("nonsense:\n  x: 1\n")
    with pytest.raises(ValueError, match="unknown keys: nonsense"):
        load_config(overlay=overlay)


def test_overlay_must_be_a_mapping(tmp_path, monkeypatch):
    monkeypatch.delenv("PILGRIM_CONFIG_OVERLAY", raising=False)
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("- not\n- a\n- mapping\n")
    with pytest.raises(ValueError):
        load_config(overlay=overlay)


def test_shipped_container_overlay_loads():
    """pilgrim/deploy/container/overlay.yaml must always validate against the
    live schema, or the image starts and dies."""
    from pilgrim.config import ROOT
    cfg = load_config(overlay=ROOT / "deploy" / "container" / "overlay.yaml")
    assert cfg.station.host == "0.0.0.0"
    assert cfg.station.port == 8080
    assert cfg.station.admin_host == "127.0.0.1"
    assert cfg.station.serve_web is True
    assert cfg.library.dir.startswith("data/")
    assert cfg.library.db.startswith("data/")
    assert cfg.logging.dir.startswith("data/")

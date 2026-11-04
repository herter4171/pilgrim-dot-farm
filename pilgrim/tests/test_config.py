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

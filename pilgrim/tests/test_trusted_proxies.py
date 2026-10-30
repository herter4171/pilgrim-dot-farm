"""Trusted-proxy client-IP chain (SPLIT_PLAN §3, RADIO.md §12, §14).

Fakes only; TestClient is used without `with` so no lifespan tasks start.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pilgrim.config import Visitors
from pilgrim.server import build_station, create_public_app
from pilgrim.tests.fakes import FakeKokoro, FakeLLM
from pydantic import ValidationError


def _client(tmp_env, proxies: list[str], peer: tuple[str, int]):
    cfg, _, _ = tmp_env
    cfg.visitors = Visitors(trusted_proxies=proxies)
    station = build_station(cfg)
    station.llm = FakeLLM()  # type: ignore[assignment]
    station.kokoro = FakeKokoro()  # type: ignore[assignment]
    return TestClient(create_public_app(cfg, station), client=peer), station


def _stored_ips(station) -> list[str]:
    rows = station.db._conn.execute(
        "SELECT ip FROM visitor_ips ORDER BY ip").fetchall()
    return [r[0] for r in rows]


def test_trusted_cidr_uses_x_real_ip(tmp_env):
    """A peer inside a trusted network has its X-Real-IP used."""
    c, station = _client(tmp_env, ["10.0.0.0/8"], ("10.1.2.3", 1234))
    c.post("/api/visitors", headers={"X-Real-IP": "203.0.113.9"})
    assert _stored_ips(station) == ["203.0.113.9"]


def test_untrusted_peer_ignores_headers(tmp_env):
    """Outside every trusted entry: both headers are ignored."""
    c, station = _client(tmp_env, ["10.0.0.0/8"], ("192.0.2.5", 1234))
    c.post("/api/visitors", headers={"X-Real-IP": "203.0.113.9",
                                     "X-Forwarded-For": "198.51.100.7"})
    assert _stored_ips(station) == ["192.0.2.5"]


def test_trusted_peer_falls_back_to_first_xff(tmp_env):
    """Trusted peer without X-Real-IP: first X-Forwarded-For entry wins."""
    c, station = _client(tmp_env, ["10.0.0.0/8"], ("10.1.2.3", 1234))
    c.post("/api/visitors",
           headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"})
    assert _stored_ips(station) == ["203.0.113.9"]


def test_ipv4_mapped_peer_is_canonicalized(tmp_env):
    """An IPv4-mapped IPv6 peer is trusted by an IPv4 network."""
    c, station = _client(tmp_env, ["127.0.0.0/8"], ("::ffff:127.0.0.1", 1234))
    c.post("/api/visitors", headers={"X-Real-IP": "203.0.113.9"})
    assert _stored_ips(station) == ["203.0.113.9"]


def test_invalid_trusted_proxy_fails_config_load():
    with pytest.raises(ValidationError, match="not a valid"):
        Visitors(trusted_proxies=["not-an-ip"])

"""FLUX client + fake (ALBUM_ART.md Step 2; AGENTS §1.2 — fakes only)."""
from __future__ import annotations

import pytest
from pilgrim.pipelines.art import (
    FluxBusy,
    FluxClient,
    FluxMalformed,
    FluxTimeout,
    FluxUnavailable,
    parse_size,
    png_dimensions,
    validate_png,
)
from pilgrim.tests.fakes.fake_flux import FakeFlux, encode_png


def _cfg(cfg):
    cfg.art.size = "64x64"
    return cfg


def test_encode_png_is_a_real_png():
    png = encode_png(32, 16)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert png_dimensions(png) == (32, 16)
    assert validate_png(png, (32, 16)) == (32, 16)


def test_parse_size():
    assert parse_size("1024x1024") == (1024, 1024)
    with pytest.raises(ValueError):
        parse_size("big")


@pytest.mark.asyncio
async def test_generate_returns_png_and_sends_the_contract(cfg):
    c = _cfg(cfg)
    fake = FakeFlux(size=(64, 64))
    client = fake.client(c)
    png = await client.generate("a red barn", seed=1234)
    assert png_dimensions(png) == (64, 64)
    body = fake.calls[-1]
    assert body["response_format"] == "b64_json"   # never `url` (§0.2 rule 1)
    assert body["n"] == 1
    assert body["size"] == "64x64"
    assert body["steps"] == c.art.steps
    assert body["seed"] == 1234
    await client.close()


@pytest.mark.asyncio
async def test_memory_pressure_is_a_busy_backoff(cfg):
    fake = FakeFlux()
    fake.fail_mode = "memory_pressure"
    client = fake.client(_cfg(cfg))
    with pytest.raises(FluxBusy) as ei:
        await client.generate("x", seed=1)
    assert "memory_pressure" in str(ei.value) or "out of memory" in str(ei.value)
    await client.close()


@pytest.mark.asyncio
async def test_timeout(cfg):
    fake = FakeFlux()
    fake.fail_mode = "timeout"
    client = fake.client(_cfg(cfg))
    with pytest.raises(FluxTimeout):
        await client.generate("x", seed=1)
    await client.close()


@pytest.mark.asyncio
async def test_http_error_is_unavailable(cfg):
    fake = FakeFlux()
    fake.fail_mode = "http_error"
    client = fake.client(_cfg(cfg))
    with pytest.raises(FluxUnavailable):
        await client.generate("x", seed=1)
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["nesting", "not_png", "wrong_size"])
async def test_malformed_bodies_are_rejected(cfg, mode):
    fake = FakeFlux()
    fake.fail_mode = mode
    client = fake.client(_cfg(cfg))
    with pytest.raises(FluxMalformed):
        await client.generate("x", seed=1)
    await client.close()


@pytest.mark.asyncio
async def test_health_ok_and_down(cfg):
    fake = FakeFlux()
    client = fake.client(_cfg(cfg))
    assert await client.health() is True
    fake.health_up = False
    assert await client.health() is False
    await client.close()


@pytest.mark.asyncio
async def test_unreachable_server_is_unavailable(cfg):
    import httpx
    from pilgrim.config import load_config

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = FluxClient(load_config(), transport=httpx.MockTransport(refuse))
    with pytest.raises(FluxUnavailable):
        await client.generate("x", seed=1)
    assert await client.health() is False
    await client.close()


def test_art_config_defaults(cfg):
    a = cfg.art
    assert a.size == "1024x1024" and a.steps == 16
    assert a.min_free_gb == 20 and a.retry_after_s == 300
    assert a.key_prefix == "art/"
    assert cfg.models.art_scene == "qwen38"

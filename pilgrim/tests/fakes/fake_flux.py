"""In-memory stand-in for the FLUX 3 server (AGENTS §1.2, ALBUM_ART.md Step 2).

Wired in as an `httpx.MockTransport`, so the real `FluxClient` parsing and
validation run against it. Renders a tiny valid PNG of whatever size the
request asks for, after a configurable delay; `fail_mode` injects the
failures the real server can produce (docs/backends.md §10).
"""
from __future__ import annotations

import asyncio
import base64
import json
import struct
import zlib
from typing import Any

import httpx


def encode_png(width: int, height: int, *, color: tuple[int, int, int] = (60, 140, 60)) -> bytes:
    """Minimal 8-bit RGB PNG (no Pillow). One flat colour plus a cheap
    gradient so it is a real image, not a degenerate one."""
    r, g, b = color
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (None)
        shade = (y * 255) // max(1, height - 1)
        for x in range(width):
            raw += bytes(((r + shade) % 256, (g + x) % 256, b))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


class FakeFlux:
    """`fail_mode` values (None = healthy):

    * `memory_pressure` — 503 `{"detail": ..., "type": "memory_pressure"}`
    * `timeout`         — the request raises `httpx.ReadTimeout`
    * `nesting`         — 200 but the PNG is at `data[0]["b64_json"]`
                          (the single-nesting mistake the double nesting invites)
    * `not_png`         — 200 with base64 of non-PNG bytes
    * `wrong_size`      — 200 with a valid PNG of a different size
    * `http_error`      — 500
    """

    def __init__(self, size: tuple[int, int] = (64, 64), delay_s: float = 0.0) -> None:
        self.size = size                  # the size the fake actually renders
        self.delay_s = delay_s
        self.fail_mode: str | None = None
        self.calls: list[dict[str, Any]] = []
        self.health_calls = 0
        self.health_up = True
        self.closed = False

    # -- transport ---------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self, cfg):
        """A real FluxClient wired to this fake."""
        from pilgrim.pipelines.art import FluxClient
        return FluxClient(cfg, transport=self.transport())

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            self.health_calls += 1
            if not self.health_up:
                return httpx.Response(503, json={"detail": "down"})
            return httpx.Response(200, json={
                "status": "ok", "port": 8084, "model": "6bit", "loaded": True,
                "rss_mb": 9053.7, "physical_memory_gb": 68.7})
        if path != "/v1/images/generations":
            return httpx.Response(404, json={"detail": "no such path"})

        body = json.loads(request.content or b"{}")
        self.calls.append(body)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)

        mode = self.fail_mode
        if mode == "timeout":
            raise httpx.ReadTimeout("fake flux: simulated timeout", request=request)
        if mode == "memory_pressure":
            return httpx.Response(503, json={
                "detail": "server_busy_or_out_of_memory: out of memory pressure "
                          "(12.3 GB free of 69 GB RAM); refusing request",
                "type": "memory_pressure"})
        if mode == "http_error":
            return httpx.Response(500, json={"detail": "boom"})

        want = self.size
        if body.get("size"):
            try:
                w, h = (int(v) for v in str(body["size"]).lower().split("x", 1))
                want = (w, h)
            except ValueError:
                return httpx.Response(400, json={"detail": "bad size"})
        if mode == "wrong_size":
            want = (want[0] // 2 or 1, want[1] // 2 or 1)

        png = encode_png(*want)
        payload = base64.b64encode(png).decode("ascii")
        item: dict[str, Any] = {
            "created": 1793591772,
            "model": "flux3-6bit",
            "bfx_revised_prompt": body.get("prompt", ""),
            "bfx_record": {"prompt": body.get("prompt", ""),
                           "input_prompt": body.get("prompt", ""),
                           "width": want[0], "height": want[1],
                           "steps": body.get("steps"), "seed": body.get("seed"),
                           "upsampled": False, "references": 0, "boxes": None},
        }
        if mode == "nesting":
            item["b64_json"] = payload          # wrong: not inside data[]
        elif mode == "not_png":
            item["data"] = [{"b64_json": base64.b64encode(b"not a png").decode(),
                             "mime_type": "image/png"}]
        else:
            item["data"] = [{"b64_json": payload, "mime_type": "image/png"}]
        return httpx.Response(200, json={"object": "list", "data": [item]})

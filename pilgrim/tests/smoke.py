"""Real-backend smoke probe (AGENTS §6 `make smoke`).

Explicitly NOT part of the unit suite: contacts LiveLLM/Kokoro/mlx/searxng.
Only run deliberately. Verifies /health on every host and reports a summary.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pilgrim.config import load_config  # noqa: E402

_TIMEOUT = httpx.Timeout(8.0, connect=5.0)


def _ok(status: int) -> bool:
    return 200 <= status < 500


def _mcp_probe(endpoint: str, key: str) -> tuple[bool, str]:
    """Minimal MCP initialize probe: proves the endpoint + auth handshake."""
    try:
        # synchronous reimplementation of the handshake via httpx sync
        with httpx.Client(timeout=_TIMEOUT, headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **({"Authorization": f"Bearer {key}"} if key else {})}) as c:
            r = c.post(endpoint.rstrip("/"), json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "smoke", "version": "0"}}})
            return r.status_code == 200, f"http {r.status_code}"
    except Exception as e:  # noqa: BLE001
        return False, e.__class__.__name__


def probe() -> int:
    cfg = load_config()
    key = os.environ.get("LITELLM_TOKEN", "")
    failures = []
    with httpx.Client(timeout=_TIMEOUT) as c:
        checks = [
            ("litellm", cfg.hosts.litellm.rstrip("/") + "/models",
             {"Authorization": f"Bearer {key}"} if key else None),
            ("kokoro", cfg.hosts.kokoro.rstrip("/") + "/health", None),
            ("mlx_serve", cfg.hosts.mlx_serve.rstrip("/") + "/v1/models", None),
        ]
        for name, url, headers in checks:
            try:
                r = c.get(url, headers=headers or {})
                ok = _ok(r.status_code)
                print(f"{name:10s} {url:45s} {'OK' if ok else 'FAIL'} ({r.status_code})")
                if not ok:
                    failures.append(f"{name} http {r.status_code}")
            except Exception as e:  # noqa: BLE001 - probe reports any reachability failure
                print(f"{name:10s} {url:45s} FAIL ({e.__class__.__name__})")
                failures.append(f"{name} {e.__class__.__name__}")
    # searxng goes through the LiteLLM MCP endpoint (not a /health route)
    ok, why = _mcp_probe(cfg.hosts.searxng, key)
    print(f"{'searxng':10s} {cfg.hosts.searxng:45s} {'OK' if ok else 'FAIL'} ({why})")
    if not ok:
        failures.append(f"searxng {why}")
    if failures:
        print("\nSMOKE FAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nSMOKE OK: all backends reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(probe())

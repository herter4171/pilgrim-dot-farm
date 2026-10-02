"""Operator authentication for /api/admin/dj/* (TUI.md §4, RADIO.md §11).

A dedicated bearer token from $PILGRIM_DJ_TOKEN (env, or a line in the
repo-root .env), never `LITELLM_TOKEN`. Missing or wrong credentials disable DJ
mutations; the public request line is untouched. Grouped as a tiny pure check
so it is testable without ASGI machinery and reusable as a FastAPI dependency.

Token handling mirrors `config.load_api_key`'s env/.env fallback so
`python -m pilgrim.server` works without a shell export.
"""
from __future__ import annotations

import hmac
import os
from collections.abc import Mapping

from fastapi import HTTPException, Request


def _dj_error(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def load_dj_token(env_file: str | None = None) -> str:
    """PILGRIM_DJ_TOKEN from the environment, else a .env line (git-ignored)."""
    token = os.environ.get("PILGRIM_DJ_TOKEN", "").strip()
    if token:
        return token
    path = env_file or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    os.pardir, ".env")
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    for line in lines:
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and key.strip() == "PILGRIM_DJ_TOKEN":
            return value.strip().strip("'\"")
    return ""


def _get_header(headers: Mapping[str, str], name: str) -> str:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return ""


def check_dj_authorized(headers: Mapping[str, str]) -> None:
    """Raise HTTPException when the operator token is missing or wrong.

    - token not configured => 403 `dj_disabled` (mutations disabled until the
      access setup is complete; TUI.md §7)
    - missing/malformed/wrong bearer under a configured token => 401
    """
    expected = load_dj_token()
    if not expected:
        raise HTTPException(
            status_code=403,
            detail=_dj_error(
                "dj_disabled",
                "DJ controls are not configured: set PILGRIM_DJ_TOKEN"))
    auth = _get_header(headers, "Authorization")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail=_dj_error("dj_unauthorized", "missing bearer token"))
    token = auth[len("Bearer "):].strip()
    if not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=401,
            detail=_dj_error("dj_unauthorized", "invalid token"))


def require_dj(request: Request) -> None:
    """FastAPI dependency for every /api/admin/dj/* route: `Depends(require_dj)`."""
    check_dj_authorized(request.headers)

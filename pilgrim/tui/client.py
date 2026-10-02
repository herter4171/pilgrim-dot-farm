"""HTTP client for the DJ console (RADIO §11, TUI.md §3). Thin, typed, resilient.

All reads run in Textual async workers so a slow backend never freezes the
interface. One shared ``httpx.AsyncClient`` with explicit timeouts from config.
Every non-2xx response raises ``DjError`` with the server's stable ``code`` and
readable ``detail`` so the console can render "reconnecting / dj_disabled /
stale" states instead of a raw traceback.
"""
from __future__ import annotations

import httpx

from pilgrim.config import Config
from pilgrim.dj_contracts import DjSongPage, DjState, StationState
from pilgrim.tui.models import load_dj_token


class DjError(Exception):
    """A transport failure or a non-2xx from the station."""
    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(f"{status} {code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail


class DjClient:
    def __init__(self, cfg: Config, base_url: str | None = None) -> None:
        host = cfg.station.host
        if host in ("0.0.0.0", "::"):  # bind address is not a usable client URL
            host = "127.0.0.1"
        self.base_url = (base_url or f"http://{host}:{cfg.station.port}").rstrip("/")
        token = load_dj_token()
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._timeout = cfg.dj.request_timeout_s
        self._h = httpx.AsyncClient(base_url=self.base_url, timeout=self._timeout,
                                    headers=self._headers)

    async def close(self) -> None:
        await self._h.aclose()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            r = await self._h.get(path, params=params)
        except httpx.HTTPError as e:
            raise DjError(0, "transport", f"{type(e).__name__}: {e}") from e
        if r.status_code >= 400:
            try:
                body = r.json()
                detail = body.get("detail", "error")
                if isinstance(detail, dict):
                    code = str(detail.get("code", "error"))
                    detail = str(detail.get("detail", detail))
                else:
                    code = "error"
            except Exception:
                code, detail = "error", r.text[:200]
            raise DjError(r.status_code, code, detail)
        return r.json()

    # ------------------------------------------------------------------ public
    async def station_state(self) -> StationState:
        return StationState.model_validate(await self._get("/api/station/state"))

    async def state(self) -> DjState:
        return DjState.model_validate(await self._get("/api/admin/dj/state"))

    # ------------------------------------------------------------------ admind
    async def songs(self, q: str | None = None, sort: str = "title",
                    direction: str = "asc", cursor: str | None = None,
                    limit: int = 500) -> DjSongPage:
        params: dict[str, object] = {"sort": sort, "direction": direction,
                                     "limit": limit}
        if q:
            params["q"] = q
        if cursor:
            params["cursor"] = cursor
        return DjSongPage.model_validate(await self._get("/api/admin/dj/songs",
                                                         params))

    async def songs_all(self, q: str | None = None, sort: str = "title",
                        direction: str = "asc") -> list:
        """Fetch every page of the catalogue (bounded by the server cursor)."""
        items: list = []
        cursor = None
        while True:
            page = await self.songs(q=q, sort=sort, direction=direction,
                                    cursor=cursor)
            items.extend(page.items)
            cursor = page.next_cursor
            if not cursor:
                return items
            if len(items) > 20000:  # safety: never loop forever
                return items

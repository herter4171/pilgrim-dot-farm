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
from pilgrim.dj_contracts import (
    CharacterInfo,
    DjSongPage,
    DjState,
    PhraseJob,
    PhraseSubmitResponse,
    StationState,
    ValidateResponse,
)
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
        # The console talks to the ADMIN listener (SPLIT_PLAN §1.4): the public
        # listener deliberately has no /api/admin/* routes.
        host = cfg.station.admin_host
        if host in ("0.0.0.0", "::"):  # bind address is not a usable client URL
            host = "127.0.0.1"
        self.base_url = (base_url or f"http://{host}:{cfg.station.admin_port}").rstrip("/")
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
        return self._decode(r)

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            r = await self._h.post(path, json=payload)
        except httpx.HTTPError as e:
            raise DjError(0, "transport", f"{type(e).__name__}: {e}") from e
        return self._decode(r)

    @staticmethod
    def _decode(r: httpx.Response) -> dict:
        """Return the JSON body, or raise DjError with the server's stable code
        and readable detail (TUI.md §3) instead of a raw traceback."""
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

    # --------------------------------------------------------- character/phrase
    async def character(self) -> CharacterInfo:
        """Configured operator character + unused voices (TUI.md §6)."""
        return CharacterInfo.model_validate(
            await self._get("/api/admin/dj/character"))

    async def validate_phrase(self, character_id: str, text: str) -> ValidateResponse:
        """Pure cleaned-text validation; no synthesis or scheduling."""
        return ValidateResponse.model_validate(await self._post(
            "/api/admin/dj/phrases/validate",
            {"character_id": character_id, "text": text}))

    async def submit_phrase(self, command_id: str, character_id: str, text: str,
                            cleaned_text_hash: str) -> PhraseSubmitResponse:
        """Confirm the cleaned text by hash and start the render job (202)."""
        return PhraseSubmitResponse.model_validate(await self._post(
            "/api/admin/dj/phrases",
            {"command_id": command_id, "character_id": character_id,
             "text": text, "cleaned_text_hash": cleaned_text_hash}))

    async def phrase_job(self, job_id: int) -> PhraseJob:
        """Render stage and finished clip for one phrase job."""
        return PhraseJob.model_validate(
            await self._get(f"/api/admin/dj/phrases/{job_id}"))

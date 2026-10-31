"""TUI tests (TUI.md §8; RADIO §15): Textual headless `App.run_test()` + Pilot
with a fake HTTP transport — no network, no server. Milestone 2 (read-only
console): rendering, client+server sort rules, keyboard + header sorting,
search/focus, Unicode + duplicate/missing names, reconnect/stale state,
repeated actions, small terminals.
"""
from __future__ import annotations

import asyncio

import pytest
from pilgrim.dj_contracts import (
    CharacterInfo,
    DjCurrentItem,
    DjSong,
    DjState,
    DjUpcomingItem,
    PhraseJob,
    PhraseSubmitResponse,
    ValidateResponse,
)
from pilgrim.tui.app import DjConsole
from pilgrim.tui.client import DjError
from textual.widgets import Button, DataTable, Input, Static


class FakeDjClient:
    """Deterministic stand-in for the DJ backend (no real HTTP)."""

    def __init__(self) -> None:
        self.songs: list[DjSong] = []
        self.cat_rev = 1
        self.fail_first_state = 0  # raise transport for the first N state calls
        self.state_calls = 0
        self.songs_calls = 0
        self.rev_refresh_calls = 0
        self.last_q: str | None = None
        self.last_sort: str | None = None
        self.last_direction: str | None = None
        # character + phrase (TUI.md §6)
        self.character_info = CharacterInfo(
            character_id="dj_operator", name="Jimmany", setup_complete=True,
            voice="am_justin", available_voices=["am_adam"])
        self.fail_character = False
        self.validated: list[str] = []
        self.submitted: list[dict] = []
        self.jobs: dict[int, PhraseJob] = {}
        self.job_statuses: list[str] = ["rendering", "aired"]
        self._next_job = 1

    def set_songs(self, songs: list[tuple[str, str]]) -> None:
        self.songs = [DjSong(id=i, title=t, artist=a, duration_s=90.0 + i)
                      for i, (t, a) in enumerate(songs, start=1)]

    async def close(self) -> None:
        pass

    async def songs_all(self, q: str | None = None, sort: str = "title",
                        direction: str = "asc") -> list[DjSong]:
        self.songs_calls += 1
        self.last_q, self.last_sort, self.last_direction = q, sort, direction
        items = list(self.songs)
        if q:
            fq = q.casefold()
            items = [s for s in items if fq in (s.title or "").casefold()
                     or fq in (s.artist or "").casefold()]

        def key(s: DjSong) -> tuple[str, str, int]:
            prim = ((s.title or "") if sort == "title" else (s.artist or "")).casefold()
            sec = ((s.artist or "") if sort == "title" else (s.title or "")).casefold()
            return (prim, sec, s.id)

        return sorted(items, key=key, reverse=(direction == "desc"))

    async def state(self) -> DjState:
        self.state_calls += 1
        if self.state_calls <= self.fail_first_state:
            raise DjError(0, "transport", "simulated outage")
        return DjState(
            epoch="e1", revision=1, server_time=1.0, on_air=True,
            current=DjCurrentItem(seq=5, media_id=9, type="song", title="Alpha",
                                  artist="One", position_s=1.0, remaining_s=9.0),
            upcoming=[DjUpcomingItem(seq=6, media_id=10, type="liner", title="",
                                     artist="", duration_s=4.0)],
            pending_commands=[], catalogue_revision=self.cat_rev,
            player_status="unknown")

    # ------------------------------------------------------- character/phrase
    async def character(self) -> CharacterInfo:
        if self.fail_character:
            raise DjError(0, "transport", "simulated outage")
        return self.character_info

    async def validate_phrase(self, character_id: str,
                              text: str) -> ValidateResponse:
        self.validated.append(text)
        if "http" in text:
            return ValidateResponse(character_id=character_id, cleaned_text=text,
                                    ok=False, rejected_reason="url")
        return ValidateResponse(character_id=character_id, cleaned_text=text,
                                ok=True, text_hash="h:" + text)

    async def submit_phrase(self, command_id: str, character_id: str, text: str,
                            cleaned_text_hash: str) -> PhraseSubmitResponse:
        self.submitted.append({"command_id": command_id,
                               "character_id": character_id, "text": text,
                               "cleaned_text_hash": cleaned_text_hash})
        jid = self._next_job
        self._next_job += 1
        self.jobs[jid] = PhraseJob(
            job_id=jid, status="queued", character_id=character_id,
            original_text=text, cleaned_text=text, voice="am_justin")
        return PhraseSubmitResponse(command_id=command_id, job_id=jid,
                                    status="queued", cleaned_text=text)

    async def phrase_job(self, job_id: int) -> PhraseJob:
        job = self.jobs[job_id]
        status = self.job_statuses.pop(0) if self.job_statuses else "aired"
        job = job.model_copy(update={"status": status})
        self.jobs[job_id] = job
        return job


async def _wait(pilot, predicate, tries: int = 200) -> None:
    """Spin the event loop until `predicate()` is true or we time out."""
    for _ in range(tries):
        if predicate():
            return
        await pilot.pause()
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


def _rows(app) -> list[str]:
    """Displayed Name column values in display order (col 0)."""
    cat: DataTable = app.query_one("#catalogue", DataTable)
    out = []
    for rk in list(cat.rows):  # dict order == display order (we rebuild each render)
        values = cat.get_row(rk)  # list of cell values in column order
        out.append(str(values[0]))
    return out


def _text(app, wid: str) -> str:
    return str(app.query_one(wid, Static).content)


@pytest.fixture
def fake(tmp_path):
    client = FakeDjClient()
    from pilgrim.config import load_config
    cfg = load_config()
    cfg.library.db = str(tmp_path / "x.db")  # kept unused (TUI never opens it)
    return client, cfg


@pytest.mark.asyncio
async def test_renders_catalogue(fake):
    client, cfg = fake
    client.set_songs([("Amber Fields", "Barn Choir"), ("Night Tractor", "Furrows")])
    app = DjConsole(cfg=cfg, client=client)
    async with app.run_test() as pilot:
        await _wait(pilot, lambda: app.query_one("#catalogue", DataTable).row_count == 2)
        assert _rows(app) == ["Amber Fields", "Night Tractor"]
        assert "2 songs" in _text(app, "#catinfo")
        # on-air + upcoming rendered from the state poll
        await _wait(pilot, lambda: "ON AIR" in _text(app, "#onair"))
        assert "Alpha — One" in _text(app, "#onair")
        assert app.query_one("#upcoming", DataTable).row_count == 1


@pytest.mark.asyncio
async def test_sort_default_is_name_asc_case_insensitive(fake):
    client, cfg = fake
    client.set_songs([("Zebra", "B"), ("apple", "C"), ("Banana", "A")])
    app = DjConsole(cfg=cfg, client=client)
    async with app.run_test() as pilot:
        await _wait(pilot, lambda: app.query_one("#catalogue", DataTable).row_count == 3)
        # default sort = title asc, case-insensitive, ties by artist then id
        assert _rows(app) == ["apple", "Banana", "Zebra"]
        assert client.last_sort == "title" and client.last_direction == "asc"


@pytest.mark.asyncio
async def test_keyboard_sort_toggles(fake):
    client, cfg = fake
    client.set_songs([("a2", "Zulu"), ("a1", "Alpha"), ("a3", "Mango")])
    app = DjConsole(cfg=cfg, client=client)
    async with app.run_test() as pilot:
        await _wait(pilot, lambda: app.query_one("#catalogue", DataTable).row_count == 3)
        # letters sort only when the search box does NOT have focus (TUI §2):
        # focus the table first so n/a hit the bindings, not the search field
        app.query_one("#catalogue", DataTable).focus()
        await pilot.press("n")  # title asc -> title desc
        await _wait(pilot, lambda: client.last_direction == "desc")
        assert _rows(app) == ["a3", "a2", "a1"]
        await pilot.press("a")  # artist asc
        await _wait(pilot, lambda: client.last_sort == "artist" and client.last_direction == "asc")
        assert _rows(app) == ["a1", "a3", "a2"]
        await pilot.press("a")  # artist desc
        await _wait(pilot, lambda: client.last_direction == "desc")
        assert _rows(app) == ["a2", "a3", "a1"]


@pytest.mark.asyncio
async def test_header_click_sorts(fake):
    client, cfg = fake
    client.set_songs([("bx", "Z"), ("ay", "A")])
    app = DjConsole(cfg=cfg, client=client)
    async with app.run_test() as pilot:
        await _wait(pilot, lambda: app.query_one("#catalogue", DataTable).row_count == 2)
        cat = app.query_one("#catalogue", DataTable)
        ck = list(cat.columns)[1]  # Artist column
        cat.post_message(DataTable.HeaderSelected(cat, ck, 1, cat.columns[ck].label))
        await pilot.pause()
        assert client.last_sort == "artist" and client.last_direction == "asc"
        assert _rows(app) == ["ay", "bx"]


@pytest.mark.asyncio
async def test_missing_names_and_unicode_and_duplicates(fake):
    client, cfg = fake
    # artist None -> DjSong default "" (missing); Unicode title; duplicate titles
    client.songs = [
        DjSong(id=1, title="Ünïcode — 名", artist="Àmber", duration_s=10),
        DjSong(id=2, title="Same Title", artist="Zulu", duration_s=20),
        DjSong(id=3, title="Same Title", artist="Aster", duration_s=30),
        DjSong(id=4, title="Missing Artist", artist=None, duration_s=40),
    ]
    app = DjConsole(cfg=cfg, client=client)
    async with app.run_test() as pilot:
        await _wait(pilot, lambda: app.query_one("#catalogue", DataTable).row_count == 4)
        # title-ascending, case-insensitive; duplicates tie-break by artist:
        # Missing Artist < Same Title < Same Title < Ünïcode — 名
        cat = app.query_one("#catalogue", DataTable)
        samples: list[tuple[str, str]] = []
        for rk in list(cat.rows):
            values = cat.get_row(rk)  # [title, artist, dur]
            samples.append((str(values[0]), str(values[1])))
        assert samples == [
            ("Missing Artist", "—"),   # artist None -> display placeholder
            ("Same Title", "Aster"),   # duplicate title, artist-asc tie-break
            ("Same Title", "Zulu"),
            ("Ünïcode — 名", "Àmber"),
        ]


@pytest.mark.asyncio
async def test_search_filters_and_letters_stay_in_input(fake):
    client, cfg = fake
    client.set_songs([("Cornfield Polka", "Barn"), ("Dreamcatcher", "Nettle"),
                      ("Cowboy Corn", "Field")])
    app = DjConsole(cfg=cfg, client=client)
    async with app.run_test() as pilot:
        await _wait(pilot, lambda: app.query_one("#catalogue", DataTable).row_count == 3)
        box: Input = app.query_one("#search", Input)
        box.focus()
        await pilot.press(*"corn")
        await _wait(pilot, lambda: (client.last_q or "").casefold() == "corn")
        # sorting still the default; "n" typed into the search is NOT a sort key
        await pilot.press("n")
        await _wait(pilot, lambda: (client.last_q or "").casefold() == "cornn")
        assert app.sort_field == "title" and app.sort_direction == "asc"
        await _wait(pilot, lambda: "matches" in _text(app, "#catinfo")
                    or app.query_one("#catalogue", DataTable).row_count == 0)


@pytest.mark.asyncio
async def test_reconnect_and_stale_revision_triggers_refresh(fake):
    client, cfg = fake
    client.set_songs([("Only", "One")])
    client.fail_first_state = 1  # one outage, then recovery
    app = DjConsole(cfg=cfg, client=client)
    async with app.run_test() as pilot:
        await _wait(pilot, lambda: "reconnecting" in str(
            _text(app, "#status")))
        # recovers -> CONNECTED
        await _wait(pilot, lambda: "CONNECTED" in str(
            _text(app, "#status")))
        before = client.songs_calls
        client.cat_rev = 42  # catalogue changed on the backend
        await _wait(pilot, lambda: client.songs_calls > before)  # auto refetch
        # table still correct and selection/state preserved
        assert app.query_one("#catalogue", DataTable).row_count == 1


@pytest.mark.asyncio
async def test_repeated_refresh_and_enter_are_safe(fake):
    client, cfg = fake
    client.set_songs([("a", "A"), ("b", "B")] * 5)
    app = DjConsole(cfg=cfg, client=client)
    async with app.run_test() as pilot:
        await _wait(pilot, lambda: app.query_one("#catalogue", DataTable).row_count == 10)
        for _ in range(4):
            await pilot.press("r")
            await pilot.pause()
        assert app.query_one("#catalogue", DataTable).row_count == 10
        # selective enter on a row -> read-only queue message, no crash
        cat = app.query_one("#catalogue", DataTable)
        cat.focus()
        await pilot.press("enter")
        assert "not enabled" in _text(app, "#status")


@pytest.mark.asyncio
async def test_small_terminal_renders(fake):
    client, cfg = fake
    client.set_songs([("Amber Fields", "Barn Choir")])
    app = DjConsole(cfg=cfg, client=client)
    async with app.run_test(size=(60, 20)) as pilot:
        await _wait(pilot, lambda: app.query_one("#catalogue", DataTable).row_count == 1)
        assert _rows(app) == ["Amber Fields"]


# ------------------------------------------------------- administrator phrases
@pytest.mark.asyncio
async def test_phrase_bar_shows_character_and_voice(fake):
    client, cfg = fake
    app = DjConsole(cfg=cfg, client=client)
    async with app.run_test() as pilot:
        await _wait(pilot, lambda: "Jimmany" in _text(app, "#charbar"))
        bar = _text(app, "#charbar")
        assert "am_justin" in bar          # the reserved administrator voice
        assert "1 unused voices" in bar
        assert app.query_one("#send", Button).disabled is False


@pytest.mark.asyncio
async def test_character_unavailable_is_reported(fake):
    client, cfg = fake
    client.fail_character = True
    app = DjConsole(cfg=cfg, client=client)
    async with app.run_test() as pilot:
        await _wait(pilot, lambda: "unavailable" in _text(app, "#charbar"))
        assert app.character is None


@pytest.mark.asyncio
async def test_phrase_validate_preview_and_send(fake):
    client, cfg = fake
    app = DjConsole(cfg=cfg, client=client)
    async with app.run_test() as pilot:
        await _wait(pilot, lambda: "Jimmany" in _text(app, "#charbar"))
        app.query_one("#phrase", Input).focus()
        await pilot.press(*"Hello")
        # live preview of the exact cleaned text that will air
        await _wait(pilot, lambda: "Will say: Hello" in _text(app, "#willsay"))
        await pilot.press("enter")  # Enter submits from the phrase field
        await _wait(pilot, lambda: bool(client.submitted), tries=400)
        assert client.submitted[0]["text"] == "Hello"
        assert client.submitted[0]["cleaned_text_hash"] == "h:Hello"
        assert client.submitted[0]["character_id"] == "dj_operator"
        # job poll reaches the terminal state
        await _wait(pilot, lambda: "AIRED" in _text(app, "#phrasestatus"), tries=400)


@pytest.mark.asyncio
async def test_phrase_rejected_never_submits(fake):
    client, cfg = fake
    app = DjConsole(cfg=cfg, client=client)
    async with app.run_test() as pilot:
        await _wait(pilot, lambda: "Jimmany" in _text(app, "#charbar"))
        app.query_one("#phrase", Input).focus()
        await pilot.press(*"http")
        await _wait(pilot, lambda: "rejected" in _text(app, "#willsay"))
        await pilot.press("enter")
        await pilot.pause()
        assert client.submitted == []
        assert "rejected" in _text(app, "#phrasestatus")


@pytest.mark.asyncio
async def test_letters_in_phrase_input_do_not_sort(fake):
    client, cfg = fake
    client.set_songs([("a", "A"), ("b", "B")])
    app = DjConsole(cfg=cfg, client=client)
    async with app.run_test() as pilot:
        await _wait(pilot, lambda: app.query_one("#catalogue", DataTable).row_count == 2)
        app.query_one("#phrase", Input).focus()
        await pilot.press(*"n")
        await pilot.pause()
        assert app.sort_field == "title" and app.sort_direction == "asc"
        assert app.query_one("#phrase", Input).value == "n"

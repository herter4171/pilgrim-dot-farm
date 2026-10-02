"""Pilgrim DJ — Textual operator console (TUI.md §2–3).

Wide-terminal layout per TUI.md §2:
    PILGRIM DJ                    CONNECTED · updated HH:MM:SS
    ON AIR   Song — Artist        01:12 / 03:24        [Skip: not yet]
    SONGS                          UPCOMING
      Search [         ]            #    Type      Name
      Sort: Name ↑ · 42 songs       ...  liner     ...
    [Enter = queue next: not yet]   ...  song      ...

Read-only in this milestone: it polls `/api/admin/dj/state` and browses/sorts/
searches `/api/admin/dj/songs`. Skip/queue/phrase are bound but report "not
yet available" until the playback-control and character milestones land.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Header, Input, Label, Static

from pilgrim.config import Config, load_config
from pilgrim.dj_contracts import DjSong
from pilgrim.tui.client import DjClient, DjError

NAME_COL = "Name"
ARTIST_COL = "Artist"
DUR_COL = "Dur"

_COL_FIELD = {NAME_COL: "title", ARTIST_COL: "artist"}


def _now_label() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


def _dur_label(sec: float | None) -> str:
    if sec is None or sec < 0:
        return "--:--"
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


def _song_key(s: DjSong, field: str) -> str:
    return (s.title or "").casefold() if field == "title" else (s.artist or "").casefold()


class DjConsole(App):
    """Read-only operator console for the DJ backend (TUI.md §2)."""

    CSS_PATH = "console.tcss"
    TITLE = "PILGRIM DJ"
    SUB_TITLE = "Pilgrim Dot Farm"

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("n", "sort_name", "Sort name"),
        Binding("a", "sort_artist", "Sort artist"),
        Binding("r", "refresh", "Refresh"),
        Binding("ctrl+k", "skip", "Skip current"),
        Binding("ctrl+p", "phrase", "Phrase"),
    ]
    # Enter = queue next is handled by the catalogue itself (RowSelected), so it
    # never fires while typing in the search field (TUI.md §2 focus rule).

    def __init__(self, cfg: Config | None = None, client: DjClient | None = None) -> None:
        super().__init__()
        self.cfg = cfg or load_config()
        self.client = client or DjClient(self.cfg)
        self._search_timer: Timer | None = None
        # preserved across refresh (TUI.md §2): search, sort, selection
        self.query_text = ""
        self.sort_field = "title"
        self.sort_direction = "asc"
        self.selected_id: str | None = None
        self._seen_rev: int | None = None
        self._cat_keys: dict[str, object] = {}  # column label -> ColumnKey
        self._cat_idx: dict[str, int] = {}      # item id -> current row index

    # ------------------------------------------------------------- lifecycle
    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("starting…", id="status", classes="bar")
        with Horizontal(id="mid"):
            with Vertical(id="left"):
                yield Static("", id="onair", classes="bar")
                yield Label("ON AIR", classes="hidden")
                yield Label("SONGS", classes="section")
                yield Input(placeholder="Search name or artist…", id="search")
                yield Static("", id="catinfo")
                yield DataTable(id="catalogue", cursor_type="row", zebra_stripes=True)
            with Vertical(id="right"):
                yield Label("UPCOMING", classes="section")
                yield DataTable(id="upcoming", cursor_type="row", zebra_stripes=True)
        yield Static("", id="charbar", classes="bar")
        yield Footer()

    def on_mount(self) -> None:
        cat = self.query_one("#catalogue", DataTable)
        for label, key in zip((NAME_COL, ARTIST_COL, DUR_COL),
                              cat.add_columns(NAME_COL, ARTIST_COL, DUR_COL),
                              strict=True):
            self._cat_keys[label] = key
        self.query_one("#upcoming", DataTable).add_columns("#", "Type", "Name")
        self.query_one("#charbar", Static).update(
            "CHARACTER: pending setup   ·   VOICE: —   ·   "
            "phrases arrive with character setup")
        self._render_catalogue([])
        self.poll_loop()
        self.refresh_catalogue()

    def on_unmount(self) -> None:
        with suppress(Exception):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # app ran on its own loop; loop already stopped
                asyncio.run(self.client.close())
                return
            loop.create_task(self.client.close())

    # ----------------------------------------------------------------- status
    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def _mark_connected(self) -> None:
        pending = ""
        self._set_status(f"CONNECTED   ·   updated {_now_label()}{pending}")

    # ---------------------------------------------------------------- workers
    @work(exclusive=True, group="poll")
    async def poll_loop(self) -> None:
        delay = max(0.2, self.cfg.dj.state_poll_s)
        while True:
            try:
                st = await self.client.state()
                self._render_state(st)
                if (self._seen_rev is not None
                        and st.catalogue_revision != self._seen_rev):
                    self.refresh_catalogue()
                self._seen_rev = st.catalogue_revision
                delay = max(0.2, self.cfg.dj.state_poll_s)
            except DjError as e:
                self._set_status(f"reconnecting — {e.code}")
                delay = min(delay * 2, max(0.2, self.cfg.dj.backoff_max_s))
            except Exception:
                self._set_status("reconnecting…")
                delay = min(delay * 2, max(0.2, self.cfg.dj.backoff_max_s))
            await asyncio.sleep(delay)

    @work(exclusive=True, group="catalogue")
    async def load_catalogue(self, query: str, sort: str, direction: str) -> None:
        try:
            songs = await self.client.songs_all(q=query or None, sort=sort,
                                                direction=direction)
        except DjError as e:
            self._set_status(f"catalogue unavailable — {e.code}")
            return
        except Exception:
            self._set_status("catalogue unavailable")
            return
        self._render_catalogue(self._sorted(songs))

    def refresh_catalogue(self) -> None:
        self.load_catalogue(self.query_text, self.sort_field, self.sort_direction)

    def _schedule_search(self) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()
        self._search_timer = self.set_timer(0.3, self.refresh_catalogue)

    # ------------------------------------------------------------------- sort
    def _sorted(self, songs: Iterable[DjSong]) -> list[DjSong]:
        """Case-insensitive order with the server's tie-break: primary column,
        then the other column, then id (TUI.md §2)."""
        field = self.sort_field
        other = "artist" if field == "title" else "title"

        def key(s: DjSong) -> tuple[str, str, int]:
            return (_song_key(s, field), _song_key(s, other), s.id)

        return sorted(songs, key=key, reverse=(self.sort_direction == "desc"))

    def _toggle_sort(self, field: str) -> None:
        if self.sort_field == field:
            self.sort_direction = "desc" if self.sort_direction == "asc" else "asc"
        else:
            self.sort_field = field
            self.sort_direction = "asc"
        self.refresh_catalogue()

    def _sort_label(self) -> str:
        arrow = "↑" if self.sort_direction == "asc" else "↓"
        field = "Name" if self.sort_field == "title" else "Artist"
        return f"Sort: {field} {arrow}"

    # ----------------------------------------------------------------- state
    def _render_state(self, st) -> None:
        cur = st.current
        if cur is not None:
            name = (f"{cur.title} — {cur.artist}" if cur.title and cur.artist
                    else (cur.title or cur.artist or cur.type))
            self.query_one("#onair", Static).update(
                f"ON AIR   {name}   {_dur_label(cur.position_s)} / "
                f"{_dur_label(cur.position_s + cur.remaining_s)}")
        else:
            self.query_one("#onair", Static).update(
                "ON AIR   (off air)" if not st.on_air else "ON AIR   (starting…)")
        up = self.query_one("#upcoming", DataTable)
        up.clear()
        for row in st.upcoming:
            up.add_row(str(row.seq), row.type, row.title or row.artist or row.type,
                       key=str(row.seq))
        pending = len(st.pending_commands or [])
        self._set_status(f"CONNECTED   ·   updated {_now_label()}"
                         + (f"   ·   {pending} pending command(s)" if pending else ""))

    # ------------------------------------------------------------- catalogue
    def _catinfo(self, count: int) -> str:
        if not self.query_text and count == 0:
            return "no songs in catalogue"
        if self.query_text and count == 0:
            return f"no songs match “{self.query_text}”"
        return f"{self._sort_label()} · {count} song{'s' if count != 1 else ''}" \
               + (f" · filter “{self.query_text}”" if self.query_text else "")

    def _render_catalogue(self, songs: list[DjSong]) -> None:
        cat = self.query_one("#catalogue", DataTable)
        sel = self.selected_id
        cat.clear()
        self._cat_idx = {}
        for i, s in enumerate(songs):
            cat.add_row(s.title or "—", s.artist or "—", _dur_label(s.duration_s),
                        key=str(s.id))
            self._cat_idx[str(s.id)] = i
            if str(s.id) == sel:
                cat.move_cursor(row=i)
        self.query_one("#catinfo", Static).update(self._catinfo(len(songs)))

    # -------------------------------------------------------------- controls
    def action_skip(self) -> None:
        self._set_status("skip not enabled — read-only console (playback control next)")

    def action_phrase(self) -> None:
        self._set_status("phrases not enabled — character setup next")

    def action_sort_name(self) -> None:
        if self.focused is not self.query_one("#search", Input):
            self._toggle_sort("title")

    def action_sort_artist(self) -> None:
        if self.focused is not self.query_one("#search", Input):
            self._toggle_sort("artist")

    def action_refresh(self) -> None:
        self.refresh_catalogue()

    # ------------------------------------------------------------- handlers
    @on(Input.Changed, "#search")
    def _on_search(self, event: Input.Changed) -> None:
        self.query_text = (event.value or "").strip()
        self._schedule_search()

    @on(DataTable.HeaderSelected, "#catalogue")
    def _on_header(self, event: DataTable.HeaderSelected) -> None:
        field = _COL_FIELD.get(str(event.label))
        if field is not None:
            self._toggle_sort(field)

    @on(DataTable.RowHighlighted, "#catalogue")
    def _on_cursor(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None:
            self.selected_id = str(event.row_key.value)

    @on(DataTable.RowSelected, "#catalogue")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        rid = str(event.row_key.value)
        self.selected_id = rid
        self._set_status(f"queue #{rid} — not enabled (read-only console, "
                         "playback control next)")


if __name__ == "__main__":
    DjConsole().run()

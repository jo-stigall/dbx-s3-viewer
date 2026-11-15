"""The Textual browser.

The app has two kinds of view:

- Home (`location is None`): bookmarks, then the external locations.
- A folder (`location` is an s3:// path): the objects in that folder.

The app opens at the start folder if one is saved (`*` sets or clears it),
else at home. `~` goes home from anywhere.

All backend calls run in daemon threads, so the UI never waits on the
network and `q` quits at once, even while a warehouse is starting.

`d` downloads the selected file after the user confirms it in a dialog.
One download runs at a time, in the background, while you keep browsing.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from rich.console import Group
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Input, Static

from .. import downloads, paths
from ..backend import Backend
from ..config import Settings
from ..errors import (
    BackendError,
    DownloadCancelled,
    InvalidPathError,
    PermissionDeniedError,
    ViewerError,
)
from ..model import Entry, Listing, Location, SortOrder, human_size, local_time, sort_entries
from .screens import DownloadScreen, GoToScreen, HelpScreen

T = TypeVar("T")

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass
class DownloadState:
    """The download that is running now."""

    source: str
    target: Path
    size: int | None
    message: str = "Starting…"
    written: int = 0
    cancel: threading.Event = field(default_factory=threading.Event)


async def in_daemon_thread(fn: Callable[..., T], *args: Any) -> T:
    """Run a blocking call in a daemon thread and wait for the result.

    Textual thread workers use the asyncio default executor, and Python
    waits for those threads at exit. A LIST call can wait minutes for a
    warehouse to start, so the app uses daemon threads, which do not block exit.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()

    def deliver(setter: Callable[[Any], None], value: Any) -> None:
        if not future.done():
            setter(value)

    def runner() -> None:
        try:
            result = fn(*args)
        except BaseException as exc:  # noqa: BLE001 - passed on to the awaiting task
            setter, value = future.set_exception, exc
        else:
            setter, value = future.set_result, result
        try:
            loop.call_soon_threadsafe(deliver, setter, value)
        except RuntimeError:
            pass  # The app closed while the call ran.

    threading.Thread(target=runner, daemon=True, name="dbx-s3-viewer-call").start()
    return await future


class EntryTable(DataTable):
    """The listing table, with vim keys and arrow keys for up and open."""

    BINDINGS = [
        Binding("enter", "app.open", "Open"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("left", "app.go_up", show=False),
        Binding("right", "app.open", show=False),
    ]


Row = Entry | Location


def _row_key(row: Row) -> str:
    return row.path if isinstance(row, Entry) else row.url


class ViewerApp(App[None]):
    """Browse folders in a table, and download single files on request."""

    TITLE = "dbx-s3-viewer"
    # The palette has a "save screenshot" command, which writes a file.
    # The tool writes no file except the config file and downloads that the
    # user confirms, so the palette is off.
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { layout: vertical; }
    #notice { height: auto; display: none; padding: 0 1; background: $warning 25%; }
    #notice.info { background: $panel; color: $text-muted; }
    #table { height: 1fr; }
    #message { height: 1fr; display: none; padding: 1 2; }
    #filter { display: none; height: 3; }
    #download { height: 1; display: none; padding: 0 1; background: $accent 30%; }
    #status { height: 1; background: $panel; }
    #status-path { width: 1fr; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #status-info { width: auto; padding: 0 1; }
    #status-state { width: auto; max-width: 60%; padding: 0 1; text-wrap: nowrap;
                    text-overflow: ellipsis; }
    GoToScreen, HelpScreen, DownloadScreen { align: center middle; }
    #dialog { width: 80%; max-width: 100; height: auto; padding: 1 2;
              border: round $accent; background: $surface; }
    .dialog-title { text-style: bold; padding-bottom: 1; }
    .dialog-hint { color: $text-muted; padding-top: 1; }
    """

    BINDINGS = [
        Binding("backspace", "go_up", "Up"),
        Binding("h", "go_up", show=False),
        Binding("l", "open", show=False),
        Binding("g", "goto", "Go to"),
        Binding("slash", "filter", "Filter"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("r", "refresh", "Refresh"),
        Binding("y", "copy_path", "Copy path"),
        Binding("b", "bookmark", "Bookmark"),
        Binding("asterisk", "start_folder", show=False),
        Binding("tilde", "home", show=False),
        Binding("d", "download", "Download"),
        Binding("x", "cancel_download", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
        Binding("escape", "escape", show=False),
    ]

    def __init__(
        self,
        backend: Backend,
        *,
        bookmarks: list[str],
        save_bookmarks: Callable[[list[str]], None] | None,
        start_path: str | None = None,
        start_folder: str | None = None,
        save_start_folder: Callable[[str | None], None] | None = None,
        settings: Settings | None = None,
        copy: Callable[[str], bool] | None = None,
    ) -> None:
        """`start_path` is where to open now. `start_folder` is the saved default."""
        super().__init__()
        self.backend = backend
        self.bookmarks = list(bookmarks)
        self._save_bookmarks = save_bookmarks
        self._start_path = start_path
        self.start_folder = start_folder
        self._save_start_folder = save_start_folder
        self.settings = settings
        self._copy = copy or (lambda _text: False)

        self.cache: dict[str, Listing] = {}
        #: The open folder, or None for home.
        self.location: str | None = None
        self.listing: Listing | None = None
        self.error: ViewerError | None = None
        self.external: list[Location] = []
        self.external_note = ""
        self.sort = SortOrder.NAME
        self.filter_text = ""
        self._rows: list[Row] = []
        self._request = 0
        self._loading_message = ""
        self._loading_since = 0.0
        self._locations_loading = False
        self._spin = 0
        self.download: DownloadState | None = None
        self._quit_armed = False

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="notice")
        yield EntryTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Static(id="message")
        yield Input(placeholder="Filter by name. Enter keeps the filter, Esc clears it.",
                    id="filter")
        yield Static(id="download")
        with Horizontal(id="status"):
            yield Static(id="status-path")
            yield Static(id="status-info")
            yield Static(id="status-state")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.1, self._tick)
        self.refresh_locations()
        if self._start_path:
            self.navigate(self._start_path)
        else:
            self.show_home()
        self.query_one(EntryTable).focus()

    # -- navigation ---------------------------------------------------------

    @property
    def loading(self) -> bool:
        return bool(self._loading_message)

    def show_home(self, select: str | None = None) -> None:
        """Show bookmarks and external locations."""
        self._request += 1  # Drop the result of any listing still on its way.
        self.workers.cancel_group(self, "listing")
        self._loading_message = ""
        self.location = None
        self.listing = None
        self.error = None
        self._set_filter("")
        self._render(select=select)

    def navigate(self, path: str, *, refresh: bool = False, select: str | None = None) -> None:
        """Open a folder. Use the cache unless `refresh` is True."""
        try:
            directory = paths.as_dir(paths.normalize(path))
        except InvalidPathError as exc:
            self.notify(f"{exc.message} {exc.hint}", severity="error", markup=False)
            return
        if directory != self.location:
            self._set_filter("")
            self.listing = None
        self.location = directory
        self.error = None
        self._request += 1
        if not refresh and directory in self.cache:
            self.workers.cancel_group(self, "listing")
            self._loading_message = ""
            self.listing = self.cache[directory]
            self._render(select=select)
            return
        self._loading_message = "Listing…"
        self._loading_since = time.monotonic()
        self._render(select=select)
        self._load(directory, self._request, select)

    @work(exclusive=True, group="listing", exit_on_error=False)
    async def _load(self, directory: str, request: int, select: str | None) -> None:
        loop = asyncio.get_running_loop()

        def progress(message: str) -> None:
            try:
                loop.call_soon_threadsafe(self._on_progress, request, message)
            except RuntimeError:
                pass

        try:
            listing = await in_daemon_thread(self.backend.list, directory, progress)
        except ViewerError as exc:
            self._on_failed(request, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - show any surprise in the UI
            self._on_failed(request, BackendError(
                "Something unexpected went wrong.",
                hint="Press r to try again. If it keeps happening, report the message below.",
                detail=f"{type(exc).__name__}: {exc}",
            ))
        else:
            self.cache[listing.path] = listing
            if request == self._request:
                self.listing = listing
                self._loading_message = ""
                self._render(select=select)

    def _on_progress(self, request: int, message: str) -> None:
        if request == self._request and self.loading:
            self._loading_message = message
            self._update_status()

    def _on_failed(self, request: int, exc: ViewerError) -> None:
        if request != self._request:
            return
        self.error = exc
        self.listing = None
        self._loading_message = ""
        self._render()

    def refresh_locations(self) -> None:
        self._locations_loading = True
        self._load_locations()

    @work(exclusive=True, group="locations", exit_on_error=False)
    async def _load_locations(self) -> None:
        note = ""
        try:
            found = await in_daemon_thread(self.backend.locations)
        except PermissionDeniedError:
            found, note = [], "External locations are hidden: you cannot list them."
        except ViewerError as exc:
            found = []
            note = f"Could not load external locations. {exc.message} {exc.hint}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            found, note = [], f"Could not load external locations: {type(exc).__name__}: {exc}"
        self.external = list(found)
        self.external_note = note
        self._locations_loading = False
        if self.location is None:
            self._render()
        else:
            self._update_status()

    # -- rendering ----------------------------------------------------------

    def _home_rows(self) -> list[Location]:
        rows = [Location(paths.basename(url), url, "bookmark") for url in self.bookmarks]
        rows += self.external
        start = self.start_folder
        if start and not any(self._is_start(row.url) for row in rows):
            rows.insert(0, Location(paths.basename(start), start, "start folder"))
        return rows

    def _is_start(self, url: str | None) -> bool:
        # External location URLs can come without the trailing slash.
        return bool(url and self.start_folder
                    and url.rstrip("/") == self.start_folder.rstrip("/"))

    def _matches(self, *texts: str) -> bool:
        needle = self.filter_text.casefold()
        return not needle or any(needle in t.casefold() for t in texts)

    def _render(self, select: str | None = None) -> None:
        """Redraw the table, the message panel, the notice and the status bar."""
        table = self.query_one(EntryTable)
        keep = select or self._highlighted_key()
        table.clear(columns=True)
        self._rows = []
        message: Text | Group | None = None
        labels: tuple[str, ...] = ()
        cells: list[tuple[Text, ...]] = []

        if self.location is None:
            labels = ("Name", "URL", "Type")
            home = self._home_rows()
            for loc in home:
                if self._matches(loc.name, loc.url):
                    kind = Text(loc.source, style="dim")
                    if self._is_start(loc.url) and loc.source != "start folder":
                        kind.append(" · start folder", style="bold")
                    cells.append((Text(loc.name, style="bold"), Text(loc.url), kind))
                    self._rows.append(loc)
            if not home and not self._locations_loading:
                message = Text.assemble(
                    ("No bookmarks or external locations yet.\n\n", "bold"),
                    "Press g to type an s3:// path. In a folder, press b to bookmark it.",
                )
            elif home and not self._rows and self.filter_text:
                message = Text(f"No names contain {self.filter_text!r}. Press Esc to clear "
                               "the filter.")
        elif self.error is not None:
            message = self._error_panel(self.error)
        elif self.listing is None:
            message = self._loading_panel()
        else:
            labels = ("Name", "Size", "Modified")
            shown = [e for e in self.listing.entries if self._matches(e.name)]
            for entry in sort_entries(shown, self.sort):
                if entry.is_dir:
                    name = Text(entry.name + "/", style="bold cyan")
                    size = Text("")
                else:
                    name = Text(entry.name)
                    size = Text(human_size(entry.size), justify="right")
                cells.append((name, size, Text(local_time(entry.modified_ms))))
                self._rows.append(entry)
            if not self._rows:
                if self.filter_text:
                    message = Text(f"No names contain {self.filter_text!r}. Press Esc to clear "
                                   "the filter.")
                elif not self.listing.entries:
                    message = Text.assemble(
                        ("No objects at this path.\n\n", "bold"),
                        "In S3, an empty folder and a missing folder look the same. "
                        "Press Backspace to go up, or g to type another path.",
                    )

        # Set each column width from the cells. With automatic widths, the
        # DataTable can draw and cache rows before it measures them, and then
        # shows names cut to the width of the header.
        for index, label in enumerate(labels):
            width = max([len(label)] + [row[index].cell_len for row in cells])
            table.add_column(label, key=label.lower(), width=width)
        for row in cells:
            table.add_row(*row)

        panel = self.query_one("#message", Static)
        if message is None:
            panel.display = False
            table.display = True
        else:
            panel.update(message)
            panel.display = True
            table.display = bool(self._rows)
        if keep:
            for index, row in enumerate(self._rows):
                if _row_key(row) == keep:
                    table.move_cursor(row=index, animate=False)
                    break
        if table.display and self.focused is None:
            table.focus()
        self._update_notice()
        self._update_status()

    def _error_panel(self, exc: ViewerError) -> Text:
        text = Text()
        text.append(exc.title + "\n\n", style="bold red")
        text.append(exc.message + "\n")
        if exc.hint:
            text.append("\nWhat to do: ", style="bold")
            text.append(exc.hint + "\n")
        if exc.detail:
            text.append("\nServer message: ", style="bold")
            text.append(exc.detail.strip()[:2000] + "\n", style="dim")
        text.append("\nPress r to try again, Backspace to go up, ~ to go home, or g to type "
                    "another path.", style="dim")
        return text

    def _loading_panel(self) -> Text:
        frame = SPINNER[self._spin % len(SPINNER)]
        elapsed = time.monotonic() - self._loading_since
        text = Text()
        text.append(f"{frame} {self._loading_message}", style="bold")
        text.append(f"  {elapsed:.0f}s\n\n", style="dim")
        text.append(self.location or "")
        return text

    def _update_notice(self) -> None:
        notice = self.query_one("#notice", Static)
        notice.remove_class("info")
        if self.location is not None and self.listing is not None and self.listing.truncated:
            notice.update(Text(
                f"⚠ Truncated: this folder has more than {self.listing.limit} objects, and "
                f"the table shows the first {self.listing.limit} only. To see more, set "
                "list_limit in the config file or start with --limit N."
            ))
            notice.display = True
        elif self.location is None and self.external_note:
            notice.update(Text(self.external_note))
            notice.add_class("info")
            notice.display = True
        else:
            notice.display = False

    def _update_status(self) -> None:
        path_label = self.location or "Home"
        self.query_one("#status-path", Static).update(Text(path_label, style="bold"))

        info = Text()
        if self.location is None:
            info.append(f"{len(self._rows)} places")
        elif self.listing is not None:
            total = len(self.listing.entries)
            plus = "+" if self.listing.truncated else ""
            count = f"{total}{plus} items"
            if self.filter_text:
                count = f"{len(self._rows)} of {total}{plus} items"
            info.append(count, style="bold yellow" if self.listing.truncated else "")
            if self.listing.truncated:
                info.append(" (truncated)", style="bold yellow")
            info.append(f" · sort: {self.sort.label}")
            if self._is_start(self.location):
                info.append(" · start folder")
        if self.filter_text:
            info.append(f" · filter: {self.filter_text}")
        self.query_one("#status-info", Static).update(info)

        state = Text()
        frame = SPINNER[self._spin % len(SPINNER)]
        if self.loading:
            elapsed = time.monotonic() - self._loading_since
            state.append(f"{frame} {self._loading_message} {elapsed:.0f}s", style="bold")
        elif self.location is None and self._locations_loading:
            state.append(f"{frame} Loading external locations…")
        elif self.error is not None and self.location is not None:
            state.append(f"✗ {self.error.title}", style="bold red")
        elif self.backend.label == "demo":
            state.append("demo data", style="dim")
        self.query_one("#status-state", Static).update(state)

    def _tick(self) -> None:
        if self.download is not None:
            self._update_download_line()
        if self.loading or (self.location is None and self._locations_loading):
            self._spin += 1
            self._update_status()
            if self.loading and self.listing is None and self.error is None and self.location:
                self.query_one("#message", Static).update(self._loading_panel())

    # -- rows ---------------------------------------------------------------

    def highlighted(self) -> Row | None:
        """Return the row under the cursor, or None if the table is empty."""
        table = self.query_one(EntryTable)
        index = table.cursor_row
        if not self._rows or index is None or not 0 <= index < len(self._rows):
            return None
        return self._rows[index]

    def _highlighted_key(self) -> str | None:
        row = self.highlighted()
        return _row_key(row) if row else None

    # -- actions ------------------------------------------------------------

    def on_descendant_blur(self, event: events.DescendantBlur) -> None:
        """Give focus back to the table when it loses focus for no reason.

        Textual blurs a widget when it hides, and the hide event can arrive
        after the table is visible again. Without this, j and k stop working
        after a folder loads. During shutdown the table is already gone, so
        the handler must not look it up.
        """
        if isinstance(event.widget, EntryTable):
            self.call_after_refresh(self._refocus_table)

    def _refocus_table(self) -> None:
        if not self.is_running:
            return
        for table in self.query(EntryTable):
            if table.display and table.screen is self.screen and self.focused is None:
                table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # A click on the row that already has the cursor.
        event.stop()
        self.action_open()

    def action_open(self) -> None:
        row = self.highlighted()
        if isinstance(row, Location):
            self.navigate(row.url)
        elif isinstance(row, Entry):
            if row.is_dir:
                self.navigate(row.path)
            else:
                self.notify(
                    "dbx-s3-viewer only lists paths. It does not open files. "
                    "Press y to copy the path.",
                    title=row.name,
                    markup=False,
                )

    def action_go_up(self) -> None:
        if self.location is None:
            return
        up = paths.parent(self.location)
        if up is None:
            self.show_home(select=self.location)
        else:
            self.navigate(up, select=self.location)

    def action_home(self) -> None:
        if self.location is not None:
            self.show_home(select=self.location)

    def action_goto(self) -> None:
        def go(path: str | None) -> None:
            if path:
                self.navigate(path)

        self.push_screen(GoToScreen(self.location or "s3://"), go)

    def action_filter(self) -> None:
        field = self.query_one("#filter", Input)
        field.display = True
        field.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self.filter_text = event.value.strip()
            self._render()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter":
            if not event.value.strip():
                event.input.display = False
            self.query_one(EntryTable).focus()

    def action_escape(self) -> None:
        field = self.query_one("#filter", Input)
        if field.display or self.filter_text:
            self._set_filter("")
            self._render()
            self.query_one(EntryTable).focus()

    def _set_filter(self, text: str) -> None:
        self.filter_text = text
        field = self.query_one("#filter", Input)
        with field.prevent(Input.Changed):
            field.value = text
        if not text:
            field.display = False

    def action_cycle_sort(self) -> None:
        self.sort = self.sort.next()
        if self.location is None:
            self.notify(f"Sort: {self.sort.label}. Sorting applies to folders.", markup=False)
        self._render()

    def action_refresh(self) -> None:
        if self.location is None:
            self.refresh_locations()
            self._render()
        else:
            self.navigate(self.location, refresh=True)

    def action_copy_path(self) -> None:
        row = self.highlighted()
        text = _row_key(row) if row else self.location
        if not text:
            return
        if self._copy(text):
            self.notify(f"Copied {text}", markup=False)
        else:
            self.copy_to_clipboard(text)
            self.notify(
                f"Sent {text} to the terminal clipboard. If paste does not work, "
                "your terminal does not support OSC 52 copy.",
                markup=False,
            )

    def action_bookmark(self) -> None:
        if self.location is not None:
            if self.location in self.bookmarks:
                self._set_bookmarks([b for b in self.bookmarks if b != self.location],
                                    f"Removed bookmark {self.location}")
            else:
                self._set_bookmarks(self.bookmarks + [self.location],
                                    f"Bookmarked {self.location}")
            return
        row = self.highlighted()
        if isinstance(row, Location) and row.source == "bookmark":
            self._set_bookmarks([b for b in self.bookmarks if b != row.url],
                                f"Removed bookmark {row.url}")
        else:
            self.notify("Open a folder, then press b to bookmark it. On the home screen, "
                        "b removes the selected bookmark.", markup=False)

    def _set_bookmarks(self, bookmarks: list[str], done: str) -> None:
        if self._store(self._save_bookmarks, bookmarks, done):
            self.bookmarks = bookmarks
            self._render()

    def action_start_folder(self) -> None:
        """Make the open folder, or the selected place, the start folder. Again: clear it."""
        if self.location is not None:
            folder = self.location
        else:
            row = self.highlighted()
            if not isinstance(row, Location):
                self.notify("Open a folder or select a place, then press * to start there "
                            "next time.", markup=False)
                return
            try:
                folder = paths.as_dir(paths.normalize(row.url))
            except InvalidPathError as exc:
                self.notify(f"{exc.message} {exc.hint}", severity="error", markup=False)
                return
        if self._is_start(folder):
            new, done = None, "Cleared the start folder. Next time, dbx-s3-viewer starts at home."
        else:
            new, done = folder, f"Next time, dbx-s3-viewer starts at {folder}"
        if self._store(self._save_start_folder, new, done):
            self.start_folder = new
            self._render()

    def _store(self, save: Callable[[Any], None] | None, value: Any, done: str) -> bool:
        """Save a setting to the config file, and say so. Return False if it failed."""
        if save is None:
            self.notify(f"{done} (demo mode: not saved)", markup=False)
            return True
        try:
            save(value)
        except ViewerError as exc:
            self.notify(f"{exc.message} {exc.hint}", title=exc.title, severity="error",
                        markup=False)
            return False
        self.notify(done, markup=False)
        return True

    # -- downloads ----------------------------------------------------------

    def action_download(self) -> None:
        row = self.highlighted()
        if not isinstance(row, Entry) or row.is_dir:
            self.notify("Select a file to download. dbx-s3-viewer downloads one file at a time.",
                        markup=False)
            return
        if self.download is not None:
            if self.download.cancel.is_set():
                self.notify("The cancelled download is still stopping. Try again in a moment.",
                            markup=False)
            else:
                self.notify(f"A download is running: {self.download.target.name}. Press x to "
                            "cancel it.", markup=False)
            return
        folder = (self.settings.download_dir if self.settings else None) \
            or downloads.default_download_dir()
        target = downloads.free_target(folder, downloads.safe_filename(row.name))

        def start(chosen: Path | None) -> None:
            if chosen is not None:
                self._start_download(row, chosen)

        self.push_screen(DownloadScreen(row.path, row.size, target), start)

    def _start_download(self, entry: Entry, target: Path) -> None:
        if self.download is not None:
            return
        self.download = DownloadState(entry.path, target, entry.size)
        self._update_download_line()
        self._run_download(self.download)

    @work(group="download", exit_on_error=False)
    async def _run_download(self, state: DownloadState) -> None:
        def message(text: str) -> None:
            state.message = text  # read by the UI on its next tick

        def on_bytes(written: int, total: int) -> None:
            state.written, state.size = written, total

        def job() -> int:
            content = self.backend.read_file(state.source, message, state.cancel)
            state.size = content.size
            state.message = "Downloading…"
            return downloads.save(content, state.target, on_bytes=on_bytes,
                                  cancel=state.cancel)

        try:
            written = await in_daemon_thread(job)
        except DownloadCancelled:
            self.notify(f"Cancelled the download of {state.target.name}. No file was saved.",
                        markup=False)
        except ViewerError as exc:
            if not state.cancel.is_set():
                detail = f" Server message: {exc.detail.strip()[:300]}" if exc.detail else ""
                self.notify(f"{exc.message} {exc.hint}{detail}", title=exc.title,
                            severity="error", timeout=20, markup=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - show any surprise in the UI
            self.notify(f"{type(exc).__name__}: {exc}", title="Download failed",
                        severity="error", timeout=20, markup=False)
        else:
            self.notify(f"Saved {state.target} ({human_size(written)})", title="Download done",
                        timeout=10, markup=False)
        finally:
            if self.download is state:
                self.download = None
            self._update_download_line()

    def _update_download_line(self) -> None:
        line = self.query_one("#download", Static)
        state = self.download
        if state is None:
            line.display = False
            return
        frame = SPINNER[self._spin % len(SPINNER)]
        if state.cancel.is_set():
            line.update(Text(f"{frame} ↓ {state.target.name}: cancelling…"))
            line.display = True
            return
        if state.written and state.size is not None:
            progress = downloads.describe(state.written, state.size)
        else:
            progress = state.message
        line.update(Text(f"{frame} ↓ {state.target.name}: {progress}   (x cancels)"))
        line.display = True

    def action_cancel_download(self) -> None:
        """Ask the running download to stop.

        The download stays on screen as "cancelling" until its thread has
        stopped the query and removed the .part file. Only then can a new
        download start, so two downloads never overlap.
        """
        state = self.download
        if state is None or state.cancel.is_set():
            return
        state.cancel.set()
        self._update_download_line()

    async def action_quit(self) -> None:
        state = self.download
        if state is not None and not self._quit_armed:
            self._quit_armed = True
            self.notify(f"{state.target.name} is still downloading. Press q again to cancel "
                        "it and quit.", markup=False)
            self.set_timer(4, self._disarm_quit)
            return
        if state is not None:
            state.cancel.set()
            # The daemon thread stops when the app exits, so remove its .part file here.
            try:
                downloads.part_path(state.target).unlink(missing_ok=True)
            except OSError:
                pass  # Windows cannot remove a file that is still open.
        self.exit()

    def _disarm_quit(self) -> None:
        self._quit_armed = False

    def action_help(self) -> None:
        about: list[tuple[str, str]] = []
        if self.backend.label == "demo":
            about.append(("Data", "demo (made up, nothing is sent to Databricks)"))
        if self.settings is not None:
            s = self.settings
            about += [
                ("Config file", str(s.config_path)),
                ("Profile", s.profile or "SDK default"),
                ("Warehouse ID", (s.warehouse_id or "not set")
                 + (f" (from {s.warehouse_source})" if s.warehouse_source else "")),
                ("List limit", str(s.list_limit)),
            ]
        about.append(("Start folder", self.start_folder or "home screen"))
        self.push_screen(HelpScreen(about))

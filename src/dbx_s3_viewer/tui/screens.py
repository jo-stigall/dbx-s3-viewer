"""Pop-up screens: "go to path", download and help."""

from __future__ import annotations

from pathlib import Path

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from .. import paths
from ..downloads import check_target, free_target, safe_filename
from ..errors import InvalidPathError, ViewerError
from ..model import human_size

KEYS = [
    ("↑ ↓  j k", "Move the cursor"),
    ("Enter  l  →", "Open the folder or place"),
    ("Backspace  h  ←", "Go up one level (from a bucket root: home)"),
    ("g", "Type a path to go to"),
    ("/", "Filter this listing by name (Esc clears)"),
    ("s", "Sort: name, size, newest first (folders stay first)"),
    ("r", "Refresh: list again, skip the cache"),
    ("y", "Copy the full path of the selected row"),
    ("b", "Bookmark this folder, or remove the bookmark"),
    ("*", "Open this folder at start (press again to start at home)"),
    ("~", "Go to the home screen"),
    ("d", "Download the selected file (asks first)"),
    ("x", "Cancel the running download"),
    ("?", "Show this help"),
    ("q", "Quit"),
]


class DownloadScreen(ModalScreen[Path | None]):
    """Ask before a download, and let the user change where the file goes.

    Returns the target path, or None on Esc.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, source: str, size: int | None, target: Path) -> None:
        super().__init__()
        self._source_path = source
        self._file_size = size
        self._default_target = target

    def compose(self) -> ComposeResult:
        size = human_size(self._file_size) if self._file_size is not None else "unknown size"
        with Vertical(id="dialog"):
            yield Static("Download file", classes="dialog-title")
            yield Static(Text(f"{self._source_path}\n{size}"))
            yield Static(Text(
                "The file contents will be saved on this computer. Check that your team "
                "allows this for the data in this bucket.", style="bold"))
            yield Static("Save to:", classes="dialog-hint")
            yield Input(value=str(self._default_target), id="download-input")
            yield Static("Enter to download, Esc to cancel", id="download-error",
                         classes="dialog-hint")

    def on_mount(self) -> None:
        field = self.query_one(Input)
        field.focus()
        field.cursor_position = len(field.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value.strip()
        if not text:
            self.query_one("#download-error", Static).update(
                Text("Type the path of the file or folder to save to.", style="red"))
            return
        target = Path(text).expanduser()
        if target.is_dir():
            # A folder: use the file's own name, and add " (1)" only if that folder needs it.
            target = free_target(target, safe_filename(paths.basename(self._source_path)))
        try:
            target = check_target(target)
        except ViewerError as exc:
            self.query_one("#download-error", Static).update(
                Text(f"{exc.message} {exc.hint}", style="red"))
            return
        self.dismiss(target)

    def action_cancel(self) -> None:
        self.dismiss(None)


class GoToScreen(ModalScreen[str | None]):
    """Ask for an s3:// path. Returns the clean path, or None on Esc."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, initial: str) -> None:
        super().__init__()
        self._initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("Go to path", classes="dialog-title")
            yield Input(value=self._initial, placeholder="s3://bucket/folder/", id="goto-input")
            yield Static("Enter to go, Esc to cancel", id="goto-error", classes="dialog-hint")

    def on_mount(self) -> None:
        field = self.query_one(Input)
        field.focus()
        field.cursor_position = len(field.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        try:
            path = paths.as_dir(paths.normalize(event.value))
        except InvalidPathError as exc:
            self.query_one("#goto-error", Static).update(Text(f"{exc.message} {exc.hint}", style="red"))
            return
        self.dismiss(path)

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """Show the keys and where the settings come from."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", show=False),
        Binding("question_mark", "close", show=False),
    ]

    def __init__(self, about: list[tuple[str, str]]) -> None:
        super().__init__()
        self._about = about

    def compose(self) -> ComposeResult:
        keys = Table.grid(padding=(0, 2))
        keys.add_column(style="bold")
        keys.add_column()
        for key, meaning in KEYS:
            keys.add_row(key, meaning)
        about = Table.grid(padding=(0, 2))
        about.add_column(style="bold")
        about.add_column()
        for label, value in self._about:
            about.add_row(label, Text(value))
        with Vertical(id="dialog"):
            yield Static("dbx-s3-viewer keys", classes="dialog-title")
            yield Static(keys)
            yield Static("")
            yield Static(about)
            yield Static("The tool never changes or deletes anything in S3. It downloads a "
                         "file only when you press d and confirm.", classes="dialog-hint")
            yield Static("Esc to close", classes="dialog-hint")

    def action_close(self) -> None:
        self.dismiss(None)

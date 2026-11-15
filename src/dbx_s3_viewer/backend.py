"""The interface that the CLI and the TUI use to list paths and download files.

Two classes implement it: `DatabricksBackend` (the real one) and
`FakeBackend` (in memory, for `--demo` and for tests).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol

from .model import FileContent, Listing, Location

# The backend calls this with short status text, for example
# "SQL warehouse is starting...". The TUI shows the text in the status bar.
ProgressFn = Callable[[str], None]


class Backend(Protocol):
    """List directories and find starting points."""

    #: Short name to show to the user, for example "demo".
    label: str

    def list(self, path: str, progress: ProgressFn | None = None) -> Listing:
        """List the objects directly inside the directory `path`.

        Raises a subclass of ViewerError on failure.
        """
        ...

    def locations(self) -> list[Location]:
        """Return the external locations that the user can see.

        Raises PermissionDeniedError if the user cannot list them.
        """
        ...

    def read_file(
        self,
        path: str,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> FileContent:
        """Prepare a download of the one file at `path`.

        Raises InvalidPathError if `path` is a folder, and a subclass of
        ViewerError for other problems. The bytes arrive when you iterate
        over `FileContent.pieces`. Set `cancel` to stop the download; the
        backend then raises DownloadCancelled.
        """
        ...

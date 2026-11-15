"""Data types for listings, plus row parsing, sorting and display formats.

`parse_rows` turns the rows that `LIST` returns into `Entry` objects. The
rows come from the SQL Statement Execution API as lists of strings, in the
column order that the result schema gives.

Example row, with columns path, name, size, modification_time:

    ["s3://b/logs/app.log", "app.log", "2048", "1706105124000"]
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from . import paths


@dataclass(frozen=True)
class Entry:
    """One object or directory in a listing.

    `name` has no trailing slash. `path` is the full s3:// path, and ends
    with "/" for a directory. `size` is in bytes. `modified_ms` is a Unix
    time in milliseconds. Both are None if the row did not have them.
    """

    name: str
    path: str
    is_dir: bool
    size: int | None = None
    modified_ms: int | None = None

    @property
    def modified(self) -> datetime | None:
        """Return the modification time in UTC, or None if it is unknown or 0."""
        if not self.modified_ms:
            return None
        return datetime.fromtimestamp(self.modified_ms / 1000, tz=timezone.utc)


@dataclass(frozen=True)
class Listing:
    """The result of listing one directory.

    `truncated` is True when LIST had more rows than `limit`. Then `entries`
    holds only the first `limit` rows, in the order that LIST returned them.
    """

    path: str
    entries: tuple[Entry, ...]
    truncated: bool
    limit: int


@dataclass
class FileContent:
    """A file that a backend is ready to send.

    `size` is the size in bytes that Databricks reported before the
    download. `pieces` gives the bytes in order. The backend reads the
    file only when you iterate over `pieces`.
    """

    path: str
    size: int
    pieces: Iterator[bytes]


@dataclass(frozen=True)
class Location:
    """A place to start browsing: a bookmark or a Unity Catalog external location."""

    name: str
    url: str
    source: str  # "bookmark" or "external location"


def is_directory(row: Mapping[str, str | None]) -> bool:
    """Decide if a LIST row is a directory. All directory checks happen here.

    Some versions of the Databricks docs show an `is_directory` column, and
    some do not. The current docs (November 2026) show only path, name, size
    and modification_time.

    1. If the row has an `is_directory` column with a value, use it.
    2. If not, the row is a directory when `name` or `path` ends with "/".

    Example: {"name": "logs/", "path": "s3://b/logs/"} is a directory.
    """
    flag = row.get("is_directory")
    if flag is not None:
        return str(flag).strip().lower() in ("true", "1")
    return (row.get("name") or "").endswith("/") or (row.get("path") or "").endswith("/")


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        try:
            return int(float(value))
        except ValueError:
            return None


def parse_rows(
    columns: Sequence[str], rows: Iterable[Sequence[str | None]], directory: str
) -> list[Entry]:
    """Turn LIST result rows into entries.

    `columns` are the column names from the result schema, in order. The
    function finds each column by name, so a new or moved column does not
    break it. `directory` is the listed path. The function uses it when a
    row has no `path`.
    """
    names = [c.lower() for c in columns]
    entries = []
    for values in rows:
        row = dict(zip(names, values))
        is_dir = is_directory(row)
        raw_path = row.get("path") or ""
        raw_name = row.get("name") or ""
        name = raw_name.strip("/") or paths.basename(raw_path)
        if not name:
            continue
        path = raw_path or paths.join(directory, name)
        if paths.as_dir(path) == paths.as_dir(directory):
            continue  # The folder marker of the listed folder, not a child.
        if is_dir:
            path = paths.as_dir(path)
        entries.append(
            Entry(
                name=name,
                path=path,
                is_dir=is_dir,
                size=_to_int(row.get("size")),
                modified_ms=_to_int(row.get("modification_time")),
            )
        )
    return entries


class SortOrder(enum.Enum):
    """The sort orders that the `s` key cycles through."""

    NAME = "name"
    SIZE = "size"
    MODIFIED = "modified"

    def next(self) -> SortOrder:
        members = list(SortOrder)
        return members[(members.index(self) + 1) % len(members)]

    @property
    def label(self) -> str:
        return {"name": "name A→Z", "size": "size, largest first", "modified": "newest first"}[
            self.value
        ]


def sort_entries(entries: Iterable[Entry], order: SortOrder = SortOrder.NAME) -> list[Entry]:
    """Sort entries with directories first, then by the given order.

    NAME sorts A to Z without regard to case. SIZE puts the largest first.
    MODIFIED puts the newest first. Ties sort by name.
    """
    by_name = sorted(entries, key=lambda e: (e.name.casefold(), e.name))
    if order is SortOrder.SIZE:
        by_name.sort(key=lambda e: -(e.size or 0))
    elif order is SortOrder.MODIFIED:
        by_name.sort(key=lambda e: -(e.modified_ms or 0))
    by_name.sort(key=lambda e: not e.is_dir)
    return by_name


_UNITS = ("KiB", "MiB", "GiB", "TiB", "PiB")


def human_size(size: int | None) -> str:
    """Format a byte count for people. Example: human_size(1536) returns "1.5 KiB"."""
    if size is None:
        return ""
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in _UNITS:
        value /= 1024
        if value < 1024 or unit == _UNITS[-1]:
            return f"{value:.1f} {unit}"
    raise AssertionError("unreachable")


def local_time(modified_ms: int | None) -> str:
    """Format a Unix time in milliseconds as local time, "YYYY-MM-DD HH:MM".

    Returns "" for None or 0, because LIST gives 0 for most directories.
    """
    if not modified_ms:
        return ""
    moment = datetime.fromtimestamp(modified_ms / 1000, tz=timezone.utc).astimezone()
    return moment.strftime("%Y-%m-%d %H:%M")

"""An in-memory backend for `--demo` and for tests. It sends nothing over the network.

Build one from a list of object paths. The fake adds every parent
directory itself:

    fake = FakeBackend.from_objects({"s3://b/logs/app.log": (2048, 1706105124000)})
    fake.list("s3://b/")        # one entry: the directory "logs"
    fake.list("s3://b/logs/")   # one entry: the file "app.log"

A download from the fake gives made-up text of the listed size, or the
bytes in `contents` if the test gives them.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import datetime, timezone

from . import paths
from .backend import ProgressFn
from .databricks_backend import MSG_CHECKING, MSG_LISTING, MSG_READING, MSG_STARTING
from .errors import (
    DownloadCancelled,
    DownloadError,
    InvalidPathError,
    PathNotFoundError,
    PermissionDeniedError,
    ViewerError,
)
from .model import Entry, FileContent, Listing, Location, human_size

#: The fake sends downloads in pieces of this many bytes.
FAKE_PIECE = 1024 * 1024


class FakeBackend:
    """A backend that lists a fixed tree of paths from memory."""

    label = "demo"

    def __init__(
        self,
        tree: Mapping[str, Iterable[Entry]],
        *,
        locations: Iterable[Location] = (),
        limit: int = 1000,
        delay: float = 0.0,
        cold_start: float = 0.0,
        failures: Mapping[str, ViewerError] | None = None,
        locations_error: ViewerError | None = None,
        sleep: Callable[[float], None] = time.sleep,
        contents: Mapping[str, bytes] | None = None,
        piece_delay: float = 0.0,
        max_download: int | None = None,
    ) -> None:
        self._tree = {path: list(entries) for path, entries in tree.items()}
        self._locations = list(locations)
        self._limit = limit
        self._delay = delay
        self._cold_start = cold_start
        self._failures = dict(failures or {})
        self._locations_error = locations_error
        self._sleep = sleep
        self._contents = dict(contents or {})
        self._piece_delay = piece_delay
        self._max_download = max_download
        #: Every directory that `list` was asked for, in order. Tests read this.
        self.calls: list[str] = []
        #: Every file that `read_file` was asked for, in order.
        self.reads: list[str] = []

    @classmethod
    def from_objects(
        cls,
        objects: Mapping[str, tuple[int, int]],
        *,
        empty_dirs: Iterable[str] = (),
        **kwargs: object,
    ) -> FakeBackend:
        """Build a fake from {object path: (size, modified_ms)}.

        `empty_dirs` adds directories that have no objects.
        """
        tree: dict[str, dict[str, Entry]] = {}

        def add_dir(directory: str) -> None:
            while True:
                tree.setdefault(directory, {})
                up = paths.parent(directory)
                if up is None:
                    return
                child = Entry(name=paths.basename(directory), path=directory, is_dir=True, size=0,
                              modified_ms=0)
                tree.setdefault(up, {})[child.name] = child
                directory = up

        for directory in empty_dirs:
            add_dir(paths.as_dir(directory))
        for path, (size, modified_ms) in objects.items():
            directory = paths.parent(path)
            assert directory is not None, f"{path} has no parent"
            add_dir(directory)
            name = paths.basename(path)
            tree[directory][name] = Entry(name=name, path=path, is_dir=False, size=size,
                                          modified_ms=modified_ms)
        # Keep the order of a real S3 listing: sorted by key.
        ordered = {d: [children[k] for k in sorted(children)] for d, children in tree.items()}
        return cls(ordered, **kwargs)  # type: ignore[arg-type]

    def list(self, path: str, progress: ProgressFn | None = None) -> Listing:
        report = progress or (lambda _msg: None)
        directory = paths.as_dir(paths.normalize(path))
        self.calls.append(directory)
        if self._cold_start:
            # Only the first call waits for the start, like a real warehouse.
            wait, self._cold_start = self._cold_start, 0.0
            report(MSG_STARTING)
            self._sleep(wait)
        report(MSG_LISTING)
        if self._delay:
            self._sleep(self._delay)
        if directory in self._failures:
            raise self._failures[directory]
        if directory not in self._tree:
            raise PathNotFoundError(
                f"{directory} does not exist.",
                hint="Check the spelling, or go up one level and browse to the folder.",
                detail="[demo] PATH_NOT_FOUND",
            )
        entries = self._tree[directory]
        return Listing(directory, tuple(entries[: self._limit]), len(entries) > self._limit,
                       self._limit)

    def locations(self) -> list[Location]:
        if self._locations_error:
            raise self._locations_error
        return list(self._locations)

    def read_file(
        self,
        path: str,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> FileContent:
        report = progress or (lambda _msg: None)
        target = paths.check_file_path(path)
        self.reads.append(target)
        report(MSG_CHECKING)
        if self._delay:
            self._sleep(self._delay)
        folder = paths.parent(target) or target
        if folder in self._failures:
            raise self._failures[folder]
        if paths.as_dir(target) in self._tree:
            raise InvalidPathError(
                f"{target} is a folder, not one file.",
                hint="dbx-s3-viewer downloads one file at a time. Open the folder and "
                "select a file.",
            )
        entry = next((e for e in self._tree.get(folder, []) if e.path == target), None)
        if entry is None:
            raise PathNotFoundError(f"{target} does not exist.", detail="[demo] PATH_NOT_FOUND")
        if entry.name.startswith(("_", ".")):
            # Like Spark: read_files skips file names that start with _ or . (SPARK-26339).
            raise PathNotFoundError(
                f"{target} does not exist, or read_files cannot see it.",
                hint="read_files may skip files whose names start with _ or ., like "
                "_SUCCESS. Download those in a notebook.",
            )
        data = self._contents.get(target)
        size = len(data) if data is not None else entry.size or 0
        if self._max_download is not None and size > self._max_download:
            raise DownloadError(
                f"Demo mode downloads files up to {human_size(self._max_download)} only, and "
                f"{entry.name} is {human_size(size)}.",
                hint="Pick a smaller file, for example README.txt in s3://acme-landing/.",
            )

        def pieces() -> Iterator[bytes]:
            report(MSG_READING)
            body = data if data is not None else _made_up_bytes(entry.name, size)
            for start in range(0, len(body), FAKE_PIECE):
                if self._piece_delay:
                    self._sleep(self._piece_delay)
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled("You cancelled the download.")
                yield body[start:start + FAKE_PIECE]

        return FileContent(target, size, pieces())


def _made_up_bytes(name: str, size: int) -> bytes:
    line = f"dbx-s3-viewer demo data for {name}. This is not a real file.\n".encode()
    return (line * (size // len(line) + 1))[:size]


# -- demo data --------------------------------------------------------------

DEMO_BOOKMARKS = ["s3://acme-curated/sales/orders/", "s3://acme-landing/events/"]
#: Demo downloads write real files, so the demo keeps them small.
DEMO_MAX_DOWNLOAD = 64 * 1024 * 1024


def demo_backend(limit: int = 1000, *, delay: float = 0.35, cold_start: float = 2.5) -> FakeBackend:
    """Build the made-up workspace that `--demo` shows.

    It has three external locations. One of them is denied, and one folder
    has 2,400 objects, so you can see the permission message and the
    truncation notice.
    """
    rng = random.Random(42)
    now_ms = int(datetime(2026, 11, 12, 9, 30, tzinfo=timezone.utc).timestamp() * 1000)

    def stamp(max_days: float) -> int:
        return now_ms - int(rng.uniform(0, max_days) * 86_400_000)

    objects: dict[str, tuple[int, int]] = {}
    landing = "s3://acme-landing/"
    objects[landing + "README.txt"] = (1_284, stamp(300))
    objects[landing + "_SUCCESS"] = (0, stamp(300))
    for day in range(1, 13):
        for station in ("north", "south", "east"):
            key = f"vendor_feeds/weather/2026/11/{day:02d}/{station}.json"
            objects[landing + key] = (rng.randint(40_000, 90_000), stamp(13 - day))
    for day in range(1, 31):
        key = f"vendor_feeds/fx_rates/fx_2026-10-{day:02d}.csv"
        objects[landing + key] = (rng.randint(3_000, 4_000), stamp(40))
    for i in range(2400):
        key = f"events/year=2026/month=11/day=12/part-{i:05d}.snappy.parquet"
        objects[landing + key] = (rng.randint(2_000_000, 140_000_000), stamp(1))
    objects[landing + "client's uploads/batch-01.zip"] = (rng.randint(1, 9) * 10**8, stamp(60))

    curated = "s3://acme-curated/"
    for table in ("sales/orders", "sales/returns", "customers"):
        for i in range(rng.randint(4, 12)):
            objects[f"{curated}{table}/_delta_log/{i:020d}.json"] = (rng.randint(800, 9_000), stamp(30))
        for i in range(rng.randint(6, 30)):
            objects[f"{curated}{table}/part-{i:05d}-c000.snappy.parquet"] = (
                rng.randint(5_000_000, 900_000_000),
                stamp(30),
            )
    objects[curated + "sales/orders/_delta_log/_last_checkpoint"] = (156, stamp(1))

    restricted = "s3://acme-restricted/"
    denied = PermissionDeniedError(
        f"You do not have permission to list {restricted}.",
        hint=(
            "Ask a Unity Catalog admin for READ FILES on the external location that "
            "contains this path."
        ),
        detail="[demo] [INSUFFICIENT_PERMISSIONS] User does not have READ FILES on "
        "External Location 'restricted'.",
    )
    return FakeBackend.from_objects(
        objects,
        max_download=DEMO_MAX_DOWNLOAD,
        piece_delay=delay / 4,
        empty_dirs=[curated + "scratch/empty_folder/", restricted],
        locations=[
            Location("curated", curated, "external location"),
            Location("landing", landing, "external location"),
            Location("restricted", restricted, "external location"),
        ],
        failures={restricted: denied},
        limit=limit,
        delay=delay,
        cold_start=cold_start,
    )

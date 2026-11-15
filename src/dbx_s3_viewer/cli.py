"""Command-line entry point: the browser, `ls`, `get` and `check`.

    dbx-s3-viewer [s3://path]            open the browser
    dbx-s3-viewer ls s3://path [--json]  print one listing and exit
    dbx-s3-viewer get s3://path/file     download one file
    dbx-s3-viewer check s3://path        test LIST on your warehouse and print what came back
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Container, Sequence
from pathlib import Path
from typing import Any, TextIO

from . import __version__, downloads, paths
from .backend import Backend, ProgressFn
from .config import (
    DEFAULT_LIMIT,
    FileConfig,
    Settings,
    default_config_path,
    load,
    resolve,
    save_bookmarks,
    save_start_folder,
)
from .databricks_backend import MSG_STARTING, MSG_WAITING, DatabricksBackend
from .errors import (
    ConfigError,
    DownloadError,
    InvalidPathError,
    ViewerError,
    missing_warehouse_error,
)
from .fake_backend import DEMO_BOOKMARKS, demo_backend
from .model import Listing, human_size, local_time, sort_entries

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


def _silence_logging() -> None:
    """Make sure no library prints log records into the terminal or the TUI.

    A handler on the root logger stops Python's fallback handler, which
    would print warnings to stderr. dbx-s3-viewer keeps no logs.
    """
    logging.getLogger().addHandler(logging.NullHandler())


def _add_common(parser: argparse.ArgumentParser, *, demo: bool = True) -> None:
    parser.add_argument(
        "--profile",
        help="Databricks config profile from ~/.databrickscfg. "
        "Default: DATABRICKS_CONFIG_PROFILE, then `profile` in the config file.",
    )
    parser.add_argument(
        "--warehouse-id",
        help="SQL warehouse ID. Default: DATABRICKS_WAREHOUSE_ID, then the config file.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help=f"Most objects to show for one folder. Default: list_limit in the config "
        f"file, or {DEFAULT_LIMIT}.",
    )
    if demo:
        parser.add_argument(
            "--demo",
            action="store_true",
            help="Use made-up data. Sends nothing to Databricks. Writes no file unless you "
            "download one.",
        )


def _settings(ns: argparse.Namespace) -> Settings:
    """Load settings. In demo mode, ignore the config file and use demo bookmarks."""
    path = default_config_path()
    if getattr(ns, "demo", False):
        file_config = FileConfig(path, bookmarks=list(DEMO_BOOKMARKS))
    else:
        file_config = load(path)
    return resolve(
        file_config,
        profile_flag=ns.profile,
        warehouse_flag=ns.warehouse_id,
        limit_flag=ns.limit,
    )


def make_backend(settings: Settings, *, demo: bool, demo_delays: bool = True) -> Backend:
    """Build the fake backend for --demo, or the Databricks backend.

    The demo backend waits a little on each listing, so the browser shows
    its spinner. `ls --demo` turns the waits off.
    """
    if demo:
        if demo_delays:
            return demo_backend(settings.list_limit)
        return demo_backend(settings.list_limit, delay=0, cold_start=0)
    return DatabricksBackend(
        profile=settings.profile,
        warehouse_id=settings.warehouse_id,
        limit=settings.list_limit,
        timeout_seconds=settings.timeout_seconds,
        config_file=settings.config_path,
    )


def print_error(exc: ViewerError, stream: TextIO | None = None) -> None:
    """Print an error with its next step and the server message."""
    out = stream or sys.stderr
    print(f"dbx-s3-viewer: {exc.title}: {exc.message}", file=out)
    if exc.hint:
        print(f"  What to do: {exc.hint}", file=out)
    if exc.detail:
        print(f"  Server message: {exc.detail.strip()}", file=out)


def _exit_code(exc: ViewerError) -> int:
    usage = (ConfigError, InvalidPathError, DownloadError)
    return EXIT_USAGE if isinstance(exc, usage) else EXIT_FAILED


def listing_to_json(listing: Listing) -> dict[str, Any]:
    """Return the listing as plain data for `ls --json`. Directories come first."""
    entries = []
    for entry in sort_entries(listing.entries):
        modified = entry.modified
        entries.append(
            {
                "name": entry.name,
                "path": entry.path,
                "is_dir": entry.is_dir,
                "size": entry.size,
                "modification_time": entry.modified_ms,
                "modified": modified.isoformat() if modified else None,
            }
        )
    return {
        "path": listing.path,
        "truncated": listing.truncated,
        "limit": listing.limit,
        "count": len(entries),
        "entries": entries,
    }


def format_listing(listing: Listing) -> list[str]:
    """Return one line per entry for `ls`: local time, size, name.

    Example:
                           DIR  logs/
        2026-01-24 15:05  2.0 KiB  app.csv
    """
    lines = []
    for entry in sort_entries(listing.entries):
        size = "DIR" if entry.is_dir else human_size(entry.size)
        name = entry.name + ("/" if entry.is_dir else "")
        lines.append(f"{local_time(entry.modified_ms):16}  {size:>10}  {name}")
    return lines


def _stderr_progress() -> ProgressFn:
    """Return a progress function that prints warehouse start-up messages once each.

    It prints to stderr, and only if stderr is a terminal.
    """
    seen: set[str] = set()

    def report(message: str) -> None:
        if message in (MSG_STARTING, MSG_WAITING) and message not in seen and sys.stderr.isatty():
            seen.add(message)
            print(message, file=sys.stderr)

    return report


def run_ls(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="dbx-s3-viewer ls",
        description="Print the objects directly inside an s3:// folder, then exit. "
        "Folders come first. Times are local.",
    )
    parser.add_argument("path", help="The s3:// folder to list.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a table.")
    _add_common(parser)
    ns = parser.parse_args(argv)
    try:
        settings = _settings(ns)
        backend = make_backend(settings, demo=ns.demo, demo_delays=False)
        listing = backend.list(ns.path, progress=_stderr_progress())
    except ViewerError as exc:
        print_error(exc)
        return _exit_code(exc)
    except KeyboardInterrupt:
        print("dbx-s3-viewer: interrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED

    if ns.json:
        print(json.dumps(listing_to_json(listing), indent=2, ensure_ascii=False))
    else:
        for line in format_listing(listing):
            print(line)
        if not listing.entries and sys.stderr.isatty():
            print(f"No objects at {listing.path}.", file=sys.stderr)
    if listing.truncated:
        print(
            f"dbx-s3-viewer: warning: the listing is truncated. It shows the first "
            f"{listing.limit} objects only. Use --limit N or list_limit in the config "
            "file to see more.",
            file=sys.stderr,
        )
    return EXIT_OK


def run_get(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="dbx-s3-viewer get",
        description="Download one file to this computer through the SQL warehouse "
        "(read_files). The file contents land on this computer, so check that your team "
        "allows this for the data. The command never replaces a file.",
    )
    parser.add_argument("path", help="The s3:// path of one file.")
    parser.add_argument(
        "-o", "--output",
        help="The file or folder to save to. Default: the file name in the current folder.",
    )
    _add_common(parser)
    ns = parser.parse_args(argv)
    try:
        source = paths.check_file_path(ns.path)
        name = downloads.safe_filename(paths.basename(source))
        target = Path(ns.output).expanduser() if ns.output else Path.cwd() / name
        if target.is_dir():
            target = target / name
        target = downloads.check_target(target)
        settings = _settings(ns)
        backend = make_backend(settings, demo=ns.demo, demo_delays=False)
        content = backend.read_file(source, progress=_stderr_progress())
        written = downloads.save(content, target, on_bytes=_stderr_bytes())
    except ViewerError as exc:
        if sys.stderr.isatty():
            print(file=sys.stderr)
        print_error(exc)
        return _exit_code(exc)
    except KeyboardInterrupt:
        print("\ndbx-s3-viewer: interrupted. The partial file was removed.", file=sys.stderr)
        return EXIT_INTERRUPTED
    if sys.stderr.isatty():
        print(f"\rSaved {human_size(written)}.{' ' * 40}", file=sys.stderr)
    print(target)
    return EXIT_OK


def _stderr_bytes() -> downloads.BytesFn | None:
    """Return a function that shows download progress on one stderr line, or None."""
    if not sys.stderr.isatty():
        return None

    def show(written: int, total: int) -> None:
        print(f"\rDownloading {downloads.describe(written, total)}", end="", file=sys.stderr,
              flush=True)

    return show


def run_check(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="dbx-s3-viewer check",
        description="Test your login, your warehouse and LIST on one folder, and print "
        "what came back. Pick a folder that contains at least one subfolder, so that "
        "the output shows how LIST marks folders. The command runs only LIST.",
    )
    parser.add_argument("path", help="An s3:// folder that contains subfolders.")
    _add_common(parser, demo=False)
    ns = parser.parse_args(argv)
    try:
        settings = _settings(ns)
    except ViewerError as exc:
        print_error(exc)
        return _exit_code(exc)

    found = "found" if settings.config_path.exists() else "not found"
    print(f"Config file:         {settings.config_path} ({found})")
    print(f"Profile:             {settings.profile or 'SDK default'}")
    source = f" (from {settings.warehouse_source})" if settings.warehouse_source else ""
    print(f"Warehouse ID:        {settings.warehouse_id or '-'}{source}")
    if not settings.warehouse_id:
        print_error(missing_warehouse_error(settings.config_path))
        return EXIT_USAGE
    backend = make_backend(settings, demo=False)
    assert isinstance(backend, DatabricksBackend)

    try:
        locations = backend.locations()
        print(f"External locations:  {len(locations)} visible")
    except ViewerError as exc:
        print(f"External locations:  not available ({exc.title}: {exc.message})")

    try:
        probe = backend.probe(ns.path)
    except ViewerError as exc:
        print_error(exc)
        return _exit_code(exc)
    except KeyboardInterrupt:
        print("dbx-s3-viewer: interrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED

    print(f"Warehouse state:     {probe.warehouse_state}")
    print(f"LIST {probe.directory!r} LIMIT 5: OK")
    print(f"  Columns:           {', '.join(probe.columns)}")
    if probe.has_is_directory_column:
        print("  Folder detection:  is_directory column found. The tool uses it.")
    else:
        print("  Folder detection:  no is_directory column. The tool treats a row as a "
              "folder when name or path ends with '/'.")
    print(f"  Rows:              {len(probe.sample_rows)}, of which {probe.rows_ending_in_slash} "
          f"end with '/' and {probe.rows_detected_as_dirs} count as folders")
    for row in probe.sample_rows:
        print(f"    {json.dumps(row, ensure_ascii=False)}")
    if probe.marker_accepted:
        print("LIST :url (parameter marker): accepted")
    else:
        print("LIST :url (parameter marker): rejected")
        print(f"  Server message:    {probe.marker_error.strip()[:500]}")
    return EXIT_OK


def run_browser(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="dbx-s3-viewer",
        description="Browse S3 paths in Databricks Unity Catalog. "
        "Press ? in the browser for the keys.",
        epilog="Other commands: `dbx-s3-viewer ls s3://path [--json]` prints one listing. "
        "`dbx-s3-viewer get s3://path/file [-o PATH]` downloads one file. "
        "`dbx-s3-viewer check s3://path` tests LIST on your warehouse. "
        f"Config file: {default_config_path()}",
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="Open the browser at this s3:// folder. Default: start_folder in the config "
        "file (press * in the browser to set it), or the home screen.",
    )
    parser.add_argument("--version", action="version", version=f"dbx-s3-viewer {__version__}")
    _add_common(parser)
    ns = parser.parse_args(argv)
    try:
        settings = _settings(ns)
        start = paths.as_dir(paths.normalize(ns.path)) if ns.path else settings.start_folder
    except ViewerError as exc:
        print_error(exc)
        return _exit_code(exc)

    from .clipboard import copy_text
    from .tui.app import ViewerApp

    def store(bookmarks: list[str]) -> None:
        save_bookmarks(settings.config_path, bookmarks)

    def store_start(folder: str | None) -> None:
        save_start_folder(settings.config_path, folder)

    app = ViewerApp(
        backend=make_backend(settings, demo=ns.demo),
        bookmarks=list(settings.bookmarks),
        save_bookmarks=None if ns.demo else store,
        start_path=start,
        start_folder=settings.start_folder,
        save_start_folder=None if ns.demo else store_start,
        settings=settings,
        copy=copy_text,
    )
    app.run()
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line. Returns the exit code."""
    _silence_logging()
    args = list(sys.argv[1:] if argv is None else argv)
    commands = {"ls": run_ls, "get": run_get, "check": run_check}
    index = _command_index(args, commands)
    if index is None:
        return run_browser(args)
    return commands[args[index]](args[:index] + args[index + 1:])


#: Options that take a value. The word after them is a value, not a command.
_VALUE_OPTIONS = {"--profile", "--warehouse-id", "--limit", "-o", "--output"}


def _command_index(args: Sequence[str], commands: Container[str]) -> int | None:
    """Return the position of the subcommand, or None for the browser.

    The subcommand is the first word that is not an option or an option
    value. So `dbx-s3-viewer --demo ls s3://b/` runs `ls`, and
    `dbx-s3-viewer --profile ls s3://b/` opens the browser with the profile "ls".
    """
    skip = False
    for index, arg in enumerate(args):
        if skip:
            skip = False
        elif arg == "--":
            return None
        elif arg.startswith("-"):
            skip = arg in _VALUE_OPTIONS
        else:
            return index if arg in commands else None
    return None

"""Save a downloaded file to disk, and pick safe file names.

`save` writes the bytes to "<name>.part" next to the target, and renames
the file to its real name only when all bytes have arrived and the size is
correct. If the download fails or you cancel it, `save` deletes the .part
file. It never replaces a file that exists.

Example:

    content = backend.read_file("s3://b/report.csv")
    save(content, Path("~/Downloads/report.csv").expanduser())
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

import platformdirs

from .errors import BackendError, DownloadCancelled, DownloadError
from .model import FileContent, human_size

#: Called with (bytes written so far, total bytes).
BytesFn = Callable[[int, int], None]

_WINDOWS_BAD = '<>:"/\\|?*'
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                     *(f"LPT{i}" for i in range(1, 10))}


def default_download_dir() -> Path:
    """Return the Downloads folder of the user, for example ~/Downloads."""
    return platformdirs.user_downloads_path()


def safe_filename(name: str, *, windows: bool | None = None) -> str:
    """Make an S3 object name usable as a local file name.

    On every platform, the function replaces "/" and control characters
    with "_". On Windows, it also replaces < > : " \\ | ? * and changes
    reserved names such as "CON".

    Example: safe_filename("run:01.csv", windows=True) returns "run_01.csv".
    """
    windows = os.name == "nt" if windows is None else windows
    bad = _WINDOWS_BAD if windows else "/"
    clean = "".join("_" if ch in bad or ord(ch) < 32 or ord(ch) == 127 else ch for ch in name)
    if windows:
        clean = clean.rstrip(" .")
        if clean.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
            clean = "_" + clean
    if clean in ("", ".", ".."):
        clean = "download"
    return clean


def _taken(path: Path) -> bool:
    """Return True if a file, folder, link (even a broken one) or .part file uses the name."""
    return path.exists() or path.is_symlink() or part_path(path).exists()


def free_target(directory: Path, name: str) -> Path:
    """Return directory/name, or "name (1).ext", "name (2).ext"... if that name is taken.

    A leading dot stays with the name: ".config.json" becomes ".config (1).json".
    """
    lead = "." if name.startswith(".") else ""
    stem, dot, suffix = name[len(lead):].partition(".")
    candidate = directory / name
    number = 1
    while _taken(candidate):
        candidate = directory / f"{lead}{stem} ({number}){dot}{suffix}"
        number += 1
    return candidate


def check_target(target: Path) -> Path:
    """Make sure the tool can save to `target`. Return the full path, or raise DownloadError."""
    target = target.expanduser()
    if target.exists() or target.is_symlink():
        raise DownloadError(
            f"{target} already exists.",
            hint="dbx-s3-viewer never replaces a file. Choose another name, or move the "
            "file away first.",
        )
    if part_path(target).exists():
        # Check now, before the warehouse runs the download queries.
        raise DownloadError(
            f"{part_path(target)} exists. Another download may be running, or one stopped "
            "without cleaning up.",
            hint="Wait for the other download, or remove the .part file, or choose another name.",
        )
    if not target.parent.is_dir():
        raise DownloadError(
            f"The folder {target.parent} does not exist.",
            hint="Create the folder first, or choose another place.",
        )
    if not os.access(target.parent, os.W_OK):
        raise DownloadError(
            f"You cannot write to {target.parent}.",
            hint="Choose another folder.",
        )
    return target


def part_path(target: Path) -> Path:
    """Return the temporary path for a download: the target name plus ".part"."""
    return target.with_name(target.name + ".part")


def save(
    content: FileContent,
    target: Path,
    *,
    on_bytes: BytesFn | None = None,
    cancel: threading.Event | None = None,
) -> int:
    """Write the file to `target` and return the number of bytes.

    The function opens the .part file when the first piece arrives, so a
    slow warehouse start leaves no file behind. It raises DownloadCancelled
    when `cancel` is set, and DownloadError for local disk problems.
    """
    target = check_target(target)
    temporary = part_path(target)
    handle = None
    written = 0
    finished = False

    def stop_if_cancelled() -> None:
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled("You cancelled the download.",
                                    hint="The partial file was removed.")

    try:
        for piece in content.pieces:
            stop_if_cancelled()
            if handle is None:
                handle = _open_new(temporary)
            handle.write(piece)
            written += len(piece)
            if on_bytes:
                on_bytes(written, content.size)
        stop_if_cancelled()
        if handle is None:
            handle = _open_new(temporary)  # an empty file
        handle.close()
        if written != content.size:
            raise BackendError(
                f"The download of {content.path} is incomplete: {written} of "
                f"{content.size} bytes arrived.",
                hint="The tool removed the partial file. Try again.",
            )
        if target.exists():
            raise DownloadError(f"{target} appeared while the download ran.",
                                hint="Choose another name and try again.")
        os.replace(temporary, target)
        finished = True
        return written
    except OSError as exc:
        raise DownloadError(
            f"Cannot write {target}.",
            hint="Check the free disk space and that you can write to the folder.",
            detail=str(exc),
        ) from None
    finally:
        # Remove only the .part file that this call created.
        if handle is not None:
            if not handle.closed:
                handle.close()
            if not finished:
                temporary.unlink(missing_ok=True)


def _open_new(path: Path):
    try:
        return open(path, "xb")
    except FileExistsError:
        raise DownloadError(
            f"{path} already exists. Another download may be running.",
            hint="Wait for it, or remove the .part file.",
        ) from None


def describe(written: int, total: int) -> str:
    """Return progress text such as "45% (210.0 MiB of 469.3 MiB)"."""
    percent = 100 if total == 0 else int(written * 100 / total)
    return f"{percent}% ({human_size(written)} of {human_size(total)})"

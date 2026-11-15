"""Check, split and join s3:// paths, and build the SQL statements the tool sends.

A path is a plain string such as "s3://my-bucket/logs/2026/". A path that
ends with "/" is a directory. The tool always lists directories, so it adds
the slash before it sends a path to Databricks.

The tool sends three kinds of statement, and this module builds all of them:
`LIST` to list a folder, and two `SELECT ... FROM read_files(...)`
statements to download a file. None of them changes data.

Example:

    >>> parent("s3://my-bucket/logs/2026/")
    's3://my-bucket/logs/'
    >>> list_statement("s3://my-bucket/it's here", 1001)
    "LIST 's3://my-bucket/it\\\\'s here/' LIMIT 1001"
"""

from __future__ import annotations

import operator

from .errors import InvalidPathError

SCHEME = "s3://"


def normalize(raw: str) -> str:
    """Return a clean s3:// path, or raise InvalidPathError.

    The function removes spaces at the start and end, and writes the scheme
    in lower case. It keeps a trailing slash if there is one.

    Example: normalize("  S3://logs/2026 ") returns "s3://logs/2026", and
    normalize("s3://logs/2026///") returns "s3://logs/2026/".
    """
    text = raw.strip()
    if not text.lower().startswith(SCHEME):
        raise InvalidPathError(
            f"{text!r} is not an s3:// path.",
            hint="Type a full path, for example s3://my-bucket/folder/.",
        )
    text = SCHEME + text[len(SCHEME):]
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise InvalidPathError(
            "The path contains a control character (for example a line break).",
            hint="Remove the character and try again.",
        )
    name = bucket(text)
    if not name or any(ch.isspace() for ch in name):
        raise InvalidPathError(
            f"{text!r} has no valid bucket name.",
            hint="Type a full path, for example s3://my-bucket/folder/.",
        )
    if text.endswith("//"):
        # "s3://b/a///" and "s3://b/a/" are the same folder. One form keeps
        # the cache and the bookmarks consistent.
        text = text.rstrip("/") + "/"
    return text


def bucket(path: str) -> str:
    """Return the bucket name. Example: bucket("s3://b/a/") returns "b"."""
    return path[len(SCHEME):].split("/", 1)[0]


def as_dir(path: str) -> str:
    """Add a trailing slash if the path has none."""
    return path if path.endswith("/") else path + "/"


def parent(path: str) -> str | None:
    """Return the parent directory, or None for a bucket root.

    Examples:
        parent("s3://b/a/c.csv") returns "s3://b/a/".
        parent("s3://b/a/") returns "s3://b/".
        parent("s3://b/") returns None.
    """
    rest = path[len(SCHEME):].rstrip("/")
    if "/" not in rest:
        return None
    return SCHEME + rest.rsplit("/", 1)[0] + "/"


def join(directory: str, name: str, *, is_dir: bool = False) -> str:
    """Join a directory and a child name.

    Example: join("s3://b/a", "logs", is_dir=True) returns "s3://b/a/logs/".
    """
    child = name.strip("/")
    return as_dir(directory) + child + ("/" if is_dir else "")


def basename(path: str) -> str:
    """Return the last part of a path, without slashes.

    Examples: basename("s3://b/a/logs/") returns "logs".
    basename("s3://b/") returns "b".
    """
    return path[len(SCHEME):].rstrip("/").rsplit("/", 1)[-1]


def escape_sql_string(text: str) -> str:
    r"""Escape text for a single-quoted Databricks SQL string literal.

    Databricks SQL uses a backslash as the escape character, so a single
    quote becomes \' and a backslash becomes \\. The function escapes
    backslashes first, so that sequences such as \n stay as two characters.
    Doubling the quote ('') is not safe: Databricks Runtime 18 joins
    adjacent literals, so 'a''b' means "ab".

    Example: escape_sql_string("it's") returns "it\\'s".
    """
    return text.replace("\\", "\\\\").replace("'", "\\'")


#: Download statements cut a file into pieces of this many bytes, one row each.
PIECE_BYTES = 4 * 1024 * 1024
#: The start of each statement the tool may send. `_execute` refuses all others.
STAT_PREFIX = "SELECT count(*) AS files,"
READ_PREFIX = "SELECT i, base64(substr(content,"
ALLOWED_PREFIXES = ("LIST ", STAT_PREFIX, READ_PREFIX)
_GLOB_CHARS = "*?[]{}"


def check_file_path(path: str) -> str:
    """Return the clean path of a file to download, or raise InvalidPathError.

    The path must not end with "/". It must not contain * ? [ ] { },
    because read_files treats these characters as wildcards and could
    read other files.
    """
    clean = normalize(path)
    if clean.endswith("/") or is_bucket_root(clean):
        raise InvalidPathError(
            f"{clean} is a folder. dbx-s3-viewer downloads one file at a time.",
            hint="Open the folder and select a file.",
        )
    found = sorted({ch for ch in clean if ch in _GLOB_CHARS})
    if found:
        raise InvalidPathError(
            f"dbx-s3-viewer cannot download {clean}, because its name contains "
            f"{' '.join(found)}.",
            hint="read_files treats these characters as wildcards, so it could read other "
            "files. Download the file in a notebook instead.",
        )
    return clean


def is_bucket_root(path: str) -> bool:
    """Return True for a bucket root such as "s3://b" or "s3://b/"."""
    return parent(path) is None


def file_stat_statement(path: str) -> str:
    """Build the statement that counts the files at a path and adds up their sizes.

    `read_files` reads all files under a folder, so the tool runs this first
    and downloads only when exactly one file matches. Example:

        SELECT count(*) AS files, coalesce(sum(length), 0) AS bytes
        FROM read_files('s3://b/a.csv', format => 'binaryFile')
    """
    literal = escape_sql_string(check_file_path(path))
    return (
        f"{STAT_PREFIX} coalesce(sum(length), 0) AS bytes "
        f"FROM read_files('{literal}', format => 'binaryFile')"
    )


def file_read_statement(path: str, piece: int | None = None) -> str:
    """Build the statement that returns a file as numbered base64 pieces.

    Each row is (i, base64 text of bytes i*piece to (i+1)*piece). Rows come
    in order of i. An empty file gives one row with an empty piece.
    """
    size = operator.index(piece if piece is not None else PIECE_BYTES)
    if size < 1:
        raise ValueError("piece must be 1 or more")
    literal = escape_sql_string(check_file_path(path))
    return (
        f"{READ_PREFIX} i * {size} + 1, {size})) AS piece "
        f"FROM read_files('{literal}', format => 'binaryFile') "
        f"LATERAL VIEW explode(sequence(0, greatest(CAST(ceil(length / {size}) AS INT) - 1, 0))) "
        "pieces AS i ORDER BY i"
    )


def list_statement(path: str, limit: int) -> str:
    """Build the LIST statement for a directory.

    The statement is always `LIST '<directory>' LIMIT <n>`. The path goes in
    as an escaped string literal, because the Databricks docs do not say
    that LIST accepts a parameter marker.
    """
    limit = operator.index(limit)
    if limit < 1:
        raise ValueError("limit must be 1 or more")
    directory = as_dir(normalize(path))
    return f"LIST '{escape_sql_string(directory)}' LIMIT {limit}"

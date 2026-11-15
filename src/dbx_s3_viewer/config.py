"""Read the config file, and combine it with command-line flags and environment variables.

The config file is TOML. It lives in the user config directory of the
platform, or at the path in DBX_S3_VIEWER_CONFIG:

- macOS: ~/Library/Application Support/dbx-s3-viewer/config.toml
- Linux: ~/.config/dbx-s3-viewer/config.toml
- Windows: %LOCALAPPDATA%\\dbx-s3-viewer\\config.toml

Example:

    warehouse_id = "1234567890abcdef"
    profile = "work"
    list_limit = 1000
    timeout_seconds = 600
    bookmarks = ["s3://my-bucket/landing/"]
    start_folder = "s3://my-bucket/landing/"
    download_dir = "~/Downloads"

dbx-s3-viewer writes the config file when you add or remove a bookmark,
or set or clear the start folder.
The only other files it writes are the downloads that you ask for.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs
import tomlkit
from tomlkit.exceptions import TOMLKitError

from . import paths
from .errors import ConfigError, InvalidPathError

APP_NAME = "dbx-s3-viewer"
CONFIG_ENV = "DBX_S3_VIEWER_CONFIG"
WAREHOUSE_ENV = "DATABRICKS_WAREHOUSE_ID"
PROFILE_ENV = "DATABRICKS_CONFIG_PROFILE"
DEFAULT_LIMIT = 1000
DEFAULT_TIMEOUT = 600

_KEYS = {
    "warehouse_id", "profile", "list_limit", "timeout_seconds", "bookmarks", "start_folder",
    "download_dir",
}

_NEW_FILE_HEADER = """\
# dbx-s3-viewer config. See the README for all keys.
# warehouse_id = "1234567890abcdef"
# profile = "DEFAULT"
# list_limit = 1000
# timeout_seconds = 600
# download_dir = "~/Downloads"
# start_folder = "s3://my-bucket/landing/"
"""


def default_config_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the config file path: DBX_S3_VIEWER_CONFIG, or the platform default."""
    env = os.environ if env is None else env
    override = env.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return platformdirs.user_config_path(APP_NAME, appauthor=False) / "config.toml"


@dataclass
class FileConfig:
    """The values in the config file. A key that is not in the file is None."""

    path: Path
    warehouse_id: str | None = None
    profile: str | None = None
    list_limit: int | None = None
    timeout_seconds: int | None = None
    bookmarks: list[str] = field(default_factory=list)
    start_folder: str | None = None
    download_dir: Path | None = None


def _s3_folder(raw: str, what: str, path: Path) -> str:
    """Check one s3:// folder from the file, and return it with a trailing slash."""
    try:
        return paths.as_dir(paths.normalize(raw))
    except InvalidPathError as exc:
        raise ConfigError(
            f"The {what} {raw!r} in {path} is not a valid s3:// path.",
            hint="Fix or remove it.",
            detail=exc.message,
        ) from None


def _positive_int(data: Mapping[str, object], key: str, path: Path) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(
            f"{key} in {path} must be a whole number of 1 or more.",
            hint=f"For example: {key} = {DEFAULT_LIMIT if key == 'list_limit' else DEFAULT_TIMEOUT}",
        )
    return value


def _string(data: Mapping[str, object], key: str, path: Path) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f'{key} in {path} must be a string in quotes, for example {key} = "abc".')
    return value.strip() or None


def _directory(data: Mapping[str, object], key: str, path: Path) -> Path | None:
    value = _string(data, key, path)
    return Path(value).expanduser() if value else None


def load(path: Path) -> FileConfig:
    """Read the config file. A missing file gives an empty config."""
    if not path.exists():
        return FileConfig(path)
    try:
        data = tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
    except (OSError, UnicodeDecodeError, TOMLKitError) as exc:
        raise ConfigError(
            f"Cannot read the config file {path}.",
            hint="Fix the TOML syntax, or move the file away to start with defaults.",
            detail=str(exc),
        ) from None
    unknown = sorted(set(data) - _KEYS)
    if unknown:
        raise ConfigError(
            f"Unknown key {unknown[0]!r} in {path}.",
            hint="Valid keys are: " + ", ".join(sorted(_KEYS)) + ".",
        )
    raw_bookmarks = data.get("bookmarks", [])
    if not isinstance(raw_bookmarks, list) or not all(isinstance(b, str) for b in raw_bookmarks):
        raise ConfigError(
            f"bookmarks in {path} must be a list of strings.",
            hint='For example: bookmarks = ["s3://my-bucket/landing/"]',
        )
    bookmarks = [_s3_folder(raw, "bookmark", path) for raw in raw_bookmarks]
    start_folder = _string(data, "start_folder", path)
    return FileConfig(
        path=path,
        warehouse_id=_string(data, "warehouse_id", path),
        profile=_string(data, "profile", path),
        list_limit=_positive_int(data, "list_limit", path),
        timeout_seconds=_positive_int(data, "timeout_seconds", path),
        bookmarks=list(dict.fromkeys(bookmarks)),
        start_folder=_s3_folder(start_folder, "start_folder", path) if start_folder else None,
        download_dir=_directory(data, "download_dir", path),
    )


@dataclass(frozen=True)
class Settings:
    """The values the tool runs with, after flags, environment and config file."""

    config_path: Path
    profile: str | None
    warehouse_id: str | None
    warehouse_source: str
    list_limit: int
    timeout_seconds: int
    bookmarks: tuple[str, ...]
    #: The folder the browser opens when you give no path. None means the home screen.
    start_folder: str | None = None
    #: Where downloads go by default. None means the Downloads folder of the user.
    download_dir: Path | None = None


def resolve(
    file_config: FileConfig,
    *,
    profile_flag: str | None = None,
    warehouse_flag: str | None = None,
    limit_flag: int | None = None,
    env: Mapping[str, str] | None = None,
) -> Settings:
    """Combine the sources. A flag wins over the environment, and both win over the file.

    Profile: --profile, then DATABRICKS_CONFIG_PROFILE, then `profile` in
    the file, then the SDK default.
    Warehouse ID: --warehouse-id, then DATABRICKS_WAREHOUSE_ID, then
    `warehouse_id` in the file.
    """
    env = os.environ if env is None else env
    if profile_flag:
        profile = profile_flag
    elif env.get(PROFILE_ENV):
        # The SDK would read the variable itself. The tool passes it on, so
        # that `check`, the help screen and login hints can show the name.
        profile = env[PROFILE_ENV]
    else:
        profile = file_config.profile

    warehouse, source = None, ""
    for value, label in (
        (warehouse_flag, "--warehouse-id"),
        (env.get(WAREHOUSE_ENV), WAREHOUSE_ENV),
        (file_config.warehouse_id, "config file"),
    ):
        if value and value.strip():
            warehouse, source = value.strip(), label
            break

    if limit_flag is not None and limit_flag < 1:
        raise ConfigError("--limit must be 1 or more.")
    return Settings(
        config_path=file_config.path,
        profile=profile,
        warehouse_id=warehouse,
        warehouse_source=source,
        list_limit=limit_flag or file_config.list_limit or DEFAULT_LIMIT,
        timeout_seconds=file_config.timeout_seconds or DEFAULT_TIMEOUT,
        bookmarks=tuple(file_config.bookmarks),
        start_folder=file_config.start_folder,
        download_dir=file_config.download_dir,
    )


def save_bookmarks(path: Path, bookmarks: list[str]) -> None:
    """Write the bookmark list to the config file, and keep all other content."""
    array = tomlkit.array()
    array.extend(bookmarks)
    array.multiline(True)

    def change(doc: tomlkit.TOMLDocument) -> None:
        doc["bookmarks"] = array

    _update_file(path, change)


def save_start_folder(path: Path, folder: str | None) -> None:
    """Write the start folder to the config file. None removes the key."""

    def change(doc: tomlkit.TOMLDocument) -> None:
        if folder is not None:
            doc["start_folder"] = folder
        elif "start_folder" in doc:
            del doc["start_folder"]

    _update_file(path, change)


def _update_file(path: Path, change: Callable[[tomlkit.TOMLDocument], None]) -> None:
    """Change one key in the config file, and keep all other content.

    The function keeps comments and other keys. It writes a temporary file
    in the same directory and then replaces the config file, so a crash
    cannot leave a half-written file.
    """
    if path.exists():
        try:
            doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, TOMLKitError) as exc:
            raise ConfigError(
                f"Cannot update {path}, because the tool cannot read it.",
                hint="Fix the TOML syntax, then try again.",
                detail=str(exc),
            ) from None
    else:
        doc = tomlkit.parse(_NEW_FILE_HEADER)
    change(doc)
    text = tomlkit.dumps(doc)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise ConfigError(
            f"Cannot write the config file {path}.",
            hint="Check that the directory exists and that you can write to it.",
            detail=str(exc),
        ) from None

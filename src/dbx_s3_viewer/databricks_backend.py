"""List and download S3 paths through a Databricks SQL warehouse.

The backend sends statements through the SQL Statement Execution API:

- `LIST '<path>' LIMIT <n>` to list a folder.
- Two `SELECT ... FROM read_files('<path>', format => 'binaryFile')`
  statements to download one file: the first counts the matching files,
  the second returns the bytes as base64 pieces.

`paths.py` builds all of them, and `_execute` refuses any other statement.
The other REST calls are:

- `get_statement`, `get_statement_result_chunk_n` and `cancel_execution`,
  for the statements above.
- `warehouses.get`, to see if the warehouse is starting.
- `external_locations.list`, for the home screen.
- A plain HTTPS GET of the result links that Databricks gives for large
  downloads (EXTERNAL_LINKS). The tool sends no Databricks token with it.

The SDK handles login. This module never reads, stores or logs a token.
"""

from __future__ import annotations

import base64
import binascii
import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

import requests
from databricks.sdk import errors as sdk_errors
from databricks.sdk.service.sql import (
    Disposition,
    ExecuteStatementRequestOnWaitTimeout,
    Format,
    State,
    StatementParameterListItem,
    StatementResponse,
    StatementState,
)

from . import paths
from .backend import ProgressFn
from .errors import (
    BackendError,
    ConfigError,
    DownloadCancelled,
    DownloadError,
    InvalidPathError,
    ListTimeoutError,
    PathNotFoundError,
    PermissionDeniedError,
    UnreachableError,
    ViewerError,
    auth_error,
    missing_warehouse_error,
)
from .model import FileContent, Listing, Location, human_size, is_directory, parse_rows

T = TypeVar("T")

#: How long execute_statement waits, at most, before it returns a PENDING or
#: RUNNING state. The API accepts 5 to 50 seconds.
WAIT_TIMEOUT_SECONDS = 30
#: Check the warehouse state again after this many idle seconds, because a
#: warehouse can stop on its own when it is idle.
STATE_RECHECK_SECONDS = 600
#: The column names that the LIST docs give, used if the result has no schema.
DEFAULT_COLUMNS = ("path", "name", "size", "modification_time")

MSG_CONNECTING = "Connecting to Databricks…"
MSG_LISTING = "Listing…"
MSG_STARTING = "SQL warehouse is starting. This can take a few minutes…"
MSG_WAITING = "Waiting for the SQL warehouse. It may be starting or busy…"
MSG_CHECKING = "Checking the file…"
MSG_READING = "Reading the file on the SQL warehouse…"

#: Files up to this size come back inside the API response (INLINE). INLINE
#: results are limited to 25 MiB, and base64 makes the data 4/3 larger.
INLINE_MAX_BYTES = 12 * 1024 * 1024
#: read_files with binaryFile cannot read larger files.
MAX_FILE_BYTES = 2**31 - 1
#: Seconds to wait for one result link (EXTERNAL_LINKS).
LINK_TIMEOUT = 120
#: How long the SDK retries a failed request (for example, a host that does
#: not answer) before it gives up. The SDK default is 300 seconds.
SDK_RETRY_SECONDS = 30

_PERMISSION_MARKERS = (
    "PERMISSION_DENIED",
    "INSUFFICIENT_PERMISSION",
    "UNAUTHORIZED_ACCESS",
    "ACCESS DENIED",
    "ACCESSDENIED",
    "DOES NOT HAVE",
    "NOT AUTHORIZED",
    "FORBIDDEN",
)
_NOT_FOUND_MARKERS = (
    "PATH_NOT_FOUND",
    "FILENOTFOUNDEXCEPTION",
    "NO SUCH FILE",
    "DOES NOT EXIST",
    "NOSUCHBUCKET",
    "NOSUCHKEY",
)


def classify_statement_error(
    code: str | None,
    message: str | None,
    path: str,
    action: str = "list",
    profile: str | None = None,
) -> ViewerError:
    """Turn a failed statement into an error with a next step.

    `action` is "list" or "read", for the message. `profile` goes into the
    login hint.

    `code` is the ServiceErrorCode name, for example "BAD_REQUEST".
    `message` is the server text. The function removes the path from the
    text before it looks for markers, so a folder named "access-denied"
    does not change the result. It removes the path without its scheme
    too, because Spark often writes s3a:// in place of s3://.
    """
    text = message or ""
    key = path[len(paths.SCHEME):] if path.startswith(paths.SCHEME) else path
    scrubbed = text
    for form in (path, path.rstrip("/"), key, key.rstrip("/")):
        if form:
            scrubbed = scrubbed.replace(form, " ")
    scrubbed = scrubbed.upper()
    if code == "UNAUTHENTICATED":
        return auth_error(profile, text)
    if code == "DEADLINE_EXCEEDED":
        return ListTimeoutError(
            f"Databricks stopped the {action} of {path} because it took too long.",
            hint="Try again. A folder with very many objects can be slow.",
            detail=text,
        )
    if any(marker in scrubbed for marker in _PERMISSION_MARKERS):
        return PermissionDeniedError(
            f"You do not have permission to {action} {path}.",
            hint=(
                "Ask a Unity Catalog admin for READ FILES on the external location "
                "that contains this path. The home screen shows the external "
                "locations you can see."
            ),
            detail=text,
        )
    if any(marker in scrubbed for marker in _NOT_FOUND_MARKERS):
        return PathNotFoundError(
            f"{path} does not exist.",
            hint="Check the spelling, or go up one level and browse to the folder.",
            detail=text,
        )
    return BackendError(
        f"Databricks could not {action} {path}.",
        hint="Read the server message, then try again.",
        detail=text,
    )


@dataclass
class ProbeResult:
    """What `dbx-s3-viewer check` found out about LIST on a real warehouse."""

    directory: str
    warehouse_state: str
    columns: list[str]
    sample_rows: list[dict[str, Any]]
    has_is_directory_column: bool
    rows_ending_in_slash: int
    rows_detected_as_dirs: int
    marker_accepted: bool | None = None
    marker_error: str = ""
    marker_columns: list[str] = field(default_factory=list)


class DatabricksBackend:
    """The real backend. It runs LIST on a SQL warehouse."""

    label = "databricks"

    def __init__(
        self,
        *,
        profile: str | None,
        warehouse_id: str | None,
        limit: int = 1000,
        timeout_seconds: float = 600,
        config_file: object = "the config file",
        client_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        http_get: Callable[..., Any] = requests.get,
    ) -> None:
        self._profile = profile
        self._warehouse_id = warehouse_id
        self._limit = limit
        self._timeout = timeout_seconds
        self._config_file = config_file
        self._factory = client_factory or self._default_client
        self._sleep = sleep
        self._clock = clock
        self._http_get = http_get
        self._lock = threading.Lock()
        self._client: Any = None
        self._last_ok: float | None = None

    # -- public interface -------------------------------------------------

    def list(self, path: str, progress: ProgressFn | None = None) -> Listing:
        report = progress or (lambda _msg: None)
        directory = paths.as_dir(paths.normalize(path))
        if not self._warehouse_id:
            raise missing_warehouse_error(self._config_file)
        # Ask for one row more than the limit. If it comes back, the listing is truncated.
        statement = paths.list_statement(directory, self._limit + 1)
        ws = self._connect(report)
        self._check_warehouse(ws, report)
        response = self._execute(ws, statement, report, directory)
        columns = _column_names(response)
        rows = self._rows(ws, response, directory)
        manifest_truncated = bool(response.manifest and response.manifest.truncated)
        entries = parse_rows(columns, rows[: self._limit], directory)
        truncated = len(rows) > self._limit or manifest_truncated
        return Listing(directory, tuple(entries), truncated, self._limit)

    def locations(self) -> list[Location]:
        ws = self._connect(lambda _msg: None)

        def fetch() -> list[Location]:
            found = [
                Location(info.name or info.url, info.url, "external location")
                for info in ws.external_locations.list()
                if info.url
            ]
            return sorted(found, key=lambda loc: loc.name.casefold())

        def forbidden(exc: Exception) -> ViewerError:
            return PermissionDeniedError(
                "You do not have permission to list external locations.",
                hint="The home screen shows your bookmarks only.",
                detail=str(exc),
            )

        return self._call(fetch, forbidden=forbidden, subject="external locations")

    def read_file(
        self,
        path: str,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> FileContent:
        """Prepare a download of one file.

        The function runs the count statement now. It runs the read
        statement only when you iterate over `FileContent.pieces`. When
        `cancel` is set, the statement that runs stops, and Databricks
        cancels it.
        """
        report = progress or (lambda _msg: None)
        target = paths.check_file_path(path)
        if not self._warehouse_id:
            raise missing_warehouse_error(self._config_file)
        ws = self._connect(report)
        self._check_warehouse(ws, report)
        response = self._execute(ws, paths.file_stat_statement(target), report, target,
                                 running=MSG_CHECKING, action="read", cancel=cancel)
        files, size = _stat_row(self._rows(ws, response, target))
        if files == 0:
            hidden = paths.basename(target).startswith(("_", "."))
            raise PathNotFoundError(
                f"{target} does not exist, or read_files cannot see it.",
                hint=(
                    "read_files may skip files whose names start with _ or ., like "
                    "_SUCCESS. Download those in a notebook."
                    if hidden else "Refresh the folder listing, then try again."
                ),
            )
        if files > 1:
            raise InvalidPathError(
                f"{target} is a folder with {files} files, not one file.",
                hint="dbx-s3-viewer downloads one file at a time. Open the folder and "
                "select a file.",
            )
        if size > MAX_FILE_BYTES:
            raise DownloadError(
                f"{target} is {human_size(size)}. read_files can return files up to 2 GiB only.",
                hint="Copy the file with a notebook or a job instead.",
            )
        inline = size <= INLINE_MAX_BYTES

        def pieces() -> Iterator[bytes]:
            read = self._execute(
                ws, paths.file_read_statement(target), report, target,
                disposition=Disposition.INLINE if inline else Disposition.EXTERNAL_LINKS,
                running=MSG_READING, action="read", cancel=cancel,
            )
            expected = 0
            for row in self._iter_rows(ws, read, target):
                index, data = (row + [None, None])[:2]
                if index is None or int(index) != expected:
                    raise BackendError(
                        f"The pieces of {target} came back out of order.",
                        hint="The tool removed the partial file. Try again.",
                        detail=f"expected piece {expected}, got {index}",
                    )
                expected += 1
                try:
                    yield decode_piece(data)
                except (binascii.Error, ValueError) as exc:
                    raise BackendError(
                        f"Piece {index} of {target} is not valid base64.",
                        hint="The tool removed the partial file. Try again. If it fails "
                        "again, report the message below.",
                        detail=f"{exc} (piece length: {len(data or '')} characters)",
                    ) from None

        return FileContent(target, size, pieces())

    def probe(self, path: str) -> ProbeResult:
        """Run LIST twice and report what came back. Used by `dbx-s3-viewer check`.

        The first run uses a string literal, like the browser does. The
        second run uses a named parameter marker (`LIST :url`), to find out
        if the warehouse accepts one. Both are LIST statements.
        """
        directory = paths.as_dir(paths.normalize(path))
        if not self._warehouse_id:
            raise missing_warehouse_error(self._config_file)
        ws = self._connect(lambda _msg: None)
        state = self._warehouse_state(ws)
        response = self._execute(ws, paths.list_statement(directory, 5), lambda _m: None, directory)
        columns = _column_names(response)
        rows = self._rows(ws, response, directory)
        dict_rows = [dict(zip([c.lower() for c in columns], row)) for row in rows]
        result = ProbeResult(
            directory=directory,
            warehouse_state=state,
            columns=list(columns),
            sample_rows=dict_rows,
            has_is_directory_column="is_directory" in [c.lower() for c in columns],
            rows_ending_in_slash=sum(
                1
                for r in dict_rows
                if (r.get("name") or "").endswith("/") or (r.get("path") or "").endswith("/")
            ),
            rows_detected_as_dirs=sum(1 for r in dict_rows if is_directory(r)),
        )
        marker = [StatementParameterListItem(name="url", value=directory, type="STRING")]
        try:
            marker_response = self._execute(
                ws, "LIST :url LIMIT 5", lambda _m: None, directory, parameters=marker
            )
        except ViewerError as exc:
            result.marker_accepted = False
            result.marker_error = exc.detail or exc.message
        else:
            result.marker_accepted = True
            result.marker_columns = list(_column_names(marker_response))
        return result

    # -- connection -------------------------------------------------------

    def _default_client(self) -> Any:
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.config import Config

        return WorkspaceClient(
            config=Config(profile=self._profile, retry_timeout_seconds=SDK_RETRY_SECONDS)
        )

    def _connect(self, report: ProgressFn) -> Any:
        with self._lock:
            if self._client is None:
                report(MSG_CONNECTING)
                try:
                    self._client = self._factory()
                except ValueError as exc:
                    # The SDK raises ValueError when it finds no usable login.
                    raise auth_error(self._profile, str(exc)) from None
            return self._client

    def _warehouse_state(self, ws: Any) -> str:
        def not_found(exc: Exception) -> ViewerError:
            return ConfigError(
                f"No SQL warehouse with ID {self._warehouse_id!r} exists in this workspace, "
                "or you cannot see it.",
                hint=(
                    "Check the warehouse ID (--warehouse-id, DATABRICKS_WAREHOUSE_ID or "
                    f"warehouse_id in {self._config_file}) and the workspace in your profile."
                ),
                detail=str(exc),
            )

        try:
            info = self._call(
                lambda: ws.warehouses.get(self._warehouse_id),
                subject="the SQL warehouse",
                not_found=not_found,
            )
        except PermissionDeniedError:
            # Some users can run statements but cannot read warehouse details.
            return "unknown"
        state = getattr(info, "state", None)
        return state.value if isinstance(state, State) else str(state or "unknown")

    def _check_warehouse(self, ws: Any, report: ProgressFn) -> None:
        """Tell the user early if the warehouse must start first."""
        if self._last_ok is not None and self._clock() - self._last_ok < STATE_RECHECK_SECONDS:
            return
        try:
            state = self._warehouse_state(ws)
        except UnreachableError:
            raise  # The next call would wait for the same dead host again.
        except (BackendError, ListTimeoutError):
            # Only a hint for the user. The LIST call reports real problems.
            return
        if state in (State.STOPPED.value, State.STARTING.value, State.STOPPING.value):
            report(MSG_STARTING)

    # -- statements -------------------------------------------------------

    def _execute(
        self,
        ws: Any,
        statement: str,
        report: ProgressFn,
        directory: str,
        parameters: Sequence[StatementParameterListItem] | None = None,
        *,
        disposition: Disposition = Disposition.INLINE,
        running: str = MSG_LISTING,
        action: str = "list",
        cancel: threading.Event | None = None,
    ) -> StatementResponse:
        """Run one statement and wait for it to finish.

        If the wait ends early (timeout, `cancel`, Ctrl+C or an error), the
        function asks Databricks to cancel the statement, so it does not
        keep the warehouse busy.
        """
        if not statement.startswith(paths.ALLOWED_PREFIXES):
            # The tool never changes data. Refuse any statement that paths.py did not build.
            raise RuntimeError("dbx-s3-viewer sends only LIST and read_files statements")
        api = ws.statement_execution
        deadline = self._clock() + self._timeout
        report(running)
        response: StatementResponse = self._call(
            lambda: api.execute_statement(
                statement=statement,
                warehouse_id=self._warehouse_id,
                wait_timeout=wait_timeout(self._timeout),
                on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CONTINUE,
                disposition=disposition,
                format=Format.JSON_ARRAY,
                parameters=list(parameters) if parameters else None,
            ),
            subject=directory,
        )
        statement_id = response.statement_id
        try:
            delay = 1.0
            while _state(response) in (StatementState.PENDING, StatementState.RUNNING):
                _stop_if_cancelled(cancel)
                report(MSG_WAITING if _state(response) is StatementState.PENDING else running)
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise ListTimeoutError(
                        f"No result for {directory} after {self._timeout:.0f} seconds.",
                        hint=(
                            "Check that the SQL warehouse is running (SQL Warehouses in "
                            "Databricks), then try again. To wait longer, raise "
                            f"timeout_seconds in {self._config_file}."
                        ),
                    )
                self._sleep(min(delay, remaining))
                delay = min(delay * 1.5, 5.0)
                _stop_if_cancelled(cancel)
                response = self._call(lambda: api.get_statement(statement_id), subject=directory)
        except BaseException:
            # Timeout, cancel, Ctrl+C or an error while polling: stop the statement.
            self._cancel(ws, statement_id)
            raise
        state = _state(response)
        if state is StatementState.SUCCEEDED:
            self._last_ok = self._clock()
            return response
        error = response.status.error if response.status else None
        if state is StatementState.FAILED:
            code = error.error_code.name if error and error.error_code else None
            raise classify_statement_error(code, error.message if error else "", directory, action,
                                           self._profile)
        raise BackendError(
            f"The {action} of {directory} stopped with state {state.value if state else 'unknown'}.",
            hint="Someone may have cancelled it in Databricks. Try again.",
            detail=(error.message or "") if error else "",
        )

    def _rows(self, ws: Any, response: StatementResponse, directory: str) -> list[list[str | None]]:
        """Collect all rows of a result."""
        return list(self._iter_rows(ws, response, directory))

    def _iter_rows(
        self, ws: Any, response: StatementResponse, directory: str
    ) -> Iterator[list[str | None]]:
        """Yield rows from the first result and any further chunks, one chunk at a time.

        A chunk has its rows inline (`data_array`) or behind a result link
        (`external_links`, for EXTERNAL_LINKS disposition).
        """
        result = response.result
        while result is not None:
            next_index = result.next_chunk_index
            if result.external_links:
                for link in result.external_links:
                    yield from self._fetch_link(link.external_link, link.http_headers)
                    if link.next_chunk_index is not None:
                        next_index = link.next_chunk_index
            else:
                yield from result.data_array or []
            if next_index is None:
                break
            result = self._call(
                lambda: ws.statement_execution.get_statement_result_chunk_n(
                    response.statement_id, next_index
                ),
                subject=directory,
            )

    def _fetch_link(self, url: str | None, headers: dict[str, str] | None) -> list[list[str | None]]:
        """Download one result chunk from its link.

        The link points to cloud storage and holds a short-lived signature,
        so the request must not carry the Databricks token. The tool uses a
        plain request, not the SDK session, and never shows the URL.
        """
        if not url:
            raise BackendError("Databricks returned a result chunk without a link.",
                               hint="Try again.")
        try:
            reply = self._http_get(url, headers=dict(headers or {}), timeout=LINK_TIMEOUT)
            reply.raise_for_status()
            rows = reply.json()
        except (requests.RequestException, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise BackendError(
                "Cannot fetch the file data from Databricks result storage.",
                hint=(
                    "Large downloads (over "
                    f"{human_size(INLINE_MAX_BYTES)}) come through result links, which "
                    "your network may block. Check your VPN or proxy, or ask your "
                    "platform team to allow Databricks result storage (cloud fetch)."
                ),
                detail=f"HTTP {status}" if status else type(exc).__name__,
            ) from None
        if not isinstance(rows, list):
            raise BackendError("A result chunk from Databricks has an unexpected format.",
                               hint="Try again.")
        return rows

    def _cancel(self, ws: Any, statement_id: str | None) -> None:
        if not statement_id:
            return
        try:
            ws.statement_execution.cancel_execution(statement_id)
        except Exception:  # noqa: BLE001 - the timeout error matters more
            pass

    # -- error mapping ----------------------------------------------------

    def _call(
        self,
        fn: Callable[[], T],
        *,
        subject: str,
        forbidden: Callable[[Exception], ViewerError] | None = None,
        not_found: Callable[[Exception], ViewerError] | None = None,
    ) -> T:
        """Call the SDK and turn its exceptions into errors with next steps."""
        try:
            return fn()
        except sdk_errors.Unauthenticated as exc:
            raise auth_error(self._profile, str(exc)) from None
        except sdk_errors.PermissionDenied as exc:
            text = str(exc)
            if _BAD_TOKEN.search(_server_text(text)):
                # Databricks answers 403, not 401, for an invalid or expired token.
                raise auth_error(self._profile, text) from None
            if forbidden:
                raise forbidden(exc) from None
            raise PermissionDeniedError(
                f"You do not have permission to use SQL warehouse {self._warehouse_id!r}.",
                hint="Ask a workspace admin for CAN USE on the warehouse, or pick another one.",
                detail=text,
            ) from None
        except sdk_errors.NotFound as exc:
            if not_found:
                raise not_found(exc) from None
            raise BackendError(
                f"Databricks could not find {subject}.",
                hint="Check the warehouse ID and the workspace in your profile.",
                detail=str(exc),
            ) from None
        except sdk_errors.DeadlineExceeded as exc:
            raise ListTimeoutError(
                f"Databricks took too long to answer for {subject}.",
                hint="Try again. If it keeps happening, check the workspace status.",
                detail=str(exc),
            ) from None
        except sdk_errors.DatabricksError as exc:
            raise BackendError(
                f"Databricks returned an error for {subject}.",
                hint="Read the server message, then try again.",
                detail=str(exc),
            ) from None
        except TimeoutError as exc:
            # The SDK retries network errors, then raises TimeoutError "from"
            # the last error. Look at that error to pick the message.
            if isinstance(exc.__cause__, requests.ConnectionError):
                raise UnreachableError(
                    "Cannot reach the Databricks workspace.",
                    hint="Check your network or VPN, and the host in your Databricks profile.",
                    detail=f"{exc}: {exc.__cause__}",
                ) from None
            raise ListTimeoutError(
                f"Databricks did not answer in time for {subject}.",
                hint="Check your network or VPN, then try again.",
                detail=f"{exc}: {exc.__cause__}" if exc.__cause__ else str(exc),
            ) from None
        except requests.Timeout as exc:
            raise ListTimeoutError(
                f"The network request for {subject} timed out.",
                hint="Check your network or VPN, then try again.",
                detail=str(exc),
            ) from None
        except requests.ConnectionError as exc:
            raise UnreachableError(
                "Cannot reach the Databricks workspace.",
                hint="Check your network or VPN, and the host in your Databricks profile.",
                detail=str(exc),
            ) from None
        except ValueError as exc:
            # The SDK resolves the login lazily, and raises ValueError if it fails.
            text = _server_text(str(exc)).lower()
            if "auth" in text or "credential" in text:
                raise auth_error(self._profile, str(exc)) from None
            raise


_BAD_TOKEN = re.compile(r"invalid (access )?token|token (is |has )?expired|expired token",
                        re.IGNORECASE)


def _server_text(message: str) -> str:
    """Remove the " Config: host=..., token=***, auth_type=..." part that the SDK adds.

    The SDK adds that part to every error, so a search for words such as
    "token" or "auth" must look only at the part before it.
    """
    return message.split(" Config: ", 1)[0]


#: Spark 3.3 and later split base64 text into lines of 76 characters.
_LINE_BREAKS = {ord("\r"): None, ord("\n"): None}


def decode_piece(text: str | None) -> bytes:
    """Decode one base64 piece from the warehouse.

    The SQL function `base64` in Spark 3.3 and later puts a line break
    (CR LF) after each 76 characters. The function removes the line breaks,
    then rejects any other character that is not base64.
    """
    return base64.b64decode((text or "").translate(_LINE_BREAKS), validate=True)


def wait_timeout(timeout_seconds: float) -> str:
    """Return the wait_timeout for execute_statement: 30s, or less for a short timeout.

    Example: wait_timeout(600) returns "30s", wait_timeout(10) returns "10s",
    and wait_timeout(2) returns "5s", the smallest value the API accepts.
    """
    return f"{max(5, min(WAIT_TIMEOUT_SECONDS, int(timeout_seconds)))}s"


def _stop_if_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled("You cancelled the download.",
                                hint="Databricks was asked to stop the query.")


def _stat_row(rows: list[list[str | None]]) -> tuple[int, int]:
    """Read (files, bytes) from the result of the count statement."""
    if not rows:
        return 0, 0
    files, size = (list(rows[0]) + [None, None])[:2]
    try:
        return int(files or 0), int(size or 0)
    except ValueError:
        raise BackendError("The file check returned an unexpected result.",
                           hint="Try again.", detail=repr(rows[0])) from None


def _state(response: StatementResponse) -> StatementState | None:
    return response.status.state if response.status else None


def _column_names(response: StatementResponse) -> Sequence[str]:
    schema = response.manifest.schema if response.manifest else None
    if not schema or not schema.columns:
        return DEFAULT_COLUMNS
    columns = sorted(schema.columns, key=lambda c: c.position if c.position is not None else 0)
    return [c.name or "" for c in columns]

"""Tests for the Databricks backend, with a fake SDK client in place of the network."""

from __future__ import annotations

import pytest
from databricks.sdk import errors as sdk_errors
from databricks.sdk.service.catalog import ExternalLocationInfo
from databricks.sdk.service.sql import (
    ColumnInfo,
    ExternalLink,
    GetWarehouseResponse,
    ResultData,
    ResultManifest,
    ResultSchema,
    ServiceError,
    ServiceErrorCode,
    State,
    StatementResponse,
    StatementState,
    StatementStatus,
)

from dbx_s3_viewer.databricks_backend import (
    MSG_STARTING,
    MSG_WAITING,
    DatabricksBackend,
    classify_statement_error,
)
from dbx_s3_viewer.errors import (
    AuthError,
    BackendError,
    ConfigError,
    ListTimeoutError,
    MissingWarehouseError,
    PathNotFoundError,
    PermissionDeniedError,
)

COLUMNS = ["path", "name", "size", "modification_time"]


def response(state=StatementState.SUCCEEDED, rows=None, columns=COLUMNS, next_chunk=None,
             error=None, truncated=False) -> StatementResponse:
    schema = ResultSchema(columns=[ColumnInfo(name=n, position=i) for i, n in enumerate(columns)])
    result = ResultData(data_array=rows, next_chunk_index=next_chunk) if rows is not None else None
    return StatementResponse(
        statement_id="stmt-1",
        status=StatementStatus(state=state, error=error),
        manifest=ResultManifest(schema=schema, truncated=truncated),
        result=result,
    )


def rows_for(n: int, directory: str = "s3://b/d/") -> list[list[str]]:
    return [[f"{directory}f{i:04d}.csv", f"f{i:04d}.csv", "10", "1706105124000"] for i in range(n)]


class FakeStatements:
    def __init__(self, responses, chunks=None, execute_error=None):
        self.responses = list(responses)
        self.chunks = chunks or {}
        self.execute_error = execute_error
        self.executed: list[tuple[str, dict]] = []
        self.polls = 0
        self.cancelled: list[str] = []

    def execute_statement(self, statement, warehouse_id, **kwargs):
        self.executed.append((statement, {"warehouse_id": warehouse_id, **kwargs}))
        if self.execute_error:
            raise self.execute_error
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def get_statement(self, statement_id):
        self.polls += 1
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def get_statement_result_chunk_n(self, statement_id, chunk_index):
        return self.chunks[chunk_index]

    def cancel_execution(self, statement_id):
        self.cancelled.append(statement_id)


class FakeWarehouses:
    def __init__(self, state=State.RUNNING, error=None):
        self.state, self.error, self.calls = state, error, 0

    def get(self, warehouse_id):
        self.calls += 1
        if self.error:
            raise self.error
        return GetWarehouseResponse(id=warehouse_id, state=self.state)


class FakeLocations:
    def __init__(self, items=(), error=None):
        self.items, self.error = list(items), error

    def list(self):
        if self.error:
            raise self.error
        return iter(self.items)


class FakeClient:
    def __init__(self, statements, warehouses=None, locations=None):
        self.statement_execution = statements
        self.warehouses = warehouses or FakeWarehouses()
        self.external_locations = locations or FakeLocations()


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make(client, *, limit=1000, timeout=600, warehouse="wh-1", clock=None):
    clock = clock or Clock()
    return DatabricksBackend(
        profile="work",
        warehouse_id=warehouse,
        limit=limit,
        timeout_seconds=timeout,
        client_factory=lambda: client,
        sleep=clock.sleep,
        clock=clock.time,
    )


def test_list_sends_one_list_statement_and_parses_rows():
    rows = [["s3://b/d/sub/", "sub/", "0", "0"], ["s3://b/d/a.csv", "a.csv", "2048", "17"]]
    statements = FakeStatements([response(rows=rows)])
    listing = make(FakeClient(statements)).list("s3://b/d")
    assert len(statements.executed) == 1
    statement, kwargs = statements.executed[0]
    assert statement == "LIST 's3://b/d/' LIMIT 1001"
    assert kwargs["warehouse_id"] == "wh-1"
    assert kwargs["wait_timeout"] == "30s"
    assert kwargs["disposition"].value == "INLINE"
    assert kwargs["format"].value == "JSON_ARRAY"
    assert kwargs["parameters"] is None
    assert listing.path == "s3://b/d/"
    assert not listing.truncated
    assert [(e.name, e.is_dir, e.size) for e in listing.entries] == [("sub", True, 0),
                                                                    ("a.csv", False, 2048)]


def test_list_marks_truncation_when_limit_plus_one_rows_come_back():
    statements = FakeStatements([response(rows=rows_for(4))])
    listing = make(FakeClient(statements), limit=3).list("s3://b/d/")
    assert statements.executed[0][0] == "LIST 's3://b/d/' LIMIT 4"
    assert listing.truncated
    assert len(listing.entries) == 3
    assert listing.limit == 3


def test_list_exactly_at_limit_is_not_truncated():
    listing = make(FakeClient(FakeStatements([response(rows=rows_for(3))])), limit=3).list("s3://b/d/")
    assert not listing.truncated
    assert len(listing.entries) == 3


def test_list_trusts_manifest_truncated_flag():
    statements = FakeStatements([response(rows=rows_for(2), truncated=True)])
    assert make(FakeClient(statements), limit=10).list("s3://b/d/").truncated


def test_list_reads_all_chunks():
    first = response(rows=rows_for(2), next_chunk=1)
    chunks = {1: ResultData(data_array=rows_for(5)[2:4], next_chunk_index=2),
              2: ResultData(data_array=rows_for(5)[4:])}
    listing = make(FakeClient(FakeStatements([first], chunks=chunks))).list("s3://b/d/")
    assert [e.name for e in listing.entries] == [f"f{i:04d}.csv" for i in range(5)]


def test_list_uses_is_directory_column_when_present():
    columns = COLUMNS + ["is_directory"]
    rows = [["s3://b/d/sub", "sub", "0", "0", "true"], ["s3://b/d/x", "x", "1", "1", "false"]]
    listing = make(FakeClient(FakeStatements([response(rows=rows, columns=columns)]))).list("s3://b/d/")
    assert [(e.name, e.is_dir, e.path) for e in listing.entries] == [
        ("sub", True, "s3://b/d/sub/"), ("x", False, "s3://b/d/x")]


def test_pending_statement_is_polled_until_done_and_reports_waiting():
    statements = FakeStatements([
        response(state=StatementState.PENDING),
        response(state=StatementState.PENDING),
        response(state=StatementState.RUNNING),
        response(rows=rows_for(1)),
    ])
    messages: list[str] = []
    clock = Clock()
    listing = make(FakeClient(statements), clock=clock).list("s3://b/d/", progress=messages.append)
    assert len(listing.entries) == 1
    assert statements.polls == 3
    assert MSG_WAITING in messages
    assert clock.sleeps and all(s <= 5 for s in clock.sleeps)


def test_stopped_warehouse_reports_starting_before_the_statement():
    messages: list[str] = []
    client = FakeClient(FakeStatements([response(rows=[])]), FakeWarehouses(State.STOPPED))
    make(client).list("s3://b/d/", progress=messages.append)
    assert messages.index(MSG_STARTING) < messages.index("Listing…")


def test_warehouse_state_is_checked_once_while_active():
    warehouses = FakeWarehouses()
    client = FakeClient(FakeStatements([response(rows=[]), response(rows=[])]), warehouses)
    backend = make(client)
    backend.list("s3://b/d/")
    backend.list("s3://b/e/")
    assert warehouses.calls == 1


def test_unknown_warehouse_is_a_config_error():
    client = FakeClient(FakeStatements([]), FakeWarehouses(error=sdk_errors.NotFound("no such")))
    with pytest.raises(ConfigError) as info:
        make(client).list("s3://b/d/")
    assert "wh-1" in info.value.message


def test_warehouse_get_permission_problem_does_not_block_listing():
    warehouses = FakeWarehouses(error=sdk_errors.PermissionDenied("no view"))
    client = FakeClient(FakeStatements([response(rows=rows_for(1))]), warehouses)
    assert len(make(client).list("s3://b/d/").entries) == 1


def test_timeout_cancels_the_statement():
    statements = FakeStatements([response(state=StatementState.PENDING)])
    with pytest.raises(ListTimeoutError) as info:
        make(FakeClient(statements), timeout=20).list("s3://b/d/")
    assert statements.cancelled == ["stmt-1"]
    assert "timeout_seconds" in info.value.hint


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("[INSUFFICIENT_PERMISSIONS] User does not have READ FILES on External Location 'x'.",
         PermissionDeniedError),
        ("PERMISSION_DENIED: request not authorized", PermissionDeniedError),
        ("[UNAUTHORIZED_ACCESS] Unauthorized access", PermissionDeniedError),
        ("java.nio.file.AccessDeniedException: s3a://b/d: 403 Forbidden", PermissionDeniedError),
        ("[PATH_NOT_FOUND] Path does not exist: s3://b/d/.", PathNotFoundError),
        ("java.io.FileNotFoundException: No such file or directory", PathNotFoundError),
        ("NoSuchBucket: The specified bucket does not exist", PathNotFoundError),
        ("[PARSE_SYNTAX_ERROR] Syntax error at or near 'LIST'", BackendError),
    ],
)
def test_failed_statement_errors(message, kind):
    error = ServiceError(error_code=ServiceErrorCode.BAD_REQUEST, message=message)
    statements = FakeStatements([response(state=StatementState.FAILED, error=error)])
    with pytest.raises(kind) as info:
        make(FakeClient(statements)).list("s3://b/d/")
    assert type(info.value) is kind
    assert info.value.detail == message
    assert info.value.hint


def test_classify_ignores_markers_inside_the_path():
    path = "s3://b/access-denied-permission_denied/"
    error = classify_statement_error("BAD_REQUEST", f"[PATH_NOT_FOUND] Path does not exist: {path}",
                                     path)
    assert isinstance(error, PathNotFoundError)


def test_classify_by_error_code():
    assert isinstance(classify_statement_error("UNAUTHENTICATED", "x", "s3://b/"), AuthError)
    assert isinstance(classify_statement_error("DEADLINE_EXCEEDED", "x", "s3://b/"),
                      ListTimeoutError)


def test_canceled_statement_is_a_backend_error():
    statements = FakeStatements([response(state=StatementState.CANCELED)])
    with pytest.raises(BackendError):
        make(FakeClient(statements)).list("s3://b/d/")


def test_missing_warehouse_fails_before_any_call():
    created = []
    backend = DatabricksBackend(profile=None, warehouse_id=None,
                                client_factory=lambda: created.append(1))
    with pytest.raises(MissingWarehouseError) as info:
        backend.list("s3://b/d/")
    assert created == []
    assert "--warehouse-id" in info.value.hint


def test_no_login_is_an_auth_error_with_next_step():
    def factory():
        raise ValueError("default auth: cannot configure default credentials")

    backend = DatabricksBackend(profile="work", warehouse_id="wh", client_factory=factory)
    with pytest.raises(AuthError) as info:
        backend.list("s3://b/d/")
    assert "databricks auth login" in info.value.hint
    assert "--profile work" in info.value.hint


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (sdk_errors.Unauthenticated("bad"), AuthError),
        (sdk_errors.PermissionDenied("Invalid access token"), AuthError),
        (sdk_errors.PermissionDenied("not allowed to use warehouse"), PermissionDeniedError),
        # The SDK adds this config part to every message. It must not look like a token problem.
        (sdk_errors.PermissionDenied("not allowed. Config: host=https://x, token=***, "
                                     "auth_type=pat"), PermissionDeniedError),
        (sdk_errors.PermissionDenied("Invalid access token. Config: host=https://x, "
                                     "token=***, auth_type=pat"), AuthError),
        (sdk_errors.DeadlineExceeded("slow"), ListTimeoutError),
        (sdk_errors.InternalError("boom"), BackendError),
    ],
)
def test_api_errors_on_execute(exc, kind):
    client = FakeClient(FakeStatements([], execute_error=exc))
    with pytest.raises(kind):
        make(client).list("s3://b/d/")


def test_backend_refuses_anything_but_list():
    statements = FakeStatements([response(rows=[])])
    backend = make(FakeClient(statements))
    with pytest.raises(RuntimeError):
        backend._execute(FakeClient(statements), "SELECT 1", lambda _m: None, "s3://b/")
    assert statements.executed == []


def test_locations_sorted_and_without_url_skipped():
    items = [ExternalLocationInfo(name="zeta", url="s3://z/"),
             ExternalLocationInfo(name="Alpha", url="s3://a/"),
             ExternalLocationInfo(name="no-url")]
    client = FakeClient(FakeStatements([]), locations=FakeLocations(items))
    found = make(client).locations()
    assert [(loc.name, loc.url, loc.source) for loc in found] == [
        ("Alpha", "s3://a/", "external location"), ("zeta", "s3://z/", "external location")]


def test_locations_denied():
    client = FakeClient(FakeStatements([]),
                        locations=FakeLocations(error=sdk_errors.PermissionDenied("nope")))
    with pytest.raises(PermissionDeniedError):
        make(client).locations()


def test_probe_reports_columns_and_marker_support():
    rows = [["s3://b/d/sub/", "sub/", "0", "0"], ["s3://b/d/a.csv", "a.csv", "1", "2"]]
    marker_error = ServiceError(error_code=ServiceErrorCode.BAD_REQUEST,
                                message="[UNBOUND_SQL_PARAMETER] Found the unbound parameter: url")
    statements = FakeStatements([
        response(rows=rows),
        response(state=StatementState.FAILED, error=marker_error),
    ])
    result = make(FakeClient(statements)).probe("s3://b/d")
    assert [s for s, _ in statements.executed] == ["LIST 's3://b/d/' LIMIT 5", "LIST :url LIMIT 5"]
    marker_param = statements.executed[1][1]["parameters"][0]
    assert (marker_param.name, marker_param.value, marker_param.type) == ("url", "s3://b/d/",
                                                                         "STRING")
    assert result.columns == COLUMNS
    assert not result.has_is_directory_column
    assert result.rows_ending_in_slash == 1
    assert result.rows_detected_as_dirs == 1
    assert result.marker_accepted is False
    assert "UNBOUND_SQL_PARAMETER" in result.marker_error
    assert result.warehouse_state == "RUNNING"


# -- downloads ------------------------------------------------------------

import base64  # noqa: E402

from dbx_s3_viewer import databricks_backend  # noqa: E402
from dbx_s3_viewer.errors import DownloadError, InvalidPathError  # noqa: E402


def stat(files, size):
    return response(rows=[[str(files), str(size)]], columns=["files", "bytes"])


def pieces_response(*chunks: bytes, start: int = 0, **kwargs):
    rows = [[str(start + i), base64.b64encode(c).decode()] for i, c in enumerate(chunks)]
    return response(rows=rows, columns=["i", "piece"], **kwargs)


def test_read_file_inline():
    statements = FakeStatements([stat(1, 5), pieces_response(b"abc", b"de")])
    content = make(FakeClient(statements)).read_file("s3://b/d/f.bin")
    assert content.size == 5
    assert [s for s, _ in statements.executed] == [
        "SELECT count(*) AS files, coalesce(sum(length), 0) AS bytes "
        "FROM read_files('s3://b/d/f.bin', format => 'binaryFile')"
    ]  # The read statement runs only when the pieces are read.
    assert b"".join(content.pieces) == b"abcde"
    read_sql, kwargs = statements.executed[1]
    assert read_sql.startswith("SELECT i, base64(substr(content,")
    assert kwargs["disposition"].value == "INLINE"


def test_read_file_large_uses_result_links_without_the_token(monkeypatch):
    monkeypatch.setattr(databricks_backend, "INLINE_MAX_BYTES", 4)
    first = StatementResponse(
        statement_id="stmt-1",
        status=StatementStatus(state=StatementState.SUCCEEDED),
        manifest=ResultManifest(schema=ResultSchema(columns=[ColumnInfo(name="i", position=0),
                                                             ColumnInfo(name="piece", position=1)])),
        result=ResultData(external_links=[ExternalLink(external_link="https://store/0",
                                                       http_headers={"x-h": "1"},
                                                       next_chunk_index=1)]),
    )
    chunks = {1: ResultData(external_links=[ExternalLink(external_link="https://store/1")])}
    statements = FakeStatements([stat(1, 6), first], chunks=chunks)
    bodies = {"https://store/0": [["0", base64.b64encode(b"abc").decode()]],
              "https://store/1": [["1", base64.b64encode(b"def").decode()]]}
    calls = []

    class Reply:
        def __init__(self, url):
            self.url = url

        def raise_for_status(self):
            pass

        def json(self):
            return bodies[self.url]

    def http_get(url, headers, timeout):
        calls.append((url, headers))
        return Reply(url)

    backend = DatabricksBackend(profile=None, warehouse_id="wh", client_factory=lambda: FakeClient(statements),
                                sleep=lambda _s: None, http_get=http_get)
    content = backend.read_file("s3://b/f")
    assert b"".join(content.pieces) == b"abcdef"
    assert statements.executed[1][1]["disposition"].value == "EXTERNAL_LINKS"
    assert calls == [("https://store/0", {"x-h": "1"}), ("https://store/1", {})]
    assert all("Authorization" not in headers for _, headers in calls)


def test_read_file_link_failure_explains_cloud_fetch(monkeypatch):
    monkeypatch.setattr(databricks_backend, "INLINE_MAX_BYTES", 1)
    first = response(columns=["i", "piece"])
    first.result = ResultData(external_links=[ExternalLink(external_link="https://store/0")])
    statements = FakeStatements([stat(1, 6), first])

    def http_get(url, headers, timeout):
        raise databricks_backend.requests.ConnectionError("blocked")

    backend = DatabricksBackend(profile=None, warehouse_id="wh", client_factory=lambda: FakeClient(statements),
                                sleep=lambda _s: None, http_get=http_get)
    content = backend.read_file("s3://b/f")
    with pytest.raises(BackendError) as info:
        b"".join(content.pieces)
    assert "cloud fetch" in info.value.hint
    assert "https://store" not in info.value.detail  # the signed URL stays hidden


def test_read_file_missing():
    statements = FakeStatements([stat(0, 0)])
    with pytest.raises(PathNotFoundError):
        make(FakeClient(statements)).read_file("s3://b/f")


def test_read_file_hidden_name_hint():
    statements = FakeStatements([stat(0, 0)])
    with pytest.raises(PathNotFoundError) as info:
        make(FakeClient(statements)).read_file("s3://b/_SUCCESS")
    assert "_SUCCESS" in info.value.hint


def test_read_file_refuses_a_folder_that_looks_like_a_file():
    statements = FakeStatements([stat(3, 300)])
    with pytest.raises(InvalidPathError, match="3 files"):
        make(FakeClient(statements)).read_file("s3://b/table1")
    assert len(statements.executed) == 1


def test_read_file_refuses_files_over_2_gib():
    statements = FakeStatements([stat(1, 2**31)])
    with pytest.raises(DownloadError, match="2 GiB"):
        make(FakeClient(statements)).read_file("s3://b/big")


def test_read_file_checks_piece_order_and_base64():
    statements = FakeStatements([stat(1, 6), pieces_response(b"abc", start=1)])
    with pytest.raises(BackendError, match="out of order"):
        b"".join(make(FakeClient(statements)).read_file("s3://b/f").pieces)
    bad = response(rows=[["0", "not base64!"]], columns=["i", "piece"])
    statements = FakeStatements([stat(1, 6), bad])
    with pytest.raises(BackendError, match="base64") as info:
        b"".join(make(FakeClient(statements)).read_file("s3://b/f").pieces)
    # The detail helps a bug report, and holds no file content.
    assert "piece length: 11 characters" in info.value.detail
    assert "not base64" not in info.value.detail


def test_read_file_permission_error_says_read():
    error = ServiceError(error_code=ServiceErrorCode.BAD_REQUEST,
                         message="[INSUFFICIENT_PERMISSIONS] User does not have READ FILES")
    statements = FakeStatements([response(state=StatementState.FAILED, error=error)])
    with pytest.raises(PermissionDeniedError, match="permission to read s3://b/f"):
        make(FakeClient(statements)).read_file("s3://b/f")


def test_backend_refuses_writes():
    statements = FakeStatements([response(rows=[])])
    backend = make(FakeClient(statements))
    for sql in ("DELETE FROM t", "COPY INTO t FROM 's3://b/'", "select i, base64(1)"):
        with pytest.raises(RuntimeError):
            backend._execute(FakeClient(statements), sql, lambda _m: None, "s3://b/")
    assert statements.executed == []


# -- audit regressions ----------------------------------------------------

import threading  # noqa: E402

import requests  # noqa: E402

from dbx_s3_viewer.errors import DownloadCancelled, UnreachableError  # noqa: E402


def test_ctrl_c_while_polling_cancels_the_statement():
    statements = FakeStatements([response(state=StatementState.PENDING)])

    def interrupt(_seconds):
        raise KeyboardInterrupt

    backend = DatabricksBackend(profile=None, warehouse_id="wh",
                                client_factory=lambda: FakeClient(statements), sleep=interrupt)
    with pytest.raises(KeyboardInterrupt):
        backend.list("s3://b/d/")
    assert statements.cancelled == ["stmt-1"]


def test_timeout_cancels_once():
    statements = FakeStatements([response(state=StatementState.PENDING)])
    with pytest.raises(ListTimeoutError):
        make(FakeClient(statements), timeout=5).list("s3://b/d/")
    assert statements.cancelled == ["stmt-1"]


def test_cancel_event_stops_a_running_download_query():
    cancel = threading.Event()
    statements = FakeStatements([stat(1, 5), response(state=StatementState.RUNNING)])
    clock = Clock()

    def sleep(seconds):
        clock.sleep(seconds)
        cancel.set()  # the user presses x while the read query runs

    backend = DatabricksBackend(profile=None, warehouse_id="wh", sleep=sleep, clock=clock.time,
                                client_factory=lambda: FakeClient(statements))
    content = backend.read_file("s3://b/f", cancel=cancel)
    with pytest.raises(DownloadCancelled):
        next(content.pieces)
    assert statements.cancelled == ["stmt-1"]
    assert len(statements.executed) == 2


def test_chunk_index_on_the_result_is_kept_when_links_have_none(monkeypatch):
    monkeypatch.setattr(databricks_backend, "INLINE_MAX_BYTES", 1)
    first = response(columns=["i", "piece"])
    first.result = ResultData(next_chunk_index=1,
                              external_links=[ExternalLink(external_link="https://store/0")])
    chunks = {1: ResultData(external_links=[ExternalLink(external_link="https://store/1")])}
    statements = FakeStatements([stat(1, 6), first], chunks=chunks)
    bodies = {"https://store/0": [["0", base64.b64encode(b"abc").decode()]],
              "https://store/1": [["1", base64.b64encode(b"def").decode()]]}

    class Reply:
        def __init__(self, url):
            self.url = url

        def raise_for_status(self):
            pass

        def json(self):
            return bodies[self.url]

    backend = DatabricksBackend(profile=None, warehouse_id="wh", sleep=lambda _s: None,
                                client_factory=lambda: FakeClient(statements),
                                http_get=lambda url, headers, timeout: Reply(url))
    assert b"".join(backend.read_file("s3://b/f").pieces) == b"abcdef"


@pytest.mark.parametrize(
    ("cause", "kind"),
    [(requests.ConnectionError("refused"), UnreachableError),
     (requests.ReadTimeout("slow"), ListTimeoutError),
     (None, ListTimeoutError)],
)
def test_sdk_retry_timeout_is_explained(cause, kind):
    def fail(*_args, **_kwargs):
        try:
            raise cause if cause else RuntimeError("x")
        except BaseException as exc:
            raise TimeoutError("Timed out after 0:00:30") from (exc if cause else None)

    statements = FakeStatements([])
    statements.execute_statement = fail
    with pytest.raises(kind):
        make(FakeClient(statements)).list("s3://b/d/")


def test_warehouse_check_does_not_hide_an_unreachable_host():
    calls = []

    class DeadWarehouses:
        def get(self, _id):
            calls.append(1)
            raise TimeoutError("Timed out") from requests.ConnectionError("refused")

    statements = FakeStatements([response(rows=[])])
    with pytest.raises(UnreachableError):
        make(FakeClient(statements, DeadWarehouses())).list("s3://b/d/")
    assert statements.executed == []  # no second wait for the same dead host


def test_classify_scrubs_the_s3a_form_of_the_path():
    path = "s3://b/forbidden/data.csv"
    error = classify_statement_error(
        "BAD_REQUEST", "java.io.FileNotFoundException: No such file: s3a://b/forbidden/data.csv",
        path)
    assert isinstance(error, PathNotFoundError)


def test_unauthenticated_statement_keeps_the_profile_in_the_hint():
    error = ServiceError(error_code=ServiceErrorCode.UNAUTHENTICATED, message="token expired")
    statements = FakeStatements([response(state=StatementState.FAILED, error=error)])
    with pytest.raises(AuthError) as info:
        make(FakeClient(statements)).list("s3://b/d/")  # make() uses profile "work"
    assert "--profile work" in info.value.hint


@pytest.mark.parametrize(("timeout", "expected"), [(600, "30s"), (30, "30s"), (10, "10s"), (2, "5s")])
def test_wait_timeout_follows_timeout_seconds(timeout, expected):
    assert databricks_backend.wait_timeout(timeout) == expected
    statements = FakeStatements([response(rows=[])])
    make(FakeClient(statements), timeout=timeout).list("s3://b/d/")
    assert statements.executed[0][1]["wait_timeout"] == expected


def spark_base64(data: bytes) -> str:
    """Encode like Spark 3.3 and later: 76 characters per line, lines joined by CR LF."""
    text = base64.b64encode(data).decode()
    return "\r\n".join(text[i:i + 76] for i in range(0, len(text), 76))


def test_read_file_accepts_base64_that_spark_splits_into_lines():
    first, second = bytes(range(256)) * 3, b"tail,of,the,file\n" * 9
    assert "\r\n" in spark_base64(first)
    rows = [["0", spark_base64(first)], ["1", spark_base64(second)]]
    statements = FakeStatements([stat(1, len(first) + len(second)),
                                 response(rows=rows, columns=["i", "piece"])])
    content = make(FakeClient(statements)).read_file("s3://b/d/f.csv")
    assert b"".join(content.pieces) == first + second

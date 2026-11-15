"""Run the real databricks-sdk against a local mock workspace (tests/mock_workspace.py).

These tests use the SDK default login chain with DATABRICKS_HOST and
DATABRICKS_TOKEN, the same way the tool runs on a laptop.
"""

from __future__ import annotations

import pytest

from dbx_s3_viewer import cli
from dbx_s3_viewer.databricks_backend import MSG_WAITING, DatabricksBackend
from dbx_s3_viewer.errors import AuthError, ConfigError, PermissionDeniedError

from .mock_workspace import TOKEN, MockWorkspace


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    for name in ("DATABRICKS_CONFIG_PROFILE", "DATABRICKS_WAREHOUSE_ID", "DATABRICKS_CLIENT_ID",
                 "DATABRICKS_CLIENT_SECRET", "DATABRICKS_AUTH_TYPE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.databrickscfg
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("DBX_S3_VIEWER_CONFIG", str(tmp_path / "config.toml"))
    with MockWorkspace() as ws:
        monkeypatch.setenv("DATABRICKS_HOST", ws.url)
        monkeypatch.setenv("DATABRICKS_TOKEN", TOKEN)
        yield ws


def backend(warehouse="wh-1", limit=1000):
    return DatabricksBackend(profile=None, warehouse_id=warehouse, limit=limit,
                             sleep=lambda _s: None)


def test_list_over_http_with_chunks(workspace):
    listing = backend().list("s3://b/data")
    assert [s["statement"] for s in workspace.statements] == ["LIST 's3://b/data/' LIMIT 1001"]
    sent = workspace.statements[0]
    assert sent["warehouse_id"] == "wh-1"
    assert sent["wait_timeout"] == "30s"
    assert sent["on_wait_timeout"] == "CONTINUE"
    assert sent["disposition"] == "INLINE"
    assert sent["format"] == "JSON_ARRAY"
    assert [e.name for e in listing.entries] == ["dir", *[f"file{i}.csv" for i in range(5)]]
    assert listing.entries[0].is_dir
    assert listing.entries[2].size == 100
    assert not listing.truncated


def test_truncation_over_http(workspace):
    listing = backend(limit=4).list("s3://b/data/")
    assert workspace.statements[0]["statement"] == "LIST 's3://b/data/' LIMIT 5"
    assert listing.truncated and len(listing.entries) == 4


def test_quote_in_path_over_http(workspace):
    listing = backend().list("s3://b/it's/")
    assert workspace.statements[0]["statement"] == "LIST 's3://b/it\\'s/' LIMIT 1001"
    assert listing.entries[0].path == "s3://b/it's/dir/"


def test_polling_over_http(workspace):
    workspace.warehouse_state = "STOPPED"
    messages: list[str] = []
    listing = backend().list("s3://b/slow/", progress=messages.append)
    assert [e.name for e in listing.entries] == ["x.csv"]
    assert workspace.polls == 2
    assert MSG_WAITING in messages
    assert any("starting" in m for m in messages)


def test_permission_error_over_http(workspace):
    with pytest.raises(PermissionDeniedError) as info:
        backend().list("s3://b/denied/")
    assert "INSUFFICIENT_PERMISSIONS" in info.value.detail


def test_bad_token_over_http(workspace, monkeypatch):
    monkeypatch.setenv("DATABRICKS_TOKEN", "wrong")
    with pytest.raises(AuthError) as info:
        backend().list("s3://b/data/")
    assert "databricks auth login" in info.value.hint


def test_no_login_at_all(workspace, monkeypatch):
    monkeypatch.delenv("DATABRICKS_HOST")
    monkeypatch.delenv("DATABRICKS_TOKEN")
    with pytest.raises(AuthError) as info:
        backend().list("s3://b/data/")
    assert "databricks auth login" in info.value.hint
    assert workspace.statements == []


def test_unknown_profile(workspace, tmp_path):
    (tmp_path / ".databrickscfg").write_text(
        f"[work]\nhost = {workspace.url}\ntoken = {TOKEN}\n", encoding="utf-8")
    good = DatabricksBackend(profile="work", warehouse_id="wh-1", sleep=lambda _s: None)
    assert len(good.list("s3://b/data/").entries) == 6
    bad = DatabricksBackend(profile="nope", warehouse_id="wh-1", sleep=lambda _s: None)
    with pytest.raises(AuthError) as info:
        bad.list("s3://b/data/")
    assert "--profile nope" in info.value.hint


def test_unknown_warehouse_over_http(workspace):
    with pytest.raises(ConfigError) as info:
        backend(warehouse="wh-missing").list("s3://b/data/")
    assert "wh-missing" in info.value.message


def test_external_locations_over_http(workspace):
    found = backend().locations()
    assert [(loc.name, loc.url) for loc in found] == [("curated", "s3://curated-bucket/data/"),
                                                      ("landing", "s3://landing-bucket/")]
    workspace.locations_status = 403
    with pytest.raises(PermissionDeniedError):
        backend().locations()


def test_ls_command_over_http(workspace, capsys):
    assert cli.main(["ls", "s3://b/data/", "--warehouse-id", "wh-1", "--json"]) == 0
    out = capsys.readouterr().out
    assert '"name": "dir"' in out


def test_check_command_over_http(workspace, capsys):
    assert cli.main(["check", "s3://b/data/", "--warehouse-id", "wh-1"]) == 0
    out = capsys.readouterr().out
    assert "External locations:  2 visible" in out
    assert "Columns:           path, name, size, modification_time" in out
    assert "no is_directory column" in out
    assert "LIST :url (parameter marker): rejected" in out
    statements = [s["statement"] for s in workspace.statements]
    assert statements == ["LIST 's3://b/data/' LIMIT 5", "LIST :url LIMIT 5"]
    assert workspace.statements[1]["parameters"] == [
        {"name": "url", "value": "s3://b/data/", "type": "STRING"}]
    assert all(s.startswith("LIST ") for s in statements)


# -- downloads over HTTP --------------------------------------------------

from dbx_s3_viewer import databricks_backend, downloads, paths  # noqa: E402
from dbx_s3_viewer.errors import InvalidPathError, PathNotFoundError  # noqa: E402


def test_download_inline_over_http(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PIECE_BYTES", 4)
    data = bytes(range(256)) * 3 + b"tail"
    workspace.files["s3://b/data/it's.bin"] = data
    content = backend().read_file("s3://b/data/it's.bin")
    assert content.size == len(data)
    written = downloads.save(content, tmp_path / "out.bin")
    assert written == len(data)
    assert (tmp_path / "out.bin").read_bytes() == data
    kinds = [s["statement"].split(" ", 3)[:3] for s in workspace.statements]
    assert kinds == [["SELECT", "count(*)", "AS"], ["SELECT", "i,", "base64(substr(content,"]]
    assert workspace.statements[1]["disposition"] == "INLINE"
    assert workspace.link_requests == []


def test_download_through_result_links_sends_no_token(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PIECE_BYTES", 3)
    monkeypatch.setattr(databricks_backend, "INLINE_MAX_BYTES", 5)
    data = b"0123456789abcdefghij"
    workspace.files["s3://b/big.bin"] = data
    downloads.save(backend().read_file("s3://b/big.bin"), tmp_path / "big.bin")
    assert (tmp_path / "big.bin").read_bytes() == data
    assert workspace.statements[1]["disposition"] == "EXTERNAL_LINKS"
    assert len(workspace.link_requests) == 4  # 7 pieces, 2 rows per chunk
    for headers in workspace.link_requests:
        assert "Authorization" not in headers
        assert headers.get("x-amz-server-side-encryption") == "test"


def test_download_errors_over_http(workspace):
    workspace.files["s3://b/table1/part-0"] = b"a"
    workspace.files["s3://b/table1/part-1"] = b"b"
    with pytest.raises(InvalidPathError, match="2 files"):
        backend().read_file("s3://b/table1")
    with pytest.raises(PathNotFoundError):
        backend().read_file("s3://b/nothing.csv")


def test_get_command_over_http(workspace, tmp_path, capsys):
    workspace.files["s3://b/report.csv"] = b"a,b\n1,2\n"
    out = tmp_path / "dl"
    out.mkdir()
    assert cli.main(["get", "s3://b/report.csv", "-o", str(out), "--warehouse-id", "wh-1"]) == 0
    assert (out / "report.csv").read_bytes() == b"a,b\n1,2\n"
    assert capsys.readouterr().out.strip() == str(out / "report.csv")


def test_unreachable_host_fails_fast_with_a_clear_message(workspace, monkeypatch):
    import socket
    import time

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead = f"http://127.0.0.1:{sock.getsockname()[1]}"
    sock.close()  # nothing listens on this port now
    monkeypatch.setenv("DATABRICKS_HOST", dead)
    monkeypatch.setattr(databricks_backend, "SDK_RETRY_SECONDS", 2)
    start = time.monotonic()
    with pytest.raises(databricks_backend.UnreachableError) as info:
        backend().list("s3://b/data/")
    assert "Cannot reach" in info.value.message
    # One retry window for the SDK's host lookup, one for the first call.
    assert time.monotonic() - start < 10

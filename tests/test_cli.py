import json

import pytest

from dbx_s3_viewer import cli
from dbx_s3_viewer.model import Entry, Listing


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Use an empty config file location and no Databricks variables."""
    monkeypatch.setenv("DBX_S3_VIEWER_CONFIG", str(tmp_path / "config.toml"))
    for name in ("DATABRICKS_WAREHOUSE_ID", "DATABRICKS_CONFIG_PROFILE", "DATABRICKS_HOST",
                 "DATABRICKS_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_ls_demo_prints_folders_first(capsys):
    assert cli.main(["ls", "s3://acme-landing/", "--demo"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split("  ")[-1].strip() for line in lines] == [
        "client's uploads/", "events/", "vendor_feeds/", "_SUCCESS", "README.txt"]
    assert "DIR" in lines[0]


def test_ls_json(capsys):
    assert cli.main(["ls", "s3://acme-curated/sales/orders", "--demo", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["path"] == "s3://acme-curated/sales/orders/"
    assert data["truncated"] is False
    first = data["entries"][0]
    assert first == {"name": "_delta_log", "path": "s3://acme-curated/sales/orders/_delta_log/",
                     "is_dir": True, "size": 0, "modification_time": 0, "modified": None}
    files = [e for e in data["entries"] if not e["is_dir"]]
    assert files and all(e["modified"].endswith("+00:00") for e in files)


def test_ls_warns_on_truncation(capsys):
    path = "s3://acme-landing/events/year=2026/month=11/day=12/"
    assert cli.main(["ls", path, "--demo", "--limit", "3", "--json"]) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["truncated"] is True
    assert data["count"] == 3
    assert "truncated" in captured.err


def test_ls_permission_denied(capsys):
    assert cli.main(["ls", "s3://acme-restricted/", "--demo"]) == cli.EXIT_FAILED
    err = capsys.readouterr().err
    assert "Permission denied" in err
    assert "What to do:" in err
    assert "Server message:" in err


def test_ls_invalid_path(capsys):
    assert cli.main(["ls", "/tmp/x", "--demo"]) == cli.EXIT_USAGE
    assert "not an s3:// path" in capsys.readouterr().err


def test_ls_without_warehouse(capsys):
    assert cli.main(["ls", "s3://b/"]) == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "No SQL warehouse ID" in err
    assert "DATABRICKS_WAREHOUSE_ID" in err


def test_ls_with_broken_config(tmp_path, capsys):
    (tmp_path / "config.toml").write_text("list_limit = -1", encoding="utf-8")
    assert cli.main(["ls", "s3://b/"]) == cli.EXIT_USAGE
    assert "list_limit" in capsys.readouterr().err


def test_check_without_warehouse(capsys):
    assert cli.main(["check", "s3://b/"]) == cli.EXIT_USAGE
    out = capsys.readouterr()
    assert "Warehouse ID:        -" in out.out
    assert "No SQL warehouse ID" in out.err


def test_format_listing():
    listing = Listing("s3://b/", (Entry("a.csv", "s3://b/a.csv", False, 2048, 0),
                                  Entry("logs", "s3://b/logs/", True, 0, 0)), False, 1000)
    lines = cli.format_listing(listing)
    assert lines[0].endswith("DIR  logs/")
    assert lines[1].endswith("2.0 KiB  a.csv")


def test_version(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--version"])
    assert info.value.code == 0
    assert "dbx-s3-viewer" in capsys.readouterr().out


def test_get_demo(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["get", "s3://acme-landing/README.txt", "--demo"]) == 0
    saved = tmp_path / "README.txt"
    assert capsys.readouterr().out.strip() == str(saved)
    assert saved.stat().st_size == 1284
    assert saved.read_bytes().startswith(b"dbx-s3-viewer demo data")
    # A second download never replaces the file.
    assert cli.main(["get", "s3://acme-landing/README.txt", "--demo"]) == cli.EXIT_USAGE
    assert "already exists" in capsys.readouterr().err
    assert saved.stat().st_size == 1284


def test_get_output_options(tmp_path, capsys):
    target = tmp_path / "renamed.txt"
    assert cli.main(["get", "s3://acme-landing/README.txt", "--demo", "-o", str(target)]) == 0
    assert target.exists()
    assert cli.main(["get", "s3://acme-landing/README.txt", "--demo",
                     "-o", str(tmp_path / "missing" / "x.txt")]) == cli.EXIT_USAGE
    assert "does not exist" in capsys.readouterr().err


def test_get_refuses_folders_and_wildcards(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["get", "s3://acme-landing/events/", "--demo"]) == cli.EXIT_USAGE
    assert "is a folder" in capsys.readouterr().err
    assert cli.main(["get", "s3://acme-landing/*.txt", "--demo"]) == cli.EXIT_USAGE
    assert "wildcards" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_get_demo_size_cap(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = "s3://acme-curated/sales/orders/part-00000-c000.snappy.parquet"
    assert cli.main(["get", path, "--demo"]) == cli.EXIT_USAGE
    assert "Demo mode downloads files up to" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_subcommand_after_options(tmp_path, capsys):
    assert cli.main(["--demo", "ls", "s3://acme-curated/"]) == 0
    assert "sales/" in capsys.readouterr().out
    assert cli.main(["--limit", "2", "--demo", "ls", "s3://acme-landing/"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_command_index():
    commands = {"ls", "get", "check"}
    assert cli._command_index(["ls", "s3://b/"], commands) == 0
    assert cli._command_index(["--demo", "get", "s3://b/f"], commands) == 1
    assert cli._command_index(["--profile", "ls"], commands) is None  # a profile named "ls"
    assert cli._command_index(["--profile", "ls", "check", "s3://b/"], commands) == 2
    assert cli._command_index(["s3://b/", "ls"], commands) is None  # the browser at a path
    assert cli._command_index(["--", "ls"], commands) is None
    assert cli._command_index([], commands) is None


def test_get_reports_underscore_files_like_read_files(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["get", "s3://acme-landing/_SUCCESS", "--demo"]) == cli.EXIT_FAILED
    assert "_SUCCESS" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def browser_kwargs(monkeypatch, argv):
    """Run the browser command with a stand-in app, and return what the app got."""
    from dbx_s3_viewer.tui import app as tui_app

    seen = {}

    class StandIn:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def run(self):
            pass

    monkeypatch.setattr(tui_app, "ViewerApp", StandIn)
    assert cli.main(argv) == cli.EXIT_OK
    return seen


def test_browser_opens_the_start_folder_unless_a_path_is_given(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(
        'warehouse_id = "w"\nstart_folder = "s3://b/landing/"\n', encoding="utf-8")
    seen = browser_kwargs(monkeypatch, [])
    assert seen["start_path"] == seen["start_folder"] == "s3://b/landing/"
    seen["save_start_folder"]("s3://b/other/")
    assert 'start_folder = "s3://b/other/"' in (tmp_path / "config.toml").read_text("utf-8")

    seen = browser_kwargs(monkeypatch, ["s3://b/elsewhere"])
    assert seen["start_path"] == "s3://b/elsewhere/"
    assert seen["start_folder"] == "s3://b/other/"


def test_demo_browser_ignores_the_start_folder(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('start_folder = "s3://b/landing/"\n', encoding="utf-8")
    seen = browser_kwargs(monkeypatch, ["--demo"])
    assert seen["start_path"] is None
    assert seen["save_start_folder"] is None

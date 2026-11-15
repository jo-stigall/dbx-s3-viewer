from datetime import datetime, timezone

import pytest

from dbx_s3_viewer.model import (
    Entry,
    SortOrder,
    human_size,
    is_directory,
    local_time,
    parse_rows,
    sort_entries,
)

COLUMNS = ["path", "name", "size", "modification_time"]


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"name": "logs/", "path": "s3://b/logs/"}, True),
        ({"name": "logs", "path": "s3://b/logs/"}, True),
        ({"name": "logs/", "path": "s3://b/logs"}, True),
        ({"name": "a.csv", "path": "s3://b/a.csv"}, False),
        ({"name": "table1", "path": "s3://b/table1", "size": "0"}, False),
        ({"name": "x", "path": "s3://b/x", "is_directory": "true"}, True),
        ({"name": "x/", "path": "s3://b/x/", "is_directory": "false"}, False),
        ({"name": "x/", "path": "s3://b/x/", "is_directory": None}, True),
        ({"name": "x", "path": "s3://b/x", "is_directory": "TRUE"}, True),
        ({}, False),
    ],
)
def test_is_directory(row, expected):
    assert is_directory(row) is expected


def test_parse_rows_with_trailing_slash_directories():
    rows = [
        ["s3://b/a/logs/", "logs/", "0", "0"],
        ["s3://b/a/app.csv", "app.csv", "2048", "1706105124000"],
    ]
    entries = parse_rows(COLUMNS, rows, "s3://b/a/")
    assert entries == [
        Entry("logs", "s3://b/a/logs/", True, 0, 0),
        Entry("app.csv", "s3://b/a/app.csv", False, 2048, 1706105124000),
    ]


def test_parse_rows_uses_is_directory_column_and_any_column_order():
    columns = ["name", "is_directory", "modification_time", "path", "size"]
    rows = [["logs", "true", "0", "s3://b/logs", "0"], ["x.txt", "false", "5", "s3://b/x.txt", "3"]]
    entries = parse_rows(columns, rows, "s3://b/")
    assert entries[0] == Entry("logs", "s3://b/logs/", True, 0, 0)
    assert entries[1] == Entry("x.txt", "s3://b/x.txt", False, 3, 5)


def test_parse_rows_fills_missing_values():
    rows = [[None, "only-name.txt", None, None], ["s3://b/p/from-path.txt", None, "1", "2"]]
    entries = parse_rows(COLUMNS, rows, "s3://b/p/")
    assert entries[0] == Entry("only-name.txt", "s3://b/p/only-name.txt", False, None, None)
    assert entries[1].name == "from-path.txt"


def test_parse_rows_skips_rows_without_any_name():
    assert parse_rows(COLUMNS, [[None, None, "0", "0"]], "s3://b/") == []


def test_parse_rows_column_names_ignore_case():
    rows = [["s3://b/x", "x", "10", "20"]]
    assert parse_rows(["PATH", "Name", "SIZE", "Modification_Time"], rows, "s3://b/")[0].size == 10


def test_entry_modified():
    assert Entry("a", "s3://b/a", False, 1, 0).modified is None
    assert Entry("a", "s3://b/a", False, 1, 1706105124000).modified == datetime(
        2024, 1, 24, 14, 5, 24, tzinfo=timezone.utc
    )


ENTRIES = [
    Entry("beta.csv", "s3://b/beta.csv", False, 10, 300),
    Entry("Alpha.csv", "s3://b/Alpha.csv", False, 500, 100),
    Entry("zeta", "s3://b/zeta/", True, 0, 0),
    Entry("gamma.csv", "s3://b/gamma.csv", False, 500, 200),
    Entry("delta", "s3://b/delta/", True, 0, 0),
]


def names(entries):
    return [e.name for e in entries]


def test_sort_by_name_puts_directories_first():
    assert names(sort_entries(ENTRIES, SortOrder.NAME)) == [
        "delta", "zeta", "Alpha.csv", "beta.csv", "gamma.csv"
    ]


def test_sort_by_size_largest_first_ties_by_name():
    assert names(sort_entries(ENTRIES, SortOrder.SIZE)) == [
        "delta", "zeta", "Alpha.csv", "gamma.csv", "beta.csv"
    ]


def test_sort_by_modified_newest_first():
    assert names(sort_entries(ENTRIES, SortOrder.MODIFIED)) == [
        "delta", "zeta", "beta.csv", "gamma.csv", "Alpha.csv"
    ]


def test_sort_order_cycles():
    assert SortOrder.NAME.next() is SortOrder.SIZE
    assert SortOrder.SIZE.next() is SortOrder.MODIFIED
    assert SortOrder.MODIFIED.next() is SortOrder.NAME


@pytest.mark.parametrize(
    ("size", "text"),
    [(None, ""), (0, "0 B"), (1023, "1023 B"), (1024, "1.0 KiB"), (1536, "1.5 KiB"),
     (5 * 1024**3, "5.0 GiB"), (3 * 1024**6, "3072.0 PiB")],
)
def test_human_size(size, text):
    assert human_size(size) == text


def test_local_time():
    assert local_time(None) == ""
    assert local_time(0) == ""
    ms = 1706105124000
    expected = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone()
    assert local_time(ms) == expected.strftime("%Y-%m-%d %H:%M")


def test_parse_rows_skips_the_marker_of_the_listed_folder():
    rows = [["s3://b/a/", "a/", "0", "0"], ["s3://b/a/", "", "0", "0"],
            ["s3://b/a/x.csv", "x.csv", "1", "2"]]
    assert [e.name for e in parse_rows(COLUMNS, rows, "s3://b/a/")] == ["x.csv"]

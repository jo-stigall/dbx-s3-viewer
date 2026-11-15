import pytest

from dbx_s3_viewer import paths
from dbx_s3_viewer.errors import InvalidPathError


def read_literal(statement: str) -> tuple[str, str]:
    """Parse `LIST '<literal>'<rest>` with Databricks string rules. Return (value, rest)."""
    assert statement.startswith("LIST '")
    out, i = [], len("LIST '")
    while True:
        ch = statement[i]
        if ch == "\\":
            out.append(statement[i + 1])
            i += 2
        elif ch == "'":
            return "".join(out), statement[i + 1:]
        else:
            out.append(ch)
            i += 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("s3://bucket/a/b", "s3://bucket/a/b"),
        ("  S3://Bucket/a/ ", "s3://Bucket/a/"),
        ("s3://bucket", "s3://bucket"),
        ("s3://bucket/it's here/", "s3://bucket/it's here/"),
        ("s3://bucket///", "s3://bucket/"),
        ("s3://bucket/a//", "s3://bucket/a/"),
    ],
)
def test_normalize_accepts(raw, expected):
    assert paths.normalize(raw) == expected


@pytest.mark.parametrize("raw", ["bucket/a", "s3:/bucket", "https://x/y", "s3://", "s3:///a", "",
                                 "s3://b/a\nb", "s3://b/\x00", "s3://   /a", "s3://my bucket/"])
def test_normalize_rejects(raw):
    with pytest.raises(InvalidPathError) as info:
        paths.normalize(raw)
    assert info.value.hint


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("s3://b/a/c.csv", "s3://b/a/"),
        ("s3://b/a/c/", "s3://b/a/"),
        ("s3://b/a/", "s3://b/"),
        ("s3://b/a", "s3://b/"),
        ("s3://b/", None),
        ("s3://b", None),
    ],
)
def test_parent(path, expected):
    assert paths.parent(path) == expected


def test_join():
    assert paths.join("s3://b/a", "c.csv") == "s3://b/a/c.csv"
    assert paths.join("s3://b/a/", "logs", is_dir=True) == "s3://b/a/logs/"
    assert paths.join("s3://b/", "logs/", is_dir=True) == "s3://b/logs/"


def test_basename_and_bucket():
    assert paths.basename("s3://b/a/logs/") == "logs"
    assert paths.basename("s3://b/a/x.csv") == "x.csv"
    assert paths.basename("s3://b/") == "b"
    assert paths.bucket("s3://b/a/") == "b"


def test_as_dir():
    assert paths.as_dir("s3://b/a") == "s3://b/a/"
    assert paths.as_dir("s3://b/a/") == "s3://b/a/"


@pytest.mark.parametrize(
    ("text", "escaped"),
    [
        ("plain", "plain"),
        ("it's", "it\\'s"),
        ("back\\slash", "back\\\\slash"),
        ("\\'", "\\\\\\'"),
        ("a\\nb", "a\\\\nb"),
    ],
)
def test_escape_sql_string(text, escaped):
    assert paths.escape_sql_string(text) == escaped


def test_list_statement_shape():
    assert paths.list_statement("s3://b/a", 1001) == "LIST 's3://b/a/' LIMIT 1001"
    assert paths.list_statement("s3://b/a/", 5) == "LIST 's3://b/a/' LIMIT 5"


@pytest.mark.parametrize(
    "path",
    [
        "s3://b/it's/",
        "s3://b/x' LIMIT 1; SELECT 1 --/",
        "s3://b/ends with backslash\\/",
        "s3://b/back\\'quote/",
        "s3://b/\\u0027/",
        "s3://b/unicodé ü/",
    ],
)
def test_list_statement_keeps_path_inside_the_literal(path):
    statement = paths.list_statement(path, 7)
    value, rest = read_literal(statement)
    assert value == path
    assert rest == " LIMIT 7"


def test_list_statement_rejects_bad_input():
    with pytest.raises(InvalidPathError):
        paths.list_statement("/local/path", 5)
    with pytest.raises(ValueError):
        paths.list_statement("s3://b/", 0)
    with pytest.raises(TypeError):
        paths.list_statement("s3://b/", "5")  # type: ignore[arg-type]


def test_check_file_path():
    assert paths.check_file_path(" s3://b/a/f.csv") == "s3://b/a/f.csv"
    for folder in ("s3://b/a/", "s3://b", "s3://b/"):
        with pytest.raises(InvalidPathError, match="folder"):
            paths.check_file_path(folder)
    for name in ("s3://b/f*.csv", "s3://b/f?.csv", "s3://b/f[1].csv", "s3://b/{a,b}.csv"):
        with pytest.raises(InvalidPathError, match="cannot download"):
            paths.check_file_path(name)


def test_file_statements_keep_path_inside_the_literal():
    path = "s3://b/it's a \\ file.csv"
    stat = paths.file_stat_statement(path)
    assert stat.startswith(paths.STAT_PREFIX)
    assert stat.endswith(", format => 'binaryFile')")
    read = paths.file_read_statement(path, 4)
    assert read.startswith(paths.READ_PREFIX)
    assert "substr(content, i * 4 + 1, 4)" in read
    assert read.endswith("pieces AS i ORDER BY i")
    for statement in (stat, read):
        literal = statement.split("read_files(", 1)[1]
        value, rest = read_literal("LIST " + literal)
        assert value == path
        assert rest.startswith(", format => 'binaryFile')")


def test_every_builder_matches_the_allowed_prefixes():
    built = [
        paths.list_statement("s3://b/", 5),
        paths.file_stat_statement("s3://b/f"),
        paths.file_read_statement("s3://b/f"),
    ]
    assert all(s.startswith(paths.ALLOWED_PREFIXES) for s in built)
    assert not "SELECT 1".startswith(paths.ALLOWED_PREFIXES)
    assert not "DELETE FROM t".startswith(paths.ALLOWED_PREFIXES)


def test_read_statement_rejects_bad_piece():
    with pytest.raises(ValueError):
        paths.file_read_statement("s3://b/f", 0)

import threading

import pytest

from dbx_s3_viewer import downloads
from dbx_s3_viewer.errors import BackendError, DownloadCancelled, DownloadError
from dbx_s3_viewer.model import FileContent


def content(*pieces: bytes, size: int | None = None) -> FileContent:
    total = sum(len(p) for p in pieces) if size is None else size
    return FileContent("s3://b/f.bin", total, iter(pieces))


@pytest.mark.parametrize(
    ("name", "windows", "expected"),
    [
        ("report.csv", False, "report.csv"),
        ("run:01.csv", False, "run:01.csv"),
        ("run:01.csv", True, "run_01.csv"),
        ('a<b>c"d|e?f*g\\h.txt', True, "a_b_c_d_e_f_g_h.txt"),
        ("tab\there", False, "tab_here"),
        ("CON.txt", True, "_CON.txt"),
        ("ends with dot.", True, "ends with dot"),
        ("..", False, "download"),
        ("", True, "download"),
    ],
)
def test_safe_filename(name, windows, expected):
    assert downloads.safe_filename(name, windows=windows) == expected


def test_free_target(tmp_path):
    assert downloads.free_target(tmp_path, "a.tar.gz") == tmp_path / "a.tar.gz"
    (tmp_path / "a.tar.gz").write_bytes(b"x")
    assert downloads.free_target(tmp_path, "a.tar.gz") == tmp_path / "a (1).tar.gz"
    (tmp_path / "a (1).tar.gz.part").write_bytes(b"x")
    assert downloads.free_target(tmp_path, "a.tar.gz") == tmp_path / "a (2).tar.gz"
    (tmp_path / ".env").write_bytes(b"x")
    assert downloads.free_target(tmp_path, ".env") == tmp_path / ".env (1)"


def test_check_target(tmp_path):
    (tmp_path / "there.txt").write_bytes(b"x")
    with pytest.raises(DownloadError, match="already exists"):
        downloads.check_target(tmp_path / "there.txt")
    with pytest.raises(DownloadError, match="does not exist"):
        downloads.check_target(tmp_path / "missing" / "f.txt")
    assert downloads.check_target(tmp_path / "new.txt") == tmp_path / "new.txt"


def test_save_writes_all_pieces_and_reports_progress(tmp_path):
    seen = []
    target = tmp_path / "f.bin"
    written = downloads.save(content(b"abc", b"de", b""), target,
                             on_bytes=lambda w, t: seen.append((w, t)))
    assert written == 5
    assert target.read_bytes() == b"abcde"
    assert seen == [(3, 5), (5, 5), (5, 5)]
    assert list(tmp_path.iterdir()) == [target]


def test_save_empty_file(tmp_path):
    target = tmp_path / "empty"
    assert downloads.save(content(size=0), target) == 0
    assert target.read_bytes() == b""


def test_save_removes_part_file_when_size_is_wrong(tmp_path):
    with pytest.raises(BackendError, match="incomplete"):
        downloads.save(content(b"abc", size=10), tmp_path / "f.bin")
    assert list(tmp_path.iterdir()) == []


def test_save_removes_part_file_when_the_source_fails(tmp_path):
    def pieces():
        yield b"abc"
        raise BackendError("network gone")

    with pytest.raises(BackendError):
        downloads.save(FileContent("s3://b/f", 6, pieces()), tmp_path / "f.bin")
    assert list(tmp_path.iterdir()) == []


def test_save_cancel_removes_part_file(tmp_path):
    cancel = threading.Event()

    def pieces():
        yield b"abc"
        cancel.set()
        yield b"def"

    with pytest.raises(DownloadCancelled):
        downloads.save(FileContent("s3://b/f", 6, pieces()), tmp_path / "f.bin", cancel=cancel)
    assert list(tmp_path.iterdir()) == []


def test_save_opens_no_file_before_the_first_piece(tmp_path):
    def pieces():
        assert list(tmp_path.iterdir()) == []
        yield b"x"

    downloads.save(FileContent("s3://b/f", 1, pieces()), tmp_path / "f.bin")


def test_save_keeps_a_part_file_that_it_did_not_create(tmp_path):
    other = tmp_path / "f.bin.part"
    other.write_bytes(b"someone else")
    with pytest.raises(DownloadError, match="Another download"):
        downloads.save(content(b"abc"), tmp_path / "f.bin")
    assert other.read_bytes() == b"someone else"


def test_save_never_replaces_a_file(tmp_path):
    target = tmp_path / "f.bin"
    target.write_bytes(b"keep me")
    with pytest.raises(DownloadError):
        downloads.save(content(b"abc"), target)
    assert target.read_bytes() == b"keep me"


def test_describe():
    assert downloads.describe(0, 0) == "100% (0 B of 0 B)"
    assert downloads.describe(512, 2048) == "25% (512 B of 2.0 KiB)"


def test_free_target_keeps_the_extension_of_dot_files(tmp_path):
    (tmp_path / ".config.json").write_bytes(b"x")
    assert downloads.free_target(tmp_path, ".config.json").name == ".config (1).json"


def test_check_target_refuses_an_existing_part_file_before_any_query(tmp_path):
    (tmp_path / "f.csv.part").write_bytes(b"x")
    with pytest.raises(DownloadError, match=r"\.part"):
        downloads.check_target(tmp_path / "f.csv")


def test_check_target_refuses_a_broken_symlink(tmp_path):
    (tmp_path / "link.csv").symlink_to(tmp_path / "nowhere")
    with pytest.raises(DownloadError, match="already exists"):
        downloads.check_target(tmp_path / "link.csv")
    assert downloads.free_target(tmp_path, "link.csv").name == "link (1).csv"

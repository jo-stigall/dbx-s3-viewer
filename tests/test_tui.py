"""Textual pilot tests: drive the browser with key presses against the fake backend."""

from __future__ import annotations

import asyncio
import threading

from textual.widgets import Input, Static

from dbx_s3_viewer.databricks_backend import MSG_STARTING
from dbx_s3_viewer.errors import AuthError, PermissionDeniedError
from dbx_s3_viewer.fake_backend import FakeBackend, demo_backend
from dbx_s3_viewer.model import Entry, Location
from dbx_s3_viewer.tui.app import EntryTable, ViewerApp
from dbx_s3_viewer.tui.screens import GoToScreen, HelpScreen

T0 = 1_700_000_000_000

OBJECTS = {
    "s3://data/raw/2026/a.csv": (100, T0 + 1000),
    "s3://data/raw/2026/b.csv": (5000, T0),
    "s3://data/raw/2025/old.csv": (1, T0),
    "s3://data/raw/readme.md": (10, T0),
    "s3://data/curated/table/part-0.parquet": (999, T0),
    "s3://data/top.txt": (3, T0),
}
LOCATIONS = [Location("data", "s3://data/", "external location")]


def fake(**kwargs) -> FakeBackend:
    kwargs.setdefault("locations", LOCATIONS)
    return FakeBackend.from_objects(OBJECTS, **kwargs)


def app_for(backend, *, bookmarks=(), saved=None, copied=None, start=None,
            start_folder=None) -> ViewerApp:
    def save(value):
        saved.append(list(value) if isinstance(value, list) else value)

    def copy(text):
        copied.append(text)
        return True

    return ViewerApp(
        backend,
        bookmarks=list(bookmarks),
        save_bookmarks=save if saved is not None else None,
        start_path=start,
        start_folder=start_folder,
        save_start_folder=save if saved is not None else None,
        copy=copy if copied is not None else None,
    )


async def settle(pilot, timeout: float = 5.0) -> None:
    """Wait until no listing or location load is running."""
    app = pilot.app
    for _ in range(int(timeout / 0.02)):
        await pilot.pause(0.02)
        if not app.loading and not app._locations_loading:
            await pilot.pause()
            return
    raise AssertionError("the app did not finish loading")


def names(app: ViewerApp) -> list[str]:
    return [row.name for row in app._rows]


def run(coro):
    asyncio.run(coro)


def test_home_shows_bookmarks_then_external_locations():
    async def scenario():
        app = app_for(fake(), bookmarks=["s3://data/raw/"])
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.location is None
            assert [(r.url, r.source) for r in app._rows] == [
                ("s3://data/raw/", "bookmark"), ("s3://data/", "external location")]
    run(scenario())


def test_navigate_with_keys_and_go_back_to_the_folder_you_came_from():
    async def scenario():
        backend = fake()
        app = app_for(backend)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("enter")  # open external location s3://data/
            await settle(pilot)
            assert app.location == "s3://data/"
            assert names(app) == ["curated", "raw", "top.txt"]
            await pilot.press("j", "l")  # down to raw, open it
            await settle(pilot)
            assert app.location == "s3://data/raw/"
            assert names(app) == ["2025", "2026", "readme.md"]
            await pilot.press("j", "right")  # 2026, open with the arrow key
            await settle(pilot)
            assert names(app) == ["a.csv", "b.csv"]
            await pilot.press("h")
            await settle(pilot)
            assert app.location == "s3://data/raw/"
            assert app.highlighted().name == "2026"  # cursor is back on the child
            await pilot.press("backspace")
            await settle(pilot)
            assert app.location == "s3://data/"
            assert app.highlighted().name == "raw"
            await pilot.press("left")  # from the bucket root to home
            await settle(pilot)
            assert app.location is None
            assert app.highlighted().url == "s3://data/"
            await pilot.press("k", "k")  # cannot move above the first row
            assert app.highlighted().url == "s3://data/"
    run(scenario())


def test_cache_makes_going_back_instant_and_r_bypasses_it():
    async def scenario():
        backend = fake()
        app = app_for(backend, start="s3://data/raw/")
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("j", "enter")
            await settle(pilot)
            await pilot.press("backspace")
            await pilot.pause()
            # Back to a cached folder: no new call, no loading state.
            assert not app.loading
            assert backend.calls == ["s3://data/raw/", "s3://data/raw/2026/"]
            await pilot.press("r")
            await settle(pilot)
            assert backend.calls[-1] == "s3://data/raw/"
            assert len(backend.calls) == 3
    run(scenario())


def test_open_on_a_file_does_nothing_but_explain():
    async def scenario():
        app = app_for(fake(), start="s3://data/raw/2026/")
        async with app.run_test(notifications=True) as pilot:
            await settle(pilot)
            await pilot.press("enter")
            await pilot.pause()
            assert app.location == "s3://data/raw/2026/"
            assert any("does not open files" in n.message for n in app._notifications)
    run(scenario())


def test_filter_and_escape():
    async def scenario():
        app = app_for(fake(), start="s3://data/raw/")
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("slash")
            field = app.query_one("#filter", Input)
            assert field.display and field.has_focus
            await pilot.press("2", "0", "2", "q")  # "q" goes into the filter, it does not quit
            assert app.is_running
            assert app.filter_text == "202q"
            assert names(app) == []
            await pilot.press("backspace")
            assert names(app) == ["2025", "2026"]
            await pilot.press("enter")  # keep the filter, back to the table
            assert app.query_one("#table").has_focus
            assert names(app) == ["2025", "2026"]
            await pilot.press("escape")
            assert app.filter_text == ""
            assert names(app) == ["2025", "2026", "readme.md"]
            assert not field.display
    run(scenario())


def test_filtered_rows_show_full_names():
    async def scenario():
        app = app_for(fake(), start="s3://data/raw/")
        async with app.run_test(size=(100, 24)) as pilot:
            await settle(pilot)
            await pilot.press("slash", "r", "e", "a")
            await pilot.pause(0.3)
            table = app.query_one("#table")
            line = "".join(segment.text for segment in table.render_line(1))
            assert "readme.md" in line
            assert "10 B" in line
    run(scenario())


def test_no_command_palette():
    async def scenario():
        app = app_for(fake())
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("ctrl+p")
            await pilot.pause()
            assert app.screen is app.screen_stack[0]
    run(scenario())


def test_sort_cycles_and_keeps_directories_first():
    async def scenario():
        app = app_for(fake(), start="s3://data/raw/2026/")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert names(app) == ["a.csv", "b.csv"]
            await pilot.press("s")  # size, largest first
            assert names(app) == ["b.csv", "a.csv"]
            await pilot.press("s")  # newest first
            assert names(app) == ["a.csv", "b.csv"]
            await pilot.press("s")  # back to name
            assert app.sort.value == "name"
    run(scenario())


def test_truncated_listing_shows_notice_and_status():
    async def scenario():
        objects = {f"s3://big/f{i:03d}": (1, T0) for i in range(12)}
        backend = FakeBackend.from_objects(objects, limit=10)
        app = app_for(backend, start="s3://big/")
        async with app.run_test(size=(120, 30)) as pilot:
            await settle(pilot)
            assert len(app._rows) == 10
            notice = app.query_one("#notice", Static)
            assert notice.display
            assert "Truncated" in str(notice.render())
            info = str(app.query_one("#status-info", Static).render())
            assert "10+ items (truncated)" in info
    run(scenario())


def test_permission_error_shows_what_to_do():
    async def scenario():
        denied = PermissionDeniedError("You do not have permission to list s3://data/raw/.",
                                       hint="Ask a Unity Catalog admin for READ FILES.",
                                       detail="[INSUFFICIENT_PERMISSIONS] nope")
        app = app_for(fake(failures={"s3://data/raw/": denied}), start="s3://data/")
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("j", "enter")
            await settle(pilot)
            panel = app.query_one("#message", Static)
            assert panel.display and not app.query_one("#table").display
            text = str(panel.render())
            assert "Permission denied" in text
            assert "Ask a Unity Catalog admin" in text
            assert "[INSUFFICIENT_PERMISSIONS] nope" in text
            assert "Permission denied" in str(app.query_one("#status-state", Static).render())
            await pilot.press("backspace")  # going up still works
            await settle(pilot)
            assert app.location == "s3://data/"
            assert app.error is None
    run(scenario())


def test_missing_path_and_empty_folder():
    async def scenario():
        backend = fake(empty_dirs=["s3://data/empty/"])
        app = app_for(backend, start="s3://data/nope/")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert "Path not found" in str(app.query_one("#message", Static).render())
            await pilot.press("g")
            await pilot.pause()
            field = app.screen.query_one(Input)
            field.value = "s3://data/empty"
            await pilot.press("enter")
            await settle(pilot)
            assert app.location == "s3://data/empty/"
            assert "No objects at this path" in str(app.query_one("#message", Static).render())
    run(scenario())


def test_goto_rejects_invalid_path_and_esc_cancels():
    async def scenario():
        app = app_for(fake())
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("g")
            await pilot.pause()
            assert isinstance(app.screen, GoToScreen)
            app.screen.query_one(Input).value = "not-a-path"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, GoToScreen)
            assert "not an s3:// path" in str(app.screen.query_one("#goto-error", Static).render())
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, GoToScreen)
            assert app.location is None
    run(scenario())


def test_copy_path():
    async def scenario():
        copied: list[str] = []
        app = app_for(fake(), start="s3://data/raw/", copied=copied)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("j", "y")
            assert copied == ["s3://data/raw/2026/"]
    run(scenario())


def test_bookmark_add_and_remove():
    async def scenario():
        saved: list[list[str]] = []
        app = app_for(fake(), start="s3://data/raw/", saved=saved)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("b")
            assert saved[-1] == ["s3://data/raw/"]
            assert app.bookmarks == ["s3://data/raw/"]
            await pilot.press("backspace", "backspace")  # up to s3://data/, then home
            await settle(pilot)
            assert app.location is None
            await pilot.press("k")  # the bookmark is the first row
            assert app.highlighted().source == "bookmark"
            await pilot.press("b")
            assert saved[-1] == []
            assert [r.source for r in app._rows] == ["external location"]
    run(scenario())


def test_demo_mode_does_not_save_bookmarks():
    async def scenario():
        app = app_for(fake(), start="s3://data/")
        async with app.run_test(notifications=True) as pilot:
            await settle(pilot)
            await pilot.press("b")
            assert app.bookmarks == ["s3://data/"]
            assert any("not saved" in n.message for n in app._notifications)
    run(scenario())


def test_denied_external_locations_show_bookmarks_only():
    async def scenario():
        backend = fake(locations_error=PermissionDeniedError("no"))
        app = app_for(backend, bookmarks=["s3://data/raw/"])
        async with app.run_test() as pilot:
            await settle(pilot)
            assert [r.source for r in app._rows] == ["bookmark"]
            notice = app.query_one("#notice", Static)
            assert notice.display and "hidden" in str(notice.render())
    run(scenario())


def test_auth_error_on_home_is_explained():
    async def scenario():
        backend = fake(locations_error=AuthError("Databricks did not accept your login.",
                                                 hint="Run `databricks auth login`."))
        app = app_for(backend)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert "databricks auth login" in str(app.query_one("#notice", Static).render())
    run(scenario())


class Gate:
    """A sleep function that blocks until the test opens the gate."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def sleep(self, _seconds):
        self.entered.set()
        assert self.release.wait(10)


def test_spinner_and_warehouse_start_message_then_result():
    async def scenario():
        gate = Gate()
        backend = fake(cold_start=60, sleep=gate.sleep)
        app = app_for(backend, start="s3://data/")
        async with app.run_test(size=(120, 30)) as pilot:
            await asyncio.get_running_loop().run_in_executor(None, gate.entered.wait, 5)
            await pilot.pause(0.3)
            assert app.loading
            state = str(app.query_one("#status-state", Static).render())
            assert MSG_STARTING in state
            assert MSG_STARTING in str(app.query_one("#message", Static).render())
            # The UI still answers keys while the call waits.
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
            gate.release.set()
            await settle(pilot)
            assert names(app) == ["curated", "raw", "top.txt"]
    run(scenario())


def test_quit_while_a_call_is_still_waiting():
    async def scenario():
        gate = Gate()
        app = app_for(fake(cold_start=60, sleep=gate.sleep), start="s3://data/")
        async with app.run_test() as pilot:
            await asyncio.get_running_loop().run_in_executor(None, gate.entered.wait, 5)
            await pilot.press("q")
            await pilot.pause()
        assert not app.is_running
        gate.release.set()
    run(scenario())


def test_stale_result_does_not_replace_the_current_folder():
    async def scenario():
        gate = Gate()
        backend = fake(cold_start=60, sleep=gate.sleep)
        app = app_for(backend, start="s3://data/raw/")
        async with app.run_test() as pilot:
            await asyncio.get_running_loop().run_in_executor(None, gate.entered.wait, 5)
            await pilot.press("backspace")  # leave while raw/ still loads
            await settle(pilot)
            assert app.location == "s3://data/"
            gate.release.set()
            await pilot.pause(0.3)
            assert app.location == "s3://data/"
            assert names(app) == ["curated", "raw", "top.txt"]
    run(scenario())


def test_help_screen_opens_and_closes():
    async def scenario():
        app = app_for(fake())
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("q")  # q closes help, it does not quit
            await pilot.pause()
            assert app.is_running
            assert not isinstance(app.screen, HelpScreen)
    run(scenario())


def test_demo_backend_has_a_truncated_folder_and_a_denied_location():
    backend = demo_backend(delay=0, cold_start=0)
    listing = backend.list("s3://acme-landing/events/year=2026/month=11/day=12/")
    assert listing.truncated and len(listing.entries) == 1000
    assert isinstance(listing.entries[0], Entry)
    try:
        backend.list("s3://acme-restricted/")
    except PermissionDeniedError:
        pass
    else:
        raise AssertionError("expected PermissionDeniedError")


# -- downloads ------------------------------------------------------------

from dbx_s3_viewer.config import Settings  # noqa: E402
from dbx_s3_viewer.tui.screens import DownloadScreen  # noqa: E402

README = "s3://data/raw/readme.md"


def download_app(backend, tmp_path, start="s3://data/raw/") -> ViewerApp:
    settings = Settings(tmp_path / "config.toml", None, None, "", 1000, 600, (),
                        download_dir=tmp_path)
    return ViewerApp(backend, bookmarks=[], save_bookmarks=None, start_path=start,
                     settings=settings)


async def wait_download(pilot, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.02)):
        await pilot.pause(0.02)
        if pilot.app.download is None:
            await pilot.pause()
            return
    raise AssertionError("the download did not finish")


def test_download_asks_then_saves(tmp_path):
    async def scenario():
        app = download_app(fake(contents={README: b"hello, laptop"}), tmp_path)
        async with app.run_test(notifications=True) as pilot:
            await settle(pilot)
            await pilot.press("j", "j")  # 2025, 2026, readme.md
            assert app.highlighted().name == "readme.md"
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, DownloadScreen)
            assert app.screen.query_one(Input).value == str(tmp_path / "readme.md")
            await pilot.press("enter")
            await wait_download(pilot)
            assert (tmp_path / "readme.md").read_bytes() == b"hello, laptop"
            assert any("Saved" in n.message for n in app._notifications)
            assert app.location == "s3://data/raw/"  # browsing state is unchanged
    run(scenario())


def test_download_needs_a_file(tmp_path):
    async def scenario():
        app = download_app(fake(), tmp_path)
        async with app.run_test(notifications=True) as pilot:
            await settle(pilot)
            await pilot.press("d")  # the cursor is on the folder 2025
            await pilot.pause()
            assert not isinstance(app.screen, DownloadScreen)
            assert any("Select a file" in n.message for n in app._notifications)
            assert list(tmp_path.iterdir()) == []
    run(scenario())


def test_download_dialog_never_replaces_a_file(tmp_path):
    async def scenario():
        (tmp_path / "readme.md").write_bytes(b"mine")
        app = download_app(fake(), tmp_path)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("j", "j", "d")
            await pilot.pause()
            field = app.screen.query_one(Input)
            assert field.value == str(tmp_path / "readme (1).md")
            field.value = str(tmp_path / "readme.md")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, DownloadScreen)
            assert "already exists" in str(app.screen.query_one("#download-error", Static).render())
            await pilot.press("escape")
            await pilot.pause()
            assert app.download is None
            assert (tmp_path / "readme.md").read_bytes() == b"mine"
    run(scenario())


def test_cancel_download_removes_the_partial_file(tmp_path):
    async def scenario():
        gate = Gate()
        backend = fake(contents={README: b"x" * (3 * 1024 * 1024)}, piece_delay=1,
                       sleep=gate.sleep)
        app = download_app(backend, tmp_path)
        async with app.run_test(size=(120, 30), notifications=True) as pilot:
            await settle(pilot)
            await pilot.press("j", "j", "d")
            await pilot.pause()
            await pilot.press("enter")
            await asyncio.get_running_loop().run_in_executor(None, gate.entered.wait, 5)
            await pilot.pause(0.2)
            line = app.query_one("#download", Static)
            assert line.display and "readme.md" in str(line.render())
            await pilot.press("x")
            await pilot.pause(0.2)
            # The download stays as "cancelling" until its thread has stopped.
            assert app.download is not None
            assert "cancelling" in str(line.render())
            await pilot.press("d")  # no second download while the first one stops
            await pilot.pause()
            assert not isinstance(app.screen, DownloadScreen)
            gate.release.set()
            await wait_download(pilot)
            assert not line.display
            assert any("No file was saved" in n.message for n in app._notifications)
            assert list(tmp_path.iterdir()) == []
    run(scenario())


def test_quit_during_download_asks_first(tmp_path):
    async def scenario():
        gate = Gate()
        backend = fake(contents={README: b"x" * 10}, piece_delay=1, sleep=gate.sleep)
        app = download_app(backend, tmp_path)
        async with app.run_test(notifications=True) as pilot:
            await settle(pilot)
            await pilot.press("j", "j", "d")
            await pilot.pause()
            await pilot.press("enter")
            await asyncio.get_running_loop().run_in_executor(None, gate.entered.wait, 5)
            await pilot.press("q")
            await pilot.pause()
            assert app.is_running
            assert any("Press q again" in n.message for n in app._notifications)
            await pilot.press("q")
            await pilot.pause()
        assert not app.is_running
        gate.release.set()
        assert not (tmp_path / "readme.md.part").exists()
    run(scenario())


def test_download_error_is_shown(tmp_path):
    async def scenario():
        app = download_app(fake(max_download=1), tmp_path)
        async with app.run_test(notifications=True) as pilot:
            await settle(pilot)
            await pilot.press("j", "j", "d")
            await pilot.pause()
            await pilot.press("enter")
            await wait_download(pilot)
            errors = [n for n in app._notifications if n.severity == "error"]
            assert errors and "Demo mode downloads files up to" in errors[0].message
            assert list(tmp_path.iterdir()) == []
    run(scenario())


# -- audit regressions ----------------------------------------------------


def test_app_survives_when_the_focused_table_disappears():
    """Shutdown removes the table while it has focus. The blur handler must not crash."""
    async def scenario():
        app = app_for(fake())
        async with app.run_test() as pilot:
            await settle(pilot)
            table = app.query_one(EntryTable)
            assert app.focused is table
            await table.remove()
            await pilot.pause(0.2)
            assert app.is_running
        assert app.return_code != 1  # the old handler crashed the app with NoMatches
    run(scenario())


def test_home_filter_without_matches_says_so():
    async def scenario():
        app = app_for(fake(), bookmarks=["s3://data/raw/"])
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("slash", "z", "z")
            await pilot.pause()
            panel = app.query_one("#message", Static)
            assert panel.display
            assert "No names contain 'zz'" in str(panel.render())
    run(scenario())


def test_download_dialog_rejects_empty_input_and_names_files_per_folder(tmp_path):
    async def scenario():
        (tmp_path / "readme.md").write_bytes(b"mine")
        other = tmp_path / "other"
        other.mkdir()
        app = download_app(fake(contents={README: b"hi"}), tmp_path)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("j", "j", "d")
            await pilot.pause()
            field = app.screen.query_one(Input)
            assert field.value == str(tmp_path / "readme (1).md")
            field.value = "   "
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, DownloadScreen)
            assert "Type the path" in str(app.screen.query_one("#download-error", Static).render())
            field.value = str(other)  # a folder where "readme.md" is free
            await pilot.press("enter")
            await wait_download(pilot)
            assert (other / "readme.md").read_bytes() == b"hi"
    run(scenario())


def kinds(app: ViewerApp) -> list[tuple[str, str]]:
    """Return (url, Type cell) for each home row."""
    table = app.query_one(EntryTable)
    return [(row.url, str(table.get_row_at(i)[2])) for i, row in enumerate(app._rows)]


def test_star_saves_the_start_folder_and_tilde_goes_home():
    async def scenario():
        saved: list = []
        app = app_for(fake(), start="s3://data/raw/2026/", saved=saved)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("asterisk")
            assert saved == ["s3://data/raw/2026/"]
            assert app.start_folder == "s3://data/raw/2026/"
            assert "start folder" in str(app.query_one("#status-info", Static).render())
            await pilot.press("tilde")
            await settle(pilot)
            assert app.location is None
            # Not a bookmark or an external location, so it gets its own row at the top.
            assert kinds(app)[0] == ("s3://data/raw/2026/", "start folder")
            await pilot.press("enter")
            await settle(pilot)
            assert app.location == "s3://data/raw/2026/"
            await pilot.press("asterisk")  # again: clear it
            assert saved[-1] is None
            assert app.start_folder is None
            await pilot.press("tilde")
            await settle(pilot)
            assert [url for url, _ in kinds(app)] == ["s3://data/"]
    run(scenario())


def test_star_on_home_marks_the_selected_place():
    async def scenario():
        saved: list = []
        app = app_for(fake(), bookmarks=["s3://data/raw/"], saved=saved)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("j", "asterisk")  # the external location, s3://data/
            assert saved == ["s3://data/"]
            assert kinds(app) == [("s3://data/raw/", "bookmark"),
                                  ("s3://data/", "external location · start folder")]
            await pilot.press("k", "asterisk")  # the bookmark replaces it
            assert saved[-1] == "s3://data/raw/"
            assert kinds(app)[0] == ("s3://data/raw/", "bookmark · start folder")
            assert kinds(app)[1] == ("s3://data/", "external location")
    run(scenario())


def test_app_opens_at_the_start_folder_and_demo_mode_does_not_save_it():
    async def scenario():
        app = app_for(fake(), start="s3://data/raw/", start_folder="s3://data/raw/")
        async with app.run_test(notifications=True) as pilot:
            await settle(pilot)
            assert app.location == "s3://data/raw/"
            await pilot.press("asterisk")
            assert app.start_folder is None
            assert any("not saved" in n.message for n in app._notifications)
    run(scenario())


def test_start_folder_that_fails_still_lets_you_go_home():
    async def scenario():
        backend = fake(failures={"s3://data/gone/": PermissionDeniedError("no")})
        app = app_for(backend, start="s3://data/gone/", start_folder="s3://data/gone/")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.error is not None
            assert "~ to go home" in str(app.query_one("#message", Static).render())
            await pilot.press("tilde")
            await settle(pilot)
            assert app.location is None
            assert kinds(app)[0] == ("s3://data/gone/", "start folder")
    run(scenario())

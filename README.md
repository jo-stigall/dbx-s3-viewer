# dbx-s3-viewer

dbx-s3-viewer is a fast, keyboard-driven terminal browser for S3 paths that Databricks Unity Catalog governs. It runs on your laptop and lists folders through `LIST` on a Databricks SQL warehouse, so you do not need AWS credentials or a notebook. It shows names, sizes and times. It can also download one file at a time, but only after you confirm it. It never changes or deletes anything in S3.

```
s3://acme-curated/sales/orders/                     12 items · sort: name A→Z
 Name                             Size        Modified
 _delta_log/
 part-00000-c000.snappy.parquet   469.3 MiB   2026-10-25 17:28
 part-00001-c000.snappy.parquet   122.9 MiB   2026-11-03 09:12
```

## What it does

- The home screen shows your bookmarks and the Unity Catalog external locations that you can see.
- Press `b` to bookmark a folder. Press `*` to make a folder your start folder: the browser then opens there each time.
- You move through folders with the arrow keys or with `h` `j` `k` `l`, like in `ranger` or `lf`.
- Folders come first. Sizes are easy to read (`1.5 KiB`). Times are in your local time zone.
- The tool keeps each listing in memory for the session, so going back is instant. Press `r` to list again.
- `d` downloads the selected file after you confirm the place to save it. See [Download a file](#download-a-file).
- `dbx-s3-viewer ls s3://path` prints one listing and exits. Add `--json` for scripts.
- `dbx-s3-viewer get s3://path/file` downloads one file from the command line.

What it does not do:

- It never writes, changes or deletes anything in S3. It sends `LIST`, and for downloads two `SELECT ... FROM read_files(...)` statements. It refuses any other statement.
- It does not show file contents on screen. A download saves the file to disk, and nothing else.
- It writes no file except its config file and the downloads that you confirm. It keeps no logs and sends no telemetry.

> **Downloads put file contents on your computer.** The buckets can hold sensitive data. Check that your team allows downloads for the data in a bucket before you press `d`.

## Install

You need Python 3.10 or later. Install the tool with `pipx` or `uv`. Both put the `dbx-s3-viewer` command on your PATH and keep its packages apart from your other Python work.

From a clone of this repository:

```bash
pipx install ./dbx-s3-viewer
```

```bash
uv tool install ./dbx-s3-viewer
```

From a Git server, replace the URL with the location of your copy:

```bash
pipx install git+https://github.com/your-org/dbx-s3-viewer
```

To try the browser before you set anything up, use made-up data. The demo sends nothing to Databricks, and writes no file unless you download one:

```bash
dbx-s3-viewer --demo
```

## Log in to Databricks

dbx-s3-viewer uses the login of the Databricks SDK. It never asks for, stores or logs a token itself.

1. Install the Databricks CLI. See [Install or update the Databricks CLI](https://docs.databricks.com/aws/en/dev-tools/cli/install).
2. Log in. The command opens your browser and saves a profile in `~/.databrickscfg`:

   ```bash
   databricks auth login --host https://<your-workspace>.cloud.databricks.com --profile work
   ```

3. Use the profile with `--profile work`, or set `profile = "work"` in the config file.

The SDK also reads the `DATABRICKS_*` environment variables, for example `DATABRICKS_HOST` and `DATABRICKS_TOKEN`, and `DATABRICKS_CONFIG_PROFILE`.

## Find the SQL warehouse ID

`LIST` runs on a SQL warehouse, so the tool needs a warehouse ID.

1. In Databricks, open **SQL Warehouses** in the sidebar.
2. Select the warehouse that your team uses. You need the **Can use** permission on it.
3. Copy the ID. It is on the **Overview** tab. It is also the last part of the **HTTP path** on the **Connection details** tab. For example, the HTTP path `/sql/1.0/warehouses/1234567890abcdef` has the ID `1234567890abcdef`.

Give the ID to the tool in one of these ways. The first one that is set wins:

1. `--warehouse-id 1234567890abcdef`
2. The `DATABRICKS_WAREHOUSE_ID` environment variable
3. `warehouse_id = "1234567890abcdef"` in the config file

## First run

Test your setup on one folder that contains subfolders:

```bash
dbx-s3-viewer check s3://my-bucket/some/folder/ --profile work --warehouse-id 1234567890abcdef
```

`check` shows the profile and the warehouse that it uses, the warehouse state, the external locations that you can see, and the first rows that `LIST` returns. It also tells you if your warehouse accepts a parameter marker in `LIST`. It sends only `LIST` statements.

Then open the browser:

```bash
dbx-s3-viewer --profile work --warehouse-id 1234567890abcdef
```

To open the browser at a folder:

```bash
dbx-s3-viewer s3://my-bucket/landing/
```

## Bookmarks and the start folder

- **Bookmark:** open a folder and press `b`. The folder shows on the home screen. Press `b` again in the folder, or on its row on the home screen, to remove it.
- **Start folder:** open a folder and press `*`. Each time you start the browser without a path, it opens that folder, not the home screen. On the home screen, `*` uses the selected bookmark or external location. Press `*` again on the start folder to clear it. Then the browser starts at the home screen again.

For example, you press `*` in `s3://my-bucket/landing/`. The next time you run `dbx-s3-viewer`, it lists `s3://my-bucket/landing/` at once. Press `~` to go to the home screen. On the home screen, the Type column shows "start folder" next to that place.

A path on the command line wins over the start folder: `dbx-s3-viewer s3://other-bucket/` opens `s3://other-bucket/`. You can have one start folder, and any number of bookmarks. Both are saved in the [config file](#config-file).

If the warehouse is stopped, the first listing starts it. The status bar shows "SQL warehouse is starting" and a timer. This can take a few minutes. You can still move around, open help or quit.

## Keys

| Key | Action |
| --- | --- |
| `↑` `↓` or `j` `k` | Move the cursor |
| `Enter`, `l` or `→` | Open the folder, bookmark or external location |
| `Backspace`, `h` or `←` | Go up one level. From a bucket root, go to the home screen |
| `g` | Type a path to go to |
| `/` | Filter the current listing by name. `Enter` keeps the filter, `Esc` clears it |
| `s` | Change the sort: name, size (largest first), time (newest first). Folders stay first |
| `r` | Refresh: list the folder again and skip the cache. On the home screen, load the external locations again |
| `y` | Copy the full path of the selected row |
| `b` | Bookmark the open folder, or remove its bookmark. On the home screen, remove the selected bookmark |
| `*` | Make the open folder the start folder, or clear it. On the home screen, use the selected place |
| `~` | Go to the home screen |
| `d` | Download the selected file. A dialog shows the size and asks where to save it |
| `x` | Cancel the running download |
| `?` | Show help |
| `q` | Quit |

`y` uses the system clipboard (`pbcopy` on macOS, the Windows clipboard, or `wl-copy`, `xclip` or `xsel` on Linux). If none works, it sends the path to the terminal clipboard with the OSC 52 escape code, which many terminals support.

## Download a file

1. Select a file and press `d`.
2. The dialog shows the full path and the size. It suggests a place to save the file: `download_dir` from the config file, or your Downloads folder. Change the path if you want.
3. Press `Enter` to start, or `Esc` to cancel.

The download runs in the background, and a line above the status bar shows the progress. You can keep browsing. Press `x` to cancel. If you press `q` while a download runs, the tool asks you to press `q` again.

How it works:

1. The tool runs `SELECT count(*), sum(length) FROM read_files('<path>', format => 'binaryFile')` on the SQL warehouse. It continues only when exactly one file matches. This stops a folder from downloading by mistake, for example when `LIST` shows a folder as a file.
2. The tool runs a second `read_files` statement that returns the file as numbered pieces of 4 MiB, in base64.
3. Files up to 12 MiB come back inside the API response. Larger files come back through result links (Databricks "cloud fetch"): short-lived HTTPS links to Databricks result storage. The tool fetches these links without your Databricks token.
4. The tool writes the pieces to `<name>.part`, checks the size, and then renames the file. If the download fails or you cancel it, the tool removes the `.part` file.

Rules and limits:

- You need the same access as for listing: **Can use** on the warehouse, and access to read files in the external location (usually `READ FILES`).
- One file at a time, up to 2 GiB. `read_files` cannot return larger files. For larger files, use a notebook or a job.
- The tool never replaces a file. If the name exists, the dialog suggests `name (1).ext`.
- The tool does not download names that contain `*`, `?`, `[`, `]`, `{` or `}`, because `read_files` treats them as wildcards and could read other files.
- `read_files` may skip files whose names start with `_` or `.`, such as `_SUCCESS`. Then the tool reports "does not exist".
- A download uses warehouse time, like any query. A cold warehouse must start first.
- If your network blocks Databricks result storage, files larger than 12 MiB fail with a message that says so.
- On Windows, the tool replaces characters such as `:` and `?` in file names with `_`.
- In `--demo` mode, downloads write made-up text, up to 64 MiB.

## The `ls` command

```bash
dbx-s3-viewer ls s3://my-bucket/landing/
```

```
                         DIR  events/
2026-05-04 09:37     1.3 KiB  README.txt
```

With `--json`, the command prints one object. Times are Unix milliseconds (`modification_time`) and ISO 8601 in UTC (`modified`):

```json
{
  "path": "s3://my-bucket/landing/",
  "truncated": false,
  "limit": 1000,
  "count": 2,
  "entries": [
    {"name": "events", "path": "s3://my-bucket/landing/events/", "is_dir": true,
     "size": 0, "modification_time": 0, "modified": null},
    {"name": "README.txt", "path": "s3://my-bucket/landing/README.txt", "is_dir": false,
     "size": 1284, "modification_time": 1777887420000, "modified": "2026-05-04T09:37:00+00:00"}
  ]
}
```

`ls` takes the same `--profile`, `--warehouse-id`, `--limit` and `--demo` options as the browser.

## The `get` command

```bash
dbx-s3-viewer get s3://my-bucket/landing/README.txt
```

`get` downloads one file into the current folder and prints the path of the saved file. Use `-o` to choose a file name or a folder:

```bash
dbx-s3-viewer get s3://my-bucket/landing/README.txt -o ~/Desktop/
```

It works like `d` in the browser: one file, up to 2 GiB, and it never replaces a file. When stderr is a terminal, it shows the progress there. `Ctrl+C` stops the download and removes the `.part` file.

## Exit codes

`ls`, `get` and `check` use these exit codes:

- `0`: the command worked. A truncated listing also gives `0`, with a warning on stderr.
- `1`: the command failed, for example because of a login, permission, not-found or timeout problem.
- `2`: a wrong path, option or config file, or a local file problem (for example, the file exists).

## Config file

The config file is TOML. The tool reads it at start, and writes it only when you add or remove a bookmark, or set or clear the start folder. When it writes, it keeps your comments and other keys.

| Platform | Location |
| --- | --- |
| macOS | `~/Library/Application Support/dbx-s3-viewer/config.toml` |
| Linux | `~/.config/dbx-s3-viewer/config.toml` (or `$XDG_CONFIG_HOME/dbx-s3-viewer/config.toml`) |
| Windows | `%LOCALAPPDATA%\dbx-s3-viewer\config.toml` |

To use another file, set `DBX_S3_VIEWER_CONFIG` to its path. `dbx-s3-viewer --help` and the help screen (`?`) show the path in use.

Example:

```toml
warehouse_id = "1234567890abcdef"
profile = "work"
list_limit = 1000
timeout_seconds = 600
download_dir = "~/Downloads/databricks"
bookmarks = [
    "s3://my-bucket/landing/",
    "s3://my-bucket/curated/sales/",
]
start_folder = "s3://my-bucket/landing/"
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `warehouse_id` | string | none | The SQL warehouse that runs `LIST`. `--warehouse-id` and `DATABRICKS_WAREHOUSE_ID` override it. |
| `profile` | string | SDK default | The profile in `~/.databrickscfg`. `--profile` and `DATABRICKS_CONFIG_PROFILE` override it. |
| `list_limit` | whole number, 1 or more | `1000` | The most objects to show for one folder. `--limit` overrides it. |
| `timeout_seconds` | whole number, 1 or more | `600` | How long to wait for one listing, including a warehouse start. |
| `bookmarks` | list of `s3://` paths | `[]` | The folders on the home screen. Press `b` to add or remove one. |
| `start_folder` | `s3://` path | none (the home screen) | The folder the browser opens when you give no path. Press `*` to set or clear it. A path on the command line overrides it. |
| `download_dir` | string (a folder; `~` works) | your Downloads folder | Where `d` suggests to save a file. You can change the path in the dialog. `get` saves to the current folder. |

An unknown key or a wrong value stops the tool with a message that names the key.

## Limits

- **Truncated listings.** `LIST` has a `LIMIT` but no offset, so the tool cannot page through a large folder. It asks for `list_limit + 1` rows. If that extra row comes back, the folder has more objects, and the tool shows a "Truncated" notice. Sorting and filtering then work on the rows that were returned only. To see more, raise `list_limit`.
- **Empty or missing folder.** In S3, an empty folder and a folder that does not exist can look the same. Both can show "No objects at this path".
- **Folder detection.** If `LIST` returns an `is_directory` column, the tool uses it. If not, a row is a folder when its `name` or `path` ends with `/`. `dbx-s3-viewer check` shows which case your warehouse gives.
- **No connection.** If the workspace does not answer, for example because your VPN is off, the tool gives up after about a minute and says so. (The SDK alone would retry for five minutes per request.)
- **Permissions.** To list a path, you need the **Can use** permission on the warehouse, and Unity Catalog must give you access to the external location that contains the path (usually the `READ FILES` privilege). The home screen shows only the external locations that you can see. If you cannot list external locations, the home screen shows your bookmarks only.

## Privacy

- The tool sends `LIST` statements to your SQL warehouse, and for each download two `read_files` statements. It also reads warehouse details (to tell you when the warehouse is starting) and the external location list. For large downloads, it fetches the result links that Databricks gives. It sends nothing else.
- It writes the config file and the downloads that you confirm. A download is a `.part` file until it is complete. It keeps no logs, no history and no cache on disk. The listing cache is in memory and ends when you quit.
- The Databricks SDK and CLI handle the login. They can keep OAuth tokens in their own cache, for example after `databricks auth login`. dbx-s3-viewer does not read or write those files itself.

## Development

```bash
uv sync
uv run pytest
uv run dbx-s3-viewer --demo
```

The tests need no Databricks workspace:

- `tests/test_paths.py`, `tests/test_model.py`, `tests/test_config.py`: path handling, SQL escaping, row parsing, folder detection, sorting and the config file.
- `tests/test_downloads.py`: saving files, `.part` files, cancel, file names on Windows.
- `tests/test_databricks_backend.py`: the backend against a fake SDK client (polling, timeouts, error messages, chunks, downloads).
- `tests/test_sdk_http.py`: the real `databricks-sdk` against a small local HTTP server (`tests/mock_workspace.py`) that answers like a workspace, including result links for large downloads.
- `tests/test_tui.py`: Textual pilot tests that drive the browser with key presses against the in-memory fake.
- `tests/test_cli.py`: the `ls`, `get` and `check` commands.

The code is in `src/dbx_s3_viewer/`:

- `paths.py` checks and joins `s3://` paths, and builds every SQL statement the tool sends: `LIST` and the two `read_files` statements.
- `backend.py` defines the `list(path)`, `locations()` and `read_file(path)` interface. `databricks_backend.py` and `fake_backend.py` implement it.
- `downloads.py` saves a download to disk and picks safe file names.
- `model.py` parses rows, decides which rows are folders (`is_directory()`), sorts and formats.
- `config.py` reads and writes the config file.
- `cli.py` is the command line. `tui/` is the Textual browser.

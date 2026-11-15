from pathlib import Path

import pytest

from dbx_s3_viewer import config
from dbx_s3_viewer.errors import ConfigError


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_file_gives_defaults(tmp_path):
    cfg = config.load(tmp_path / "nope.toml")
    settings = config.resolve(cfg, env={})
    assert settings.list_limit == config.DEFAULT_LIMIT
    assert settings.timeout_seconds == config.DEFAULT_TIMEOUT
    assert settings.warehouse_id is None
    assert settings.bookmarks == ()


def test_load_all_keys(tmp_path):
    path = write(tmp_path, """
warehouse_id = "abc123"
profile = "work"
list_limit = 50
timeout_seconds = 90
bookmarks = ["s3://b/a", "S3://b/c/", "s3://b/a/"]
""")
    cfg = config.load(path)
    assert cfg.warehouse_id == "abc123"
    assert cfg.profile == "work"
    assert cfg.list_limit == 50
    assert cfg.timeout_seconds == 90
    assert cfg.bookmarks == ["s3://b/a/", "s3://b/c/"]


@pytest.mark.parametrize(
    "text",
    [
        "warehouse = 'x'",
        "list_limit = 0",
        "list_limit = 'many'",
        "list_limit = true",
        "warehouse_id = 5",
        "bookmarks = 's3://b/'",
        "bookmarks = ['/local']",
        "this is not toml",
    ],
)
def test_bad_config(tmp_path, text):
    with pytest.raises(ConfigError) as info:
        config.load(write(tmp_path, text))
    assert str(tmp_path) in info.value.message


def test_warehouse_precedence(tmp_path):
    cfg = config.load(write(tmp_path, 'warehouse_id = "from-file"'))
    env = {"DATABRICKS_WAREHOUSE_ID": "from-env"}
    assert config.resolve(cfg, warehouse_flag="from-flag", env=env).warehouse_id == "from-flag"
    settings = config.resolve(cfg, env=env)
    assert (settings.warehouse_id, settings.warehouse_source) == ("from-env",
                                                                   "DATABRICKS_WAREHOUSE_ID")
    settings = config.resolve(cfg, env={})
    assert (settings.warehouse_id, settings.warehouse_source) == ("from-file", "config file")


def test_profile_precedence(tmp_path):
    cfg = config.load(write(tmp_path, 'profile = "from-file"'))
    assert config.resolve(cfg, profile_flag="flag", env={}).profile == "flag"
    # The tool passes the variable on, so it can show the name in check, help and hints.
    assert config.resolve(cfg, env={"DATABRICKS_CONFIG_PROFILE": "env"}).profile == "env"
    assert config.resolve(cfg, profile_flag="flag",
                          env={"DATABRICKS_CONFIG_PROFILE": "env"}).profile == "flag"
    assert config.resolve(cfg, env={}).profile == "from-file"


def test_limit_flag(tmp_path):
    cfg = config.load(write(tmp_path, "list_limit = 50"))
    assert config.resolve(cfg, env={}).list_limit == 50
    assert config.resolve(cfg, limit_flag=7, env={}).list_limit == 7
    with pytest.raises(ConfigError):
        config.resolve(cfg, limit_flag=0, env={})


def test_default_path_override(tmp_path):
    target = tmp_path / "x.toml"
    assert config.default_config_path({"DBX_S3_VIEWER_CONFIG": str(target)}) == target
    assert config.default_config_path({}).name == "config.toml"


def test_save_bookmarks_keeps_comments_and_keys(tmp_path):
    path = write(tmp_path, '# my settings\nwarehouse_id = "abc"  # the team warehouse\n')
    config.save_bookmarks(path, ["s3://b/one/", "s3://b/two/"])
    text = path.read_text(encoding="utf-8")
    assert "# my settings" in text
    assert "# the team warehouse" in text
    cfg = config.load(path)
    assert cfg.warehouse_id == "abc"
    assert cfg.bookmarks == ["s3://b/one/", "s3://b/two/"]
    config.save_bookmarks(path, ["s3://b/two/"])
    assert config.load(path).bookmarks == ["s3://b/two/"]
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]


def test_save_bookmarks_creates_file_and_directory(tmp_path):
    path = tmp_path / "new" / "dir" / "config.toml"
    config.save_bookmarks(path, ["s3://b/it's/"])
    assert config.load(path).bookmarks == ["s3://b/it's/"]
    assert "warehouse_id" in path.read_text(encoding="utf-8")  # the commented example


def test_save_bookmarks_refuses_a_broken_file(tmp_path):
    path = write(tmp_path, "this is not toml")
    with pytest.raises(ConfigError):
        config.save_bookmarks(path, ["s3://b/"])
    assert path.read_text(encoding="utf-8") == "this is not toml"


def test_start_folder_is_checked_and_passed_on(tmp_path):
    cfg = config.load(write(tmp_path, 'start_folder = "S3://b/landing"'))
    assert cfg.start_folder == "s3://b/landing/"
    assert config.resolve(cfg, env={}).start_folder == "s3://b/landing/"
    assert config.load(write(tmp_path, 'start_folder = ""')).start_folder is None
    for bad in ('start_folder = "/local/dir"', "start_folder = 5"):
        with pytest.raises(ConfigError, match="start_folder"):
            config.load(write(tmp_path, bad))


def test_save_start_folder_sets_and_clears_the_key(tmp_path):
    path = write(tmp_path, '# mine\nwarehouse_id = "abc"\nbookmarks = ["s3://b/x/"]\n')
    config.save_start_folder(path, "s3://b/x/")
    cfg = config.load(path)
    assert (cfg.start_folder, cfg.warehouse_id, cfg.bookmarks) == ("s3://b/x/", "abc",
                                                                   ["s3://b/x/"])
    config.save_start_folder(path, None)
    text = path.read_text(encoding="utf-8")
    assert "start_folder =" not in text
    assert "# mine" in text
    assert config.load(path).start_folder is None
    config.save_start_folder(path, None)  # clearing twice is fine
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]


def test_save_start_folder_creates_the_file(tmp_path):
    path = tmp_path / "new" / "config.toml"
    config.save_start_folder(path, "s3://b/it's/")
    assert config.load(path).start_folder == "s3://b/it's/"

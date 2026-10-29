from dataclasses import fields
import os
from pathlib import Path
import sqlite3

import pytest

from demo_v2.config import DemoConfig
from demo_v2.persistence.database import DemoDatabase


def test_only_demo_environment_is_consulted(tmp_path):
    class DemoOnlyEnvironment(dict):
        def get(self, key, default=None):
            assert key.startswith("DEMO_"), key
            return super().get(key, default)

    settings = DemoConfig.from_env(DemoOnlyEnvironment(DEMO_DATA_DIR=str(tmp_path)))
    assert settings.db_path == tmp_path / "demo_v2.sqlite3"
    assert not any("private_key" in field.name for field in fields(settings))
    assert settings.login_password_hash == settings.session_secret == ""


def test_live_env_does_not_configure_demo(monkeypatch, tmp_path):
    # These are deliberately fake canaries, never production credentials.
    monkeypatch.setenv("LIVE_DB_PATH", str(tmp_path / "poly_live.sqlite3"))
    monkeypatch.setenv("LIVE_LOGIN_USERNAME", "fake-live-user")
    monkeypatch.setenv("LIVE_SESSION_SECRET", "fake-live-secret")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "fake-unused-key")
    monkeypatch.setenv("DEMO_DATA_DIR", str(tmp_path / "demo"))
    config = DemoConfig.from_env()
    assert config.db_path == tmp_path / "demo" / "demo_v2.sqlite3"
    assert config.login_username == config.session_secret == ""
    assert not config.data_dir.exists()


def test_auth_settings_are_separate_and_not_in_repr(tmp_path):
    config = DemoConfig.from_env({
        "DEMO_DATA_DIR": str(tmp_path),
        "DEMO_LOGIN_USERNAME": "fake-demo-user",
        "DEMO_LOGIN_PASSWORD_HASH": "fake-hash-not-a-credential",
        "DEMO_SESSION_SECRET": "fake-demo-secret",
    })
    assert config.login_username == "fake-demo-user"
    assert config.session_cookie_name == "demo_v2_session"
    assert config.session_cookie_path == "/demo/"
    assert all(value not in repr(config) for value in (
        "fake-demo-user", "fake-hash-not-a-credential", "fake-demo-secret",
    ))


@pytest.mark.parametrize("target", ["poly_live.sqlite3", "../demo_v2.sqlite3", "file:other?mode=ro"])
def test_arbitrary_db_targets_are_rejected(tmp_path, target):
    with pytest.raises(ValueError, match="DEMO_DB_PATH"):
        DemoConfig(data_dir=tmp_path, db_path=tmp_path / target)


def test_symlink_and_hardlink_aliases_are_rejected(tmp_path):
    foreign = tmp_path / "foreign.sqlite3"
    foreign.write_bytes(b"untouched sentinel")
    root = tmp_path / "demo"
    root.mkdir()
    path = root / "demo_v2.sqlite3"
    path.symlink_to(foreign)
    with pytest.raises(ValueError):
        DemoConfig(data_dir=root)
    path.unlink()
    os.link(foreign, path)
    with pytest.raises(ValueError, match="hard-link"):
        DemoConfig(data_dir=root)
    assert foreign.read_bytes() == b"untouched sentinel"


def test_symlink_swapped_after_configuration_is_rejected_before_sqlite(tmp_path, monkeypatch):
    config = DemoConfig(data_dir=tmp_path / "demo")
    config.data_dir.mkdir()
    config.db_path.symlink_to(tmp_path / "foreign.sqlite3")
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: pytest.fail("must not open SQLite"))
    with pytest.raises(ValueError):
        DemoDatabase(config).probe()


def test_probe_creates_only_dedicated_temp_database_and_no_schema(tmp_path):
    config = DemoConfig(data_dir=tmp_path / "demo")
    database = DemoDatabase(config)
    assert not config.data_dir.exists()
    assert database.probe()
    with database.connect() as conn:
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    assert {path.name for path in config.data_dir.iterdir()} == {"demo_v2.sqlite3"}


def test_foreign_schema_is_rejected_without_changing_it(tmp_path):
    config = DemoConfig(data_dir=tmp_path)
    with sqlite3.connect(config.db_path) as conn:
        conn.execute("CREATE TABLE live_positions (sentinel TEXT)")
        conn.execute("INSERT INTO live_positions VALUES ('unchanged')")
    before = config.db_path.read_bytes()
    with pytest.raises(ValueError, match="namespace"):
        DemoDatabase(config).probe()
    assert config.db_path.read_bytes() == before


@pytest.mark.parametrize("setting,value", [
    ("decision_queue_capacity", 0), ("telemetry_capacity", 0),
    ("observation_max_age_seconds", float("nan")),
    ("observation_max_age_seconds", -1), ("session_ttl_seconds", 0),
    ("session_cookie_name", "live_session"), ("session_cookie_path", "/"),
])
def test_invalid_isolation_and_stream_settings_fail_closed(tmp_path, setting, value):
    with pytest.raises(ValueError):
        DemoConfig(data_dir=tmp_path, **{setting: value})

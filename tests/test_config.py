"""Config file round-trips and environment overrides."""

import pytest

from charms_core.types import ConfigurationError
from charms_launcher import config as cfg


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHARMS_HOME", str(tmp_path))
    monkeypatch.delenv("RUNE_KEY", raising=False)
    monkeypatch.delenv("SERVER_URL", raising=False)
    monkeypatch.delenv("SEED_CACHE_PATH", raising=False)
    return tmp_path


def test_defaults_when_no_file() -> None:
    config = cfg.load_config()
    assert config.server_url == cfg.DEFAULT_SERVER_URL
    assert config.rune_key == ""
    assert config.env == {}


def test_save_and_load_roundtrip(isolated_home) -> None:  # type: ignore[no-untyped-def]
    saved = cfg.LauncherConfig(
        server_url="ws://example:8600", rune_key="rk_test", env={"API_KEY": "x"}
    )
    path = cfg.save_config(saved)
    assert path == isolated_home / "launcher.json"
    assert cfg.load_config() == saved


def test_environment_overrides_file(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cfg.save_config(cfg.LauncherConfig(server_url="ws://file:1", rune_key="rk_file"))
    monkeypatch.setenv("SERVER_URL", "ws://env:2")
    monkeypatch.setenv("RUNE_KEY", "rk_env")
    config = cfg.load_config()
    assert config.server_url == "ws://env:2"
    assert config.rune_key == "rk_env"


def test_http_url() -> None:
    assert cfg.LauncherConfig(server_url="ws://host:8600").http_url == "http://host:8600"
    assert cfg.LauncherConfig(server_url="wss://host/").http_url == "https://host"


def test_require_auth() -> None:
    with pytest.raises(ConfigurationError, match="rune key"):
        cfg.require_auth(cfg.LauncherConfig())
    config = cfg.LauncherConfig(rune_key="rk_x")
    assert cfg.require_auth(config) is config


def test_cache_dir_env_override(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert cfg.cache_dir() == cfg.charms_home() / "seed-cache"
    monkeypatch.setenv("SEED_CACHE_PATH", str(tmp_path / "custom"))
    assert cfg.cache_dir() == tmp_path / "custom"

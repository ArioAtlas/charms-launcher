"""
Local launcher state under ``~/.charms``.

Layout:

- ``launcher.json`` — server URL, rune key, and saved seed env vars
  (``{"server_url": ..., "rune_key": ..., "env": {...}}``).
- ``seed-cache/``   — model artifact downloads (``SEED_CACHE_PATH`` default).
- ``packages/``     — extracted pulled seed packages, one dir per seed id.
- ``envs/``         — per-seed virtualenvs for pulled seeds.
- ``rune-logs/``    — Rune Manager process logs.

Environment variables (``RUNE_KEY``, ``SERVER_URL``, ``SEED_CACHE_PATH``)
always win over the config file, so scripted setups keep working unchanged.
"""

import contextlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from charms_core.types import ConfigurationError

DEFAULT_SERVER_URL = "ws://localhost:8600"


def charms_home() -> Path:
    override = os.environ.get("CHARMS_HOME")  # tests point this at a tmp dir
    return Path(override) if override else Path.home() / ".charms"


def config_path() -> Path:
    return charms_home() / "launcher.json"


def cache_dir() -> Path:
    return Path(os.environ.get("SEED_CACHE_PATH") or charms_home() / "seed-cache")


def packages_dir() -> Path:
    return charms_home() / "packages"


def envs_dir() -> Path:
    return charms_home() / "envs"


def logs_dir() -> Path:
    return charms_home() / "rune-logs"


@dataclass
class LauncherConfig:
    server_url: str = DEFAULT_SERVER_URL
    rune_key: str = ""
    env: dict[str, str] = field(default_factory=dict)  # saved seed env vars

    @property
    def http_url(self) -> str:
        """The REST base URL for the configured server (ws:// → http://)."""
        return self.server_url.replace("ws://", "http://").replace("wss://", "https://").rstrip("/")


def load_config() -> LauncherConfig:
    """File config with environment overrides (env always wins)."""
    config = LauncherConfig()
    path = config_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        config.server_url = str(data.get("server_url") or config.server_url)
        config.rune_key = str(data.get("rune_key") or "")
        env = data.get("env")
        if isinstance(env, dict):
            config.env = {str(k): str(v) for k, v in env.items()}
    if os.environ.get("SERVER_URL"):
        config.server_url = os.environ["SERVER_URL"]
    if os.environ.get("RUNE_KEY"):
        config.rune_key = os.environ["RUNE_KEY"]
    return config


def save_config(config: LauncherConfig) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"server_url": config.server_url, "rune_key": config.rune_key, "env": config.env}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):  # best-effort: the rune key is a credential
        path.chmod(0o600)
    return path


def require_auth(config: LauncherConfig) -> LauncherConfig:
    if not config.rune_key:
        raise ConfigurationError(
            "no rune key configured — run `charms-launcher login` "
            "(create a key in the web UI → My Runes) or set RUNE_KEY"
        )
    return config

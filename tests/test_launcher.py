import os
from pathlib import Path

import pytest
from charms_seed_echo import EchoSeed

from charms_core import protocol
from charms_launcher.cli import _ws_url, load_env_file, load_seed_class
from charms_launcher.executor import SeedExecutor


async def test_executor_runs_task() -> None:
    result = await SeedExecutor(EchoSeed()).run_task("t1", {"text": "hi"}, None)
    assert isinstance(result, protocol.TaskResultMsg)
    assert result.output == {"text": "hi"}
    assert result.compute_ms >= 0


async def test_executor_applies_config() -> None:
    result = await SeedExecutor(EchoSeed()).run_task("t1", {"text": "hi"}, {"uppercase": True})
    assert isinstance(result, protocol.TaskResultMsg)
    assert result.output == {"text": "HI"}


async def test_executor_reports_invalid_input() -> None:
    result = await SeedExecutor(EchoSeed()).run_task("t1", {"wrong": 1}, None)
    assert isinstance(result, protocol.TaskErrorMsg)
    assert "invalid input" in result.error


def test_seed_entry_point_loading() -> None:
    assert load_seed_class("echo") is EchoSeed
    with pytest.raises(SystemExit):
        load_seed_class("does-not-exist")


def test_ws_url_normalization() -> None:
    assert _ws_url("http://host:8600/") == "ws://host:8600"
    assert _ws_url("https://host") == "wss://host"
    assert _ws_url("ws://host:8600") == "ws://host:8600"


def test_load_env_file_reads_cwd_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(
        "CHARMS_TEST_FROM_DOTENV=hello\nCHARMS_TEST_PRESET=from-file\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CHARMS_TEST_FROM_DOTENV", raising=False)
    monkeypatch.setenv("CHARMS_TEST_PRESET", "from-env")

    load_env_file()

    assert os.environ["CHARMS_TEST_FROM_DOTENV"] == "hello"
    # Real environment variables always win over .env entries.
    assert os.environ["CHARMS_TEST_PRESET"] == "from-env"
    monkeypatch.delenv("CHARMS_TEST_FROM_DOTENV", raising=False)

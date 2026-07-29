import os
from pathlib import Path

import pytest
from charms_core import protocol
from charms_core.seed import SeedDescriptor

from charms_launcher.cli import _pricing_cell, _ws_url, load_env_file, load_seed_class
from charms_launcher.executor import SeedExecutor
from charms_launcher.registry import SeedPackageInfo
from charms_seed_echo import EchoSeed


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


class _TokenEchoSeed(EchoSeed):
    """Echo variant that reports token usage, like the API-backed seeds do."""

    async def run(self, input, config=None):  # type: ignore[no-untyped-def]
        self.billable_units = 42.0
        return await super().run(input, config)


async def test_executor_reports_and_resets_billable_units() -> None:
    executor = SeedExecutor(_TokenEchoSeed())
    result = await executor.run_task("t1", {"text": "hi"}, None)
    assert isinstance(result, protocol.TaskResultMsg)
    assert result.billable_units == 42.0

    plain = await SeedExecutor(EchoSeed()).run_task("t2", {"text": "hi"}, None)
    assert isinstance(plain, protocol.TaskResultMsg)
    assert plain.billable_units is None


def test_seed_entry_point_loading() -> None:
    assert load_seed_class("echo") is EchoSeed
    with pytest.raises(SystemExit):
        load_seed_class("does-not-exist")


def test_descriptor_carries_work_spec() -> None:
    """Work specs ride rune registration via the descriptor's manifest."""
    descriptor = SeedDescriptor.from_seed(EchoSeed)
    assert descriptor.manifest.work is not None
    assert descriptor.manifest.work.unit == "kchar"
    assert descriptor.manifest.work.meter == "pre"


def _listing(**overrides: object) -> SeedPackageInfo:
    defaults: dict[str, object] = {"id": "demo", "name": "Demo", "version": "0.2.0"}
    return SeedPackageInfo.model_validate({**defaults, **overrides})


def test_pricing_cell_prefers_work_unit() -> None:
    assert _pricing_cell(_listing(work_unit="audio_minute", work_meter="pre")) == (
        "per audio_minute"
    )


def test_pricing_cell_flags_post_metered_units() -> None:
    assert _pricing_cell(_listing(work_unit="ktoken", work_meter="post")) == "per ktoken (post)"


def test_pricing_cell_falls_back_to_legacy_price() -> None:
    assert _pricing_cell(_listing(price_value=2, price_unit="mana_per_token")) == "2 token"


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

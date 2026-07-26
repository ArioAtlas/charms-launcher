"""Registry client over httpx.MockTransport (no network)."""

import httpx
import pytest

from charms_core.types import ConfigurationError
from charms_launcher import registry
from charms_launcher.config import LauncherConfig

CONFIG = LauncherConfig(server_url="ws://server:8600", rune_key="rk_test")


@pytest.fixture
def transport(monkeypatch):  # type: ignore[no-untyped-def]
    def install(handler):  # type: ignore[no-untyped-def]
        monkeypatch.setattr(registry, "_transport", httpx.MockTransport(handler))

    return install


def test_search_sends_bearer_and_parses(transport) -> None:  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer rk_test"
        assert request.url.path == "/api/seed-packages"
        assert request.url.params["scope"] == "all"
        assert request.url.params["query"] == "demo"
        return httpx.Response(
            200,
            json={
                "packages": [{"id": "demo", "name": "Demo", "version": "0.2.0", "online_runes": 3}]
            },
        )

    transport(handler)
    packages = registry.search_packages(CONFIG, query="demo")
    assert len(packages) == 1
    assert packages[0].id == "demo"
    assert packages[0].online_runes == 3


def test_rejected_key_raises(transport) -> None:  # type: ignore[no-untyped-def]
    transport(lambda request: httpx.Response(401, json={"detail": "invalid"}))
    with pytest.raises(ConfigurationError, match="rejected the rune key"):
        registry.search_packages(CONFIG)


def test_error_detail_surfaces(transport) -> None:  # type: ignore[no-untyped-def]
    transport(lambda request: httpx.Response(404, json={"detail": "seed package not found"}))
    with pytest.raises(ConfigurationError, match="seed package not found"):
        registry.get_package(CONFIG, "missing")


def test_download_writes_archive(transport, tmp_path) -> None:  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/seed-packages/demo/download"
        return httpx.Response(200, content=b"zip-bytes")

    transport(handler)
    path = registry.download_package(CONFIG, "demo", tmp_path / "downloads")
    assert path.read_bytes() == b"zip-bytes"


def test_unauthenticated_config_refuses() -> None:
    with pytest.raises(ConfigurationError, match="rune key"):
        registry.search_packages(LauncherConfig())

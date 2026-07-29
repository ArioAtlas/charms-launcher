"""
HTTP client for the server's seed package registry (``/api/seed-packages``).

Authenticates with the owner's rune key as a bearer token — the same key the
launcher registers runes with, so `login` once covers both paths.
"""

import contextlib
from pathlib import Path

import httpx
from charms_core.package import PyprojectInfo, SeedPackageManifest
from charms_core.types import ConfigurationError
from pydantic import BaseModel, Field

from charms_launcher.config import LauncherConfig, require_auth

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_transport: httpx.BaseTransport | None = None  # test seam (httpx.MockTransport)


class SeedPackageInfo(BaseModel):
    """One registry listing entry (subset of the server's SeedPackageOut)."""

    id: str
    name: str
    version: str
    description: str = ""
    author: str = ""
    owner_display_name: str = ""
    mine: bool = False
    visibility: str = "private"
    price_value: float = 0
    price_unit: str = "mana_per_second"
    # Work-unit pricing summary (charms.md §12.3). None means the package
    # declares no `work` block and clients fall back to price_value/price_unit.
    work_unit: str | None = None
    work_meter: str | None = None  # "pre" | "post"
    supports_streaming: bool = False
    size_bytes: int = 0
    sha256: str = ""
    online_runes: int = 0


class SeedPackageDetail(SeedPackageInfo):
    manifest: SeedPackageManifest
    pyproject: PyprojectInfo = Field(default_factory=lambda: PyprojectInfo(name="", version=""))
    readme: str = ""


def _client(config: LauncherConfig) -> httpx.Client:
    require_auth(config)
    return httpx.Client(
        base_url=config.http_url,
        headers={"Authorization": f"Bearer {config.rune_key}"},
        timeout=_TIMEOUT,
        transport=_transport,
    )


def _raise_for_status(response: httpx.Response, context: str) -> None:
    if response.status_code == 401:
        raise ConfigurationError("the server rejected the rune key — run `charms-launcher login`")
    if response.is_error:
        detail = ""
        with contextlib.suppress(ValueError, AttributeError):
            detail = str(response.json().get("detail", ""))
        raise ConfigurationError(f"{context} failed ({response.status_code}) {detail}".strip())


def search_packages(
    config: LauncherConfig, query: str = "", scope: str = "all"
) -> list[SeedPackageInfo]:
    """Everything the caller may pull: their own seeds plus public ones."""
    with _client(config) as client:
        response = client.get("/api/seed-packages", params={"scope": scope, "query": query})
        _raise_for_status(response, "seed search")
        payload = response.json()
    return [SeedPackageInfo.model_validate(entry) for entry in payload.get("packages", [])]


def get_package(config: LauncherConfig, seed_id: str) -> SeedPackageDetail:
    with _client(config) as client:
        response = client.get(f"/api/seed-packages/{seed_id}")
        _raise_for_status(response, f"fetching seed '{seed_id}'")
        return SeedPackageDetail.model_validate(response.json())


def download_package(config: LauncherConfig, seed_id: str, dest_dir: Path) -> Path:
    """Download the package zip into *dest_dir*; returns the archive path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{seed_id}.zip"
    with _client(config) as client:
        response = client.get(f"/api/seed-packages/{seed_id}/download")
        _raise_for_status(response, f"downloading seed '{seed_id}'")
        target.write_bytes(response.content)
    return target


def verify_login(config: LauncherConfig) -> int:
    """Check the rune key against the server; returns the pullable-seed count."""
    return len(search_packages(config))

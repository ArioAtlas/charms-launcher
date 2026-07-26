"""Seed-table helpers from the Rune Manager (pure logic — no Tk windows)."""

import pytest

pytest.importorskip("tkinter")  # gui.py imports tkinter at module scope

from charms_launcher.gui import SeedInfo, update_available, version_cell  # noqa: E402
from charms_launcher.registry import SeedPackageInfo  # noqa: E402


def listing(**overrides):  # type: ignore[no-untyped-def]
    defaults = {"id": "demo", "name": "Demo", "version": "0.2.0", "sha256": "b" * 8}
    return SeedPackageInfo(**{**defaults, **overrides})


def pulled(**overrides):  # type: ignore[no-untyped-def]
    defaults = {"id": "demo", "source": "pulled", "version": "0.1.0", "sha256": "a" * 8}
    return SeedInfo(**{**defaults, **overrides})


def test_update_available_on_sha_mismatch() -> None:
    assert update_available(pulled(), listing())


def test_update_available_same_sha() -> None:
    assert not update_available(pulled(sha256="x"), listing(sha256="x"))


def test_update_available_requires_pulled_source_and_both_shas() -> None:
    assert not update_available(pulled(source="local"), listing())
    assert not update_available(pulled(source="remote"), listing())
    assert not update_available(pulled(sha256=""), listing())
    assert not update_available(pulled(), listing(sha256=""))
    assert not update_available(pulled(), None)


def test_version_cell_shows_upgrade_arrow() -> None:
    assert version_cell(pulled(), listing()) == "0.1.0 → 0.2.0"


def test_version_cell_republished_same_version() -> None:
    assert version_cell(pulled(), listing(version="0.1.0")) == "0.1.0 (republished)"


def test_version_cell_plain_when_current_or_unknown() -> None:
    assert version_cell(pulled(sha256="x"), listing(sha256="x")) == "0.1.0"
    assert version_cell(pulled(), None) == "0.1.0"
    assert version_cell(SeedInfo(id="broken"), None) == "—"

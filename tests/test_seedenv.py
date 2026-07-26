"""Pulled-package extraction and env bootstrap logic (no real venvs)."""

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

from charms_core.types import SeedPackageError
from charms_launcher import seedenv

MANIFEST = {
    "schema_version": 1,
    "id": "demo",
    "name": "Demo",
    "version": "0.1.0",
    "price": {"value": 1, "unit": "mana_per_second"},
}

PYPROJECT = """
[project]
name = "charms-seed-demo"
version = "0.1.0"
dependencies = ["numpy>=2"]

[project.entry-points."charms.seeds"]
demo = "charms_seed_demo:DemoSeed"
"""


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHARMS_HOME", str(tmp_path / "home"))
    return tmp_path


def build_archive(tmp_path: Path, prefix: str = "") -> Path:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{prefix}manifest.json", json.dumps(MANIFEST))
        archive.writestr(f"{prefix}pyproject.toml", PYPROJECT)
        archive.writestr(f"{prefix}README.md", "# Demo")
        archive.writestr(f"{prefix}src/charms_seed_demo/__init__.py", "class DemoSeed: ...")
    path = tmp_path / "demo.zip"
    path.write_bytes(buffer.getvalue())
    return path


def test_filter_runtime_deps() -> None:
    deps = [
        "charms-core",
        "charms_core>=0.1",
        "charms-core[extra]==1.0",
        "numpy>=2",
        "torch==2.8; sys_platform == 'win32'",
    ]
    assert seedenv.filter_runtime_deps(deps) == [
        "numpy>=2",
        "torch==2.8; sys_platform == 'win32'",
    ]


def test_dependency_install_args_include_manifest_indexes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    archive = build_archive(tmp_path)
    pulled = seedenv.extract_archive(archive, "demo")
    pulled.package.manifest.install.extra_index_urls = [
        "https://download.pytorch.org/whl/cu128"
    ]
    assert seedenv.dependency_install_args(pulled.package) == [
        "--extra-index-url",
        "https://download.pytorch.org/whl/cu128",
        "numpy>=2",
    ]
    # no deps → no args at all (index flags alone would be a pip error)
    pulled.package.pyproject.dependencies = ["charms-core"]
    assert seedenv.dependency_install_args(pulled.package) == []


def test_venv_python_shape(tmp_path) -> None:  # type: ignore[no-untyped-def]
    python = seedenv.venv_python(tmp_path)
    if sys.platform == "win32":
        assert python == tmp_path / "Scripts" / "python.exe"
    else:
        assert python == tmp_path / "bin" / "python"


def test_runtime_sources_are_this_checkout() -> None:
    sources = seedenv.runtime_sources()
    assert sources != list(seedenv.RUNTIME_GIT_REQUIREMENTS)
    assert [Path(s).name for s in sources] == ["core", "launcher"]
    assert all((Path(s) / "pyproject.toml").is_file() for s in sources)


def test_extract_and_reload(tmp_path) -> None:  # type: ignore[no-untyped-def]
    archive = build_archive(tmp_path)
    pulled = seedenv.extract_archive(archive, "demo")
    assert pulled.package.manifest.id == "demo"
    assert (pulled.root / "manifest.json").is_file()
    assert pulled.sha256

    reloaded = seedenv.load_pulled("demo")
    assert reloaded is not None
    assert reloaded.package.manifest.name == "Demo"
    assert reloaded.sha256 == pulled.sha256
    assert [seed.seed_id for seed in seedenv.list_pulled()] == ["demo"]
    assert not reloaded.env_ready()  # no env built yet


def test_extract_folder_wrapped_archive(tmp_path) -> None:  # type: ignore[no-untyped-def]
    archive = build_archive(tmp_path, prefix="demo_seed/")
    pulled = seedenv.extract_archive(archive, "demo")
    assert pulled.root.name == "demo_seed"
    assert seedenv.load_pulled("demo") is not None


def test_extract_rejects_wrong_seed_id(tmp_path) -> None:  # type: ignore[no-untyped-def]
    archive = build_archive(tmp_path)
    with pytest.raises(SeedPackageError, match="expected 'other'"):
        seedenv.extract_archive(archive, "other")


def test_load_pulled_missing() -> None:
    assert seedenv.load_pulled("nope") is None
    assert seedenv.list_pulled() == []

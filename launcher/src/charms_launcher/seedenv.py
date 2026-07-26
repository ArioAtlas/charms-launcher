"""
Pulled seed packages and their isolated environments.

A pulled seed lives in two places under ``~/.charms``:

- ``packages/<seed_id>/`` — the extracted archive plus ``charms-package.json``
  (parsed metadata + archive sha256, written at pull time).
- ``envs/<seed_id>/``     — a virtualenv holding the launcher runtime
  (charms_launcher + its vendored charms_core), the seed's declared
  dependencies, and the seed package itself.

The seed's ``charms-core`` dependency (if declared) is filtered out — the
runtime provides those modules. Running a pulled seed spawns
``<env python> -m charms_launcher.cli run <seed_id>``; inside that env the
seed's entry point resolves like any installed package.
"""

import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from charms_core.package import SeedPackage, load_seed_package
from charms_core.types import ConfigurationError, SeedPackageError
from charms_launcher.config import LauncherConfig, envs_dir, packages_dir
from charms_launcher.registry import download_package

# Used when the launcher runs from an installed wheel (no repo checkout on
# disk to pip-install into seed envs).
RUNTIME_GIT_REQUIREMENTS = (
    "charms-core @ git+https://github.com/ArioAtlas/charms-launcher#subdirectory=core",
    "charms-launcher @ git+https://github.com/ArioAtlas/charms-launcher#subdirectory=launcher",
)

METADATA_FILENAME = "charms-package.json"

LogFn = Callable[[str], None]


@dataclass
class PulledSeed:
    seed_id: str
    root: Path  # extracted package dir containing manifest.json
    package: SeedPackage
    sha256: str

    @property
    def env_dir(self) -> Path:
        return envs_dir() / self.seed_id

    @property
    def python(self) -> Path:
        return venv_python(self.env_dir)

    def env_ready(self) -> bool:
        marker = self.env_dir / METADATA_FILENAME
        if not marker.is_file() or not self.python.exists():
            return False
        try:
            state = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(state.get("sha256") == self.sha256)


def venv_python(env_dir: Path) -> Path:
    if sys.platform == "win32":
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def runtime_sources() -> list[str]:
    """What to pip-install to provide the launcher runtime inside a seed env."""
    for parent in Path(__file__).resolve().parents:
        core = parent / "core" / "pyproject.toml"
        launcher = parent / "launcher" / "pyproject.toml"
        if core.is_file() and launcher.is_file():
            return [str(core.parent), str(launcher.parent)]
    return list(RUNTIME_GIT_REQUIREMENTS)


def _requirement_name(dep: str) -> str:
    """Canonical distribution name of a requirement string ('Torch>=2' → 'torch')."""
    name = re.split(r"[<>=!~;\[\s@]", dep.strip(), maxsplit=1)[0]
    return name.lower().replace("_", "-")


def filter_runtime_deps(dependencies: list[str]) -> list[str]:
    """Drop charms-core in any requirement spelling — the runtime provides it."""
    return [dep for dep in dependencies if _requirement_name(dep) != "charms-core"]


def dependency_install_steps(package: SeedPackage) -> list[list[str]]:
    """
    pip-install arg lists for the seed's dependencies, in order. Requirements
    pinned via ``install.index_packages`` get their own exclusive-index step
    FIRST (the only reliable way to pick CUDA torch wheels over newer PyPI
    releases); everything else installs in one step with the manifest's
    index/extra-index flags. Empty when nothing needs installing.
    """
    deps = filter_runtime_deps(package.pyproject.dependencies)
    if not deps:
        return []
    install = package.manifest.install
    index_packages = {
        key.lower().replace("_", "-"): url for key, url in install.index_packages.items()
    }

    pinned: dict[str, list[str]] = {}
    rest: list[str] = []
    for dep in deps:
        index = index_packages.get(_requirement_name(dep))
        if index is not None:
            pinned.setdefault(index, []).append(dep)
        else:
            rest.append(dep)

    steps = [["--index-url", index, *group] for index, group in pinned.items()]
    if rest:
        flags: list[str] = []
        if install.index_url:
            flags += ["--index-url", install.index_url]
        for url in install.extra_index_urls:
            flags += ["--extra-index-url", url]
        steps.append([*flags, *rest])
    return steps


def package_root(extract_dir: Path) -> Path | None:
    """The dir holding manifest.json: the extract root or its single subdir."""
    if (extract_dir / "manifest.json").is_file():
        return extract_dir
    subdirs = [d for d in extract_dir.iterdir() if d.is_dir()] if extract_dir.is_dir() else []
    if len(subdirs) == 1 and (subdirs[0] / "manifest.json").is_file():
        return subdirs[0]
    return None


def _pip(python: Path, *args: str) -> None:
    result = subprocess.run(
        [str(python), "-m", "pip", "--disable-pip-version-check", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-8:]
        raise ConfigurationError("pip failed:\n" + "\n".join(tail))


def extract_archive(archive: Path, seed_id: str) -> PulledSeed:
    """Validate + extract a downloaded package zip into packages/<seed_id>/."""
    data = archive.read_bytes()
    package = load_seed_package(data)  # full validation, incl. unsafe paths
    if package.manifest.id != seed_id:
        raise SeedPackageError(
            f"archive declares seed id '{package.manifest.id}', expected '{seed_id}'"
        )
    dest = packages_dir() / seed_id
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(dest)
    root = package_root(dest)
    if root is None:  # cannot happen after load_seed_package, but be safe
        raise SeedPackageError("extracted package has no manifest.json")
    pulled = PulledSeed(
        seed_id=seed_id, root=root, package=package, sha256=hashlib.sha256(data).hexdigest()
    )
    metadata = {"sha256": pulled.sha256, "root": root.name if root != dest else "."}
    metadata["package"] = json.loads(package.model_dump_json())
    (dest / METADATA_FILENAME).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return pulled


def load_pulled(seed_id: str) -> PulledSeed | None:
    """Rehydrate a previously pulled seed from packages/<seed_id>/."""
    dest = packages_dir() / seed_id
    meta_path = dest / METADATA_FILENAME
    if not meta_path.is_file():
        return None
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        package = SeedPackage.model_validate(metadata["package"])
        root = dest if metadata.get("root", ".") == "." else dest / str(metadata["root"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return None
    if package_root(dest) is None:
        return None
    return PulledSeed(
        seed_id=seed_id, root=root, package=package, sha256=str(metadata.get("sha256", ""))
    )


def list_pulled() -> list[PulledSeed]:
    base = packages_dir()
    if not base.is_dir():
        return []
    pulled = (load_pulled(entry.name) for entry in sorted(base.iterdir()) if entry.is_dir())
    return [seed for seed in pulled if seed is not None]


def ensure_env(pulled: PulledSeed, log: LogFn = print) -> Path:
    """Create (or reuse) the seed's virtualenv; idempotent per archive sha."""
    env_dir = pulled.env_dir
    if pulled.env_ready():
        return env_dir
    if env_dir.exists():
        shutil.rmtree(env_dir)
    log(f"creating environment for '{pulled.seed_id}' …")
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(env_dir)], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise ConfigurationError(f"could not create a virtualenv: {result.stderr.strip()}")
    python = venv_python(env_dir)
    log("installing the launcher runtime …")
    _pip(python, "install", "--quiet", *runtime_sources())
    for step in dependency_install_steps(pulled.package):
        log("installing seed dependencies: " + " ".join(step))
        _pip(python, "install", "--quiet", *step)
    log("installing the seed package …")
    _pip(python, "install", "--quiet", "--no-deps", str(pulled.root))
    (env_dir / METADATA_FILENAME).write_text(
        json.dumps({"seed_id": pulled.seed_id, "sha256": pulled.sha256}), encoding="utf-8"
    )
    log(f"environment ready: {env_dir}")
    return env_dir


def pull(config: LauncherConfig, seed_id: str, log: LogFn = print) -> PulledSeed:
    """Download, extract, and build the environment for a registry seed."""
    log(f"pulling '{seed_id}' from {config.http_url} …")
    downloads = packages_dir() / "_downloads"
    archive = download_package(config, seed_id, downloads)
    try:
        pulled = extract_archive(archive, seed_id)
    finally:
        archive.unlink(missing_ok=True)
    ensure_env(pulled, log)
    return pulled

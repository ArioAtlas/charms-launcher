"""
The seed package format.

A seed package is the distributable form of a Seed — the zip archive users
import into, and export from, the platform's seed registry (docker analogy:
the pushed image). At the archive root it must contain:

- ``manifest.json``  — a :class:`SeedPackageManifest`: the seed node's options
  schema (drives UI widgets), the price, and environment-variable rules.
- ``pyproject.toml`` — packaging metadata (name, description, authors,
  dependencies) and the ``charms.seeds`` entry point.
- ``README.md``      — documentation rendered in the catalog.
- the Python code (conventionally ``src/<package>/``) implementing the Seed.

``load_seed_package`` validates all of that without importing any seed code.
"""

import io
import json
import re
import zipfile
from collections.abc import Mapping
from enum import Enum

import tomllib
from pydantic import BaseModel, Field, ValidationError, model_validator

from charms_core.seed import SeedResources
from charms_core.types import SeedPackageError

MANIFEST_FILENAME = "manifest.json"
PYPROJECT_FILENAME = "pyproject.toml"
README_FILENAME = "README.md"
SEED_ENTRY_POINT_GROUP = "charms.seeds"

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class OptionType(str, Enum):
    """
    The value type of a seed-node option; determines how the option renders
    in the UI (boolean → switch, enum → dropdown, integer/float → number
    input with min/max, char → length-limited text input, …).
    """

    TEXT = "text"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    FLOAT = "float"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    CHAR = "char"  # length-limited string; requires `length`
    ENUM = "enum"  # predefined list of choices; requires `choices`


class SeedOption(BaseModel):
    """One entry of the seed node's options schema (mirrors a Config field)."""

    name: str = Field(description="snake_case option name, matching the Config model field")
    type: OptionType
    label: str = ""
    description: str = ""
    required: bool = False
    default: str | int | float | bool | None = None
    min: float | None = Field(default=None, description="integer/float only")
    max: float | None = Field(default=None, description="integer/float only")
    length: int | None = Field(default=None, ge=1, description="char only: maximum length")
    choices: list[str] | None = Field(default=None, description="enum only: allowed values")

    @model_validator(mode="after")
    def _check_constraints(self) -> "SeedOption":
        if not _ID_RE.match(self.name):
            raise ValueError(f"option name {self.name!r} must be snake_case")
        numeric = self.type in (OptionType.INTEGER, OptionType.FLOAT)
        if (self.min is not None or self.max is not None) and not numeric:
            raise ValueError(f"option {self.name!r}: min/max only apply to integer/float")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"option {self.name!r}: min must be <= max")
        if self.type is OptionType.CHAR and self.length is None:
            raise ValueError(f"option {self.name!r}: char options require `length`")
        if self.length is not None and self.type is not OptionType.CHAR:
            raise ValueError(f"option {self.name!r}: `length` only applies to char")
        if self.type is OptionType.ENUM and not self.choices:
            raise ValueError(f"option {self.name!r}: enum options require non-empty `choices`")
        if self.choices is not None and self.type is not OptionType.ENUM:
            raise ValueError(f"option {self.name!r}: `choices` only applies to enum")
        return self


class PriceUnit(str, Enum):
    """
    How the seed's price is denominated. Billing currently meters
    ``mana_per_second`` (compute time); other units are stored and shown in
    the catalog so seeds can declare them ahead of metering support.
    """

    MANA_PER_SECOND = "mana_per_second"
    MANA_PER_TOKEN = "mana_per_token"
    MANA_PER_REQUEST = "mana_per_request"


class SeedPrice(BaseModel):
    """What running this seed costs."""

    value: float = Field(default=1.0, ge=0)
    unit: PriceUnit = PriceUnit.MANA_PER_SECOND


class EnvVarSpec(BaseModel):
    """
    An environment variable the seed reads, with validation rules the
    launcher enforces before starting a Rune.
    """

    name: str = Field(description="UPPER_SNAKE_CASE variable name")
    description: str = ""
    required: bool = False
    secret: bool = False  # UI renders the value masked
    default: str | None = None
    pattern: str | None = Field(default=None, description="regex the value must fully match")

    @model_validator(mode="after")
    def _check(self) -> "EnvVarSpec":
        if not _ENV_NAME_RE.match(self.name):
            raise ValueError(f"environment variable name {self.name!r} must be UPPER_SNAKE_CASE")
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(
                    f"environment variable {self.name!r}: invalid pattern: {exc}"
                ) from exc
        return self


class SeedInstallSpec(BaseModel):
    """
    Package-index configuration for installing the seed's dependencies into
    its isolated environment. Needed by seeds whose wheels live off PyPI
    (e.g. CUDA torch builds: ``extra_index_urls:
    ["https://download.pytorch.org/whl/cu128"]`` — the ``+cu128`` local
    versions sort above the PyPI releases, so the GPU wheels win).
    """

    index_url: str | None = Field(default=None, description="replaces PyPI entirely")
    extra_index_urls: list[str] = Field(
        default_factory=list, description="searched in addition to PyPI"
    )

    @model_validator(mode="after")
    def _check(self) -> "SeedInstallSpec":
        for url in [self.index_url, *self.extra_index_urls]:
            if url is not None and not url.startswith(("http://", "https://")):
                raise ValueError(f"package index {url!r} must be an http(s) URL")
        return self


class SeedPackageManifest(BaseModel):
    """The ``manifest.json`` document at the root of every seed package."""

    schema_version: int = 1
    id: str = Field(description="snake_case, globally unique, e.g. 'echo', 'whisper'")
    name: str
    version: str = "0.1.0"
    description: str = ""
    supports_dispatch: bool = True
    supports_streaming: bool = False
    options: list[SeedOption] = Field(default_factory=list)
    price: SeedPrice = Field(default_factory=SeedPrice)
    environment: list[EnvVarSpec] = Field(default_factory=list)
    resources: SeedResources = Field(default_factory=SeedResources)
    install: SeedInstallSpec = Field(default_factory=SeedInstallSpec)

    @model_validator(mode="after")
    def _check(self) -> "SeedPackageManifest":
        if self.schema_version != 1:
            raise ValueError(f"unsupported manifest schema_version {self.schema_version}")
        if not _ID_RE.match(self.id):
            raise ValueError(f"seed id {self.id!r} must be snake_case")
        names = [option.name for option in self.options]
        if len(names) != len(set(names)):
            raise ValueError("option names must be unique")
        env_names = [spec.name for spec in self.environment]
        if len(env_names) != len(set(env_names)):
            raise ValueError("environment variable names must be unique")
        return self


class PyprojectInfo(BaseModel):
    """Packaging metadata extracted from the package's ``pyproject.toml``."""

    name: str
    version: str
    description: str = ""
    authors: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    entry_point: str | None = None  # "<module>:<Class>" for the charms.seeds entry


class SeedPackage(BaseModel):
    """A parsed, validated seed package (metadata only — never executes code)."""

    manifest: SeedPackageManifest
    pyproject: PyprojectInfo
    readme: str


def validate_environment(manifest: SeedPackageManifest, env: Mapping[str, str]) -> list[str]:
    """
    Check *env* against the manifest's environment rules; returns a list of
    human-readable problems (empty when everything passes).
    """
    problems: list[str] = []
    for spec in manifest.environment:
        value = env.get(spec.name, spec.default)
        if value is None or value == "":
            if spec.required:
                hint = f": {spec.description}" if spec.description else ""
                problems.append(f"{spec.name} is required{hint}")
            continue
        if spec.pattern is not None and re.fullmatch(spec.pattern, value) is None:
            problems.append(f"{spec.name} does not match pattern {spec.pattern!r}")
    return problems


def _parse_pyproject(text: str, manifest: SeedPackageManifest) -> PyprojectInfo:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SeedPackageError(f"pyproject.toml is not valid TOML: {exc}") from exc
    project = document.get("project")
    if not isinstance(project, dict):
        raise SeedPackageError("pyproject.toml has no [project] table")
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise SeedPackageError("pyproject.toml [project] must declare name and version")
    authors: list[str] = []
    for author in project.get("authors", []):
        if isinstance(author, dict):
            label = str(author.get("name", "")).strip()
            email = str(author.get("email", "")).strip()
            if label and email:
                authors.append(f"{label} <{email}>")
            elif label or email:
                authors.append(label or email)
    dependencies = [dep for dep in project.get("dependencies", []) if isinstance(dep, str)]
    entry_points = project.get("entry-points", {})
    seed_entries = (
        entry_points.get(SEED_ENTRY_POINT_GROUP, {}) if isinstance(entry_points, dict) else {}
    )
    entry_point = seed_entries.get(manifest.id) if isinstance(seed_entries, dict) else None
    if not isinstance(entry_point, str):
        raise SeedPackageError(
            f'pyproject.toml must declare [project.entry-points."{SEED_ENTRY_POINT_GROUP}"] '
            f'{manifest.id} = "<module>:<SeedClass>"'
        )
    return PyprojectInfo(
        name=name,
        version=version,
        description=str(project.get("description", "")),
        authors=authors,
        dependencies=dependencies,
        entry_point=entry_point,
    )


def _archive_root(names: list[str]) -> str:
    """
    Packages may be zipped either from inside the seed directory (files at the
    archive root) or as the directory itself (one shared top-level folder).
    Returns the prefix ("" or "<folder>/") under which the package files live.
    """
    if MANIFEST_FILENAME in names:
        return ""
    tops = {name.split("/", 1)[0] for name in names if name.strip("/")}
    if len(tops) == 1:
        prefix = f"{next(iter(tops))}/"
        if f"{prefix}{MANIFEST_FILENAME}" in names:
            return prefix
    raise SeedPackageError(f"{MANIFEST_FILENAME} is missing from the package root")


def _read_text(archive: zipfile.ZipFile, name: str) -> str:
    try:
        data = archive.read(name)
    except KeyError:
        raise SeedPackageError(f"{name} is missing from the package") from None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SeedPackageError(f"{name} is not valid UTF-8") from exc


def load_seed_package(data: bytes) -> SeedPackage:
    """
    Parse and validate a seed package archive. Raises :class:`SeedPackageError`
    with a user-facing message on any structural problem. Never imports or
    executes packaged code.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SeedPackageError("not a valid zip archive") from exc
    with archive:
        names = archive.namelist()
        for member in names:
            normalized = member.replace("\\", "/")
            if normalized.startswith("/") or ".." in normalized.split("/"):
                raise SeedPackageError(f"unsafe path in archive: {member!r}")
        root = _archive_root([name.replace("\\", "/") for name in names])

        raw_manifest = _read_text(archive, f"{root}{MANIFEST_FILENAME}")
        try:
            manifest = SeedPackageManifest.model_validate(json.loads(raw_manifest))
        except json.JSONDecodeError as exc:
            raise SeedPackageError(f"{MANIFEST_FILENAME} is not valid JSON: {exc}") from exc
        except ValidationError as exc:
            first = exc.errors()[0]
            location = ".".join(str(part) for part in first["loc"])
            detail = f" ({location})" if location else ""
            raise SeedPackageError(
                f"{MANIFEST_FILENAME} is invalid{detail}: {first['msg']}"
            ) from exc

        pyproject = _parse_pyproject(_read_text(archive, f"{root}{PYPROJECT_FILENAME}"), manifest)
        if pyproject.version != manifest.version:
            raise SeedPackageError(
                f"version mismatch: manifest.json says {manifest.version}, "
                f"pyproject.toml says {pyproject.version}"
            )
        readme = _read_text(archive, f"{root}{README_FILENAME}")
        has_python = any(
            name.replace("\\", "/").startswith(root) and name.endswith(".py") for name in names
        )
        if not has_python:
            raise SeedPackageError("the package contains no Python code (*.py)")
    return SeedPackage(manifest=manifest, pyproject=pyproject, readme=readme)

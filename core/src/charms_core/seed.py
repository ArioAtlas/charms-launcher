"""
The Seed SDK.

A Seed is the smallest unit of the ecosystem: a module able to download (if
needed) and load an AI model and expose it as a runnable interface. A Launcher
loads a Seed and serves it as a Rune. Analogy: Seed = docker image, Rune =
docker container.

Heavy libraries must be lazy-imported inside ``load()``/executor functions and
model instances cached on the Seed instance — ``load()`` runs once per Rune
lifetime. Blocking work inside ``run()`` goes through
``loop.run_in_executor``.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar, Generic, Literal, Protocol

from pydantic import BaseModel, Field

from charms_core.chunk import Chunk
from charms_core.node import ConfigT, InputT, OutputT
from charms_core.types import ConfigurationError, NodeMetadata, PortSchema


class SeedResources(BaseModel):
    """Resource expectations, shown to contributors before they run a Rune."""

    vram_mb: int = 0
    notes: str = ""


class SeedArtifact(BaseModel):
    """A model file (or repo) the seed needs downloaded before it can load."""

    name: str
    kind: Literal["hf_repo", "url"]
    ref: str  # HF repo id, or URL
    revision: str | None = None  # hf_repo pin
    sha256: str | None = None  # url integrity check


class SeedManifest(BaseModel):
    id: str = Field(description="snake_case, globally unique, e.g. 'echo', 'whisper'")
    name: str
    version: str = "0.1.0"
    description: str = ""
    supports_dispatch: bool = True
    supports_streaming: bool = False
    resources: SeedResources = Field(default_factory=SeedResources)
    artifacts: list[SeedArtifact] = Field(default_factory=list)


class ArtifactDownloader(Protocol):
    """Implemented by the launcher; resolves an artifact into the cache dir."""

    async def __call__(self, artifact: SeedArtifact, dest_dir: Path) -> Path: ...


class SeedContext:
    """Runtime context handed to ``Seed.load()`` by the launcher."""

    def __init__(self, cache_dir: Path, downloader: ArtifactDownloader | None = None) -> None:
        self.cache_dir = cache_dir
        self._downloader = downloader

    async def download(self, artifact: SeedArtifact) -> Path:
        """Download (or reuse a cached copy of) *artifact*; returns its local path."""
        if self._downloader is None:
            raise ConfigurationError("this SeedContext has no artifact downloader")
        return await self._downloader(artifact, self.cache_dir)


class Seed(ABC, Generic[InputT, OutputT, ConfigT]):
    """
    A runnable model module.

    Dispatch seeds implement ``run()``. Streaming seeds additionally set
    ``manifest.supports_streaming = True`` and implement the ``stream_*``
    trio; streams are stateful per ``stream_id`` and a Rune serves one task
    or stream at a time.
    """

    manifest: ClassVar[SeedManifest]
    inputs: ClassVar[list[PortSchema]]
    outputs: ClassVar[list[PortSchema]]
    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]
    config_model: ClassVar[type[BaseModel] | None] = None

    # Seeds priced per token/request (rather than per compute-second) set
    # this during run()/stream handling; the launcher reads it after each
    # task (resetting it before) and reports it for billing. A rune serves
    # one task at a time, so a plain attribute is race-free.
    billable_units: float | None = None

    @abstractmethod
    async def load(self, ctx: SeedContext) -> None:
        """Download artifacts (via ctx) and load the model. Runs once per Rune."""

    async def unload(self) -> None:  # noqa: B027 — optional hook, default no-op
        """Release model resources (free VRAM etc.). Default: nothing to do."""

    @abstractmethod
    async def run(self, input: InputT, config: ConfigT | None = None) -> OutputT:
        """Execute one dispatch task."""

    # ------------------------------------------------------------------ #
    #  Realtime (only when manifest.supports_streaming)                    #
    # ------------------------------------------------------------------ #

    async def stream_open(self, stream_id: str, config: ConfigT | None = None) -> None:
        raise NotImplementedError(f"{type(self).__name__} does not support streaming")

    async def stream_chunk(self, stream_id: str, chunk: Chunk) -> Chunk | None:
        """Process one chunk; may return 0..1 chunks now (buffering seeds return None)."""
        raise NotImplementedError(f"{type(self).__name__} does not support streaming")

    async def stream_close(self, stream_id: str) -> OutputT | None:
        """Flush the stream; optionally return a final aggregate output."""
        raise NotImplementedError(f"{type(self).__name__} does not support streaming")


class SeedDescriptor(BaseModel):
    """
    The wire/storage representation of a seed: manifest + ports + JSON schemas.
    Sent by launchers at registration and stored by the server; a rune node's
    ``NodeMetadata`` is derived from it.
    """

    manifest: SeedManifest
    inputs: list[PortSchema]
    outputs: list[PortSchema]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    config_schema: dict[str, Any] | None = None

    @classmethod
    def from_seed(cls, seed_cls: "type[Seed[Any, Any, Any]]") -> "SeedDescriptor":
        return cls(
            manifest=seed_cls.manifest,
            inputs=list(seed_cls.inputs),
            outputs=list(seed_cls.outputs),
            input_schema=seed_cls.input_model.model_json_schema(),
            output_schema=seed_cls.output_model.model_json_schema(),
            config_schema=(
                seed_cls.config_model.model_json_schema()
                if seed_cls.config_model is not None
                else None
            ),
        )

    def node_metadata(self) -> NodeMetadata:
        """The rune-node metadata this seed contributes to the recipe catalog."""
        return NodeMetadata(
            name=self.manifest.id,
            namespace="seed",
            version=self.manifest.version,
            description=self.manifest.description,
            inputs=self.inputs,
            outputs=self.outputs,
        )

"""
BasicNode — the server-executed recipe node.

Basic nodes are strictly lightweight glue (the heavy-compute principle):
no GPU, no model runtimes, no heavy imports, no blocking I/O. Anything
heavier is a Seed executed remotely by a Rune.
"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel

from charms_core.chunk import Chunk
from charms_core.types import (
    IncompatiblePortError,
    NodeMetadata,
    PortSchema,
    modalities_compatible,
)

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)
ConfigT = TypeVar("ConfigT", bound="BaseModel | None")


class BasicNode(ABC, Generic[InputT, OutputT, ConfigT]):
    """
    Atomic in-process processing unit.

    Generic parameters
    ------------------
    InputT  — Pydantic model for data inputs
    OutputT — Pydantic model for data outputs
    ConfigT — Pydantic model for configuration (use ``None`` if not needed)

    Conventions: ``run()`` never reads env vars or files; errors at the node
    boundary are ``CharmsError`` subclasses; ``Field(description=...)`` doubles
    as the UI tooltip and ``json_schema_extra`` carries UI hints.
    """

    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]
    config_model: ClassVar[type[BaseModel] | None] = None
    supports_streaming: ClassVar[bool] = False  # if True, implement transform_chunk()

    @classmethod
    @abstractmethod
    def metadata(cls) -> NodeMetadata:
        """Return static metadata describing this node's ports."""

    @abstractmethod
    async def run(self, input: InputT, config: ConfigT | None = None) -> OutputT:
        """Execute this node's logic and return an output."""

    async def transform_chunk(self, chunk: Chunk) -> Chunk | None:
        """
        Per-chunk transform for streaming-capable basic nodes on a realtime
        chunk path. Return None to swallow a chunk.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support streaming")

    # ------------------------------------------------------------------ #
    #  Port / compatibility helpers                                        #
    # ------------------------------------------------------------------ #

    @classmethod
    def input_ports(cls) -> list[PortSchema]:
        return cls.metadata().inputs

    @classmethod
    def output_ports(cls) -> list[PortSchema]:
        return cls.metadata().outputs

    @classmethod
    def accepts(cls, port: PortSchema) -> bool:
        """Return True if this node has an input port compatible with *port*."""
        return any(modalities_compatible(port.modality, inp.modality) for inp in cls.input_ports())

    @classmethod
    def assert_compatible(cls, upstream: "type[BasicNode[Any, Any, Any]]") -> None:
        """
        Raise IncompatiblePortError if no output port of *upstream* matches any
        input port of this node.
        """
        for out_port in upstream.output_ports():
            if cls.accepts(out_port):
                return
        raise IncompatiblePortError(
            f"No compatible connection: {upstream.metadata().node_id} → {cls.metadata().node_id}"
        )

"""Shared primitive types used across the Charms platform."""

from enum import Enum

from pydantic import BaseModel


class Modality(str, Enum):
    """Declares what kind of data a port carries."""

    TEXT = "text"
    EMBEDDING = "embedding"
    AUDIO = "audio"
    IMAGE = "image"
    VIDEO = "video"
    STRUCTURED = "structured"
    CONFIG = "config"  # port that carries configuration
    ANY = "any"


def modalities_compatible(source: Modality, target: Modality) -> bool:
    """An output port can feed an input port iff modalities match or either side is ANY."""
    return source == target or Modality.ANY in (source, target)


class PortSchema(BaseModel):
    """Describes a single input or output port on a node."""

    name: str
    modality: Modality
    description: str = ""
    optional: bool = False


class NodeMetadata(BaseModel):
    """
    Static metadata every recipe node exposes.

    Basic nodes declare this directly; rune nodes derive it from their
    SeedDescriptor (namespace="seed", name=<seed id>).
    """

    name: str  # unique within its namespace, snake_case
    namespace: str  # "basic" for basic nodes; "seed" for rune nodes
    version: str = "0.1.0"
    description: str = ""
    inputs: list[PortSchema]
    outputs: list[PortSchema]
    # True → ports are derived per-instance by the frontend (e.g. template slots)
    has_dynamic_inputs: bool = False
    has_dynamic_outputs: bool = False

    @property
    def node_id(self) -> str:
        """The identifier used in recipes, e.g. "basic.template" or "seed.echo"."""
        return f"{self.namespace}.{self.name}"


# ------------------------------------------------------------------ #
#  Exceptions                                                          #
# ------------------------------------------------------------------ #


class CharmsError(Exception):
    """Base exception for all Charms errors."""


class IncompatiblePortError(CharmsError):
    """Raised when two ports cannot be connected."""


class ConfigurationError(CharmsError):
    """Raised when a node or seed receives invalid or missing configuration."""


class RecipeValidationError(CharmsError):
    """Raised when a recipe document is structurally invalid (e.g. contains a cycle)."""


class NoRuneAvailableError(CharmsError):
    """Raised when a realtime session cannot pin a rune for a required seed."""


class TaskFailedError(CharmsError):
    """Raised when a rune task fails (seed error, timeout, or exhausted retries)."""


class InsufficientManaError(CharmsError):
    """Raised when the payer's mana balance cannot cover the requested work."""


class ProtocolError(CharmsError):
    """Raised on malformed wire messages or broken chunk framing."""


class SeedPackageError(CharmsError):
    """Raised when a seed package archive is malformed or fails validation."""


class NodeExecutionError(CharmsError):
    """Raised when a recipe node fails during execution."""

    def __init__(self, instance_id: str, node_id: str, message: str) -> None:
        super().__init__(message)
        self.instance_id = instance_id
        self.node_id = node_id

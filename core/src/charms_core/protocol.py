"""
Every WebSocket control message, as Pydantic models — no raw dict poking.

Control messages are JSON text frames shaped ``{"type": "<name>", ...}``.
Chunk messages are special: they are encoded/decoded by ``charms_core.chunk``
(flattened Chunk fields + ``binary`` flag, optionally followed by one binary
frame) under the type names ``CHUNK_TYPE_LAUNCHER`` / ``CHUNK_TYPE_CLIENT``;
they are therefore *not* part of the unions here — connection readers check
``is_chunk_message`` first.

Unknown message types are ignored for forward compatibility: the parse
functions return ``None`` for them and raise ``ProtocolError`` only on
malformed input.
"""

import json
from typing import Annotated, Any, Literal, TypeVar

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from charms_core.seed import SeedDescriptor
from charms_core.types import ProtocolError

CHUNK_TYPE_LAUNCHER = "stream.chunk"  # launcher socket, both directions
CHUNK_TYPE_CLIENT = "chunk"  # realtime client socket, both directions

# WS close code for a bad/revoked rune key at registration.
CLOSE_UNAUTHORIZED = 4401


class LauncherInfo(BaseModel):
    name: str
    host: str


class HardwareInfo(BaseModel):
    gpu: str | None = None
    vram_mb: int | None = None


# ------------------------------------------------------------------ #
#  Launcher → server                                                   #
# ------------------------------------------------------------------ #


class RegisterMsg(BaseModel):
    type: Literal["register"] = "register"
    rune_key: str
    launcher: LauncherInfo
    hardware: HardwareInfo = Field(default_factory=HardwareInfo)
    seed: SeedDescriptor


class HeartbeatMsg(BaseModel):
    type: Literal["heartbeat"] = "heartbeat"
    rune_id: str
    busy: bool = False


class TaskAcceptMsg(BaseModel):
    type: Literal["task.accept"] = "task.accept"
    task_id: str


class TaskResultMsg(BaseModel):
    type: Literal["task.result"] = "task.result"
    task_id: str
    output: dict[str, Any]
    compute_ms: float = 0.0
    # Seed-reported units (tokens, …) for seeds priced per unit rather than
    # per compute-second; None when the seed reports nothing.
    billable_units: float | None = None


class TaskErrorMsg(BaseModel):
    type: Literal["task.error"] = "task.error"
    task_id: str
    error: str
    compute_ms: float = 0.0
    billable_units: float | None = None  # partial usage still bills (§12.3)


class StreamOpenedMsg(BaseModel):
    type: Literal["stream.opened"] = "stream.opened"
    stream_id: str


class StreamClosedMsg(BaseModel):
    type: Literal["stream.closed"] = "stream.closed"
    stream_id: str
    final_output: dict[str, Any] | None = None
    compute_ms: float = 0.0
    billable_units: float | None = None  # cumulative, for unit-priced seeds


class StreamErrorMsg(BaseModel):
    type: Literal["stream.error"] = "stream.error"
    stream_id: str
    error: str


# ------------------------------------------------------------------ #
#  Server → launcher                                                   #
# ------------------------------------------------------------------ #


class RegisteredMsg(BaseModel):
    type: Literal["registered"] = "registered"
    rune_id: str


class HeartbeatAckMsg(BaseModel):
    type: Literal["heartbeat_ack"] = "heartbeat_ack"


class TaskAssignMsg(BaseModel):
    type: Literal["task.assign"] = "task.assign"
    task_id: str
    input: dict[str, Any]
    config: dict[str, Any] | None = None


class TaskCancelMsg(BaseModel):
    type: Literal["task.cancel"] = "task.cancel"
    task_id: str


class StreamOpenMsg(BaseModel):
    type: Literal["stream.open"] = "stream.open"
    stream_id: str
    config: dict[str, Any] | None = None


class StreamCloseMsg(BaseModel):
    type: Literal["stream.close"] = "stream.close"
    stream_id: str


# ------------------------------------------------------------------ #
#  Realtime client ↔ server                                            #
# ------------------------------------------------------------------ #


class StartMsg(BaseModel):
    type: Literal["start"] = "start"
    inputs: dict[str, Any] = Field(default_factory=dict)


class StopMsg(BaseModel):
    type: Literal["stop"] = "stop"


class StartedMsg(BaseModel):
    type: Literal["started"] = "started"
    session_id: str


class ResultMsg(BaseModel):
    type: Literal["result"] = "result"
    output: dict[str, Any]
    mana_cost: int = 0


class ClosedMsg(BaseModel):
    type: Literal["closed"] = "closed"
    mana_cost: int = 0


ErrorCode = Literal[
    "invalid_start", "no_rune_available", "insufficient_mana", "rune_lost", "internal"
]


class ErrorMsg(BaseModel):
    type: Literal["error"] = "error"
    code: ErrorCode
    message: str


# ------------------------------------------------------------------ #
#  Unions + parsing                                                    #
# ------------------------------------------------------------------ #

LauncherToServerMsg = (
    RegisterMsg
    | HeartbeatMsg
    | TaskAcceptMsg
    | TaskResultMsg
    | TaskErrorMsg
    | StreamOpenedMsg
    | StreamClosedMsg
    | StreamErrorMsg
)
ServerToLauncherMsg = (
    RegisteredMsg | HeartbeatAckMsg | TaskAssignMsg | TaskCancelMsg | StreamOpenMsg | StreamCloseMsg
)
ClientToServerMsg = StartMsg | StopMsg
ServerToClientMsg = StartedMsg | ResultMsg | ClosedMsg | ErrorMsg

_launcher_to_server: TypeAdapter[LauncherToServerMsg] = TypeAdapter(
    Annotated[LauncherToServerMsg, Field(discriminator="type")]
)
_server_to_launcher: TypeAdapter[ServerToLauncherMsg] = TypeAdapter(
    Annotated[ServerToLauncherMsg, Field(discriminator="type")]
)
_client_to_server: TypeAdapter[ClientToServerMsg] = TypeAdapter(
    Annotated[ClientToServerMsg, Field(discriminator="type")]
)
_server_to_client: TypeAdapter[ServerToClientMsg] = TypeAdapter(
    Annotated[ServerToClientMsg, Field(discriminator="type")]
)

T = TypeVar("T")


def parse_json_frame(raw: str | bytes) -> dict[str, Any]:
    """Parse a text frame into a message object; requires a string ``type`` field."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"frame is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        raise ProtocolError("message frame must be a JSON object with a string 'type'")
    return obj


def is_chunk_message(obj: dict[str, Any]) -> bool:
    return obj.get("type") in (CHUNK_TYPE_LAUNCHER, CHUNK_TYPE_CLIENT)


def _parse(adapter: TypeAdapter[T], obj: dict[str, Any]) -> T | None:
    try:
        return adapter.validate_python(obj)
    except ValidationError as exc:
        if any(e["type"] == "union_tag_invalid" for e in exc.errors()):
            return None  # unknown message type — caller logs + ignores
        raise ProtocolError(f"malformed '{obj.get('type')}' message: {exc}") from exc


def parse_launcher_to_server(obj: dict[str, Any]) -> LauncherToServerMsg | None:
    return _parse(_launcher_to_server, obj)


def parse_server_to_launcher(obj: dict[str, Any]) -> ServerToLauncherMsg | None:
    return _parse(_server_to_launcher, obj)


def parse_client_to_server(obj: dict[str, Any]) -> ClientToServerMsg | None:
    return _parse(_client_to_server, obj)


def parse_server_to_client(obj: dict[str, Any]) -> ServerToClientMsg | None:
    return _parse(_server_to_client, obj)


def encode_message(msg: BaseModel) -> str:
    return msg.model_dump_json()

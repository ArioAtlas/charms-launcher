"""
The streaming unit and its wire codec.

A ``Chunk`` travels over WebSockets as one JSON text frame; if it carries a
binary payload the JSON includes ``"binary": true`` and **exactly one** binary
frame with the raw bytes follows immediately on the same connection. Text
chunks embed their payload inline and send no binary frame. This module is the
only place that framing rule is implemented — both the launcher and client
sockets reuse it.

Audio payloads are PCM float32 little-endian mono unless ``meta`` says
otherwise; ``meta.sample_rate`` is mandatory for audio chunks.
"""

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator

from charms_core.types import Modality, ProtocolError


class Chunk(BaseModel):
    """One unit of a realtime stream."""

    stream_id: str
    seq: int = Field(ge=0, description="0-based sequence number within the stream")
    timestamp_ms: float = 0.0
    is_final: bool = False  # final marker; may carry a zero-length payload
    modality: Modality
    text: str | None = None  # TEXT chunks: payload travels inline in the JSON
    data: bytes | None = Field(default=None, exclude=True)  # binary payload, never in JSON
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_payload(self) -> "Chunk":
        if self.text is not None and self.data is not None:
            raise ValueError("a chunk carries either inline text or binary data, not both")
        if self.text is not None and self.modality is not Modality.TEXT:
            raise ValueError("inline text payload requires modality=text")
        if (
            self.modality is Modality.AUDIO
            and self.data is not None
            and "sample_rate" not in self.meta
        ):
            raise ValueError("audio chunks must declare meta.sample_rate")
        return self


def encode_chunk_message(message_type: str, chunk: Chunk) -> tuple[str, bytes | None]:
    """
    Encode a chunk as a wire message.

    Returns ``(json_text, binary_payload)``; when the payload is not None the
    sender MUST transmit it as the next binary frame on the same connection.
    """
    payload = chunk.model_dump(mode="json")
    payload["type"] = message_type
    payload["binary"] = chunk.data is not None
    return json.dumps(payload), chunk.data


def decode_chunk_message(obj: dict[str, Any], binary: bytes | None) -> Chunk:
    """
    Decode a parsed chunk-message JSON object plus its (optional) binary frame.

    Raises ProtocolError when framing is broken (a declared binary frame is
    missing, an undeclared one is supplied, or the fields don't validate).
    """
    expects_binary = bool(obj.get("binary", False))
    if expects_binary and binary is None:
        raise ProtocolError("chunk message declared a binary frame but none was supplied")
    if not expects_binary and binary is not None:
        raise ProtocolError("binary frame supplied for a chunk message that declared none")
    fields = {k: v for k, v in obj.items() if k not in ("type", "binary")}
    if binary is not None:
        fields["data"] = binary
    try:
        return Chunk.model_validate(fields)
    except ValidationError as exc:
        raise ProtocolError(f"invalid chunk message: {exc}") from exc

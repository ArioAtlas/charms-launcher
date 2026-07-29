"""Streaming echo seed — dependency-free realtime demo.

Echoes every TEXT chunk back uppercased, and returns the concatenation of
everything it saw at stream close (so both output modes can be demonstrated).
"""

from charms_core.chunk import Chunk
from charms_core.seed import Seed, SeedContext, SeedManifest
from charms_core.types import Modality, PortSchema
from charms_core.work import VarSource, VarTransform, WorkSpec, WorkVar
from pydantic import BaseModel, Field


class EchoStreamInput(BaseModel):
    text: str = Field(description="Text chunk to echo back")


class EchoStreamOutput(BaseModel):
    text: str


class EchoStreamSeed(Seed[EchoStreamInput, EchoStreamOutput, None]):
    manifest = SeedManifest(
        id="echo_stream",
        name="Echo Stream",
        version="0.2.0",
        description="Echoes text chunks back uppercased. Realtime demo seed.",
        supports_streaming=True,
        work=WorkSpec(
            unit="kchar",
            meter="pre",
            formula="chars / 1000",
            variables=[
                WorkVar(
                    name="chars",
                    source=VarSource.INPUT,
                    path="text",
                    transform=VarTransform.LENGTH,
                    default=0,
                    min=0,
                    max=1_000_000,
                )
            ],
            min_units=0.001,
        ),
        stream_work=WorkSpec(
            unit="stream_second",
            meter="post",
            formula="secs",
            estimate="1",
            variables=[WorkVar(name="secs", source=VarSource.STREAM, path="stream_seconds")],
        ),
    )
    inputs = [PortSchema(name="text", modality=Modality.TEXT, description="Text chunks")]
    outputs = [PortSchema(name="text", modality=Modality.TEXT, description="Echoed chunks")]
    input_model = EchoStreamInput
    output_model = EchoStreamOutput

    def __init__(self) -> None:
        self._buffers: dict[str, list[str]] = {}

    async def load(self, ctx: SeedContext) -> None:
        pass  # nothing to download or load

    async def run(self, input: EchoStreamInput, config: None = None) -> EchoStreamOutput:
        return EchoStreamOutput(text=input.text.upper())

    # ------------------------------------------------------------------ #
    #  Streaming (stateful per stream_id)                                  #
    # ------------------------------------------------------------------ #

    async def stream_open(self, stream_id: str, config: None = None) -> None:
        self._buffers[stream_id] = []

    async def stream_chunk(self, stream_id: str, chunk: Chunk) -> Chunk | None:
        if chunk.text is None:
            return None  # final markers / non-text payloads produce nothing
        upper = chunk.text.upper()
        self._buffers.setdefault(stream_id, []).append(upper)
        return chunk.model_copy(update={"text": upper})

    async def stream_close(self, stream_id: str) -> EchoStreamOutput:
        return EchoStreamOutput(text="".join(self._buffers.pop(stream_id, [])))

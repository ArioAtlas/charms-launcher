"""Echo seed — dependency-free dispatch demo proving the distributed path."""

from charms_core.seed import Seed, SeedContext, SeedManifest
from charms_core.types import Modality, PortSchema
from charms_core.work import VarSource, VarTransform, WorkSpec, WorkVar
from pydantic import BaseModel, Field


class EchoInput(BaseModel):
    text: str = Field(description="Text to echo back")


class EchoOutput(BaseModel):
    text: str


class EchoConfig(BaseModel):
    uppercase: bool = Field(default=False, description="Uppercase the echoed text")


class EchoSeed(Seed[EchoInput, EchoOutput, EchoConfig]):
    manifest = SeedManifest(
        id="echo",
        name="Echo",
        version="0.2.0",
        description="Echoes text back (optionally uppercased). Dispatch demo seed.",
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
    )
    inputs = [PortSchema(name="text", modality=Modality.TEXT, description="Text to echo")]
    outputs = [PortSchema(name="text", modality=Modality.TEXT, description="Echoed text")]
    input_model = EchoInput
    output_model = EchoOutput
    config_model = EchoConfig

    async def load(self, ctx: SeedContext) -> None:
        pass  # nothing to download or load

    async def run(self, input: EchoInput, config: EchoConfig | None = None) -> EchoOutput:
        text = input.text.upper() if config is not None and config.uppercase else input.text
        return EchoOutput(text=text)

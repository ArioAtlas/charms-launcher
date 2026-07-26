# charms-core

Shared contracts for the **Charms** platform — a community-powered AI
workflow system where users design **Charms** (node-graph **Recipes**) and
run them on a distributed fleet of **Runes**: instances of model-loading
**Seeds** contributed by users who share their compute and earn **Mana**.

This package is the dependency-light foundation both the Charms server and
the [charms-launcher](https://github.com/ArioAtlas/charms-launcher) build
on. Pure Pydantic v2 models plus stdlib — no web framework, no database, no
model runtimes.

## What's inside

| Module | Contents |
|---|---|
| `charms_core.types` | Modalities, port schemas, node metadata, the `CharmsError` hierarchy |
| `charms_core.node` | `BasicNode` — the in-process glue-node SDK |
| `charms_core.seed` | `Seed` — the model-module SDK (`load()` / `run()` / streaming trio), manifests, descriptors |
| `charms_core.package` | The seed package format: `manifest.json` models (typed options, prices, env rules, install indexes) + zip validation |
| `charms_core.recipe` | Recipe graph models, validation, topological sort |
| `charms_core.chunk` | The streaming chunk model + binary framing codec |
| `charms_core.protocol` | Every WebSocket message on the launcher and realtime-client wires |

## Install

```bash
pip install charms-core
```

Python ≥ 3.12. Fully typed (`py.typed`).

## Building a Seed

```python
from pydantic import BaseModel
from charms_core.seed import Seed, SeedContext, SeedManifest
from charms_core.types import Modality, PortSchema

class EchoInput(BaseModel):
    text: str

class EchoOutput(BaseModel):
    text: str

class EchoSeed(Seed[EchoInput, EchoOutput, None]):
    manifest = SeedManifest(id="echo", name="Echo")
    inputs = [PortSchema(name="text", modality=Modality.TEXT)]
    outputs = [PortSchema(name="text", modality=Modality.TEXT)]
    input_model = EchoInput
    output_model = EchoOutput

    async def load(self, ctx: SeedContext) -> None: ...

    async def run(self, input: EchoInput, config: None = None) -> EchoOutput:
        return EchoOutput(text=input.text)
```

Package it (with a `manifest.json`, `pyproject.toml`, and `README.md`) and
import it into a Charms server's seed registry; contributors then pull and
run it as a Rune via the launcher.

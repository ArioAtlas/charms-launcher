"""
Recipe — the definition of a Charm.

A recipe is a directed graph of node instances plus input/output bindings.
This module owns the document model, `validate_recipe` (the seven rules of
charms.md §7.3), and Kahn topological sorting with cycle detection.
"""

from collections import deque
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

from charms_core.types import (
    Modality,
    PortSchema,
    RecipeValidationError,
    modalities_compatible,
)

SCHEMA_VERSION = 1


class Position(BaseModel):
    """Editor-only canvas position; the engine ignores it."""

    x: float
    y: float


class FieldValue(BaseModel):
    """
    Value source for one input port: "scalar" (literal carried in the recipe),
    "connected" (arrives via an edge), or "bound" (arrives from a charm-level
    input binding at run time).
    """

    kind: Literal["scalar", "connected", "bound"]
    value: Any = None


class RecipeNode(BaseModel):
    instance_id: str
    node_id: str  # "<namespace>.<name>", e.g. "basic.template", "seed.echo"
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    # Seed configuration for rune nodes, validated against the seed's
    # config_model on the launcher (task.assign / stream.open `config`).
    # Ignored for basic nodes.
    config: dict[str, Any] | None = None
    position: Position | None = None


class RecipeEdge(BaseModel):
    source_instance: str
    source_port: str
    target_instance: str
    target_port: str


class Binding(BaseModel):
    """Exposes one node port as a charm-level input or output."""

    name: str
    instance_id: str
    port: str
    modality: Modality
    description: str = ""


class Bindings(BaseModel):
    inputs: list[Binding] = Field(default_factory=list)
    outputs: list[Binding] = Field(default_factory=list)


class Recipe(BaseModel):
    schema_version: int = SCHEMA_VERSION
    name: str
    description: str = ""
    mode: Literal["dispatch", "realtime"]
    output_mode: Literal["stream", "aggregate"] = "stream"  # realtime only
    nodes: list[RecipeNode] = Field(default_factory=list)
    edges: list[RecipeEdge] = Field(default_factory=list)
    bindings: Bindings = Field(default_factory=Bindings)


class CatalogEntry(BaseModel):
    """What validation needs to know about one node_id (basic node or seed)."""

    node_id: str
    inputs: list[PortSchema]
    outputs: list[PortSchema]
    supports_streaming: bool = False
    # Dynamic ports (e.g. basic.template slots) are derived per-instance by the
    # frontend; validation accepts any port name on that side and skips
    # modality checks for ports it cannot see.
    has_dynamic_inputs: bool = False
    has_dynamic_outputs: bool = False


NodeCatalog = Mapping[str, CatalogEntry]


class ValidationProblem(BaseModel):
    code: str
    message: str
    instance_id: str | None = None
    port: str | None = None


# ------------------------------------------------------------------ #
#  Topological sort                                                    #
# ------------------------------------------------------------------ #


def topo_sort(recipe: Recipe) -> list[str]:
    """
    Kahn's algorithm over instance ids. Edges referencing unknown instances are
    ignored here (validation reports them). Raises RecipeValidationError on a
    cycle. Deterministic: ready nodes are processed in sorted order.
    """
    ids = [n.instance_id for n in recipe.nodes]
    indegree = dict.fromkeys(ids, 0)
    downstream: dict[str, list[str]] = {i: [] for i in ids}
    for edge in recipe.edges:
        if edge.source_instance in indegree and edge.target_instance in indegree:
            downstream[edge.source_instance].append(edge.target_instance)
            indegree[edge.target_instance] += 1

    ready = deque(sorted(i for i in ids if indegree[i] == 0))
    order: list[str] = []
    while ready:
        current = ready.popleft()
        order.append(current)
        for nxt in sorted(downstream[current]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)

    if len(order) != len(ids):
        raise RecipeValidationError("recipe contains a cycle")
    return order


def chunk_path(recipe: Recipe, catalog: NodeCatalog) -> list[str]:
    """
    Resolve the linear realtime chunk path (§7.3 rule 7): instance ids from
    the stream-source input binding to the output-binding node. The runtime
    source of truth for the realtime engine; raises RecipeValidationError on
    any violation (validation reports the same rules as coded problems).
    """
    by_instance = {n.instance_id: n for n in recipe.nodes}

    def streams(instance_id: str) -> bool:
        node = by_instance.get(instance_id)
        entry = catalog.get(node.node_id) if node is not None else None
        return entry is not None and entry.supports_streaming

    sources = [b for b in recipe.bindings.inputs if streams(b.instance_id)]
    if len(sources) != 1:
        raise RecipeValidationError(
            f"realtime recipes need exactly one input binding on a streaming-capable "
            f"node (found {len(sources)})"
        )
    output_instances = {b.instance_id for b in recipe.bindings.outputs}

    path: list[str] = []
    current = sources[0].instance_id
    visited: set[str] = set()
    while True:
        if current in visited:
            raise RecipeValidationError("chunk path contains a cycle")
        visited.add(current)
        path.append(current)
        successors = sorted(
            {
                e.target_instance
                for e in recipe.edges
                if e.source_instance == current and streams(e.target_instance)
            }
        )
        if len(successors) > 1:
            raise RecipeValidationError("the chunk path must be linear")
        if not successors:
            if current not in output_instances:
                raise RecipeValidationError("the chunk path must end at an output binding")
            return path
        current = successors[0]


# ------------------------------------------------------------------ #
#  Validation                                                          #
# ------------------------------------------------------------------ #


def validate_recipe(recipe: Recipe, catalog: NodeCatalog) -> list[ValidationProblem]:
    """Apply all §7.3 rules; an empty result means the recipe is valid."""
    problems: list[ValidationProblem] = []

    def problem(
        code: str, message: str, instance_id: str | None = None, port: str | None = None
    ) -> None:
        problems.append(
            ValidationProblem(code=code, message=message, instance_id=instance_id, port=port)
        )

    # Rule 1 — schema version and known node ids.
    if recipe.schema_version != SCHEMA_VERSION:
        problem("bad_schema_version", f"unsupported schema_version {recipe.schema_version}")
    for node in recipe.nodes:
        if node.node_id not in catalog:
            problem("unknown_node", f"unknown node_id '{node.node_id}'", node.instance_id)

    # Rule 2 — unique instance ids; edges reference existing instances/ports.
    by_instance: dict[str, RecipeNode] = {}
    for node in recipe.nodes:
        if node.instance_id in by_instance:
            problem("duplicate_instance", f"duplicate instance_id '{node.instance_id}'")
        by_instance[node.instance_id] = node

    def entry_of(instance_id: str) -> CatalogEntry | None:
        node = by_instance.get(instance_id)
        return catalog.get(node.node_id) if node is not None else None

    def has_port(instance_id: str, port: str, direction: str) -> bool:
        entry = entry_of(instance_id)
        if entry is None:
            return True  # unknown node/instance already reported; avoid cascades
        if entry.has_dynamic_outputs if direction == "out" else entry.has_dynamic_inputs:
            return True  # dynamic ports are derived per-instance; accept any name
        ports = entry.outputs if direction == "out" else entry.inputs
        return any(p.name == port for p in ports)

    for edge in recipe.edges:
        for instance_id in (edge.source_instance, edge.target_instance):
            if instance_id not in by_instance:
                problem("unknown_instance", f"edge references unknown instance '{instance_id}'")
        if edge.source_instance in by_instance and not has_port(
            edge.source_instance, edge.source_port, "out"
        ):
            problem(
                "unknown_port",
                f"no output port '{edge.source_port}'",
                edge.source_instance,
                edge.source_port,
            )
        if edge.target_instance in by_instance and not has_port(
            edge.target_instance, edge.target_port, "in"
        ):
            problem(
                "unknown_port",
                f"no input port '{edge.target_port}'",
                edge.target_instance,
                edge.target_port,
            )

    # Rule 3 — modality compatibility; at most one edge per input port.
    def port_schema(instance_id: str, port: str, direction: str) -> PortSchema | None:
        entry = entry_of(instance_id)
        if entry is None:
            return None
        ports = entry.outputs if direction == "out" else entry.inputs
        return next((p for p in ports if p.name == port), None)

    seen_targets: set[tuple[str, str]] = set()
    for edge in recipe.edges:
        target_key = (edge.target_instance, edge.target_port)
        if target_key in seen_targets:
            problem(
                "multiple_edges",
                "input port has more than one incoming edge",
                edge.target_instance,
                edge.target_port,
            )
        seen_targets.add(target_key)
        src = port_schema(edge.source_instance, edge.source_port, "out")
        dst = port_schema(edge.target_instance, edge.target_port, "in")
        if (
            src is not None
            and dst is not None
            and not modalities_compatible(src.modality, dst.modality)
        ):
            problem(
                "incompatible_modality",
                f"{src.modality.value} output cannot feed {dst.modality.value} input",
                edge.target_instance,
                edge.target_port,
            )

    # Rule 4 — no cycles.
    try:
        topo_sort(recipe)
    except RecipeValidationError:
        problem("cycle", "recipe contains a cycle")

    # Rule 5 — every required input port is satisfied.
    bound_ports = {(b.instance_id, b.port) for b in recipe.bindings.inputs}
    for node in recipe.nodes:
        entry = catalog.get(node.node_id)
        if entry is None:
            continue
        for port in entry.inputs:
            if port.optional:
                continue
            key = (node.instance_id, port.name)
            field = node.fields.get(port.name)
            satisfied = (
                key in seen_targets
                or key in bound_ports
                or (field is not None and field.kind == "scalar" and field.value is not None)
            )
            if not satisfied:
                problem(
                    "unsatisfied_input",
                    f"required input '{port.name}' has no edge, scalar, or binding",
                    node.instance_id,
                    port.name,
                )

    # Rule 6 — bindings reference existing ports; unique names; ≥1 output binding.
    for direction, bindings in (("in", recipe.bindings.inputs), ("out", recipe.bindings.outputs)):
        names: set[str] = set()
        for binding in bindings:
            if binding.name in names:
                problem("duplicate_binding", f"duplicate binding name '{binding.name}'")
            names.add(binding.name)
            if binding.instance_id not in by_instance:
                problem(
                    "bad_binding",
                    f"binding '{binding.name}' references unknown instance",
                    binding.instance_id,
                )
            elif not has_port(binding.instance_id, binding.port, direction):
                problem(
                    "bad_binding",
                    f"binding '{binding.name}' references a nonexistent port",
                    binding.instance_id,
                    binding.port,
                )
    if not recipe.bindings.outputs:
        problem("no_output_binding", "a charm needs at least one output binding")

    # Rule 7 — realtime: linear, fully streaming-capable chunk path.
    if recipe.mode == "realtime" and not problems:
        _validate_realtime_path(recipe, catalog, by_instance, problem)

    return problems


def _validate_realtime_path(
    recipe: Recipe,
    catalog: NodeCatalog,
    by_instance: dict[str, RecipeNode],
    problem: Any,
) -> None:
    def streams(instance_id: str) -> bool:
        entry = catalog.get(by_instance[instance_id].node_id)
        return entry is not None and entry.supports_streaming

    sources = [b for b in recipe.bindings.inputs if streams(b.instance_id)]
    if len(sources) != 1:
        problem(
            "realtime_stream_source",
            "realtime recipes need exactly one input binding on a streaming-capable node "
            f"(found {len(sources)})",
        )
        return

    output_instances = {b.instance_id for b in recipe.bindings.outputs}
    current = sources[0].instance_id
    visited: set[str] = set()
    while True:
        if current in visited:
            return  # cycle — already reported by rule 4
        visited.add(current)
        if not streams(current):
            problem(
                "realtime_not_streamable",
                f"node '{by_instance[current].node_id}' on the chunk path cannot stream",
                current,
            )
            return
        successors = sorted(
            {e.target_instance for e in recipe.edges if e.source_instance == current}
        )
        streaming_successors = [s for s in successors if s in by_instance and streams(s)]
        if len(streaming_successors) > 1:
            problem(
                "realtime_branching",
                "the chunk path must be linear (one streaming successor per node)",
                current,
            )
            return
        if not streaming_successors:
            if current not in output_instances:
                problem(
                    "realtime_path_broken",
                    "the chunk path must end at an output binding",
                    current,
                )
            return
        current = streaming_successors[0]

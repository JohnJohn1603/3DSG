"""Backend-neutral contracts for incremental scene-graph providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class GraphProviderCapabilities:
    """Declared provider support; absence means unsupported, never inferred."""

    provider_id: str
    input_modalities: tuple[str, ...] = ()
    entity_types: tuple[str, ...] = ()
    relation_types: tuple[str, ...] = ()
    supports_incremental_updates: bool = True
    supports_auxiliary_depth: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not self.provider_id.strip():
            raise ValueError("graph provider ID must be non-empty")
        for name, values in (("input_modalities", self.input_modalities),
                             ("entity_types", self.entity_types),
                             ("relation_types", self.relation_types)):
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"{name} must contain non-empty strings")
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must not contain duplicates")
        if type(self.supports_incremental_updates) is not bool:
            raise ValueError("supports_incremental_updates must be boolean")
        if type(self.supports_auxiliary_depth) is not bool:
            raise ValueError("supports_auxiliary_depth must be boolean")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("provider capability metadata must be a mapping")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "3dsg-graph-provider-capabilities/v1",
            "provider_id": self.provider_id,
            "input_modalities": list(self.input_modalities),
            "entity_types": list(self.entity_types),
            "relation_types": list(self.relation_types),
            "supports_incremental_updates": self.supports_incremental_updates,
            "supports_auxiliary_depth": self.supports_auxiliary_depth,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class SceneGraphSnapshot:
    """One provider's complete, frame-aligned graph view at a revision."""

    provider_id: str
    run_id: str
    scene_id: str
    frame_id: str
    frame_index: int
    revision: int
    nodes: tuple[Mapping[str, Any], ...]
    edges: tuple[Mapping[str, Any], ...]
    status: str = "provisional"
    provenance: Mapping[str, Any] = field(default_factory=dict)
    schema: str = "3dsg-scene-graph-snapshot/v1"

    def __post_init__(self) -> None:
        if self.schema != "3dsg-scene-graph-snapshot/v1":
            raise ValueError(f"unsupported graph snapshot schema: {self.schema}")
        for name in ("provider_id", "run_id", "scene_id", "frame_id", "status"):
            value = getattr(self, name)
            if value is None or not str(value).strip():
                raise ValueError(f"graph snapshot {name} must be non-empty")
        if type(self.frame_index) is not int or self.frame_index < 0:
            raise ValueError("graph snapshot frame_index must be a non-negative integer")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("graph snapshot revision must be a non-negative integer")
        if not isinstance(self.nodes, (tuple, list)) or not isinstance(self.edges, (tuple, list)):
            raise ValueError("graph snapshot nodes and edges must be sequences")
        if not isinstance(self.provenance, Mapping):
            raise ValueError("graph snapshot provenance must be a mapping")
        ids: set[str] = set()
        for node in self.nodes:
            if not isinstance(node, Mapping):
                raise ValueError("graph snapshot nodes must be mappings")
            node_value = node.get("id", node.get("node_id", ""))
            node_id = "" if node_value is None else str(node_value).strip()
            if not node_id or node_id in ids:
                raise ValueError("graph snapshot node IDs must be present and unique")
            ids.add(node_id)
        for edge in self.edges:
            if not isinstance(edge, Mapping):
                raise ValueError("graph snapshot edges must be mappings")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "provider_id": self.provider_id,
            "run_id": self.run_id,
            "scene_id": self.scene_id,
            "as_of_frame_id": self.frame_id,
            "as_of_frame_index": self.frame_index,
            "revision": self.revision,
            "status": self.status,
            "nodes": [dict(node) for node in self.nodes],
            "edges": [dict(edge) for edge in self.edges],
            "provenance": dict(self.provenance),
        }


@runtime_checkable
class SceneGraphProvider(Protocol):
    """Incremental provider independent of any mapper or sensor framework."""

    provider_id: str
    capabilities: GraphProviderCapabilities

    def update(self, observation: Any) -> SceneGraphSnapshot: ...
    def close(self) -> None: ...


def validate_snapshot_for_observation(snapshot: SceneGraphSnapshot,
                                      observation: Any,
                                      expected_provider_id: str | None = None) -> None:
    if not isinstance(snapshot, SceneGraphSnapshot):
        raise TypeError("scene graph provider must return SceneGraphSnapshot")
    keyframe = observation.keyframe
    expected = (keyframe.run_id, keyframe.scene_id, keyframe.frame_id, keyframe.frame_index)
    actual = (snapshot.run_id, snapshot.scene_id, snapshot.frame_id, snapshot.frame_index)
    if actual != expected:
        raise ValueError(f"graph snapshot identity {actual} differs from observation {expected}")
    if expected_provider_id is not None and snapshot.provider_id != expected_provider_id:
        raise ValueError("graph snapshot provider identity mismatch")


def snapshot_from_mapping(value: Mapping[str, Any]) -> SceneGraphSnapshot:
    """Parse the stable mapping form used by plugin boundaries."""
    nodes = value.get("nodes", ())
    edges = value.get("edges", ())
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)):
        raise ValueError("graph snapshot nodes must be a sequence")
    if not isinstance(edges, Sequence) or isinstance(edges, (str, bytes)):
        raise ValueError("graph snapshot edges must be a sequence")
    return SceneGraphSnapshot(
        schema=str(value.get("schema", "3dsg-scene-graph-snapshot/v1")),
        provider_id=str(value.get("provider_id", "")),
        run_id=str(value.get("run_id", "")),
        scene_id=str(value.get("scene_id", "")),
        frame_id=str(value.get("as_of_frame_id", value.get("frame_id", ""))),
        frame_index=int(value.get("as_of_frame_index", value.get("frame_index", -1))),
        revision=int(value.get("revision", -1)),
        nodes=tuple(nodes), edges=tuple(edges),
        status=str(value.get("status", "provisional")),
        provenance=dict(value.get("provenance", {})),
    )

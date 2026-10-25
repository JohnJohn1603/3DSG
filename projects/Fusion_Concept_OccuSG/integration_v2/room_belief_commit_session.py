"""Stateful RB1 updates bound to immutable unified graph commits.

This adapter reuses the T3 consensus helpers and RoomBeliefLedger. It does not
run perception, mapping, membership geometry, or Qwen. Membership must arrive
from a prefix-scoped producer with an explicit scope assessment.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from standalone_sg.semantics.room_belief_v1 import (
    ConsensusPolicy,
    RoomBeliefEvent,
    RoomBeliefLedger,
    RoomBeliefPolicy,
    RoomObjectEvidence,
    SemanticObservation,
    aggregate_object_semantics,
)

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = ("mean_no_neutral", "mean_neutral", "sum_no_neutral", "sum_neutral")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _atomic_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False,
                      allow_nan=False).encode("utf-8") + b"\n"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    return _sha_file(path)


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _require_revision(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class GraphCommitEnvelope:
    """Current unified commit plus only the semantic evidence available now."""

    event_id: str
    run_id: str
    scene_id: str
    sequence_id: str
    frame_index: int
    frame_id: str
    timestamp_s: float
    unified_revision: int
    graph_revision_id: str
    graph_path: str
    graph_sha256: str
    object_revision: int
    object_state_sha256: str
    room_revision: int
    room_state_sha256: str
    source_hashes: Mapping[str, str]
    current_observations: Sequence[Mapping[str, Any]]
    objects: Sequence[Mapping[str, Any]]
    rooms: Sequence[Mapping[str, Any]]
    scope: Mapping[str, Any]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GraphCommitEnvelope":
        required = {
            "event_id", "run_id", "scene_id", "sequence_id", "frame_index", "frame_id",
            "timestamp_s", "unified_revision", "graph_revision_id", "graph_path", "graph_sha256",
            "object_revision", "object_state_sha256", "room_revision", "room_state_sha256",
            "source_hashes", "current_observations", "objects", "rooms", "scope",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            missing = sorted(required - set(value)) if isinstance(value, Mapping) else sorted(required)
            extra = sorted(set(value) - required) if isinstance(value, Mapping) else []
            raise ValueError(f"invalid GraphCommitEnvelope fields; missing={missing}, extra={extra}")
        for key in ("graph_sha256", "object_state_sha256", "room_state_sha256"):
            if not isinstance(value[key], str) or not _SHA_RE.fullmatch(value[key]):
                raise ValueError(f"{key} must be a lowercase SHA-256 digest")
        hashes = value["source_hashes"]
        if not isinstance(hashes, Mapping) or not hashes:
            raise ValueError("source_hashes must be a non-empty mapping")
        for key, digest in hashes.items():
            if not isinstance(key, str) or not key or not isinstance(digest, str) or not _SHA_RE.fullmatch(digest):
                raise ValueError("source_hashes contains an invalid key/digest")
        timestamp = value["timestamp_s"]
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(float(timestamp)):
            raise ValueError("timestamp_s must be finite")
        if not isinstance(value["current_observations"], (list, tuple)):
            raise ValueError("current_observations must be a sequence")
        if not isinstance(value["objects"], (list, tuple)) or not isinstance(value["rooms"], (list, tuple)):
            raise ValueError("objects and rooms must be sequences")
        if any(not isinstance(row, Mapping) for row in (*value["current_observations"], *value["objects"], *value["rooms"])):
            raise ValueError("current observations, objects, and rooms must contain objects")
        if not isinstance(value["scope"], Mapping):
            raise ValueError("scope must be an object")
        return cls(
            event_id=_require_text(value["event_id"], "event_id"),
            run_id=_require_text(value["run_id"], "run_id"),
            scene_id=_require_text(value["scene_id"], "scene_id"),
            sequence_id=_require_text(value["sequence_id"], "sequence_id"),
            frame_index=_require_revision(value["frame_index"], "frame_index"),
            frame_id=_require_text(value["frame_id"], "frame_id"),
            timestamp_s=float(timestamp),
            unified_revision=_require_revision(value["unified_revision"], "unified_revision"),
            graph_revision_id=_require_text(value["graph_revision_id"], "graph_revision_id"),
            graph_path=_require_text(value["graph_path"], "graph_path"),
            graph_sha256=value["graph_sha256"],
            object_revision=_require_revision(value["object_revision"], "object_revision"),
            object_state_sha256=value["object_state_sha256"],
            room_revision=_require_revision(value["room_revision"], "room_revision"),
            room_state_sha256=value["room_state_sha256"],
            source_hashes=dict(value["source_hashes"]),
            current_observations=tuple(dict(row) for row in value["current_observations"]),
            objects=tuple(dict(row) for row in value["objects"]),
            rooms=tuple(dict(row) for row in value["rooms"]),
            scope=dict(value["scope"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "run_id": self.run_id, "scene_id": self.scene_id,
            "sequence_id": self.sequence_id, "frame_index": self.frame_index, "frame_id": self.frame_id,
            "timestamp_s": self.timestamp_s, "unified_revision": self.unified_revision,
            "graph_revision_id": self.graph_revision_id, "graph_path": self.graph_path,
            "graph_sha256": self.graph_sha256, "object_revision": self.object_revision,
            "object_state_sha256": self.object_state_sha256, "room_revision": self.room_revision,
            "room_state_sha256": self.room_state_sha256, "source_hashes": dict(self.source_hashes),
            "current_observations": [dict(x) for x in self.current_observations],
            "objects": [dict(x) for x in self.objects], "rooms": [dict(x) for x in self.rooms],
            "scope": dict(self.scope),
        }


class RoomBeliefCommitSession:
    """Prefix-only session around T3 consensus helpers and the RB1 ledger."""

    def __init__(self, *, config: Mapping[str, Any], tables: Mapping[str, Any],
                 table_digests: Mapping[str, str], categories: Sequence[str], semantic_rooms: Sequence[str],
                 aliases: Mapping[str, str], consensus_policy: ConsensusPolicy,
                 label_policy: RoomBeliefPolicy, admission_min_detections: int,
                 config_provenance: Mapping[str, Any], overlay_root: Path):
        if type(admission_min_detections) is not int or admission_min_detections < 1:
            raise ValueError("admission_min_detections must be a positive effective value")
        self.config = dict(config)
        self.tables = {variant: {cat: dict(row) for cat, row in tables[variant].items()} for variant in VARIANTS}
        self.table_digests = dict(table_digests)
        self.categories = tuple(categories)
        self.semantic_rooms = tuple(semantic_rooms)
        self.aliases = dict(aliases)
        from scripts.run_causal_hybrid_room_belief import canonical
        self._canonical = canonical
        self.consensus_policy = consensus_policy
        self.label_policy = label_policy
        self.admission_min_detections = admission_min_detections
        self.config_provenance = dict(config_provenance)
        self.overlay_root = Path(overlay_root).resolve()
        self.run_id: str | None = None
        self.scene_id: str | None = None
        self.sequence_id: str | None = None
        self._status = "open"
        self._ledgers = self._new_ledgers()
        self._observation_catalog: dict[str, dict[str, Any]] = {}
        self._object_ids: set[str] = set()
        self._object_memberships: dict[str, tuple[str, str | None]] = {}
        self._active_rooms: set[str] = set()
        self._seen_events: dict[str, tuple[str, dict[str, Any]]] = {}
        self._last_unified_revision = -1
        self._last_object_revision = -1
        self._last_room_revision = -1
        self._last_frame_index = -1
        self._last_timestamp_s = -math.inf
        self._seen_frame_ids: set[str] = set()
        self._terminal_result: dict[str, Any] | None = None

    @classmethod
    def open(cls, *, config_path: Path, overlay_root: Path, admission_min_detections: int,
             scope_policy: Mapping[str, Any] | None = None) -> "RoomBeliefCommitSession":
        """Resolve every pinned input once. No model or mapper is loaded."""
        config_path = Path(config_path).resolve()
        config = json.loads(config_path.read_text(encoding="utf-8"))
        required = {"schema", "a_tables_archive", "consensus_policy", "room_belief_policy",
                    "detector_profile", "ontology", "scope_policy"}
        if set(config) != required or config.get("schema") != "t4-live-room-belief-config/v1":
            raise ValueError("unsupported T4 room-belief config schema/fields")
        effective_scope_policy = dict(scope_policy or config["scope_policy"])
        if effective_scope_policy != config["scope_policy"]:
            raise ValueError("runtime scope policy must match the hashed T4 config")
        if effective_scope_policy.get("mode") not in {"verified_only", "assumed_single_level_demo"}:
            raise ValueError("unsupported scope policy mode")
        if set(effective_scope_policy) != {"mode", "assumed_declaration"}:
            raise ValueError("scope_policy must explicitly contain mode and assumed_declaration")
        if effective_scope_policy["mode"] == "verified_only" and effective_scope_policy["assumed_declaration"] is not None:
            raise ValueError("verified-only config may not carry an assumed declaration")
        def resolve_file(spec: Mapping[str, Any], label: str) -> tuple[Path, str]:
            if set(spec) != {"path", "sha256"}:
                raise ValueError(f"{label} pin must contain path and sha256")
            path = Path(spec["path"])
            if not path.is_absolute():
                path = (ROOT / path).resolve()
            if not path.is_file():
                raise FileNotFoundError(f"{label} is missing: {path}")
            actual = _sha_file(path)
            if actual != spec["sha256"]:
                raise ValueError(f"{label} hash mismatch: {path}")
            return path, actual
        archive, archive_hash = resolve_file(config["a_tables_archive"], "A-table archive")
        consensus_path, consensus_hash = resolve_file(config["consensus_policy"], "consensus policy")
        belief_path, belief_hash = resolve_file(config["room_belief_policy"], "room-belief policy")
        profile_path, profile_hash = resolve_file(config["detector_profile"], "detector profile")
        ontology_path, ontology_hash = resolve_file(config["ontology"], "ontology")
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        aliases = profile.get("vocabulary", {}).get("canonical_map")
        if not isinstance(aliases, dict) or not aliases:
            raise ValueError("pinned detector profile has no canonical_map")
        from scripts.run_causal_hybrid_room_belief import canonical, load_a_tables
        normalized_aliases = {re.sub(r"\s+", " ", str(k).strip().lower()): str(v).strip().lower()
                              for k, v in aliases.items()}
        tables, categories, semantic_rooms, digests = load_a_tables(archive)
        consensus_doc = json.loads(consensus_path.read_text(encoding="utf-8"))
        belief_doc = json.loads(belief_path.read_text(encoding="utf-8"))
        ontology_doc = json.loads(ontology_path.read_text(encoding="utf-8"))
        if consensus_doc.get("view_group_policy_id") != "exact_camera_pose_matrix_v1":
            raise ValueError("T4 session must use the locked exact-pose view grouping")
        declared_rooms = [str(row["id"]) for row in ontology_doc.get("rooms", [])]
        if declared_rooms and tuple(sorted(semantic_rooms)) != tuple(sorted(declared_rooms)):
            raise ValueError("A-table room labels differ from pinned room-belief ontology")
        provenance = {
            "config_path": str(config_path), "config_sha256": _sha_file(config_path),
            "a_tables_path": str(archive), "a_tables_archive_sha256": archive_hash,
            "a_table_content_digests": digests,
            "consensus_policy_path": str(consensus_path), "consensus_policy_sha256": consensus_hash,
            "room_belief_policy_path": str(belief_path), "room_belief_policy_sha256": belief_hash,
            "detector_profile_path": str(profile_path), "detector_profile_sha256": profile_hash,
            "ontology_path": str(ontology_path), "ontology_sha256": ontology_hash,
            "canonicalization_policy": "pinned detector profile canonical_map; raw class preserved",
            "view_group_policy_id": consensus_doc["view_group_policy_id"],
            "admission_min_detections": admission_min_detections,
            "admission_source": "same run_conceptgraphs_cached_hybrid.base_mapping_config used by live ConceptGraphs worker",
            "scope_policy": effective_scope_policy,
        }
        policy = ConsensusPolicy(
            consensus_doc["policy_id"], consensus_doc["view_group_policy_id"],
            int(consensus_doc["minimum_view_groups"]), float(consensus_doc["minimum_top_share"]),
            float(consensus_doc["minimum_margin"]))
        room_policy = RoomBeliefPolicy(
            belief_doc["policy_id"], int(belief_doc["minimum_supported_categories"]),
            float(belief_doc["minimum_top_belief"]), float(belief_doc["minimum_margin"]),
            float(belief_doc["relabel_margin"]), int(belief_doc["relabel_evidence_revisions"]))
        session = cls(config=config, tables=tables, table_digests=digests, categories=categories,
                      semantic_rooms=semantic_rooms, aliases=normalized_aliases,
                      consensus_policy=policy, label_policy=room_policy,
                      admission_min_detections=admission_min_detections,
                      config_provenance=provenance, overlay_root=overlay_root)
        return session

    def _new_ledgers(self) -> dict[str, RoomBeliefLedger]:
        return {variant: RoomBeliefLedger(
            room_ids=self.semantic_rooms, room_node_ids=(), allow_empty_room_nodes=True,
            room_priors={room: 1.0 for room in self.semantic_rooms},
            category_room_scores=self.tables[variant], label_policy=self.label_policy,
            evidence_policy_id="rb1-causal-hybrid-v1", ontology_version="rb2-A-room-ontology-v1",
            a_table_hash=self.table_digests[variant]) for variant in VARIANTS}

    @classmethod
    def from_resolved(cls, **kwargs: Any) -> "RoomBeliefCommitSession":
        """Model-free test seam; production callers should use :meth:`open`."""
        return cls(**kwargs)

    @property
    def status(self) -> str:
        return self._status

    def bind_identity(self, *, run_id: str, scene_id: str, sequence_id: str) -> None:
        if self._status != "open":
            raise RuntimeError("session identity can only be bound while open")
        identity = (_require_text(run_id, "run_id"), _require_text(scene_id, "scene_id"),
                    _require_text(sequence_id, "sequence_id"))
        if self.run_id is not None and identity != (self.run_id, self.scene_id, self.sequence_id):
            raise ValueError("semantic session is already bound to another run/scene/sequence")
        self.run_id, self.scene_id, self.sequence_id = identity

    def _resolve_scope(self, envelope: GraphCommitEnvelope) -> tuple[str, str | None, str | None, str]:
        scope = envelope.scope
        status = str(scope.get("status", "unavailable"))
        reason = str(scope.get("reason", "unavailable_elevation_scope"))
        policy = self.config["scope_policy"]
        mode = policy["mode"]
        if mode == "verified_only":
            if status != "verified" or scope.get("evidence_status") != "verified":
                return "unavailable", None, None, reason if status == "unavailable" else "verified_scope_evidence_missing"
            return "verified", None, None, "verified_scope"
        declaration_pin = policy.get("assumed_declaration")
        if not isinstance(declaration_pin, Mapping) or set(declaration_pin) != {"path", "sha256"}:
            return "unavailable", None, None, "assumed_scope_declaration_missing"
        declaration_path = Path(str(declaration_pin["path"]))
        if not declaration_path.is_absolute():
            declaration_path = (ROOT / declaration_path).resolve()
        if not declaration_path.is_file() or _sha_file(declaration_path) != declaration_pin["sha256"]:
            return "unavailable", None, None, "assumed_scope_declaration_hash_mismatch"
        declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
        if (declaration.get("mode") != "assumed_single_level_demo"
                or declaration.get("scope_status") != "assumed"
                or declaration.get("approval", {}).get("status") != "approved_by_user"
                or declaration.get("approval", {}).get("approved_by_user") is not True
                or declaration.get("assumption", {}).get("evidence_status") != "assumed_not_verified"):
            return "unavailable", None, None, "assumed_scope_declaration_not_approved"
        expected_hash = str(declaration_pin["sha256"])
        expected_id = declaration.get("id")
        if "floor_id" in declaration or "z_bounds" in declaration:
            return "unavailable", None, None, "assumed_scope_declaration_invents_floor_bounds"
        if (status != "assumed" or scope.get("evidence_status") != "assumed_not_verified"
                or scope.get("declaration_id") != expected_id
                or scope.get("declaration_sha256") != expected_hash
                or scope.get("run_id") != envelope.run_id
                or scope.get("scene_id") != envelope.scene_id
                or scope.get("sequence_id") != envelope.sequence_id
                or declaration.get("run_id") != envelope.run_id
                or declaration.get("scene_id") != envelope.scene_id
                or declaration.get("sequence_id") != envelope.sequence_id):
            return "unavailable", None, None, "assumed_scope_declaration_mismatch"
        start, end = declaration.get("frame_start"), declaration.get("frame_end")
        if type(start) is not int or type(end) is not int or not start <= envelope.frame_index <= end:
            return "unavailable", None, None, "assumed_scope_outside_frame_range"
        return "assumed", str(expected_id), str(expected_hash), "assumed_not_verified"

    @staticmethod
    def _verify_graph(envelope: GraphCommitEnvelope) -> dict[str, Any]:
        path = Path(envelope.graph_path).expanduser().resolve()
        if not path.is_file() or _sha_file(path) != envelope.graph_sha256:
            raise ValueError("unified graph artifact is missing or hash-mismatched")
        graph = json.loads(path.read_text(encoding="utf-8"))
        expected = (envelope.graph_revision_id, envelope.scene_id, envelope.sequence_id,
                    envelope.frame_id, envelope.frame_index)
        actual = (graph.get("graph_revision_id"), graph.get("scene_id"), graph.get("sequence_id"),
                  graph.get("frame_id"), graph.get("frame_index"))
        if actual != expected:
            raise ValueError(f"graph artifact identity differs from envelope: {actual} != {expected}")
        if (type(graph.get("object_revision")) is not int or graph.get("object_revision") != envelope.object_revision
                or type(graph.get("room_revision")) is not int or graph.get("room_revision") != envelope.room_revision):
            raise ValueError("unified graph object/room revisions differ from envelope")
        refs = graph.get("source_revisions", {})
        if refs:
            if (refs.get("object", {}).get("state_sha256") != envelope.object_state_sha256
                    or refs.get("room", {}).get("state_sha256") != envelope.room_state_sha256):
                raise ValueError("unified graph source snapshot hashes differ from envelope")
        graph_object_ids = {str(row.get("causal_object_id")) for row in graph.get("objects", [])}
        envelope_object_ids = {str(row.get("object_id")) for row in envelope.objects}
        if graph_object_ids and graph_object_ids != envelope_object_ids:
            raise ValueError("unified graph object nodes differ from the current object snapshot")
        graph_room_ids = {str(row.get("node_id")) for row in graph.get("rooms", [])}
        envelope_room_ids = {str(row.get("node_id")) for row in envelope.rooms}
        if graph_room_ids and graph_room_ids != envelope_room_ids:
            raise ValueError("unified graph room nodes differ from the current room snapshot")
        return graph

    def _observation_rows(self, envelope: GraphCommitEnvelope) -> dict[str, dict[str, Any]]:
        catalog = copy.deepcopy(self._observation_catalog)
        current_ids: set[str] = set()
        for row in envelope.current_observations:
            oid = _require_text(row.get("observation_id"), "observation_id")
            if oid in current_ids:
                raise ValueError(f"duplicate observation ID in current frame: {oid}")
            current_ids.add(oid)
            if row.get("frame_id") != envelope.frame_id or row.get("frame_index") != envelope.frame_index:
                raise ValueError(f"observation {oid} is not from the current committed frame")
            pose = row.get("camera_pose_W_Copt")
            if not isinstance(pose, list) or len(pose) != 4 or any(not isinstance(x, list) or len(x) != 4 for x in pose):
                raise ValueError(f"observation {oid} lacks the exact 4x4 camera pose required by V1")
            pose_bytes = _canonical_json(pose)
            pose_hash = _sha_bytes(pose_bytes)[:16]
            source_hashes = row.get("source_hashes")
            if not isinstance(source_hashes, Mapping) or not source_hashes:
                raise ValueError(f"observation {oid} has no source hashes")
            for source_name, digest in source_hashes.items():
                if not isinstance(source_name, str) or not source_name or not isinstance(digest, str) or not _SHA_RE.fullmatch(digest):
                    raise ValueError(f"observation {oid} has invalid source hash provenance")
            raw_class = row.get("raw_class")
            category = self._canonical(raw_class, self.aliases) if raw_class is not None else None
            semantic = SemanticObservation(
                observation_id=oid, frame_id=envelope.frame_id,
                view_group_id="pose_exact:" + pose_hash,
                raw_class=None if raw_class is None else str(raw_class), category=category)
            normalized = {**semantic.as_dict(), "frame_index": envelope.frame_index,
                          "source_hashes": dict(source_hashes),
                          "camera_pose_sha256": _sha_bytes(pose_bytes)}
            prior = catalog.get(oid)
            if prior is not None and prior != normalized:
                raise ValueError(f"observation ID was reused with different semantic/source content: {oid}")
            catalog[oid] = normalized
        return catalog

    def update_after_unified_commit(self, envelope_value: GraphCommitEnvelope | Mapping[str, Any]) -> dict[str, Any]:
        if self._status != "open":
            raise RuntimeError(f"semantic session is {self._status}")
        envelope = envelope_value if isinstance(envelope_value, GraphCommitEnvelope) else GraphCommitEnvelope.from_dict(envelope_value)
        self.bind_identity(run_id=envelope.run_id, scene_id=envelope.scene_id, sequence_id=envelope.sequence_id)
        event_hash = _sha_bytes(_canonical_json(envelope.as_dict()))
        previous = self._seen_events.get(envelope.event_id)
        if previous is not None:
            if previous[0] != event_hash:
                raise ValueError("unified event_id was reused with a different envelope hash")
            return {**previous[1], "idempotent_replay": True}
        if envelope.unified_revision <= self._last_unified_revision:
            raise ValueError("stale/out-of-order unified revision rejected before ledger mutation")
        if envelope.frame_index <= self._last_frame_index or envelope.timestamp_s <= self._last_timestamp_s:
            raise ValueError("stale/out-of-order frame index or timestamp rejected before ledger mutation")
        if envelope.frame_id in self._seen_frame_ids:
            raise ValueError("frame_id was reused by a later unified revision")
        if envelope.object_revision <= self._last_object_revision or envelope.room_revision <= self._last_room_revision:
            raise ValueError("stale object/room source revision rejected before ledger mutation")
        graph = self._verify_graph(envelope)
        if graph.get("future_observations_used") not in (None, False):
            raise ValueError("unified graph declares future observations")
        catalog = self._observation_rows(envelope)
        active_rooms = {_require_text(row.get("node_id"), "room node_id") for row in envelope.rooms}
        if len(active_rooms) != len(envelope.rooms):
            raise ValueError("duplicate active room node in GraphCommitEnvelope")
        scope_status, declaration_id, declaration_hash, scope_reason = self._resolve_scope(envelope)
        ledgers = copy.deepcopy(self._ledgers)
        for ledger in ledgers.values():
            for room_id in sorted(active_rooms):
                ledger.register_room_node(room_id)

        object_rows: list[dict[str, Any]] = []
        current_ids: set[str] = set()
        current_memberships: dict[str, tuple[str, str | None]] = {}
        membership_support_by_room: dict[str, list[dict[str, Any]]] = {}
        events_by_variant: dict[str, list[RoomBeliefEvent]] = {variant: [] for variant in VARIANTS}
        diagnostics: dict[str, dict[str, dict[str, Any]]] = {variant: {} for variant in VARIANTS}
        room_contains_edges: list[dict[str, Any]] = []
        observation_ids_in_frame = set(current for current in catalog
                                       if int(catalog[current]["frame_index"]) == envelope.frame_index)

        for raw in envelope.objects:
            object_id = _require_text(raw.get("object_id"), "object_id")
            if object_id in current_ids:
                raise ValueError(f"duplicate object ID in graph prefix: {object_id}")
            current_ids.add(object_id)
            obs_ids = tuple(str(x) for x in raw.get("observation_ids", []))
            if len(obs_ids) != len(set(obs_ids)):
                raise ValueError(f"duplicate observation lineage for object {object_id}")
            missing = [oid for oid in obs_ids if oid not in catalog]
            if missing:
                raise ValueError(f"object {object_id} references observations not yet committed: {missing[:3]}")
            detection_count = raw.get("detection_count")
            if type(detection_count) is not int or detection_count < 0:
                raise ValueError(f"object {object_id} has invalid detection_count")
            admitted = detection_count >= self.admission_min_detections
            obs_records = []
            for oid in obs_ids:
                item = catalog[oid]
                if int(item["frame_index"]) > envelope.frame_index:
                    raise ValueError(f"future observation in prefix {envelope.frame_index}: {oid}")
                obs_records.append(SemanticObservation(
                    observation_id=oid, frame_id=str(item["frame_id"]),
                    view_group_id=str(item["view_group_id"]), raw_class=item.get("raw_class"),
                    category=item.get("category"), admission_status="retained" if admitted else "deferred"))
            consensus = aggregate_object_semantics(object_id, obs_records,
                semantic_revision=envelope.unified_revision, policy=self.consensus_policy)
            membership = dict(raw.get("membership") or {"status": "unavailable", "reason": scope_reason})
            membership_status = str(membership.get("status", "unavailable"))
            if scope_status == "unavailable":
                membership = {"status": "unavailable", "reason": scope_reason,
                              "scope_status": "unavailable", "declaration_id": None,
                              "declaration_sha256": None}
                # RB1's ledger requires a concrete membership enum. Keep the
                # public status unavailable, but represent the closed gate as
                # non-assigned internally so no room/category evidence can enter.
                membership_status = "unassigned"
            elif membership_status not in {"assigned", "unassigned", "ambiguous"}:
                raise ValueError(f"scope {scope_status} requires a prefix membership result for {object_id}")
            elif membership_status == "assigned":
                room_id = membership.get("room_id")
                if room_id not in active_rooms:
                    raise ValueError(f"membership for {object_id} targets inactive room {room_id}")
                if membership.get("policy") not in (None, "greedy_voxel_support_v1"):
                    raise ValueError("membership policy differs from locked greedy voxel support")
                if (membership.get("scope_status") != scope_status
                        or membership.get("room_revision") != envelope.room_revision
                        or membership.get("object_revision") != envelope.object_revision):
                    raise ValueError(f"membership for {object_id} is not bound to the current scope/object/room revisions")
                current_memberships[object_id] = ("assigned", str(room_id))
                room_contains_edges.append({"relation": "room_contains", "source_id": str(room_id),
                    "target_id": object_id, "provenance": "rb1_prefix_greedy_voxel_support",
                    "unified_revision": envelope.unified_revision})
                membership_support_by_room.setdefault(str(room_id), []).append({
                    "object_id": object_id, "support_fraction": membership.get("support_fraction"),
                    "runner_up_fraction": membership.get("runner_up_fraction"),
                    "outside_fraction": membership.get("outside_fraction"),
                    "support_counts": membership.get("support_counts", {}),
                    "tied": membership.get("tied"), "geometry_revisions": membership.get("geometry_revisions", {}),
                })
            else:
                current_memberships[object_id] = (membership_status, None)
            object_doc = {
                "object_id": object_id, "hypothesis_id": object_id,
                "snapshot_hypothesis_id": object_id, "causal_object_id": object_id,
                "observation_ids": list(obs_ids),
                "prefix_admission": {"eligible": admitted, "status": "eligible" if admitted else "provisional",
                    "reason": f"detection_count>={self.admission_min_detections}" if admitted else
                              f"detection_count<{self.admission_min_detections}; provisional_only"},
                "semantic_consensus": consensus.as_dict() | {"future_observations_used": False},
                "membership": membership,
                "evidence_eligibility": {"eligible": False, "reasons": []},
            }
            if scope_status == "unavailable":
                object_doc["evidence_eligibility"] = {"eligible": False,
                    "reasons": ["membership_unavailable", scope_reason]}
            object_rows.append(object_doc)
            for variant in VARIANTS:
                category = consensus.category
                eligible = False
                if scope_status != "unavailable":
                    from scripts.run_causal_hybrid_room_belief import room_evidence_eligible
                    eligible = room_evidence_eligible(admitted, consensus.status, membership,
                                                      category, self.tables[variant])
                evidence = RoomObjectEvidence(
                    object_id=object_id, revision=envelope.unified_revision, retained=eligible,
                    semantic_status=consensus.status, category=category,
                    membership_status=(membership_status if membership_status in {"assigned", "unassigned", "ambiguous"}
                                       else "unassigned"),
                    room_id=membership.get("room_id") if membership_status == "assigned" and scope_status != "unavailable" else None,
                    source_observation_ids=tuple(consensus.eligible_observation_ids),
                    provenance=f"t4-unified-prefix:{variant}", membership_evidence=membership,
                    semantic_evidence={"status": consensus.status, "category": category,
                        "policy_id": consensus.policy_id, "view_group_count": consensus.view_group_count,
                        "scope_status": scope_status, "scope_reason": scope_reason,
                        "declaration_id": declaration_id, "declaration_sha256": declaration_hash,
                        "future_observations_used": False})
                events_by_variant[variant].append(RoomBeliefEvent(
                    f"t4:{envelope.run_id}:{variant}:{object_id}:{envelope.unified_revision}",
                    object_id, envelope.unified_revision, evidence))
                if eligible:
                    object_doc["evidence_eligibility"]["eligible"] = True
                elif scope_status != "unavailable":
                    reasons = object_doc["evidence_eligibility"]["reasons"]
                    if not admitted: reasons.append("prefix_admission_not_met")
                    if consensus.status != "stable": reasons.append(f"semantic_{consensus.status}")
                    if membership_status != "assigned": reasons.append(membership.get("reason", "membership_unassigned"))
                    if category not in self.tables[variant]: reasons.append("unsupported_category")

        for object_id in sorted(self._object_ids - current_ids):
            for variant in VARIANTS:
                events_by_variant[variant].append(RoomBeliefEvent(
                    f"t4:{envelope.run_id}:{variant}:{object_id}:{envelope.unified_revision}",
                    object_id, envelope.unified_revision, None))
        # A room retirement invalidates all old membership evidence for any object
        # that is still present but has no valid current membership in an active room.
        for object_id, (old_status, old_room_id) in self._object_memberships.items():
            if old_status == "assigned" and old_room_id not in active_rooms and object_id not in current_ids:
                for variant in VARIANTS:
                    event_id = f"t4:{envelope.run_id}:{variant}:{object_id}:{envelope.unified_revision}"
                    if not any(event.object_id == object_id for event in events_by_variant[variant]):
                        events_by_variant[variant].append(RoomBeliefEvent(event_id, object_id,
                            envelope.unified_revision, None))

        prior_history = {variant: {room_id: len(ledgers[variant].room_state(room_id).label_history)
                                   for room_id in active_rooms}
                         for variant in VARIANTS}
        for variant in VARIANTS:
            if events_by_variant[variant]:
                ledgers[variant].apply_frame(events_by_variant[variant])
            variant_rooms: dict[str, Any] = {}
            for room_id in sorted(active_rooms):
                if scope_status == "unavailable":
                    variant_rooms[room_id] = {
                        "status": "unavailable", "reason": scope_reason,
                        "eligible_categories": [], "candidate_label": None,
                        "committed_label": None, "abstain_reason": None,
                        "not_scored": True,
                    }
                    continue
                state = ledgers[variant].room_state(room_id).as_dict()
                categories = sorted(category for category, ids in ledgers[variant].category_members.get(room_id, {}).items() if ids)
                support_ids = sorted({oid for category in categories
                                      for oid in ledgers[variant].category_members[room_id][category]})
                ranked = state.get("candidates", [])
                candidate = ranked[0][0] if ranked else None
                raw_scores = dict(state.get("raw_scores", []))
                from scripts.run_causal_hybrid_room_belief import _decision_fields
                decision = _decision_fields(state, self.label_policy, {"status": scope_status})
                abstain = decision["abstain_reason"]
                contributions = [{"category": cat, "A_log_ratio_by_room_type": self.tables[variant][cat],
                                  "object_ids": sorted(ledgers[variant].category_members[room_id][cat])}
                                 for cat in categories]
                variant_rooms[room_id] = {
                    "status": state.get("status"), "reason": abstain,
                    "eligible_categories": categories, "eligible_objects": support_ids,
                    "category_contributions": contributions, "raw_S": raw_scores,
                    "beliefs": ranked, "candidate_label": decision["candidate_label"],
                    "committed_label": decision["committed_label"], "abstain_reason": abstain,
                    "decision_reason": decision["decision_reason"],
                    "label_history_delta": state.get("label_history", [])[prior_history[variant].get(room_id, 0):],
                    "membership_support": sorted(membership_support_by_room.get(room_id, []), key=lambda row: row["object_id"]),
                    "not_scored": False,
                }
            diagnostics[variant] = variant_rooms

        active_before = self._active_rooms
        room_lifecycle = {"active_room_ids": sorted(active_rooms),
                          "created_room_ids": sorted(active_rooms - active_before),
                          "retired_room_ids": sorted(active_before - active_rooms),
                          "room_revision": envelope.room_revision}
        overlay = {
            "schema": "t4-room-belief-semantic-overlay/v1", "status": "complete",
            "event_id": envelope.event_id, "event_sha256": event_hash,
            "run_id": envelope.run_id, "scene_id": envelope.scene_id,
            "sequence_id": envelope.sequence_id, "frame_index": envelope.frame_index,
            "frame_id": envelope.frame_id, "timestamp_s": envelope.timestamp_s,
            "unified_revision": envelope.unified_revision,
            "graph_revision_id": envelope.graph_revision_id, "graph_sha256": envelope.graph_sha256,
            "revision": envelope.object_revision,
            "object_revision": envelope.object_revision, "object_state_sha256": envelope.object_state_sha256,
            "room_revision": envelope.room_revision, "room_state_sha256": envelope.room_state_sha256,
            "source_hashes": dict(envelope.source_hashes),
            "scope_status": scope_status, "scope_reason": scope_reason,
            "membership_scope": {"status": scope_status, "reason": scope_reason,
                                 "declaration_id": declaration_id,
                                 "declaration_sha256": declaration_hash},
            "declaration_id": declaration_id, "declaration_sha256": declaration_hash,
            "scope_policy": dict(self.config["scope_policy"]),
            "membership_availability": {"status": scope_status if scope_status != "unavailable" else "unavailable",
                                        "reason": scope_reason},
            "room_semantics_availability": {"status": "not_scored" if scope_status == "unavailable" else "available",
                                             "reason": scope_reason if scope_status == "unavailable" else None},
            "objects": object_rows, "rooms": diagnostics,
            "room_diagnostics": diagnostics, "room_contains_edges": room_contains_edges,
            "room_lifecycle": room_lifecycle, "future_observations_used": False,
            "input_provenance": self.config_provenance,
            "variants": list(VARIANTS), "scope_modes": ["verified_only"],
            "config_provenance": self.config_provenance,
            "ledger_event_counts": {variant: len(events_by_variant[variant]) for variant in VARIANTS},
        }
        overlay_path = self.overlay_root / f"rev-{envelope.unified_revision:06d}.json"
        if overlay_path.exists():
            raise FileExistsError(f"refusing to overwrite semantic overlay: {overlay_path}")
        overlay_sha = _atomic_json(overlay_path, overlay)
        result = {"status": "complete", "overlay_path": str(overlay_path),
                  "overlay_sha256": overlay_sha, "overlay": overlay,
                  "unified_revision": envelope.unified_revision,
                  "semantic_status": overlay["room_semantics_availability"]["status"]}
        # Mutate session state only after immutable overlay is safely persisted.
        self._ledgers = ledgers
        self._observation_catalog = catalog
        self._object_ids = current_ids
        self._object_memberships = current_memberships
        self._active_rooms = active_rooms
        self._seen_events[envelope.event_id] = (event_hash, result)
        self._last_unified_revision = envelope.unified_revision
        self._last_object_revision = envelope.object_revision
        self._last_room_revision = envelope.room_revision
        self._last_frame_index = envelope.frame_index
        self._last_timestamp_s = envelope.timestamp_s
        self._seen_frame_ids.add(envelope.frame_id)
        return result

    def write_failure_overlay(self, envelope_value: GraphCommitEnvelope | Mapping[str, Any], error: BaseException) -> dict[str, Any]:
        """Publish a graph-bound failure state without labels or stale semantics."""
        envelope = envelope_value if isinstance(envelope_value, GraphCommitEnvelope) else GraphCommitEnvelope.from_dict(envelope_value)
        self._verify_graph(envelope)
        doc = {"schema": "t4-room-belief-semantic-overlay/v1", "status": "failed",
               "event_id": envelope.event_id, "run_id": envelope.run_id,
               "scene_id": envelope.scene_id, "sequence_id": envelope.sequence_id,
               "frame_index": envelope.frame_index, "frame_id": envelope.frame_id,
               "timestamp_s": envelope.timestamp_s, "unified_revision": envelope.unified_revision,
               "graph_revision_id": envelope.graph_revision_id, "graph_sha256": envelope.graph_sha256,
               "object_revision": envelope.object_revision, "object_state_sha256": envelope.object_state_sha256,
               "room_revision": envelope.room_revision, "room_state_sha256": envelope.room_state_sha256,
               "scope_status": "unavailable", "scope_reason": "semantic_update_failed",
               "scope_policy": dict(self.config["scope_policy"]),
               "membership_scope": {"status": "unavailable", "reason": "semantic_update_failed"},
               "membership_availability": {"status": "unavailable", "reason": "semantic_update_failed"},
               "room_semantics_availability": {"status": "failed", "reason": f"{type(error).__name__}: {error}"},
               "variants": list(VARIANTS), "scope_modes": ["verified_only"],
               "objects": [], "rooms": {variant: {} for variant in VARIANTS},
               "room_diagnostics": {variant: {} for variant in VARIANTS},
               "config_provenance": self.config_provenance,
               "room_contains_edges": [], "future_observations_used": False}
        path = self.overlay_root / f"rev-{envelope.unified_revision:06d}-failed.json"
        if path.exists():
            raise FileExistsError(f"refusing to overwrite semantic failure overlay: {path}")
        digest = _atomic_json(path, doc)
        return {"status": "failed", "overlay_path": str(path), "overlay_sha256": digest,
                "overlay": doc}

    def finalize(self, terminal_event: Mapping[str, Any]) -> dict[str, Any]:
        if self._status == "closed":
            raise RuntimeError("cannot finalize a closed semantic session")
        if self._status == "finalized":
            return self._terminal_result or {}
        lineage = terminal_event.get("lineage")
        doc = {"schema": "t4-room-belief-terminal/v1", "status": "terminal_semantics_unavailable",
               "run_id": self.run_id, "scene_id": self.scene_id, "sequence_id": self.sequence_id,
               "terminal_event": dict(terminal_event), "lineage_available": bool(lineage),
               "reason": "terminal_postprocess_lineage_not_proven" if not lineage else
                         "terminal event requires an explicit terminal ledger update adapter",
               "prefix_ledgers_modified": False}
        path = self.overlay_root.parent / "terminal.json"
        if path.exists():
            raise FileExistsError(f"refusing to overwrite terminal semantic document: {path}")
        digest = _atomic_json(path, doc)
        self._status = "finalized"
        self._terminal_result = {"status": doc["status"], "path": str(path), "sha256": digest, "document": doc}
        return self._terminal_result

    def close(self) -> None:
        if self._status == "closed":
            return
        self._status = "closed"

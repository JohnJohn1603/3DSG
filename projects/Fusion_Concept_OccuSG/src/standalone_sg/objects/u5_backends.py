"""Experimental U5 M9 object association backend adapters.

The source wrapper delegates without changing the source manager's behavior.
The geometry backend adapts the frozen U3 geometry-only assignment policy to
M9's update/snapshot contract. Neither backend merges or retires tracks.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import Mapping

import numpy as np

from ..contracts.objects import DetectionBatch
from .association import (
    ObjectAssociation,
    ObjectAssociationBackend,
    ObjectAssociationManager,
    ObjectAssociationResult,
    ObjectNode,
)
from .lifecycle import ObjectBackendCapabilities, ObjectLifecycleEvent
from .same_frame_duplicates_v2 import canonicalize_same_frame_duplicates_v2
from .u3_experimental import ExperimentalAssociation, U3Observation


class SourceAssociationBackend:
    """Transparent protocol wrapper around the unchanged source manager."""

    backend_name = "source_manager"

    def __init__(self, manager: ObjectAssociationManager) -> None:
        self.manager = manager
        self.last_lifecycle_events: tuple[dict, ...] = ()
        # Keep lifecycle identity identical to the wrapped manager so direct
        # source and wrapper runs serialize the same graph state.
        self.capabilities = manager.capabilities

    def update(self, batch: DetectionBatch) -> ObjectAssociationResult:
        result = self.manager.update(batch)
        events = []
        events.extend({"event": "created", "object_id": object_id} for object_id in result.new_object_ids)
        events.extend({"event": "updated", "object_id": object_id} for object_id in result.updated_object_ids)
        self.last_lifecycle_events = tuple(events)
        return result

    def snapshot(self) -> tuple[ObjectNode, ...]:
        return self.manager.snapshot()


class GeometryOnlyAssociationBackend:
    """Causal geometry-only U3 backend adapted to SceneGraphBuilder/M9.

    Input batches are time ordered by M9. Detections inside a frame are sorted
    by ID to match the frozen U3 runner. Same-frame aliases are suppressed
    before association and surfaced as lifecycle diagnostics.
    """

    backend_name = "u3_geometry_only"

    def __init__(self, *, duplicate_aliases: Mapping[str, str] | None = None,
                 max_center_m: float = 0.75, geom_min_iou: float = 0.01,
                 geom_only_min_iou: float = 0.05, ambiguity_margin: float = 0.05) -> None:
        self.engine = ExperimentalAssociation(
            "geometry", max_center_m=max_center_m, geom_min_iou=geom_min_iou,
            geom_only_min_iou=geom_only_min_iou, ambiguity_margin=ambiguity_margin,
        )
        self.duplicate_aliases = dict(duplicate_aliases or {})
        self._objects: dict[str, ObjectNode] = {}
        self._assignment_by_detection: dict[str, str] = {}
        self.last_lifecycle_events: tuple[dict, ...] = ()
        self.last_candidate_ledger: tuple[dict, ...] = ()
        self.last_decisions: tuple[dict, ...] = ()
        self.last_deferred_detection_ids: tuple[str, ...] = ()
        self._last_timestamp = float("-inf")
        self._lifecycle_revision = 0
        self.capabilities = ObjectBackendCapabilities(1, ("create", "update"), backend_name=self.backend_name)

    def snapshot(self) -> tuple[ObjectNode, ...]:
        return tuple(self._objects[key] for key in sorted(self._objects))

    def update(self, batch: DetectionBatch) -> ObjectAssociationResult:
        if batch.timestamp_s < self._last_timestamp:
            raise ValueError("geometry backend requires nondecreasing batch timestamps")
        self._last_timestamp = batch.timestamp_s
        self.last_lifecycle_events = ()
        self.last_candidate_ledger = ()
        self.last_decisions = ()
        self.last_deferred_detection_ids = ()
        if batch.detections is None:
            return ObjectAssociationResult("provider_absent", batch.frame_id, batch.timestamp_s, (), self.snapshot())
        if not batch.detections:
            return ObjectAssociationResult("empty_frame", batch.frame_id, batch.timestamp_s, (), self.snapshot())

        all_detections = sorted(batch.detections, key=lambda detection: detection.detection_id)
        canonical = [d for d in all_detections if d.detection_id not in self.duplicate_aliases]
        observations = [U3Observation(
            detection_id=detection.detection_id,
            frame_id=batch.frame_id,
            timestamp_s=detection.timestamp_s,
            center=detection.center_W,
            extents=detection.extents_m,
            layer_id=detection.layer_id,
            class_name=detection.class_name,
            feature=None,
            mask_hash=str(detection.provider_metadata.get("mask_sha256", "")),
        ) for detection in canonical]
        decision_start = len(self.engine.decisions)
        ledger_start = len(self.engine.ledger)
        frame_decisions = self.engine.update_frame(observations) if observations else []
        frame_ledger = self.engine.ledger[ledger_start:]
        del self.engine.ledger[ledger_start:]
        events: list[dict] = []
        associations: list[ObjectAssociation] = []
        typed_events: list[ObjectLifecycleEvent] = []
        new_ids: list[str] = []
        updated_ids: list[str] = []
        det_by_id = {d.detection_id: d for d in canonical}
        for decision in frame_decisions:
            detection = det_by_id[decision.detection_id]
            object_id = decision.track_id
            if decision.decision == "created" and object_id is not None:
                node = ObjectNode(
                    object_id=object_id,
                    class_name=detection.class_name,
                    class_id=detection.class_id,
                    confidence=detection.confidence,
                    center_W=detection.center_W,
                    center_method=detection.center_method,
                    extents_m=detection.extents_m,
                    layer_id=detection.layer_id,
                    first_seen_s=detection.timestamp_s,
                    last_seen_s=detection.timestamp_s,
                    first_detection_id=detection.detection_id,
                    last_detection_id=detection.detection_id,
                    first_provider_id=batch.provider_id,
                    last_provider_id=batch.provider_id,
                )
                self._objects[object_id] = node
                self._assignment_by_detection[detection.detection_id] = object_id
                new_ids.append(object_id)
                events.append({"event": "created", "track_id": object_id, "detection_id": detection.detection_id})
                associations.append(self._association(detection, batch, object_id, "created", 0.0))
                typed_events.append(self._lifecycle_event("create", object_id, detection, batch))
            elif decision.decision == "matched" and object_id is not None:
                prior = self._objects[object_id]
                center = tuple((x + y) / 2.0 for x, y in zip(prior.center_W, detection.center_W))
                if prior.extents_m is not None and detection.extents_m is not None:
                    extents = tuple((x + y) / 2.0 for x, y in zip(prior.extents_m, detection.extents_m))
                else:
                    extents = detection.extents_m or prior.extents_m
                self._objects[object_id] = replace(
                    prior, center_W=center, extents_m=extents,
                    last_seen_s=detection.timestamp_s,
                    last_detection_id=detection.detection_id,
                    last_provider_id=batch.provider_id,
                )
                self._assignment_by_detection[detection.detection_id] = object_id
                updated_ids.append(object_id)
                events.append({"event": "updated", "track_id": object_id, "detection_id": detection.detection_id})
                distance = next((float(row["center_distance_m"]) for row in frame_ledger
                                 if row.get("detection_id") == detection.detection_id
                                 and row.get("track_id") == object_id), 0.0)
                associations.append(self._association(detection, batch, object_id, "geometry_match", distance))
                typed_events.append(self._lifecycle_event("update", object_id, detection, batch))
            elif decision.decision == "defer":
                events.append({"event": "deferred", "detection_id": detection.detection_id, "reason": decision.reason})

        deferred = tuple(d.detection_id for d in frame_decisions if d.decision == "defer")
        for detection in all_detections:
            target = self.duplicate_aliases.get(detection.detection_id)
            if target is None:
                continue
            track_id = self._assignment_by_detection.get(target)
            events.append({"event": "duplicate_suppressed", "detection_id": detection.detection_id,
                           "duplicate_of": target, "track_id": track_id})
            if track_id is not None:
                associations.append(self._association(detection, batch, track_id, "duplicate_suppressed", 0.0))

        self.last_lifecycle_events = tuple(events)
        self.last_candidate_ledger = tuple(dict(row) for row in frame_ledger)
        self.last_decisions = tuple({
            "detection_id": row.detection_id, "track_id": row.track_id,
            "decision": row.decision, "selected_candidate_score": row.selected_candidate_score,
            "reason": row.reason,
        } for row in frame_decisions)
        self.last_deferred_detection_ids = deferred
        return ObjectAssociationResult("processed", batch.frame_id, batch.timestamp_s,
                                       tuple(associations), self.snapshot(), tuple(new_ids), tuple(updated_ids),
                                       tuple(typed_events))

    def _lifecycle_event(self, kind: str, object_id: str, detection, batch: DetectionBatch) -> ObjectLifecycleEvent:
        self._lifecycle_revision += 1
        return ObjectLifecycleEvent(
            schema_version=1,
            event_id=f"u3-geometry:{self._lifecycle_revision}:{kind}:{object_id}:{detection.detection_id}",
            kind=kind,
            object_id=object_id,
            frame_id=batch.frame_id,
            timestamp_s=detection.timestamp_s,
            causal_revision=self._lifecycle_revision,
            provenance="ExperimentalAssociation:u3_geometry_locked_v1",
            evidence_ids=(detection.detection_id,),
        )

    @staticmethod
    def _association(detection, batch, object_id: str, decision: str, score: float) -> ObjectAssociation:
        return ObjectAssociation(
            detection_id=detection.detection_id, provider_id=batch.provider_id,
            frame_id=batch.frame_id, timestamp_s=detection.timestamp_s,
            object_id=object_id, decision=decision, distance_m=float(score),
            normalized_class_name=ObjectAssociationManager._normalize(detection.class_name),
        )


class GeometryOnlyDuplicateV2AssociationBackend(GeometryOnlyAssociationBackend):
    """Opt-in M9 backend with the locked same-frame duplicate v2 guard.

    The canonicalizer sees only detections from the current DetectionBatch.
    P2 masks are loaded from hash-bound cache paths and P3 support comes from
    each detection's saved provider metadata. Ambiguous observations remain in
    the geometry association input; this class does not quarantine them.
    Observation aliases are diagnostics, never graph-track merge events.
    """

    backend_name = "u3_geometry_only_dedup_v2"

    def __init__(self, *, duplicate_config: dict,
                 max_center_m: float = 0.75, geom_min_iou: float = 0.01,
                 geom_only_min_iou: float = 0.05, ambiguity_margin: float = 0.05) -> None:
        super().__init__(max_center_m=max_center_m, geom_min_iou=geom_min_iou,
                         geom_only_min_iou=geom_only_min_iou,
                         ambiguity_margin=ambiguity_margin)
        self.duplicate_config = json.loads(json.dumps(duplicate_config))
        self.capabilities = ObjectBackendCapabilities(
            1, ("create", "update"), backend_name=self.backend_name,
        )
        self.last_duplicate_decisions: tuple[dict, ...] = ()
        self.last_duplicate_aliases: tuple[dict, ...] = ()
        self.last_duplicate_pair_evidence: tuple[dict, ...] = ()
        self.last_ambiguous_detection_ids: tuple[str, ...] = ()

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def update(self, batch: DetectionBatch) -> ObjectAssociationResult:
        self.last_duplicate_decisions = ()
        self.last_duplicate_aliases = ()
        self.last_duplicate_pair_evidence = ()
        self.last_ambiguous_detection_ids = ()
        if batch.detections is None or not batch.detections:
            self.duplicate_aliases = {}
            return super().update(batch)

        observations = []
        masks: dict[str, np.ndarray] = {}
        support: dict[str, dict[str, object]] = {}
        for detection in sorted(batch.detections, key=lambda item: item.detection_id):
            metadata = detection.provider_metadata
            mask_path_value = metadata.get("u5_duplicate_v2_mask_path")
            mask_sha = metadata.get("u5_duplicate_v2_mask_sha256")
            if not isinstance(mask_path_value, str) or not isinstance(mask_sha, str):
                raise ValueError(f"v2 duplicate backend requires hash-bound P2 mask metadata: {detection.detection_id}")
            mask_path = Path(mask_path_value).resolve()
            if not mask_path.is_file() or self._sha256(mask_path) != mask_sha:
                raise ValueError(f"v2 duplicate backend P2 mask missing/hash mismatch: {detection.detection_id}")
            masks[detection.detection_id] = np.asarray(np.load(mask_path, allow_pickle=False), dtype=bool)
            observations.append(U3Observation(
                detection_id=detection.detection_id,
                frame_id=batch.frame_id,
                timestamp_s=detection.timestamp_s,
                center=detection.center_W,
                extents=detection.extents_m,
                layer_id=detection.layer_id,
                class_name=detection.class_name,
                feature=None,
                mask_hash=mask_sha,
            ))
            support[detection.detection_id] = {
                "valid_depth_pixels": int(metadata.get("u5_duplicate_v2_valid_depth_pixels", 0)),
                "cluster_points": int(metadata.get("u5_duplicate_v2_cluster_points", 0)),
                "detector_score": float(metadata.get("u5_duplicate_v2_detector_score", 0.0)),
            }

        kept, aliases, ledger = canonicalize_same_frame_duplicates_v2(
            observations, masks, support, self.duplicate_config,
        )
        alias_by_id = {str(row["detection_id"]): str(row["duplicate_of"]) for row in aliases}
        decisions = tuple(dict(row) for row in ledger["observation_decisions"])
        self.last_duplicate_decisions = decisions
        self.last_duplicate_aliases = tuple(dict(row) for row in aliases)
        self.last_duplicate_pair_evidence = tuple(dict(row) for row in ledger["pair_evidence"])
        self.last_ambiguous_detection_ids = tuple(ledger["ambiguous_observation_ids"])

        # The parent backend consumes only the alias decision for this frame;
        # prior raw observations are never removed from packet/history.
        self.duplicate_aliases = alias_by_id
        try:
            result = super().update(batch)
        finally:
            self.duplicate_aliases = {}

        # Keep observation alias diagnostics distinct from graph lifecycle.
        transformed = []
        for event in self.last_lifecycle_events:
            if event.get("event") == "duplicate_suppressed":
                transformed.append({
                    "event": "observation_alias",
                    "event_scope": "same_frame_observation",
                    "detection_id": event["detection_id"],
                    "duplicate_of": event["duplicate_of"],
                    "track_id": event.get("track_id"),
                    "counts_as_independent_view": False,
                    "semantic_vote_count": 0,
                    "graph_track_merge": False,
                })
            else:
                transformed.append(event)
        self.last_lifecycle_events = tuple(transformed)
        alias_decisions = []
        current_track_by_id = {row.detection_id: row.object_id for row in result.associations}
        for alias_id, canonical_id in sorted(alias_by_id.items()):
            alias_decisions.append({
                "detection_id": alias_id,
                "track_id": current_track_by_id.get(canonical_id),
                "decision": "duplicate_alias",
                "reason": "same_frame_duplicate_v2",
                "duplicate_of": canonical_id,
                "counts_as_independent_view": False,
            })
        self.last_decisions = tuple(self.last_decisions) + tuple(alias_decisions)
        return result

    def _lifecycle_event(self, kind: str, object_id: str, detection, batch: DetectionBatch) -> ObjectLifecycleEvent:
        event = super()._lifecycle_event(kind, object_id, detection, batch)
        return replace(event, provenance="ExperimentalAssociation:u3_geometry_locked_v1+same_frame_duplicate_v2")

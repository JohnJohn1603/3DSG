"""Pinned-code object association, deliberately separate from room membership."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Literal

from ..config.schema import ObjectAssociationConfig
from ..contracts.errors import ContractError
from ..contracts.objects import DetectionBatch, Detection3D


@dataclass(frozen=True, slots=True)
class ObjectNode:
    """Persistent output whose geometry/class/score remain first-observation values."""

    object_id: str
    class_name: str | None
    class_id: str | None
    confidence: float | None
    center_W: tuple[float, float, float]
    center_method: str
    extents_m: tuple[float, float, float] | None
    layer_id: str
    first_seen_s: float
    last_seen_s: float
    first_detection_id: str
    last_detection_id: str
    first_provider_id: str
    last_provider_id: str


@dataclass(frozen=True, slots=True)
class ObjectAssociation:
    detection_id: str
    provider_id: str
    frame_id: str
    timestamp_s: float
    object_id: str
    decision: Literal["created", "same_class_match", "same_pose_suppressed"]
    distance_m: float
    normalized_class_name: str | None
    provenance_kind: Literal["detection_association"] = "detection_association"


@dataclass(frozen=True, slots=True)
class ObjectAssociationResult:
    status: Literal["provider_absent", "empty_frame", "processed"]
    frame_id: str
    timestamp_s: float
    associations: tuple[ObjectAssociation, ...]
    objects: tuple[ObjectNode, ...]
    new_object_ids: tuple[str, ...] = ()
    updated_object_ids: tuple[str, ...] = ()


class ObjectAssociationManager:
    """Sequential nearest-candidate association matching OccuSG ObjectNodeManager.

    This class does no class synonym expansion, multi-view fusion, confirmation,
    GT matching, or room anchoring. Every batch is processed in input order, as
    in the pinned manager.
    """

    def __init__(self, config: ObjectAssociationConfig | None = None, *, world_frame: str = "world") -> None:
        self.config = config or ObjectAssociationConfig()
        if type(world_frame) is not str or not world_frame.strip():
            raise ContractError("world_frame must be a non-empty string")
        self.world_frame = world_frame.strip()
        self._objects: list[ObjectNode] = []
        self._next_id = 1

    def snapshot(self) -> tuple[ObjectNode, ...]:
        return tuple(self._objects)

    @staticmethod
    def _normalize(class_name: str | None) -> str | None:
        normalized = class_name.strip().lower() if class_name is not None else ""
        return normalized or None

    @staticmethod
    def _distance(lhs: tuple[float, float, float], rhs: tuple[float, float, float]) -> float:
        return math.dist(lhs, rhs)

    @staticmethod
    def _id_order(object_id: str) -> int:
        return int(object_id.rsplit("-", 1)[1])

    def update(self, batch: DetectionBatch) -> ObjectAssociationResult:
        if not isinstance(batch, DetectionBatch):
            raise ContractError("object association input must be DetectionBatch")
        if batch.detections is None:
            return ObjectAssociationResult("provider_absent", batch.frame_id, batch.timestamp_s,
                                           (), self.snapshot())
        if not batch.detections:
            return ObjectAssociationResult("empty_frame", batch.frame_id, batch.timestamp_s,
                                           (), self.snapshot())

        # Validate atomically, then keep source-order sequential association.
        for detection in batch.detections:
            self._validate_detection(detection)

        associations: list[ObjectAssociation] = []
        new_ids: list[str] = []
        updated_ids: list[str] = []
        for detection in batch.detections:
            label = self._normalize(detection.class_name)
            same_class: list[tuple[float, int, int]] = []
            same_pose: list[tuple[float, int, int]] = []
            suppression_radius = min(self.config.same_class_radius_m,
                                     self.config.same_pose_suppression_cap_m)
            for index, node in enumerate(self._objects):
                if node.layer_id != detection.layer_id:
                    continue
                distance = self._distance(detection.center_W, node.center_W)
                order = self._id_order(node.object_id)
                if distance <= suppression_radius:
                    same_pose.append((distance, order, index))
                old_label = self._normalize(node.class_name)
                if (label is not None and old_label is not None and label == old_label
                        and distance <= self.config.same_class_radius_m):
                    same_class.append((distance, order, index))

            decision: Literal["created", "same_class_match", "same_pose_suppressed"]
            if same_class:
                distance, _, index = min(same_class)
                prior = self._objects[index]
                updated = replace(prior, last_seen_s=detection.timestamp_s,
                                  last_detection_id=detection.detection_id,
                                  last_provider_id=batch.provider_id)
                self._objects[index] = updated
                object_id = prior.object_id
                decision = "same_class_match"
                updated_ids.append(object_id)
            elif same_pose:
                distance, _, index = min(same_pose)
                prior = self._objects[index]
                # Source suppression reuses the old node; its first class, pose,
                # extents and score are deliberately not rewritten.
                self._objects[index] = replace(prior, last_seen_s=detection.timestamp_s,
                                               last_detection_id=detection.detection_id,
                                               last_provider_id=batch.provider_id)
                object_id = prior.object_id
                decision = "same_pose_suppressed"
                updated_ids.append(object_id)
            else:
                object_id = f"object-{self._next_id:06d}"
                self._next_id += 1
                node = ObjectNode(
                    object_id=object_id, class_name=detection.class_name,
                    class_id=detection.class_id, confidence=detection.confidence,
                    center_W=detection.center_W, center_method=detection.center_method,
                    extents_m=detection.extents_m, layer_id=detection.layer_id,
                    first_seen_s=detection.timestamp_s, last_seen_s=detection.timestamp_s,
                    first_detection_id=detection.detection_id,
                    last_detection_id=detection.detection_id,
                    first_provider_id=batch.provider_id, last_provider_id=batch.provider_id,
                )
                self._objects.append(node)
                distance = 0.0
                decision = "created"
                new_ids.append(object_id)

            associations.append(ObjectAssociation(
                detection_id=detection.detection_id, provider_id=batch.provider_id,
                frame_id=batch.frame_id, timestamp_s=detection.timestamp_s,
                object_id=object_id, decision=decision, distance_m=distance,
                normalized_class_name=label,
            ))
        return ObjectAssociationResult("processed", batch.frame_id, batch.timestamp_s,
                                       tuple(associations), self.snapshot(),
                                       tuple(new_ids), tuple(updated_ids))

    def _validate_detection(self, detection: Detection3D) -> None:
        if detection.frame_id != self.world_frame:
            raise ContractError(
                f"detection center must be in {self.world_frame!r}, got {detection.frame_id!r}"
            )

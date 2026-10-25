"""Current-frame P3 and OpenCLIP sessions for T2-B2.

The module owns lifecycle, ordering and packet provenance.  P3 uses the
existing backprojection and clustering primitives from the reference script;
OpenCLIP uses the already pinned loader/crop/normalization helpers.  No
future route manifest is required by ``update_frame``.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

from integration_v2.perception_session import PerceptionSession, PerceptionSessionError


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256_bytes(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


def _finite_array(value: Any, name: str) -> np.ndarray:
    array = np.asarray(value)
    if not np.isfinite(array).all():
        raise ValueError(f"{name} contains non-finite values")
    return array


def _p3_project_current_frame(packet: Mapping[str, Any], profile: Mapping[str, Any]) -> dict[str, Any]:
    """Project one current frame using the locked P3 policy.

    This is intentionally a frame adapter around the same M1 primitives used
    by ``scripts/project_detection_masks_to_3d.py``.  It never reads a route
    manifest or observations from a later frame.
    """
    from standalone_sg.contracts.objects import Detection3D
    from standalone_sg.geometry import backproject_depth
    from scripts.project_detection_masks_to_3d import cluster_components

    rgbd = packet["rgbd"]
    depth = np.asarray(rgbd["depth_m"], dtype=np.float32)
    valid_mask = np.asarray(rgbd["valid_mask"], dtype=bool)
    intrinsics = packet["intrinsics"]
    transform = packet["T_W_Copt"]
    if depth.ndim != 2 or valid_mask.shape != depth.shape:
        raise ValueError("current RGB-D depth/valid_mask shape mismatch")
    if not np.isfinite(depth).all() and np.any(np.isfinite(depth) & valid_mask):
        # NaNs outside the validity mask are legal; finite valid values are
        # checked by backproject_depth below.
        pass
    geometry = profile["geometry"]
    min_points = int(geometry["cluster_min_points"])
    max_points = int(geometry["cluster_max_points"])
    eps = float(geometry["cluster_eps_m"])
    if min_points < 1 or max_points < min_points or not math.isfinite(eps) or eps <= 0:
        raise ValueError("invalid locked P3 geometry profile")
    frame_id = str(packet["frame_id"])
    timestamp = float(packet["timestamp_s"])
    rgb = np.asarray(packet["rgb"])
    results: list[dict[str, Any]] = []
    for det in packet.get("p2_detections", ()):
        det_id = str(det["detection_id"])
        mask = np.asarray(det["mask"], dtype=bool)
        if mask.shape != depth.shape:
            raise ValueError(f"{det_id}: mask shape mismatch")
        selected = valid_mask & mask
        valid_count = int(selected.sum())
        common = {
            "detection_id": det_id, "frame_id": frame_id,
            "frame_index": int(packet["frame_index"]), "timestamp_s": timestamp,
            "class_raw": str(det["class_raw"]),
            "class_canonical": str(det["class_canonical"]),
            "class_id": int(det["class_id"]), "score_raw": float(det["score_raw"]),
            "mask_sha256": str(det["mask_sha256"]),
            "valid_depth_pixels": valid_count,
            "geometry_method": "M1_backproject+source_euclidean_largest_cluster",
            "source_profile": str(profile.get("profile_id", "unknown")),
        }
        if valid_count < min_points:
            results.append({**common, "status": "rejected_insufficient_depth_points",
                            "decision": "rejected", "reason": "rejected_insufficient_depth_points"})
            continue
        masked_depth = np.where(selected, depth, np.nan).astype(np.float32)
        cloud = backproject_depth(
            masked_depth, intrinsics, transform, meters_per_unit=1.0,
            min_depth_m=float(geometry["min_depth_m"]),
            max_depth_m=float(geometry["max_depth_m"]), valid_mask=selected,
            timestamp=timestamp, scan_id=frame_id, stride=1,
        )
        points = np.asarray(cloud.xyz_W, dtype=np.float64)
        components = cluster_components(points, eps)
        ids = components[0] if components else np.empty(0, dtype=np.int64)
        common.update({"backprojected_points": int(len(points)),
                       "cluster_count": int(len(components)),
                       "largest_cluster_points": int(len(ids))})
        if len(ids) < min_points:
            results.append({**common, "status": "rejected_cluster_below_minimum",
                            "decision": "rejected", "reason": "rejected_cluster_below_minimum"})
            continue
        if len(ids) > max_points:
            results.append({**common, "status": "rejected_cluster_above_maximum",
                            "decision": "rejected", "reason": "rejected_cluster_above_maximum"})
            continue
        selected_points = np.asarray(points[ids], dtype=np.float64)
        low, high = selected_points.min(axis=0), selected_points.max(axis=0)
        center = (low + high) / 2.0
        extents = high - low
        detection = Detection3D(
            detection_id=det_id, timestamp_s=timestamp,
            center_W=tuple(float(v) for v in center),
            center_method=str(geometry["center_method"]),
            class_name=str(det["class_canonical"]), class_id=str(det["class_id"]),
            confidence=float(det["score_raw"]), frame_id="world", layer_id="floor_0",
            extents_m=tuple(float(max(0.0, v)) for v in extents),
            provider_metadata={"provider": "p2_yolo11n_seg_onnx+p3_depth",
                               "detection_2d_id": det_id, "class_raw": str(det["class_raw"]),
                               "bbox_xyxy": list(det["bbox_xyxy"]),
                               "valid_depth_pixels": valid_count,
                               "cluster_points": int(len(ids)),
                               "frame_index": int(packet["frame_index"]),
                               "camera_pose_W_Copt": np.asarray(transform.matrix).tolist(),
                               "depth_units": "m", "depth_representation": "axial",
                               "semantic_gt_used": False, "room_gt_used": False},
        )
        results.append({**common, "status": "accepted", "decision": "accepted",
                        "reason": "largest_cluster", "center_W": list(detection.center_W),
                        "extents_m": list(detection.extents_m),
                        "cluster_points": int(len(ids)),
                        "detection_3d": detection})
    return {"frame_id": frame_id, "frame_index": int(packet["frame_index"]),
            "timestamp_s": timestamp,
            "status": "present" if any(r["status"] == "accepted" for r in results) else "empty",
            "detections": results,
            "provenance": {"geometry_method": "standalone_sg.geometry.backproject_depth",
                            "profile_id": str(profile.get("profile_id", "unknown")),
                            "semantic_gt_used": False, "room_gt_used": False}}


class P3GeometrySession:
    """Ordered current-frame P3 adapter; geometry itself is stateless."""

    def __init__(self, *, profile: Mapping[str, Any], projector: Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]] | None = None):
        self.profile = dict(profile)
        self._projector = projector or _p3_project_current_frame
        self._lifecycle = PerceptionSession.open(process_frame=self._process)

    @classmethod
    def open(cls, *, profile: Mapping[str, Any], projector=None) -> "P3GeometrySession":
        return cls(profile=profile, projector=projector)

    def _process(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        if "rgbd" not in packet or "p2_detections" not in packet:
            raise PerceptionSessionError("P3 packet requires rgbd and p2_detections")
        return self._projector(packet, self.profile)

    def update_frame(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        return self._lifecycle.update_frame(packet)

    def finalize(self) -> None:
        self._lifecycle.finalize()

    def close(self) -> None:
        self._lifecycle.close()

    @property
    def processed_frame_ids(self) -> tuple[str, ...]:
        return self._lifecycle.processed_frame_ids


@dataclass(frozen=True)
class FeatureContract:
    checkpoint_sha256: str
    model_fingerprint: str
    tokenizer_id: str
    preprocessing_id: str
    prompt_policy: str

    @property
    def cache_prefix(self) -> str:
        return canonical_hash(self.__dict__)


class OpenCLIPFeatureSession:
    """One OpenCLIP model for image and raw-class text features."""

    def __init__(self, *, checkpoint: Path, contract: FeatureContract,
                 image_encoder: Callable[[np.ndarray, list[float]], np.ndarray] | None = None,
                 text_encoder: Callable[[str], tuple[np.ndarray, list[int]]] | None = None,
                 model: Any = None, preprocess: Any = None, torch_module: Any = None,
                 tokenizer: Any = None):
        self.checkpoint = checkpoint
        self.contract = contract
        self._image_encoder = image_encoder
        self._text_encoder = text_encoder
        self._model = model
        self._preprocess = preprocess
        self._torch = torch_module
        self._tokenizer = tokenizer
        self._text_cache: dict[str, dict[str, Any]] = {}
        self.image_encode_count = 0
        self.text_encode_count = 0
        self.model_load_count = 0 if model is None else 1
        self._lifecycle = PerceptionSession.open(process_frame=self._process, resources=(self,))
        self._closed = False

    @classmethod
    def open(cls, *, checkpoint: Path, contract: FeatureContract,
             image_encoder=None, text_encoder=None) -> "OpenCLIPFeatureSession":
        if image_encoder is not None or text_encoder is not None:
            return cls(checkpoint=checkpoint, contract=contract,
                       image_encoder=image_encoder, text_encoder=text_encoder)
        import open_clip
        import torch
        import sys
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        import extract_u2_appearance as u2
        model, preprocess, torch_module = u2.load_model(checkpoint)
        tokenizer = open_clip.get_tokenizer("ViT-B-32")
        return cls(checkpoint=checkpoint, contract=contract, model=model,
                   preprocess=preprocess, torch_module=torch_module, tokenizer=tokenizer)

    def _encode_image_real(self, rgb: np.ndarray, bbox: list[float]) -> tuple[np.ndarray, list[float]]:
        import sys
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        import extract_u2_appearance as u2
        row = {"rgb": rgb, "bbox_xyxy": bbox}
        crop, crop_box = u2.make_crop(row)
        vec = u2.encode_crop(self._model, self._preprocess, self._torch,
                             np.asarray(crop, dtype=np.uint8))
        return vec, crop_box

    def _encode_text_real(self, text: str) -> tuple[np.ndarray, list[int]]:
        with self._torch.inference_mode():
            tokens = self._tokenizer([text]).to("cpu")
            vec = self._model.encode_text(tokens)
            vec = vec / vec.norm(dim=-1, keepdim=True)
        array = vec[0].detach().cpu().numpy().astype(np.float32, copy=False)
        if array.shape != (512,) or not np.isfinite(array).all() or not np.isclose(np.linalg.norm(array), 1.0, atol=1e-5, rtol=1e-5):
            raise ValueError(f"invalid text feature for {text!r}")
        return array, tokens[0].detach().cpu().tolist()

    def _text_key(self, raw_class: str) -> str:
        return canonical_hash({"contract": self.contract.__dict__, "encoded_string": raw_class})

    def _process(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        rgb = np.asarray(packet["rgb"])
        rows = []
        for det in packet.get("p2_detections", ()):
            obs_id = str(det["detection_id"])
            if self._image_encoder is not None:
                image_vec = np.asarray(self._image_encoder(rgb, list(det["bbox_xyxy"])), dtype=np.float32)
                crop_box = list(det["bbox_xyxy"])
            else:
                image_vec, crop_box = self._encode_image_real(rgb, list(det["bbox_xyxy"]))
            if image_vec.shape != (512,) or not np.isfinite(image_vec).all() or not np.isclose(np.linalg.norm(image_vec), 1.0, atol=1e-5, rtol=1e-5):
                raise ValueError(f"invalid image feature for {obs_id}")
            self.image_encode_count += 1
            raw = str(det["class_raw"])
            key = self._text_key(raw)
            cached = self._text_cache.get(key)
            if cached is None:
                if self._text_encoder is not None:
                    text_vec, token_ids = self._text_encoder(raw)
                else:
                    text_vec, token_ids = self._encode_text_real(raw)
                text_vec = np.asarray(text_vec, dtype=np.float32)
                if text_vec.shape != (512,) or not np.isfinite(text_vec).all() or not np.isclose(np.linalg.norm(text_vec), 1.0, atol=1e-5, rtol=1e-5):
                    raise ValueError(f"invalid text feature for {raw!r}")
                cached = {"raw_class": raw, "cache_key": key, "token_ids": list(token_ids),
                          "vector": text_vec.copy(), "encoded_string": raw}
                self._text_cache[key] = cached
                self.text_encode_count += 1
            rows.append({"observation_id": obs_id, "frame_id": str(packet["frame_id"]),
                         "frame_index": int(packet["frame_index"]), "class_raw": raw,
                         "class_canonical": str(det["class_canonical"]),
                         "class_id": int(det["class_id"]),
                         "image_feature": image_vec, "image_feature_shape": [512],
                         "image_feature_norm": float(np.linalg.norm(image_vec)),
                         "crop_box_xyxy_float": crop_box,
                         "text_feature": np.asarray(cached["vector"], dtype=np.float32),
                         "text_cache_key": key, "text_encoded_string": raw,
                         "text_token_ids": list(cached["token_ids"]),
                         "feature_contract": self.contract.__dict__})
        return {"frame_id": str(packet["frame_id"]), "frame_index": int(packet["frame_index"]),
                "status": "present" if rows else "empty", "rows": rows}

    def update_frame(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        return self._lifecycle.update_frame(packet)

    def finalize(self) -> None:
        self._lifecycle.finalize()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._model = None
        self._preprocess = None
        self._tokenizer = None
        self._torch = None
        self._lifecycle.close()

    @property
    def text_cache_size(self) -> int:
        return len(self._text_cache)


def validate_join(frame_packet: Mapping[str, Any], p3_result: Mapping[str, Any],
                  feature_result: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a ready current-frame join without silently dropping rows."""
    frame_id = str(frame_packet["frame_id"])
    if str(p3_result["frame_id"]) != frame_id or str(feature_result["frame_id"]) != frame_id:
        raise ValueError("packet join frame_id mismatch")
    expected = {str(d["detection_id"]) for d in frame_packet.get("p2_detections", ())}
    p3_ids = {str(d["detection_id"]) for d in p3_result.get("detections", ())}
    feature_ids = {str(d["observation_id"]) for d in feature_result.get("rows", ())}
    if feature_ids != expected:
        raise ValueError("feature output does not cover every P2 observation")
    if not p3_ids <= expected:
        raise ValueError("P3 output contains an unknown observation")
    source_hashes = frame_packet.get("source_hashes", {})
    if not isinstance(source_hashes, Mapping) or not source_hashes:
        raise ValueError("ready packet requires source hashes")
    return {"frame_id": frame_id, "frame_index": int(frame_packet["frame_index"]),
            "status": "ready", "p2_observation_count": len(expected),
            "p3_accepted_count": sum(d.get("status") == "accepted" for d in p3_result.get("detections", [])),
            "p3_rejected_count": sum(d.get("status") != "accepted" for d in p3_result.get("detections", [])),
            "feature_count": len(feature_ids), "source_hashes": dict(source_hashes)}


def build_conceptgraphs_input(frame_packet: Mapping[str, Any], p3_result: Mapping[str, Any],
                              feature_result: Mapping[str, Any]) -> dict[str, Any]:
    """Build the current-frame ConceptGraphs adapter packet.

    Every P2 observation remains present.  P3 status is attached as evidence
    and never used to filter the ConceptGraphs population.
    """
    validate_join(frame_packet, p3_result, feature_result)
    p3_by_id = {str(row["detection_id"]): row for row in p3_result.get("detections", [])}
    features_by_id = {str(row["observation_id"]): row for row in feature_result.get("rows", [])}
    observations = []
    for det in frame_packet.get("p2_detections", ()):
        oid = str(det["detection_id"])
        feature = features_by_id[oid]
        observations.append({
            "observation_id": oid, "class_raw": str(det["class_raw"]),
            "class_canonical": str(det["class_canonical"]), "class_id": int(det["class_id"]),
            "score_raw": float(det["score_raw"]), "mask": det["mask"],
            "mask_sha256": str(det["mask_sha256"]), "bbox_xyxy": list(det["bbox_xyxy"]),
            "p3": p3_by_id.get(oid, {"status": "not_returned"}),
            "image_feature": np.asarray(feature["image_feature"], dtype=np.float32),
            "text_feature": np.asarray(feature["text_feature"], dtype=np.float32),
            "text_cache_key": str(feature["text_cache_key"]),
            "text_encoded_string": str(feature["text_encoded_string"]),
        })
    return {"schema": "conceptgraphs-current-frame-input/v1",
            "frame_id": str(frame_packet["frame_id"]),
            "frame_index": int(frame_packet["frame_index"]),
            "timestamp_s": float(frame_packet["timestamp_s"]),
            "rgb": frame_packet["rgb"], "rgbd": frame_packet["rgbd"],
            "intrinsics": frame_packet["intrinsics"], "T_W_Copt": frame_packet["T_W_Copt"],
            "source_hashes": dict(frame_packet["source_hashes"]),
            "observations": observations, "future_manifest_required": False}


def build_occusg_frame_packet(frame_packet: Mapping[str, Any], p3_result: Mapping[str, Any], *,
                              scene_id: str, sequence_id: str):
    """Adapt one joined frame to the existing OccuSG ``FramePacket`` contract."""
    from standalone_sg.contracts.frames import RGBDFrame
    from standalone_sg.contracts.objects import DetectionBatch
    from standalone_sg.pipeline import FramePacket

    rgbd_data = frame_packet["rgbd"]
    rgbd = RGBDFrame(depth_m=np.asarray(rgbd_data["depth_m"], dtype=np.float32),
                     intrinsics=frame_packet["intrinsics"], T_W_Copt=frame_packet["T_W_Copt"],
                     timestamp=float(frame_packet["timestamp_s"]),
                     rgb=np.asarray(frame_packet["rgb"], dtype=np.uint8),
                     valid_mask=np.asarray(rgbd_data["valid_mask"], dtype=bool),
                     frame_id=str(frame_packet["frame_id"]))
    accepted = tuple(row["detection_3d"] for row in p3_result.get("detections", [])
                     if row.get("status") == "accepted" and row.get("detection_3d") is not None)
    batch = DetectionBatch(provider_id="t2_b2_p3_current_frame",
                           frame_id=str(frame_packet["frame_id"]),
                           timestamp_s=float(frame_packet["timestamp_s"]),
                           detections=accepted,
                           provenance={"p3_status": str(p3_result.get("status", "empty")),
                                       "scope_semantics": "unavailable"})
    base = frame_packet.get("base_pose_W")
    return FramePacket(scene_id=str(scene_id), sequence_id=str(sequence_id),
                       frame_id=str(frame_packet["frame_id"]),
                       frame_index=int(frame_packet["frame_index"]),
                       timestamp_s=float(frame_packet["timestamp_s"]), input_mode="rgbd",
                       base_pose_W=tuple(float(x) for x in base) if base is not None else None,
                       detections=batch, rgbd=rgbd,
                       source_metadata={"source_hashes": dict(frame_packet["source_hashes"]),
                                        "future_manifest_required": False,
                                        "scope_semantics": "unavailable"})

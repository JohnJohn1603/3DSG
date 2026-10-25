from __future__ import annotations

import numpy as np
import pytest
import json
import shutil
from argparse import Namespace
from pathlib import Path

from scripts.run_t2_b2_perception_smoke import checkpoint_digest
from scripts import run_t2_b2_perception_smoke as smoke_runner

from integration_v2.perception_feature_sessions import (
    FeatureContract,
    OpenCLIPFeatureSession,
    P3GeometrySession,
    build_conceptgraphs_input,
    build_occusg_frame_packet,
    validate_join,
)
from integration_v2.perception_session import DuplicateOrStalePerceptionFrame


def packet(index=0, frame_id=None, detections=None):
    return {
        "frame_index": index, "frame_id": frame_id or f"f{index}", "timestamp_s": float(index),
        "rgb": np.zeros((2, 2, 3), dtype=np.uint8),
        "rgbd": {"depth_m": np.ones((2, 2), dtype=np.float32), "valid_mask": np.ones((2, 2), dtype=bool)},
        "p2_detections": detections or [], "source_hashes": {"rgb": f"sha-{index}"},
    }


def detection(name="f0:det-0000", raw="couch"):
    return {"detection_id": name, "class_id": 1, "class_raw": raw,
            "class_canonical": "sofa", "score_raw": .9,
            "bbox_xyxy": [0., 0., 2., 2.], "mask": np.ones((2, 2), dtype=bool),
            "mask_sha256": f"mask-{name}"}


def test_p3_session_rejects_duplicate_before_projector():
    calls = []
    session = P3GeometrySession.open(profile={"geometry": {}}, projector=lambda p, _: calls.append(p) or {"frame_id": p["frame_id"], "detections": []})
    session.update_frame(packet())
    with pytest.raises(DuplicateOrStalePerceptionFrame):
        session.update_frame(packet(frame_id="f0"))
    assert len(calls) == 1
    session.close()


def test_feature_session_text_cache_and_empty_frame():
    image_calls, text_calls = [], []
    vec = np.zeros(512, dtype=np.float32); vec[0] = 1.0

    def image(rgb, bbox):
        image_calls.append(1); return vec

    def text(raw):
        text_calls.append(raw); return vec, [1, 2]

    session = OpenCLIPFeatureSession.open(
        checkpoint=__import__("pathlib").Path("unused"),
        contract=FeatureContract("c", "m", "tok", "pre", "raw"),
        image_encoder=image, text_encoder=text,
    )
    d = detection()
    first = session.update_frame(packet(detections=[d]))
    second = session.update_frame(packet(index=1, detections=[{**d, "detection_id": "f1:det-0000"}]))
    empty = session.update_frame(packet(index=2, detections=[]))
    assert first["status"] == second["status"] == "present"
    assert empty["status"] == "empty"
    assert len(image_calls) == 2
    assert text_calls == ["couch"]
    assert session.text_cache_size == 1
    session.close()


def test_join_requires_all_p2_features_and_preserves_p3_rejection():
    p = packet(detections=[detection()])
    p3 = {"frame_id": "f0", "detections": [{"detection_id": "f0:det-0000", "status": "rejected_cluster_below_minimum"}]}
    features = {"frame_id": "f0", "rows": [{"observation_id": "f0:det-0000"}]}
    out = validate_join(p, p3, features)
    assert out["status"] == "ready"
    assert out["p3_rejected_count"] == 1
    with pytest.raises(ValueError, match="cover every P2"):
        validate_join(p, p3, {"frame_id": "f0", "rows": []})


def test_consumer_adapters_accept_empty_current_frame_without_future_state():
    from standalone_sg.contracts.frames import CameraIntrinsics, Transform
    p = packet(detections=[])
    p["intrinsics"] = CameraIntrinsics(2, 2, 1.0, 1.0, .5, .5)
    p["T_W_Copt"] = Transform.identity()
    p["base_pose_W"] = (0.0, 0.0, 0.0)
    p3 = {"frame_id": "f0", "detections": [], "status": "empty"}
    features = {"frame_id": "f0", "rows": []}
    cg = build_conceptgraphs_input(p, p3, features)
    occu = build_occusg_frame_packet(p, p3, scene_id="scene", sequence_id="seq")
    assert cg["future_manifest_required"] is False
    assert cg["observations"] == []
    assert occu.input_mode == "rgbd"
    assert occu.detections.status == "empty"


def test_checkpoint_metadata_accepts_sha256_and_rejects_conflict(tmp_path):
    checkpoint = tmp_path / "weights.bin"
    checkpoint.write_bytes(b"locked")
    import hashlib
    digest = hashlib.sha256(b"locked").hexdigest()
    assert checkpoint_digest({"sha256": digest}, checkpoint) == (digest, digest)
    with pytest.raises(ValueError, match="disagree"):
        checkpoint_digest({"sha256": digest, "checkpoint_sha256": "0" * 64}, checkpoint)


def test_resolve_context_uses_real_artifact_schema_and_requires_fingerprint(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    capture = root / "artifacts/conceptgraphs_hybrid_runs/hm3d00802_main29_640_quality_v1_attempt2/capture"
    p2 = root / "artifacts/t2_b_perception_smoke_20261018_v1/p2/detection_2d_index.json"
    subset = root / "artifacts/t2_b_perception_smoke_20261018_v1/subset_manifest.json"
    profile = root / "configs/perception/yolo11n_seg_replica_cad_dev.json"
    checkpoint = root / "artifacts/models/openclip/vit-b-32-openai-a6f597a30f7b82c51704746581f9a4e41421e878/open_clip_model.safetensors"
    checkpoint_report = checkpoint.parent / "checkpoint_metadata.json"
    reference = root / "artifacts/conceptgraphs_hybrid_runs/hm3d00802_main29_640_quality_v1_attempt2"
    output = tmp_path / "prep"
    output.mkdir()
    shutil.copy(root / "artifacts/t2_b2_perception_smoke_20261018_v2/smoke_lock.json", output / "smoke_lock.json")
    args = Namespace(capture=capture, p2_index=p2, subset_manifest=subset, profile=profile,
                     checkpoint=checkpoint, checkpoint_report=checkpoint_report,
                     reference_run=reference)
    cap = json.loads((capture / "capture_manifest.json").read_text())
    p2_data = json.loads(p2.read_text()); subset_data = json.loads(subset.read_text())
    meta = json.loads(checkpoint_report.read_text())
    context = smoke_runner.resolve_context(args, capture=cap, p2=p2_data,
                                           subset=subset_data, checkpoint_meta=meta, output=output)
    assert context["status"] == "pass"
    assert context["model_fingerprint"]
    assert context["feature_contract"]["checkpoint_sha256"] == meta["sha256"]

    original_read = smoke_runner.read_json
    def missing_fingerprint(path):
        value = original_read(path)
        if Path(path).name == "feature_extraction_report.json":
            value.pop("model_fingerprint", None)
        return value
    monkeypatch.setattr(smoke_runner, "read_json", missing_fingerprint)
    with pytest.raises(ValueError, match="model_fingerprint"):
        smoke_runner.resolve_context(args, capture=cap, p2=p2_data,
                                     subset=subset_data, checkpoint_meta=meta, output=output)

#!/usr/bin/env python3
"""Run the bounded T2-B2 P3 + OpenCLIP current-frame smoke.

P2 is read from an already verified cache.  This command does not run the
detector, capture, mapping, OccuSG or Qwen.  Each frame is handed to the
session APIs independently; output is published only after the complete join
has validated IDs and source hashes.
"""
from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))

from adapters.habitat.frames import frame_from_capture_record  # noqa: E402
from integration_v2.perception_feature_sessions import (  # noqa: E402
    FeatureContract, OpenCLIPFeatureSession, P3GeometrySession,
    sha256_file, validate_join,
)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_vector(path: Path, *, image: np.ndarray, text: np.ndarray) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, image_feature=np.asarray(image, dtype=np.float32),
                        text_feature=np.asarray(text, dtype=np.float32))
    return sha256_file(path)


def checkpoint_digest(metadata: dict, checkpoint: Path) -> tuple[str, str]:
    """Resolve the pinned digest and reject contradictory metadata schemas."""
    candidates = {key: str(metadata[key]) for key in ("sha256", "checkpoint_sha256") if metadata.get(key)}
    if not candidates:
        raise ValueError("checkpoint metadata must contain sha256 or checkpoint_sha256")
    if len(set(candidates.values())) != 1:
        raise ValueError(f"checkpoint metadata digests disagree: {candidates}")
    expected = next(iter(candidates.values()))
    actual = sha256_file(checkpoint)
    if actual != expected:
        raise ValueError(f"checkpoint SHA-256 mismatch: expected {expected}, actual {actual}")
    return expected, actual


def resolve_context(args: argparse.Namespace, *, capture: dict, p2: dict, subset: dict,
                    checkpoint_meta: dict, output: Path) -> dict:
    """Resolve every required metadata field before any model load."""
    paths = {
        "capture_manifest": args.capture.resolve() / "capture_manifest.json",
        "p2_index": args.p2_index.resolve(), "subset_manifest": args.subset_manifest.resolve(),
        "profile": args.profile.resolve(), "checkpoint": args.checkpoint.resolve(),
        "checkpoint_report": args.checkpoint_report.resolve(),
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("preflight missing: " + ", ".join(missing))
    lock_path = output / "smoke_lock.json"
    if not lock_path.is_file():
        raise ValueError("preflight requires smoke_lock.json written before execution")
    lock = read_json(lock_path)
    if lock.get("status") != "locked_before_smoke":
        raise ValueError("smoke lock is not marked locked_before_smoke")
    expected_digest, actual_digest = checkpoint_digest(checkpoint_meta, paths["checkpoint"])
    cap_sha = sha256_file(paths["capture_manifest"])
    p2_sha = sha256_file(paths["p2_index"])
    subset_sha = sha256_file(paths["subset_manifest"])
    profile_sha = sha256_file(paths["profile"])
    locked = lock.get("inputs", {})
    for name, digest in (("capture_manifest", cap_sha), ("p2_index", p2_sha),
                         ("subset_manifest", subset_sha), ("profile", profile_sha),
                         ("checkpoint", actual_digest)):
        if locked.get(name, {}).get("sha256") != digest:
            raise ValueError(f"smoke lock hash mismatch: {name}")
    if p2.get("status") != "pass" or p2.get("capture_manifest_sha256") != cap_sha:
        raise ValueError("P2 source/capture hash contract mismatch")
    if subset.get("resolution_px") != capture.get("resolution_px"):
        raise ValueError("subset resolution differs from capture")
    subset_ids = [str(x) for x in subset.get("frame_ids", [])]
    if not subset_ids or any(fid not in {str(x["frame_id"]) for x in capture["frames"]} for fid in subset_ids):
        raise ValueError("subset frame IDs are not present in capture")
    p2_frames = {str(x["frame_id"]): x for x in p2.get("frames", [])}
    if any(fid not in p2_frames for fid in subset_ids):
        raise ValueError("subset frame IDs are not present in P2")
    raw_classes = sorted({str(d["class_raw"]) for fid in subset_ids for d in p2_frames[fid].get("detections", [])})
    if any(not value.strip() for value in raw_classes):
        raise ValueError("P2 contains an empty raw class string")
    reference_root = args.reference_run.resolve()
    refs = {
        "p3": reference_root / "detection_3d/detection_3d.jsonl",
        "p3_manifest": reference_root / "detection_3d/detection_3d_manifest.json",
        "image_manifest": reference_root / "image_features/feature_manifest.jsonl",
        "image_report": reference_root / "image_features/feature_extraction_report.json",
        "text_manifest": reference_root / "text_features/manifest.json",
        "text_contract": reference_root / "text_features/experiment_contract.json",
    }
    missing_refs = [str(path) for path in refs.values() if not path.is_file()]
    if missing_refs:
        raise FileNotFoundError("preflight reference artifact missing: " + ", ".join(missing_refs))
    image_report = read_json(refs["image_report"])
    text_manifest = read_json(refs["text_manifest"])
    text_contract = read_json(refs["text_contract"])
    image_rows = [json.loads(line) for line in refs["image_manifest"].read_text(encoding="utf-8").splitlines() if line.strip()]
    if not image_rows:
        raise ValueError("image feature reference manifest is empty")
    if image_report.get("checkpoint_sha256") != expected_digest:
        raise ValueError("image feature reference checkpoint digest differs")
    if text_manifest.get("checkpoint_sha256") != expected_digest:
        raise ValueError("text feature reference checkpoint digest differs")
    model_fingerprint = image_report.get("model_fingerprint")
    if not isinstance(model_fingerprint, str) or not model_fingerprint:
        raise ValueError("image feature provenance lacks model_fingerprint")
    model_info = text_contract.get("model", {})
    if model_info.get("checkpoint_sha256") != expected_digest:
        raise ValueError("text model contract checkpoint digest differs")
    if checkpoint_meta.get("revision") and model_info.get("checkpoint_revision") != checkpoint_meta.get("revision"):
        raise ValueError("checkpoint revision differs between checkpoint and text contract")
    if image_report.get("feature_dimension") != 512:
        raise ValueError("image feature output dimension contract is not 512")
    image_row = image_rows[0]
    if image_row.get("checkpoint_sha256") != expected_digest or image_row.get("model_fingerprint") != model_fingerprint:
        raise ValueError("image feature row provenance disagrees with resolved checkpoint/model fingerprint")
    if image_row.get("preprocessing_id") != image_report.get("preprocessing_id"):
        raise ValueError("image feature row preprocessing differs from image provenance")
    if text_manifest.get("embedding_shape", [None, None])[-1] != 512:
        raise ValueError("text feature output dimension contract is not 512")
    tokenizer = text_contract.get("reference_equivalence", {}).get("reference_call", "")
    if "tokenizer" not in tokenizer or "encode_text" not in tokenizer:
        raise ValueError("text reference tokenizer/encoding contract is missing")
    context = {
        "status": "pass", "checkpoint_sha256_expected": expected_digest,
        "checkpoint_sha256_actual": actual_digest, "capture_manifest_sha256": cap_sha,
        "p2_index_sha256": p2_sha, "subset_manifest_sha256": subset_sha,
        "profile_sha256": profile_sha, "subset_frame_ids": subset_ids,
        "raw_class_strings": raw_classes,
        "canonicalization_policy": "raw_class and canonical_class are stored separately; raw string is encoded",
        "text_prompt_policy": "exact raw class string; no room-belief alias",
        "tokenizer_contract": tokenizer,
        "model_fingerprint": model_fingerprint,
        "model": model_info,
        "image_preprocessing_id": image_report.get("preprocessing_id"),
        "reference_paths": {key: str(path) for key, path in refs.items()},
        "reference_hashes": {key: sha256_file(path) for key, path in refs.items()},
        "tolerance": read_json(output / "smoke_lock.json").get("tolerance", {}),
        "feature_contract": {
            "checkpoint_sha256": expected_digest, "model_fingerprint": model_fingerprint,
            "tokenizer_id": "open_clip.get_tokenizer('ViT-B-32')",
            "preprocessing_id": str(image_report.get("preprocessing_id")),
            "prompt_policy": "exact raw class string; no room alias/canonical rewrite",
        },
        "output_contract": {
            "image_feature_shape": [512], "text_feature_shape": [512],
            "p3_rejected_preserved_for_conceptgraphs": True,
            "empty_detection_valid": True, "future_manifest_required": False,
        },
        "model_loaded": False,
    }
    return context


def preflight(args: argparse.Namespace, *, capture: dict, p2: dict, subset: dict,
              checkpoint_meta: dict, output: Path) -> dict:
    """Validate and persist one complete model-load context."""
    context = resolve_context(args, capture=capture, p2=p2, subset=subset,
                              checkpoint_meta=checkpoint_meta, output=output)
    (output / "preflight.json").write_text(
        json.dumps(context, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return context


def detection_rows(frame: dict, p2_root: Path) -> list[dict]:
    rows = []
    for det in frame.get("detections", []):
        mask_path = p2_root / str(det["mask_file"])
        if not mask_path.is_file():
            raise FileNotFoundError(mask_path)
        mask = np.asarray(np.load(mask_path, allow_pickle=False), dtype=bool)
        rows.append({**det, "mask": mask, "mask_path": str(mask_path),
                     "mask_file": str(det["mask_file"])})
    return rows


def run(args: argparse.Namespace) -> dict:
    started = time.perf_counter()
    capture_root = args.capture.resolve(); p2_root = args.p2_index.resolve().parent
    output = args.output.resolve()
    if output.exists():
        allowed = {"smoke_lock.json", "preflight.json"}
        unexpected = [p.name for p in output.iterdir() if p.name not in allowed]
        if unexpected:
            raise FileExistsError(f"output must be new or contain only smoke_lock.json: {output} ({unexpected})")
    else:
        output.mkdir(parents=True)
    (output / "features").mkdir()

    capture = read_json(capture_root / "capture_manifest.json")
    p2 = read_json(args.p2_index.resolve())
    profile = read_json(args.profile.resolve())
    subset = read_json(args.subset_manifest.resolve())
    subset_ids = [str(x) for x in subset["frame_ids"]]
    cap_by_id = {str(x["frame_id"]): x for x in capture["frames"]}
    p2_by_id = {str(x["frame_id"]): x for x in p2["frames"]}
    if any(fid not in cap_by_id or fid not in p2_by_id for fid in subset_ids):
        raise ValueError("subset contains a frame absent from capture/P2")
    if subset.get("resolution_px") != capture.get("resolution_px"):
        raise ValueError("subset resolution differs from capture")
    checkpoint = args.checkpoint.resolve()
    checkpoint_meta = read_json(args.checkpoint_report.resolve())
    preflight_result = preflight(args, capture=capture, p2=p2, subset=subset,
                                 checkpoint_meta=checkpoint_meta, output=output)
    contract = FeatureContract(**preflight_result["feature_contract"])
    p3_session = P3GeometrySession.open(profile=profile)
    clip_session = OpenCLIPFeatureSession.open(checkpoint=checkpoint, contract=contract)
    rows = []
    frame_timings = []
    status = "failed"
    error = None
    try:
        for ordinal, fid in enumerate(subset_ids):
            frame_start = time.perf_counter()
            record = cap_by_id[fid]
            _rgbd, habitat_packet = frame_from_capture_record(record, capture_root)
            rgbd = habitat_packet.rgbd
            detections = detection_rows(p2_by_id[fid], p2_root)
            # The session packet contains only this frame and its own hashes.
            packet = {
                "frame_index": int(record["frame_index"]), "frame_id": fid,
                "timestamp_s": float(record["timestamp_s"]), "rgb": np.asarray(rgbd.rgb),
                "rgbd": {"depth_m": np.asarray(rgbd.depth_m), "valid_mask": np.asarray(rgbd.valid_mask, dtype=bool),
                         "depth_units": "m", "depth_representation": "axial"},
                "intrinsics": rgbd.intrinsics, "T_W_Copt": rgbd.T_W_Copt,
                "p2_detections": detections,
                "source_hashes": {
                    "capture_manifest_sha256": sha256_file(capture_root / "capture_manifest.json"),
                    "p2_index_sha256": sha256_file(args.p2_index.resolve()),
                    "rgb.npy": record.get("raw_files_sha256", {}).get("rgb.npy"),
                    "depth_m.npy": record.get("raw_files_sha256", {}).get("depth_m.npy"),
                    "valid_mask.npy": record.get("raw_files_sha256", {}).get("valid_mask.npy"),
                    "metadata.json": record.get("raw_files_sha256", {}).get("metadata.json"),
                    "capture_sensor_contract_sha256": str(p2.get("capture_sensor_contract_sha256", "")),
                },
            }
            p3_result = p3_session.update_frame(packet)
            feature_result = clip_session.update_frame(packet)
            join = validate_join(packet, p3_result, feature_result)
            feature_refs = []
            for feature in feature_result["rows"]:
                obs = str(feature["observation_id"])
                path = output / "features" / (obs.replace(":", "__") + ".npz")
                file_sha = save_vector(path, image=feature["image_feature"], text=feature["text_feature"])
                feature_refs.append({"observation_id": obs, "path": str(path.relative_to(ROOT)),
                                     "sha256": file_sha, "image_shape": [512], "text_shape": [512],
                                     "mask_sha256": next(d["mask_sha256"] for d in detections if d["detection_id"] == obs),
                                     "class_raw": feature["class_raw"], "class_canonical": feature["class_canonical"],
                                     "text_cache_key": feature["text_cache_key"],
                                     "text_encoded_string": feature["text_encoded_string"]})
            serial_p3 = []
            for det in p3_result["detections"]:
                item = {k: v for k, v in det.items() if k != "detection_3d"}
                obj = det.get("detection_3d")
                if obj is not None:
                    item["detection_3d"] = {"detection_id": obj.detection_id, "center_W": list(obj.center_W),
                                            "extents_m": list(obj.extents_m), "class_name": obj.class_name,
                                            "class_id": obj.class_id, "confidence": obj.confidence,
                                            "provider_metadata": dict(obj.provider_metadata)}
                serial_p3.append(item)
            rows.append({"frame_id": fid, "frame_index": int(record["frame_index"]),
                         "timestamp_s": float(record["timestamp_s"]), "status": "ready",
                         "p3": {"status": p3_result["status"], "detections": serial_p3},
                         "features": feature_refs, "join": join,
                         "consumer_adapters": {
                             "conceptgraphs": {"status": "ready", "p2_observations": len(detections),
                                                "p3_rejected_preserved": True, "rgbd_pose_features": True},
                             "occusg": {"status": "ready", "frame_packet": "FramePacket-compatible RGBD + P3 batch",
                                         "scope_semantics": "unavailable"},
                         },
                         "future_manifest_required": False,
                         "elapsed_s": time.perf_counter() - frame_start})
            print(json.dumps({"stage": "t2_b2", "frame": f"{ordinal + 1}/{len(subset_ids)}", "frame_id": fid,
                              "p2": len(detections), "p3_accepted": join["p3_accepted_count"],
                              "features": join["feature_count"], "elapsed_s": round(rows[-1]["elapsed_s"], 3)}, ensure_ascii=False), flush=True)
        p3_session.finalize(); clip_session.finalize()
        status = "complete"
    except BaseException as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        try:
            p3_session.close()
        finally:
            clip_session.close()
    manifest = {
        "schema": "t2-b2-perception-frame-join/v1", "status": status,
        "execution_mode": "current_frame_session_smoke", "future_manifest_required": False,
        "capture_manifest_sha256": sha256_file(capture_root / "capture_manifest.json"),
        "p2_index_sha256": sha256_file(args.p2_index.resolve()),
        "subset_manifest_sha256": sha256_file(args.subset_manifest.resolve()),
        "profile_sha256": sha256_file(args.profile.resolve()), "checkpoint_sha256": sha256_file(checkpoint),
        "checkpoint_contract": contract.__dict__, "preflight": preflight_result, "frames": rows,
        "p2_detector_rerun": False, "capture_rerun": False, "mapping_rerun": False,
        "occusg_rerun": False, "qwen_rerun": False,
        "model_load_counts": {"p2": 0, "openclip_image_text_shared": clip_session.model_load_count},
        "encode_counts": {"image": clip_session.image_encode_count, "text": clip_session.text_encode_count,
                           "text_cache_size": clip_session.text_cache_size},
        "timing": {"total_s": time.perf_counter() - started,
                   "per_frame_s": [float(x["elapsed_s"]) for x in rows],
                   "cold_start_s": "captured in model_load not separately instrumented",
                   "memory_peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0},
        "error": error,
    }
    (output / "packet_join_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--capture", type=Path, required=True)
    p.add_argument("--p2-index", type=Path, required=True)
    p.add_argument("--subset-manifest", type=Path, required=True)
    p.add_argument("--profile", type=Path, required=True)
    p.add_argument("--checkpoint", type=Path, required=True)
    p.add_argument("--checkpoint-report", type=Path, required=True)
    p.add_argument("--reference-run", type=Path, required=True,
                   help="locked full C2 run containing P3/image/text reference artifacts")
    p.add_argument("--preflight-only", action="store_true",
                   help="validate artifacts and write preflight.json without loading a model")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.preflight_only:
        output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
        capture = read_json(args.capture.resolve() / "capture_manifest.json")
        p2 = read_json(args.p2_index.resolve())
        subset = read_json(args.subset_manifest.resolve())
        meta = read_json(args.checkpoint_report.resolve())
        result = preflight(args, capture=capture, p2=p2, subset=subset,
                           checkpoint_meta=meta, output=output)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    result = run(args)
    print(json.dumps({"status": result["status"], "output": str(args.output.resolve()),
                      "frames": len(result["frames"])}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run the pinned ConceptGraphs mapping core on already cached local perception.

This is a hybrid cached-perception experiment, not the full ConceptGraphs-Detect
recipe. Reference mapping, text fusion, and end-of-run postprocessing are reused.
"""
from __future__ import annotations

import argparse, ast, copy, gzip, hashlib, json, os, pickle, random, resource, sys, time
import binascii, struct, zlib
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "ref_ConceptGraph/concept-graphs"
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT))


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def peak_rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def seed_mapping_rngs(seed: int):
    """Seed RNGs used by the CPU mapping path before calling reference code."""
    import torch
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    return {"seed": int(seed), "python_random": "random.seed(seed)",
            "numpy": "numpy.random.seed(seed)", "torch": "torch.manual_seed(seed), CPU mapping only",
            "cuda_rng": "not used"}


def reference_import_bridge() -> dict:
    """Lazy-import only from_intrinsics_matrix, avoiding unused GradSLAM imports.

    The AST-extracted function body is byte-for-byte the pinned reference
    function. This only supplies the exact scalar helper used by that function.
    """
    if str(REF) not in sys.path: sys.path.insert(0, str(REF))
    import torch
    from conceptgraph.utils.general_utils import to_scalar
    source = REF / "conceptgraph/dataset/datasets_common.py"
    tree = ast.parse(source.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "from_intrinsics_matrix")
    module = ast.Module(body=[fn], type_ignores=[])
    ns = {"torch": torch, "to_scalar": to_scalar}
    exec(compile(module, str(source), "exec"), ns)
    import types
    shim = types.ModuleType("conceptgraph.dataset.datasets_common")
    shim.from_intrinsics_matrix = ns["from_intrinsics_matrix"]
    sys.modules[shim.__name__] = shim
    return {"compatibility_patch": "AST lazy import bridge for pinned from_intrinsics_matrix only; function body unchanged; avoids unused GradSLAM dataset imports", "source": str(source), "function_sha256": hashlib.sha256(ast.get_source_segment(source.read_text(), fn).encode()).hexdigest()}


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def validate_feature_population(cache_ids: set[str], image_ids: set[str], text_ids: set[str]) -> None:
    """Require image/text vectors for exactly the cached P2 observation population."""
    if not cache_ids or cache_ids != image_ids or cache_ids != text_ids:
        raise ValueError(f"feature population mismatch: P2={len(cache_ids)}, image={len(image_ids)}, text={len(text_ids)}; IDs must match the full selected cache")


def base_mapping_config() -> dict:
    """Reference mapping config; min=3 is the unchanged default/control."""
    return {
        "device":"cpu", "spatial_sim_type":"overlap", "phys_bias":0.0, "match_method":"sim_sum",
        "sim_threshold":1.2, "mask_area_threshold":25, "mask_conf_threshold":0.25,
        "max_bbox_area_ratio":0.5, "skip_bg":False, "min_points_threshold":16,
        "downsample_voxel_size":0.025, "dbscan_remove_noise":True, "dbscan_eps":0.1,
        "dbscan_min_points":10, "obj_min_points":0, "obj_min_detections":3,
        "merge_overlap_thresh":0.7, "merge_visual_sim_thresh":0.7, "merge_text_sim_thresh":0.7,
        "denoise_interval":20, "filter_interval":-1, "merge_interval":-1,
    }


def apply_locked_experiment_config(base_config: dict, experiment_path: Path | None, seed: int,
                                   input_hashes: dict | None = None):
    """Apply the single reviewed filter ablation; never alter mapper controls."""
    if experiment_path is None:
        return dict(base_config), None
    experiment_path = Path(experiment_path)
    spec = json.loads(experiment_path.read_text())
    if spec.get("schema") != "conceptgraphs-hybrid-filter-ablation/v1":
        raise ValueError("unsupported hybrid experiment config schema")
    base_hash = hashlib.sha256(json.dumps(base_config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if spec.get("base_effective_config_sha256") != base_hash:
        raise ValueError("experiment base config hash mismatch")
    if int(spec.get("seed", -1)) != int(seed):
        raise ValueError("experiment config seed mismatch")
    if spec.get("overrides") != {"obj_min_detections": 1}:
        raise ValueError("this experiment permits exactly obj_min_detections=1")
    if input_hashes is not None and spec.get("input_hashes") != input_hashes:
        raise ValueError("experiment config input hashes do not match selected cache/sidecar")
    effective = dict(base_config)
    effective["obj_min_detections"] = 1
    return effective, {"experiment_id": spec["experiment_id"],
                       "experiment_config_path": str(experiment_path.resolve()),
                       "experiment_config_sha256": sha(experiment_path),
                       "base_effective_config_sha256": base_hash,
                       "overrides": {"obj_min_detections": 1}}


def load_inputs(cache: Path, text_root: Path, nframes: int, image_features_root: Path | None = None):
    p2_path = cache / "detection_2d/detection_2d_index.json"
    p3_path = cache / "detection_3d/detection_3d.jsonl"
    p3_manifest_path = cache / "detection_3d/detection_3d_manifest.json"
    feature_root = (image_features_root or (cache / "appearance/u2_features")).resolve()
    feature_manifest_path = feature_root / "feature_manifest.jsonl"
    p2 = json.loads(p2_path.read_text())
    p3manifest = json.loads(p3_manifest_path.read_text())
    text_manifest = json.loads((text_root / "manifest.json").read_text())
    if p3manifest.get("status") != "pass": raise ValueError("P3 cache manifest status is not pass")
    text_npz_path = text_root / "text_features.npz"
    text_rows_path = text_root / "text_feature_manifest.jsonl"
    if text_manifest.get("status") != "pass" or sha(text_npz_path) != text_manifest["text_features_sha256"] or sha(text_rows_path) != text_manifest["rows_sha256"]:
        raise ValueError("text feature sidecar status/hash mismatch")
    p3frames = load_jsonl(p3_path)
    p3_pose_by_frame = {}
    for row in p3frames:
        for det in row.get("detections", []):
            frame = str(det["detection_id"]).rsplit(":det-", 1)[0]
            pose = np.asarray(det["provider_metadata"]["camera_pose_W_Copt"], dtype=np.float64)
            if frame in p3_pose_by_frame and not np.allclose(p3_pose_by_frame[frame], pose, atol=1e-7):
                raise ValueError(f"inconsistent cached P3 camera pose within {frame}")
            p3_pose_by_frame[frame] = pose
    # P3 may legitimately reject every detection (e.g. sparse masks at this
    # capture resolution), but mapping diagnostics still require every frame's
    # pose. Use the exact RGB-D capture adapter already used in the mapping loop;
    # retain P3 poses where available and verify them against that same source.
    capture = cached_capture(cache)
    capture_pose_by_frame = {}
    from adapters.habitat.frames import frame_from_capture_record
    for rec in capture["frames"]:
        packet, _frame_packet = frame_from_capture_record(rec, cache / "capture")
        capture_pose_by_frame[str(rec["frame_id"])] = np.asarray(packet.T_W_Copt.matrix, dtype=np.float64)
    pose_by_frame = complete_frame_pose_map(
        [str(fr["frame_id"]) for fr in p2["frames"]], p3_pose_by_frame, capture_pose_by_frame)
    text = np.load(text_npz_path, allow_pickle=False)
    text_by_id = {str(oid): text["text_feature"][i] for i, oid in enumerate(text["observation_id"].tolist())}
    feature_rows = {str(r["observation_id"]): r for r in load_jsonl(feature_manifest_path)}
    if text_manifest.get("observation_count") != len(text_by_id) or not text_by_id:
        raise ValueError("text feature sidecar observation count is invalid")
    if any(r.get("checkpoint_sha256") != text_manifest.get("checkpoint_sha256") or r.get("feature_shape") != [512]
           for r in feature_rows.values()):
        raise ValueError("cached image features do not match text sidecar model/dimension provenance")
    ordered_frames = sorted(p2["frames"], key=lambda x: int(x["frame_index"]))
    if nframes < 1 or nframes > len(ordered_frames):
        raise ValueError(f"requested frame count {nframes} outside cache range 1..{len(ordered_frames)}")
    frames = ordered_frames[:nframes]
    cache_ids = {str(d["detection_id"]) for fr in ordered_frames for d in fr["detections"]}
    feature_ids = set(feature_rows)
    text_ids = set(text_by_id)
    validate_feature_population(cache_ids, feature_ids, text_ids)
    all_ids = {d["detection_id"] for fr in frames for d in fr["detections"]}
    if not all_ids <= set(text_by_id) or not all_ids <= set(feature_rows):
        raise ValueError("P2 observations lack aligned cached image/text features")
    if p2.get("capture_manifest_sha256") and p2["capture_manifest_sha256"] != p3manifest.get("capture_manifest_sha256"):
        raise ValueError("capture provenance differs between P2/P3")
    return p2, frames, pose_by_frame, text_by_id, feature_rows, p2_path, p3_path, text_manifest, feature_manifest_path


def complete_frame_pose_map(frame_ids, p3_pose_by_frame, capture_pose_by_frame):
    """Return verified poses for requested frames, preferring P3 when present."""
    result = {}
    for frame_id in map(str, frame_ids):
        p3_pose = p3_pose_by_frame.get(frame_id)
        capture_pose = capture_pose_by_frame.get(frame_id)
        if p3_pose is None and capture_pose is None:
            raise ValueError(f"no verified camera pose for frame {frame_id}")
        if p3_pose is not None and capture_pose is not None and not np.allclose(
                p3_pose, capture_pose, atol=1e-5, rtol=0):
            raise ValueError(f"cached P3 pose disagrees with RGB-D capture pose for {frame_id}")
        result[frame_id] = p3_pose if p3_pose is not None else capture_pose
    return result


def make_reference_api():
    bridge = reference_import_bridge()
    if str(REF) not in sys.path: sys.path.insert(0, str(REF))
    from conceptgraph.slam.slam_classes import MapObjectList
    from conceptgraph.slam.mapping import (compute_spatial_similarities, compute_visual_similarities,
        aggregate_similarities, merge_detections_to_objects)
    from conceptgraph.slam.utils import (gobs_to_detection_list, filter_gobs, denoise_objects,
        filter_objects, merge_objects)
    from conceptgraph.slam.utils import merge_obj2_into_obj1
    return bridge, MapObjectList, compute_spatial_similarities, compute_visual_similarities, aggregate_similarities, merge_detections_to_objects, gobs_to_detection_list, filter_gobs, denoise_objects, filter_objects, merge_objects, merge_obj2_into_obj1


def build_gobs(asset_root: Path, frame: dict, image: np.ndarray, features: dict, text_by_id: dict, class_names: list[str]):
    detections = frame["detections"]
    local_classes = sorted({str(d["class_raw"]) for d in detections})
    # The session supplies a first-seen prefix registry.  It is deliberately
    # not computed from classes in future frames.
    active_classes = list(class_names) if class_names else local_classes
    class_to_local = {name: i for i, name in enumerate(active_classes)}
    mask_root = asset_root / "detection_2d"
    masks, boxes, ids, conf, class_ids, imfeat, textfeat = [], [], [], [], [], [], []
    for d in detections:
        oid = str(d["detection_id"])
        mask = np.asarray(np.load(mask_root / d["mask_file"], allow_pickle=False), dtype=bool)
        if sha(mask_root / d["mask_file"]) != d["mask_sha256"]:
            raise ValueError(f"cached P2 mask hash mismatch: {oid}")
        if mask.shape != image.shape[:2]: raise ValueError(f"mask/RGB shape mismatch: {oid}")
        featrow = features[oid]
        featpath = ROOT / featrow["feature_path"]
        if sha(featpath) != featrow["feature_file_sha256"]: raise ValueError(f"image feature hash mismatch: {oid}")
        z = np.load(featpath, allow_pickle=False)
        vec = np.asarray(z["image_feature"], dtype=np.float32).reshape(-1)
        if vec.shape != (512,) or not np.isfinite(vec).all() or not np.isclose(np.linalg.norm(vec), 1, atol=1e-4):
            raise ValueError(f"invalid cached image feature: {oid}")
        boxes.append(np.asarray(d["bbox_xyxy"], dtype=np.float32))
        masks.append(mask); ids.append(oid); conf.append(float(d["score_raw"]))
        class_ids.append(class_to_local[str(d["class_raw"])])
        imfeat.append(vec); textfeat.append(np.asarray(text_by_id[oid], dtype=np.float32))
    return {"xyxy": np.asarray(boxes, dtype=np.float32), "mask": np.asarray(masks, dtype=bool),
            "confidence": np.asarray(conf, dtype=np.float32), "class_id": np.asarray(class_ids, dtype=np.int64),
            "classes": active_classes, "image_feats": np.asarray(imfeat, dtype=np.float32),
            "text_feats": np.asarray(textfeat, dtype=np.float32), "source_observation_ids": ids}


def _update_mapping_state(state: SimpleNamespace, fr: dict) -> dict:
    """The unchanged per-frame reference mapping body, now session-callable."""
    frame_mapping_started = time.perf_counter()
    idx = int(fr["frame_index"])
    fid = str(fr["frame_id"])
    capture_root = Path(fr.get("capture_root", state.capture_root))
    asset_root = Path(fr.get("asset_root", state.asset_root))
    rec = fr.get("capture_record")
    if rec is None:
        rec = state.capture_records.get(fid)
    if rec is None:
        raise ValueError(f"frame {fid} lacks a persisted capture_record; update_frame cannot discover future route state")
    fdir = capture_root / rec["frame_dir"]
    image = np.asarray(np.load(fdir / "rgb.npy", allow_pickle=False)[..., :3], dtype=np.uint8)
    depth = np.asarray(np.load(fdir / "depth_m.npy", allow_pickle=False), dtype=np.float32).copy()
    valid = np.asarray(np.load(fdir / "valid_mask.npy", allow_pickle=False), dtype=bool)
    if depth.shape != image.shape[:2] or valid.shape != depth.shape:
        raise ValueError(f"RGB-D dimensions mismatch frame {idx}")
    depth[~valid] = np.nan
    meta = json.loads((fdir / "metadata.json").read_text())
    from adapters.habitat.frames import habitat_camera_intrinsics, frame_from_capture_record
    intr = habitat_camera_intrinsics(int(meta["width"]), int(meta["height"]),
                                     float(meta["hfov_deg"]), calibration_id=str(meta["calibration_id"]))
    import torch
    K = torch.tensor([[intr.fx, 0, intr.cx], [0, intr.fy, intr.cy], [0, 0, 1]], dtype=torch.float32)
    rgbd_packet, _frame_packet = frame_from_capture_record(rec, capture_root)
    T = np.asarray(rgbd_packet.T_W_Copt.matrix, dtype=np.float64)
    if fid in state.poses and not np.allclose(T, state.poses[fid], atol=1e-5, rtol=0):
        raise ValueError(f"capture adapter pose disagrees with cached P3 pose for {fid}")
    frame_classes = sorted({str(d["class_raw"]) for d in fr["detections"]})
    for class_name in frame_classes:
        if class_name not in state.class_names:
            state.class_names.append(class_name)
    gobs = build_gobs(asset_root, fr, image, state.features, state.text_by_id, state.class_names)
    filtered = state.filter_gobs(state.cfg, copy.deepcopy(gobs), image)
    retained_ids = list(filtered["source_observation_ids"])
    state.filtered_ids_by_frame[idx] = retained_ids
    fg, bg = state.to_dets(state.cfg, image, depth, K, idx, gobs, trans_pose=T,
                           class_names=state.class_names, color_path=str(fdir / "rgb.npy"))
    for det in bg:
        cname = det["class_name"][0]
        state.bg_objects[cname] = (det if state.bg_objects.get(cname) is None
                                    else state.merge_one(state.cfg, state.bg_objects[cname], det, run_dbscan=False))
    if len(fg):
        if len(state.objects) == 0:
            state.objects.extend(fg)
        else:
            ss = state.spatial(state.cfg, fg, state.objects)
            vs = state.visual(state.cfg, fg, state.objects)
            agg = state.aggregate(state.cfg, ss, vs)
            agg[agg < state.cfg.sim_threshold] = float("-inf")
            state.objects = state.merge_dets(state.cfg, fg, state.objects, agg)
    for det in fg:
        state.ledger.append({"observation_id": retained_ids[int(det["mask_idx"][-1])],
                             "frame_index": idx, "disposition": "mapped_foreground"})
    for det in bg:
        state.ledger.append({"observation_id": retained_ids[int(det["mask_idx"][-1])],
                             "frame_index": idx,
                             "disposition": "reference_background_class_accumulator_not_exported_as_object"})
    if state.timeline_recorder is not None:
        state.timeline_recorder.record_frame(
            frame_index=idx, frame_id=fid,
            timestamp_s=float(fr.get("timestamp_s", idx * 0.1)),
            objects=state.objects, filtered_ids_by_frame=state.filtered_ids_by_frame,
            p2_frames=state.p2_frames, poses_by_frame_id=state.poses,
            processing_ms=(time.perf_counter() - frame_mapping_started) * 1000.0)
    return {"frame_index": idx, "frame_id": fid,
            "online_object_count": len(state.objects),
            "observation_count": len(state.ledger),
            "processing_ms": (time.perf_counter() - frame_mapping_started) * 1000.0}


def _finalize_mapping_state(state: SimpleNamespace) -> dict:
    """Terminal reference postprocessing; never called from update_frame."""
    online_lineage = _stage_lineage(state.objects, state.filtered_ids_by_frame,
                                    state.p2_frames, state.poses, "online_before_postprocess")
    lineage_stages = {"online_before_postprocess": online_lineage}
    before_post = len(state.objects)
    if len(state.objects):
        state.objects = state.denoise(state.cfg, state.objects)
        lineage_stages["after_denoise"] = _stage_lineage(
            state.objects, state.filtered_ids_by_frame, state.p2_frames, state.poses, "after_denoise")
        prefilter = len(state.objects)
        state.objects = state.filter_objs(state.cfg, state.objects)
        postfilter = len(state.objects)
        lineage_stages["after_min_detections_filter"] = _stage_lineage(
            state.objects, state.filtered_ids_by_frame, state.p2_frames, state.poses,
            "after_min_detections_filter")
        state.objects = state.merge_objs(state.cfg, state.objects)
        lineage_stages["after_text_gated_merge"] = _stage_lineage(
            state.objects, state.filtered_ids_by_frame, state.p2_frames, state.poses,
            "after_text_gated_merge")
    else:
        prefilter = postfilter = 0
        for stage in ("after_denoise", "after_min_detections_filter", "after_text_gated_merge"):
            lineage_stages[stage] = []
    state.bg_objects = state.MapObjectList([x for x in state.bg_objects.values() if x is not None])
    if len(state.bg_objects):
        state.bg_objects = state.denoise(state.cfg, state.bg_objects)
    return {"online_lineage": online_lineage, "lineage_stages": lineage_stages,
            "before_post": before_post, "objects_after_denoise": prefilter,
            "objects_after_filter": postfilter, "final_count": len(state.objects)}


def run(cache: Path, text_root: Path, output: Path, nframes: int, seed: int = 20261014,
        experiment_config: Path | None = None, image_features_root: Path | None = None,
        room_snapshot: Path | None = None, timeline_out: Path | None = None,
        live_publish_root: Path | None = None):
    # Seed before reference imports or any mapping call. The exercised reference
    # path uses NumPy global RNG for instance colors and 4 mm point perturbation.
    rng_control = seed_mapping_rngs(seed)
    started = time.perf_counter(); cache = cache.resolve(); text_root = text_root.resolve(); output = output.resolve()
    if output.exists(): raise FileExistsError(f"output exists: {output}")
    output.mkdir(parents=True)
    (output / "native").mkdir(); (output / "export").mkdir()
    p2, frames, poses, text_by_id, features, p2_path, p3_path, text_manifest, feature_manifest_path = load_inputs(
        cache, text_root, nframes, image_features_root)
    room_snapshot = (room_snapshot or (cache / "replay/snapshot.json")).resolve()
    if not room_snapshot.is_file(): raise FileNotFoundError(f"matching room snapshot missing: {room_snapshot}")
    room_raw = json.loads(room_snapshot.read_text())
    room_data = room_raw.get("snapshot", room_raw)
    capture_manifest = cached_capture(cache)
    if (room_data.get("scene_id"), room_data.get("sequence_id")) != (capture_manifest.get("scene_id"), capture_manifest.get("sequence_id")):
        raise ValueError("room snapshot scene/sequence does not match cached capture")
    api = make_reference_api()
    bridge, MapObjectList, spatial, visual, aggregate, merge_dets, to_dets, filter_gobs, denoise, filter_objs, merge_objs, merge_one = api
    from omegaconf import OmegaConf
    base_config = base_mapping_config()
    cfg = OmegaConf.create(base_config)
    input_hashes = {"capture_manifest_sha256": sha(cache / "capture/capture_manifest.json"),
                    "p2_index_sha256": sha(p2_path),
                    "p3_manifest_sha256": sha(cache / "detection_3d/detection_3d_manifest.json"),
                    "text_features_manifest_sha256": sha(text_root / "manifest.json"),
                    "image_features_manifest_sha256": sha(feature_manifest_path),
                    "room_snapshot_sha256": sha(room_snapshot)}
    config, experiment_metadata = apply_locked_experiment_config(base_config, experiment_config, seed, input_hashes)
    cfg.obj_min_detections = config["obj_min_detections"]
    environment_lock=json.loads((ROOT/"locks/integration_v2/cg_local_cpu_hybrid.lock.json").read_text())
    environment_lock_hash=hashlib.sha256(json.dumps(environment_lock["environment_candidate_not_verified"],sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
    contract = {"schema":"conceptgraphs-cached-hybrid-mapping/v1", "status":"locked_before_mapping",
        "description":"cached YOLO11n-seg masks + cached ViT-B-32 image features + newly encoded exact detector class text features + pinned ConceptGraphs mapping core; not full ConceptGraphs-Detect/reference parity",
        "rng_control":{**rng_control,"seeded_before":"reference imports, frame mapping, geometry construction, and postprocessing","reference_numpy_consumers":["get_random_color/instance color","create_object_pcd 4e-3 Gaussian point perturbation"]},
        "reference_commit":"93277a02bd89171f8121e84203121cf7af9ebb5d", "effective_config":config,
        "experiment": experiment_metadata or {"experiment_id": "baseline_obj_min_detections_3", "overrides": {}},
        "runner_sha256":sha(Path(__file__).resolve()),
        "cpu_environment_lock_effective_section_sha256":environment_lock_hash,
        "text_features_manifest_sha256":sha(text_root/"manifest.json"),
        "image_features_manifest_sha256":sha(feature_manifest_path), "room_snapshot":str(room_snapshot),
        "room_snapshot_sha256":sha(room_snapshot), "cache_root":str(cache),
        "p2_index_sha256":sha(p2_path), "p3_manifest_sha256":sha(cache/"detection_3d/detection_3d_manifest.json"),
        "capture_manifest_sha256":sha(cache/"capture/capture_manifest.json"), "input_observation_ids":[d["detection_id"] for fr in frames for d in fr["detections"]],
        "causality":"reference frame-ordered mapping; each frame detections are assembled then mapped; no GT/annotation/room labels",
        "compatibility":bridge, "hybrid_threshold_status":"ConceptGraphs thresholds retained; compatibility with YOLO11n-seg + ViT-B-32 is unvalidated",
        "calibration":"adapters.habitat.frames.habitat_camera_intrinsics; T_W_Copt from cached P3 provider metadata when detections exist, otherwise frame_from_capture_record; matching poses are cross-checked",
        "session_lifecycle": {
            "api": "ConceptGraphsMappingSession.open/update_frame/finalize/close",
            "batch_runner_uses_same_update": True,
            "terminal_postprocess": "reference denoise_objects -> filter_objects -> merge_objects; finalize only",
            "input_order": "strictly increasing frame_index and unique frame_id",
            "duplicate_or_stale_policy": "reject before mapping callback/RNG; failed callback requires replay_required",
            "class_registry_policy": "deterministic first-seen prefix registry; newly seen names added in sorted order; no future class list",
            "update_frame_packet": "frame detections plus capture_record/capture_root/asset_root; no route manifest discovery during update"
        }}
    (output/"experiment_contract.json").write_text(json.dumps(contract,indent=2,sort_keys=True)+"\n")
    timeline_recorder = None
    if timeline_out is not None:
        from standalone_sg.serialization.hybrid_timeline import HybridCausalTimelineRecorder
        timeline_recorder = HybridCausalTimelineRecorder(
            Path(timeline_out), scene_id=str(room_data["scene_id"]),
            sequence_id=str(room_data["sequence_id"]), cache_root=cache,
            mapping_output=output, input_hashes=input_hashes,
            config_hash=hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            seed=seed, room_snapshot=room_snapshot,
            runner_sha256=sha(Path(__file__).resolve()), live_publish_root=live_publish_root)
    from integration_v2.conceptgraphs_session import ConceptGraphsMappingSession
    state = SimpleNamespace(
        cfg=cfg, cache=cache, asset_root=cache, capture_root=cache / "capture",
        features=features, text_by_id=text_by_id,
        p2_frames=p2["frames"], poses=poses, timeline_recorder=timeline_recorder,
        MapObjectList=MapObjectList, spatial=spatial, visual=visual,
        aggregate=aggregate, merge_dets=merge_dets, to_dets=to_dets,
        filter_gobs=filter_gobs, denoise=denoise, filter_objs=filter_objs,
        merge_objs=merge_objs, merge_one=merge_one,
        objects=MapObjectList(), bg_objects={}, ledger={},
        filtered_ids_by_frame={}, class_names=[], capture_records={
            str(rec["frame_id"]): rec for rec in cached_capture(cache)["frames"]})
    # Keep one shared per-frame implementation for batch and future streaming.
    # The explicit map list is used below only to retain the old JSONL order.
    state.ledger = []
    session = ConceptGraphsMappingSession.open(
        state=state, update_impl=_update_mapping_state,
        finalize_impl=_finalize_mapping_state)
    t_map = time.perf_counter()
    try:
        for fr in frames:
            packet = dict(fr)
            packet["capture_record"] = state.capture_records[str(fr["frame_id"])]
            packet["capture_root"] = str(state.capture_root)
            packet["asset_root"] = str(state.asset_root)
            session.update_frame(packet)
    except BaseException:
        session.close()
        raise
    mapping_s = time.perf_counter() - t_map
    # Capture immutable lineage at each reference postprocessing boundary.
    # This is diagnostic serialization only; it does not feed the mapper.
    online_lineage = _stage_lineage(
        state.objects, state.filtered_ids_by_frame, p2["frames"], poses,
        "online_before_postprocess")
    diagnostic_rows = _save_diagnostic_hypotheses(
        cache, output, state.objects, online_lineage, p2["frames"], frames, poses)
    try:
        terminal = session.finalize()
    except BaseException:
        session.close()
        raise
    # Mapping state is no longer mutable after terminal postprocessing.
    session.close()
    lineage_stages = terminal["lineage_stages"]
    objects = state.objects
    bg_objects = state.bg_objects
    ledger = state.ledger
    filtered_ids_by_frame = state.filtered_ids_by_frame
    classes = list(state.class_names)
    before_post = terminal["before_post"]
    prefilter = terminal["objects_after_denoise"]
    postfilter = terminal["objects_after_filter"]
    final_count = terminal["final_count"]
    diagnostic_manifest = _finalize_diagnostic_hypotheses(
        output, diagnostic_rows, lineage_stages["after_min_detections_filter"],
        lineage_stages["after_text_gated_merge"], int(cfg.obj_min_detections))
    contract["mapping_runtime_s"]=mapping_s; contract["pre_postprocess_object_count"]=before_post
    contract["after_min_detections_filter_count"]=postfilter; contract["final_object_count"]=final_count
    (output/"experiment_contract.json").write_text(json.dumps(contract,indent=2,sort_keys=True)+"\n")
    _export(cache,output,objects,bg_objects,classes,ledger,contract,frames,nframes,filtered_ids_by_frame,room_snapshot)
    (output / "lineage_stages.json").write_text(json.dumps({
        "schema": "conceptgraphs-hybrid-lineage-stages/v1",
        "source_cache_capture_manifest_sha256": contract["capture_manifest_sha256"],
        "input_observation_ids": contract["input_observation_ids"],
        "effective_config_sha256": hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "stage_semantics": {
            "online_before_postprocess": "Reference online association/fusion state after all input frames; before denoise/filter/text-gated merge.",
            "after_denoise": "Reference denoise_objects output.",
            "after_min_detections_filter": "Reference filter_objects output; removed objects are filtered, not merged into survivors.",
            "after_text_gated_merge": "Reference merge_objects output.",
            "pose_count": "Count of distinct cached camera pose matrices; not an independent-view count."
        },
        "stages": lineage_stages,
    }, indent=2, sort_keys=True, allow_nan=False) + "\n")
    timeline_manifest = None
    if timeline_recorder is not None:
        timeline_manifest = timeline_recorder.finalize(
            final_lineage_path=output / "lineage_stages.json",
            final_export_root=output / "export",
            peak_rss_mib=peak_rss_mib())
    elapsed=time.perf_counter()-started
    result={"status":"pass","frames":len(frames),"observations":len(contract["input_observation_ids"]),
        "reference_detections_mapped":len(ledger),"foreground_objects_before_postprocess":before_post,
        "objects_after_filter":postfilter,"final_objects_after_text_gated_merge":final_count,
        "runtime_s":elapsed,"mapping_runtime_s":mapping_s,"peak_rss_mib":peak_rss_mib(),
        "objects_after_denoise":prefilter,
        "output":str(output),"seed":int(seed),"lineage_stages_file":"lineage_stages.json",
        "diagnostic_hypotheses": len(diagnostic_manifest["objects"]),
        "diagnostic_manifest": "diagnostic/diagnostic_manifest.json",
        "postprocessing":{"denoise":"reference denoise_objects","filter":f"reference obj_min_detections={int(cfg.obj_min_detections)}","merge":"reference merge_objects with visual+text similarity gates"}}
    if timeline_manifest is not None:
        result["timeline_output"] = str(Path(timeline_out).resolve())
        result["timeline_manifest"] = timeline_manifest
    (output/"run_manifest.json").write_text(json.dumps({**result,"contract_sha256":sha(output/"experiment_contract.json"),"export_manifest_sha256":sha(output/"export/export_manifest.json")},indent=2,sort_keys=True)+"\n")
    print(json.dumps(result,indent=2)); return result


_CAPTURE_MANIFEST = None
def cached_capture(cache):
    global _CAPTURE_MANIFEST
    if _CAPTURE_MANIFEST is None: _CAPTURE_MANIFEST=json.loads((cache/"capture/capture_manifest.json").read_text())
    return _CAPTURE_MANIFEST


def _stage_lineage(objects, filtered_ids_by_frame, p2_frames, poses_by_frame_id, stage):
    """Serialize object membership and simple geometry diagnostics without mutation."""
    frame_id_by_index = {int(fr["frame_index"]): str(fr["frame_id"]) for fr in p2_frames}
    rows = []
    for ordinal, obj in enumerate(objects):
        pairs = [(int(fi), int(mi)) for fi, mi in zip(obj.get("image_idx", []), obj.get("mask_idx", []))]
        observation_ids = []
        for fi, mi in pairs:
            ids = filtered_ids_by_frame.get(fi, [])
            if 0 <= mi < len(ids):
                observation_ids.append(ids[mi])
        # Preserve reference observation order while de-duplicating lineage entries.
        observation_ids = list(dict.fromkeys(observation_ids))
        frames = sorted({fi for fi, _ in pairs})
        pose_keys = set()
        for fi in frames:
            frame_id = frame_id_by_index.get(fi)
            pose = poses_by_frame_id.get(frame_id)
            if pose is not None:
                pose_keys.add(tuple(np.round(np.asarray(pose, dtype=np.float64).reshape(-1), 6).tolist()))
        points = np.asarray(obj["pcd"].points, dtype=np.float64)
        names = [str(x) for x in obj.get("class_name", [])]
        rows.append({
            "stage": stage,
            "ordinal": ordinal,
            "lineage_key": hashlib.sha256("\n".join(sorted(observation_ids)).encode()).hexdigest()[:16],
            "observation_ids": observation_ids,
            "detection_count": len(observation_ids),
            "frame_indices": frames,
            "distinct_frame_count": len(frames),
            "distinct_pose_count": len(pose_keys),
            "class_history_raw": names,
            "point_count": int(len(points)),
            "bounds_minmax": np.stack([points.min(0), points.max(0)]).tolist() if len(points) else None,
        })
    return rows


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", binascii.crc32(body) & 0xffffffff)


def _write_rgb_png(path: Path, image: np.ndarray) -> None:
    image = np.asarray(image, dtype=np.uint8)
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("PNG panel must be uint8 HxWx3 RGB")
    height, width = image.shape[:2]
    rows = b"".join(b"\x00" + image[y].tobytes() for y in range(height))
    payload = (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
               + _png_chunk(b"IDAT", zlib.compress(rows, level=6)) + _png_chunk(b"IEND", b""))
    Path(path).write_bytes(payload)


def _save_diagnostic_hypotheses(cache: Path, output: Path, objects, online_rows, p2_frames, selected_frames, poses_by_frame):
    """Persist pre-filter hypotheses without changing the reference graph/export."""
    diagnostic = output / "diagnostic"
    cloud_root = diagnostic / "hypotheses"
    evidence_root = diagnostic / "evidence"
    cloud_root.mkdir(parents=True, exist_ok=False)
    evidence_root.mkdir(parents=True, exist_ok=False)
    all_detections = {str(d["detection_id"]): (fr, d) for fr in selected_frames for d in fr["detections"]}
    capture = cached_capture(cache)
    capture_by_index = {int(r["frame_index"]): r for r in capture["frames"]}
    rgb_by_frame = {}
    evidence = {}
    for oid, (frame, det) in all_detections.items():
        frame_index = int(frame["frame_index"])
        if frame_index not in rgb_by_frame:
            rec = capture_by_index[frame_index]
            rgb_path = cache / "capture" / rec["frame_dir"] / "rgb.npy"
            rgb_by_frame[frame_index] = np.asarray(np.load(rgb_path, allow_pickle=False)[..., :3], dtype=np.uint8)
        rgb = rgb_by_frame[frame_index]
        mask_path = cache / "detection_2d" / det["mask_file"]
        mask = np.asarray(np.load(mask_path, allow_pickle=False), dtype=bool)
        if mask.shape != rgb.shape[:2]:
            raise ValueError(f"diagnostic RGB/mask shape mismatch for {oid}")
        # Full observation ID (including frame) plus panel type makes every output path unique.
        safe_id = oid.replace(":", "_").replace("/", "_")
        rgb_name, mask_name, overlay_name, crop_name = (f"{safe_id}_{kind}.png" for kind in ("rgb", "mask", "overlay", "crop"))
        _write_rgb_png(evidence_root / rgb_name, rgb)
        mask_rgb = np.repeat((mask.astype(np.uint8) * 255)[..., None], 3, axis=2)
        _write_rgb_png(evidence_root / mask_name, mask_rgb)
        overlay = rgb.copy()
        if mask.any():
            overlay[mask] = (0.50 * overlay[mask] + 0.50 * np.asarray([255, 48, 48])).astype(np.uint8)
            ys, xs = np.where(mask)
            pad = max(8, int(0.12 * max(xs.max() - xs.min() + 1, ys.max() - ys.min() + 1)))
            x0, x1 = max(0, int(xs.min()) - pad), min(rgb.shape[1], int(xs.max()) + pad + 1)
            y0, y1 = max(0, int(ys.min()) - pad), min(rgb.shape[0], int(ys.max()) + pad + 1)
            crop = overlay[y0:y1, x0:x1]
            _write_rgb_png(evidence_root / crop_name, crop)
        else:
            x0=y0=x1=y1=0
            _write_rgb_png(evidence_root / crop_name, overlay)
        _write_rgb_png(evidence_root / overlay_name, overlay)
        evidence[oid] = {"frame_index": frame_index, "raw_class": str(det["class_raw"]),
                         "score_raw": float(det.get("score_raw", 0.0)),
                         "camera_pose_W_Copt": np.asarray(poses_by_frame[str(frame["frame_id"])], dtype=np.float64).tolist(),
                         "source_rgb": str((cache / "capture" / capture_by_index[frame_index]["frame_dir"] / "rgb.npy").resolve()),
                         "source_rgb_sha256": sha(cache / "capture" / capture_by_index[frame_index]["frame_dir"] / "rgb.npy"),
                         "source_mask": str(mask_path.resolve()), "source_mask_sha256": sha(mask_path),
                         "panels": {kind: f"evidence/{name}" for kind, name in
                                    (("rgb", rgb_name), ("mask", mask_name), ("overlay", overlay_name), ("crop", crop_name))},
                         "mask_crop_xyxy": [x0, y0, x1, y1]}
    rows = []
    for row, obj in zip(online_rows, objects):
        hypothesis_id = "hypothesis_" + row["lineage_key"]
        rel = f"hypotheses/{hypothesis_id}.npz"
        points = np.asarray(obj["pcd"].points, dtype=np.float32)
        colors = np.asarray(obj["pcd"].colors, dtype=np.float32)
        if not len(points) or not np.isfinite(points).all():
            raise ValueError(f"invalid pre-filter hypothesis cloud {hypothesis_id}")
        np.savez_compressed(diagnostic / rel, xyz=points, rgb=np.clip(colors * 255, 0, 255).astype(np.uint8))
        rows.append({**row, "hypothesis_id": hypothesis_id, "cloud_npz": rel,
                     "cloud_sha256": sha(diagnostic / rel)})
    (diagnostic / "evidence_manifest.json").write_text(json.dumps({
        "schema": "conceptgraphs-hybrid-observation-panels/v1", "cache_root": str(cache.resolve()),
        "pose_count_semantics": "number of unique cached camera pose matrices, not independent-view count",
        "observations": evidence}, indent=2, sort_keys=True) + "\n")
    return rows


def _finalize_diagnostic_hypotheses(output: Path, rows, after_filter, final_stage, min_detections):
    after_filter_by_id = {oid: row["lineage_key"] for row in after_filter for oid in row["observation_ids"]}
    final_by_id = {oid: row["lineage_key"] for row in final_stage for oid in row["observation_ids"]}
    final_rows = {row["lineage_key"]: row for row in final_stage}
    result = []
    for row in rows:
        ids = row["observation_ids"]
        retained_ids = [oid for oid in ids if oid in after_filter_by_id]
        target_keys = sorted({final_by_id[oid] for oid in ids if oid in final_by_id})
        if not retained_ids:
            status, disposition = "filtered", "filtered_not_merged"
            reason = f"detection_count={row['detection_count']} < obj_min_detections={min_detections}"
        else:
            status = "retained"
            if len(target_keys) == 1 and sorted(final_rows[target_keys[0]]["observation_ids"]) != sorted(ids):
                disposition = "merged_or_repartitioned_after_filter"
            elif len(target_keys) > 1:
                disposition = "repartitioned_after_filter"
            else:
                disposition = "retained_without_membership_change"
            reason = f"detection_count={row['detection_count']} >= obj_min_detections={min_detections}"
        result.append({**row, "status_after_filter": status, "filter_reason": reason,
                       "postprocess_disposition": disposition, "final_object_keys": target_keys})
    manifest = {"schema": "conceptgraphs-hybrid-object-hypotheses/v1",
                "scope": "diagnostic_only_pre_filter_hypotheses; not imported into room graph and not used as semantic evidence",
                "filter_rule": f"pinned reference filter_objects; obj_min_detections={min_detections}; obj_min_points=0",
                "objects": result,
                "counts": {"before_filter": len(rows),
                           "retained_after_filter": sum(r["status_after_filter"] == "retained" for r in result),
                           "filtered": sum(r["status_after_filter"] == "filtered" for r in result),
                           "merged_or_repartitioned_after_filter": sum(r["postprocess_disposition"] == "merged_or_repartitioned_after_filter" for r in result)}}
    path = output / "diagnostic/diagnostic_manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def _export(cache, output, objects, bg_objects, classes, ledger, contract, frames, nframes, filtered_ids_by_frame, room_snapshot):
    export=output/"export"; native=output/"native/conceptgraphs_map.pkl.gz"
    serial=objects.to_serializable()
    with gzip.open(native,"wb") as f: pickle.dump({"objects":serial,"bg_objects":bg_objects.to_serializable(),"class_names":classes,"effective_config":contract["effective_config"],"reference_commit":contract["reference_commit"]},f,protocol=pickle.HIGHEST_PROTOCOL)
    by_frame={int(fr["frame_index"]):fr for fr in frames}; times={int(fr["frame_index"]):float(fr.get("timestamp_s",int(fr["frame_index"])*.1)) for fr in cached_capture(cache)["frames"]}
    obs_ids={r["observation_id"] for r in ledger}; items=[]; files={}
    for i,obj in enumerate(objects):
        points=np.asarray(obj["pcd"].points,dtype=np.float32); colors=np.asarray(obj["pcd"].colors,dtype=np.float32)
        if len(colors)!=len(points): colors=np.zeros((len(points),3),dtype=np.float32)
        if not len(points) or not np.isfinite(points).all(): raise ValueError(f"invalid final geometry object {i}")
        frame_idxs=[int(x) for x in obj.get("image_idx",[])]; mask_idxs=[int(x) for x in obj.get("mask_idx",[])]
        evidence=[]
        for fi,mi in zip(frame_idxs,mask_idxs):
            ids=filtered_ids_by_frame.get(fi,[])
            if 0 <= mi < len(ids): evidence.append(ids[mi])
        evidence=list(dict.fromkeys(evidence))
        # Native objects retain image_idx/mask_idx; use a content-derived id, never track suffix.
        export_id="cg_"+hashlib.sha256("\n".join(sorted(evidence)).encode()).hexdigest()[:16]
        if not evidence: raise ValueError(f"final object {i} has no observation lineage")
        rel=f"objects/{export_id}.npz"; (export/"objects").mkdir(exist_ok=True)
        np.savez_compressed(export/rel,xyz=points,rgb=np.clip(colors*255,0,255).astype(np.uint8))
        files[rel]=sha(export/rel)
        bounds=np.stack([points.min(0),points.max(0)]).astype(float).tolist()
        names=list(obj.get("class_name",[])); raw=Counter(names).most_common(1)[0][0] if names else None
        confs=[float(c) for c in obj.get("conf",[]) if np.isfinite(c)]
        seen=[int(x) for x in obj.get("image_idx",[])]
        items.append({"export_id":export_id,"cloud_npz":rel,"bounds_minmax":bounds,
            "membership_status":"unavailable","raw_detector_class":raw,"fused_semantic_label":None,
            "fused_semantic_status":"unavailable","confidence":float(np.mean(confs)) if confs else None,
            "evidence":evidence,"observation_count":len(evidence),
            "first_seen_s":min(times.get(x,0.0) for x in seen) if seen else 0.0,
            "last_seen_s":max(times.get(x,0.0) for x in seen) if seen else 0.0,
            "reference_class_history":names,"point_count":len(points)})
    # Immutable native artifact referenced relative to export directory parent.
    native_rel=str(native.relative_to(output)); native_hash=sha(native)
    snap=json.loads(Path(room_snapshot).read_text())
    nested=snap.get("snapshot",snap); scene=nested["scene_id"]; sequence=nested["sequence_id"]
    manifest={"schema":"conceptgraphs-object-export/v1","identity_scope":"final_map_only_no_persistent_lifecycle_claim",
        "coordinate_frame":"conceptgraphs_world_from_capture_contract","units":"m","scene_id":scene,
        "sequence_id":sequence,"source_run_id":None,
        "T_capture_world_from_cg_world":np.eye(4).tolist(),"native_map_file":native_rel,
        "native_map_sha256":native_hash,"objects":items,"files_sha256":files,
        "provenance":{"source_kind":"reference","display_label":"Hybrid YOLO11n-seg + ViT-B-32 / ConceptGraphs reference mapping core","pipeline":"hybrid cached YOLO11n-seg + cached ViT-B-32 image features + ViT-B-32 detector-text features + pinned ConceptGraphs reference mapping core","reference_commit":contract["reference_commit"],"source_cache_id":cache.name,"source_cache_capture_manifest_sha256":contract["capture_manifest_sha256"],"source_cache_p2_index_sha256":contract["p2_index_sha256"],"text_feature_manifest_sha256":contract["text_features_manifest_sha256"],"mapping_contract_sha256":sha(output/"experiment_contract.json"),"not_full_reference_parity":True}}
    (export/"export_manifest.json").write_text(json.dumps(manifest,indent=2,sort_keys=True,allow_nan=False)+"\n")
    (output/"observation_lineage.jsonl").write_text("".join(json.dumps(r,sort_keys=True)+"\n" for r in ledger))


def main():
    p=argparse.ArgumentParser(); p.add_argument("--cache",type=Path,required=True); p.add_argument("--text-features",type=Path,required=True); p.add_argument("--image-features",type=Path,default=None); p.add_argument("--room-snapshot",type=Path,default=None); p.add_argument("--output",type=Path,required=True); p.add_argument("--frames",type=int,required=True); p.add_argument("--experiment-config",type=Path,default=None); p.add_argument("--timeline-out",type=Path,default=None,help="Optional read-only causal object timeline output directory")
    p.add_argument("--seed",type=int,default=20261014,help="Seed Python, NumPy, and Torch CPU RNGs before mapping (default: 20261014).")
    p.add_argument("--live-publish-root",type=Path,default=None,help="Optional D1 run root; publish and barrier each immutable object revision")
    a=p.parse_args(); run(a.cache,a.text_features,a.output,a.frames,a.seed,a.experiment_config,a.image_features,a.room_snapshot,a.timeline_out,a.live_publish_root)

if __name__=="__main__": main()

#!/usr/bin/env python3
"""Compare the same HM3D fixed-pose run at 160x120 and 320x240."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_run(root: Path) -> dict:
    manifest = read_json(root / "launcher_manifest.json")
    capture = read_json(root / "capture/capture_manifest.json")
    route = read_json(root / "effective_route_config.json")
    return {"root": root, "manifest": manifest, "capture": capture, "route": route}


def ensure_pair(baseline: dict, candidate: dict) -> None:
    b, c = baseline["capture"], candidate["capture"]
    if baseline["manifest"].get("status") != "completed":
        raise ValueError("baseline launcher run must be completed")
    # The launcher invokes this as its final stage, while candidate manifest is
    # necessarily still `running`; require every upstream stage to have passed.
    candidate_stages = candidate["manifest"].get("stages", {})
    required = ("capture", "detection_2d", "detection_3d", "native_replay", "semantic", "evaluation")
    if any(candidate_stages.get(stage, {}).get("status") != "pass" for stage in required):
        raise ValueError("candidate upstream pipeline stages are incomplete or failed")
    if b.get("resolution_px") != [160, 120] or c.get("resolution_px") != [320, 240]:
        raise ValueError("expected baseline 160x120 and candidate 320x240")
    if b.get("scene_id") != c.get("scene_id") or b.get("frame_count") != c.get("frame_count"):
        raise ValueError("scene/frame count differs")
    if b.get("scene_id") != "00802-wcojb4TFT35":
        raise ValueError("this comparison is scoped to HM3D 00802")
    allowed = {"resolution_px", "route_policy", "comparison_baseline_run_id"}
    br = {k: v for k, v in baseline["route"].items() if k not in allowed}
    cr = {k: v for k, v in candidate["route"].items() if k not in allowed}
    if br != cr:
        raise ValueError("effective route configs differ beyond resolution/comparison metadata")
    for idx, (bf, cf) in enumerate(zip(b["frames"], c["frames"])):
        if bf.get("frame_id") != cf.get("frame_id") or bf.get("timestamp_s") != cf.get("timestamp_s"):
            raise ValueError(f"frame ID/timestamp mismatch at index {idx}")


def validate_capture_arrays(run: dict) -> dict:
    root, capture = run["root"], run["capture"]
    w, h = capture["resolution_px"]
    checks = []
    for item in capture["frames"]:
        d = root / "capture" / item["frame_dir"]
        meta = read_json(d / "metadata.json")
        rgb = np.load(d / "rgb.npy", mmap_mode="r", allow_pickle=False)
        depth = np.load(d / "depth_m.npy", mmap_mode="r", allow_pickle=False)
        valid = np.load(d / "valid_mask.npy", mmap_mode="r", allow_pickle=False)
        if rgb.ndim != 3 or rgb.shape[:2] != (h, w) or rgb.shape[2] not in (3, 4) or depth.shape != (h, w) or valid.shape != (h, w):
            raise ValueError(f"captured array shape mismatch at {item['frame_id']}: rgb={rgb.shape}, depth={depth.shape}, valid={valid.shape}")
        if meta.get("width") != w or meta.get("height") != h:
            raise ValueError(f"capture metadata resolution mismatch at {item['frame_id']}")
        checks.append({"frame_id": item["frame_id"], "timestamp_s": item["timestamp_s"],
                       "rgb_shape": list(rgb.shape), "depth_shape": list(depth.shape),
                       "valid_pixels": int(np.count_nonzero(valid))})
    return {"resolution_px": [w, h], "checked_frames": len(checks), "shapes": checks}


def metrics(run: dict) -> dict:
    root = run["root"]
    d2 = read_json(root / "detection_2d/detection_2d_index.json")
    ledger_path = root / "detection_3d/object_evidence_ledger.jsonl"
    ledger = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    d3 = read_json(root / "detection_3d/detection_3d_manifest.json")
    snap = read_json(root / "replay/snapshot.json").get("snapshot", {})
    nodes = snap.get("nodes", [])
    diag = read_json(root / "replay/frame_diagnostics.json")
    labels = read_json(root / "semantic/region_labels.json")
    replay_manifest = read_json(root / "replay/manifest.json")
    pred = sum(row.get("status") == "predicted" for row in labels)
    unknown = sum(row.get("status") == "unknown" for row in labels)
    stages = run["manifest"].get("stages", {})
    runtime = {k: stages.get(k, {}).get("elapsed_s") for k in
               ("capture", "detection_2d", "detection_3d", "native_replay", "semantic", "evaluation")}
    runtime["sum_processing_stages_s"] = round(sum(v for v in runtime.values() if isinstance(v, (int, float))), 3)
    return {
        "frames": run["capture"].get("frame_count"),
        "detections_2d": d2.get("detection_count"),
        "detections_3d_accepted": sum(row.get("decision") == "accepted" for row in ledger),
        "detections_3d_rejected": sum(row.get("decision") == "rejected" for row in ledger),
        "object_nodes": sum(n.get("kind") == "object" for n in nodes),
        "regions": diag[-1].get("m4_region_count") if diag else None,
        "room_nodes": sum(n.get("kind") == "room" for n in nodes),
        "predicted_room_labels": pred,
        "unknown_room_labels": unknown,
        "stage_runtime_s": runtime,
        "native_replay_python_peak_rss_mib": round(replay_manifest.get("python_process_ru_maxrss_kib", 0) / 1024, 1),
        "d3_manifest": {
            "detection_3d_count": d3.get("detection_3d_count"),
            "accepted_count": d3.get("detection_3d_count", 0) - d3.get("rejected_detection_count", 0),
            "rejected_count": d3.get("rejected_detection_count"),
        },
    }


def render_contact_sheet(baseline: dict, candidate: dict, output: Path) -> list[int]:
    count = baseline["capture"]["frame_count"]
    indexes = sorted(set([0, count // 4, count // 2, (3 * count) // 4, count - 1]))
    tile_w, tile_h, header = 640, 480, 44
    canvas = Image.new("RGB", (tile_w * 2, (tile_h + header) * len(indexes)), "white")
    draw = ImageDraw.Draw(canvas)
    for row, idx in enumerate(indexes):
        bframe, cframe = baseline["capture"]["frames"][idx], candidate["capture"]["frames"][idx]
        bdir = baseline["root"] / "capture" / bframe["frame_dir"]
        cdir = candidate["root"] / "capture" / cframe["frame_dir"]
        brgb_array = np.load(bdir / "rgb.npy", allow_pickle=False).astype(np.uint8)[..., :3]
        crgb_array = np.load(cdir / "rgb.npy", allow_pickle=False).astype(np.uint8)[..., :3]
        brgb = Image.fromarray(brgb_array)
        crgb = Image.fromarray(crgb_array)
        # Upscaling is only for legible display in this comparison image; raw captures remain untouched.
        brgb = brgb.resize((tile_w, tile_h), Image.Resampling.NEAREST)
        crgb = crgb.resize((tile_w, tile_h), Image.Resampling.NEAREST)
        y = row * (tile_h + header)
        draw.text((8, y + 12), f"160x120 · {bframe['frame_id']} · t={bframe['timestamp_s']:.1f}s (nearest display enlargement)", fill="black")
        draw.text((tile_w + 8, y + 12), f"320x240 · {cframe['frame_id']} · t={cframe['timestamp_s']:.1f}s (native capture)", fill="black")
        canvas.paste(brgb, (0, y + header))
        canvas.paste(crgb, (tile_w, y + header))
    canvas.save(output, format="PNG", optimize=True)
    return indexes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline, candidate = load_run(args.baseline.resolve()), load_run(args.candidate.resolve())
    ensure_pair(baseline, candidate)
    b_shapes, c_shapes = validate_capture_arrays(baseline), validate_capture_arrays(candidate)
    pose_checks = []
    intrinsic_checks = []
    for bf, cf in zip(baseline["capture"]["frames"], candidate["capture"]["frames"]):
        bm = read_json(baseline["root"] / "capture" / bf["frame_dir"] / "metadata.json")
        cm = read_json(candidate["root"] / "capture" / cf["frame_dir"] / "metadata.json")
        for key in ("route_entry", "timestamp_s", "sensor_position_habitat", "sensor_rotation_habitat"):
            if bm.get(key) != cm.get(key):
                raise ValueError(f"capture pose/extrinsics field {key} differs at {bf['frame_id']}")
        for key in ("base_pose_habitat", "base_pose_canonical"):
            if not np.allclose(np.asarray(bm[key]), np.asarray(cm[key]), rtol=0.0, atol=1e-6):
                raise ValueError(f"actual base pose {key} differs at {bf['frame_id']}")
        pose_checks.append(bf["frame_id"])
        for run, expected_w, expected_h in ((baseline, 160, 120), (candidate, 320, 240)):
            trace_path = run["root"] / "grid_trace/frames" / f"{bf['frame_index']:06d}.json"
            trace = read_json(trace_path)
            intr = trace.get("intrinsics", {})
            hfov = np.deg2rad(run["capture"]["horizontal_fov_deg"])
            expected = {"width": expected_w, "height": expected_h,
                        "fx": expected_w / (2 * np.tan(hfov / 2)),
                        "fy": expected_w / (2 * np.tan(hfov / 2)),
                        "cx": (expected_w - 1) / 2, "cy": (expected_h - 1) / 2}
            if int(intr.get("width", -1)) != expected_w or int(intr.get("height", -1)) != expected_h:
                raise ValueError(f"grid trace intrinsics resolution mismatch at {bf['frame_id']}")
            for key in ("fx", "fy", "cx", "cy"):
                if not np.isclose(float(intr.get(key, np.nan)), expected[key], rtol=1e-7, atol=1e-7):
                    raise ValueError(f"grid trace intrinsics {key} mismatch at {bf['frame_id']}: {intr.get(key)} != {expected[key]}")
            intrinsic_checks.append({"run_id": run["manifest"]["run_id"], "frame_id": bf["frame_id"],
                                     "intrinsics": {k: intr[k] for k in ("width", "height", "fx", "fy", "cx", "cy")}})
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    paired = render_contact_sheet(baseline, candidate, output / "resolution_side_by_side.png")
    report = {
        "schema_version": 1,
        "scene_id": baseline["capture"]["scene_id"],
        "baseline_run_id": baseline["manifest"]["run_id"],
        "candidate_run_id": candidate["manifest"]["run_id"],
        "route_aliases": [baseline["manifest"].get("route_alias"), candidate["manifest"].get("route_alias")],
        "capture_validation": {"baseline": b_shapes, "candidate": c_shapes},
        "paired_frame_indices": paired,
        "pose_extrinsics_match_frames": len(pose_checks),
        "intrinsics_verified_frames": intrinsic_checks,
        "metrics": {"baseline_160x120": metrics(baseline), "candidate_320x240": metrics(candidate)},
        "intrinsics_expected_from_90deg_hfov": {
            "160x120": {"fx": 80.0, "fy": 80.0, "cx": 79.5, "cy": 59.5},
            "320x240": {"fx": 160.0, "fy": 160.0, "cx": 159.5, "cy": 119.5},
        },
        "input_sha256": {
            "baseline_launcher_manifest": digest(baseline["root"] / "launcher_manifest.json"),
            "baseline_capture_manifest": digest(baseline["root"] / "capture/capture_manifest.json"),
            "baseline_route": digest(baseline["root"] / "effective_route_config.json"),
            "candidate_launcher_manifest": digest(candidate["root"] / "launcher_manifest.json"),
            "candidate_capture_manifest": digest(candidate["root"] / "capture/capture_manifest.json"),
            "candidate_route": digest(candidate["root"] / "effective_route_config.json"),
        },
        "limitations": ["Detection counts and graph counts are not accuracy metrics.",
                        "The 160x120 side of the contact sheet is enlarged only for display; raw captures are unchanged.",
                        "RSS is the replay runner Python process peak only; it does not represent total pipeline/simulator/child-process memory."],
    }
    (output / "manifest.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    bm, cm = report["metrics"]["baseline_160x120"], report["metrics"]["candidate_320x240"]
    rows = [
        ("Capture frames", bm["frames"], cm["frames"]),
        ("Detection2D", bm["detections_2d"], cm["detections_2d"]),
        ("Detection3D accepted", bm["detections_3d_accepted"], cm["detections_3d_accepted"]),
        ("Detection3D rejected", bm["detections_3d_rejected"], cm["detections_3d_rejected"]),
        ("ObjectNodes", bm["object_nodes"], cm["object_nodes"]),
        ("Regions (final M4 diagnostic)", bm["regions"], cm["regions"]),
        ("RoomNodes", bm["room_nodes"], cm["room_nodes"]),
        ("Predicted room labels", bm["predicted_room_labels"], cm["predicted_room_labels"]),
        ("Unknown room labels", bm["unknown_room_labels"], cm["unknown_room_labels"]),
        ("Stage runtime sum (s)", bm["stage_runtime_s"]["sum_processing_stages_s"], cm["stage_runtime_s"]["sum_processing_stages_s"]),
        ("Native replay Python peak RSS (MiB)", bm["native_replay_python_peak_rss_mib"], cm["native_replay_python_peak_rss_mib"]),
    ]
    lines = ["# So sánh resolution HM3D 00802", "",
             f"Baseline: `{report['baseline_run_id']}` (160×120); candidate: `{report['candidate_run_id']}` (320×240).",
             f"Scene: `{report['scene_id']}`; route aliases: `{report['route_aliases'][0]}` / `{report['route_aliases'][1]}`; cùng route/config trừ resolution metadata: đã xác minh.",
             "", "| Chỉ số | 160×120 | 320×240 |", "|---|---:|---:|"]
    lines.extend(f"| {name} | {a} | {b} |" for name, a, b in rows)
    lines += ["", "## Kiểm tra capture và intrinsics", "",
              f"RGB(A)/depth/valid mask được kiểm tra đủ {b_shapes['checked_frames']} frame ở mỗi run; shape là H×W×3 hoặc H×W×4 cho RGB(A), rồi H×W và H×W theo resolution đã khai báo.",
              "HFOV giữ 90°. Intrinsics pinhole dự kiến: 160×120 = fx/fy 80, cx 79.5, cy 59.5; 320×240 = fx/fy 160, cx 159.5, cy 119.5. Grid trace và capture metadata được giữ nguyên trong từng run để kiểm tra thêm.",
              "", "## Ảnh đối chiếu", "", "Mỗi hàng là cùng frame ID/timestamp. Ảnh 160×120 được phóng nearest-neighbor chỉ để trình bày; raw capture không bị resize. Ảnh 320×240 là capture native.",
              "", "![Cùng frame ở hai resolution](resolution_side_by_side.png)", "",
              "## Diễn giải", "",
              "Tăng resolution trong khi giữ P3 stride cố định làm tăng mật độ điểm ảnh/depth đưa vào các bước downstream. Nhiều detection, node hoặc nhãn hơn không tự chứng minh accuracy cao hơn. Detector weights/input size, thresholds, P3, M6, segmentation và semantic rules không được đổi cho phép so sánh này.",
              "RSS là peak memory của riêng tiến trình Python replay theo `resource.getrusage(RUSAGE_SELF)`, không đại diện tổng memory của simulator, tiến trình native con hay toàn pipeline. Runtime là tổng elapsed time các stage capture→evaluation ghi trong launcher manifest, không gồm stage tạo báo cáo so sánh.", ""]
    (output / "report.vi.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"status": "pass", "output": str(output), "paired_frames": paired}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

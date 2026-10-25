#!/usr/bin/env python3
"""Build an identity-only review gallery for true duplicate partition changes."""
from __future__ import annotations

import hashlib
import html
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from standalone_sg.objects.duplicate_partition_review import compare_duplicate_partitions  # noqa: E402

EXPERIMENT = ROOT / "artifacts/runs/u3_same_frame_dup_candidate_20261010_v6"
SCENE_RUNS = {
    "00800": ROOT / "artifacts/runs/hm3d_00800_u4ov_multiview_pilot_v2_640",
    "00802": Path("/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/artifacts/runs/hm3d_00802_20261006T163140.468551Z_e4a058"),
    "00808": ROOT / "artifacts/runs/u6_hm3d_00808_quality_v1_20261010",
}
OUT = ROOT / "artifacts/reviews/u3_duplicate_partition_review_v3"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha(path: Path) -> str:
    return sha(path.read_bytes())


def jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def safe_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def save_png(path: Path, array: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(array).save(path, format="PNG", optimize=False)


def panel_html(path: str, title: str) -> str:
    return f'<figure><img src="{html.escape(path)}"><figcaption>{html.escape(title)}</figcaption></figure>'


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=False)
    panels_root = OUT / "panels"
    all_deltas: list[dict] = []
    scene_summaries = {}
    review_cases = []
    validation_rows = []

    for scene, run in SCENE_RUNS.items():
        base = EXPERIMENT / scene
        old_rows = jsonl(base / "assignments_geometry_only.jsonl")
        new_rows = jsonl(base / "assignments_geometry_only_candidate.jsonl")
        deltas = compare_duplicate_partitions(old_rows, new_rows)
        counts = Counter(delta.kind for delta in deltas)
        old_group_count = len({(r["frame_index"], r["frame_id"], r.get("duplicate_of") or r["detection_id"])
                               for r in old_rows})
        changed_old_group_count = sum(len(delta.old_groups) for delta in deltas if delta.kind != "canonical_only")
        scene_summaries[scene] = {
            "input_run": str(run),
            "old_assignment_sha256": file_sha(base / "assignments_geometry_only.jsonl"),
            "candidate_assignment_sha256": file_sha(base / "assignments_geometry_only_candidate.jsonl"),
            "same_membership_same_canonical": old_group_count - counts["canonical_only"] - changed_old_group_count,
            "canonical_only_groups": counts["canonical_only"],
            "membership_expansion_or_merge_cases": counts["membership_expansion_or_merge"],
            "split_or_released_cases": counts["split_or_released"],
            "other_membership_cases": counts["membership_repartition"],
        }
        all_deltas.extend({"scene": scene, **delta.__dict__} for delta in deltas)

        p2_path = run / "detection_2d/detection_2d_index.json"
        capture_path = run / "capture/capture_manifest.json"
        p2 = json.loads(p2_path.read_text(encoding="utf-8"))
        capture = json.loads(capture_path.read_text(encoding="utf-8"))
        detections = {d["detection_id"]: d for f in p2["frames"] for d in f["detections"]}
        frames = {f["frame_id"]: f for f in capture["frames"]}

        for delta in deltas:
            if delta.kind == "canonical_only":
                continue
            obs_ids = sorted({obs for group in (*delta.old_groups, *delta.candidate_groups) for obs in group})
            if len(obs_ids) < 2:
                # A released singleton is still an identity-risk change; it is
                # displayed with the old/candidate grouping below.
                pass
            case_id = f"{scene}-{delta.frame_index:04d}-{safe_hash('|'.join(obs_ids))}"
            case_dir = panels_root / case_id
            case_dir.mkdir(parents=True, exist_ok=False)
            per_obs = []
            rgb_arrays = {}
            mask_arrays = {}
            for observation_id in obs_ids:
                det = detections.get(observation_id)
                if det is None:
                    raise ValueError(f"P2 observation missing: {observation_id}")
                frame_id = observation_id.split(":", 1)[0]
                frame_dir = run / "capture" / frames[frame_id]["frame_dir"]
                rgb_src = frame_dir / "rgb.npy"
                mask_src = run / "detection_2d" / det["mask_file"]
                rgb_bytes, mask_bytes = rgb_src.read_bytes(), mask_src.read_bytes()
                rgb_raw = np.asarray(np.load(rgb_src, allow_pickle=False), dtype=np.uint8)
                if rgb_raw.ndim == 3 and rgb_raw.shape[2] == 4:
                    # P2 consumes RGB; 00802 stores an opaque alpha channel.
                    if not np.all(rgb_raw[:, :, 3] == 255):
                        raise ValueError(f"non-opaque alpha in source RGB: {observation_id}")
                    rgb = rgb_raw[:, :, :3]
                    rgb_conversion = "RGBA source; alpha=255 verified; RGB channels rendered"
                else:
                    rgb = rgb_raw
                    rgb_conversion = "RGB source rendered directly"
                mask = np.asarray(np.load(mask_src, allow_pickle=False), dtype=bool)
                if rgb.ndim != 3 or rgb.shape[2] != 3 or mask.shape != rgb.shape[:2]:
                    raise ValueError(f"panel shape mismatch for {observation_id}: {rgb.shape}, {mask.shape}")
                if sha(mask_bytes) != det["mask_sha256"]:
                    raise ValueError(f"mask source hash mismatch: {observation_id}")
                rgb_arrays[observation_id] = rgb
                mask_arrays[observation_id] = mask
                token = safe_hash(observation_id)
                rgb_rel = f"panels/{case_id}/{token}__full_rgb.png"
                mask_rel = f"panels/{case_id}/{token}__pred_mask.png"
                masked_rel = f"panels/{case_id}/{token}__rgb_pred_mask.png"
                save_png(OUT / rgb_rel, rgb)
                mask_image = np.zeros((*mask.shape, 3), dtype=np.uint8)
                mask_image[mask] = (255, 255, 255)
                save_png(OUT / mask_rel, mask_image)
                overlay = rgb.copy()
                overlay[mask] = (0.45 * overlay[mask] + 0.55 * np.array([255, 50, 50])).astype(np.uint8)
                save_png(OUT / masked_rel, overlay)
                panels = {"full_rgb": rgb_rel, "prediction_mask": mask_rel, "rgb_with_prediction_mask": masked_rel}
                per_obs.append({
                    "observation_id": observation_id,
                    "frame_id": frame_id,
                    "frame_index": delta.frame_index,
                    "source_rgb_path": str(rgb_src),
                    "source_rgb_sha256": sha(rgb_bytes),
                    "source_rgb_shape": list(rgb_raw.shape),
                    "rgb_render_conversion": rgb_conversion,
                    "source_mask_path": str(mask_src),
                    "source_mask_sha256": sha(mask_bytes),
                    "source_mask_expected_sha256": det["mask_sha256"],
                    "panels": panels,
                })
                for kind, rel in panels.items():
                    decoded = np.asarray(Image.open(OUT / rel).convert("RGB"), dtype=np.uint8)
                    expected = rgb if kind == "full_rgb" else mask_image if kind == "prediction_mask" else overlay
                    if not np.array_equal(decoded, expected):
                        raise ValueError(f"rendered panel differs from source transform: {observation_id}/{kind}")
                    validation_rows.append({"case_id": case_id, "observation_id": observation_id,
                                            "frame_id": frame_id, "panel_type": kind,
                                            "output_path": rel, "output_sha256": file_sha(OUT / rel),
                                            "source_rgb_sha256": sha(rgb_bytes), "source_mask_sha256": sha(mask_bytes),
                                            "pixel_validation": "pass"})

            # Same-scale overlay: colors mark the two masks on the original RGB.
            pair_overlay_rel = f"panels/{case_id}/case__same_scale_pair_overlay.png"
            base_rgb = rgb_arrays[obs_ids[0]].copy()
            combined = base_rgb.copy()
            palette = [(255, 40, 40), (30, 230, 255), (255, 220, 30), (190, 50, 255)]
            for index, observation_id in enumerate(obs_ids):
                color = np.array(palette[index % len(palette)], dtype=np.uint8)
                mask = mask_arrays[observation_id]
                combined[mask] = (0.35 * combined[mask] + 0.65 * color).astype(np.uint8)
            save_png(OUT / pair_overlay_rel, combined)
            if any(rgb_arrays[o].shape != base_rgb.shape for o in obs_ids):
                raise ValueError(f"different RGB dimensions inside case {case_id}")
            if not np.array_equal(np.asarray(Image.open(OUT / pair_overlay_rel).convert("RGB")), combined):
                raise ValueError(f"pair overlay pixel validation failed: {case_id}")

            cards = []
            for row in per_obs:
                cards.append(f"<h4>{html.escape(row['observation_id'])}</h4><div class='panels'>" +
                             panel_html(row["panels"]["full_rgb"], "RGB full frame") +
                             panel_html(row["panels"]["prediction_mask"], "Prediction mask") +
                             panel_html(row["panels"]["rgb_with_prediction_mask"], "RGB + prediction mask") + "</div>")
            cards.append("<div class='panels'>" + panel_html(pair_overlay_rel, "Same-scale combined mask overlay") + "</div>")
            case_html = (f"<article><h2>{html.escape(case_id)}</h2><p>Frame: {html.escape(delta.frame_id)} ({delta.frame_index})</p>"
                         f"{''.join(cards)}"
                         "<p>Identity review: ☐ same physical object &nbsp; ☐ different objects &nbsp; ☐ uncertain</p></article>")
            review_cases.append({
                "case_id": case_id,
                "scene": scene,
                "frame_id": delta.frame_id,
                "frame_index": delta.frame_index,
                "kind": delta.kind,
                "old_groups": [list(x) for x in delta.old_groups],
                "candidate_groups": [list(x) for x in delta.candidate_groups],
                "observation_ids": obs_ids,
                "observations": per_obs,
                "pair_overlay": pair_overlay_rel,
                "html": case_html,
            })

    # Output paths must belong to exactly one observation and one panel type.
    output_paths = [row["output_path"] for row in validation_rows]
    if len(output_paths) != len(set(output_paths)):
        raise ValueError("a rendered observation panel path was reused")

    with (OUT / "partition_deltas.jsonl").open("w", encoding="utf-8") as handle:
        for row in all_deltas:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with (OUT / "panel_validation.jsonl").open("w", encoding="utf-8") as handle:
        for row in validation_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    (OUT / "case_manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "purpose": "human identity review of membership-changing same-frame duplicate groups",
        "predictor_annotations_used": False,
        "case_count": len(review_cases),
        "panel_observation_count": sum(len(c["observation_ids"]) for c in review_cases),
        "cases": [{k: v for k, v in c.items() if k not in {"html", "kind", "old_groups", "candidate_groups"}}
                  for c in review_cases],
        "scenes": scene_summaries,
        "panel_validation": {"status": "pass", "individual_panel_count": len(validation_rows),
                             "unique_output_path_count": len(set(output_paths)),
                             "pixel_exact_transform_check": True},
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (OUT / "internal_partition_case_mapping.json").write_text(json.dumps({
        "purpose": "analysis-only mapping from blinded review case IDs to changed partitions",
        "cases": [{k: v for k, v in c.items() if k in {"case_id", "scene", "frame_id", "frame_index", "kind",
                                                            "old_groups", "candidate_groups", "observation_ids"}}
                  for c in review_cases],
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    html_doc = """<!doctype html><meta charset="utf-8"><title>U3 duplicate partition identity review v1</title>
<style>body{font:15px system-ui,sans-serif;max-width:1400px;margin:2rem auto;padding:0 1rem;color:#202124}article{border:1px solid #bbb;border-radius:8px;padding:1rem;margin:1.5rem 0}figure{margin:.4rem;max-width:32%;flex:1}img{width:100%;height:auto;border:1px solid #777}figcaption{font-weight:600}.panels{display:flex;align-items:flex-start;gap:.5rem;flex-wrap:wrap}h4{margin-bottom:.2rem}</style>
<h1>Same-frame duplicate identity review</h1><p>Review identity only. Predicted classes, GT identity, and predicted grouping are hidden. For each case choose same physical object, different objects, or uncertain. Frames are reviewed independently; no cross-frame identity is implied.</p>
""" + "\n".join(c["html"] for c in review_cases)
    (OUT / "index.html").write_text(html_doc, encoding="utf-8")
    report = {
        "status": "review_package_ready",
        "experiment_input": str(EXPERIMENT),
        "review_gallery": str(OUT / "index.html"),
        "case_count": len(review_cases),
        "scene_summary": scene_summaries,
        "scope_note": "Only same-frame membership changes are included; canonical-only swaps are excluded from identity review.",
    }
    (OUT / "summary.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

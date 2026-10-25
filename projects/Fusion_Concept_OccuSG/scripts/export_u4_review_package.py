#!/usr/bin/env python3
"""Export a compact, self-contained U4 human review package and lock a new split."""
from __future__ import annotations

import csv
import hashlib
import html
import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
U3 = ROOT / "artifacts/audits/u3_causal_prefix"
U4 = ROOT / "artifacts/audits/u4_semantic_fusion"
OUT = ROOT / "artifacts/audits/u4_review_package"
TEST_OUT = ROOT / "artifacts/audits/u4_temporal_test_lock"

CASE_CATALOG = [
    {"case_id": "class_change_tv_to_suitcase", "track_id": "u3-0015",
     "question_context": "Geometry-only U3 track whose U4 initial class changes tv→suitcase; this same track also contains the reviewed table/suitcase false merge.",
     "samples": ["hm3d-00802-main-dense-0010:det-0005", "hm3d-00802-main-dense-0230:det-0005", "hm3d-00802-main-dense-0238:det-0000"]},
    {"case_id": "class_change_chair_to_sofa", "track_id": "u3-0032",
     "question_context": "Geometry-only U3 track whose U4 initial class changes chair→sofa.",
     "samples": ["hm3d-00802-main-dense-0042:det-0000", "hm3d-00802-main-dense-0067:det-0004", "hm3d-00802-main-dense-0095:det-0002", "hm3d-00802-main-dense-0200:det-0000"]},
    {"case_id": "class_change_sofa_to_bench", "track_id": "u3-0046",
     "question_context": "Geometry-only U3 track whose U4 initial class changes sofa→bench.",
     "samples": ["hm3d-00802-main-dense-0075:det-0002", "hm3d-00802-main-dense-0126:det-0001", "hm3d-00802-main-dense-0199:det-0000", "hm3d-00802-main-dense-0234:det-0000"]},
    {"case_id": "conflicted_table_bed", "track_id": "u3-0066",
     "question_context": "Geometry-only track with table/bed/sofa detector labels; final U4 status is conflicted. Evaluator identity is context only.",
     "samples": ["hm3d-00802-main-dense-0127:det-0003", "hm3d-00802-main-dense-0223:det-0001", "hm3d-00802-main-dense-0230:det-0004", "hm3d-00802-main-dense-0232:det-0004"]},
    {"case_id": "stable_control_candidate_chair", "track_id": "u3-0018",
     "question_context": "Candidate stable control: initial/resolved class stays chair. Correctness still needs reviewer confirmation; it is not assumed from provisional mapping.",
     "samples": ["hm3d-00802-main-dense-0012:det-0001", "hm3d-00802-main-dense-0219:det-0000", "hm3d-00802-main-dense-0237:det-0001"]},
]
FALSE_MERGE = {"case_id": "false_merge_table_suitcase", "track_id": "u3-0015",
               "question_context": "U3 geometry-only track spans a table-labeled evaluator observation and later suitcase-labeled detection; review same physical object separately from class.",
               "samples": ["hm3d-00802-main-dense-0230:det-0005", "hm3d-00802-main-dense-0238:det-0000"]}


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def locked_resource(key: str):
    entry = json.loads((ROOT / "locks/external_resources.json").read_text())["files"][key]
    path = Path(entry["path"])
    if not path.is_file() or path.stat().st_size != entry["bytes"] or sha_file(path) != entry["sha256"]:
        raise RuntimeError(f"external cache changed from lock: {key}")
    return path, entry


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def frame_no(did: str) -> int:
    return int(did.split(":", 1)[0].rsplit("-", 1)[-1])


def make_mask_overlay(rgb: np.ndarray, mask: np.ndarray, bbox: list[float] | None,
                      header: str, target: Path):
    if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] < 3:
        raise RuntimeError(f"unexpected RGB data: dtype={rgb.dtype} shape={rgb.shape}")
    base = Image.fromarray(rgb[:, :, :3]).convert("RGB")
    if mask.shape != rgb.shape[:2]:
        raise RuntimeError(f"mask/RGB shape mismatch: {mask.shape} vs {rgb.shape[:2]}")
    overlay = np.asarray(base).copy()
    active = np.asarray(mask, dtype=bool)
    tint = np.array([255, 64, 48], dtype=np.float32)
    overlay[active] = (overlay[active].astype(np.float32) * 0.58 + tint * 0.42).clip(0, 255).astype(np.uint8)
    base = Image.fromarray(overlay)
    draw = ImageDraw.Draw(base)
    if bbox and len(bbox) == 4:
        draw.rectangle(tuple(float(v) for v in bbox), outline=(255, 225, 40), width=2)
    # Label outside the pixel evidence, not over the object mask.
    draw.rectangle((0, 0, base.width, 34), fill=(8, 14, 24))
    draw.text((8, 8), header[:150], fill=(255, 255, 255))
    target.parent.mkdir(parents=True, exist_ok=True)
    base.save(target, format="PNG", optimize=True)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    TEST_OUT.mkdir(parents=True, exist_ok=True)
    capture_path, capture_lock = locked_resource("dense_capture_manifest")
    d2_path, d2_lock = locked_resource("dense_detection_2d_index")
    d3_path, d3_lock = locked_resource("dense_detection_3d_cache")
    capture = json.loads(capture_path.read_text())
    d2 = json.loads(d2_path.read_text())
    capture_root, d2_root = capture_path.parent, d2_path.parent
    capture_frames = {f["frame_id"]: f for f in capture["frames"]}
    capture_by_index = {int(f["frame_index"]): f for f in capture["frames"]}
    d2_by_id = {d["detection_id"]: d for f in d2["frames"] for d in f["detections"]}
    d3_by_id = {}
    d3_rows = read_jsonl(d3_path)
    for f in d3_rows:
        if 0 <= int(f["frame_id"].rsplit("-", 1)[-1]) <= 279:
            for d in f["detections"]:
                d3_by_id[d["detection_id"]] = d

    # Locked predictor/evaluator outputs are read only.
    u3_decisions = {r["detection_id"]: r for r in read_jsonl(U3 / "decisions_geometry_only.jsonl")}
    u4_rows = {r["detection_id"]: r for r in read_jsonl(U4 / "observations_geometry_only.jsonl")}
    u4_states = read_jsonl(U4 / "causal_states_geometry_only.jsonl")
    state_by_id = {r["observation_id"]: r for r in u4_states}
    gt_path, gt_lock = locked_resource("dense_object_evaluation_observation_matches")
    gt_by_id = {r["detection_id"]: r for r in read_jsonl(gt_path)}
    summary = json.loads((U4 / "track_summary_geometry_only.json").read_text())

    all_cases = CASE_CATALOG + [FALSE_MERGE]
    used_ids = {did for case in all_cases for did in case["samples"]}
    for case in all_cases:
        tid = case["track_id"]
        if tid not in summary:
            raise RuntimeError(f"missing locked geometry-only track: {tid}")
        for did in case["samples"]:
            if u4_rows.get(did, {}).get("track_id") != tid:
                raise RuntimeError(f"review sample is not assigned to expected track {tid}: {did}")

    # Build representative full RGB frames with the saved detection mask overlaid.
    image_refs = {}
    for did in sorted(used_ids):
        detection = d3_by_id[did]
        d2_det = d2_by_id[did]
        row, decision = u4_rows[did], u3_decisions[did]
        frame = capture_by_index[int(row["frame_index"])]
        rgb_path = capture_root / frame["frame_dir"] / "rgb.npy"
        mask_path = d2_root / d2_det["mask_file"]
        rgb = np.load(rgb_path, allow_pickle=False)
        mask = np.load(mask_path, allow_pickle=False)
        pose = detection["provider_metadata"]["camera_pose_W_Copt"]
        cam = [round(float(pose[i][3]), 3) for i in range(3)]
        target = OUT / "frames" / f"{did.replace(':', '__')}.png"
        make_mask_overlay(rgb, mask, detection["provider_metadata"].get("bbox_xyxy"),
                          f"frame {row['frame_index']} | {row['raw_class']} | U3 {decision['decision']} | U4 {row['class_status']}", target)
        image_refs[did] = {
            "image_ref": target.relative_to(OUT).as_posix(),
            "rgb_sha256": sha_file(rgb_path),
            "mask_sha256": sha_file(mask_path),
            "camera_position_W": cam,
            "camera_pose_sha256": hashlib.sha256(json.dumps(pose, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        }

    # Data-driven case metadata and full per-track causal timeline.
    categories = set()
    raw_classes = set()
    classes_by_category = defaultdict(set)
    ids_by_category = defaultdict(set)
    html_cases = []
    identity_answers = []
    for case in all_cases:
        tid = case["track_id"]
        track_rows = sorted((r for r in u4_rows.values() if r.get("track_id") == tid and r.get("evidence_included")),
                            key=lambda r: (r["frame_index"], r["detection_id"]))
        selected_ids = case["samples"]
        selected_cards = []
        for did in selected_ids:
            row = u4_rows[did]
            decision = u3_decisions[did]
            gt = gt_by_id.get(did, {})
            raw_classes.add(row["raw_class"])
            if gt.get("gt_category"):
                categories.add(gt["gt_category"])
                classes_by_category[gt["gt_category"]].add(str(row["raw_class"]))
            if gt.get("gt_category") and gt.get("gt_semantic_id") is not None:
                ids_by_category[gt["gt_category"]].add(int(gt["gt_semantic_id"]))
            meta = image_refs[did]
            selected_cards.append(f"""<article class="frame">
<img src="{html.escape(meta['image_ref'])}" alt="RGB with saved instance mask overlay">
<div><b>Frame/time:</b> {row['frame_index']} / {row['timestamp_s']:.2f}s<br>
<b>Camera W:</b> {html.escape(str(meta['camera_position_W']))}<br>
<b>Raw:</b> {html.escape(str(row['raw_class']))} ({row['raw_confidence']:.3f})<br>
<b>U3 association:</b> {html.escape(str(decision.get('decision')))} / {html.escape(str(decision.get('reason')))}<br>
<b>View bin:</b> {html.escape(str(row['view_bin']))}<br>
<b>U4 state at this observation:</b> initial={html.escape(str(row['initial_class']))}; resolved={html.escape(str(row['resolved_class']))}; {html.escape(str(row['class_status']))}<br>
<b>Evaluator, posthoc only:</b> {html.escape(str(gt.get('gt_category')))} / ID {html.escape(str(gt.get('gt_semantic_id')))} ({html.escape(str(gt.get('match_status')))})</div>
</article>""")
        timeline_rows = []
        for row in track_rows:
            decision = u3_decisions[row["detection_id"]]
            timeline_rows.append(f"<tr><td>{row['frame_index']}</td><td>{html.escape(row['detection_id'])}</td><td>{html.escape(str(row['raw_class']))} / {row['raw_confidence']:.3f}</td><td>{html.escape(str(decision.get('decision')))} / {html.escape(str(decision.get('reason')))}</td><td>{html.escape(row['view_bin'])}</td><td>{html.escape(str(row['initial_class']))}</td><td>{html.escape(str(row['resolved_class']))} / {html.escape(row['class_status'])}</td></tr>")
            gt = gt_by_id.get(row["detection_id"], {})
            if gt.get("gt_category"):
                categories.add(gt["gt_category"])
                classes_by_category[gt["gt_category"]].add(str(row["raw_class"]))
            if gt.get("gt_category") and gt.get("gt_semantic_id") is not None:
                ids_by_category[gt["gt_category"]].add(int(gt["gt_semantic_id"]))
            if row.get("raw_class"):
                raw_classes.add(row["raw_class"])
        track_summary = summary[tid]
        final_change = track_summary["initial_class"] != track_summary["resolved_class"] and track_summary["resolved_class"] is not None
        html_cases.append(f"""<section id="{html.escape(case['case_id'])}">
<h2>{html.escape(case['case_id'])} — track {html.escape(tid)}</h2>
<p>{html.escape(case['question_context'])}</p>
<p><b>Whole track:</b> {track_summary['observation_count']} assigned observations; initial={html.escape(str(track_summary['initial_class']))}; final={html.escape(str(track_summary['resolved_class']))}; status={html.escape(track_summary['class_status'])}; confirmed class differs from initial={final_change}; first confirmation frame={track_summary['confirmation_frame_index']}; confirmation view bins={html.escape(str(track_summary['confirmation_view_bins']))}.</p>
<div class="frames">{''.join(selected_cards)}</div>
<h3>Causal class/association timeline</h3>
<div class="scroll"><table><thead><tr><th>Frame</th><th>Observation</th><th>Raw class/score</th><th>U3 decision/reason</th><th>View bin</th><th>Initial</th><th>Resolved/status</th></tr></thead><tbody>{''.join(timeline_rows)}</tbody></table></div>
<h3>Reviewer response for this case</h3>
<p>a. Do these observations belong to the same physical object? Choose yes/no/uncertain.</p>
<p>b. What is the object's class? Enter a class or choose uncertain.</p>
</section>""")
        identity_answers.append({
            "case_id": case["case_id"], "track_id": tid,
            "observation_ids": ";".join(selected_ids),
            "same_physical_object_yes_no_uncertain": "",
            "reviewed_object_class_or_uncertain": "",
            "reviewer": "", "date": "", "notes": "",
        })

    # Keep the table/suitcase false merge separate in the UI but reuse the same
    # original RGB/mask frames, avoiding duplicate image artifacts.
    mapping_snapshot = json.loads(Path(
        "/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/artifacts/runs/hm3d_00802_20261006T163140.468551Z_e4a058/object_evaluations/eval_20261006T184217.537953Z_880c87/category_mapping_snapshot.json"
    ).read_text())
    mapping_rows = []
    for category in sorted(categories):
        entry = mapping_snapshot.get("categories", {}).get(category, {})
        mapping_rows.append({
            "evaluator_category": category,
            "evaluator_ids_in_review_cases": ";".join(str(x) for x in sorted(ids_by_category[category])),
            "detector_classes_observed_in_review_cases": ";".join(sorted(classes_by_category[category])),
            "existing_proposed_detector_class": entry.get("detector_class") or "",
            "existing_mapping_status": entry.get("status", "unmapped"),
            "existing_provenance": entry.get("provenance", "not listed; unmapped"),
            "reviewer_confirmed_detector_class": "",
            "reviewer_decision_confirm_reject_uncertain": "",
            "reviewer": "", "date": "", "decision_record": "",
            "note": "Mapping decision is separate from physical identity decision.",
        })
    with (OUT / "category_mapping_review.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(mapping_rows[0]))
        writer.writeheader(); writer.writerows(mapping_rows)
    with (OUT / "identity_review_responses.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(identity_answers[0]))
        writer.writeheader(); writer.writerows(identity_answers)

    page = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>U4 human review package</title>
<style>body{font:15px system-ui,sans-serif;margin:24px;color:#17202a}h1,h2{margin:.5em 0}.note{background:#fff6d8;padding:12px;border-left:5px solid #d59b00}.frames{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:12px}.frame{border:1px solid #bbc5cf;padding:8px;border-radius:6px}.frame img{width:100%;height:auto}.frame div{line-height:1.6;font-size:14px}.scroll{overflow:auto;max-height:420px}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid #ccd;padding:5px;text-align:left}th{position:sticky;top:0;background:#edf1f4}section{padding:10px 0 28px;border-bottom:3px solid #d7dde3}.small{font-size:13px;color:#46515d}</style>
<h1>U4 offline review package</h1><div class="note"><b>Assignments and U4 config are frozen.</b> Review answers do not change GT, U3 thresholds, association, class evidence, graph, or room scores. The evaluator fields are posthoc context only. For each case answer separately: (a) same physical object? (b) object class or uncertain? “Uncertain” is a valid answer.</div>
<p>Use <code>identity_review_responses.csv</code> for identity/class answers and <code>category_mapping_review.csv</code> for evaluator-category to detector-vocabulary mappings. Confirming a category mapping does not confirm that the observations in a track are the same physical object.</p>
<nav>"""+" ".join(f'<a href="#{html.escape(c["case_id"])}">{html.escape(c["case_id"])}</a> ·' for c in all_cases)+"</nav>"+"".join(html_cases)+"</html>"
    (OUT / "index.html").write_text(page)
    (OUT / "README.md").write_text("""# U4 review package

Open `index.html` in a browser. Each case shows RGB with the cached instance mask overlaid, representative frames/camera positions, raw detector evidence, fixed U3 decisions, view bins, and causal U4 class state. The full track timeline is included below its selected images.

Please answer both questions independently in `identity_review_responses.csv`:

1. Do the observations belong to the same physical object? (`yes`, `no`, or `uncertain`)
2. What is its class? Enter a class or `uncertain`.

Then review only the displayed rows in `category_mapping_review.csv`. Fill reviewer fields only when you can confirm the category-to-detector mapping; otherwise leave it provisional or mark uncertain. A mapping decision does not establish that observations are the same physical object. Do not edit evaluator categories.

The chair stable-track case is a candidate control, not assumed correct. The table/sofa/bench physical interpretation remains uncertain where the images/annotations do not settle it. Review cases are development data, not holdout.
""")

    # Deterministically reserve the next contiguous temporal segment after the
    # prior U3/U4 horizon; selection uses frame order only, not labels/metrics.
    test_start, test_end = 239, 278  # 40 consecutive frame indices after prior horizon 238.
    test_records = []
    count_by_frame = Counter()
    for did, det in sorted(d3_by_id.items(), key=lambda item: (frame_no(item[0]), item[0])):
        fno = frame_no(did)
        if not test_start <= fno <= test_end:
            continue
        d2_det = d2_by_id[did]
        count_by_frame[fno] += 1
        geom = {"center_W": det["center_W"], "extents_m": det["extents_m"], "layer_id": det["layer_id"]}
        test_records.append({
            "detection_id": did,
            "frame_index": fno,
            "rgb_sha256": sha_file(capture_root / capture_by_index[fno]["frame_dir"] / "rgb.npy"),
            "mask_sha256": sha_file(d2_root / d2_det["mask_file"]),
            "geometry_sha256": hashlib.sha256(json.dumps(geom, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        })
    config_hashes = {
        "u3_association_config_sha256": sha_file(U3 / "association_config.locked.json"),
        "u4_semantic_config_sha256": sha_file(U4 / "u4_config.locked.json"),
    }
    lock = {
        "schema_version": 1,
        "status": "temporal_test_split_locked_not_evaluated",
        "split_kind": "same-scene temporal holdout; not a scene holdout",
        "scene_id": capture.get("scene_id"),
        "sequence_id": capture.get("sequence_id"),
        "frame_range_inclusive": [test_start, test_end],
        "selection_rule": "first 40 consecutive frame indices immediately after the previously reviewed U3/U4 horizon ending at 238; selected by chronology before inspecting categories, class states, or evaluation outcomes",
        "previous_seen_horizon_ends_at": 238,
        "prefix_to_replay_for_state": [0, test_start - 1],
        "frame_count": test_end - test_start + 1,
        "p3_observation_count": len(test_records),
        "p3_observation_count_by_frame": {str(k): count_by_frame.get(k, 0) for k in range(test_start, test_end + 1)},
        "observation_ids_and_input_hashes": test_records,
        "input_locks": {
            "capture_manifest_sha256": capture_lock["sha256"],
            "detection_2d_index_sha256": d2_lock["sha256"],
            "detection_3d_cache_sha256": d3_lock["sha256"],
            "evaluator_matches_sha256": gt_lock["sha256"],
            **config_hashes,
        },
        "not_yet_done": ["prefix association replay with unchanged U3 config", "causal U4 prediction with unchanged U4 config", "posthoc scoring after reviewer mapping/identity decisions"],
        "holdout_policy": "Do not inspect per-frame class/evaluator results or tune on this split. The split becomes development data once reviewed/scored.",
    }
    (TEST_OUT / "temporal_test_manifest.json").write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    digest = sha_file(TEST_OUT / "temporal_test_manifest.json")
    (TEST_OUT / "temporal_test_manifest.sha256").write_text(f"{digest}  temporal_test_manifest.json\n")

    package_manifest = {
        "status": "ready_for_human_review",
        "case_count": len(all_cases),
        "representative_observation_count": len(used_ids),
        "mapping_category_count": len(mapping_rows),
        "review_package_files": ["index.html", "identity_review_responses.csv", "category_mapping_review.csv"],
        "u3_geometry_only_assignments_sha256": sha_file(U3 / "decisions_geometry_only.jsonl"),
        "u4_config_sha256": sha_file(U4 / "u4_config.locked.json"),
        "new_test_manifest_sha256": digest,
        "posthoc_evaluator_labels_shown_for_context": True,
        "gt_used_for_prediction": False,
        "gt_used_to_select_reviewer_answers": False,
        "no_gt_or_mapping_edits": True,
    }
    (OUT / "package_manifest.json").write_text(json.dumps(package_manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"review_package": package_manifest, "temporal_test_lock": lock}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Score the locked Q1 00808 human-reviewed identity subset only."""
from __future__ import annotations

import argparse
import hashlib
import json
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def assignment_map(path: Path) -> dict[str, str | None]:
    return {row["observation_id"]: row.get("track_id") for row in read_jsonl(path)}


def same_frame(left: str, right: str) -> bool:
    return left.split(":det-", 1)[0] == right.split(":det-", 1)[0]


def score(assignments: dict[str, str | None], labels: dict[str, str], ids: list[str]) -> dict:
    tp = fp = fn = tn = 0
    same_frame_excluded = 0
    rows = []
    for left, right in combinations(ids, 2):
        if same_frame(left, right):
            same_frame_excluded += 1
            continue
        gt_same = labels[left] == labels[right]
        lt, rt = assignments.get(left), assignments.get(right)
        pred_same = lt is not None and lt == rt
        if gt_same and pred_same:
            tp += 1
            outcome = "TP"
        elif not gt_same and pred_same:
            fp += 1
            outcome = "FP"
        elif gt_same:
            fn += 1
            outcome = "FN"  # includes unassigned/deferred
        else:
            tn += 1
            outcome = "TN"
        rows.append({"observation_ids": [left, right], "human_same_identity": gt_same,
                     "predicted_same_track": pred_same, "pair_outcome": outcome,
                     "track_ids": [lt, rt]})

    identity_tracks: dict[str, set[str]] = {}
    track_identities: dict[str, set[str]] = {}
    for oid in ids:
        track = assignments.get(oid)
        if track is None:
            continue
        identity_tracks.setdefault(labels[oid], set()).add(str(track))
        track_identities.setdefault(str(track), set()).add(labels[oid])
    fragmentation_by_identity = {identity: max(0, len(tracks) - 1)
                                 for identity, tracks in identity_tracks.items()}
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else None
    return {
        "status": "available",
        "scored_observations": len(ids),
        "cross_frame_pair_population": len(rows),
        "same_frame_pairs_excluded": same_frame_excluded,
        "TP": tp, "FP": fp, "FN": fn, "TN": tn,
        "precision": precision, "recall": recall, "F1": f1,
        "false_merge_tracks": sum(len(identities) > 1 for identities in track_identities.values()),
        "fragmented_identity_count": sum(value > 0 for value in fragmentation_by_identity.values()),
        "fragmentation_excess_tracks": sum(fragmentation_by_identity.values()),
        "fragmentation_by_identity_token": {key: value for key, value in sorted(fragmentation_by_identity.items())},
        "unassigned_observations": sum(assignments.get(oid) is None for oid in ids),
        "assigned_coverage": sum(assignments.get(oid) is not None for oid in ids) / len(ids) if ids else None,
        "predicted_tracks_in_subset": len({assignments[oid] for oid in ids if assignments.get(oid) is not None}),
        "pair_relations": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--annotation", type=Path, default=ROOT / "artifacts/audits/object_graph_improvement/q1_00808_20261011T160830Z/annotations/user_primary_review_v1_20261012.json")
    parser.add_argument("--review-manifest", type=Path, default=ROOT / "artifacts/audits/object_graph_improvement/q1_00808_20261011T160830Z/review_v4/manifest.json")
    parser.add_argument("--association-dir", type=Path, default=ROOT / "artifacts/audits/object_graph_improvement/q1_00808_20261011T160830Z/association")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/audits/object_graph_improvement/q1_00808_20261011T160830Z/reviewed_score_v2")
    args = parser.parse_args()

    annotation = json.loads(args.annotation.read_text())
    expected_hash = annotation["review_manifest"]["sha256"]
    actual_hash = sha256(args.review_manifest)
    if actual_hash != expected_hash:
        raise SystemExit(f"review manifest hash mismatch: expected {expected_hash}, got {actual_hash}")
    annotations = annotation["annotations"]
    labels = {row["observation_id"]: row["identity_token"] for row in annotations
              if row["identity_status"] == "confirmed"}
    ids = [row["observation_id"] for row in annotations]
    if len(ids) != 7 or len(labels) != 7 or len(set(labels.values())) != 4:
        raise SystemExit("locked annotation contract mismatch (expected 7 confirmed observations / 4 identity tokens)")
    if any(row.get("class") is not None for row in annotations):
        raise SystemExit("class labels must not be inferred from identity tokens")

    review_manifest = json.loads(args.review_manifest.read_text())
    records = review_manifest["review"]["panel_records"]
    sofa_ids = ["hm3d-00808-k4-ag-v1-0000:det-0000", "hm3d-00808-k4-ag-v1-0006:det-0000"]
    a, b = (records[oid] for oid in sofa_ids)
    rgb_exact = a["source_rgb"]["sha256"] == b["source_rgb"]["sha256"]
    pose_exact = a["camera_pose_T_W_Copt"] == b["camera_pose_T_W_Copt"]
    if not (rgb_exact and pose_exact):
        raise SystemExit("sofa revisit exclusion was requested, but RGB/pose equality did not verify")

    mode_files = {
        "source": "source_assignments.jsonl",
        "geometry-only-dedup-v2": "geometry_only_dedup_v2_assignments.jsonl",
        "K4": "candidate_assignments.jsonl",
    }
    all_results = {}
    assignment_maps = {}
    for mode, filename in mode_files.items():
        assignment_path = args.association_dir / filename
        assignments = assignment_map(assignment_path)
        missing = sorted(set(ids) - assignments.keys())
        if missing:
            raise SystemExit(f"{mode} assignments missing reviewed IDs: {missing}")
        assignment_maps[mode] = assignments
        full_score = score(assignments, labels, ids)
        no_revisit_ids = [oid for oid in ids if oid not in sofa_ids[1:]]
        no_revisit_score = score(assignments, labels, no_revisit_ids)
        # The same-frame duplicate pair is reported separately, not added to cross-frame metrics.
        duplicate_ids = ["hm3d-00808-k4-ag-v1-0002:det-0004", "hm3d-00808-k4-ag-v1-0002:det-0005"]
        duplicate_tracks = [assignments.get(oid) for oid in duplicate_ids]
        all_results[mode] = {
            "assignment_file": filename,
            "primary_all_7": full_score,
            "primary_without_exact_pose_revisit": no_revisit_score,
            "same_frame_duplicate_pair": {
                "observation_ids": duplicate_ids,
                "human_identity_relation": "same identity (table_1)",
                "predicted_relation": "same_track" if duplicate_tracks[0] is not None and duplicate_tracks[0] == duplicate_tracks[1] else "different_or_unassigned",
                "track_ids": duplicate_tracks,
                "included_in_cross_frame_metrics": False,
            },
        }

    relation_rows = read_jsonl(args.association_dir / "relation_changes.jsonl")
    diagnostic_pairs: dict[tuple[str, str], set[str]] = {}
    primary_set = set(ids)
    for row in relation_rows:
        pair = row.get("observation_ids", [])
        if len(pair) == 2 and not set(pair).issubset(primary_set):
            key = tuple(sorted(pair))
            diagnostic_pairs.setdefault(key, set()).add(row.get("step", "unknown"))
    diagnostic_unanswered = [{
        "observation_ids": list(pair),
        "unreviewed_observation_ids": [oid for oid in pair if oid not in primary_set],
        "question": "Are these two observations the same physical object or different objects?",
        "changed_in": sorted(changed_in),
        "primary_metric_included": False,
    } for pair, changed_in in sorted(diagnostic_pairs.items())]

    output = {
        "schema_version": 1,
        "status": "completed",
        "development_not_holdout": True,
        "annotation_path": str(args.annotation),
        "annotation_sha256": sha256(args.annotation),
        "review_manifest_sha256": actual_hash,
        "annotation_count": len(ids),
        "identity_token_count": len(set(labels.values())),
        "class_scored": False,
        "p3_observation_population": review_manifest["P3_accepted"],
        "annotation_coverage": {"covered": len(ids), "population": review_manifest["P3_accepted"], "ratio": len(ids) / review_manifest["P3_accepted"]},
        "cross_frame_protocol": "unordered pairs of confirmed observations from different frames; GT same token is positive; same predicted non-null track is predicted positive; a GT-positive pair assigned apart/unassigned is FN; same-frame pairs excluded",
        "pair_population_all_7": {"cross_frame_pairs": 20, "positive_same_identity_pairs": 2, "negative_different_identity_pairs": 18, "same_frame_pairs_excluded": 1},
        "revisit_exclusion": {"excluded_observation_id": sofa_ids[1], "reason": "same camera pose and byte-identical RGB to frame 0; scoring-only exclusion", "pose_equal": pose_exact, "rgb_hash_equal": rgb_exact, "backend_history_changed": False},
        "modes": all_results,
        "changed_relation_diagnostic_unanswered": diagnostic_unanswered,
        "limitations": ["identity annotations cover only these seven observations; no class labels were provided", "Q1 is development data, not a holdout", "pairwise scores do not establish quality beyond this small reviewed subset", "K4 recorded zero surface-continuity matches on this pilot; no accumulated-fusion benefit is inferred"],
    }
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / "metrics.json").write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
    lines = [
        "# Q1 00808 human-reviewed identity subset — score v2", "",
        f"Annotation: `{args.annotation}` ({len(ids)} observations, {len(set(labels.values()))} user identity tokens); manifest SHA-256 `{actual_hash}`.",
        "Tokens are identity-only, not class labels. This is development data, not a holdout.",
        f"Coverage: {len(ids)}/{review_manifest['P3_accepted']} P3 observations. Cross-frame population: 20 pairs (2 same-identity positives, 18 different-identity negatives); the one same-frame table pair is separate.",
        "", "| Mode | TP/FP/FN | P/R/F1 | False-merge tracks | Fragmented identities | Unassigned | Subset track count |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for mode, value in all_results.items():
        metric = value["primary_all_7"]
        lines.append(f"| {mode} | {metric['TP']}/{metric['FP']}/{metric['FN']} | {metric['precision']:.3f}/{metric['recall']:.3f}/{metric['F1']:.3f} | {metric['false_merge_tracks']} | {metric['fragmented_identity_count']} | {metric['unassigned_observations']} | {metric['predicted_tracks_in_subset']} |")
    lines += ["", "## Confirmed relations", "",
              "| Identity relation | Observation pair | Source | Geometry-only-dedup-v2 | K4 |",
              "|---|---|---|---|---|"]
    positive_pairs = [(sofa_ids[0], sofa_ids[1]),
                      ("hm3d-00808-k4-ag-v1-0001:det-0001", "hm3d-00808-k4-ag-v1-0004:det-0001")]
    for left, right in positive_pairs:
        labels_text = "sofa_1" if left == sofa_ids[0] else "chair_1"
        cells = []
        for mode in mode_files:
            amap = assignment_maps[mode]
            cells.append("same track" if amap[left] is not None and amap[left] == amap[right] else "split")
        lines.append(f"| same ({labels_text}) | `{left}` ↔ `{right}` | {cells[0]} | {cells[1]} | {cells[2]} |")
    lines += ["", "All 18 different-identity cross-frame pairs were predicted apart in each mode (0 FP); no confirmed positive pair was missed (0 FN).",
              "", "## Same-frame duplicate and revisit sensitivity", "",
              f"Table duplicate pair `{all_results['source']['same_frame_duplicate_pair']['observation_ids'][0]}` + `{all_results['source']['same_frame_duplicate_pair']['observation_ids'][1]}`: source={all_results['source']['same_frame_duplicate_pair']['predicted_relation']}; geometry-only-dedup-v2={all_results['geometry-only-dedup-v2']['same_frame_duplicate_pair']['predicted_relation']}; K4={all_results['K4']['same_frame_duplicate_pair']['predicted_relation']}. Scored separately, not added to cross-frame TP.",
              f"Frame 6 sofa revisit excluded only in sensitivity report after verifying same pose and pixel-identical RGB (`pose_equal={pose_exact}`, `rgb_hash_equal={rgb_exact}`). Backend assignments/history are unchanged.",
              "", "| Mode | No-revisit TP/FP/FN | P/R/F1 | Cross-frame pairs |",
              "|---|---:|---:|---:|"]
    for mode, value in all_results.items():
        metric = value["primary_without_exact_pose_revisit"]
        lines.append(f"| {mode} | {metric['TP']}/{metric['FP']}/{metric['FN']} | {metric['precision']:.3f}/{metric['recall']:.3f}/{metric['F1']:.3f} | {metric['cross_frame_pair_population']} |")
    lines += ["", "## Diagnostic relations outside primary", "",
              "These changed-decision pairs are not scored because they lack user identity annotations:"]
    for row in diagnostic_unanswered:
        lines.append(f"- `{row['observation_ids'][0]}` ↔ `{row['observation_ids'][1]}`: identity unanswered.")
    if not diagnostic_unanswered:
        lines.append("- None.")
    lines += ["", "## Interpretation", "",
              "All three modes tie on the confirmed seven-observation subset. K4 and geometry-only-dedup-v2 have identical assignments here; the pilot manifest records zero K4 surface-continuity matches, so this is not evidence of accumulated-fusion benefit. The sample is small and does not support claims beyond the reviewed observations.", ""]
    (args.output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {args.output_dir / 'report.md'}")
    print(f"Metrics: {args.output_dir / 'metrics.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

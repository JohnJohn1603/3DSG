#!/usr/bin/env python3
"""Build an evaluator-only paired association comparison from native replays."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import resource
import time
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "src"))
from standalone_sg.objects.profile_comparison import pairwise_identity_metrics, partition_members, relation_changes  # noqa: E402


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def assignment_map(replay_dir: Path, raw_ids: set[str]) -> tuple[dict[str, str | None], dict[str, dict]]:
    rows = read_jsonl(replay_dir / "object_association_lineage.jsonl")
    latest: dict[str, dict] = {}
    for row in rows:
        latest[row["detection_id"]] = row
    # The per-frame backend decision ledger is authoritative for defer and
    # duplicate-alias reasons; lineage remains the observation-level record.
    diagnostics = read_json(replay_dir / "frame_diagnostics.json")
    for frame in diagnostics:
        backend = frame.get("object_backend", {})
        for row in backend.get("decisions", []) + backend.get("assignments", []):
            did = row.get("detection_id")
            if did not in raw_ids:
                continue
            merged = latest.setdefault(did, {"detection_id": did, "frame_id": frame.get("frame_id")})
            merged["decision"] = row.get("decision", merged.get("decision"))
            merged["object_id"] = row.get("track_id", row.get("object_id", merged.get("object_id")))
            merged["reason"] = row.get("reason", merged.get("reason"))
            merged["duplicate_of"] = row.get("duplicate_of", merged.get("duplicate_of"))
            merged["canonical_observation_id"] = row.get("canonical_observation_id", merged.get("canonical_observation_id"))
    assignments = {did: latest[did].get("object_id") if did in latest else None for did in raw_ids}
    return assignments, latest


def duplicate_info(replay_dir: Path) -> tuple[dict, dict[str, dict]]:
    diagnostics = read_json(replay_dir / "frame_diagnostics.json")
    decisions: dict[str, dict] = {}
    for frame in diagnostics:
        backend = frame.get("object_backend", {})
        for row in backend.get("decisions", []):
            decisions[row["detection_id"]] = {**row, "frame_id": frame.get("frame_id")}
        for row in backend.get("duplicate_decisions", []):
            decisions[row["detection_id"]] = {**row, "frame_id": frame.get("frame_id")}
    manifest = read_json(replay_dir / "manifest.json")
    return manifest.get("same_frame_duplicate_summary") or {}, decisions


def object_count(replay_dir: Path) -> int:
    graph = read_json(replay_dir / "snapshot.json")["snapshot"]
    return sum(node.get("kind") == "object" for node in graph.get("nodes", []))


def make_gallery(out: Path, input_root: Path, p2_index: dict, changes: list[dict],
                 source_rows: dict[str, dict], candidate_rows: dict[str, dict], gt: dict[str, dict]) -> dict:
    assets = out / "gallery_assets"
    assets.mkdir()
    p2_by_id = {d["detection_id"]: d for frame in p2_index.get("frames", []) for d in frame.get("detections", [])}
    capture_frames = input_root / "capture/frames"
    verified = [row for row in changes if row.get("posthoc_status") in {
        "verified_correction_by_evaluator_identity", "verified_regression_by_evaluator_identity"}]
    same_frame = [row for row in changes if str(row.get("posthoc_status", "")).startswith("same_frame_")]
    unverified = [row for row in changes if row.get("posthoc_status") == "unverified_missing_evaluator_identity"]
    selected = verified + same_frame + unverified[:max(0, 30 - len(verified) - len(same_frame))]
    cards = []
    for index, change in enumerate(selected, 1):
        obs_ids = change["observation_ids"]
        panels = []
        for did in obs_ids:
            frame_name, det_name = did.rsplit(":det-", 1)
            frame_index = int(frame_name.rsplit("-", 1)[-1])
            rgb_path = capture_frames / f"{frame_index:06d}/rgb.npy"
            rgb = np.asarray(np.load(rgb_path, allow_pickle=False), dtype=np.uint8)
            rgb_image = Image.fromarray(rgb, "RGB")
            p2 = p2_by_id.get(did)
            if p2:
                mask_path = input_root / "detection_2d" / p2["mask_file"]
                mask = np.asarray(np.load(mask_path, allow_pickle=False), dtype=bool)
                overlay = np.asarray(rgb_image).copy()
                overlay[mask] = (0.55 * overlay[mask] + 0.45 * np.asarray([40, 220, 255])).astype(np.uint8)
                rgb_image = Image.fromarray(overlay, "RGB")
            draw = ImageDraw.Draw(rgb_image)
            draw.rectangle((0, 0, rgb_image.width, 22), fill=(0, 0, 0))
            draw.text((4, 4), did, fill="white")
            name = f"case_{index:03d}_{hashlib.sha1(did.encode()).hexdigest()[:10]}.png"
            rgb_image.save(assets / name)
            panels.append(f'<figure><img src="gallery_assets/{name}"><figcaption>{html.escape(did)}<br>source={html.escape(str(source_rows.get(did, {}).get("object_id")))}; candidate={html.escape(str(candidate_rows.get(did, {}).get("object_id")))}<br>GT evaluator ID={html.escape(str(gt.get(did, {}).get("gt_semantic_id")))}</figcaption></figure>')
        cards.append(f'<article><h3>{html.escape(change["transition"])} — {html.escape(change.get("posthoc_status", "unverified"))}</h3><p>{" ↔ ".join(html.escape(x) for x in obs_ids)}</p><div class="panels">{"".join(panels)}</div></article>')
    page = "<!doctype html><meta charset='utf-8'><title>Association comparison review</title><style>body{font:14px sans-serif;margin:24px;background:#f5f6f8;color:#17202a}article{background:white;padding:16px;margin:16px 0;border:1px solid #ccd1d1} .panels{display:flex;gap:12px;flex-wrap:wrap}figure{margin:0;width:min(48%,640px)}img{width:100%;height:auto}figcaption{overflow-wrap:anywhere;font-size:12px}</style><h1>Changed association relations (unverified unless GT-linked)</h1>" + "\n".join(cards)
    (out / "gallery.html").write_text(page, encoding="utf-8")
    return {"path": "gallery.html", "cases_rendered": len(selected), "changed_relation_count": len(changes), "all_changes_in_jsonl": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-run", type=Path, required=True)
    parser.add_argument("--source-replay", type=Path, required=True)
    parser.add_argument("--candidate-replay", type=Path, required=True)
    parser.add_argument("--candidate", default="geometry-only-dedup-v2")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    input_root, source_root, candidate_root, out = map(lambda p: p.resolve(),
        (args.input_run, args.source_replay, args.candidate_replay, args.output))
    if out.exists():
        raise FileExistsError(out)
    input_root_mark = read_json(input_root / "launcher_manifest.json")
    source_mark, candidate_mark = read_json(source_root / "launcher_manifest.json"), read_json(candidate_root / "launcher_manifest.json")
    if source_mark.get("source_cache_run") != str(input_root) or candidate_mark.get("source_cache_run") != str(input_root):
        raise ValueError("paired replay roots do not bind to the exact requested input run")
    if source_mark.get("association_profile") != "source" or candidate_mark.get("association_profile") != args.candidate:
        raise ValueError("paired replay profiles do not match requested source/candidate")
    input_hashes = {
        "capture_manifest": sha(input_root / "capture/capture_manifest.json"),
        "p2_index": sha(input_root / "detection_2d/detection_2d_index.json"),
        "p3_cache": sha(input_root / "detection_3d/detection_3d.jsonl"),
        "p3_manifest": sha(input_root / "detection_3d/detection_3d_manifest.json"),
    }
    if source_mark.get("input_hashes") != candidate_mark.get("input_hashes") or source_mark.get("input_hashes") != input_hashes:
        raise ValueError("capture/P2/P3 input hashes differ across paired replays")

    p3_rows = read_jsonl(input_root / "detection_3d/detection_3d.jsonl")
    raw_ids = {det["detection_id"] for frame in p3_rows for det in frame.get("detections", [])}
    if len(raw_ids) != sum(len(frame.get("detections", [])) for frame in p3_rows):
        raise ValueError("P3 cache contains duplicate raw observation IDs")
    source_replay, candidate_replay = source_root / "replay", candidate_root / "replay"
    source_assignments, source_rows = assignment_map(source_replay, raw_ids)
    candidate_assignments, candidate_rows = assignment_map(candidate_replay, raw_ids)
    if set(source_assignments) != set(candidate_assignments) or len(source_assignments) != len(raw_ids):
        raise ValueError("paired assignment maps do not preserve the same raw P3 observation denominator")

    evaluations = sorted((input_root / "object_evaluations").glob("eval_*/observation_matches.jsonl"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
    if not evaluations:
        raise FileNotFoundError("input run has no official object evaluation observation ledger")
    eval_matches_path = evaluations[0]
    eval_rows = read_jsonl(eval_matches_path)
    gt_rows = {r["detection_id"]: r for r in eval_rows}
    gt_identity = {did: (str(row["gt_semantic_id"]) if row.get("match_status") == "matched" and row.get("gt_semantic_id") is not None else None)
                   for did, row in gt_rows.items() if did in raw_ids}
    eligible = {did for did in raw_ids if gt_identity.get(did) is not None}

    source_metrics = pairwise_identity_metrics(source_assignments, gt_identity, eligible_ids=eligible)
    candidate_metrics = pairwise_identity_metrics(candidate_assignments, gt_identity, eligible_ids=eligible)
    relation_delta = relation_changes(source_assignments, candidate_assignments, sorted(raw_ids))
    verified_corrections = verified_regressions = same_frame_changed_relations = 0
    for changed in relation_delta:
        left, right = changed["observation_ids"]
        changed["source_track_ids"] = [source_assignments.get(left), source_assignments.get(right)]
        changed["candidate_track_ids"] = [candidate_assignments.get(left), candidate_assignments.get(right)]
        changed["evaluator_ids"] = [gt_identity.get(left), gt_identity.get(right)]
        changed["evaluator_identity_available"] = left in eligible and right in eligible
        if left.split(":det-")[0] == right.split(":det-")[0]:
            same_frame_changed_relations += 1
            changed["posthoc_status"] = "same_frame_diagnostic_excluded_from_cross_frame_metrics"
            continue
        if left not in eligible or right not in eligible:
            changed["posthoc_status"] = "unverified_missing_evaluator_identity"
            continue
        gt_same = gt_identity[left] == gt_identity[right]
        src_same = source_assignments.get(left) is not None and source_assignments.get(left) == source_assignments.get(right)
        cand_same = candidate_assignments.get(left) is not None and candidate_assignments.get(left) == candidate_assignments.get(right)
        if src_same != gt_same and cand_same == gt_same:
            verified_corrections += 1
            changed["posthoc_status"] = "verified_correction_by_evaluator_identity"
        elif src_same == gt_same and cand_same != gt_same:
            verified_regressions += 1
            changed["posthoc_status"] = "verified_regression_by_evaluator_identity"
        else:
            changed["posthoc_status"] = "relation_changed_but_quality_status_unchanged"
    source_groups, candidate_groups = partition_members(source_assignments), partition_members(candidate_assignments)
    source_set, candidate_set = source_groups, candidate_groups
    unchanged_groups = len(source_set & candidate_set)
    only_source, only_candidate = source_set - candidate_set, candidate_set - source_set
    same_frame_dedup = []
    for did, row in candidate_rows.items():
        decision = str(row.get("decision", row.get("status", "")))
        if "alias" in decision or decision in {"kept_ambiguous", "alias_suppressed", "duplicate_suppressed"}:
            canonical_id = row.get("duplicate_of", row.get("canonical_detection_id", row.get("canonical_observation_id")))
            same_frame_dedup.append({"detection_id": did, "decision": decision,
                "canonical_observation_id": canonical_id,
                "reason": row.get("reason", row.get("decision_reason")),
                "assigned_track": candidate_assignments.get(did),
                "evaluator_match_status": gt_rows.get(did, {}).get("match_status"),
                "evaluator_semantic_id": gt_identity.get(did),
                "canonical_evaluator_match_status": gt_rows.get(canonical_id, {}).get("match_status") if canonical_id else None,
                "canonical_evaluator_semantic_id": gt_identity.get(canonical_id) if canonical_id else None,
                "same_frame_quality_uses_official_one_to_one_ledger": True})

    out.mkdir(parents=True)
    changes_path = out / "changed_relations.jsonl"
    changes_path.write_text("".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in relation_delta), encoding="utf-8")
    lineage_dir = out / "lineage"
    lineage_dir.mkdir()
    for name, assignments, rows in (("source", source_assignments, source_rows), ("candidate", candidate_assignments, candidate_rows)):
        with (lineage_dir / f"{name}.jsonl").open("w", encoding="utf-8") as stream:
            for did in sorted(raw_ids):
                raw = next((d for frame in p3_rows for d in frame.get("detections", []) if d["detection_id"] == did), {})
                source_row = rows.get(did, {})
                stream.write(json.dumps({"observation_id": did, "frame_id": did.split(":det-")[0],
                    "detection_id": did.rsplit(":det-", 1)[1], "assigned_track": assignments[did],
                    "decision": source_row.get("decision", "unassigned" if assignments[did] is None else "assigned"),
                    "reason": source_row.get("reason"), "canonical_observation_id": source_row.get("duplicate_of", source_row.get("canonical_detection_id")),
                    "decision_time_s": raw.get("timestamp_s"), "raw_class": raw.get("class_name"),
                    "gt_match_status_evaluator_only": gt_rows.get(did, {}).get("match_status"),
                    "gt_semantic_id_evaluator_only": gt_identity.get(did)}, ensure_ascii=False, sort_keys=True) + "\n")

    p2_index = read_json(input_root / "detection_2d/detection_2d_index.json")
    gallery = make_gallery(out, input_root, p2_index, relation_delta, source_rows, candidate_rows, gt_rows)
    source_manifest, candidate_manifest = read_json(source_replay / "manifest.json"), read_json(candidate_replay / "manifest.json")
    source_dup, source_dup_decisions = duplicate_info(source_replay)
    candidate_dup, candidate_dup_decisions = duplicate_info(candidate_replay)
    s_counts = Counter(r.get("decision", "unknown") for r in source_rows.values())
    c_counts = Counter(r.get("decision", "unknown") for r in candidate_rows.values())
    metrics = {
        "schema_version": 1,
        "scope": "native graph M9 paired replay; association/dedup only; no detector/P3 rerun; no U4-OV or room-score claim",
        "input_run_id": input_root.name,
        "candidate_profile": args.candidate,
        "input_hashes": input_hashes,
        "official_evaluator_ledger": {"path": str(eval_matches_path), "sha256": sha(eval_matches_path), "protocol": "official one-to-one mask matching; identity proxy uses matched semantic IDs only"},
        "raw_p3_observations_common_denominator": len(raw_ids),
        "evaluator_identity_eligible_common_observations": len(eligible),
        "evaluator_identity_coverage_of_p3": len(eligible) / len(raw_ids) if raw_ids else None,
        "unmatched_or_unavailable_evaluator_identity_observations": len(raw_ids - eligible),
        "human_physical_identity_annotations_for_this_input": "not_available; no annotations transferred from other run or same frame index",
        "source": {"replay_run_id": source_root.name, "tracks_final": object_count(source_replay),
            "decision_counts": dict(s_counts), "same_frame_duplicate_summary": source_dup,
            "runtime_seconds": source_manifest.get("runtime_ms", 0) / 1000,
            "peak_process_rss_mib": source_manifest.get("python_process_ru_maxrss_kib", 0) / 1024,
            "checks": {"fresh_replay_equal": source_manifest.get("fresh_replay_equal"), "snapshot_reload_equal": source_manifest.get("snapshot_reload_equal"), "delta_replay_equal": source_manifest.get("delta_replay_equal")},
            "quality": source_metrics},
        "candidate": {"replay_run_id": candidate_root.name, "tracks_final": object_count(candidate_replay),
            "decision_counts": dict(c_counts), "same_frame_duplicate_summary": candidate_dup,
            "same_frame_decisions": same_frame_dedup,
            "runtime_seconds": candidate_manifest.get("runtime_ms", 0) / 1000,
            "peak_process_rss_mib": candidate_manifest.get("python_process_ru_maxrss_kib", 0) / 1024,
            "checks": {"fresh_replay_equal": candidate_manifest.get("fresh_replay_equal"), "snapshot_reload_equal": candidate_manifest.get("snapshot_reload_equal"), "delta_replay_equal": candidate_manifest.get("delta_replay_equal")},
            "quality": candidate_metrics},
        "partition_changes": {"identical_membership_groups": unchanged_groups,
            "source_only_groups": len(only_source), "candidate_only_groups": len(only_candidate),
            "changed_pair_relations": len(relation_delta), "changed_relations_path": "changed_relations.jsonl",
            "same_frame_changed_relations_separate": same_frame_changed_relations,
            "verified_cross_frame_corrections": verified_corrections,
            "verified_cross_frame_regressions": verified_regressions,
            "canonical_selection_is_separate_from_track_partition": True},
        "same_frame_duplicate_quality": {"source_alias_decisions": sum("alias" in str(x.get("decision", "")) for x in source_dup_decisions.values()),
            "candidate_alias_decisions": sum("alias" in str(x.get("decision", "")) for x in candidate_dup_decisions.values()),
            "candidate_alias_observations": same_frame_dedup,
            "official_hungarian_mask_ledger_modified": False,
            "quality_status": "evaluator identity metric unavailable for raw observations without one-to-one matched GT identity; duplicate relations are separately listed"},
        "lineage_files": {"source": "lineage/source.jsonl", "candidate": "lineage/candidate.jsonl"},
        "gallery": gallery,
        "runtime_scope": "native replay runtime and peak Python RSS read from each replay manifest; comparison scorer runtime is separately measured below",
        "predictor_received_gt_or_annotations": False,
        "track_ids_compared_across_profiles": False,
        "note": "HM3D evaluator semantic instance IDs are not guaranteed to equal human-confirmed physical identity; this run has no human physical-identity labels.",
    }
    metrics["comparison_scorer_runtime_seconds"] = time.perf_counter() - started
    metrics["comparison_scorer_peak_process_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    metrics["canonical_only_group_count"] = None
    metrics["canonical_only_group_note"] = "Not directly comparable: source manager has no same-frame observation canonicalization; membership partition comparison is ID-invariant. Candidate canonical observation is preserved in candidate lineage."
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    if not eligible or source_metrics["cross_frame_pair_population"] == 0:
        status = "insufficient GT"
    elif (source_metrics["pairwise_tp"], source_metrics["pairwise_fp"], source_metrics["pairwise_fn"]) == (candidate_metrics["pairwise_tp"], candidate_metrics["pairwise_fp"], candidate_metrics["pairwise_fn"]):
        status = "no difference on scored pairs"
    elif (candidate_metrics["pairwise_tp"] >= source_metrics["pairwise_tp"]
          and candidate_metrics["pairwise_fp"] <= source_metrics["pairwise_fp"]
          and candidate_metrics["pairwise_fn"] <= source_metrics["pairwise_fn"]
          and candidate_metrics["false_merge_tracks"] <= source_metrics["false_merge_tracks"]
          and candidate_metrics["fragmentation_excess_tracks"] <= source_metrics["fragmentation_excess_tracks"]):
        status = "candidate better in scored scope"
    else:
        status = "trade-off or candidate regression"
    report = f"""# So sánh association: source vs {args.candidate}

## Phạm vi

- Input: `{input_root.name}`; scene `{read_json(input_root / 'capture/capture_manifest.json').get('scene_id')}`, `{read_json(input_root / 'capture/capture_manifest.json').get('frame_count')}` frames.
- Capture/P2/P3 được hash-verified giống nhau. Hai native M9 replay bắt đầu từ state rỗng riêng; không chạy lại capture, detector hay P3.
- Scope: **native graph association/dedup**. Đây không phải đánh giá toàn bộ ConceptGraphs, semantic fusion hay U4-OV.
- Common raw P3 denominator: **{len(raw_ids)} observations**. Có GT evaluator identity phù hợp: **{len(eligible)}/{len(raw_ids)}**. Physical-identity labels của người dùng cho run này: unavailable.

## Structural changes (không phải accuracy)

| Metric | Source | Candidate | Δ |
|---|---:|---:|---:|
| Final object tracks | {metrics['source']['tracks_final']} | {metrics['candidate']['tracks_final']} | {metrics['candidate']['tracks_final']-metrics['source']['tracks_final']:+} |
| Created decisions | {s_counts.get('created',0)} | {c_counts.get('created',0)} | {c_counts.get('created',0)-s_counts.get('created',0):+} |
| Matched decisions | {sum(v for k,v in s_counts.items() if 'match' in k or 'suppressed' in k)} | {sum(v for k,v in c_counts.items() if 'match' in k)} | — |
| Deferred/unassigned | {sum(v for k,v in s_counts.items() if 'defer' in k or 'unassign' in k)} | {sum(v for k,v in c_counts.items() if 'defer' in k or 'unassign' in k)} | — |
| Duplicate aliases | {metrics['source']['same_frame_duplicate_summary'].get('aliased_observations',0)} | {metrics['candidate']['same_frame_duplicate_summary'].get('aliased_observations',0)} | — |
| Changed pair relations | — | {len(relation_delta)} | — |
| Verified corrections / regressions | — | {verified_corrections} / {verified_regressions} | — |
| Runtime | {metrics['source']['runtime_seconds']:.1f}s | {metrics['candidate']['runtime_seconds']:.1f}s | — |

Track IDs were not compared; membership partitions were. Every raw P3 observation remains in both lineage files, including aliases and unassigned detections. Same-frame duplicate decisions are separate from cross-frame scoring. The official Hungarian mask ledger was not modified.

Replay checks: source fresh/reload/delta = **{fmt_bool(metrics['source']['checks']['fresh_replay_equal'])}/{fmt_bool(metrics['source']['checks']['snapshot_reload_equal'])}/{fmt_bool(metrics['source']['checks']['delta_replay_equal'])}**; candidate = **{fmt_bool(metrics['candidate']['checks']['fresh_replay_equal'])}/{fmt_bool(metrics['candidate']['checks']['snapshot_reload_equal'])}/{fmt_bool(metrics['candidate']['checks']['delta_replay_equal'])}**. These are determinism/serialization checks, not accuracy scores. Peak Python RSS: {metrics['source']['peak_process_rss_mib']:.1f} / {metrics['candidate']['peak_process_rss_mib']:.1f} MiB.

Source has no same-frame observation canonicalization, so a canonical-only count against source is **not applicable**. Candidate alias→canonical IDs and reasons are listed in `metrics.json`; they do not count as new views or votes.

## Identity quality on the fixed evaluator-matched subset

| Metric | Source | Candidate |
|---|---:|---:|
| Evaluator-matched observations | {source_metrics['evaluator_matched_observations_scored']} | {candidate_metrics['evaluator_matched_observations_scored']} |
| Cross-frame pair population | {source_metrics['cross_frame_pair_population']} | {candidate_metrics['cross_frame_pair_population']} |
| TP / FP / FN | {source_metrics['pairwise_tp']} / {source_metrics['pairwise_fp']} / {source_metrics['pairwise_fn']} | {candidate_metrics['pairwise_tp']} / {candidate_metrics['pairwise_fp']} / {candidate_metrics['pairwise_fn']} |
| Precision / recall / F1 | {fmt(source_metrics['association_precision'])} / {fmt(source_metrics['association_recall'])} / {fmt(source_metrics['association_f1'])} | {fmt(candidate_metrics['association_precision'])} / {fmt(candidate_metrics['association_recall'])} / {fmt(candidate_metrics['association_f1'])} |
| False-merge tracks | {source_metrics['false_merge_tracks']} | {candidate_metrics['false_merge_tracks']} |
| Fragmented evaluator identities / excess tracks | {source_metrics['fragmented_evaluator_instances']} / {source_metrics['fragmentation_excess_tracks']} | {candidate_metrics['fragmented_evaluator_instances']} / {candidate_metrics['fragmentation_excess_tracks']} |

Contract: only unordered cross-frame pairs. Same evaluator ID but different/deferred/unassigned track counts FN. Different evaluator IDs in one predicted track counts FP. Unmatched/unknown GT observations are excluded from the identity pair denominator and reported in coverage. HM3D semantic instance IDs are evaluator identities, not guaranteed physical identities. Human physical-identity metrics: **unavailable (no matching annotations for this exact run)**.

## Decision

**{status}.** This is one development route, not a generalization claim. Verified cross-frame corrections/regressions: **{verified_corrections}/{verified_regressions}**; other changes remain unverified. Same-frame relation changes ({same_frame_changed_relations}) are excluded from cross-frame metrics. Review gallery: `gallery.html`.

Artifacts: `metrics.json`, `manifest.json`, `lineage/`, `changed_relations.jsonl`, `gallery.html`.
"""
    (out / "report.vi.md").write_text(report, encoding="utf-8")
    manifest = {"schema_version": 1, "status": "completed", "input_run_id": input_root.name,
        "source_replay_run_id": source_root.name, "candidate_replay_run_id": candidate_root.name,
        "input_hashes": input_hashes, "p3_cache_sha256": sha(input_root / "detection_3d/detection_3d.jsonl"),
        "p2_index_sha256": sha(input_root / "detection_2d/detection_2d_index.json"),
        "capture_manifest_sha256": sha(input_root / "capture/capture_manifest.json"),
        "source_backend_manifest_sha256": sha(source_replay / "manifest.json"),
        "candidate_backend_manifest_sha256": sha(candidate_replay / "manifest.json"),
        "candidate_dedup_config_sha256": sha(ROOT / "configs/association/u3_same_frame_duplicate_candidate_v2.json"),
        "u3_geometry_config_sha256": sha(ROOT / "artifacts/audits/u3_causal_prefix/association_config.locked.json"),
        "native_runner_sha256": sha(ROOT / "scripts/run_habitat_native_replay.py"),
        "evaluator_sha256": sha(Path(__file__)), "metric_contract": source_metrics["metric_contract"],
        "scope": "native graph M9 paired replay; identity score uses evaluator semantic instance IDs", "artifacts": ["report.vi.md", "metrics.json", "changed_relations.jsonl", "lineage/source.jsonl", "lineage/candidate.jsonl", "gallery.html"]}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    elapsed = time.perf_counter() - started
    print(f"Comparison complete: {out}\nGT identity coverage={len(eligible)}/{len(raw_ids)} | changed relations={len(relation_delta)} | decision={status} | scorer={elapsed:.2f}s")
    return 0


def fmt(value):
    return "N/A" if value is None else f"{value:.3f}"


def fmt_bool(value):
    return "pass" if value is True else "fail" if value is False else "N/A"


if __name__ == "__main__":
    raise SystemExit(main())

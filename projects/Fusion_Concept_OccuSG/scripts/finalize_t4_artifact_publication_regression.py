#!/usr/bin/env python3
"""Finish HTTP verification for an already-published T4 artifact regression.

This helper is deliberately read-only with respect to branch/graph/evidence
artifacts. It does not run join, semantic update, mapper, perception or capture.
It only verifies the existing publication through localhost HTTP, then records
the result and hashes in the regression run manifest.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from integration_v2 import live_processing as lp  # noqa: E402
from scripts.run_t4_artifact_publication_regression import (  # noqa: E402
    _file_hashes,
    _http_read_all,
    sha,
    write_json,
)


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _verify_existing(output: Path, source_run: Path) -> tuple[dict, dict, dict, dict]:
    manifest_path = output / "run_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = _load(manifest_path)
    if manifest.get("execution_mode") != "artifact_driven_regression":
        raise ValueError("output is not an artifact-driven regression")
    if manifest.get("source_run_id") != _load(source_run / "run_manifest.json").get("run_id"):
        raise ValueError("source run identity differs from regression manifest")
    if sha(source_run / "run_manifest.json") != manifest.get("source_run_manifest_sha256"):
        raise ValueError("source run manifest hash changed")

    frame_lines = (output / "joined/frames.jsonl").read_text(encoding="utf-8").splitlines()
    if len(frame_lines) != 1:
        raise ValueError("regression must contain exactly one joined frame")
    frame = json.loads(frame_lines[0])
    packet = _load(output / "packets/frame-000000.json")
    overlay = _load(output / "semantics/overlays/rev-000001.json")
    viewer = output / "viewer"
    renderer_path = viewer / "xyz_renderer.js"
    if not renderer_path.is_file() or sha(renderer_path) != sha(ROOT / "scripts/xyz_renderer.js"):
        raise ValueError("viewer XYZ renderer is missing or differs from the pinned workspace asset")
    index = _load(viewer / "live_index.json")
    head = _load(viewer / "live_head.json")
    graph_path = lp._resolve_viewer_artifact_url(output, viewer, frame["unified_url"], label="unified_url")
    overlay_path = output / "semantics/overlays/rev-000001.json"

    for key in ("frame_id", "frame_index", "timestamp_s"):
        if packet.get(key) != frame.get(key):
            raise ValueError(f"frame/packet identity mismatch at {key}")
    if packet.get("source_hashes") != manifest.get("source_hashes"):
        raise ValueError("packet source hashes differ from locked regression manifest")
    if overlay.get("run_id") != manifest.get("source_run_id"):
        raise ValueError("semantic overlay is not bound to the source run")
    for key in ("scene_id", "sequence_id", "frame_id", "frame_index", "timestamp_s"):
        expected = packet.get(key)
        if overlay.get(key) != expected:
            raise ValueError(f"semantic overlay identity differs from source packet at {key}")
    if frame.get("unified_sha256") != sha(graph_path):
        raise ValueError("published unified graph hash does not match the graph artifact")
    if overlay.get("graph_sha256") != frame.get("unified_sha256") or overlay.get("frame_id") != frame.get("frame_id"):
        raise ValueError("semantic overlay is not bound to the published graph/frame")
    if overlay.get("scope_status") != "unavailable" or overlay.get("room_contains_edges"):
        raise ValueError("verified-only unavailable scope produced membership edges")
    if overlay.get("room_semantics_availability", {}).get("status") != "not_scored":
        raise ValueError("expected executed verified-only update to report not_scored")
    if frame.get("room_belief_status") != "not_scored":
        raise ValueError("joined frame does not expose semantic not_scored state")
    if len(index.get("frames", [])) != 1 or head.get("revision_count") != 1:
        raise ValueError("live index/head does not expose exactly one unified revision")
    if head.get("status") != "completed" or head.get("live_index_sha256") != sha(viewer / "live_index.json"):
        raise ValueError("live head is not terminal or does not bind the live index")

    # Verify the old run is still the same source and that copied immutable
    # packet/branch/capture inputs in this output match their source bytes.
    copied = ["packets/frame-000000.json", "packets/joined-000000.json", "packets/p2-000000.json"]
    source_packet = _load(source_run / "packets/frame-000000.json")
    source_frame = source_run / "capture" / source_packet["capture_record"]["frame_dir"]
    out_frame = output / "capture" / source_packet["capture_record"]["frame_dir"]
    copied.extend(str(Path("capture") / source_packet["capture_record"]["frame_dir"] / n)
                  for n in ("rgb.npy", "depth_m.npy", "valid_mask.npy", "metadata.json"))
    for relative in copied:
        if sha(source_run / relative) != sha(output / relative):
            raise ValueError(f"copied regression source differs from failed run: {relative}")
    for branch in ("objects", "rooms"):
        original_root = source_run / "branches" / branch
        copied_root = output / "branches" / branch
        # Join acknowledgements are regression outputs; all source branch
        # state/event/geometry bytes must remain identical.
        original_files = {p.relative_to(original_root).as_posix(): sha(p) for p in original_root.rglob("*")
                          if p.is_file() and not p.relative_to(original_root).as_posix().startswith("acks/")}
        copied_files = {p.relative_to(copied_root).as_posix(): sha(p) for p in copied_root.rglob("*")
                        if p.is_file() and not p.relative_to(copied_root).as_posix().startswith("acks/")}
        if original_files != copied_files:
            raise ValueError(f"copied {branch} branch artifacts differ from failed run")
    return manifest, frame, overlay, {"viewer": viewer, "index": index, "head": head,
                                     "graph_path": graph_path, "overlay_path": overlay_path}


def finalize(output: Path, source_run: Path) -> dict:
    output = output.resolve()
    source_run = source_run.resolve()
    manifest, frame, overlay, refs = _verify_existing(output, source_run)
    manifest.update({
        "status": "publication_http_pending",
        "semantic_update_executed": True,
        "semantic_status": "not_scored",
        "semantic_reason": overlay.get("scope_reason"),
        "unified_revisions": 1,
        "branch_updates_reused": {"objects": 1, "rooms": 1},
        "mapper_updates_executed": 0,
        "perception_executed": False,
        "capture_executed": False,
        "source_inputs_unchanged": True,
        "publication_artifacts_validated_before_http": True,
        "viewer_asset_provenance": {"xyz_renderer.js": {
            "source": "scripts/xyz_renderer.js", "sha256": sha(ROOT / "scripts/xyz_renderer.js")}},
    })
    write_json(output / "run_manifest.json", manifest)
    http_result = _http_read_all(output, refs["viewer"], frame, frame["live_evidence"])
    manifest.update({
        "status": "completed",
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "http_viewer_data_load": http_result,
        "live_index_sha256": sha(refs["viewer"] / "live_index.json"),
        "live_head_sha256": sha(refs["viewer"] / "live_head.json"),
        "live_head_binds_live_index": refs["head"].get("live_index_sha256") == sha(refs["viewer"] / "live_index.json"),
        "graph_sha256": frame["unified_sha256"],
        "overlay_sha256": frame["room_belief_overlay_sha256"],
        "notes": [
            "HTTP verification consumed already-published artifacts only; no branch join or semantic update was rerun.",
            "No mapper, capture, detector, encoder, Qwen, or membership update ran.",
            "Verified-only scope gate remained unavailable; semantic update was executed and returned not_scored.",
        ],
    })
    write_json(output / "run_manifest.json", manifest)
    output_hashes = _file_hashes(output, excluded={"run_manifest.json", "output_sha256.json", "run_manifest.sha256.json"})
    write_json(output / "output_sha256.json", {"schema": "t4-artifact-regression-output-hashes/v1", "files": output_hashes})
    manifest["output_hashes_sha256"] = sha(output / "output_sha256.json")
    write_json(output / "run_manifest.json", manifest)
    write_json(output / "run_manifest.sha256.json", {"schema": "t4-artifact-regression-manifest-receipt/v1",
               "manifest": "run_manifest.json", "sha256": sha(output / "run_manifest.json"), "status": "completed"})
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-run", type=Path, required=True)
    args = parser.parse_args()
    result = finalize(args.output, args.source_run)
    print(json.dumps({"status": result["status"], "semantic_status": result["semantic_status"],
                      "http_viewer_data_load": result["http_viewer_data_load"]["status"],
                      "checked_paths": len(result["http_viewer_data_load"]["checked_paths"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

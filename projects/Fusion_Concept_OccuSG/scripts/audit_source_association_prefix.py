#!/usr/bin/env python3
"""Replay the unchanged source M6 manager over an explicit cache prefix."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from standalone_sg.config import load_config  # noqa: E402
from standalone_sg.objects import ObjectAssociationManager  # noqa: E402
from run_habitat_native_replay import _load_detection_cache  # noqa: E402


def replay_prefix(cache_path: Path, config_path: Path, frame_count: int, output: Path) -> list[dict]:
    batches = _load_detection_cache(cache_path)
    ordered = list(batches.values())
    if frame_count < 1 or frame_count > len(ordered):
        raise ValueError(f"frame_count must be within 1..{len(ordered)}")
    manager = ObjectAssociationManager(load_config(config_path).object_association)
    rows = []
    for batch in ordered[:frame_count]:
        result = manager.update(batch)
        for association in result.associations:
            detection = next(d for d in batch.detections if d.detection_id == association.detection_id)
            rows.append({
                "frame_id": association.frame_id,
                "timestamp_s": association.timestamp_s,
                "detection_id": association.detection_id,
                "provider_id": association.provider_id,
                "observation_class_name": detection.class_name,
                "candidate_id": association.object_id,
                "object_id": association.object_id,
                "decision": association.decision,
                "distance_m": association.distance_m,
                "normalized_class_name": association.normalized_class_name,
            })
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, sort_keys=True, allow_nan=False) + "\n" for row in rows), encoding="utf-8")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--detection-3d-cache", type=Path, required=True)
    parser.add_argument("--pipeline-config", type=Path, default=ROOT / "configs/reference_code_mp3d.yaml")
    parser.add_argument("--frame-count", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = replay_prefix(args.detection_3d_cache, args.pipeline_config, args.frame_count, args.output)
    print(json.dumps({"status": "pass", "frames": args.frame_count,
                      "associations": len(rows), "output": str(args.output.resolve())}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

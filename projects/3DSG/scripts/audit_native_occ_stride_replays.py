#!/usr/bin/env python3
"""Compare saved native OccuSG replay outputs from the same source keyframe."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_replay(label: str, root: Path) -> dict[str, Any]:
    summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    frame_index = int(summary["frame_index"])
    snapshot_path = root / "branches/rooms/snapshots" / f"rev-{frame_index:06d}.json"
    snapshot_doc = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot = snapshot_doc["snapshot"]
    mapping = summary["occusg_aux_depth_integration"]["mapping_record"]
    nodes = snapshot.get("nodes", [])
    edges = snapshot.get("edges", [])
    return {
        "label": label,
        "replay_dir": str(root),
        "status": summary.get("status"),
        "frame_id": summary.get("frame_id"),
        "frame_index": frame_index,
        "source_hashes": summary.get("capture_hashes", {}),
        "primary_stride": int(summary.get("occusg_primary_stride", 1)),
        "auxiliary_stride": int(summary.get("occusg_auxiliary_stride", 1)),
        "mapping": {
            key: mapping.get(key) for key in (
                "m1_m2_m3_runtime_ms", "m1_runtime_ms", "m2_runtime_ms", "m3_runtime_ms",
                "backprojected_hit_count", "auxiliary_point_count",
                "auxiliary_sensor_integrated", "auxiliary_calibration_sha256",
                "auxiliary_file_sha256", "m2_free_key_count", "m2_occupied_key_count",
                "m3_free_cell_count", "m3_occupied_cell_count",
            )
        },
        "graph": {
            "node_count": len(nodes),
            "node_kinds": dict(sorted(Counter(node.get("kind", "unknown")
                                                for node in nodes).items())),
            "edge_count": len(edges),
            "edge_relations": dict(sorted(Counter(edge.get("relation", "unknown")
                                                   for edge in edges).items())),
            "snapshot_sha256": _canonical_sha256(snapshot),
        },
        "summary_sha256": _canonical_sha256(summary),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", action="append", required=True, metavar="LABEL=PATH",
                        help="saved replay directory; repeat for each stride variant")
    parser.add_argument("--output", type=Path, required=True,
                        help="write the comparison JSON to this path")
    args = parser.parse_args()

    rows = []
    for value in args.replay:
        if "=" not in value:
            parser.error("--replay must use LABEL=PATH")
        label, raw_path = value.split("=", 1)
        if not label.strip():
            parser.error("--replay label must be non-empty")
        root = Path(raw_path).resolve(strict=True)
        rows.append(_read_replay(label.strip(), root))

    identities = {(row["frame_id"], _canonical_sha256(row["source_hashes"])) for row in rows}
    identity_match = len(identities) == 1
    output = {
        "schema": "3dsg-native-occ-stride-replay-comparison/v1",
        "same_source_frame_and_hashes": identity_match,
        "replay_count": len(rows),
        "replays": rows,
        "interpretation": (
            "Comparable only because all replay inputs share frame identity and source hashes. "
            "A single keyframe is diagnostic, not an accuracy validation."
        ),
    }
    if not identity_match:
        raise SystemExit("replay inputs differ in frame identity or source hashes")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "same_source_frame_and_hashes": identity_match,
                      "replay_count": len(rows)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build immutable unified object/room graph revisions from existing prefixes."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integration_v2.unified_incremental_timeline import build_unified_timeline  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeline", type=Path, required=True, help="M0/M1 immutable object+room prefix timeline")
    parser.add_argument("--causal-state", type=Path, required=True, help="A.1 causal hybrid prefix state")
    parser.add_argument("--output", type=Path, required=True, help="new output directory; existing non-empty paths are rejected")
    args = parser.parse_args()
    manifest = build_unified_timeline(args.timeline, args.causal_state, args.output)
    unavailable = sum(row["membership_status"] == "unavailable" for row in manifest["revisions"])
    print(json.dumps({
        "output": str(args.output.resolve()),
        "scene_id": manifest["scene_id"],
        "sequence_id": manifest["sequence_id"],
        "revisions": manifest["revision_count"],
        "membership_unavailable_revisions": unavailable,
        "membership_policy": manifest["default_scope_policy"],
        "final_postprocess_included": manifest["final_postprocess_included"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

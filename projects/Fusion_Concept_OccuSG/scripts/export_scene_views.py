#!/usr/bin/env python3
"""Export clean and filtered scene-graph views from one serialized snapshot."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from standalone_sg.serialization import load_snapshot  # noqa: E402
from standalone_sg.visualization import render_debug_graph, render_snapshot_views  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--show-navigation", action="store_true", help="include M7 navigation nodes/edges in debug view")
    parser.add_argument("--show-temporal", action="store_true", help="include agent temporal edges in debug view")
    parser.add_argument("--hide-agents", action="store_true")
    parser.add_argument("--hide-objects", action="store_true")
    parser.add_argument("--label-kinds", default="room,agent,object,navigation")
    args = parser.parse_args()

    snapshot = load_snapshot(args.snapshot)
    output = args.output.resolve()
    paths = render_snapshot_views(snapshot, output)
    debug_path = output / "scene_graph_debug.png"
    render_debug_graph(
        snapshot,
        debug_path,
        show_navigation=args.show_navigation,
        show_temporal=args.show_temporal,
        show_agents=not args.hide_agents,
        show_objects=not args.hide_objects,
        label_kinds=tuple(kind.strip() for kind in args.label_kinds.split(",") if kind.strip()),
    )
    paths["scene_graph_debug"] = debug_path
    print(json.dumps({key: str(value) for key, value in paths.items()}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

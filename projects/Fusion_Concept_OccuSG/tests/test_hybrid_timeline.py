from __future__ import annotations

import json
from pathlib import Path

from standalone_sg.serialization.hybrid_timeline import replay_timeline


def test_timeline_replay_checks_revision_chain_and_prefix(tmp_path: Path):
    root = tmp_path / "timeline"
    (root / "snapshots").mkdir(parents=True)
    snapshots = []
    for rev, frame in enumerate((0, 1), start=1):
        snap = {
            "schema": "conceptgraphs-causal-object-timeline/v1",
            "revision": rev, "frame_index": frame, "frame_id": f"frame-{frame}",
            "objects": [{"hypothesis_id": f"h-{rev}", "observation_ids": [f"scene-{frame:04d}:det-0000"]}],
        }
        path = root / "snapshots" / f"rev-{rev:06d}.json"
        path.write_text(json.dumps(snap))
        snapshots.append(path)
    deltas = [{"base_revision": 0, "new_revision": 1, "snapshot": "snapshots/rev-000001.json"},
              {"base_revision": 1, "new_revision": 2, "snapshot": "snapshots/rev-000002.json"}]
    (root / "delta_log.json").write_text(json.dumps({"deltas": deltas}))
    (root / "manifest.json").write_text(json.dumps({"delta_log": "delta_log.json", "revision_count": 2}))
    assert [s["revision"] for s in replay_timeline(root)] == [1, 2]


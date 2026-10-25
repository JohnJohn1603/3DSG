#!/usr/bin/env python3
"""Run fixed synthetic U3 association fixtures in source/geometry/combined modes."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from standalone_sg.config.schema import ObjectAssociationConfig  # noqa: E402
from standalone_sg.contracts.objects import Detection3D, DetectionBatch  # noqa: E402
from standalone_sg.objects.association import ObjectAssociationManager  # noqa: E402
from standalone_sg.objects.u3_experimental import ExperimentalAssociation, U3Observation, deduplicate_frame_masks  # noqa: E402


def obs(did, frame, x=0.0, ext=(1., 1., 1.), f=(1., 0.), cls="table", mask=None):
    return U3Observation(did, str(frame), float(frame), (x, 0., 0.), ext, "floor_0", cls, tuple(f), mask or did)


def source_result(frames):
    m = ObjectAssociationManager(ObjectAssociationConfig(.75, .15))
    out = {}
    for frame in frames:
        for o in sorted(frame, key=lambda x: x.detection_id):
            d = Detection3D(o.detection_id, o.timestamp_s, o.center, "synthetic", class_name=o.class_name,
                            frame_id="world", layer_id=o.layer_id, extents_m=o.extents)
            result = m.update(DetectionBatch("fixture", "world", o.timestamp_s, (d,)))
            out[o.detection_id] = {"track": result.associations[0].object_id,
                                   "decision": result.associations[0].decision}
    return out


def experiment_result(frames, mode, masks=None):
    engine = ExperimentalAssociation(mode)
    all_decisions = {}
    masks = masks or {}
    for frame in frames:
        frame = sorted(frame, key=lambda x: x.detection_id)
        if masks:
            frame, _ = deduplicate_frame_masks(frame, masks)
        for result in engine.update_frame(frame):
            all_decisions[result.detection_id] = {"track": result.track_id, "decision": result.decision,
                                                   "reason": result.reason}
    return all_decisions, engine


def main():
    fixtures = {
        "object_class_flip": [[obs("a", 1, cls="dining_table")], [obs("b", 2, cls="bed")]],
        "nearby_same_class_distinct": [[obs("a", 1, x=0., cls="chair"), obs("b", 1, x=.7, cls="chair")]],
        "nearby_different_class_distinct": [[obs("a", 1, ext=(.5,.5,.5), f=(1.,0.), cls="table")],
                                              [obs("b", 2, x=.32, ext=(.5,.5,.5), f=(-1.,0.), cls="bed")]],
        "partial_view_overlap": [[obs("a", 1, ext=(1.,1.,1.))],
                                  [obs("b", 2, x=.1, ext=(.35,.5,.6))]],
        "duplicate_observation": [[obs("a", 1, cls="chair", mask="m1"), obs("b", 1, cls="chair", mask="m2")]],
        "ambiguous_match": [[obs("a", 1, f=(1.,.08)), obs("b", 1, f=(1.,-.08))],
                            [obs("c", 2, f=(1.,0.))]],
    }
    result = {"schema_version": 1, "synthetic_not_dataset_evidence": True,
              "input_policy": "same synthetic observations to each strategy; duplicate fixture is common-prefiltered",
              "fixtures": {}}
    for name, frames in fixtures.items():
        source = source_result(frames)
        geom, _ = experiment_result(frames, "geometry")
        app, app_engine = experiment_result(frames, "geometry_appearance")
        if name == "duplicate_observation":
            masks = {"a": np.ones((4,4), dtype=bool), "b": np.ones((4,4), dtype=bool)}
            source_frames = [deduplicate_frame_masks(frame, masks)[0] for frame in frames]
            source = source_result(source_frames)
            geom, _ = experiment_result(frames, "geometry", masks)
            app, app_engine = experiment_result(frames, "geometry_appearance", masks)
        result["fixtures"][name] = {"source": source, "geometry_only": geom,
                                    "geometry_appearance": app,
                                    "geometry_appearance_defer_count": sum(d.decision == "defer" for d in app_engine.decisions)}
    out = ROOT / "artifacts/audits/u3_association/fixture_matrix.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(out.relative_to(ROOT))
    for name, row in result["fixtures"].items():
        print(name, {m: {k: v["decision"] for k,v in row[m].items()} for m in ("source", "geometry_only", "geometry_appearance")})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

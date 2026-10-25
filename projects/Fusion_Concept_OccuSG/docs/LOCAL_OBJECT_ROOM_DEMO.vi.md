# Demo local object–room graph

Demo adapter-only: it reads an existing OccuSG room snapshot and a final-map object export, preserves the object IDs, assigns membership geometrically, and renders the combined graph. It does not run ConceptGraphs inference or M6 association. The prepared demo input in this workspace is a **fixture**, never a ConceptGraphs result.

## Reproduce the demo

From the workspace root:

```bash
python3 scripts/create_object_room_demo_fixture.py \
  --room-snapshot artifacts/runs/u5m9_20261011T035542.851355Z_b39b87/replay/snapshot.json \
  --out artifacts/unified_graph_demo/input_fixture_final

./run_scene.sh unified-graph \
  --room-snapshot artifacts/runs/u5m9_20261011T035542.851355Z_b39b87/replay/snapshot.json \
  --object-export artifacts/unified_graph_demo/input_fixture_final/object_export \
  --source-kind fixture

./run_scene.sh open <unified-id-printed-by-the-previous-command> unified
```

Each run gets a timestamped output directory; the launcher refuses to overwrite an existing one. Copy the printed `unified_...` ID into the `open` command. `unified_snapshot.json` is the graph, `manifest.json` records input hashes and membership decisions, and `index.html` shows room boundaries, object centers, labels and room ownership. The map is `object_room_map.png` when the optional Matplotlib renderer is available, or dependency-free `object_room_map.svg` otherwise. If the desktop viewer cannot launch, open the printed `index.html` path manually.

Expected fixture result: 3 objects; one assigned, one ambiguous at a room boundary, and one unassigned outside room geometry. The room snapshot and fixture use matching scene and sequence IDs. The fixture's map marker is a contract-test payload, not a native ConceptGraphs map.

## Use a real export later

Replace `--object-export` with a directory containing `export_manifest.json` and the hashed object point-cloud files. The manifest must declare schema `conceptgraphs-object-export/v1`, `units: m`, `scene_id`, `sequence_id`, `provenance.source_kind: reference`, final-map-only identity scope, and `T_capture_world_from_cg_world`. Use a room snapshot from the matching scene and sequence. The importer verifies IDs, transform, and hashes, then fails on scene/sequence mismatch. Do not pass an OccuSG legacy graph and label it as reference output.

Room membership is `assigned` only when the projected object footprint is wholly supported by one room polygon. Boundary or multi-room evidence is `ambiguous`; no room overlap is `unassigned`. Those objects do not receive a `room_contains` edge. Raw and fused labels remain separate. Room semantic scoring is unchanged.

## Current boundary

Local adapter, serialization, fixture membership and viewer are verified. No genuine ConceptGraphs object export exists in the current workspace inventory. Pinned reference loader/inference, native-map export and import of a real ConceptGraphs result remain pending. Fixture success is technical contract coverage, not a reference-pipeline or object/room quality result.

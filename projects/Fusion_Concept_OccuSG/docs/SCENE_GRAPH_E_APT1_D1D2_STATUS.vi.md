# Mốc E — ReplicaCAD apt_1 và nghiệm thu D1+D2 đồng thời

Ngày: 2026-10-16  
Run C2: `c2_replica_apt1_short_e_v1`  
Run D1: `d1_c2_replica_apt1_short_e_v1_retry1`

## Kết luận

Đã chạy một config/route ReplicaCAD thứ hai (`apt_1`) qua C2, rồi chạy D1
trên cache của run đó trong khi D2 XYZ viewer được mở và nhận revision trước
khi producer kết thúc. Điều này xác nhận một vertical slice config-driven
trên scene ReplicaCAD thứ hai và nghiệm thu D1+D2 đồng thời cho run này.

Đây là development run đã biết trước, không phải holdout hoặc đánh giá chất
lượng. Nó không chứng minh hỗ trợ mọi ReplicaCAD scene, HM3D/MP3D portability,
live simulation, realtime hay chất lượng object/room labels.

## Config, route và provenance

| Input | Giá trị | SHA-256 |
|---|---|---|
| C2 config | `configs/scene_runs/replica_cad_apt1_short_e_v1.json` | `4f96e1b17e4e23973c980f7e55d1bba0524b9fb65ff82404132ea5a1382952ee` |
| Route config | `configs/habitat/m12_replica_cad_apt1_instance_route.json` | `e84cb95d442e77a68e6125cf60b6fab933b4c263f23b48939b03405fd0c64d8e` |
| Scene | ReplicaCAD `apt_1` | Scene/source hashes are in C2 run manifest |
| Sensor | RGB-D, 160×120, same configuration as apt_0 C2 | Effective route config is in run directory |
| Route | 12 fixed pose+yaw steps, 5 unique positions with revisits | Route was fixed before inspecting predictor outputs |
| Seed | `20261014` for ConceptGraphs mapping | Effective mapping manifest |

The route is a short development route with repeated viewpoints, not a blind
route. C2 config, effective route and complete input/output hashes are recorded
in [the C2 run manifest](../artifacts/conceptgraphs_hybrid_runs/c2_replica_apt1_short_e_v1/c2_run_manifest.json).

## Stages and repeated work

Each C2 stage ran once. The one C2 invocation completed preflight, capture,
P2, P3, image-feature extraction, text-feature extraction, OccuSG room build,
ConceptGraphs mapping, and unified graph/viewer build. Timings include:

| C2 stage | Runs | Elapsed |
|---|---:|---:|
| Capture RGB-D | 1 | 21.92 s |
| P2 masks | 1 | 5.39 s |
| P3 projection | 1 | 1.10 s |
| Image features (12 observations) | 1 | 14.65 s |
| Text features (12 observations; cached class strings/checkpoint) | 1 | 14.74 s |
| OccuSG room build | 1 | 135.90 s |
| ConceptGraphs mapping | 1 | 13.32 s stage time; mapping core 0.96 s in its internal report |
| Unified graph/viewer build | 1 | C2 manifest |

D1 then reused the verified C2 capture/perception/features and ran the hybrid
mapping core once more and one OccuSG replay to publish their revisions while
the coordinator and D2 viewer were active. The rerun was necessary for the
producer/coordinator acceptance; it did not rerun capture, detector, encoder,
or Qwen. D1 completed 12 joined frame revisions in 84.387 s. The D1 manifest
and logs contain stage-level timings and hashes.

Thus, across C2 + D1, ConceptGraphs mapping ran twice and native OccuSG
processing ran twice; capture, P2, P3, and feature extraction each ran once.
This report does not combine C2 batch timings and D1 cached-publishing timings
as if they were one realtime latency measurement.

## Object and graph results

- Route/capture: 12 of 12 RGB-D frames; P2 produced 18 2D detections/masks.
- P3: 1 accepted and 11 rejected under the unchanged configuration. This is
  a diagnostic count, not a mask-quality or scene-quality score.
- ConceptGraphs mapping: 12 hypotheses before final admission; 0 retained at
  `min_detections=3`; 0 after final merge. No threshold was changed.
- The valid zero-retained export exposed a C2 integration assertion that had
  incorrectly required a retained object. The failed initial manifest is
  preserved at `recovery/c2_run_manifest.initial_failure.json`. Only unified
  graph validation/viewer generation was rerun; perception, features, mapping,
  and native room construction were not rerun for that recovery.
- C2 OccuSG output had one source-native `potted_plant` object relation. The
  hybrid unified graph intentionally excludes native object nodes; it records
  that one excluded native membership and preserves room geometry, adjacency,
  navigation and agent relations. It does not relabel the native object as a
  ConceptGraphs object.
- D1 emitted 12 prefix revisions. Hypothesis counts by revision:
  `2, 2, 4, 4, 4, 5, 6, 6, 6, 10, 11, 12`. Final postprocess is separate and
  contains zero objects. These counts are pipeline state, not physical-object
  accuracy.
- All 12 joined graph states were checked for dangling edges. No
  ConceptGraphs `room_contains` edges were emitted.

## D1+D2 simultaneous GUI acceptance

The browser opened the canonical D2 entrypoint while D1 producers were still
running. The reviewer saw new revisions arrive before run completion, switched
to historical revision 3 while live-follow was off, and confirmed the view
stayed at revision 3 as later events arrived. The browser was then returned to
the latest prefix. Object picking showed revision-local observation lineage;
the final-postprocess stage remained distinct from online hypotheses.

The displayed interface showed XY room polygons and adjacency beside XYZ
object samples and camera path, plus current RGB/mask/crop evidence. Trajectory
ended at the selected revision. Membership and room semantics remained
unavailable. This was actual browser interaction on the run, not a screenshot
or synthetic-event substitute.

Open the run through the existing launcher:

```bash
./run_scene.sh results d1_c2_replica_apt1_short_e_v1_retry1
./run_scene.sh open d1_c2_replica_apt1_short_e_v1_retry1 live --port 8766
```

The `results` command resolved `index_d2_live.html` from the D2 manifest. The
run's original D1 manifest still describes its XY-only index separately; use
the launcher-resolved D2 entrypoint above for the accepted XYZ viewer.
The HTTP server was run-owned (PID 73568, bound to `127.0.0.1:8766`) and was
left available for inspection; no unrelated process was stopped.

Measured browser receive/render instrumentation varied by selected frame;
the observed sample was approximately 20–75 ms receive and 45–142 ms render.
These are browser-side sample timings, not end-to-end display latency or a
realtime claim. D1 processing, barrier wait and publication timings are in the
run manifest. C2+D1 is live processing from cached inputs, not live simulation.

## Tests and status

Targeted command:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \\
  tests/test_c2_fresh_replica.py tests/test_d1_live_processing.py \\
  tests/test_d2_xyz_viewer.py tests/test_scene_launcher.py \\
  --basetemp /tmp/e-recovery-dangling-tests-20261016
```

Result: **63 passed**. The C2 revalidation was also run once against existing
outputs; it did not repeat mapping or room construction.

| Gate | Status |
|---|---|
| C2 config-driven fresh route on second ReplicaCAD scene | Passed for this `apt_1` config/route only |
| D1 producers + D2 XYZ viewer receiving revisions before completion | Passed on this C2 cache |
| Cached playback | Available; distinct from the live producer run |
| Live processing from cache | Passed for this D1 run |
| Live simulation producing fresh RGB-D each step | Not implemented |
| Wall-clock realtime | Not measured or claimed |
| HM3D/MP3D portability | Not tested in this E run |
| Object admission/quality | 0 final retained objects under locked `min_detections=3`; no quality claim |
| Spatial scope, object-room membership, room labels | Unavailable; no scope gate bypass |
| Multi-floor support | Not implemented or claimed |

## Smallest next step: step-synchronized Habitat integration

Build a narrow simulator adapter that accepts a scene/route config, advances
Habitat by one fixed route step, emits that step's RGB-D + pose to the existing
producer contracts, waits for the matching graph revision, then advances the
simulator. No navigation planner is required. The demo camera panels are
read-only display feeds and must not enter detector, feature, mapping, or room
predictor inputs unless explicitly designated as predictor sensors in config.

Suggested bounded implementation sequence:

1. **S0 contract/prototype (2–3 h):** wrap Habitat's existing fixed-route
   capture loop as `step() -> {frame_id, rgb, depth, intrinsics, pose}`; test
   identity/alignment on one existing scene without running predictors.
2. **S1 one-step streaming slice (4–6 h):** pass a fresh step into the
   existing P2 and publication path; expose a matching-revision barrier and
   timeout/failure semantics. Keep all extra camera panels display-only.
3. **S2 graph integration (4–8 h):** connect the existing mapping/room
   producers to step events, publish unified revisions, and show XYZ/XY plus
   camera panels. Preserve unavailable membership/semantics.
4. **S3 bounded acceptance (2–4 h):** run one short configured ReplicaCAD
   route, verify causal frame IDs, backpressure, cancellation, viewer
   follow/seek and stage timings; report completed wall-clock throughput
   without setting a realtime target.

Estimate: **12–21 hours**, with an explicit stop after S0/S1 if existing
ConceptGraphs or OccuSG APIs cannot consume one-step input without a larger
rewrite. HM3D/MP3D adapters should be follow-up portability work after the
step-synchronized path is validated on one supported scene type. No
implementation or simulator run was started by this proposal.

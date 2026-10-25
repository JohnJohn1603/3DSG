# T1.1 all-13 prefix admission and room-belief replay — 2026-10-19

## Scope and execution

- Mapper-only source: `hm3d00802_t1_1_all13_mapper_only_20261019_v1`; source run manifest SHA-256: `d30dc27759bf8eb29ae87fccb1037fa5b2b944d54bbdacc44a622ae3d950fa58`.
- Scene/sequence: `00802-wcojb4TFT35` / `hm3d-v02-00802-t1-1-turn-continue-v1_smooth_smooth_t1_1_hm3d00802_turn_continue_20261018_v2`.
- 13 source keyframes processed in order: `0, 26, 33, 40, 50, 61, 68, 75, 82, 89, 96, 103, 110`; 45 P2 observation records are joined by full detection ID.
- Source stages remain distinct: 113 camera frames in T1.1 capture, 13 selected mapper keyframes, 13 object prefix revisions, 13 room prefix revisions, and 13 unified revisions. Object revisions are 1, 27, 34, 41, 51, 62, 69, 76, 83, 90, 97, 104, 111, room revisions are 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, unified revisions are 1–13.
- Source terminal ConceptGraphs postprocess remains separate (`15` pre-terminal hypotheses to `6` final objects). It was not used for prefix admission or backfilled into semantic states.
- T3 semantic replay: one verified-only adapter/replay; 0 capture, perception, Qwen, ConceptGraphs mapping, and OccuSG mapping runs. Runtime: 0.526 s.
- A-table archive SHA-256: `0e30cc12058ec22372fe8b2df49279ec6a917027bef74182f8831374a7418997`; variants: `mean_no_neutral, mean_neutral, sum_no_neutral, sum_neutral`. No variant was selected as a winner.

## Admission, consensus, and room evidence

Prefix admission read `obj_min_detections=3` from the mapper run effective config. Consensus uses the existing `exact_camera_pose_matrix_v1` group policy (2 groups minimum, top share 0.70, margin 0.20). Distinct pose groups are exact transform groups, not statistically independent views; no smooth-route near-pose grouping was introduced. Across the 13 selected frames, unique exact camera-pose matrices: 13.

| Frame | Object rev | Room rev | Unified rev | Prefix hypotheses | Admission eligible | Stable / conflicted / unknown | Stable categories | Active rooms |
|---:|---:|---:|---:|---:|---:|---|---|---|
| 0 | 1 | 1 | 1 | 5 | 0 | 0 / 0 / 5 | — | room:1, room:2 |
| 26 | 27 | 2 | 2 | 6 | 0 | 0 / 0 / 6 | — | room:1, room:2 |
| 33 | 34 | 3 | 3 | 6 | 1 | 1 / 0 / 5 | chair | room:1, room:2 |
| 40 | 41 | 4 | 4 | 7 | 2 | 2 / 0 / 5 | chair | room:1, room:2, room:3 |
| 50 | 51 | 5 | 5 | 12 | 2 | 2 / 0 / 10 | chair | room:1, room:2 |
| 61 | 62 | 6 | 6 | 12 | 3 | 1 / 2 / 9 | chair | room:1, room:2 |
| 68 | 69 | 7 | 7 | 12 | 3 | 1 / 2 / 9 | chair | room:1, room:2 |
| 75 | 76 | 8 | 8 | 12 | 3 | 2 / 1 / 9 | chair | room:1, room:2 |
| 82 | 83 | 9 | 9 | 12 | 3 | 1 / 2 / 9 | chair | room:1, room:2 |
| 89 | 90 | 10 | 10 | 12 | 3 | 1 / 2 / 9 | chair | room:1, room:2 |
| 96 | 97 | 11 | 11 | 14 | 4 | 2 / 2 / 10 | chair, sofa | room:1, room:2 |
| 103 | 104 | 12 | 12 | 15 | 4 | 2 / 2 / 11 | chair, sofa | room:1, room:2, room:4 |
| 110 | 111 | 13 | 13 | 15 | 6 | 4 / 2 / 9 | chair, sofa, tv | room:1, room:2, room:4 |

At the final prefix, 15 online hypotheses remain in the object snapshot; 6 meet admission, 4 have stable consensus, 2 are conflicted, and 9 are unknown/provisional. Stable categories present are chair, sofa, and tv. These are semantic/admission statuses, not object-identity or class-accuracy scores.

All 13 prefixes retain scope status `unavailable`. Object membership is unavailable for all hypotheses, `room_contains` edges are zero, and all four variants report no eligible room categories, no candidate label, no committed label, and `unavailable_elevation_scope`. The uniform prior-only vector in RB1 is not treated as a candidate or a room prediction. This is distinct from `insufficient_support`: room belief was blocked before membership/category evidence could be formed.

Room IDs are physical geometry identifiers only. The active room set changes by prefix; no semantic label was attached. Category cap/retract integration was covered by the existing RB1 fixture tests, but no real T1.1 category contribution occurred because membership remained unavailable.

## Provenance and outputs

- `raw_capture_provenance_addendum.json` records all 13 raw keyframe file hashes; set SHA-256: `e7bfc97808887f1275fd30dc71b70b7d69aa469305411281fc0f3a8c233407dd`.
- `prefix_room_belief_summary.csv`: per frame/room/four variant decision and evidence fields.
- `object_prefix_semantics.csv`: per-prefix object admission, consensus status, exact pose-group counts, raw classes, and full observation IDs.
- `assumed_scope_declaration_T1_1_DRAFT_PENDING.json`: a non-runtime draft; no declaration was applied.
- Viewer package: `viewer_package_v3/viewer/index.html`; 13 prefixes, 4 A variants, 45 RGB/mask/overlay/crop evidence panels. Viewer build receipt: `viewer_package_v3/viewer_build_receipt.json`.
- GUI: pending browser verification in this session. HTTP/data and manifest checks do not constitute GUI acceptance.

## Gates and limits

- T2: existing mapper sessions and 13 paired source revisions were reused; no T2 mapper stage was rerun.
- T3: verified-only prefix admission, consensus, and four-variant RB1 replay now run over T1.1 snapshots. Scoped membership correctly remains unavailable; room labels were not generated.
- T4: not complete. This is a cached/static prefix replay, not a live camera run. No per-unified-commit call to semantic update exists in the camera-live producer path yet.
- No room-function ground truth was used; accuracy/F1 is unavailable.
- Terminal denoise/filter/merge semantic handling is not synthesized; the terminal final object set was kept separate and no semantic state was backfilled.

## Tests and viewer check

- `python3 -m py_compile` on the T3 runner, adapter, viewer builder, and causal viewer builder: passed.
- `python3 -m pytest -q tests/test_room_belief_v1.py tests/test_t3_assumed_scope_contract.py tests/test_t3_room_belief_viewer.py`: 32 passed. Includes category-presence cap, object retract, assumed-scope fixture, A/B isolation, A-only viewer, and non-contiguous source revision alignment.
- `node tests/test_viewer_atomic_revision.js`: passed delayed/out-of-order/failure-retention/cancellation fixture.
- Mapper source integrity: all 289 immutable outputs matched the locked run manifest during adapter validation.
- Browser GUI not available to the agent; please inspect first/middle/last and seek backward using the checklist below.

## Mở viewer và kiểm tra GUI

Khởi động server read-only từ workspace:

```bash
python3 scripts/serve_causal_timeline_viewer.py \
  --root artifacts \
  --viewer room_belief/t1_1_all13_t3_verified_20261019_v1/viewer_package_v3/viewer \
  --port 8783
```

Mở: [http://127.0.0.1:8783/room_belief/t1_1_all13_t3_verified_20261019_v1/viewer_package_v3/viewer/index.html](http://127.0.0.1:8783/room_belief/t1_1_all13_t3_verified_20261019_v1/viewer_package_v3/viewer/index.html). HTTP GET cho entrypoint, manifest, first/middle/last snapshots, room geometry, XYZ geometry, unified graph và RGB/mask panels trả 200. GUI tương tác vẫn pending vì browser không khả dụng cho agent trong phiên này.

## GUI checklist for user verification

1. Open the command/URL below and confirm frame 0 loads.
2. Choose `mean_no_neutral`, then inspect first, middle and final keyframe; confirm frame ID, object revision, room revision and unified revision are all displayed distinctly.
3. Select a few objects and open their observation IDs; confirm raw class and RGB/mask/overlay/crop correspond to the displayed source frame.
4. Confirm the scope badge says A · verified-only, room polygons show `unavailable_elevation_scope`, and no `room_contains` edges or semantic room labels appear.
5. Seek from final back to first and verify the trajectory stops at the selected prefix.

## One next checkpoint

After your decision on the T1.1 assumed-scope draft, the smallest implementation checkpoint toward T4 is a semantic commit hook called immediately after each unified revision commit, reading that exact object/room revision pair and publishing the resulting T3 state. First run can remain verified-only and report `unavailable_elevation_scope`; it must not wait for terminal capture or use future manifests. This does not require backpressure mode or a new model run.

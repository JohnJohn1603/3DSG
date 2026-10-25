# T4 pilot — timebase contract A, 2026-10-19

## Scope and frozen inputs

One real camera-live pilot ran from empty state after model-free readiness checks. No retry was made after capture began. It used HM3D `00802-wcojb4TFT35`, the locked T1.1 route `hm3d-v02-00802-t1-1-turn-continue-paced-rtf1-v2`, native 640×480, and verified-only room scope.

- Pilot run: `t4_camera_live_00802_t1_1_timebaseA_20261019_pilot_v1`
- Route run: `t4_camera_live_00802_t1_1_timebaseA_20261019_v1`
- Effective config: `configs/habitat/hm3d_minival_v02_00802_t1_1_turn_continue_paced_640x480_timebaseA_v1.effective.json`, SHA-256 `adb5f97c16b1627b39026e2fa98354ddd38a99ece46b6e6be7eabe8ced9497e7`
- Timebase policy: `configs/habitat/smooth_route_t1_paced_v5_timebase_a.json`, SHA-256 `74ddfd4399af8cbb84e3623492df2a90fd4f53c84fc5b8bef5e2223d7a580765`
- C2 source: `hm3d00802_main29_640_quality_v1_attempt2`, manifest SHA-256 `48e62b2f4031061d77c69fd603269e78707174e600d5ad89f7dc5a0b0a6c4753`
- Route producer manifest SHA-256: `057121ff0fe78aa969f4e9bbbceb89208528bfd09ea3645b1c9b09242d386af7`
- Room-belief config: `configs/room_semantics/t4_live_room_belief_v1.json`; `verified_only`, no assumed declaration.
- D2 URL: `http://127.0.0.1:8813/artifacts/live_processing_runs/t4_camera_live_00802_t1_1_timebaseA_20261019_pilot_v1/viewer/index_d2_camera_live.html?live=1`

The source hashes recorded by the run manifest were rechecked against the files on disk and match: `run_camera_live_pilot.py` `e08875976a85d0003373fa98f4e261a920c6980a58de401c622c26a36650a0e7`; `camera_pacing.py` `039e2757b00f67daf7c1febdfd941e4c061520ebe027ebfa2e1a5ac3f30f3076`; `run_habitat_smooth_route_preview.py` `a706e338cb382c774a362757126327a8495635a173767554dd559f6a4075a778`; `camera_live_startup.py` `3309011b1582034cbd32e22586a19d890a3801f5ac94ddbdda809091310dc247`; `live_evidence.py` `c5ddc25346cd62df0b7edb72f22e814acb14c6eda160fe95beb664c29dbd3930`; `live_processing.py` `c8ee3de8373859e5a62574c54f2a40c590e5a20663177593f9edd29785300a03`; `build_d2_xyz_viewer.py` `834acb37983db54de67b266756056b254053581398ed4750878f9541a3831c79`; viewer template `scripts/causal_timeline_viewer.html` `d22fbbf1bed8829ebe2bf8024d865d87b36373fb579f9f908375cd7c9a5b571f`.

The run used requested `dt=0.040 s`, fixed `0.008 m` / `1.2°` action increments, target 25 FPS and RTF 1.0. The increments and route were not changed after observing pilot output. Nominal speeds remain 0.2 m/s and 30°/s; the route runner measures actual pose deltas against Habitat `get_world_time()`.

## Preparation and run count

- Targeted model-free suite: **101 passed** across pacing, startup, capture/evidence contract, IPC, coordinator, D2 XYZ and publication-over-HTTP tests; Python compile checks passed.
- Startup preflight attempt 1 did not start workers because port 8776 was occupied by an existing server; that process was left untouched. Startup preflight attempt 2 passed on port 8812, reached the capture boundary with fake workers, and recorded zero model loads and zero frames.
- The real pilot ran exactly once on port 8813. Five persistent workers initialized once (P2, P3, features, ConceptGraphs and OccuSG); there were no retries or per-frame model reloads. All five workers exited with code 0.
- D2 was opened before start. The browser showed `waiting_for_first_revision` / no live evidence before the explicit start confirmation. HTTP log records the D2 entrypoint and renderer requests before capture.
- The route producer reported 23.704 s wall runtime; camera first-to-last frame span was 11.658 s. Pilot orchestration start-to-finish was about 100.4 s. All were below the 20-minute producer cap and 120-minute overall cap.

## Camera/timebase results

Habitat produced 278 camera frames and 277 simulation actions. The route completed, recorded zero collision events, and used one simulator session. Simulation time advanced from 0 to 11.696 s. Requested action time was 11.080 s, so cumulative observed-minus-requested deviation was +0.616 s.

- Observed simulation step: mean 42.224 ms, median 40 ms, p95/max 48 ms.
- Camera frame wall interval: mean 42.085 ms, median 40.492 ms, p95 53.621 ms, max 115.906 ms.
- Achieved camera rate: 23.761 FPS against a 25 FPS target.
- Simulation/wall factor: 1.0033 against target RTF 1.0.
- Pacing recorded 276 overruns, 2.227 s total pacing wait and 0.312 s maximum lag.
- The configured nominal 0.2 m/s / 30°/s are not run-wide measured speeds. Aggregate measured planar speed across all 277 actions was mean 0.0426, p50 0, p95 0.2000 m/s; angular speed was mean 22.220, p50 29.9999, p95 30.0001°/s. These aggregates include turn, translation and stationary actions; they do not describe a constant velocity.
- Route summary peak RSS was 481,660,928 bytes for the process scope recorded by that producer metric; this is not a host-wide peak.

No timestamp was clamped, no extra simulator step was inserted, and no delay was added after the last frame. The 40/48 ms variation is recorded rather than treated as a fixed 40 ms observation.

## Keyframes, perception and mapper joins

There were 13 keyframe candidates and 13 persisted raw keyframes. The configured latest-wins pending queue recorded 13 queued, 11 superseded before processing dispatch, 2 admitted, and 2 processed. Raw writer superseded count was zero. Superseded IDs are recorded in `execution_ledger.jsonl`; this is not a claim that all candidates were mapped.

P2, P3 and OpenCLIP began on the first admitted frame while the camera was still capturing. Both mapper sessions then received the same two admitted frames, in order, with the frame/source identities carried through the joined packets. Each mapper performed two frame updates. The unified publisher committed two graph revisions:

| Unified revision | Source camera frame | Simulation time | Graph hash | Semantic overlay |
|---|---|---:|---|---|
| 1 | `...cam-000000` | 0.000 s | `44580bbc328e90c368c5aa15baf94b2c2cb059a4d25df3a068047dd62bebbf1d` | `not_scored`, overlay SHA `5111eab72877061381687b25fc06b244ebd90449a09d3294c90202664645d32c` |
| 2 | `...cam-000271` | 11.408 s | `cf939cc2b09bd101495b4d07b4e8e34d777c2f792d5f3a78a0050c5d3f40db11` | `not_scored`, overlay SHA `a34a2a245755aa4eb4a49d4e83cb06ef21c779acdbeb3a9c6980d69ff036618b` |

Each overlay is bound to its own frame, graph hash and unified revision. Verified spatial scope was unavailable, so membership was `unavailable_elevation_scope` and both executed semantic updates were `not_scored`; this is not an abstention after scoring and not room accuracy.

First P2 started at host-monotonic 18083.6567. The last camera frame was observed at 18095.1674 and capture completion at 18095.1858. Unified revision 1 committed at 18101.1449, **5.9591 s after capture completion**. Revision 2 committed at 18117.4830. Therefore perception overlapped capture, but no unified graph revision did. Queue drain and terminal processing occurred after capture and do not count as overlap. The browser was polling before capture, but because no revision existed until after capture ended, a graph revision could not have been rendered during capture.

Terminal mapper flush and terminal semantic handling remained separate from frame revisions. `semantics/terminal.json` records `terminal_semantics_unavailable` because terminal filter/merge lineage was not proven. No terminal label was backfilled into a prefix.

## Acceptance by gate

| Gate | Result | Evidence / limit |
|---|---|---|
| A — route finishes normally | Pass | 278 frames, route completed, zero collision events. |
| B — timebase and motion reported | Pass as measurement | 23.761 FPS / RTF 1.0033; 40–48 ms observed step distribution; nominal and measured speeds are distinguished. Target values are not guaranteed values. |
| C — queue accounting | Pass | 13 candidates/persisted, 2 admitted/processed, 11 pending superseded with reasons. |
| D — both mapper sessions receive same admitted frames | Pass | Two updates per branch for frames 000000 and 000271; two unified commits; workers stayed alive for the run. |
| E — a unified commit before final camera frame | **Fail** | First commit was 5.9591 s after capture completion. |
| F — semantic overlay bound to graph revision | Pass, not-scored | Two per-revision overlays match graph/frame/source; verified scope unavailable. |
| G — browser renders a revision during capture | **Not attained** | D2 was ready before capture and later rendered real run revisions; no graph revision existed before capture ended. |
| H — viewer shows consistent current-run state | Partial | Post-run browser showed current-run RGB/mask and XYZ at revision 2. No simultaneous graph/camera state was possible; no latency claim. |
| I — drain and terminal stages separate | Pass | Two frame revisions followed by separate mapper terminal flush; semantic terminal status is unavailable. |
| J — cleanup, statuses and hashes | Pass with integrity addendum | Run/bootstrap completed, all five worker exits 0. Original 386-file inventory had 373 matching and 13 mismatching entries, all from mutable logs/journals/bootstrap or live publication pointers; no files were missing. All 370 immutable inventory entries matched. `integrity_final_addendum.json` records final hashes and classifications without rewriting the original inventory. Addendum SHA-256: `70c49eb8fef8bea1f12c40a9155be9a1d3c9f50d540490e57ea5e09f0d023e07`. |

## Status and next decision

This was a **successful producer execution with overlap acceptance not met**, not a model or room-scoring failure. It demonstrates camera-live capture, current-frame perception, two persistent mapper sessions, atomic unified commits and per-revision semantic `not_scored` overlays. It does not demonstrate graph/camera overlap, render-during-capture, realtime performance, verified membership, room labels, or quality improvement.

The single smallest next decision toward a semantic demo is whether to authorize a **run-specific experimental `assumed_single_level_demo` declaration for this pilot run**, isolated to its exact scene/sequence/frame range and hashes. Until such separate approval, retain verified-only and `not_scored`; do not transfer the prior main29 declaration.

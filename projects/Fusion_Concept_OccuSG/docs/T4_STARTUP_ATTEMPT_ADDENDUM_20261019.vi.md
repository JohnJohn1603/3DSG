# T4 startup attempt addendum — 2026-10-19

This addendum corrects how the two startup attempts should be interpreted. The original run manifests, bootstrap files, and execution ledgers remain unchanged.

## Attempt ledger

The authorized limit was two startup attempts before the first camera frame, with at most one attempt allowed to enter capture. Both attempts were consumed; neither produced a camera frame. No further startup or producer attempt was made.

| Attempt | Run ID | Result | Evidence |
|---|---|---|---|
| Model-free startup dry-run | `t4_startup_contract_dryrun_20261019_v1` | `startup_preflight_failed` | Five fake workers reached ready. The browser start marker was written, then confirmation raised `NameError: name 'root' is not defined` before the route runner/its pre-capture validator was invoked. No model loads, capture directory, camera event journal, route producer manifest, or mapper mutation. |
| Single real pilot attempt | `t4_paced_startup_contract_00802_v3` | `failed_before_capture` by observed evidence; historical manifest says `failed` | Five real worker processes initialized and became ready; the D2 waiting page was served and browser confirmation was recorded. The coordinator marked the run `running` when it spawned the camera child. The child correctly rejected that state at its pre-capture validator: `pilot manifest is not at pre-capture started state: running`. No camera frame/event, persisted keyframe, capture directory, producer manifest, packet, mapper update, or unified revision exists. The later `FileNotFoundError` for the missing producer manifest was a secondary reporting error. |

### Corrected interpretation of attempt 2

The old manifest fields `execution_summary.capture_started=true`, the `capture_started` ledger event, and stage `camera_capture=failed_or_partial` came from the pre-fix coordinator treating process spawn as capture start. They do **not** prove that Habitat produced a frame. The route runner rejected the run before capture: `events.jsonl`, `camera_keyframes.jsonl`, `capture/`, and `camera_producer_manifest.json` are all absent. The old manifest is preserved; this addendum supplies the correction.

Attempt 2 initialized all five workers. P2 loaded its locked ONNX model once and OpenCLIP loaded its locked checkpoint once. P3 reported zero model loads; the ConceptGraphs and OccuSG workers each initialized once and reported zero model loads. The workers processed no frame. Their recorded `-15` exit codes are the coordinator's cleanup termination, not inference failures.

## Operational cleanup

At inspection, the viewer server owned by attempt 2 was PID `26856`, serving only run `t4_paced_startup_contract_00802_v3` on `127.0.0.1:8779`; it was stopped after the failed run was diagnosed. The unrelated servers remained untouched: PID `14871` on port `8776` (`t4_paced_overlap_00802_v1`) and PID `21725` on port `8777` (`t4_paced_publication_timebase_00802_v2`) were still listening at the final check. No camera, perception, or mapper worker remained alive.

The browser reached the actual D2 waiting page for attempt 2. No graph revision existed to render, so GUI acceptance for a real graph update remains pending.

## Code correction and verification

The current coordinator keeps pilot status `started` after spawning the route process. It transitions to `running` only after observing the first source `camera_frame` event. If a pre-capture failure occurs, processing stages are terminalized as `not_started`; worker readiness is recorded separately. The failed route child's log is reported before attempting to read a producer manifest that may not exist.

Current source SHA-256 values:

| File | SHA-256 |
|---|---|
| `integration_v2/camera_live_startup.py` | `09946755579da91aab5588edf0d8c0a65f5c733282e38896b5779203ef0b5b1e` |
| `integration_v2/live_processing.py` | `5a19a8045fe2094ec43f70915603c98a4dabdefc6e03af22d70b922e2a5e6451` |
| `scripts/run_camera_live_pilot.py` | `99486c389b3a24e824e73d7d7503e61ed3abb102ab4c345e94aa5057b0365b3e` |
| `scripts/run_t2_d_cached_packet_worker.py` | `cf45334de30c1b309a03995a80ac3e22468355ec43577d08572d58a1da4fbf2c` |
| `tests/test_camera_live_startup.py` | `76adfee60b7252af61970cbf7a5602055a796e76c52277a3c2c9cc5a3bc6e685` |
| `tests/test_camera_live_contract_ipc.py` | `6362698cfb128c73008d3fde62b82005a336279a880dfb4e7d7f62f9588f06b0` |

Model-free checks run after the correction: 52 targeted tests passed; the existing localhost publication/HTTP end-to-end tests passed 2/2; `py_compile` passed for the changed runtime and test modules. These checks do not replace a successful startup dry-run or real capture.

## Immutable evidence hashes

| Artifact | SHA-256 |
|---|---|
| Dry-run `run_manifest.json` | `df71ed7028c5a2a97345c466c1eda462265a73ac19c9a605783642082284fc9c` |
| Dry-run `pilot_bootstrap.json` | `249b66ee2bee3b30d674fe2d856ceb381336d6cee8f70cc6ed8030ba4dda1e7c` |
| Dry-run `execution_ledger.jsonl` | `db846cc46f2f36d06dcfa121f53be9647122ed7eba6bfa1d6a1f58221ca74cfc` |
| Pilot `run_manifest.json` | `17f5bedc0ea4d968d439db2d796d28cc1cd4a0ab758f26971c77122b08f42062` |
| Pilot `pilot_bootstrap.json` | `46f2bd17af2cfc6b0f9432c72e13bdbd41b51cf00615a07b4674897bee304882` |
| Pilot `execution_ledger.jsonl` | `067f1b8c5c3e7d6011c2fe4c060c16d1c4bfbebcd0cae6dc16b89bd6e1477e7c` |

The route/config hash was `fef21191f9ba2fd6b1c219379289630748ea5876797947d61ba15b3e624fc377`; the locked C2 source manifest hash was `48e62b2f4031061d77c69fd603269e787074e600d5ad89f7dc5a0b0a6c4753`. Both attempts used scene `00802-wcojb4TFT35` and the locked T1.1 sequence/config. The v3 pilot's source hashes are retained in its original manifest and remain tied to the pre-fix source.

## Status

This was a **pre-capture code/contract failure**, not a perception or mapper failure, not an overlap result, and not a GUI-render failure. T4 remains pending. The current startup-state fix is covered by model-free tests, but the allowed startup attempts are exhausted; a new real attempt requires renewed authorization. No T4, room-scoring, or quality claim is made.

## Startup-contract closure — later authorization, 2026-10-19

The user later authorized up to three additional **model-free startup dry-runs** and explicitly prohibited a real pilot, model load, capture, inference, or mapping. Two dry-runs were used; unit tests are not producer attempts. The history above is unchanged.

### Historical gate correction

- The earlier dry-run `t4_startup_contract_dryrun_20261019_v1` stopped before the route validator because the browser-ready callback raised `NameError: root is not defined`. It did **not** pass startup acceptance.
- The subsequent real pilot `t4_paced_startup_contract_00802_v3` started workers, but there is no evidence a complete dry-run had passed before it began. That pilot's child was rejected before capture. This ordering remains a historical gate violation; later unit tests do not retroactively satisfy it.

### Dry-runs in this authorization

| Run ID | Result | What was reached |
|---|---|---|
| `t4_startup_contract_closure_20261019_v1` | Failed before route validation | The sandbox denied binding the localhost viewer socket (`PermissionError: Operation not permitted`). No worker readiness, start handshake, route subprocess, model load, or capture occurred. This was recorded as a failed startup preflight. |
| `t4_startup_contract_closure_20261019_v2` | **Complete model-free startup dry-run passed** | Followed the production launcher/bootstrap/HTTP-control/IPC/subprocess/route-validator path and stopped at the capture boundary immediately before `sim.get_sensor_observations()`. No RGB-D observation was requested. |

The second attempt used the actual locked Habitat interpreter for the route runner and the standard coordinator/bootstrap path. Sequence observed in `execution_ledger.jsonl`:

1. Create the new run directory and write run/bootstrap manifests.
2. Start the run-owned localhost D2 server; validate the entrypoint and its HTTP assets.
3. Start five fake workers (`p2`, `p3`, `features`, `conceptgraphs`, `occusg_rooms`) through the production readiness IPC; all five reported ready.
4. Execute the production `confirm_browser_ready` control handler using an HTTP/control client. It wrote `control/start.json`; the coordinator validated the run ID, bootstrap/config identity, pointer hash, marker owner, and timestamp.
5. Spawn the route runner; the coordinator records `camera_runner=spawned_waiting_for_startup_validation`, not capture started.
6. The Habitat route runner validates the effective route/config, initializes Habitat with the selected scene/navmesh, validates both waypoint segments and path clearance, then publishes `camera_startup_validated` as `habitat_route_runner`.
7. The runner validates the explicit start marker and reaches the declared boundary immediately before its first `sim.get_sensor_observations()` call. Dry-run mode returns there; it does not call that function.
8. Stop the route child, close all five fake workers, stop only this run's viewer server, and write terminal startup status.

The ledger records 21 events. The route preflight checked 14 samples / 13 path segments, 0.514 m total geodesic route, with measured minimum obstacle clearance 0.150 m against the loaded navmesh. Its stated assurance remains navmesh path/footprint and action-level movement filtering; physics and static-object collision coverage were not enabled. Route preflight took 9.316 s. The HTTP/control handshake was verified, but no visual browser interaction occurred; this is **not GUI acceptance**.

### State ownership and counts

In the passing run, the coordinator owns the pilot lifecycle, run manifest, spawn record, and cleanup; the Habitat route runner owns `camera_startup_validated` and actual frame events; worker processes own their readiness replies. The event and manifest evidence distinguishes spawn from readiness and from first-frame evidence.

Final `run_manifest.json` status is `startup_preflight_passed`, `terminal=true`; `camera_runner.status=preflight_validated_stopped`, with `capture_has_started=false`, `capture_completed=false`, and first-frame/capture timestamps null. Processing stages remain `not_started`; route preflight is `capture_boundary_ready`; terminal mapping/flush is `not_run`. `camera_runner` exited 0, five fake workers each closed with exit code 0, and the run-owned viewer server exited 0. A process check after cleanup found none of those run PIDs still alive.

The D2 pointer/manifests were created during bootstrap with `pipeline_status=running` and were not rewritten after this intentionally pre-capture dry-run. For this run, the coordinator-owned `run_manifest.json` is the terminal lifecycle record; the D2 bootstrap status is not evidence that processing remained active. Since the server was shut down and GUI rendering was not tested, no claim is made about the post-shutdown viewer presentation.

Recorded totals: 5 fake-worker initialization attempts / 5 successes / 5 ready; one route-runner spawn / one startup validation; **0 model loads, 0 camera frames, 0 capture starts/completions, 0 inference calls, 0 keyframes consumed, 0 mapper pair dispatches, 0 unified revisions**. `capture_boundary_reached=true` means startup/preflight was validated at the boundary; it does not mean capture began.

The run contains 300 files under `viewer/evidence/`. They are four static panels for each of the 75 observations in the already existing C2 source evidence manifest, not newly captured frames. The evidence index binds to `hm3d00802_main29_640_quality_v1_attempt2/mapping/diagnostic/evidence_manifest.json` with source hash `fd8081b09d1a44f86f8158fbdbd355b962cb7c1f0a844126cda271921280316a`. The dry-run created no `capture/`, `events.jsonl`, `camera_keyframes.jsonl`, or `camera_producer_manifest.json`.

### Lifecycle and race checks

Model-free regression run:

```text
python3 -m py_compile integration_v2/camera_live_startup.py integration_v2/live_processing.py scripts/run_camera_live_pilot.py scripts/run_habitat_smooth_route_preview.py tests/test_camera_live_startup.py
python3 -m pytest -q tests/test_camera_live_startup.py tests/test_camera_live_coordinator.py
47 passed in 0.65s
```

The targeted tests exercise controlled alternate scheduling of coordinator/route validation, a delayed manifest read, valid and invalid run/hash/stale start markers, the browser-ready callback regression, cancellation before first frame, child failure after spawn, terminal stage states with no data, and cleanup/error preservation. Scheduling uses explicit synchronization rather than random timing. These tests are separate from the two dry-run attempts.

### Provenance and immutable evidence

Hashes identify the source/config that the passing run actually used:

| File | SHA-256 |
|---|---|
| `integration_v2/camera_live_startup.py` | `65eaa9387c8cf79adea5f8924cdeeac4b48ced5aee97a4c24e3b953485e8f938` |
| `integration_v2/live_processing.py` | `d1be2771158905eda387eafba9b3d481699614c387a7aa3923684878c907396d` |
| `scripts/run_camera_live_pilot.py` | `d48de8b888eaafdd1807c9709a8c799769d14a51b5cc857c132874597535144f` |
| `scripts/run_habitat_smooth_route_preview.py` | `d2382408c7c4bc4936ba9986065d10d8216633888fb457c984d08d28e0142821` |
| `configs/habitat/hm3d_minival_v02_00802_t1_1_turn_continue_paced_640x480_v2.effective.json` | `fef21191f9ba2fd6b1c219379289630748ea5876797947d61ba15b3e624fc377` |
| `configs/room_semantics/t4_live_room_belief_v1.json` | `da4077923da86d1e7288ef9f51413792634b045009bf66b9c832a9dbd169f022` |
| C2 source `run_manifest.json` | `48e62b2f4031061d77c69fd603269e787074e600d5ad89f7dc5a0b0a6c4753` |
| `tests/test_camera_live_startup.py` | `74c6019bfb63990eefdc0a056a08a4d0ec1a94c4b0b5b8bb09104ecb4c2da558` |

Hashes of the passing run's primary records:

| Artifact | SHA-256 |
|---|---|
| `run_manifest.json` | `da15e7bfe621359ec56d553ca3e4239395dea4a6d104a99dbdb9645b157964e0` |
| `pilot_bootstrap.json` | `9c73bc4758e70894f6c11c7213f66d12934ab210fceb95298a2f1f5520943e95` |
| `execution_ledger.jsonl` | `bbb70d14433a6163c5f18e51f4d87ae08597749f55e3d8c625d2e98bccfbed21` |
| `control/start.json` | `b7f8b3642f88bfc7f24f8689ef9dca4a2405b000ae5a94128986a0c6cb3499d3` |
| `logs/camera_producer.log` | `6fa6608d00ca24003a86f009b542ae35300e6b116849602590f443ded352c01b` |
| `viewer/d2_viewer_entrypoint.json` | `2554b749cc27b4c11dfc4cbdc6eadc2964d13e1fe4d122b9636e5acbea622b3a` |
| `viewer/d2_xyz_manifest_camera_live.json` | `e6840edc9f8411e54639c103a996f22de13f62e13b5a55a1440e0cc28a997fc4` |
| `viewer/evidence_index.json` | `039a08a97576bfeea91cc415a02a22db90771d15415e7de297c6a4e461be103c` |

All run artifacts are under `artifacts/live_processing_runs/t4_startup_contract_closure_20261019_v2/`. The older failed runs and earlier addendum were not rewritten.

### Gate result and limit

**Startup contract passed** for the model-free pre-capture path: the exact launcher/bootstrap/HTTP control/IPC/subprocess/route validation reached the capture boundary and shut down cleanly. The previous v1 dry-run failure and the later real pilot without a preceding complete dry-run remain accurately recorded.

This does not validate actual RGB-D observation, first-frame transitions on a real producer, model initialization, perception, mapper updates, capture/graph overlap, GUI rendering, room scoring, or collision beyond the route-preflight assurance stated above. D2's HTTP assets/control endpoint were checked, not its visual GUI. T4 live pilot remains pending.

Exactly one next pilot is proposed for separate approval: a fresh run on the same locked HM3D 00802 T1.1 route/config and current source, with D2 ready first, no retry after first capture, and the existing capture/perception/object-room/unified revision path. Suggested cap: 120 minutes for that single real attempt. Acceptance should separately record perception start versus capture end, first unified commit versus capture end, and GUI render evidence; verified-only room scope stays unchanged. This pilot was **not** run under the current authorization.

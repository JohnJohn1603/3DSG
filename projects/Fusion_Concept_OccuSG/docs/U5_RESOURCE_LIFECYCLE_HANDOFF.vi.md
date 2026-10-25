# U5 resource binding và lifecycle handoff

Ngày chạy: 2026-10-09. Không thay baseline, threshold, model, vocabulary, room rule hoặc policy association.

## Regression và resource binding

Lần full-suite trước ghi **19 failed / 20 skipped**. Sau đó test IDs đã được đối chiếu với test collection; các resource-only skips nay chạy bằng resolver dùng chung `locks/external_resources.json`. Resolver xác minh path ngoài workspace, role input-only, kích thước, SHA-256, executable bit và `ldd`; `--integration-resources` fail trước collection nếu resource bắt buộc thiếu.

### 19 failed trước khi bind resource

- HM3D lock: `tests/test_hm3d_preparation.py::test_hm3d_template_is_unselected_and_keeps_semantic_gt_out_of_predictor`.
- M11 native paths: `tests/test_m11_native_smoke.py::test_native_pointcloud_m2_to_m10_smoke`; `tests/test_m11_rgbd_smoke.py::test_synthetic_rgbd_native_m1_to_m10_smoke`; `tests/test_m11_two_room_sequence.py::test_two_room_native_rgbd_sequence_and_exports`.
- DuDe/M4 native binary (8): `tests/test_m4_dude.py::test_duDe_real_kernel_rectangle_l_doorway_obstacle_and_unknown`; `test_m5_adapter_commits_accepted_regions_and_preserves_current_adjacency`; `test_opt_in_preprocessing_diagnostics_include_cleaned_and_black_masks`; `test_extended_unknown_boundary_policy_is_explicit_and_clamps_unknown_overlap`; `test_empty_then_real_update_guard_reject_does_not_age_tracker`; `test_worker_rejects_parameter_change_and_can_explicitly_reset`; `test_resolution_change_requires_reset_and_validation_is_strict`; `test_decomposer_handles_map_growth_and_origin_shift_in_same_process`.
- M4/M5/M8: `tests/test_m4_degenerate_resolution.py::test_degenerate_single_pixel_and_second_resolution_after_reset`; `tests/test_m4_m5_m8_integration.py::test_m4_m5_doorway_sequence_promotes_stable_rooms_then_stale_rejection_preserves_them`.
- Pinned source oracle parity (3): `tests/test_m4_source_parity.py::test_worker_parity_with_direct_pinned_source_oracle_on_fixed_fixtures`; `test_live_worker_matches_live_direct_source_for_demo_sequence_and_fresh_final`; `test_live_doorway_reveal_updates_geometry_and_adjacency_with_source_parity`.
- Stateful DuDe worker (2): `tests/test_m4_stateful_worker.py::test_repeated_update_reports_source_reuse_branch_and_preserved_geometry`; `test_demo_backend_rejections_preserve_m5_evidence_until_source_accepts_again`.

One intermediate resource-bound rerun exposed a real M11 output-manifest bug: it required an external locked binary path to be workspace-relative. The manifest now records explicit path and hash; no assertion was weakened.

### 20 resource skips before binding

- `tests/test_m2_octomap.py`: `test_octomap_scan_updates_match_golden_fixture`; `test_octomap_key_sets_match_independent_insert_scan_harness`; `test_edge_case_key_sets_match_independent_insert_scan_harness[m2_octomap_edgecases.scan]`; `test_edge_case_key_sets_match_independent_insert_scan_harness[m2_octomap_edgecases_reversed.scan]`; `test_resolution005_fixture_matches_reference_key_sets`.
- `tests/test_m2_worker.py`: `test_python_worker_keeps_one_octomap_across_updates`; `test_worker_snapshot_exposes_accumulated_leaves_for_projection`; `test_worker_validates_requests_and_close`; `test_worker_timeout_terminates_hung_child`; `test_worker_timeout_covers_partial_response_before_newline`; `test_worker_write_timeout_covers_child_that_does_not_read_large_request`; `test_worker_invalid_response_enters_explicit_error_state`; `test_worker_accumulates_hit_and_miss_to_clamping_thresholds`.
- `tests/test_m3_native_integration.py`: `test_integrate_project_integrate_project_uses_accumulated_native_tree`; `test_partial_update_expands_map_and_shifts_world_origin`; `test_full_refresh_is_stable_across_repeated_calls`; `test_native_callback_reference_matches_metric_bounds_and_deletion_cases`; `test_native_projection_rejects_invalid_mode_before_request`; `test_clean_full_refresh_uses_new_converter_on_same_tree_and_matches_source`.
- `tests/test_m7_navigation.py::test_native_m2_integrate_m3_project_then_m7_smoke`.

Current command: `python3 -m pytest --integration-resources -q` — **363 passed, 0 failed, 0 skipped** (86.51 s). Targeted lifecycle/backend/resource set — **81 passed**; after fixing native diagnostics JSON conversion, focused regression — **25 passed**. No full-suite test was skipped to get a green result.

## Lifecycle contract / gate

- Protocol v1: active-canonical-object snapshot; append-only lifecycle history; explicit event ID, type, object/target ID, frame/time, causal revision, provenance and evidence IDs.
- M9 handles explicit create/update/canonical-alias/retire events, validates backend capability declarations, active IDs, aliases and revisions; it removes retired/aliased object nodes and stale edges, then recomputes accepted M8 membership. M10 persists and replays graph lifecycle history with snapshot/delta. Reload does **not** reconstruct mutable backend runtime state.
- Synthetic scripted backend tests cover create/update/alias/retire, room ownership reconciliation, stale/fresh membership, roundtrip/delta replay, alias cycles, absent target, duplicate/conflicting events and backend mutation failure. Failure remains `failed/replay-required`; graph commit is preserved, backend rollback is not claimed.
- Source and geometry real backends currently advertise/emit create/update only. No real alias/retire event was observed, and no merge/retirement policy was introduced. Policy status is **unsupported/deferred**, not a completed runtime capability.

## Fresh 29-frame cached replay

Native preflight passed. Both executables were read from the baseline in input-only mode and matched `locks/external_resources.json`:

| Resource | SHA-256 | Size | `ldd` |
|---|---|---:|---|
| M2/M3 | `bd171a8ac9974eec18957af97e6fc369729358ca62985390eb994c26f2040b29` | 422,552 B | pass |
| DuDe M4 | `7868c892f3584faa17712e1ede0660006d97531b05be5c258fe303015f1ef959` | 940,344 B | pass |

`replay-cached` reuses capture/P2/P3 caches but reruns native M1–M3 RGB-D mapping; no capture, detector, P2, P3 or OpenCLIP inference was rerun. Each replay got a unique output.

| Graph backend | Run | Objects | Runtime | Peak RSS | Fresh/reload/delta | Integrity |
|---|---|---:|---:|---:|---|---|
| Source wrapper | `u5m9_20261009T063525.490715Z_455818` | 9 | 129.9 s | 543,352 KiB | pass / pass / pass | 0 dangling, self, conflicting owner |
| Geometry-only | `u5m9_20261009T063739.654426Z_7e7fe5` | 11 | 144.8 s | 549,008 KiB | pass / pass / pass | 0 dangling, self, conflicting owner |

Source emitted 12 create/update events; geometry emitted 11 create events. Every event has detection evidence ID and causal frame/time. Geometry assignments matched the locked offline reference on all 12 evaluated assignments. M3 arrays and M4 result fields matched on all 29 frames; M5 room geometry matched after excluding dynamic object membership. Detailed comparison: `artifacts/runs/u5m9_20261009T063525.490715Z_455818/replay/backend_comparison_m2_m5.json`.

## Gate status

| Gate | Status |
|---|---|
| Shared resource resolver / exact lock binding | Pass |
| Source direct vs source-wrapper graph/delta parity | Pass; hermetic M9 regression |
| Geometry-only through M9 and offline assignment parity | Pass on locked replay |
| Lifecycle contract and synthetic reconciliation | Pass |
| Lifecycle alias/retirement observed on real backend | Not observed; backend does not emit these events |
| Snapshot reload / delta replay / fresh-process determinism | Pass on both 29-frame runs |
| Resource-bound full suite | Pass, 363/363 |
| Dense 280-frame replay | Not run |

**Overall U5 remains partial.** Plumbing/reconciliation is implemented and tested, while merge/retirement decision policy is intentionally deferred. Source remains default; geometry-only is opt-in; geometry+appearance remains ablation-only; U4-OV remains diagnostics-only. No quality-improvement claim is made.

# U3 causal prefix/holdout verification

Status: **completed as a diagnostic, not promoted**. U4, class confirmation, room-score changes, and graph mutation were not performed. The source association manager and the earlier U0/U1/U2/U3 artifacts remain unchanged. All new outputs are in `artifacts/audits/u3_causal_prefix/`.

## Locked input and causal setup

- Immutable index: `causal_input_index.json`, SHA-256 `b6dbc463dfb410bf21b365338efc39eb5075490bcaf4c1ba3f779832c912fc89`. It selects 622 P3-accepted records by stable detection ID: 583 in frames 0–230 and 39 in frames 231–238. The index binds RGB, mask, crop, feature, geometry and available point-set hashes. It is an input lock, not an identity assignment.
- The 47 U2 vectors were reused. The remaining 575 crops were encoded from saved RGB and saved detections. No YOLO, P3, native M1–M10, GT, room label, or semantic instance ID was used to make predictions. Saved P3 AABB centers/extents/layers were used for geometry. Sidecar point sets exist for 47/622 observations; the earlier U1 conclusion remains fixture-only parity, not exact point-set parity for the full old cache.
- Each of source/raw, source/dedup, geometry-only, and geometry+appearance started from an empty, separate state and consumed frames in order 0–238. The prefix state snapshot is after frame 230. No mode was initialized from source assignments or GT; later observations did not revise earlier decisions.
- Source manager code was kept intact. The extra source/raw mode retains all 622 observations. Source/dedup, geometry-only, and geometry+appearance use the same deterministic same-frame mask deduplication; 16 duplicate masks were suppressed in the full sequence. There were no duplicates in the holdout itself.
- U3 thresholds were copied unchanged from the previous experiment and hash-locked in `association_config.locked.json` (SHA-256 `f0296efcd5e943d006b436e91a4ad4217b43604fd34fa60e2a386c079d67bf0c`): candidate center radius 0.75 m; appearance-mode minimum AABB IoU 0.01; geometry-only minimum IoU 0.05; cosine threshold 0.72; combined score threshold 0.55; ambiguity margin 0.05; duplicate mask IoU 0.98; equal layer gate. No values were selected or changed after reviewing frames 231–238.

## Metric contract and coverage

Pairwise scores count cross-frame observation pairs with at least one endpoint in frames 231–238. A pair enters precision/recall only if **both** detections have evaluator status `matched` and a non-null evaluator semantic ID. Same-frame pairs are excluded. GT is used after all predictions are frozen.

- TP: same evaluator ID and the two observations share a predicted track.
- FP: different evaluator IDs but the observations share a predicted track (false merge).
- FN: same evaluator ID but the observations are on different tracks, or either endpoint was unmatched/deferred/unassigned.
- TN is diagnostic only and is not part of precision or recall.
- GT-unmatched/ignored or unresolved pairs are excluded from TP/FP/FN; they are never counted as correct. Deferred/unassigned observations are reported separately and their eligible same-GT pairs count as FN.
- “False merge tracks touched by holdout” counts predicted tracks containing at least one scored pair from different evaluator IDs. Fragmentation counts evaluator identities assigned to more than one predicted track; excess tracks is the total additional assigned tracks beyond one per such evaluator identity. Deferred records do not create a predicted track and remain visible in defer rate and FN.

Holdout evaluator coverage was 34/39 detections (87.18%); 5 were unmatched/ignored. Across all cross-frame pairs involving a holdout observation, 9,079 of 22,772 deduplicated pairs were excluded for missing evaluator identity, leaving 13,693 scored pairs. Prefix coverage was 392/583 (67.24%); 191 records were unmatched/ignored. Source/raw has a different denominator because it retains duplicate observations: 13,829 scored pairs and 9,567 exclusions out of 23,396 pair rows.

## Holdout results

| Mode | TP | FP | FN | Pair precision | Pair recall | False-merge tracks | Fragmented evaluator instances (excess tracks) | Defer / 39 | Mode replay | Peak sampled RSS |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Source/raw | 270 | 147 | 112 | 0.6475 | 0.7068 | 6 | 7 (14) | 0 (0%) | 0.511 s | 54.36 MiB |
| Source/dedup | 269 | 147 | 110 | 0.6466 | 0.7098 | 6 | 7 (14) | 0 (0%) | 0.530 s | 54.38 MiB |
| Geometry-only | 338 | 1 | 41 | 0.9971 | 0.8918 | 1 | 5 (6) | 4 (10.26%) | 0.572 s | 55.50 MiB |
| Geometry + appearance | 317 | 11 | 62 | 0.9665 | 0.8364 | 1 | 5 (6) | 8 (20.51%) | 0.675 s | 55.64 MiB |

Both source modes use `ObjectAssociationManager` as-is. Comparing them isolates the effect of deduplication on this sequence: it removed the duplicate rows but changed holdout pair metrics only slightly (precision 0.6475→0.6466, recall 0.7068→0.7098). Geometry-only substantially outscored the source modes on this temporal segment. Adding appearance reduced precision by 0.0306 and recall by 0.0554 against geometry-only, added 10 FPs and 21 FNs, and doubled holdout defer count from 4 to 8. False-merge and fragmentation counts were unchanged. Under this locked configuration, appearance did not help; geometry-only is the simpler candidate for any later review, not a promoted implementation.

Appearance changed the prior-track link set for 7 of 39 holdout observations. It deferred several observations that geometry-only matched, including one detector `bed` observation whose evaluator identity was labeled `table`; it also created a link for one observation where geometry-only created a new track. These are outcomes, not threshold-tuning evidence.

## Table/bed and adjacent detections

Posthoc evaluator matching shows frame 232's detector `bed` observation matched evaluator category `table` (ID 417): geometry-only linked it to the earlier evaluator-consistent track; geometry+appearance deferred as ambiguous. Frame 233's detector `dining_table` matched evaluator category `table` (ID 357), but both modes deferred it. These IDs were used only for evaluator-side review; no instance ID or class was hard-coded into association.

The contact sheets also show several crops of the same evaluator ID represented with detector classes dining_table, sofa, and bench. For example, frame 233's dining_table crop and later sofa/bench crops are evaluator ID 357. The images look like views of the same table-like surface, illustrating that evaluator-instance identity and a physical-object interpretation are not interchangeable. Among the nearby false-merge examples, geometry-only linked the dining-table crop at frame 230 to a different evaluator identity seen as suitcase at frame 238; geometry+appearance instead linked that dining-table crop to the other suitcase-labeled evaluator identity at frame 238. These are evaluator-side false merges, not evidence to revise labels. Neighboring chairs, screens, a piano-like object, and plant/vase records were also present in the pair review. Unknown evaluator pairs remain unscored. This segment is a same-scene temporal holdout, not a scene holdout and not evidence of generalization.

The posthoc package contains all 23,396 cross-frame pair rows, including crop refs and all four track assignments, plus contact sheets for 74 cross-class/same-evaluator pairs, 81 representative same-class/different-evaluator pairs, and 151 likely false-merge pairs. The full predictions/candidates are in `decisions_*.jsonl` and `candidates_*.jsonl`; the review export did not alter thresholds or predictions.

## Runtime, memory, tests, and commands

- Feature extraction: 575 new crops, 116.45 s accumulated inference (0.203 s/crop), 144.56 s script time; measured peak RSS 1,489 MiB (1,525 MiB from `/usr/bin/time -v`). The 47 U2 features were reused. Runtime was higher than the preliminary estimate; configuration was not changed.
- Geometry input inventory: AABB center/extents/layer available for all 622; point-set sidecars for 47. Rebuilding the original sampled point sets from cached RGB-D and masks was profiled on a deterministic 16-record sample: 2.212 s total, mean 0.138 s, p95 0.426 s, peak RSS 141.7 MiB. Linear estimate for 622 is about 86 s. Full point-set reconstruction was not run because the locked U3 association uses the already-saved AABB geometry; it would not change this experiment.
- Four-mode causal runner: 3.87 s end-to-end, 2.286 s aggregate per-mode replay, process peak RSS 56.77 MiB. Per-mode time and sampled RSS are in the table and `causal_holdout_report.json`.
- Tests run: `pytest -q tests/test_u3_experimental_association.py tests/test_m6_object_association.py` — 18 passed. Script syntax checked with `python3 -m py_compile` for the three new U3 causal scripts. This is targeted coverage, not a full project suite or runtime qualification.
- Reproduction commands from workspace root:

  ```bash
  .venv-appearance/bin/python scripts/extract_u3_causal_features.py
  python3 scripts/run_u3_causal_prefix.py
  python3 scripts/export_u3_causal_holdout_review.py
  pytest -q tests/test_u3_experimental_association.py tests/test_m6_object_association.py
  python3 -m py_compile scripts/extract_u3_causal_features.py scripts/run_u3_causal_prefix.py scripts/export_u3_causal_holdout_review.py
  ```

The capture/cache inputs and prior artifacts were read-only inputs; new feature/replay/review artifacts were written only under the workspace. The baseline tree and reference repositories were not modified. Next step is an independent review of the locked ledgers/contact sheets and, if desired, a separately specified longer or different-scene evaluation before considering promotion. Do not proceed to U4 from this result alone.

# U4 offline semantic fusion — status

**Completed as an offline sidecar; not promoted. U5 was not started.** Geometry-only remains the primary association candidate; source/source-dedup is the baseline, and geometry+appearance remains the U3 ablation. U3 assignments and all U3 thresholds are hash-locked inputs. No runtime graph, source manager, room label, or baseline was changed.

## Pre-review and frozen predictor

Before U4 output generation, the limited review covered the U3 false merges around the table/suitcase crops and evaluator IDs whose crops carry `dining_table`/`sofa`/`bench` or `bed` labels. Where visual and annotation evidence did not settle physical identity, it is marked **uncertain**. The HM3D labels were not edited. See [pre-review scope](U4_PREREVIEW_SCOPE.vi.md) and `artifacts/audits/u4_semantic_fusion/u4_prereview_manifest.json`.

The locked U4 algorithm is `causal_view_capped_class_evidence_v1`, config SHA-256 `ba46de2d5d656afe522c5741d3d8e6f210734282bddb379f17466fed1ff3dcd9`:

- Keep each observation's saved P3 top-1 class and confidence as raw evidence.
- Quantize camera-to-object view into 30° azimuth/elevation bins. Only the highest-confidence observation in each bin contributes; ties choose the earliest frame and detection ID. This caps repeated/nearly identical frames and duplicate masks.
- Confirm only with at least two independent bins, score margin ≥0.25 and winner fraction ≥0.67. Otherwise remain tentative/conflicted.
- Track `initial_class`, `resolved_class`, and class status separately. Evidence is recomputed causally; losing evidence can deconfirm a class, and a later qualified class can replace it. No class result changes U3 assignments.
- Predictor reads no GT or room labels. Evaluator data is opened in a separate posthoc script after prediction artifacts are written.

This view-capped evidence state machine is a project adaptation. ConceptGraphs informed multi-view feature/evidence accumulation generally; this U4 implementation does not claim direct ConceptGraphs source parity.

## Results on fixed assignments

The first-observation label and final fused label are compared on each mode's exact same assigned tracks. These are track coverage/correction counts, not class accuracy; no confirmed class mapping exists for correctness scoring.

| Fixed assignment mode | Tracks | Confirmed | Confirmed coverage | Confirmed class differs from first | Class correction events | Median / p90 time to first confirm | View bins at first confirm |
|---|---:|---:|---:|---:|---:|---:|---:|
| Source/raw | 72 | 34 | 47.2% | 0 | 0 | 2.65 / 20.4 s | median 2, range 2–3 |
| Source/dedup | 71 | 34 | 47.9% | 0 | 0 | 2.60 / 20.4 s | median 2, range 2–3 |
| **Geometry-only** | **79** | **27** | **34.2%** | **3** | **3** | **2.80 / 16.1 s** | **2 for all first confirmations** |
| Geometry + appearance | 75 | 26 | 34.7% | 3 | 3 | 2.25 / 16.1 s | 2 for all first confirmations |

Coverage denominator is the number of tracks assigned by that fixed U3 mode. A confirmed class is not asserted correct. Across geometry-only and geometry+appearance, three tracks ended with a confirmed class different from the first observation; source modes had no such changes. There were no real-sequence deconfirmation events, although a synthetic test verifies a previously confirmed class can be deconfirmed and replaced. The geometry-only track statuses at sequence end were 27 confirmed, 6 conflicted and 46 tentative.

### Class-aware correctness is unavailable

The locked HM3D category mapping snapshot has 15 provisional, 2 unmapped and 5 unsupported categories, with **zero confirmed mappings carrying reviewer/date/decision provenance**. Therefore:

- Class accuracy/F1 are unavailable.
- Counts of wrong classes corrected and correct classes changed incorrectly are **N/A**, not zero.
- We do not use provisional lexical mappings to score bed/table, sofa/chair or other classes.

The evaluator observation matches provide posthoc categories/IDs; those IDs never entered U4 class decisions. `u4_evaluation_report.json` records the mapping hash and the unavailable metrics explicitly.

## Requested cases and controls

- **Table→bed:** evaluator ID 417 has table annotations, while some detections are `bed` and other observations in the same geometry-only track include `dining_table`/`sofa`. That track ends conflicted with `resolved_class=null`, so U4 abstains. A separate earlier geometry-only fragment ends confirmed `dining_table`; the `table`→`dining_table` mapping remains unconfirmed, so this is not scored as correct. Source/dedup retains a bed-only tentative track for a later fragment. U4 does not repair the fragmentation or change assignments.
- **Evaluator ID 357:** reviewed crops include detector classes `dining_table`, `sofa`, and `bench` against evaluator category `table`. Geometry-only evidence remains conflicted on the mixed-label track; physical identity is marked uncertain. No GT identity is used to merge the tracks.
- **Table/suitcase false merge:** geometry-only U3 track `u3-0015` contains detector labels beginning with `tv`, then `dining_table`, and later `suitcase`. U4 resolves that fixed track to `suitcase` after two view bins. Posthoc evaluator records include table ID 357 and printer ID 351 in that track, so this is a known false-merge failure mode; the semantic result cannot be treated as a correction. Geometry+appearance likewise has a table/suitcase false merge with a separate computer-tower-labeled evaluator identity. Crop/evaluator disagreement remains uncertain; neither GT nor detector labels were edited.
- **Controls:** chair/sofa and piano/bench transitions also appear in the sidecar. They are retained in the CSV and ledgers; without confirmed category mappings they are not counted as correct or harmful switches.

The detailed posthoc slice is `artifacts/audits/u4_semantic_fusion/u4_posthoc_case_review.csv`. It shows observation ID, raw/initial/resolved class, status, fixed U3 track, and evaluator category/ID for review.

## Causality, resources, and tests

- All 622 U3 input observations and fixed assignments were reused. The predictor replay is causal in frame order; no future observations revise earlier states. Source/raw, source/dedup, geometry-only and geometry+appearance have separate label states.
- U3 holdout frames 231–238 were already viewed and are **seen data**, not an untouched U4 holdout. Freeze this U4 config and get a new test set before any U4 tuning or promotion.
- Predictor runtime: 5.04 s across the four modes; peak RSS 42.97 MiB. No model/dependency install, detector run, P3 run, native M1–M10 replay or room-label counterfactual was needed.
- Tests: `pytest -q tests/test_u4_class_fusion.py tests/test_u3_experimental_association.py tests/test_m6_object_association.py` — **23 passed**. `py_compile` passed for the U4 runner, evaluator, module and tests. Full project suite was not run.
- Reproduction:

  ```bash
  pytest -q tests/test_u4_class_fusion.py tests/test_u3_experimental_association.py tests/test_m6_object_association.py
  python3 -m py_compile scripts/run_u4_semantic_fusion_offline.py scripts/evaluate_u4_semantic_fusion_offline.py src/standalone_sg/semantics/u4_class_fusion.py tests/test_u4_class_fusion.py
  python3 scripts/run_u4_semantic_fusion_offline.py
  python3 scripts/evaluate_u4_semantic_fusion_offline.py
  ```

New outputs are isolated under `artifacts/audits/u4_semantic_fusion/`. Room-label counterfactual was not run. Next: keep the config fixed, confirm class mappings/annotations with reviewer provenance or collect a new labeled test set, then assess U4 coverage and correctness there. Stop before U5.

## Human review and next test lock

A compact, fillable package is at `artifacts/audits/u4_review_package/index.html`; answer identity and class separately in `identity_review_responses.csv`, then review only the relevant category rows in `category_mapping_review.csv`. Evaluator annotations appear as posthoc context and do not answer the physical-identity question. The control chair track is a candidate control, not assumed correct.

The next test split is already locked by chronology, not by U4 outcomes: same-scene frames 239–278 (40 frames, 90 cached P3 observations). Its manifest and hash are in `artifacts/audits/u4_temporal_test_lock/`. It has not been replayed/scored. For temporal causality, replay prefix 0–238 with unchanged U3 config, then process the locked interval with unchanged U4 config. This is a same-scene temporal test, not a scene holdout; once reviewed/scored, it becomes development data.

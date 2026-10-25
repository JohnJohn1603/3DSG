# U4-OV fresh-scene diagnostic — HM3D 00800

**Status:** locked route executed; association/class evaluation incomplete because P3 yielded only two temporally isolated observations. No promotion. U3/U4-OV model, vocabulary, prompts, thresholds, and baseline remain unchanged.

## Frozen setup

- Scene: `00800-TEEsavR23oF`, the first/lowest-ID locally available minival scene other than 00802 with basis mesh, semantic mesh, semantic descriptor, and navmesh. Selection used asset completeness and mesh size, not model scores.
- Route: 5 fixed floor anchors × 3 headings, 15 RGB-D frames at 160×120, 90° HFOV, 1.5 m sensor height. Validation passed for all 15 poses and consecutive navmesh paths. Lock was written before capture; SHA-256 `48120a29c35971ed2a228acf4aa51a49062c4470800b8813a389afb4fd14e519`.
- The four modes consumed the same saved P2/P3 inputs and each started from independent empty state. Predictors received no semantic GT or room labels. U4-OV used the existing ViT-B-32/OpenAI checkpoint, 105-label vocabulary and text cache.
- U3 config SHA-256 `f0296efcd5e943d006b436e91a4ad4217b43604fd34fa60e2a386c079d67bf0c`; U4-OV config SHA-256 `e5d265700c355e623f5ceefefef483ea0c10ae1995991eb1a1dd133ea1afb98e`; vocabulary SHA-256 `879ed505b4a433ce23dfef2b3b6e9071238a1593b4f7f0eb281324286c977427`.

## Taxonomy proposal for evaluator approval

Treat lexical/canonical synonyms separately from parent–child relations. These proposals are evaluator-only; none is predictor input or confirmed yet.

| Proposal | Relation | Evaluator treatment |
|---|---|---|
| `couch` ↔ `sofa` | synonym | May map to one evaluator label if approved |
| `dining table` ↔ `dining_table` | spelling/canonical form | May map to one evaluator label if approved |
| `potted plant` ↔ `potted_plant` | spelling/canonical form | May map to one evaluator label if approved |
| `armchair` → `chair` | child → parent | Keep exact labels distinct; optional parent-level score only if approved |
| `dining_table` → `table` | child → parent | Keep distinct; **do not treat table as dining_table** |
| `coffee_table` → `table`; `side_table` → `table` | child → parent | Keep distinct; optional parent-level score only if approved |

Please confirm or reject each row independently. A parent-level score would be reported separately from exact-match accuracy. HM3D category-to-predictor mapping also needs explicit approval after the evaluator lists categories visible in this route. No mapping has been auto-confirmed.

## Capture and perception results

- P2: 16 detections across 15 frames; raw labels were traffic_light (4), suitcase (4), truck (4), person (2), dining_table (1), umbrella (1).
- P3: 2 accepted, 14 rejected by the locked minimum-depth/cluster rules. Accepted IDs: `hm3d-00800-u4ov-dense-0008:det-0002` and `hm3d-00800-u4ov-dense-0014:det-0001`. They occur in different frames and their centers are 2.50 m apart. No same-frame duplicates were found among accepted observations.
- The small 160×120 render and locked detector/geometry pipeline produced too few valid repeated observations for association or multi-view analysis. No threshold or route adjustment was made after seeing these outcomes.

## Association

| Mode | Assigned observations | Tracks | Runtime |
|---|---:|---:|---:|
| Source raw | 2/2 | 2 | 0.00067 s |
| Source deduplicated | 2/2 | 2 | 0.00034 s |
| Geometry-only | 2/2 | 2 | 0.00016 s |
| Geometry + appearance | 2/2 | 2 | 0.00006 s |

All modes created two independent tracks. For the second observation, source failed its same-class and same-pose gates; both candidates failed the 0.75 m center-radius gate. Source raw and deduplicated were identical because no accepted same-frame duplicate existed.

Pairwise precision/recall, false merge, and fragmentation are **unavailable**, not zero: there are no eligible cross-frame pairs for the same physical object, and no human identity review has been supplied. The semantic assets are present, but GT matching cannot create temporal evidence. Runtime for the actual association replay was below 1 ms per mode; sampled peak RSS was 33.7 MiB for the runner process.

## Semantic classifier and review

- U4-OV single-view: 1 classified (`coffee_table`), 15 conflicted, 0 unknown. Scores are cosine similarities, not calibrated probabilities.
- Multi-view output: unavailable; only two P3 observations survived and neither had another view of the same predicted track.
- Feature extraction + classification: 16 image crops in 16.74 s; peak process RSS 1506.8 MiB on CPU. Same U2 checkpoint and locked text cache were reused.
- The frozen deterministic review sample contains one classified and four conflicted observations. The association-hard stratum is empty because there was no defer or source/candidate disagreement. Review page: `artifacts/runs/hm3d_00800_u4ov_test_v1/u4ov/review/index.html`.
- For each selected detection, answer separately: (a) which reviewed detections show the same physical object, with uncertain allowed; (b) its class, or uncertain. Responses apply only to the listed detection IDs.
- Causal correctness, coverage after human review, time-to-confirmation, harmful changes, final-map accuracy, and class-aware GT metrics remain unavailable until observations are reviewed and evaluator mappings are approved. This run has no usable multi-view class evidence.

## Decision and limits

This run does not support a conclusion about U3 association or U4-OV classification quality. The immediate blocker is route/input coverage: only two observations pass locked P3, and they do not revisit one object. Keep the baseline and current candidates unchanged. Before another acceptance test, improve route coverage using a separate development run while retaining the frozen sensor and P3 profile; then lock a new route/test before viewing its predictions. Any use of this run to revise route or algorithm makes HM3D 00800 development data and requires a fresh test scene for acceptance.

If the purpose is to test the classifier itself, the provided review can still identify obvious false detections, but it cannot establish multi-view accuracy. If the purpose is association, first ensure a route yields repeated P3-accepted objects without changing P3 thresholds.

## Reproducibility artifacts

- Pre-capture route/protocol lock: `artifacts/audits/u4ov_new_scene_hm3d_00800/test_lock.json`
- P2/P3 caches: `artifacts/runs/hm3d_00800_u4ov_test_v1/detection_2d/`, `detection_3d/`
- Association logs and ledger: `artifacts/runs/hm3d_00800_u4ov_test_v1/association/`
- U4-OV predictions/features/review: `artifacts/runs/hm3d_00800_u4ov_test_v1/u4ov/`
- Commands executed: `scripts/capture_habitat_replica.py`; `scripts/run_replica_cad_instance_segmentation.py`; `scripts/project_detection_masks_to_3d.py`; `scripts/run_u3_hm3d_route_v1.py`; `scripts/run_u4ov_hm3d_route_v1.py`; `scripts/build_hm3d_u4ov_review_html.py`.

No test suite was run. No files in the read-only baseline or reference repositories were modified, and no graph/room score/U5 integration was performed.

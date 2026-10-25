# Region/object room-label module — checkpoint S0–S6

Date: 2026-10-04  
Status: **S0–S6 technical checkpoint complete on locked apt_0 replay; functional room-label quality remains unverified.**

## Baseline and boundaries

- Locked source run: `artifacts/runs/habitat_replica_cad_apt0_perception_dev_long_20261001`.
- 24 cached RGB-D frames; 40 2D detections across 12 observed 2D classes; 23 accepted 3D detection observations; 13 persistent ObjectNodes at the final frame.
- The original replay snapshot SHA-256 is `e0af99d47c32323a848b1175cdf411a1700220ed4ec7fae6056b455a2fdc02a4`. The semantic runner checked before/after equality; it did not modify the source snapshot.
- Pipeline config SHA-256 `210124b16fd3651c06fd548a1704f27595f61761dd1bfb4db4099e51895d1523`; M2/M3 binary SHA-256 `bd171a8ac9974eec18957af97e6fc369729358ca62985390eb994c26f2040b29`; DuDe binary SHA-256 `7868c892f3584faa17712e1ede0660006d97531b05be5c258fe303015f1ef959`.
- Detector weights SHA-256 `0bc32bc92e985b881141ef9bd2216e2a746f70519d0d24da9fc85decc4428cf4`; 2D detector config SHA-256 `ec64d9e8c508d54b313abf8495b15631fa95e047e894131db5c5fec7c2dfd0b3`; Detection3D cache SHA-256 `0963d4614d90bb12e284ef6fcf8e3595bcb697bdf3493ffed2c7fbdc992c9a9c`.
- Pose is the saved Habitat-Sim simulator state. Perception is the locked real RGB instance-segmentation cache. No room-function labels, scene asset names, mesh, or navmesh were provided to inference.

## S0–S6 result

- **S0:** Re-ran current P5 on the locked final M8 snapshot. The versioned profile preserves all P5 labels, scores, candidate order, and IDs (`p5_m8_baseline.json` vs `p5_m8_versioned_profile.json`). The M8 snapshot is unchanged.
- **S1:** Added typed graph-independent `RegionEvidence` / `RegionObjectEvidence`, M5 and M8 adapters, source-frame/pose/mask provenance, JSON round-trip, and tests. M5 labels all current M5 regions, including regions with zero members. Membership uses the existing `RegionMembershipResolver` and does not use camera visibility as room membership.
- **S2:** Added a versioned semantic profile based on the existing P5 weights/thresholds. No weight, cap, score, margin, or support threshold was tuned. Raw spelling aliases only normalize `dining table` and `potted plant`; the actual 2D/accepted-3D vocabulary and unsupported classes are listed in `detector_vocabulary_inventory.json`.
- **S3:** The rule labeler counts unique persistent ObjectNode IDs, not frame detections. It retains candidate scores, candidate-specific support, observed/eligible/supporting IDs, source-compatible `(class, rounded XY)` set signature, stable evidence hash, reason and timestamps. Scores remain uncalibrated additive evidence, not probabilities.
- **S4:** Offline timeline was generated from already saved M5/M8 trace and M6 diagnostics; no detector, projection, or segmentation was rerun. It records region additions/removals, object membership changes, stale reuse and accepted revisions. The locked sequence had **0 rejected/stale M5 frames**, so stale/reject behavior is covered by unit tests, not claimed as exercised by this capture. Object positions use M6 first-observation geometry; later detections add provenance only. No future-frame observations are included in earlier evidence.
- **S5:** 13 targeted semantic tests pass. Three fresh Python processes on the same input/config produced the same normalized labels/history/evidence/membership projection; normalized digest `34bcd215d0821dbbc49243c2f0f76e6f687c6d01765bf8333f7ebea2c3d5666a`. Spatial map PNG was byte-identical in the three runs (SHA-256 `10a99c9863fb65d9d153539549d6714ced92f49144a7b1a2f1dfb1de64eedd33`). Wall-clock processing timestamps were excluded from this comparison; semantic evidence/hash and geometry were not normalized away. No functional room GT is available, so accuracy is `null` and output remains not human-verified.
- **S6:** The final self-contained package is `artifacts/runs/region_object_room_labels_apt0_20261004_v3/`; see its `manifest.json` and `report.vi.md`.

## Final apt_0 result (descriptive only)

Five M5 regions remain separate. One is `predicted: living_room`; four are `unknown`; prediction coverage is 0.20. No region was merged, split, renamed, or removed by semantic inference. On the final frame, the M5 adapter's room ownership agrees with M8 `room_contains` for every object with an owner (zero differences). Across the 24-frame history, the report records 7 region additions, 2 removals, 16 object-membership changes and 4 outside/unassigned observations. These counts describe this replay; they are not room-segmentation or classification quality metrics.

The one M5 `living_room` is a rule hypothesis from observed objects. It is not confirmation that the physical area is in fact a living room. Four architectural boundary polygons do not constitute functional room-label GT.

## Source-aligned vs project extension

**OccuSG/source-aligned:** stable geometric room/region IDs, object-to-region direct membership, unique object identity, and object semantic signature represented as a set of class/rounded-position tokens. This is evidence representation, not a room-function classifier.

**Project extension:** rule scores that map object evidence to function names, abstention statuses (`unknown`, `ambiguous`, `stale`), region-level semantic history, provenance export, and the review page. No claim of source parity is made for room-function prediction.

## Reproduction and review

From project root:

```bash
RUN_ID=$(date +%Y%m%d_%H%M%S)
PYTHONPATH=.:src MPLCONFIGDIR=/tmp/m12-webots-mpl \
python3 scripts/run_region_object_room_labels.py \
  --replay artifacts/runs/habitat_replica_cad_apt0_perception_dev_long_20261001/replay \
  --trace artifacts/runs/habitat_replica_cad_apt0_perception_dev_long_20261001/grid_trace \
  --detection-3d artifacts/runs/habitat_replica_cad_apt0_perception_dev_long_20261001/detection_3d \
  --detection-2d artifacts/runs/habitat_replica_cad_apt0_perception_dev_long_20261001/detection_2d \
  --config configs/semantics/region_object_room_rules_replica_cad_v1.json \
  --output "artifacts/runs/region_object_room_labels_apt0_${RUN_ID}"
```

Runner không ghi đè output không rỗng. Mở `region_object_evidence.html` trong trình duyệt để xem từng object, RGB/mask overlay và frame provenance; checkbox ẩn nhãn/score semantic trong khi giữ region, membership và mask để người review có thể tự hình thành đánh giá. Trang này chỉ hỗ trợ review, không biến AI labels thành GT.

Các artifact quan trọng:

- `spatial_semantic_map.png`: M3 occupancy theo semantics gốc (unknown xám, free trắng, occupied đen), footprint M5, trajectory base thực tế và marker object; đây là bản đồ 2D, không phải 3D mesh.
- `region_labels.json`, `region_labels_history.jsonl`, `region_lifecycle_events.jsonl`: label hiện tại, per-frame history và tombstone/membership events.
- `region_evidence.jsonl`, `region_object_evidence.csv`, `region_object_evidence.html`, `evidence_previews/`: object evidence, stable IDs, mask provenance và review visuals.
- `actual_trajectory.csv`, `membership_audit.jsonl`, `detector_vocabulary_inventory.json`, `evaluation.json`, `manifest.json`.

## Regression

Targeted command:

```bash
PYTHONPATH=.:src pytest -q tests/test_room_semantics.py tests/test_region_object_room_semantics.py
```

Full repository regression: `PYTHONPATH=.:src MPLCONFIGDIR=/tmp/m12-webots-mpl pytest -q` — **281 passed in 122.82 s**. Nothing here changes M1–M10, M4 segmentation, P3, M6, detector weights, capture, or the source semantic baseline.

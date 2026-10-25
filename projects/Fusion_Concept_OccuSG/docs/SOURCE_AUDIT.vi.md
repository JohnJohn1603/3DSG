# Source audit — 07-10-2026

Đọc chọn lọc các đường chạy liên quan; không tuyên bố audit mọi dòng source hoặc đã chạy ConceptGraphs.

## Baseline source đã kiểm tra

- `src/standalone_sg/contracts/objects.py`: Detection3D/DetectionBatch.
- `src/standalone_sg/objects/association.py`: first-observation fields, sequential same-class/same-pose decisions.
- `src/standalone_sg/pipeline/builder.py`: injected manager, object export, M8 entities, graph commit integration.
- `src/standalone_sg/semantics/evidence.py`, `room_labeler.py`: RegionEvidence/state và additive rules.
- `scripts/project_detection_masks_to_3d.py`: cKDTree/fallback, accepted/rejected geometry output.
- `scripts/run_habitat_native_replay.py`: effective managers/native params, repeated verification.
- `scripts/scene_launcher.py`: env/registry strict keys, stage interpreters, output handling.
- `native/CMakeLists.txt`, `native/dude/CMakeLists.txt`: source lock/MapConversion and external-prefix requirements.
- `configs/launcher`, perception/semantic profiles, HM3D dense/eval profile; portions of Phase1/source lock.
- `ref_OccuSG/OccuSG` HEAD and clean status verified; reference behavior is consistent with audited M6 source.

Important facts:

1. Manager already returns full per-detection association; final graph alone loses this history. Export at the interface before building new inference logic.
2. Builder serialization is not fully generic: a plug-in manager returning new fields does not automatically export them or reconcile merged/deleted objects.
3. P3 default environment lacks SciPy in this inventory; code has all-pairs fallback. Treat as measured environment fact and profiling hypothesis, not a completed speed optimization.
4. Relative paths, imported legacy scripts, configs pointing artifacts and source lock external checkout prevent calling a source copy a runnable standalone project without U0.
5. Boundary, membership and functional GT are distinct. Normalized apt_0 spatial GT exists; its validation explicitly says functional labels/adjacency/object-room assignments unconfirmed.

## ConceptGraphs audit, pinned main

Commit: `93277a02bd89171f8121e84203121cf7af9ebb5d`.

- `conceptgraph/slam/mapping.py`: spatial score, cosine appearance score, weighted aggregate, argmax association.
- `conceptgraph/slam/utils.py`: cloud construction, random perturbation, voxel/DBSCAN processing, merge lists/points/features, object filter/merge.
- `conceptgraph/slam/cfslam_pipeline_batch.py`: threshold, contains penalty, periodic operations and saved objects.
- `conceptgraph/utils/model_utils.py`: crop/padding + image/text embeddings, heavy SAM import and CUDA-specific code.
- `conceptgraph/scripts/generate_gsa_results.py`: OpenCLIP setup (ViT-H-14 reference path).
- README/setup/LICENSE; ali-dev README and mapping excerpt inspected from Git without checkout modification.

The default choice is main as algorithm reference. README recommends ali-dev for streamlined real-time/demo workflows, but this project does not adopt that stack or claim its speed. Any later ali-dev reuse requires its own pinned commit/file ledger.

## What has NOT been proved

- No ConceptGraphs inference/import test or full environment installation.
- No fusion implementation, cross-class identity validation, room-boundary gain or performance improvement.
- No new full regression or Habitat end-to-end from the new workspace at initial seeding.
- No new legal clearance for datasets/FIST/models. Existing license constraints persist.
- No exhaustive search of model weights/GPU across the machine.

## Sources for implementers

- [ConceptGraphs pinned repository](https://github.com/concept-graphs/concept-graphs/tree/93277a02bd89171f8121e84203121cf7af9ebb5d)
- [Mapping](https://github.com/concept-graphs/concept-graphs/blob/93277a02bd89171f8121e84203121cf7af9ebb5d/conceptgraph/slam/mapping.py)
- [Fusion/filtering helpers](https://github.com/concept-graphs/concept-graphs/blob/93277a02bd89171f8121e84203121cf7af9ebb5d/conceptgraph/slam/utils.py)
- [Feature extraction](https://github.com/concept-graphs/concept-graphs/blob/93277a02bd89171f8121e84203121cf7af9ebb5d/conceptgraph/utils/model_utils.py)
- [OccuSG pinned repository](https://github.com/crcz25/OccuSG/tree/2bca2fa06af87fd9dd0542957039be8f580f0dca)

Local files take precedence for the pinned audit; web main can change.

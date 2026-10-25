# R2/R3 preparation — ConceptGraphs-Detect on Kaggle

Date: 2026-10-12. Scope: R2 CPU-side package/adapter and R3 execution preparation only. No Kaggle upload, model download, dependency installation, or remote run was performed.

## R3 preparation update — bundle v11

The v8 bundle and manifest remain unchanged. Bundle v9 added source commit pins and provenance fields; bundle v10 narrowed the active wrapper. Bundle v11 preserves v8–v10 and completes the selected direct dependency pins in the lock/notebook. The exact pins and remaining runtime/provenance limits are in [the R3 checklist](R3_KAGGLE_EXECUTION_CHECKLIST.vi.md), the [v11 changelog](R3_KAGGLE_BUNDLE_V11_CHANGELOG.vi.md), the [minimal core map](CONCEPTGRAPHS_MINIMAL_CORE_MAP.vi.md), and `locks/integration_v2/cg_kaggle_environment.lock.json`.

GradSLAM remains a pinned candidate sourced from a downstream recipe that states it follows the ConceptGraphs mapping/graph stack. chamferdist remains pinned for reference evaluation only and is excluded from the selected install path. The ConceptGraphs README itself specifies GradSLAM's `conceptfusion` branch but no commit and gives no chamferdist commit. Kaggle fetch/build/import and pipeline smoke are still unverified. RAM still has only a mutable upstream `main` reference; its download cell was removed from automatic staging until immutable provenance is resolved. No checkpoint was fetched and no checksum was invented.

Notebook cell 1 gives each execution a new UTC output root. Smoke and seven-frame pilot use independent clean copies; reruns require a new root, not reuse of an existing map. Read the checklist before upload. Private Kaggle visibility does not answer whether HM3D transfer to a cloud service is authorized; no upload has been made.

## Local artifacts

- Locked input: `artifacts/integration_v2/packages/q1_00808_cg_v6/` (7 frames, about 12 MB).
- Input package schema: `occusg-conceptgraphs-rgbd-package/v1`; content SHA-256: `1716e74aaf988861df1d215a88c068cf1a566ab5cd20ef34b676654e175cf108`.
- ZIP for a future authorized manual transfer: `artifacts/integration_v2/packages/q1_00808_cg_v6.zip` (7,246,772 bytes; SHA-256 `190867d8fe746dbc7b12b0e9f859e9ba3175eeb18ae897663685ba8ff13b2ad7`). It is local-only and has not been uploaded.
- Builder: `python3 scripts/build_conceptgraphs_input_package.py`.
- Validator: `python3 scripts/validate_conceptgraphs_package.py artifacts/integration_v2/packages/q1_00808_cg_v6`.
- Notebook: `notebooks/conceptgraphs_detect_q1_00808.ipynb`.
- Versioned runner and adapter checker: `scripts/conceptgraphs_kaggle_runner.py`, `scripts/check_conceptgraphs_dataset_adapter.py`.
- Local result receive validator: `python3 scripts/validate_conceptgraphs_export.py <downloaded-run>/export`.
- Transfer bundle builder (code + notebook only; no data/model): `python3 scripts/build_conceptgraphs_kaggle_bundle.py`.

The versioned bundle builder now defaults to `artifacts/integration_v2/kaggle_bundle/conceptgraphs_r2_bundle_v11.zip`; the v8, v9 and v10 bundles remain untouched. The bundle packages versioned scripts, lock, notebook and handoff docs only; it excludes input RGB-D packages, checkpoints, tokens and reference trees.

The package's actual pose records come from `adapters/habitat/frames.py`. To compensate for the pinned `Hm3dDataset` loader's `P @ pose @ P.T`, stored poses use `P @ T_W_Copt @ P.T`; the loader therefore returns the capture's absolute canonical `T_W_Copt`. Mapping batch uses `dataset.poses[idx]` for lifting; the loader's separate first-frame-relative `transformed_poses` is not that mapping input. Intrinsics preserve the adapter pixel-center convention `(W-1)/2,(H-1)/2`; no image resizing/cropping is configured. RGB and axial depth remain aligned at 640×480, depth unit is metres, invalid depth is NaN with an explicit valid mask.

Frame order is 0000–0006. The existing route has only four unique sensor poses: exact-pose revisits are `[0,6]`, `[1,5]`, `[2,4]`; they are not additional independent viewpoints. The manifest records every source RGB/depth/valid-mask SHA-256, source metadata SHA-256, exported-file hashes and effective camera matrices.

## Data and upload restriction

The package is derived from the already locked Q1 capture `q1_00808_20261011T160830Z`, frames 0000–0006. The original capture was not modified. Package excludes GT, review annotations, legacy P2/P3 predictions, scene assets and secrets. Capture metadata says HM3D academic/non-commercial use. Whether this permits transfer to a third-party Kaggle service has not been verified. Do not upload until that permission is verified and the user explicitly authorizes the transfer. The assistant did not upload anything.

## Recipe and resource notes

Locked model recipe: RAM tagging → GroundingDINO SwinT-OGC → SAM ViT-H → OpenCLIP ViT-H-14/laion2b_s32b_b79k → ConceptGraphs reference mapping. Detector thresholds: box/text 0.2, NMS 0.5; RAM input 384, reference normalization; accumulated RAM vocabulary and background class additions on. Mapping uses stride 1, overlap spatial similarity, `sim_sum`, threshold 1.2, DBSCAN epsilon 0.1 m; remaining settings are emitted explicitly by the runner and recorded in its manifest. No legacy P2/P3 or M6/K4 inputs are used.

The reference detector process initializes RAM, GroundingDINO, SAM and CLIP on the same selected device and retains them for the frame loop. Two T4 GPUs are separate devices and their VRAM cannot be pooled. Notebook probes each GPU and the runner requires at least 13 GiB free on GPU 0 before attempting the recipe. It does not split models, change precision or substitute checkpoints. This is a conservative preflight, not a guarantee against OOM.

R1 estimated checkpoint footprint is approximately 12.65 GB total: RAM 5.63 GB, GroundingDINO 694 MB, SAM ViT-H 2.39 GB, OpenCLIP ViT-H-14 3.94 GB. These are approximate download sizes, not measured peak VRAM. The runner now fails closed unless it finds the exact OpenCLIP checkpoint in the pinned model cache; it will not rely on implicit first-use network download. A separate notebook cell stages only that registered checkpoint when explicitly enabled, verifies OpenCLIP 2.20.0 and writes its path, byte count and SHA-256. This cell was not run.

The local builder used the already installed NumPy 1.26.4 and Pillow 10.2.0; nothing was installed. The workspace's active Python probe is 3.12.3 with no Torch/CUDA, so the pinned reference loader itself was not executed locally.

Dependency reference: Python 3.10; PyTorch 2.0.1 / torchvision 0.15.2 / torchaudio 2.0.2 with CUDA 11.8; PyTorch3D 0.7.4 `py310_cu118_pyt201`; faiss-cpu 1.7.4; OpenCLIP 2.20.0; Open3D 0.17.0. GradSLAM is pinned as a candidate and required by the selected dataset-loader import path; Kaggle fetch/build/import remains unverified. chamferdist is pinned for reference evaluation only and excluded from the selected pilot install path. The notebook install cells are opt-in and do not claim runtime compatibility.

The only declared reference adaptation is a run-local single-file copy of `generate_gsa_results.py` changing `matplotlib.use("TkAgg")` to `matplotlib.use("Agg")` for headless Kaggle; the full reference checkout is no longer copied. It is recorded in each run manifest; it changes rendering backend only. The original reference checkout is untouched.

Source/license notes: [GroundingDINO official repo/checkpoint docs](https://github.com/IDEA-Research/GroundingDINO), [Segment Anything official repo/checkpoint](https://github.com/facebookresearch/segment-anything), and [LAION OpenCLIP checkpoint card](https://huggingface.co/laion/CLIP-ViT-H-14-laion2B-s32B-b79K). GroundingDINO and SAM code repos publish Apache-2.0 licenses; the separate checkpoint terms still need checking. The OpenCLIP card lists MIT but also says any deployed use is out-of-scope. Treat this as offline research-only and do not deploy this model without a separate review. RAM checkpoint provenance follows the pinned Grounded-Segment-Anything README; its checkpoint terms remain unverified. Verify HM3D third-party transfer permission before sharing or publishing.

## Kaggle manual steps (not executed)

1. Confirm dataset license/third-party transfer permission; obtain explicit authorization to upload. The prepared local package is not uploaded.
2. Build the code bundle, then attach the bundle and package as private Kaggle inputs only after authorization.
3. Open `notebooks/conceptgraphs_detect_q1_00808.ipynb`, enable a T4 session and run probe cell A. Check Python/CUDA and free VRAM. Do not treat x2 as pooled.
4. In Kaggle, validate the exact GradSLAM candidate with the opt-in source setup, then run package/loader smoke. Do not install chamferdist for this selected recipe; it is evaluation-only.
5. Attach authorized checkpoints or download from the official sources after license review. Record SHA-256 and verify the three GSA files. If OpenCLIP is not already cached, review disk/license/transfer first, then explicitly enable the dedicated OpenCLIP staging cell; it records the file SHA-256. The runner refuses to begin mapping if the checkpoint is missing.
6. Run adapter/package preflight D, then the one-frame smoke E. Inspect stage logs and output checks before continuing.
7. Run all seven frames F from a fresh writable copy. Runner creates detector and mapping outputs, native ConceptGraphs pkl, per-frame masks/features, object clouds/bounds/features/membership, hashes, logs, exit codes and sampled RAM/VRAM runtime evidence.
8. Save notebook output / download the results archive before the Kaggle session expires. Local receive validation uses `scripts/validate_conceptgraphs_export.py`.

No Kaggle result exists yet. The notebook currently expects the package under `/kaggle/input/q1-00808-cg-package` and code bundle under `/kaggle/input/conceptgraphs-r2-bundle`.

## Output identity contract

Native ConceptGraphs artifacts are preserved. Exported objects are explicitly `final_map_only_no_persistent_lifecycle_claim`; content-derived snapshot keys are not advertised as persistent online IDs. Each observation record has a frame/detection ID, class/confidence, mask and image/text features with source detection hash. Each object exports XYZ cloud, min/max bounds, features, and membership when the reference fields permit it. The object export declares the capture-world transform; because runner feeds the decoded absolute capture-world poses, `T_capture_world_from_cg_world` is identity. It does not create OccuSG nodes or run room mapping.


Bundle v10 narrows selected-package setup and records the static call-path map. It has not been run in Kaggle. Local checks do not establish Grounded-SAM dependency closure, reference loader success, GPU execution, or output parity.


The v11 component map is in `docs/CONCEPTGRAPHS_MINIMAL_CORE_MAP.vi.md`; it records the selected function/dependency closure and the single Matplotlib compatibility patch. Grounded-SAM dependency closure remains a Kaggle preflight item because its pinned checkout is not available locally; the notebook avoids its broad `requirements.txt` and stops on import failure.

Current transfer artifact is bundle v11; v10 was retained as an earlier in-turn package snapshot rather than overwritten.

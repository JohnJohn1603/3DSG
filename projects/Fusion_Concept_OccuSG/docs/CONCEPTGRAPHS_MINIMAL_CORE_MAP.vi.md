# ConceptGraphs minimal core map (E)

Phạm vi này dùng đúng ConceptGraphs commit `93277a02bd89171f8121e84203121cf7af9ebb5d` và Grounded-SAM commit `a4d76a2b55e348943cba4cd57d7553c354296223` đã khóa. Đây là kiểm kê tĩnh call path, không phải xác nhận import/runtime trên Kaggle.

## Perception recipe được giữ

| Stage | Reference entry/function | Dùng trong pilot | Dependencies theo import/call path |
|---|---|---|---|
| Dataset/camera input | `conceptgraph/dataset/datasets_common.py::get_dataset` | Đọc RGB-D và pose từ adapter package Habitat; loader resize/canonicalize theo config đã khóa | `gradslam` (eager imports `datautils`, `relative_transformation`, `PointFusion`, `RGBDImages`), PyTorch, OpenCV, SciPy, NumPy, imageio, PyYAML, natsort |
| Tagging | `conceptgraph/scripts/generate_gsa_results.py::main`, `class_set=ram` | RAM Swin Large checkpoint từ pinned GSA checkout | PyTorch/torchvision, RAM module, PIL, OpenCV, NumPy |
| Detection | Cùng entrypoint, `detector=dino` | GroundingDINO SwinT-OGC, locked config/checkpoint, recipe thresholds | GroundingDINO source, PyTorch/torchvision, OpenCV, NumPy, supervision |
| Segmentation | `get_sam_predictor("sam", ...)`, `get_sam_segmentation_from_xyxy` | SAM ViT-H prompted by DINO boxes | `segment_anything`, PyTorch/torchvision, NumPy, PIL, matplotlib |
| Image features | `conceptgraph/utils/model_utils.py::compute_clip_features` | OpenCLIP ViT-H-14/laion2b_s32b_b79k | open-clip-torch, timm, PyTorch, PIL, torchvision |

The recipe is kept as one pinned upstream entrypoint. This avoids reimplementing preprocessing, model calls, or feature packing. No GT categories or semantic sensor are passed into its vocabulary.

## Object mapping core được giữ

The selected mapping entrypoint is `conceptgraph/slam/cfslam_pipeline_batch.py::main`. It consumes the cached per-frame detections and calls:

1. `conceptgraph/slam/utils.py::gobs_to_detection_list` and helpers (`filter_gobs`, point-cloud/bounds construction).
2. `conceptgraph/slam/mapping.py::compute_spatial_similarities`, `compute_visual_similarities`, `aggregate_similarities`, and `merge_detections_to_objects`.
3. `conceptgraph/slam/utils.py::merge_obj2_into_obj1`, `process_pcd`, `get_bounding_box`; `conceptgraph/slam/slam_classes.py::{MapObjectList,DetectionList}`.
4. Existing configured filtering/denoise/final postprocess calls, only where enabled by the effective recipe.

For the locked `spatial_sim_type=overlap`, `mapping.py` calls `compute_overlap_matrix_2set`; that path uses FAISS nearest-point support and `compute_3d_iou_accuracte_batch`, which imports `pytorch3d.ops.box3d_overlap`. Thus FAISS, PyTorch3D, Open3D, PyTorch, Hydra/OmegaConf and their direct imported helpers are in the selected core closure. Visual similarity is cosine similarity over the reference CLIP features. The reference assignment and merge policy remain unchanged.

## Scope exclusions and dependency status

- `chamferdist`: retained as a pinned reference-evaluation dependency only. Static search found it in evaluation utilities/scripts, not the selected detector or mapping call path; v10 notebook does not fetch or install it.
- Full ConceptGraphs environment and repository-wide Grounded-SAM `requirements.txt`: excluded from the active setup path because they include tools beyond the selected recipe. The notebook installs only the two selected Grounded-SAM source packages with `--no-deps` when explicitly enabled; missing transitive imports must stop the smoke and be resolved in the lock before use.
- LLM/caption/chat, relationship/scene-graph extras, navigation/viewer/training demos, alternate SLAM, and evaluator-only tooling are not invoked by this pilot.
- OccuSG non-ROS room backend remains local and outside the Kaggle perception/mapping runner. Adapter/export boundaries are unchanged.

The source pin for GradSLAM is a documented candidate based on the downstream ConceptGraphs-derived recipe, not proof that this runtime is compatible. The lock explicitly separates `core_required` from `reference_evaluation_only`; no dependency was installed or downloaded in this work.

Grounded-SAM's pinned top-level requirements include packages for unrelated serving/evaluation paths (for example diffusers, gradio and onnxruntime). The selected package setup metadata and `requirements.txt` are pinned by the GSA commit; versioned direct packages used by this recipe are recorded in the environment lock. Kaggle `pip check` and imports remain a required stop gate. See the [pinned GroundedDINO setup](https://github.com/IDEA-Research/Grounded-Segment-Anything/blob/a4d76a2b55e348943cba4cd57d7553c354296223/GroundingDINO/setup.py), [pinned GSA requirements](https://github.com/IDEA-Research/Grounded-Segment-Anything/blob/a4d76a2b55e348943cba4cd57d7553c354296223/requirements.txt), and [pinned SAM setup](https://github.com/IDEA-Research/Grounded-Segment-Anything/blob/a4d76a2b55e348943cba4cd57d7553c354296223/segment_anything/setup.py).

## Wrapper compatibility patch

`scripts/conceptgraphs_kaggle_runner.py` now creates only `output/compat/generate_gsa_results.py` and changes the single upstream `matplotlib.use("TkAgg")` call to `matplotlib.use("Agg")` for headless execution. Detector, mask, feature, and mapping algorithms are untouched. The pinned checkout is read-only and used directly via `PYTHONPATH`; the runner no longer copies the full ConceptGraphs repository into every run output. Each run manifest records the adapted file hash and patch description.

## Verification boundary

Local tests cover package/adapter contracts and the one-file adaptation helper. Static call-path inspection does not prove the dependency lock resolves, the GradSLAM loader imports, the CUDA kernels run, or output parity holds. Kaggle reference-loader smoke, GPU run, and reference output parity remain pending.

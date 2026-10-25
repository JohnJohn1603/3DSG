# ConceptGraphs → OccuSG rebuild: R0/R1 preflight

Ngày: 2026-10-12. Phạm vi: checkpoint R0 và read-only preflight R1; chưa triển khai R2/R3. Plan mới ở [CONCEPTGRAPHS_OCCUSG_REBUILD_PLAN.vi.md](CONCEPTGRAPHS_OCCUSG_REBUILD_PLAN.vi.md) thay đường phát triển chủ động U/K/Q. Các kết quả và file cũ được giữ làm lịch sử/baseline.

## Kết quả

- **R0 checkpoint: đạt.** Root không phải Git repository (`git rev-parse` báo `fatal: not a git repository`; `.git` là thư mục rỗng), nên không có commit làm rollback. Đã tạo archive source có hash và hướng dẫn phục hồi tại [checkpoint R0](../artifacts/checkpoints/r0_20261012/README.vi.md). Archive có 460 file, gồm code/config/docs/locks/entrypoints; loại dataset, artifacts lớn, virtualenv, weights và reference repositories. `sha256sum -c` và `tar --compare` đều pass. Không có task/run perception đang hoạt động khi kiểm tra.
- **R1 recipe/resource preflight: hoàn tất, execution bị chặn bởi tài nguyên.** ConceptGraphs reference đang ở commit `93277a02bd89171f8121e84203121cf7af9ebb5d`, sạch. Recipe được xác minh qua README và code cùng commit; chưa cài/tải/chạy model. Đề xuất effective recipe ở [proposal JSON](../locks/integration_v2/conceptgraphs_detect_preflight_proposal.json).
- **Cleanup: chưa áp dụng.** Không có file nào bị xóa hoặc di chuyển. Exact-path proposal và phụ thuộc/phục hồi ở [cleanup manifest](CLEANUP_MANIFEST_R0_R1.vi.md).

## Inventory: giữ lại theo vai trò

| Mục tiêu | File/path hiện có cần giữ | Boundary / phụ thuộc |
|---|---|---|
| Habitat capture | `adapters/habitat/{frames.py,raw.py}`, `scripts/capture_habitat_replica.py`, `scripts/preflight_habitat_run.py`, `scripts/preview_hm3d.py`, `configs/habitat/`, `configs/launcher/scenes.json`, `configs/launcher/local_environment.json` | Dùng Habitat Python 3.9.23 / Habitat-Sim 0.3.3 hiện có. Capture metadata có RGB, axial-depth mét, K/pose thực và transform canonical; route template cũ giữ nguyên. Không để scene GT/semantic render vào predictor. |
| OccuSG room backend không ROS | `src/standalone_sg/rooms/{geometry.py,anchoring.py,membership.py,relink.py}`, `src/standalone_sg/regions/{pipeline.py,guard.py,tracker.py,dude_client.py}`, `src/standalone_sg/mapping/{native.py,native_pipeline.py,projection.py,grid_artifacts.py}`, `src/standalone_sg/geometry/`, `src/standalone_sg/contracts/`, `src/standalone_sg/serialization/json_graph.py`, `native/occupancy/` | Các module room/region/occupancy là phần tái sử dụng candidate. Không import nguyên object association trong `src/standalone_sg/objects/` hoặc legacy builder làm backend object của kiến trúc mới. D.U.D.E và occupancy có binary/dependency input-only được khai báo trong `locks/external_resources.json`; runtime path/hash cần resolver lại trước khi R4. |
| OccuSG reference | `ref_OccuSG/OccuSG/`, lock tại `locks/reference_repositories.json` | Commit `2bca2fa06af87fd9dd0542957039be8f580f0dca`, clean. Repo có ROS packages; `src/scene_graph_core/scene_graph_core/` là Python graph core không ROS đáng tham khảo. Không sửa repo; room backend candidate đang nằm trong `src/standalone_sg/` và `native/occupancy/`. |
| ConceptGraphs reference | `ref_ConceptGraph/concept-graphs/`, lock tại `locks/reference_repositories.json` | Commit `93277a02bd89171f8121e84203121cf7af9ebb5d`, clean. Giữ nguyên, read-only. Không clone lại. |
| Evaluator/annotation/launcher | `evaluation/`, `scripts/scene_launcher.py`, `scripts/evaluate_hm3d_object_gt.py`, `scripts/evaluate_apt0_ground_truth.py`, `scripts/evaluate_apt0_partition_comparison.py`, `scripts/score_q1_00808_reviewed_subset.py`, `scripts/compare_association_profiles.py`, `run_scene.sh`, `Cheatsheat.md`, `artifacts/reviews/`, `artifacts/audits/`, `artifacts/gt_cache/` | GT, user annotations và evaluator ledger chỉ hậu kiểm. Giữ toàn bộ output cũ; annotation mask/pipeline cũ không tự áp dụng sang SAM masks. Launcher cũ giữ để rollback/đối chiếu cho tới R6. |
| Dataset/models/env/native resources | `locks/{baseline_seed_manifest.json,external_resources.json,reuse_resources.json,sources.json,reference_repositories.json}`, `artifacts/runs/`, `artifacts/models/`, `artifacts/cache/`, `.venv-appearance/`; baseline read-only `/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG` | Giữ nguyên. Không copy/chạy reference weights từ U4-OV như thể cùng model. Datasets và binaries baseline là external input-only; output mới tiếp tục nằm trong workspace. |

`src/standalone_sg/pipeline/builder.py` hiện kết hợp nhiều stage cũ, bao gồm object path; không được dùng nguyên builder này để lén đưa M6/legacy identity vào ConceptGraphs objects. R2/R4 cần adapter/API boundary riêng và tests trước khi tích hợp.

Kích thước chính tại thời điểm preflight: `src/` 1.5 MB, `scripts/` 4.2 MB, `tests/` 2.2 MB, `docs/` 636 KB, `locks/` 196 KB, ConceptGraphs reference 6.3 MB, OccuSG reference 29 MB; `artifacts/runs/` 749 MB, `artifacts/audits/` 357 MB, `artifacts/models/` 578 MB, `artifacts/cache/` 171 MB, `artifacts/reviews/` 45 MB, `artifacts/gt_cache/` 65 MB, `.venv-appearance/` 997 MB. Đây là inventory, không phải đề xuất xóa.

Hash kiểm tra read-only cho tài nguyên room backend tại baseline: `standalone_sg_octomap_fixture_runner` SHA-256 `bd171a8ac9974eec18957af97e6fc369729358ca62985390eb994c26f2040b29`, `occusg_dude_worker` `7868c892f3584faa17712e1ede0660006d97531b05be5c258fe303015f1ef959`, source oracle `occusg_dude_source_reference` `49a9b467a8ba3c54b406098aacb4dc549a4a595cee554e0d8097ea43134ef5b6`; đều khớp `locks/external_resources.json`. `ldd` cho thấy worker dùng OpenCV 4.06 và GMP từ baseline `build/deps/m4-prefix`, cùng system libraries; không chạy binary. Mapconversion external source khớp commit lock `14b8cb9e6534ba21e84f624c94d20960f6613f3c`. Không copy/build/sửa các tài nguyên này.

## R1: recipe ConceptGraphs-Detect đã resolve

Tại pinned ConceptGraphs commit, README dùng `generate_gsa_results.py` cho detections/features rồi `cfslam_pipeline_batch.py` cho mapping. Code xác nhận **RAM tagger → GroundingDINO SwinT-OGC → SAM ViT-H → OpenCLIP ViT-H-14 / `laion2b_s32b_b79k`**, sau đó lifting, spatial + visual similarity, object association/fusion và post-processing. Đây là recipe nặng đã chỉ định trong plan; **không thay bằng YOLO11n hay ViT-B-32**.

Nguồn đã đối chiếu: [ConceptGraphs README tại commit pin](https://github.com/concept-graphs/concept-graphs/blob/93277a02bd89171f8121e84203121cf7af9ebb5d/README.md), [detector/features entrypoint](https://github.com/concept-graphs/concept-graphs/blob/93277a02bd89171f8121e84203121cf7af9ebb5d/conceptgraph/scripts/generate_gsa_results.py), [mapping entrypoint](https://github.com/concept-graphs/concept-graphs/blob/93277a02bd89171f8121e84203121cf7af9ebb5d/conceptgraph/slam/cfslam_pipeline_batch.py), và [GSA README ở commit ConceptGraphs nêu](https://github.com/IDEA-Research/Grounded-Segment-Anything/tree/a4d76a2b55e348943cba4cd57d7553c354296223).

Effective detector proposal cho capture Q1 có sẵn: `class_set=ram`, `detector=dino`, `sam_variant=sam`, `device=cuda`, `box_threshold=0.2`, `text_threshold=0.2`, `nms_threshold=0.5` (README overrides; detector code defaults kiểm chứng), RAM input 384×384 với ImageNet mean/std, RAM tags thành phrase list, thêm `other item` và background tags khi `add_bg_classes`; `accumu_classes` giữ category tags quan sát được từ ảnh theo code. Không dùng `class_set=scene`: code nhánh đó đọc `obj_meta.json`, vốn sẽ làm vocabulary phụ thuộc scene metadata/GT.

Mapping override từ README: `stride=1`, `spatial_sim_type=overlap`, `mask_conf_threshold=0.25`, `match_method=sim_sum`, `sim_threshold=1.2`, `dbscan_eps=0.1`, `gsa_variant=ram_withbg_allclasses`, `skip_bg=False`, `max_bbox_area_ratio=0.5`. Các giá trị còn lại giữ defaults của `conceptgraph/configs/slam_pipeline/base.yaml` sau khi thay toàn bộ đường dẫn dataset/authors bằng run-local paths: `mask_area_threshold=25`, `min_points_threshold=16`, `downsample_voxel_size=0.025`, DBSCAN noise removal và `min_points=10`, `obj_min_detections=3`, `denoise_interval=20`, `filter_interval=-1`, `merge_interval=-1`; file ghi final/post output. Base yaml có đường dẫn tuyệt đối máy tác giả và **không dùng nguyên trạng**.

Feature extraction trong code dùng crop image + CLIP text feature của category; object mapping gọi spatial/visual similarities, aggregate score, association và object merge/filter. README recipe có merge/post steps; R3 phải log effective config thay vì coi `base.yaml` là recipe đầy đủ. Không dùng GT category để sinh RAM tags/vocabulary.

Đã kiểm tra thêm:

- `class_set=scene` lấy category từ `obj_meta.json`; loại khỏi main recipe. Nhánh RAM lấy tagger output; không phát hiện một đường fallback sang category GT.
- DINO detections có `class_id=-1` bị loại. `utils.py` có random point jitter `N(0, 0.004)` và random colors; không thấy seed setup trong entrypoints. `accumu_classes` dùng set → list nên thứ tự class serialization có thể đổi. R3 phải ghi nondeterminism/fix seed policy ở instrumentation, không được giả định bitwise repeatability.
- CLIP helper gọi CUDA trực tiếp trong một đường (`.to("cuda")`); code không phải CPU fallback đầy đủ. Không có GPU khả dụng ở máy này.
- Exact commands/option defaults được ghi trong proposal JSON và repo source; chưa chạy launcher/reference command.

## Phần cứng, dependencies và weights

Đo tại máy hiện tại: RAM tổng 15.9 GB, available khoảng 6.4 GB, swap 4 GB; disk còn 314 GB. `lspci` chỉ thấy Intel UHD Raptor Lake-P, `nvidia-smi` và `nvcc` không có, nên không có NVIDIA VRAM/CUDA device quan sát được. Không thể đo peak VRAM hay thời gian reference inference.

Đã tìm trong workspace, cache model đã biết và baseline model folder:

| Tài nguyên | Trạng thái local | Nhu cầu proposal |
|---|---|---|
| RAM Swin-Large 14M `ram_swin_large_14m.pth` | Không thấy | Official model repo báo 5.63 GB |
| GroundingDINO `groundingdino_swint_ogc.pth` | Không thấy | khoảng 694 MB; official GroundingDINO/Grounded-SAM URL |
| SAM `sam_vit_h_4b8939.pth` | Không thấy | khoảng 2.39 GB |
| OpenCLIP ViT-H-14 `laion2b_s32b_b79k` | Không thấy | khoảng 3.94 GB |
| Tổng bốn checkpoint | Chưa có | khoảng **12.65 GB (~12.7 GB)**, chưa gồm repo/dependency/env/cache |
| GSA code | Không có clone trong workspace | README pin codebase commit `a4d76a2b55e348943cba4cd57d7553c354296223`; cần clone đúng commit và lock hash |
| GradSLAM/chamferdist | Không có local package/reference clone đã lock | README yêu cầu GradSLAM nhánh `conceptfusion` và chamferdist; cần pin commit trước cài |
| Existing `.venv-appearance` | Có, 997 MB; Python 3.12.3, Torch 2.6.0 CPU, torchvision 0.21 CPU, OpenCLIP 3.2.0 | Chỉ chứa U2 ViT-B-32 checkpoint ~605 MB; **không tương thích/reference substitute** cho CUDA recipe ViT-H-14 |
| Habitat env | Có Python 3.9.23 / Habitat-Sim 0.3.3 | Không cài torch/open_clip/ConceptGraphs deps theo package metadata; giữ nguyên |
| System Python / baseline perception env | Python 3.12.3; package metadata không có Torch/ConceptGraphs stack | Không dùng/cài thêm trong lượt này |

Reference README nêu env Python 3.10, PyTorch 2.0.1 / torchvision 0.15.2 / torchaudio 2.0.2 với CUDA 11.8, PyTorch3D 0.7.4 build tương ứng, Faiss CPU 1.7.4, OpenCLIP/Hydra và package GSA; environment.yml rất lớn và chứa Anaconda distribution. Tạo một env riêng là bắt buộc; không dùng `.venv-appearance`, Habitat hoặc baseline env. Với việc ba backbone detector/SAM/CLIP cùng được đưa lên device, 15.9 GB RAM và không có GPU không đủ để đề xuất reference inference đáng tin. **Mức tài nguyên mục tiêu để xin ở bước sau:** NVIDIA GPU ≥24 GB VRAM, host RAM ≥32 GB; đây là headroom proposal, không phải đã benchmarked minimum. Disk hiện có dư cho weights và env.

Chi phí tải tối thiểu của weights khoảng 12.7 GB. Cài đặt chính xác và kích thước environment chưa khóa/đo; dependency recipe có PyTorch3D và GSA CUDA extension build nên dự toán environment thêm vài đến hàng chục GB là bất định. Không phát sinh chi phí remote trong preflight này. Không tải/cài bất cứ thứ gì.

Size references để dự toán (không phải SHA pin): [RAM checkpoint repo](https://huggingface.co/xinyu1205/recognize_anything_model/tree/main) hiển thị file 5.63 GB; [OpenCLIP ViT-H-14 checkpoint](https://huggingface.co/laion/CLIP-ViT-H-14-laion2B-s32B-b79K/tree/main) hiển thị khoảng 3.94 GB; [GroundingDINO weight listing](https://huggingface.co/1038lab/GroundingDINO/blob/main/groundingdino_swint_ogc.pth) khoảng 694 MB; SAM ViT-H khoảng 2.39 GB theo listing checkpoint. Khi download phải xác minh upstream URL, revision và SHA-256; các size listing không thay thế hash lock.

## R2/R3 proposal — chưa được thực thi

**Input hiện có:** `artifacts/runs/q1_00808_20261011T160830Z/capture/`, bảy frame raw RGB-D 640×480, depth axial mét, K và pose per-frame trong metadata. Frame list: `hm3d-00808-k4-ag-v1-0000` tới `...-0006` theo thứ tự; capture manifest SHA-256 `7dcd9c2d466e8d159380a2544b69abc264eaffccf889b08aeee25957db718122`, pilot lock SHA-256 `fcd0d2bbbf16d6a1b1a33d1429e649e612b04a9432fa862526da2c146ef0cc78`. Đối chiếu pose metadata cho thấy chỉ **4 pose duy nhất** (frames 0–3); frames 4/5/6 quay lại đúng pose lần lượt của 2/1/0. Frame 3 có yaw khác; 4–6 là repeats, không tính thêm independent views. Proposal vẫn dùng cả 7 để kiểm tra repeated-frame handling nhưng chất lượng multiview chỉ có tối đa 4 pose bins. Scene 00808 đã development, không unseen. User-reviewed masks từ P2 cũ không map tự động sang GroundingDINO/SAM outputs.

**R2 adapter:** thêm dataset adapter mỏng từ capture manifest sang API `conceptgraph.dataset.datasets_common.get_dataset` contract: RGB uint8, axial depth mét, K pixel-center đã lưu, `T_W_C`/camera axes và timestamp/frame ID. Kiểm tra RGB-depth alignment, depth invalid policy, image orientation, Habitat optical→canonical→CG transform, relative-pose behavior của loader, project/backproject round-trip và first-frame normalization. Không đưa P2/P3 masks/GT vào object pipeline.

**R3 frames:** khóa đúng 7 capture frame IDs 0000–0006, 640×480, stride 1; không native replay. Chạy recipe detector+mapping hai phần reference trên cùng frame list, xuất per-frame RAM tags, DINO boxes, SAM masks, CLIP image/text features, lifted clouds, native object snapshots/final post map, config/input/model/code hashes, stage runtimes và RAM/VRAM. Test wrapper against unwrapped reference only khi cùng inputs/seed/config khả thi; báo non-determinism, không đòi bitwise parity.

**Ước lượng cho proposal kế tiếp (bất định, cần máy GPU và phê duyệt tài nguyên trước):** 3–5 giờ adapter + calibration tests; 2–4 giờ environment/build và smoke tests chưa gồm thời gian tải theo mạng; 20–60 phút GPU runtime cho 7 frame + export/checks, có thể tăng mạnh do checkpoint RAM/SAM/H14 và build. Tổng setup/tests/run ước lượng **6–10 giờ** trên máy có GPU đáp ứng. Nếu tiếp tục trên máy hiện tại, R3 bị block; không thử CPU thay recipe.

**Hoàn tất khi:** adapter pass transform/depth tests; config và hashes khóa trước run; cả 7 frame đi qua RAM→GroundingDINO→SAM→CLIP→lifting/mapping không gọi legacy P2/P3/K4/U4; outputs/reference provenance đầy đủ; resource/runtime logged; graph object outputs mở/xem được. Đây là integration/development pilot, chưa là quality promotion.

## Blockers và bước tiếp theo

1. Blocker cụ thể: chưa có NVIDIA CUDA GPU/VRAM quan sát được; host RAM hiện available 6.4 GB; thiếu cả bốn checkpoints và GSA repo/dependencies. Reference recipe không thể chạy tại máy hiện tại mà không đổi thành CPU/adapted recipe.
2. Trước R2/R3 cần duyệt: có sẵn máy GPU local ≥24 GB VRAM và ≥32 GB RAM hay không; nếu không, chờ tài nguyên tại chỗ hoặc trình variant adapted có tên khác. User đã cấm remote compute nên không đề xuất chạy thuê ngoài.
3. Sau khi xác định tài nguyên: lập lock dependency/commit và download manifest chính xác, xin duyệt riêng việc tải khoảng 12.7 GB checkpoints và cài isolated Python 3.10 CUDA env. Sau đó mới làm R2/R3 theo bảy frame trên.
4. Không có gate cũ U0–U6/K0–K6 nào được đánh dấu lại hoặc xóa; lịch sử giữ nguyên.

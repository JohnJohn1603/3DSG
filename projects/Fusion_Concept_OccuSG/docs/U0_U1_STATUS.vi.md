# Trạng thái U0 + U1

Ngày hoàn tất: 07-10-2026. Phạm vi: bind tài nguyên, source replay, observation lineage và audit frame 224–230. Chưa có fusion, thay threshold hay room refinement.

## U0 — Bind và kiểm chứng môi trường

- 283 file source seed ban đầu khớp manifest trước khi sửa: 0 mismatch. OccuSG và ConceptGraphs vẫn ở commit đã pin, worktree sạch; license notes giữ nguyên.
- License metadata của hai reference checkout đều là MIT; đây không thay đổi các giới hạn license riêng của HM3D/data/model trong tài liệu nguồn.
- `configs/launcher/local_environment.json` đã chuyển sang schema 2 và bind rõ perception/Habitat interpreters, read-only baseline root, HM3D data, model, native binaries/prefix và MapConversion.
- `locks/external_resources.json` khóa SHA-256 của model, native binaries, HM3D scene/config/navmesh, baseline lock, smoke/dense captures và caches đã chọn. Launcher kiểm hash trước `check`/`run`; nguồn ngoài chỉ được dùng làm input. `MPLCONFIGDIR`, run output và audit output đặt dưới workspace mới.
- Resolver của launcher, preflight và route validator ánh xạ input HM3D/model sang resource roots; các output `artifacts/runs` vẫn ở workspace mới. Không symlink, không copy dữ liệu/model/env, không ghi vào baseline.
- `./run_scene.sh help`, `maps`, `routes hm3d_00802` và `check hm3d_00802 --route smoke` chạy được. Smoke route PASS: 10/10 pose navigable và các đoạn liên tiếp kết nối.
- Lần kiểm route đầu tiên FAIL vì validator ghép scene config/navmesh dưới workspace mới. Đã sửa đường ánh xạ tới input root và kiểm tra lại PASS.

## U1 — Replay và lineage

- Dùng capture smoke đã có (`hm3d_00802_20261005T155214.379554Z_657f1e`), cache P3 hiện có, profile `configs/reference_code_mp3d.yaml`, binary/prefix hiện có. Không chạy capture, YOLO hay dense M1–M10 replay lại. Output ở `artifacts/runs/u0_source_replay_smoke_final/replay/`.
- Replay 10 frame trong workspace mới hoàn tất, `fresh_replay_equal=true`, snapshot reload/delta replay đều bằng nhau. Snapshot, delta log và trajectory canonical trùng byte với baseline run cùng capture: SHA-256 snapshot `855f1445d65623327057b193ba9d814b1418e687c16939ce0c7216046ced4a8c`, delta `9f8dd19efe3db7486a264029b650d636e7cd4a7dc609fd701c8dde993169de24`, trajectory CSV `7753ae822413d6a430605092b8298cc2b1bc7d2110a9883837d5d112bc632a97`. Trace grid bật ở pass đầu và tắt ở fresh-check; graph/delta vẫn bằng nhau.
- Replay mới xuất thêm `object_association_lineage.jsonl`: mỗi observation có frame/time/detection/provider, class đầu vào, object/candidate ID được chọn, quyết định, khoảng cách và normalized class. `ObjectAssociationManager` và builder decision không đổi.
- P3 cache cũ không lưu point/pixel indices. Đã tái tạo sidecar cho frames 224–230 từ RGB-D + mask cache đã khóa, dùng cùng backprojection/largest-cluster policy; 47 observations accepted đều khớp center cache trong sai số 2e-6 m. NPZ ghi selected world points và row/column pixel indices cùng hashes nguồn.
- Perception env Python 3.12 thiếu SciPy; P3 fallback của nó chạy all-pairs O(n²). Một lần thử trích lineage bằng env này đã được dừng. Habitat env Python 3.9 có SciPy nhưng source dùng `dataclass(slots=True)` nên Python 3.9 không tương thích. Trích sidecar chọn frame bằng system Python 3.12 + SciPy 1.11.4; không cài package và không thay thuật toán/clustering.

## Audit dining_table → bed, frames 224–230

Replay M6 source theo đúng thứ tự cache từ frame 0000 đến 0230 (231 frame, 583 associations) để giữ trạng thái trước đó. Trace đầy đủ ở `artifacts/audits/dining_table_bed_224_230/object_association_lineage_prefix_231.jsonl`; point-set refs ở `artifacts/audits/dining_table_bed_224_230/pointsets/`.

| Frame | Observation | M6 outcome | Khoảng cách | Ghi chú |
|---:|---|---|---:|---|
| 224 | dining_table det-0002 | match `object-000021` | 0.3806 m | P3 accepted |
| 225 | dining_table det-0003 | match `object-000021` | 0.3977 m | P3 accepted |
| 226 | dining_table det-0004 | match `object-000021` | 0.4629 m | P3 accepted |
| 227 | dining_table det-0004 | match `object-000021` | 0.4503 m | P3 accepted |
| 228 | dining_table det-0004 | match `object-000025` | 0.4390 m | P3 accepted |
| 229 | dining_table det-0003 | match `object-000025` | 0.4226 m | Cùng frame còn dining_table det-0004 → `object-000016` |
| 230 | bed det-0004 | **create `object-000069`** | 0.0000 m | Không đổi class hay nhập vào table track |
| 230 | dining_table det-0005 | match `object-000016` | 0.6117 m | Observation riêng trong cùng frame |

`bed` ở frame 230 được đưa vào predictor với class thô `bed`; M6 tạo object mới. Trong evaluator annotation độc lập, cùng detection ID được ghép với `gt_category=table`, `gt_semantic_id=417`, mask IoU 0.8394; class mapping của detection được ghi `unmapped`. Đây là nhãn evaluator, không được đưa vào replay hoặc predictor. Vì vậy đây là lỗi class của detector được evaluator ghi nhận, chưa phải class correction/identity merge của hệ thống.

## Kiểm thử và giới hạn

- **65 passed**: M6 association, builder M9, launcher và perception runtime config. Đây không phải full suite.
- Không chạy ReplicaCAD smoke, full suite, CMake rebuild hay full dense replay. Dense baseline replay mất khoảng 55 phút; audit case dùng M6 prefix từ cache và đối chiếu graph frame 230 có sẵn.
- U2 cần một extractor appearance và môi trường tách biệt. Hiện các env được kiểm kê chưa có Torch/OpenCLIP; không tải checkpoint hoặc cài package trong milestone này. Trước U2 cần chốt extractor/checkpoint/hash/preprocessing, runtime compatible với Python 3.12, CPU/GPU khả dụng và chi phí/license; không dùng label evaluator làm feature/predictor input.

## Lệnh đã kiểm chứng

```bash
./run_scene.sh help
./run_scene.sh maps
./run_scene.sh routes hm3d_00802
./run_scene.sh check hm3d_00802 --route smoke
PYTHONDONTWRITEBYTECODE=1 MPLCONFIGDIR=artifacts/cache/matplotlib PYTHONPATH=.:src python3 -B -m pytest -q -p no:cacheprovider tests/test_m6_object_association.py tests/test_m9_builder.py tests/test_scene_launcher.py tests/test_perception_runtime_config.py
```

## Files changed outside generated artifacts

- `configs/launcher/local_environment.json`
- `scripts/scene_launcher.py`, `scripts/preflight_habitat_run.py`, `scripts/validate_habitat_route.py`
- `src/standalone_sg/pipeline/builder.py`, `scripts/run_habitat_native_replay.py`
- `scripts/export_selected_pointset_lineage.py`, `scripts/audit_source_association_prefix.py`
- `locks/external_resources.json`, this status note and README status link

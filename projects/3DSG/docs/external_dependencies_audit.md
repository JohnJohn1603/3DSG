# Kiểm kê phụ thuộc ngoài thư mục 3DSG

Ngày rà soát: 25/10/2026  
Phạm vi: mã nguồn, cấu hình, worker, script replay/bảo trì và run metadata đang có trong checkout 3DSG. Không khởi chạy live worker hay Habitat episode. Một probe chỉ đọc đã xác nhận các checkout, interpreter, model/checkpoint, profile/contract, worker và source artifact mặc định tồn tại tại thời điểm rà soát; probe đó không xác minh rằng có thể tái lập toàn pipeline hoặc các revision đang đồng bộ.

## Kết luận nhanh

**Có.** 3DSG có phụ thuộc ngoài repo ở ba lớp khác nhau:

1. **Môi trường chạy Habitat và dữ liệu scene HM3D** — cần cho navigation/demo tương ứng.
2. **Hai checkout sibling `Fusion_Concept_OccuSG` và `Rebuild_3DScene_OccuSG`** — P2 cần một phần của chúng; plugin native ConceptGraphs + OccuSG cần phần lớn cả hai.
3. **Script replay/bảo trì** — một số script đọc artifact cũ, một số script có thể sửa source trong hai repo sibling khi được gọi rõ ràng.

Điều này **không có nghĩa mọi lần chạy 3DSG đều cần cả ba repo**. Chạy trực tiếp navigation-only cần Habitat + scene assets; bật P2 thêm một tập con từ hai sibling; chọn native graph plugin mới dùng đầy đủ ConceptGraphs/OccuSG và các worker native.

## Sơ đồ luồng và ranh giới repo

```text
┌────────────────────────────── 3DSG ───────────────────────────────┐
│ run_frontier_habitat.py / desktop.py                              │
│   ├─ Habitat-Sim (import trong runner env)                        │
│   ├─ P2WorkerClient ── JSONL + RGB file path ──► p2_onnx_worker.py │
│   │                                      └────► Fusion P2 API      │
│   │                                      └────► Rebuild ONNX/env   │
│   └─ native_graph_plugin (chỉ khi chọn rõ)                         │
│       ├─ P3/OpenCLIP worker ────────────────► Fusion APIs/checkpt  │
│       ├─ ConceptGraphs worker ──────────────► Fusion checkout      │
│       └─ OccuSG shim/worker ───────────────► Rebuild env/source   │
│                                              └► M2 + DuDe binaries │
│                                                                    │
│ Capture, packet, mask, graph/run output thường nằm dưới 3DSG/runs │
└────────────────────────────────────────────────────────────────────┘
        ▲                                     ▲
        │ scene GLB/navmesh/dataset            │ models/configs/source
        │                                     │
┌───────┴────────────────┐          ┌─────────┴────────────────────┐
│ Rebuild_3DScene_OccuSG  │          │ Fusion_Concept_OccuSG        │
└────────────────────────┘          └──────────────────────────────┘
```

Các worker trao đổi frame identity và metadata qua JSON-lines; ảnh, depth, mask và packet lớn thường được ghi thành file trong run output rồi truyền bằng đường dẫn + hash. Vì vậy khi gộp code cần giữ cả **mã adapter/protocol** lẫn **cách phân phối model, scene data và runtime**, không chỉ chép Python source.

## Thư mục ngoài 3DSG được tham chiếu

| Đường dẫn | Vai trò | Khi nào cần |
|---|---|---|
| `/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG` | Checkout ConceptGraphs và một số API perception/P2 dùng chung; worker ConceptGraphs, P3/OpenCLIP, profile, vocabulary, checkpoint, feature contract, route/effective config và source-run bootstrap | P2 khi bật; P3/OpenCLIP và ConceptGraphs khi dùng native graph; replay/preflight |
| `/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG` | Checkout OccuSG; Python env perception, source `standalone_sg`, model ONNX, Habitat scene assets, M2/M3 và DuDe binaries | Scene navigation theo config hiện tại; P2 runtime/model; OccuSG native; replay/preflight |
| `/home/kiettran05/miniconda3/envs/habitat` | Python environment chạy Desktop runner, Habitat-Sim, Magnum/quaternion và các package simulator | Mặc định của Desktop; có thể thay bằng `--runner-python` |
| `/tmp/occusg-m2-binary-backup-20261024` và các thư mục temp khác | Backup, fixture hoặc output chẩn đoán do các script bảo trì/replay dùng | Chỉ khi chạy đúng script liên quan; không phải dependency của live runner bình thường |
| `/proc` | Resource monitor đọc PID tree, CPU và RSS của process | Runner hiện truy cập trong quá trình theo dõi tài nguyên; đây là giao diện hệ điều hành, không phải repo dự án khác |

Đường dẫn hai repo sibling được suy ra mặc định từ `ROOT.parent` trong runner/plugin. Các giá trị gốc thường là đường dẫn máy cá nhân ở trên; native plugin có thể đổi root bằng `3DSG_CONCEPTGRAPHS_ROOT` và `3DSG_OCCUSG_ROOT`.

## Phân loại theo chế độ chạy

### Navigation-only

- Runner import `exploration.*` trong repo và import `habitat_sim`, `magnum`, `quaternion` trong process Python được chọn. Habitat **không** được khởi chạy thành subprocess riêng.
- Cấu hình mặc định `configs/exploration/hm3d_00802_cpu.json` và các config HM3D khác tham chiếu GLB, navmesh, scene-dataset JSON trong `Rebuild_3DScene_OccuSG/artifacts/habitat_data/...`.
- Config cũng tham chiếu `source_effective_config` trong `Fusion_Concept_OccuSG/configs/habitat/...`; loader xác minh các path/hash này. Vì vậy ngay cả navigation-only hiện tại cũng gắn với dữ liệu ngoài hai repo nếu dùng các config đóng gói sẵn.
- Không cần ConceptGraphs/OccuSG worker và không cần model P2 nếu chạy `--navigation-only` hoặc gọi runner trực tiếp mà không bật P2.

Điểm vào: [run_frontier_habitat.py](../scripts/run_frontier_habitat.py), [habitat_factory.py](../exploration/habitat_factory.py), [hm3d_00802_cpu.json](../configs/exploration/hm3d_00802_cpu.json).

### P2 segmentation (tùy chọn)

P2 được bật bởi `--enable-p2`; Desktop hiện bật checkbox graph/P2 mặc định, còn gọi trực tiếp runner thì P2/graph mặc định tắt.

- Process dùng Python: `Rebuild_3DScene_OccuSG/.venv-perception/bin/python`.
- Model: `Rebuild_3DScene_OccuSG/artifacts/models/ultralytics/yolo11n-seg.onnx`.
- Profile và class names: `Fusion_Concept_OccuSG/configs/perception/yolo11n_seg_replica_cad_dev.json` và `.../coco.names`.
- Script worker là **trong 3DSG** (`scripts/p2_onnx_worker.py`), nhưng worker thêm Fusion root vào `sys.path` và import `scripts.run_replica_cad_instance_segmentation` từ Fusion.
- Worker nhận frame ID/index/timestamp cùng đường dẫn RGB đã lưu; kết quả mask được ghi dưới run directory.

Điểm vào/consumer: [run_frontier_habitat.py](../scripts/run_frontier_habitat.py), [p2_worker_client.py](../exploration/p2_worker_client.py), [p2_onnx_worker.py](../scripts/p2_onnx_worker.py).

### Native ConceptGraphs + OccuSG (tùy chọn, phải chọn plugin)

`--enable-graph` tự nó **không đồng nghĩa** native backends. Plugin mặc định là `exploration.demo_graph_plugin:create_plugin` (nội bộ 3DSG). Muốn dùng hai backend thật, phải truyền `--graph-plugin exploration.native_graph_plugin:create_plugin`.

Khi native plugin được chọn:

- **ConceptGraphs:** Python `.venv-cg-cpu` và worker script `Fusion_Concept_OccuSG/scripts/run_t2_d_cached_packet_worker.py` chạy từ checkout Fusion.
- **P3 geometry + OpenCLIP:** Python `.venv-appearance` trong Fusion; adapter/session code lấy từ Fusion (`adapters`, `integration_v2`, `ref_ConceptGraph`), profile/feature contract/checkpoint nằm ở Fusion. Worker điều phối `scripts/native_perception_worker.py` nằm trong 3DSG.
- **OccuSG:** Python `.venv-perception` trong Rebuild; source `standalone_sg` nằm ở `Rebuild_3DScene_OccuSG/src`. Shim `scripts/native_occusg_worker.py` ở 3DSG nạp worker ConceptGraphs từ Fusion rồi thay mapping bằng adapter OccuSG.
- **Native binaries:** source run bootstrap mặc định ở `Fusion_Concept_OccuSG/artifacts/conceptgraphs_hybrid_runs/c2_20261017T063106Z_99bc38` cung cấp manifest trỏ đến M2/M3 OctoMap worker và DuDe worker trong `Rebuild_3DScene_OccuSG/build/...`. Hai binary được gọi gián tiếp bởi package OccuSG.
- **Checkpoint/contract/config chính:** OpenCLIP `.../artifacts/models/openclip/vit-b-32-openai-a6f597a30f7b82c51704746581f9a4e41421e878/open_clip_model.safetensors`; P3/P2 profile `configs/perception/yolo11n_seg_replica_cad_dev.json`; feature contract `artifacts/t2_b2_perception_smoke_20261018_v3/preflight.json`; route/config `configs/launcher/local_environment.json` trong Fusion.
- Capture, masks, P3 features, packet, mapper state và output phiên chạy được ghi dưới run output của 3DSG; các checkout sibling là code/runtime/input, không phải nơi chứa output 3DSG thông thường.
- Theo metadata của plugin hiện tại, OccuSG nhận auxiliary depth đã calibration riêng; ConceptGraphs vẫn chạy primary RGB-D. Snapshot hiển thị lấy từ nhánh OccuSG; object branch ConceptGraphs vẫn giữ provenance và chưa được cross-backend join.

Điểm vào/consumer: [native_graph_plugin.py](../exploration/native_graph_plugin.py), [native_perception_client.py](../exploration/native_perception_client.py), [native_perception_worker.py](../scripts/native_perception_worker.py), [replay_saved_native_keyframe.py](../scripts/replay_saved_native_keyframe.py), [native_occusg_worker.py](../scripts/native_occusg_worker.py).

## Công cụ ngoài runtime chính

| Script | Đọc phụ thuộc ngoài | Có thể ghi ngoài 3DSG? | Ghi chú |
|---|---|---|---|
| `scripts/preflight_legacy_mappers.py` | Hai checkout, ba venv, model/config/binary | Không theo thiết kế; chỉ probe/đọc và in report | Kiểm tra readiness, không cài package hoặc chạy mapper |
| `scripts/replay_saved_native_keyframe.py`, `scripts/replay_native_keyframe_sequence.py` | Hai checkout, env, checkpoint, source run, binaries | Bình thường ghi replay vào output được chọn; không nên giả định mọi CLI path đều nằm trong 3DSG | Phục vụ replay/offline verification |
| `scripts/replay_m2_compact_parity.py` | M2 fixture binary trong Rebuild | Có thể tạo output fixture theo tham số | Không phải startup dependency; default binary path có thể cũ |
| `scripts/compare_p3_cluster_backends.py` | Source/adapters và env của hai checkout | Diagnostic output tùy CLI | Chỉ so sánh backend offline |
| `scripts/patch_occusg_m2_binary_transport.py` | Source OccuSG và Fusion | **Có.** Có thể sửa C++/Python/tests/lock trong cả hai checkout và file adapter/test trong 3DSG; tạo backup ở `/tmp` | Công cụ patch chủ động; không chạy trong startup bình thường |
| `scripts/fix_occusg_m2_identity_mapping.py` | Source/test của Fusion | **Có.** Sửa `Fusion_Concept_OccuSG/src/standalone_sg/mapping/native.py` và test tương ứng; backup ở `/tmp` | Công cụ patch chủ động; không chạy trong startup bình thường |

Hai patcher là điểm cần báo teammate rõ nhất: nếu tích hợp thay đổi của chúng vào repo chuẩn, hãy mang patch và regression test sang đúng repository sở hữu thay vì tiếp tục chạy script trên sibling checkout dùng chung.

## Mức độ ràng buộc và tính di động

- Nhiều config chứa đường dẫn tuyệt đối bắt đầu bằng `/home/kiettran05/Desktop/test_Room_segmentation/...`; trên máy teammate, cùng file config sẽ không tự hoạt động.
- Mặc định runner giả định hai repo nằm cạnh `3DSG`; Desktop còn hardcode Python Habitat cá nhân.
- `pyproject.toml` không mô tả đầy đủ các môi trường Habitat, P2, ConceptGraphs, OpenCLIP, OccuSG hay native toolchain. Không thể tái tạo native demo chỉ bằng `pip install -e 3DSG`.
- Không đưa virtualenv, HM3D, checkpoint hoặc build binaries lớn vào Git. Cần manifest tài nguyên có version/hash và hướng dẫn tải/đặt asset.
- Các run records có thể giữ đường dẫn tuyệt đối để provenance; phân biệt chúng với config đầu vào mà teammate cần dùng lại.

Trong quickstart hiện có một câu chưa đồng bộ: [demo_quickstart.vi.md](demo_quickstart.vi.md) dòng 96 nói auxiliary navigation depth chưa vào native mapper, nhưng dòng 98 và adapter code ghi nhận OccuSG nhận auxiliary depth riêng. Cần thống nhất câu này khi cập nhật tài liệu gộp.

## Checklist khi gộp với teammate

1. **Chốt quyền sở hữu source:** xác định phần nào thuộc 3DSG adapter/orchestrator, phần nào thuộc Fusion/ConceptGraphs, phần nào thuộc Rebuild/OccuSG. Đưa thay đổi từ hai patcher về repo sở hữu tương ứng.
2. **Không gộp virtualenv hoặc output:** tạo environment lock/setup riêng cho Habitat, P2/OccuSG, ConceptGraphs và P3/OpenCLIP; ghi Python/CUDA/CPU/native compiler requirements.
3. **Thay đường dẫn máy cá nhân bằng cấu hình:** ví dụ biến `THREEDSG_CONCEPTGRAPHS_ROOT`, `THREEDSG_OCCUSG_ROOT`, `THREEDSG_HABITAT_PYTHON`, `THREEDSG_ASSET_ROOT`; truyền config path rõ ràng và kiểm tra preflight sớm.
4. **Đóng gói data tách khỏi source:** HM3D, weights, OpenCLIP checkpoint và native binaries có manifest, version/license, SHA-256, và lệnh cài/tải nội bộ; tránh commit artifact lớn.
5. **Pin giao diện giữa repo:** ghi commit/revision của mỗi checkout, schema packet/JSONL, semantics frame/time/hash, stride/calibration auxiliary depth, M2/DuDe binary ABI và protocol version.
6. **Giữ replay làm hợp đồng tích hợp:** golden packet/keyframe phải chạy được từ môi trường teammate; so khớp output hash/state trước khi thử live.
7. **Tách profile theo mục tiêu:** navigation-only không cần import P2/native; profile native phải fail sớm với danh sách path/checkpoint/binary còn thiếu.

## Giới hạn của báo cáo

Đây là kiểm kê dựa trên source/config/run metadata cùng một probe chỉ đọc. Probe xác nhận path mặc định tồn tại tại thời điểm kiểm tra, nhưng không chạy episode để xác nhận độ tương thích end-to-end hoặc pin revision của các checkout. Plugin tự viết được truyền qua `--graph-plugin` có thể mở thêm dependency tùy ý, nên không thể liệt kê toàn bộ ảnh hưởng của mọi plugin tùy biến.

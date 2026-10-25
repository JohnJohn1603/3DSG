# U5 duplicate-v2 M9 integration report

Ngày kiểm tra: 2026-10-11  
Trạng thái: **experimental opt-in đạt kiểm chứng tích hợp có giới hạn**. Source vẫn là mặc định; không có tuyên bố cải thiện chất lượng tổng quát.

## Khóa và phạm vi

- Candidate: `geometry-only-dedup-v2`; profile cũ `geometry-only` và source vẫn nguyên trạng.
- Config `configs/association/u3_same_frame_duplicate_candidate_v2.json`: SHA-256 `257dd354c7b562fed4b8d2b6548fcf38fe67946aef2e95ae1c084d79044d3ec3`.
- Offline reference assignments cho 00808: SHA-256 `7fd667f3526c6889f42a089f116e19c98ed8c89b72a4f795e9e82f64a993417f`.
- Capture 00808: SHA-256 manifest `ed2c843d85414c50475f6b66e34c122b4b7afe7b2d24014d434b3f97bbf473ab`; P3 cache SHA-256 `17749b9ddcac6e38785885a77a6ad6381aadfbb0a03b09693cbb7631917ff06b`.
- Binaries dùng nguyên từ resource lock: M2–M3 `bd171a8ac9974eec18957af97e6fc369729358ca62985390eb994c26f2040b29`; DUDE `7868c892f3584faa17712e1ede0660006d97531b05be5c258fe303015f1ef959`.
- Không dùng GT, annotation, class hay observation tương lai để deduplicate. V2 chỉ đọc mask P2 đã hash-verify và P3 support của frame hiện tại. Guard containment vẫn là heuristic area-ratio 1.5; ambiguous chỉ có nghĩa không alias, không buộc association defer.
- Observation alias được giữ trong diagnostics với `duplicate_of`, canonical ID, `graph_track_merge=false`, không tính independent view/semantic vote. Đây không phải graph-track alias/merge event; raw detection cache không bị xóa.

## Kết quả parity và fixture

`check_u5_duplicate_v2_adapter_parity.py` đạt parity chính xác với offline reference trên cả ba cache:

| Scene | Observations | Alias | Ambiguous | Deferred | Assignment/group/evidence/geometry parity |
|---|---:|---:|---:|---:|---|
| 00800 | 30 | 0 | 0 | 0 | exact |
| 00802 | 713 | 41 | 2 | 16 | exact |
| 00808 | 18 | 1 | 0 | 0 | exact |

00802 R23 được kiểm tra thêm qua M9 fixed-region causal prefix frames 0–235: 611 assignment rows khớp offline reference; `det-0001 → u3-0018`, `det-0003 → defer`, không alias. Snapshot reload/delta replay đạt, không có dangling/self edges. Đây là fixture với region cố định, không chạy native M1–M5 và không khẳng định tương đương dense native replay.

## Paired native replay 00808

Cùng capture/P2/P3 packets và native binaries; mỗi profile có process/state/output riêng. Capture, detector, P3 và OpenCLIP không chạy lại. Native mapping M1–M5 vẫn được replay từ RGB-D cache theo contract launcher.

| Profile | Run ID | Thời gian | Peak RSS | Nodes / edges | Rooms / regions |
|---|---|---:|---:|---:|---:|
| geometry-only | `u5m9_20261011T013316.162775Z_58aad8` | 71.5 s | 599,968 KiB | 77 / 425 | 1 / 1 |
| geometry-only-dedup-v2 | `u5m9_20261011T013431.466602Z_136a93` | 66.2 s | 599,960 KiB | 76 / 424 | 1 / 1 |

- Cả hai run: offline assignment parity `true`; fresh-process replay, snapshot reload và delta replay đều `true`; graph integrity có 0 dangling edge, 0 self edge, 0 conflicting room owner.
- Năm file grid `.npz` giống hash từng frame; grid trace index cũng giữ nguyên. Các trường cấu trúc `native_mapping` (M1–M3), bỏ các trường runtime, khớp; M4 status/branch/guard/stale/region count và room contour/region structure khớp. Runtime khác nhau như dự kiến.
- Khác biệt dự kiến ở M9/M8 membership: frame 2, geometry-only tạo track riêng cho `...0002:det-0004` và `...0002:det-0005`; v2 tạo track cho `det-0004`, ghi `det-0005` là alias observation cùng canonical. Alias không tạo graph merge event/semantic vote. Graph cuối ít hơn một object node và một `room_contains` membership edge; room/region geometry không đổi. Provenance alias nằm trong frame diagnostics.
- D1: các observations 0000:det-0001, 0001:det-0003, 0002:det-0003 và 0004:det-0001 tiếp tục vào `u3-0002` ở cả hai profile; revisit không bị đổi.
- D2: hai detections cùng frame đi vào cùng track ở v2 do observation alias; điều này xác nhận tích hợp hành vi duplicate đã khóa, không phải một phép đo accuracy mới.

## Launcher, tests, và giới hạn

Đã kiểm chứng lệnh:

```bash
python3 scripts/scene_launcher.py replay-cached u6_hm3d_00808_quality_v1_20261010 --profile geometry-only
python3 scripts/scene_launcher.py replay-cached u6_hm3d_00808_quality_v1_20261010 --profile geometry-only-dedup-v2
python3 scripts/scene_launcher.py results u5m9_20261011T013431.466602Z_136a93
```

Lệnh `results` hiển thị graph backend, profile, semantic status, alias/ambiguous/deferred, offline parity và cảnh báo experimental. Mỗi run có thư mục riêng.

Regression đã chạy:

```bash
python3 -m pytest -q tests/test_u5_duplicate_v2_backend.py tests/test_u5_backends.py tests/test_same_frame_duplicate_candidate.py tests/test_same_frame_duplicate_candidate_v2.py tests/test_scene_launcher.py tests/test_m9_builder.py tests/test_m9_object_lifecycle.py tests/test_m10_serialization.py
```

Kết quả: **98 passed**. `py_compile` cho backend, runner, launcher, parity checker, R23 fixture và test module cũng pass. Full suite chưa chạy.

Gate đạt: adapter/offline parity trên ba cache; M9 integration paired trên capture ngắn; serialization/reload/delta/invariants; source/default không đổi. Gate còn partial: chỉ 5 frame native ở 00808; không kiểm chứng dense native 00802; area-ratio guard chỉ có ít containment cases và không nhận biết mixed-object masks tổng quát; không có merge/retirement policy. Geometry-only-dedup-v2 chỉ nên được dùng experimental opt-in với các giới hạn này. U4-OV vẫn diagnostics-only; không đổi class hoặc room scores.

## Code/config hashes

| File | SHA-256 |
|---|---|
| `src/standalone_sg/objects/u5_backends.py` | `d86147246ad8f513d7e27aaa7b891732e9e3f054c3978d8242d5ba72ab337133` |
| `scripts/run_habitat_native_replay.py` | `b28d45f2a3eff622cbc03d312cf29fee37a7333b4f763b9258e85ebad7fb7038` |
| `scripts/scene_launcher.py` | `edfd5cd702c739c2a1f107f1ef44b35d517a15138be91cc92755313b9dbc79fb` |
| `scripts/check_u5_duplicate_v2_adapter_parity.py` | `db1f656aefa5edab7144c9283003107df15adeea75157f35c5faeb93b01ec7ac` |
| `scripts/run_u5_duplicate_v2_r23_fixed_region_m9_fixture.py` | `d4c4bee7f23a4d08a487f6c4aad6e0bed2f7450a1982fabc2a42b39e6da44914` |
| `tests/test_u5_duplicate_v2_backend.py` | `8a02e062fe57c5e24f9282f20d95b08767cbb4d0c4b7d6db0b58c0adb6969b6d` |

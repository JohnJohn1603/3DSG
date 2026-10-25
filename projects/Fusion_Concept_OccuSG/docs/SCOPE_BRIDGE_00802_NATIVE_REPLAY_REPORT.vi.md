# Scope Bridge 00802 — kết quả instrumentation và một native replay

Ngày: 2026-10-16  
Phạm vi được duyệt: instrumentation diagnostic và đúng một native replay trên cache 29 frame. Không chạy detector, encoder, Qwen, hybrid mapping, capture hoặc thay room projection/matcher.

## Kết quả

**Kết quả C — spatial scope vẫn chưa đủ bằng chứng.** Instrumentation xác nhận M3 có các ô free với nhiều khoảng Z rời nhau; M3 lưu đáy-thấp nhất và đỉnh-cao nhất thành một envelope trước khi M4 tạo room polygons chỉ có XY. Đây là dấu hiệu collapse của projection. Các khoảng rời cũng có thể do furniture/occlusion, nên chưa chứng minh projection đã gộp các tầng không tương thích. Không tạo scope contract; membership và room belief tiếp tục `unavailable`.

Gate được ghi trong `scope_bridge_replay_contract.json` trước replay. Gate chỉ coi một prefix/room tương thích khi mọi cell được M4 hỗ trợ trong polygon có interval trace chính xác và đúng một free interval còn lại sau ngưỡng nguồn `min_free_height_m=0.3`; đồng thời object geometry phải có cùng world frame và chỉ dùng prefix đã đến thời điểm đó. Không có ngưỡng mới được chọn sau khi xem kết quả. Điều kiện này là gate bảo thủ cho phạm vi quan sát được, không chứng minh toàn scene không có tầng chưa quan sát.

## Provenance đã khóa

| Đầu vào / thành phần | Giá trị đã dùng |
|---|---|
| Capture manifest SHA-256 | `1576cfd90c1ad8432dccc2312725fa703949f0232691a1c2cddcc96b0554d0d2` |
| Scene / sequence | `00802-wcojb4TFT35` / `hm3d-v02-00802-main-route-01` |
| Frames | 29 |
| Config `configs/reference_code_mp3d.yaml` SHA-256 | `210124b16fd3651c06fd548a1704f27595f61761dd1bfb4db4099e51895d1523` |
| MapConversion source revision | `14b8cb9e6534ba21e84f624c94d20960f6613f3c` |
| Original locked M2/M3 binary SHA-256 | `bd171a8ac9974eec18957af97e6fc369729358ca62985390eb994c26f2040b29` |
| Instrumented M2/M3 binary used for the single replay SHA-256 | `49351be3c8ec8c4200c2d3d0ba00d51179f00c67db8abd291823af24919cebb2` |
| M4 binary SHA-256 | `7868c892f3584faa17712e1ede0660006d97531b05be5c258fe303015f1ef959` |
| Source lock SHA-256 | `a717323522bded8d1e846125b740889f748ca38a6b842de8b71c95039a346a3c` |

Room/scene inputs và binaries ngoài workspace chỉ được đọc. Build instrumentation dùng OctoMap đã có, trong `/tmp`, `MAX_JOBS=2`; không cài dependency, không sửa baseline binary hoặc resource lock.

## Instrumentation và kiểm chứng

Observer ghi `HeightRangeMap` tại đúng lần M3 update, sau source pruning và trước khi các khoảng free bị rút thành envelope. Sidecar giữ frame/map revision, cell/grid origin/resolution, world XY, khoảng free trước/sau pruning, occupied intervals và trạng thái M3. Observer không đưa dữ liệu ngược vào occupancy, projection, segmentation hoặc RNG. M4 nhận cùng grid và chạy binary gốc.

Fixtures một free interval và hai free intervals rời tại cùng XY đã so baseline binary, instrumented trace-off và trace-on. Giá trị M3, floor/ceiling arrays, origin/resolution/shape và chữ ký regions M4 khớp; trace lần lượt có một và hai intervals tại cell kiểm tra. Sau khi thêm assertion kiểm tra trực tiếp `z_floor_m`/`z_ceiling_m`, binary đã dùng cho replay thất bại đúng assertion này: tọa độ world double bị đưa vào getter chỉ nhận grid index. Source diagnostic được sửa bằng công thức index `floor((world_xy - map_offset_xy)/resolution)`. Bản sửa chỉ được build/test trên fixtures sau replay; binary sửa không được dùng để replay cache. Các raw direct-Z fields trong trace của replay phải coi là không hợp lệ. Phân tích đã bỏ qua chúng và join intervals với M3 NPZ theo đúng grid transform; cả **145,425/145,425** cell có free interval khớp chính xác M3 envelope trong sai số `1e-5 m`.

Hai frame không có cell trace trong update cục bộ: frame 13 không có map update; frame 22 update có grid `1×2`, không có interior cell mà thuật toán nguồn xử lý. Vì vậy một số room support có trace thiếu; các cell thiếu không được coi là single-scope.

## Một native replay và parity

Replay duy nhất được ghi tại `artifacts/scope_bridge/00802_29frame/run_20261016T022841Z/replay/`, với interval sidecars trong `grid_trace/`. Runtime là **101.67 s**, peak RSS **603,248 KiB (~589 MiB)**. Không bật determinism replay lần hai; `fresh_replay_equal` vì vậy là `null`. Snapshot/delta reload checks của replay đạt.

Đối chiếu với replay baseline cùng input/config:

- `snapshot.json` và `delta_log.json`: byte-identical với baseline.
- M3 direct pinned-source oracle: bounds, input leaves và geometry khớp 29/29 frame; value/floor/ceiling mismatch đều bằng 0.
- Các field M4 theo frame (accepted revision, branch/status, guard, region count, stale/reason), events và node counts: khớp 29/29 frame.
- Tất cả 29 frame có liên kết interval trace ↔ M3 revision ↔ M4 input/room revision; không dùng room/object state tương lai trong phân tích prefix.

Các con số trên là parity của output được đối chiếu, không phải fresh replay thứ hai. Trường `instrumentation_status` trong manifest replay còn giá trị `read_only_candidate_pending_fixture_parity` từ lúc runner tạo manifest; postflight này ghi kết quả fixture đã chạy sau đó thay vì sửa log gốc.

## Evidence theo prefix

- 194,960 cell-update records được ghi; 11,932 có ít nhất hai free intervals rời nhau sau ngưỡng producer.
- 29 frame được nối với đúng M4 mask và M8 room revisions; 155 room-prefix states được kiểm tra. **0/155** đạt gate nghiêm ngặt “mọi support cell có trace và đúng một free interval”.
- Projection-collapse indicator đầu tiên xuất hiện tại frame 0 trong `room:1`; room IDs có indicator lần đầu: `room:1` frame 0, `room:2` frame 2, `room:3/4/5` frame 4, `room:6` frame 5, `room:7` frame 11.
- Ở frame cuối, support trong room polygons:

| Room | M4 support cells | Trace thiếu | Không còn free interval | Một interval | Nhiều interval rời / envelope collapse |
|---|---:|---:|---:|---:|---:|
| `room:2` | 9,059 | 0 | 61 | 8,451 | 547 |
| `room:3` | 3,325 | 0 | 9 | 2,705 | 611 |
| `room:5` | 3,104 | 0 | 553 | 2,452 | 99 |
| `room:6` | 8,529 | 0 | 0 | 8,193 | 336 |
| `room:7` | 1,307 | 32 | 86 | 1,003 | 186 |

Ví dụ tại frame 0, một cell trong room polygon có free intervals `[2.04995, 2.40005] m` và `[0.99995, 2.00005] m`; M3 envelope là `[0.99995, 2.40005] m`. Đây chứng minh envelope bao hai khoảng rời, không cho biết khoảng cách do sàn khác hay furniture/occlusion.

## Hybrid object scope riêng

Validator đọc đủ 29 prefix snapshots từ hybrid timeline (không dùng final export để lấp lịch sử): 665 hypothesis-cloud references và 664,705 điểm XYZ. Hash cloud, finite XYZ, point count, bounds, full observation lineage và quy tắc không có future observations đều được kiểm tra. Contract cùng capture manifest, frame là `conceptgraphs_world_from_capture_contract`, units mét, `T_capture_world_from_cg_world=identity`. Prefix points có world-Z từ `-0.02135` đến `2.17408 m`.

Điều này xác nhận geometry hybrid có tọa độ metric world/prefix; **không gán floor/layer cho objects**. Không dùng P3 legacy để loại observations thiếu P3, và không dùng final hybrid membership.

## Giới hạn và bước producer nhỏ nhất

Room producer không gắn free intervals vào connected spatial components hoặc layer IDs, còn M4 chỉ giữ polygon XY. Intervals riêng lẻ chưa đủ để giải nghĩa furniture/occlusion so với elevation components. Hybrid object XYZ cũng không tự khẳng định một room polygon tương ứng với cùng scope đứng.

Bước producer nhỏ nhất cần thiết trước membership là một diagnostic sidecar theo room revision: giữ source-cell/interval lineage và tính/ghi connected free-space component IDs trong 3D cho phần support của mỗi M4 polygon. M4 polygons vẫn giữ nguyên; không có unique component hoặc object-component evidence thì tiếp tục `unavailable`. Đây là đề xuất, chưa được triển khai hay dùng để gán floor trong lượt này.

## Artifacts và kiểm tra

- Prefix report: `artifacts/scope_bridge/00802_29frame/run_20261016T022841Z/scope_bridge_prefix_report.json`
- Postflight hashes/test evidence: `artifacts/scope_bridge/00802_29frame/run_20261016T022841Z/scope_bridge_postflight.json`
- Free-interval ↔ M3 envelope join: `artifacts/scope_bridge/00802_29frame/run_20261016T022841Z/height_interval_m3_envelope_join.jsonl` (89,313,020 bytes)
- Native replay manifest/output: `artifacts/scope_bridge/00802_29frame/run_20261016T022841Z/replay/`

Targeted tests thực chạy:

The original trace-off/on fixture parity (M3 arrays and M4 signatures) passed **2/2** before the direct-Z assertion was added. With the stronger assertion, rerunning the preserved binary used for the cache replay failed **2/2** at that direct-Z field, reproducing the instrumentation defect; this is not counted as a gate pass. After the minimal index fix, the isolated fixture build passed **2/2**, including per-cell direct-Z equality against captured M3 arrays. The analyzer tests passed **3/3**.

Reproducible current commands and saved logs:

```bash
PYTHONPATH=src SCOPE_BRIDGE_NATIVE_BINARY=artifacts/scope_bridge/00802_29frame/run_20261016T022841Z/instrumented_build/scope_bridge_m2_m3_worker python3 -m pytest -q -p no:cacheprovider tests/test_scope_bridge_height_trace.py --basetemp /tmp/scope_bridge_height_fixture_tests_original_saved
# Expected reproduction of old direct-Z defect: 2 failed at z_floor_m/z_ceiling_m assertion.

PYTHONPATH=src SCOPE_BRIDGE_NATIVE_BINARY=artifacts/scope_bridge/00802_29frame/run_20261016T022841Z/instrumented_build/scope_bridge_m2_m3_worker_post_replay_z_index_fix_fixture_only python3 -m pytest -q -p no:cacheprovider tests/test_scope_bridge_height_trace.py --basetemp /tmp/scope_bridge_height_fixture_tests_zfix_saved
# 2 passed

python3 -m py_compile scripts/analyze_scope_bridge_trace.py
PYTHONPATH=src python3 -m pytest -q -p no:cacheprovider tests/test_scope_bridge_trace_analysis.py --basetemp /tmp/scope_bridge_analysis_tests_final4
# 3 passed
```

Logs: `artifacts/scope_bridge/00802_29frame/run_20261016T022841Z/postflight/height_trace_original_binary_pytest.log`, `height_trace_zfix_pytest.log`, and `trace_analysis_pytest.log`.

Không chạy membership/semantic variants vì spatial-scope gate chưa mở. A tables, thresholds, admission, hybrid timeline, room geometry và default không đổi.

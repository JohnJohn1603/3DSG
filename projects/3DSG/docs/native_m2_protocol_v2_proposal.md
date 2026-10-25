# Giao thức OccuSG M2 v2: đề xuất và trạng thái triển khai

Ngày cập nhật: 24-10-2026. M2 compact response và bounded 3SGM v2 binary input đã được triển khai trong native worker/Python client đang dùng. Binary transport là opt-in; ASCII vẫn là mặc định. Golden packet frame 146 và replay tích lũy 7 frame đã xác nhận parity trước khi thử live.

## Trạng thái mới nhất

- Runtime OccuSG nhận cờ `--m2-response-mode compact|debug`; mặc định là `compact`. Compact trả revision và số lượng free/occupied key; debug giữ đầy đủ key/observations để đối chiếu. Python client hỗ trợ cả hai, và M3 stage lấy count từ compact response.
- Golden frame 146 cho ASCII/binary exact parity trên M2 keyset hashes và toàn bộ M3 arrays/metadata; xem [exact report](../runs/occusg-m2-transport-parity-20261024-a/frame146_exact.json). 7 keyframe tích lũy cho mapping records, auxiliary-depth provenance, M3 source oracle và room/object semantic hashes; xem [sequence parity report](../runs/occusg-m2-transport-parity-20261024-a/report.json), [ASCII replay](../runs/occusg-m2-ascii-sequence-20261024-c/summary.json) và [binary replay](../runs/occusg-m2-binary-sequence-20261024-b/summary.json).
- Binary input decoder/client đã được nối và build trong hai repository backend. Packet có frame ID/index, monotonic timestamp/domain, SHA-256, projection mode, float32 points và little-endian hit bitset; server giới hạn payload 128 MiB và kiểm tra identity echo. Chỉ bật bằng `OCCUSG_M2_TRANSPORT=binary`; không tự fallback sau khi gửi update. Source backup trước patch ở `/tmp/occusg-m2-binary-backup-20261024/`.

## Vì sao cần v2

- ASCII vẫn là transport mặc định tương thích; tùy chọn `OCCUSG_M2_TRANSPORT=binary` gửi một bounded `3SGM` v2 request thay cho header/dòng text trên từng điểm. Response mặc định gồm revision/counts; debug mode giữ các arrays `free_keys`, `occupied_keys` và `observations`.
- Mapping stage trong 3DSG chỉ dùng revision và độ dài hai key-set từ kết quả `integrate`; không đọc key values hoặc `observations`. M3 vẫn cần kết quả projection đầy đủ.
- Trong native combined run, frame 146 có 307.200 điểm; M2 wall `13,04 s`, Python CPU `10,26 s`, native child CPU `6,22 s`, request write wall `2,28 s`, response read wall `9,09 s`. Native map logic vẫn chiếm CPU; đổi transport không loại bỏ OctoMap computation.

## Envelope

Mỗi request/response là một frame nhị phân trên cùng stdin/stdout persistent process:

| Field | Encoding | Mục đích |
|---|---|---|
| magic | 4 byte ASCII `3SGM` | Phát hiện stream lệch |
| version | `uint16 LE`, giá trị 2 | Version negotiation |
| opcode | `uint16 LE` | `HELLO`, `INTEGRATE_PROJECT`, `CLOSE` |
| request_id | `uint64 LE` | Ghép đúng response với request |
| payload_len | `uint64 LE` | Đọc frame đầy đủ, giới hạn allocation |
| flags | `uint32 LE` | Bằng 0 trong v2 hiện tại |
| reserved | `uint32 LE` | Bằng 0; dành cho mở rộng |

Header dài 32 byte. Bản benchmark dùng đúng struct này.

## `HELLO`

Client hỏi capability/version trước khi gửi điểm. Server trả version được hỗ trợ, max payload, dtypes, endian, M2/M3 capability và revision hiện tại. Không tự fallback sau khi đã gửi một update; nếu response bị mất giữa update thì worker/session phải bị poison và replay từ keyframe đã lưu để tránh tích phân trùng.

## `INTEGRATE_PROJECT`

Wire payload hiện tại có thứ tự cố định: `INTEGRATE_METADATA`, `projection_mode`, `INTEGRATE_IDENTITY`, UTF-8 frame ID, package digest, XYZ và hit bitset. Các trường:

- `max_range_m`: `float64 LE`, giữ đúng kiểu `double` của `Scan.max_range`.
- `origin_xyz`: 3 × `float32 LE`, đúng kiểu `Scan.origin` và phép cast hiện hành vào OctoMap.
- `point_count`: `uint32 LE`.
- `projection_mode`: `uint8` enum `full_refresh=1`, `partial_update=2`, `clean_full_refresh=3`; nằm ngay sau metadata fixed-size.
- `frame_index`: `uint64 LE`.
- `capture_monotonic_ns`: `uint64 LE`, trong clock domain được khai báo ở run manifest; worker echo nguyên giá trị, không so sánh trực tiếp giữa host khác nhau.
- `frame_id_length`: `uint16 LE`, tiếp theo là UTF-8 `frame_id` (1..1024 byte).
- `package_sha256`: 32 byte digest nhị phân của immutable keyframe package; response echo frame index, timestamp, ID và digest.
- `xyz`: `point_count × 3` `float32 LE`, row-major và giữ nguyên thứ tự pixel của M1.
- `has_hit`: bitset little-bit-order, `ceil(point_count/8)` byte. Bit 1 là hit; bit 0 là confirmed no-return. Invalid/masked depth không được biến thành no-return ray.

Với 307.200 điểm: XYZ chiếm 3.686.400 byte; hit bitset 38.400 byte; metadata/envelope có identity/provenance vẫn dưới 1.2 KB. Tổng khoảng 3,73 MB, so với ASCII hiện tại khoảng 17,95 MB ở đúng frame 146. Giao thức v2 giữ float32 point cloud hiện được OccuSG nhận; không giảm stride hay bỏ điểm.

Response v2 nên echo `request_id`, frame identity, M2 revision, M3 revision/origin/shape/resolution/bounds và checksum state. Compact response đã thực hiện cho M2, nhưng M3 response vẫn giữ output/schema hiện tại; v2 đề xuất thay grid JSON lists bằng binary typed arrays (`int8` values, `float64` floor/ceiling; `NaN` cho unknown). Phải bảo toàn M3 backend, partial-update bounds và exact grid state.

Gộp tích phân và projection trong một opcode bỏ một lần request/response và giữ một transaction/revision boundary. Nếu upstream muốn tách opcode, client vẫn phải ghép revision và reject response M3 không cùng revision với M2.

## Client codec và benchmark trong workspace

Codec client/harness hiện ở [native_m2_wire.py](../exploration/native_m2_wire.py); script [benchmark_native_m2_wire.py](../scripts/benchmark_native_m2_wire.py) gọi chung codec này. Parser tham chiếu C++ nằm ở [m2_v2_codec.hpp](../native/m2_v2_codec.hpp), với CLI fixture [m2_v2_decode_fixture.cpp](../native/m2_v2_decode_fixture.cpp). Packet có envelope 32 byte và request `INTEGRATE_PROJECT`, gồm metadata, `projection_mode` (`uint8`), frame identity, package digest, XYZ và packed hit bitset. Python/C++ decoder kiểm tra magic/version/opcode, flags, payload cap 128 MiB, chiều dài/frame ID, projection enum, finite geometry và padding của hit bitset. Bốn Python fixture codec kiểm tra round-trip, giới hạn payload và packet lỗi.

Benchmark dùng keyframe native frame 146. Tái chiếu depth độc lập cho ra 307.200 điểm đúng từng bit với upstream `backproject_depth`; origin float32, hit flags, frame ID/index, monotonic timestamp, package SHA-256 và projection mode được round-trip kiểm tra. Golden packet hoàn chỉnh:

```bash
python3 scripts/benchmark_native_m2_wire.py \
  --packet-output runs/native-m2-wire-v2-schema-complete-20261024-a/frame_00000146.m2v2.bin \
  --output runs/native-m2-wire-v2-schema-complete-20261024-a/report.json
```

Kết quả 3 lần của packet schema đầy đủ:

- Text v1: 17.947.434 byte; encode median `2,00 s` trong run artifact này.
- Binary envelope + `projection_mode` + frame identity/provenance: 3.724.964 byte; encode median `6,74 ms`; payload nhỏ hơn `4,82×`.
- Binary round-trip giữ chính xác toàn bộ float32 XYZ và hit flags. Kết quả này chỉ đo packing/size; chưa đo C++ parse, OctoMap, projection hoặc end-to-end runtime.
- Parser C++ tham chiếu biên dịch bằng `g++ -std=c++17 -O2 -Wall -Wextra -Werror -pedantic` và đọc golden packet:

  ```bash
  g++ -std=c++17 -O2 -Wall -Wextra -Werror -pedantic \
    native/m2_v2_decode_fixture.cpp -o /tmp/m2_v2_decode_fixture
  /tmp/m2_v2_decode_fixture \
    runs/native-m2-wire-v2-schema-complete-20261024-a/frame_00000146.m2v2.bin
  ```

  Frame ID/index, mode `full_refresh`, SHA, 307.200 XYZ/hit được nhận; first/last XYZ khớp Python decoder. Transcript: [cpp_decode.txt](../runs/native-m2-wire-v2-schema-complete-20261024-a/cpp_decode.txt). Đây xác minh Python→C++ wire compatibility của parser fixture, chưa phải OccuSG server integration hay OctoMap parity.
- Frame 146 có 307.200 hit và không có no-return ray; vì vậy semantic của bit `has_hit=0` chưa được đối chiếu với upstream từ fixture này.

Artifacts: [wire benchmark report](../runs/native-m2-wire-v2-schema-complete-20261024-a/report.json), [golden packet](../runs/native-m2-wire-v2-schema-complete-20261024-a/frame_00000146.m2v2.bin). Binary request decoder đã được nối vào OccuSG server/native session; response M3 chưa có typed binary codec. Đây là transport integration có parity bằng replay, nhưng chưa có bằng chứng về throughput improvement.

## Điều kiện trước khi bật

1. Implement server/client v2 trong một native OccuSG branch có version, giữ v1 fallback bằng handshake capability.
2. Cùng keyframe, cùng resolution/config và cùng thứ tự point: so sánh M2 revision, exact free/occupied keys ở debug mode, M3 origin/shape/values/floor/ceiling, changed bounds và room state hash.
3. Replay 11 packet tích lũy; sau đó paired ConceptGraphs/OccuSG replay với exact room/object state hash và provenance.
4. Đo write/read wall, Python/native CPU, peak RSS, graph lag và NavMap stale/drop; chỉ sau parity mới thử combined 120 giây movement/freshness gate.
5. Nếu M2 native CPU vẫn thống trị sau giảm protocol overhead, profile OctoMap compute/incremental update riêng. Không dùng stride giảm, sampling, thay đổi ray semantics hoặc nới freshness để tạo tốc độ biểu kiến.

Đường native đã xác nhận: `NativePointCloudMappingStage.build_grid()` tạo `ScanPoint` Python theo từng điểm trong `src/standalone_sg/mapping/native_pipeline.py`; `OctoMapWorker.integrate()` trong `src/standalone_sg/mapping/native.py` hỗ trợ ASCII mặc định và binary opt-in; `native/occupancy/fixture_runner.cpp` đọc cả hai. Compact response và binary v2 encoder/parser đã được build trong hai sibling checkout. Binary đang opt-in, có strict identity echo, chưa có capability negotiation và không fallback âm thầm sau khi đã gửi update.

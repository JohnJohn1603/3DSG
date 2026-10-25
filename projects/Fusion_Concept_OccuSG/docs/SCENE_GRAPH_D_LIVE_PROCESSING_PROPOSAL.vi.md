# Proposal D — live processing viewer trên cache

Trạng thái: **D1 đã chạy một real validation từ cache C2; xem kết quả nghiệm thu ở cuối tài liệu.**  
Căn cứ: kế hoạch chính tại `SCENE_GRAPH_PROJECT_REVIEW_AND_COMPLETION_PLAN.vi.md`; C2 run `c2_replica_apt0_short_20261016_retry3`.

## Mục tiêu và ranh giới

Thử một run **live processing từ cache**: phát các frame đã capture theo thứ tự, để object mapper và OccuSG room runner xử lý, rồi xem revisions ngay khi producer phát chúng. Đây không phải live capture, camera-rate realtime hay benchmark hiệu năng. D không yêu cầu membership/room semantics khả dụng; spatial scope chưa xác minh phải tiếp tục hiển thị unavailable.

Run đầu dùng capture/P2/P3/features đã có của C2 `apt_0`. Nó sẽ chạy lại hai phần xử lý cần tạo revisions — ConceptGraphs mapping core và OccuSG room replay — trên cache theo thứ tự; capture, detector, image/text encoder và model được reuse, không chạy lại. Output đặt trong run directory mới. Giữ matcher, thresholds, seed, bảng A, geometry và final filter/merge như C2.

## Đã có và cần bổ sung

| Thành phần | Trạng thái hiện tại | D cần làm |
|---|---|---|
| C2 source cache | RGB-D/poses, P2 masks, P3 ledger, features, room snapshot và hashes đã có | Preflight/reuse theo manifest/hash; không dò “run mới nhất” |
| ConceptGraphs mapper | Xử lý frames theo thứ tự; `HybridCausalTimelineRecorder.record_frame()` ghi snapshot/geometry riêng từng frame | Flush immutable revision event/index ngay sau mỗi frame, không chờ `finalize()` ghi `frame_index.json` và `delta_log.json` |
| OccuSG runner | `_run_once()` gọi `builder.update()` theo frame; `snapshot.json` và `delta_log.json` chỉ được ghi sau khi hết packets | Phát room snapshot/delta và progress sau từng frame; giữ cùng binary/config và xác minh parity cuối run |
| Causal timeline viewer | Đọc timeline đã build xong; server Python stdlib chỉ phục vụ file tĩnh read-only | Mở rộng viewer/server sẵn có bằng endpoint đọc event journal mới; không thêm web framework |
| Unified graph | C2 tạo final unified graph sau khi các producer kết thúc | Tạo view revision từ state refs theo frame; không chạy association lần nữa, không thêm cạnh khi membership unavailable |
| Final postprocess | Filter/merge diễn ra sau online frame loop | Chỉ công bố final map sau khi stage đó thật sự chạy xong; không ghi final IDs/geometry/history vào prefix cũ |

### Nhân quả và đồng bộ hai nhánh

- Mỗi producer ghi event bất biến gồm `run_id`, branch, `scene_id`, `sequence_id`, `frame_index`, **full `frame_id`**, timestamp, branch revision, state/geometry paths và SHA-256. Số revision object và room độc lập; đồng bộ bằng full frame identity/timestamp đã xác thực, không ghép theo số thứ tự revision.
- D1 dùng barrier theo frame: chỉ phát unified view revision đã ghép khi hai nhánh đều xác nhận state đến cùng frame. Trước khi đủ cặp, UI giữ graph revision trước đó, hiện tiến độ/as-of riêng và nhánh đang chờ; không ghép object frame mới với room state tương lai hoặc ngầm tuyên bố hai state đồng bộ.
- Room update không đổi geometry vẫn phát `no_change`/state reference cho frame, để barrier biết room branch đã xử lý frame đó. Khi membership/scope chưa đủ, graph revision giữ `unavailable_elevation_scope`/`unavailable_membership_scope`, không tạo room→ConceptGraphs-object `room_contains`.
- Dùng queue tối đa một revision chưa ghép cho mỗi nhánh trong D1. Nhánh nhanh chịu backpressure khi nhánh kia chậm; event journal là append-only trên disk. UI có thể gộp render trung gian nhưng không được bỏ hoặc đổi thứ tự event đã commit. Nếu sau này cần cho producer chạy tự do, phải có thiết kế bounded queue/watermark riêng trước khi bật.
- Khi một nhánh lỗi, giữ revision hoàn chỉnh cuối cùng, hiển thị nhánh/lỗi/frame cuối, đánh dấu run `failed` và bảo toàn partial manifest. `completed` chỉ sau khi mọi frame được xử lý, hai nhánh đạt frame cuối, final postprocess/graph artifacts được kiểm tra và hashes ghi xong.

### Tiến độ, độ trễ và bộ nhớ

- Progress theo producer/stage: `started/running/completed/failed`, frame hiện tại/tổng frame, số revisions đã publish, frame lag, queue depth, thời gian từng frame/stage và elapsed. `flush=True` hoặc ghi event atomic để log đọc được khi process vẫn chạy.
- Đo latency từ lúc producer nhận frame cache đến lúc revision tương ứng xuất hiện trong viewer; báo throughput theo frames/s nhưng không gọi realtime nếu chưa có target và phép đo phù hợp.
- Event/state/geometry lịch sử nằm trên disk và tải khi seek/chọn revision. Viewer chỉ giữ state đang xem, một cặp đang đồng bộ và LRU giới hạn 6 revision geometry (theo cache policy viewer hiện có); không nạp mọi cloud lịch sử.
- Polling HTTP nếu dùng chỉ hỏi “có revision mới do producer phát chưa”; slider/play controls không sinh frame, và không dùng timer playback để mô phỏng live.

## Command dự kiến

**Đã chạy và kiểm tra** trên run `d1_c2_apt0_20261016_v2`:

```bash
./run_scene.sh live-replay \
  --source-run-id c2_replica_apt0_short_20261016_retry3 \
  --run-id d1_c2_apt0_20261016_v2 \
  --port 8765 \
  --viewer-base-url http://127.0.0.1:8765
```

Lệnh mở/tra cứu đã kiểm tra:

```bash
./run_scene.sh results d1_c2_apt0_20261016_v2
./run_scene.sh open d1_c2_apt0_20261016_v2 live --port 8765
```

Lệnh `open` in URL của server localhost sẵn có; nó không thay thế hoặc dừng process/server ngoài run. Preflight kiểm tra cache hashes, frame order, calibration/pose alignment, scene/sequence và native resource lock trước khi producer bắt đầu.

## Acceptance tests cho D1

1. Revision đầu xuất hiện trong event feed **trước** trạng thái completed; command không thể pass chỉ bằng playback timeline đã hoàn tất.
2. Mọi object state chỉ chứa observations có `frame_index <= event.frame_index`; mọi room state có cùng hoặc sớm hơn frame; final filter/merge không có trong prefix.
3. Object/room revision IDs khác nhau vẫn ghép đúng bằng scene/sequence + full frame ID; timestamp/frame mismatch bị từ chối.
4. Làm chậm một producer theo test hook: viewer hiện lag, barrier giữ revision trước, queue không vượt giới hạn, không có edge/geometry tương lai; khi producer bắt kịp, state mới hiển thị đúng thứ tự.
5. Scope unavailable được giữ qua mọi revision; không có object membership edge hoặc semantic label bị backfill.
6. Lỗi giữa run để lại manifest `failed` và partial outputs, không tạo manifest `completed`; run đủ điều kiện mới completed.
7. Memory policy: chỉ selected/current revisions được nạp; seek lùi fetch đúng state; cache không vượt LRU 6 revisions.
8. Kết thúc run: object partition/geometry sau cùng và room geometry/adjacency khớp C2 đã khóa trong tolerance/contract hiện tại; final postprocess vẫn là stage riêng. Đây là parity regression, không phải quality score.
9. GUI trực tiếp: xác nhận frame đầu/giữa/cuối đến trong lúc producer chạy, branch lag/status, object lineage/evidence, room polygons, seek và lỗi; lưu screenshot/log. Nếu không mở browser được, GUI gate giữ pending.

## Ngân sách đề xuất

Ước tính **4–5 giờ implementation + kiểm chứng** cho D1 trên đúng cache này:

- per-frame publishing cho object và room producers: 1.5–2 giờ;
- event join/barrier, status/progress manifest, failure handling: 1 giờ;
- tái dùng viewer/server với live event polling và bounded cache: 1 giờ;
- targeted tests, một lần replay từ cache và GUI acceptance: 0.5–1 giờ.

Room replay C2 đã đo 81.97 giây và mapping stage khoảng 8.63 giây trên input này; D runtime sẽ được đo lại, không suy ra realtime từ các con số đó. Đây là ngân sách triển khai dự kiến, chưa phải quyền chạy. Dừng xin duyệt nếu scope/implementation vượt 5 giờ hoặc cần thay native algorithm/binary.

## Sau D1

D1 chỉ chứng minh live processing từ **một cache route đã biết**. Live capture, realtime, route/scene thứ hai, multi-floor scope và quality validation vẫn là các gate riêng. Bước nhỏ nhất sau khi D1 đạt là chạy cùng workflow trên một Replica scene/config khác để kiểm tra portability; không đồng thời tuning object/room quality.

## Kết quả D1 trên cache C2

- **D1a — producer/coordinator:** đạt trong một lần chạy producer. Một revision object và một room revision được ghép theo full frame ID ở từng frame; queue unmatched tối đa một event mỗi nhánh; producer không cần browser để tiến; snapshots/geometry được ghi atomic trước khi event publish và joined states được hash.
- **D1b — live viewer:** GUI localhost thật được mở trong khi run chạy. Đã xem prefix đầu/giữa/cuối, live-follow, seek lùi giữ historical revision, play/pause, object detail, RGB/mask/overlay/crop, room polygons, object clouds và trajectory. Đây là GUI pass cho **2D XY**; XYZ/orbit không được hỗ trợ.
- **D1c — tests/parity/failure/GUI:** 50 targeted regression tests đạt; fixture tests bao phủ journal tail/duplicates, publisher lag, cancellation, nonzero producer failure, partial manifest và cleanup đúng process group. C2↔D1 object export/geometry và room graph parity đạt chính xác. Real run không cố ý làm hỏng producer; producer failure thực vẫn chưa được diễn tập trên input thật.
- Một attempt `d1_c2_apt0_20261016_v1` dừng trước producer vì sandbox từ chối tạo socket (`PermissionError`, 0 frame). Dùng server localhost read-only đã chạy sẵn cho `d1_c2_apt0_20261016_v2`. Run v2 hoàn tất đủ producer stages một lần; lỗi đầu tiên chỉ ở terminal viewer URL serialization và được khôi phục bằng bước viewer-only (`mapping_rerun=false`, `occusg_rerun=false`). Tổng số real producer replay là **một**.
- Input: ReplicaCAD `apt_0`, sequence `replica-cad-apt0-instance-route-01`, 12 fixed poses, 160×120. Capture/P2/P3/features được reuse; ConceptGraphs hybrid mapping và OccuSG room replay mỗi thứ chạy lại đúng một lần. Không chạy capture, detector, encoder hoặc Qwen.
- 15 online hypotheses và 1 final retained object là số lượng output của stage, không phải accuracy. Final object geometry và evidence IDs khớp C2; room graph final có 151 nodes/956 edges như C2.
- Membership giữ `unavailable_elevation_scope`; room semantics giữ `unavailable_membership_scope`; không có hybrid `room_contains`. Prefix admission riêng không được mapper xuất nên trạng thái hiển thị unavailable. Final filter/merge là terminal stage tách khỏi prefix.
- Runtime coordinator 64.71 s; terminal viewer-only repair 0.06 s. Room processing sum theo frame 54.34 s; object-side measured work 0.47 s, nhưng object mapping manifest wall runtime 58.67 s có tính barrier/ACK wait. Publish journal trung bình 1.53 ms/frame phía object và 1.40 ms/frame phía room. Đây không phải end-to-end display latency. Peak RSS theo process: object 457.25 MiB, room 650.26 MiB; không cộng hai số này thành host peak. Artifacts khoảng 17.64 MB.
- Run manifest và hash inventory: `artifacts/live_processing_runs/d1_c2_apt0_20261016_v2/run_manifest.json`; nghiệm thu chi tiết: `artifacts/live_processing_runs/d1_c2_apt0_20261016_v2/validation/d1c_acceptance.json`.

**Sau D1, gate còn pending:** renderer XYZ/orbit (được xử lý ở D2), producer failure thật trên run dài, crash resume của mapper, live capture/realtime, scene/route thứ hai, spatial scope/membership/room labels và quality validation.

### D2 — XYZ renderer trên artifacts D1 (2026-10-16)

- **Trạng thái:** triển khai viewer-only đạt trên run
  `d1_c2_apt0_20261016_v2`, 12 revisions. Không chạy lại capture,
  perception, Qwen, mapping hoặc native replay.
- Renderer dùng WebGL có sẵn trong browser, không thêm package/CDN. Object
  clouds, axes/thước mét và camera translation trajectory hiển thị ở XYZ;
  room polygons/adjacency giữ panel XY, không dựng room volumes.
- Có orbit, pan mode, zoom, fit/reset, picking theo object ID/revision/lineage.
  Trajectory lấy đến revision đang xem; cloud samples hiển thị `shown/total`
  (tối đa 300 điểm/object). JSON geometry LRU tối đa 6 revisions; renderer
  giữ một revision GPU hiện hành và dispose buffers khi rời trang.
- GUI browser thật: first/mid/last, seek ngược, play/pause, live-follow toggle,
  final postprocess terminal riêng, click object/lineage, RGB/mask/crop; fixture
  synthetic xác minh XYZ/metre, axis orientation, picking và pan/orbit/zoom.
  Synthetic event fixture phát lần lượt ba revision D1 đã lưu; browser follow
  tới revision mới, seek lịch sử vẫn đứng yên khi có event khác, rồi follow
  trở lại head. Đây không phải producer mapping run mới.
- Bản được kiểm tra:
  `artifacts/live_processing_runs/d1_c2_apt0_20261016_v2/viewer/index_d2_v9.html`.
  Manifest/hash: `viewer/d2_xyz_manifest_v9.json` (202 input, 3 output hashes;
  includes builder and fixture-template source hashes).
  14 targeted tests pass. Report chi tiết:
  [D2 XYZ viewer report](D2_XYZ_LIVE_VIEWER_REPORT.vi.md).

**Gate còn pending sau D2:** event arrival từ producer mapping mới chưa chạy lại
trong lượt viewer-only này; producer failure thật trên run dài, crash resume
mapper, live capture/realtime, scene/route thứ hai, spatial scope/membership/
room labels và quality validation vẫn pending.
Room polygons hiện không có elevation bounds; room volumes cần scope contract
riêng, không thuộc D2.

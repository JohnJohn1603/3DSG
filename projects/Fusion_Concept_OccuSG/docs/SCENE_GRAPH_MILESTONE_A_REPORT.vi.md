# Mốc A — causal correctness và integration hardening

Ngày thực hiện: 2026-10-15  
Kế hoạch đối chiếu: `SCENE_GRAPH_PROJECT_REVIEW_AND_COMPLETION_PLAN.vi.md`  
Phạm vi: chỉ mốc A. Các mốc B–E chưa triển khai.

## Kết quả

Mốc A đã được triển khai trên runner causal hybrid hiện tại và chạy lại
trên timeline 29 frame của `00802`. Artifact mới nằm tại:

`artifacts/scene_graph_timeline/m4_causal_hybrid_00802_a_v2/`

Lệnh tái lập:

```bash
python3 scripts/run_causal_hybrid_room_belief.py \
  --timeline artifacts/scene_graph_timeline/m0_m1_00802/recorder_room_1/timeline \
  --a-zip artifacts/room_belief/rb2_kaggle_diagnostics/full_a_20261015/run_7c43877ac67928df_artifacts.zip \
  --output artifacts/scene_graph_timeline/m4_causal_hybrid_00802_a_v2
```

Kết quả chạy: 29 prefix revisions, runtime khoảng 1.30 s. Không chạy
capture, detector, encoder, Qwen, native backend hoặc mapping mới.

## Đối chiếu từng finding

### P1-A — room lifecycle ledger

**Trạng thái: đã xử lý ở biên serialized timeline; còn giới hạn nội bộ được
ghi rõ.**

- Runner đọc `room_revision_files` theo frame index trong manifest, không suy
  file theo `0..N-1`.
- Mỗi revision chỉ xuất semantic room state cho `active_room_ids` của
  revision đó.
- `room_lifecycle` ghi `created_room_ids`, `retired_room_ids`,
  `active_room_ids` và `room_revision`.
- Artifact kiểm tra được: `room:1` xuất hiện ở revision đầu; `room:2` ở
  revision 3; `room:3/4/5` ở revision 5; `room:6` ở revision 6;
  `room:7` ở revision 12; `room:1` và `room:4` retired ở revision 21.
  Hai room này không còn trong semantic state cuối.

Ledger vẫn giữ catalog room vật lý của run để phục vụ schema/retraction;
catalog đó không được serialize thành state của room tương lai. Việc suy
identity giữa hai room ID khi split/merge chưa được thêm.

### P1-B — elevation/floor scope

**Trạng thái: gate an toàn đã xử lý; membership hiện vẫn unavailable.**

Runner không còn coi `floor_0`, P3 layer hoặc final snapshot tự chúng là
bằng chứng đủ để bật polygon XY. Membership chỉ mở khi có scope contract
explicit với `status=verified_single_floor_scope`, đúng một `floor_id`,
`source_refs`, prefix layers đồng nhất và provenance khớp.

Trên run thật không có contract nên manifest ghi:

```text
status: unavailable
reason: room_scope_contract_missing_for_2d_polygons
room_geometry_kind: 2D XY polygons; room nodes have no floor/layer/Z bounds
multi_floor_safety: false
```

Profile/P3/source object layers đều quan sát được `floor_0`, nhưng chỉ là
cross-check provenance; không được dùng để tuyên bố verified single-floor.
Synthetic mixed-layer test (`floor_0` + `floor_1`) cũng giữ gate đóng với
reason `prefix_scope_contains_mixed_floor_layers`.

Vì gate đóng, artifact không tạo `room_contains` và không tạo room label từ
membership. Đây là trạng thái đúng theo contract, không phải lỗi crash.

### P1-C — frame atomicity

**Trạng thái: đã xử lý.**

`RoomBeliefLedger.apply_frame(...)` nhận toàn bộ events cùng
`producer_revision`, sắp xếp deterministically, tính net category changes,
relabel mỗi room bị ảnh hưởng một lần, và rollback toàn bộ state nếu scoring
thất bại. Event order không làm đổi kết quả; event lặp/stale vẫn bị kiểm tra.

### P2-A — runner gắn với một thử nghiệm

**Trạng thái: đã xử lý trong runner hiện tại.**

- Admission đọc `obj_min_detections` từ
  `mapping/diagnostic/diagnostic_manifest.json`; run này ghi nhận `3` và
  lưu path/hash/filter rule.
- A tables được kiểm tra rectangular theo số category/room thực tế, unique
  keys và complete schema; không còn assert cứng `88` ngoài metadata shape.
- Mỗi variant có content SHA-256 thật, không dùng tên variant làm hash.
- Room revision lấy từ manifest index và geometry revision được truyền theo
  room revision thực.
- Các giả định online hypotheses, prefix admission eligibility và final
  postprocess được ghi riêng trong `stage_semantics`.

Exact-pose/view-group policy vẫn là policy baseline hiện có; lượt này không
đổi hoặc gọi các pose gần nhau là independent views.

### P2-B — test coverage

**Trạng thái: đã bổ sung một phần cốt lõi; còn test mở được liệt kê.**

Đã chạy logic thật cho lifecycle active/retire, missing scope, mixed-layer
scope, prefix không dùng future observations, A-table/admission provenance,
frame event order invariance và atomic rollback. Các test RB1 hiện có tiếp
tục bao phủ transfer/retraction, category cap, serialization và stale event.

Chưa tuyên bố hoàn tất các test chưa có trong lượt này: so sánh độc lập
prefix execution với full-run prefix trên cùng input, kiểm tra mọi dạng
frame-index không liên tục, và một test riêng cho mọi loại config/hash
mismatch của runner. Đây là phần còn lại của P2-B, không chặn kết luận
membership gate của run hiện tại.

### P2-C — active documentation status

**Trạng thái: chưa xử lý trong mốc A.**

Các trang active cũ vẫn có thể lệch trạng thái local/A-table. Báo cáo này
là status handoff của mốc A; việc hợp nhất trang active và đánh dấu
superseded là mốc tài liệu riêng, không tự mở trong lượt này.

### P2-D — end-to-end launcher

**Trạng thái: chưa xử lý; thuộc mốc D/E.**

Không tạo launcher map+route mới trong lượt này.

## Artifact và invariants

Manifest của artifact v2 ghi:

- 4 A variants; mỗi variant 11 categories × 8 room types = 88 rows;
- A content digests, source ZIP hash và timeline manifest hash;
- admission threshold/provenance;
- causality riêng cho objects/rooms/membership/room semantics;
- 29 revisions, `future_observations_used=false`;
- membership scope unavailable, không có cạnh `room_contains`;
- native object membership không được dùng cho hybrid objects.

Các output cũ, gồm `m4_causal_hybrid_00802_a_v1` và lịch sử v5, được giữ
nguyên. v2 là artifact chạy theo code hiện tại; v5 không được dùng làm bằng
chứng verified multi-floor.

## Tests đã chạy

```bash
python3 -m pytest -q -p no:cacheprovider \
  tests/test_room_belief_v1.py \
  tests/test_hybrid_timeline.py \
  tests/test_causal_hybrid_room_belief.py \
  tests/test_causal_timeline_viewer.py \
  tests/test_conceptgraphs_adapter.py \
  tests/test_unified_scene_graph.py \
  tests/test_causal_correctness_a.py \
  --basetemp /tmp/scenegraph-review-a-tests
```

Kết quả: **48 passed**. `py_compile` cũng đạt cho runner, ledger và test mới.

## Kết luận và giới hạn

Mốc A đạt ở mức causal object timeline, room lifecycle serialization, frame
transaction và provenance gates. Run hiện tại **chưa có causal room
membership hoặc room labels**, vì room polygons chỉ là 2D XY và chưa có scope
contract được khóa. Không dùng `Room_1`, `Room_2`, ... làm semantic label và
không suy room accuracy.

Bước cần duyệt tiếp theo (chưa triển khai): hoàn thiện test gaps P2-B và/hoặc
đưa một scope contract hợp lệ từ room-generation provenance trước khi bật
greedy membership. Không tuning matcher, A tables, thresholds, default hay
mở các mốc B–E trong lượt này.

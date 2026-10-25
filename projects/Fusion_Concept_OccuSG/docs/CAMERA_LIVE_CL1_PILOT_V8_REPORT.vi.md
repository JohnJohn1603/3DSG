# Báo cáo pilot camera-live CL1 v8 — 2026-10-18

## Kết luận

Một pilot thật đã chạy đúng một lần, từ state rỗng, trên route T1.1 đã khóa.
Tất cả worker hoàn tất với exit code 0 và pipeline ghi được hai unified
revisions. Kết quả này **không đạt acceptance overlap của graph**: perception
bắt đầu khi camera còn chạy, nhưng revision đầu tiên chỉ commit sau khi capture
kết thúc. GUI render trực quan vẫn pending vì phiên này không có browser surface
để điều khiển. Đây là pipeline hoàn tất nhưng chưa đạt demo graph đồng thời;
không phải lỗi timestamp hay lỗi worker.

## Run và provenance

- Pilot run ID: `camera_live_t1_1_20261018_v8`.
- Scene/route: HM3D `00802-wcojb4TFT35`,
  `smooth_t1_1_hm3d00802_turn_continue_20261018_v2`.
- Capture: native `640×480`, HFOV `90°`, intrinsics `fx=fy=320`,
  `cx=319.5`, `cy=239.5`, seed `0`.
- Config SHA-256:
  `4e02f12ed0894047721b720d49f8d1c2f8efd2535acb0b2a2334410c239e2c65`.
- C2 source:
  `artifacts/conceptgraphs_hybrid_runs/hm3d00802_main29_640_quality_v1_attempt2`;
  `c2_run_manifest.json` SHA-256
  `48e62b2f4031061d77c69fd603269e78707174e600d5ad89f7dc5a0b0a6c4753`.
- Dùng đúng một Habitat producer run, một process/session P2, một P3, một
  OpenCLIP, một ConceptGraphs session và một OccuSG session. Không retry.
- P2 ONNX tải một lần; OpenCLIP tải một lần. P3 là geometry session. Cả năm
  worker thoát với code `0`.

## Kết quả theo stage

- Habitat route preflight đạt: 8.979 s. Capture tạo 113 camera frames trong
  12.809 s wall-clock, tương ứng 11.648 s simulation; không có collision.
- Thời gian từ bootstrap đến terminal viewer state khoảng 42.224 s, quy đổi
  trong cùng host monotonic/wall-clock domain.
- Có 13 candidate/keyframe đã ghi và queue nhận đủ 13. Queue latest-wins chỉ
  admit và xử lý frame `000000` cùng `000110`; 11 keyframes pending bị
  supersede trước dispatch. Không có frame đã dispatch bị thay thế.
- Hai keyframe được P2, P3 và OpenCLIP xử lý. Mỗi keyframe có 5 P2 detections,
  5 P3 rows và 5 feature rows. P2 giữ một model load; OpenCLIP giữ một model
  load, tăng image encode từ 5 lên 10 và text encode từ 3 lên 4 nhờ cache.
- `timestamp_s` lỗi cũ **không tái hiện**. Tám response/packet P2, P3, OpenCLIP
  và joined cho hai frame giữ đúng full frame ID, `timestamp_s`, simulation
  clock domain, capture monotonic clock và source hashes.
- ConceptGraphs và OccuSG cùng nhận frame `000000`, rồi frame `000110`, với
  source hashes khớp. Có 2 object prefix revisions, 2 room prefix revisions
  và 2 unified revisions. Thời gian nhánh từ event: ConceptGraphs 368 ms và
  557 ms; OccuSG 7.135 s và 9.011 s. Pair dispatch → unified commit lần lượt
  khoảng 7.276 s và 9.127 s.
- Terminal stage được giữ riêng. Room ghi terminal flush revision 3 sau hai
  frame revisions. ConceptGraphs terminal log ghi 9 hypotheses trước filter,
  0 sau filter, 0 trước merge và 0 sau merge. Đây không phải phép đo accuracy;
  prefix vẫn lưu state hypotheses theo từng revision.
- Membership và room semantics vẫn `unavailable_elevation_scope` /
  `unavailable_membership_scope`; không có room label được suy ra.

## Đồng thời và viewer

Tất cả các giá trị dưới đây cùng host-monotonic clock domain:

- P2 bắt đầu tại `3907.244893`; camera capture kết thúc tại `3910.701816`:
  perception bắt đầu sớm hơn khoảng **3.457 s**.
- Unified revision 1 commit tại `3916.977413`, tức **6.276 s sau** capture end.
- Vì vậy acceptance “unified revision được commit khi camera còn chạy” **không
  đạt**. Route có 11.648 s simulation nhưng camera wall run chỉ 12.809 s; mapper
  room update mất 7.135 s cho frame đầu.
- D2 server/entrypoint được bootstrap trước camera; HTTP log ghi 200 cho trang,
  JS renderer, `live_index` và `live_head`. Browser API của phiên trả về không có
  app/tab, và không có bằng chứng poll/render revision từ browser khi producer
  còn chạy. **GUI acceptance pending**; HTTP 200 không được tính là GUI pass.

URL của run:

`http://127.0.0.1:8791/artifacts/live_processing_runs/camera_live_t1_1_20261018_v8/viewer/index_d2_camera_live.html?live=1`

Pilot runner đã dừng server thuộc run khi kết thúc. Để xem artifacts sau run,
khởi động server chỉ đọc trong workspace:

```bash
cd /home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG
python3 -m http.server 8791 --bind 127.0.0.1 --directory .
```

Sau đó mở URL phía trên trong Brave/Chrome. Nếu port 8791 đang bận, chọn port
khác và thay số port trong URL; không dừng server khác.

## Tài nguyên và giới hạn đo

- RAM available quan sát được: khoảng 6.3 GiB trước run, 3.8 GiB trong run và
  7.1 GiB sau run; các số này là mẫu `free -h`, không phải host peak.
- Habitat process peak RSS ghi trong producer manifest là 477,360,128 bytes.
  Peak RSS từng worker và host peak không được instrument; không suy ra tổng
  memory từ RSS camera.
- P2/P3/OpenCLIP không có timestamp kết thúc riêng trong instrumentation hiện
  hành. Các khoảng stage cụ thể ngoài thông tin trên không được ước lượng.
- Không chạy Qwen, detector ngoài P2, encoder ngoài OpenCLIP, mapping batch,
  capture khác hoặc verification retry.
- Đây là camera-live processing file-backed, không phải wall-clock realtime,
  không phải live graph overlap acceptance, và không phải semantic/quality claim.

## Vấn đề manifest được phát hiện

`run_manifest.json` được Habitat route runner ghi vào cùng đường dẫn mà
coordinator dùng. Bản được giữ nguyên có top-level `run_id` là route ID,
thiếu enclosing pilot config/source hashes; `pilot_bootstrap.json` vẫn ghi
`bootstrapping`. Không sửa đè các file gốc sau run. Để xác định đúng trạng thái
pilot, dùng sidecar
[`pilot_acceptance_manifest.json`](../artifacts/live_processing_runs/camera_live_t1_1_20261018_v8/pilot_acceptance_manifest.json).
Sidecar này ghi pilot ID, acceptance, provenance và lỗi manifest gốc. Lỗi này
cần sửa trong orchestration trước lần pilot kế tiếp; **không chạy lại pilot**.

## Artifacts và integrity

- [Run directory](../artifacts/live_processing_runs/camera_live_t1_1_20261018_v8/)
- [Acceptance manifest](../artifacts/live_processing_runs/camera_live_t1_1_20261018_v8/pilot_acceptance_manifest.json)
- [Integrity manifest](../artifacts/live_processing_runs/camera_live_t1_1_20261018_v8/pilot_integrity_manifest.json)
- Integrity snapshot: 572 file, 90,543,755 bytes; SHA-256 manifest
  `185c2cb8ef246fba9cfe1b5d1983a56d1b22df5084b619c6963b132d377c0cc4`.
- Acceptance manifest SHA-256
  `67e10ed9adb021eb19820f6710bfe97fea86e42f107a0d4609770cab945e6fef`.
- Raw `run_manifest.json` SHA-256
  `bd83c4e9b5def51090b017da29a885713665af36c5d64d82d582a59663241bc9`.
- Giữ nguyên v6/v7. Không chạy thêm producer hoặc sửa dữ liệu inference của v8.

## Trạng thái acceptance

| Gate | Kết quả |
|---|---|
| Startup/bootstrap và route preflight | Pass |
| Camera capture + keyframe publication | Pass |
| Timestamp contract P2 → P3 → OpenCLIP → mapper | Pass trên 2 keyframes đã admit |
| Hai mapper cùng frame order/source hashes | Pass trên 2 keyframes đã admit |
| Unified revisions và terminal stage riêng | 2 prefix revisions + terminal hoàn tất |
| Perception bắt đầu trước capture end | Pass |
| Unified revision trước capture end | **Không đạt** |
| Browser render trực quan trong lúc producer chạy | **Pending** |
| Membership / room labels | Unavailable theo scope gate |
| Realtime / chất lượng / accuracy | Không được tuyên bố |

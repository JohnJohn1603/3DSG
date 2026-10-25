# Capture profile 640×480 và baseline chất lượng HM3D 00802

Ngày: 2026-10-17

## Trạng thái

640×480 là **operational capture profile** cho workflow scene-run mới trên ReplicaCAD và HM3D. Đây không phải quality promotion. Không capture, perception, encoder, ConceptGraphs hay OccuSG pipeline nào được chạy khi chuẩn bị tài liệu này.

- Profile dùng chung: `native_640x480_v1`; menu chọn sẵn profile này khi nhấn Enter.
- Profile smoke chọn tường minh: `legacy_160x120_smoke_v1`.
- HFOV, sensor extrinsics, depth range/convention và detector preprocessing được kế thừa từ route. Intrinsics được tính bởi `adapters.habitat.frames.habitat_camera_intrinsics`; ở 640×480/HFOV 90°: fx=fy=320, cx=319.5, cy=239.5.
- Route JSON 160×120 cũ, completed runs và manifests bất biến không bị sửa.
- Object–room membership và room labels vẫn unavailable theo scope gate hiện hành.

## Cache và provenance

Capture manifest mới khóa profile, resolution, HFOV, intrinsics, calibration ID, sensor pose/settings và SHA-256 các file RGB/depth/valid-mask/metadata theo frame. P2 compatibility fingerprint khóa capture manifest/sensor contract, RGB hashes, detector config/checkpoint, runtime, preprocessing và vocabulary. P2 xác minh hash đầu vào; P3 và image-feature readers xác minh lại raw capture files, masks, sensor contract và RGB lineage trước khi dùng. Image features chỉ được reuse khi RGB hash, mask hash, calibration/sensor contract, checkpoint và crop preprocessing cùng khớp. Failed-run reuse chỉ chấp nhận capture/P2 có đủ fingerprint mới và cùng contract; cache legacy thiếu provenance bị từ chối để tránh trộn dữ liệu.

Annotation không được ghép chỉ bằng observation ID. Cần khớp capture manifest, P2 index và image/mask hashes; nếu nguồn ảnh khác thì annotation cũ không tự áp dụng.

## Visual triage 18 detections đã có

Đã đọc paired full-frame overlays và crop panels đã lưu cho 3 frame 640×480. Đây là **assistant visual triage**, không phải human-confirmed identity/class/mask-purity review. JSON ghi đầy đủ 18 observation IDs, confidence, bbox, mask hash, RGB hash và ghi chú; identity/class/purity vẫn `not_adjudicated`.

- Có 3 dự đoán `suitcase` chồng trên cùng vật tối trên bàn ở frame 1: duplicate/part-mask risk cần người dùng phân xử.
- Frame 0 det-0003 dự đoán `chair` nhưng mask hiển thị phần lớn trên mặt bàn.
- Frame 2 có sofa và ottoman tách vùng; nhãn/physical identity của ottoman chưa kết luận.
- Các chair, plant, refrigerator, dining table và TV khác có mask nhìn tương đối phù hợp hoặc còn bị che khuất; không suy ra identity hay độ tinh khiết chính xác từ panel.

Chi tiết: [visual triage JSON](../artifacts/experiments/hm3d00802_paired_resolution_v1/candidate_640/visual_triage_v1/assistant_visual_triage.json), [báo cáo review](../artifacts/experiments/hm3d00802_paired_resolution_v1/candidate_640/visual_triage_v1/report.vi.md), [manifest/hash](../artifacts/experiments/hm3d00802_paired_resolution_v1/candidate_640/visual_triage_v1/manifest.json). Nguồn là capture SHA-256 `e64d3df18eed870d4c39184f74d5cc8a7b0a0f31c335fd2400b8d3a016277007` và P2 index SHA-256 `20624d8d447a6afe64cbed61f55ecfdfbdc24c402a1564b8e74fd00d3ed4fdf9`.

## Baseline C2 HM3D 00802 — được duyệt một lần

### Input đã khóa

- Dataset/scene: HM3D minival v0.2, `00802-wcojb4TFT35`.
- Route: `main`, 29 poses, sequence `hm3d-v02-00802-main-route-01`; route density không đổi. Route source vẫn khai báo 160×120, còn profile hiệu lực nâng capture native lên 640×480.
- HFOV 90°, seed 20261014, model/checkpoint, detector input 640×640, preprocessing, confidence/NMS, matcher, admission và thresholds giữ nguyên.
- Run mới dùng state/cache rỗng; không reuse masks/depth/features/annotations 160×120. Không so trực tiếp annotation theo ID nếu image-source hash khác.
- Membership `unavailable_elevation_scope`; room semantics `unavailable_membership_scope`.

### Lệnh sau khi duyệt ngân sách

Config đã sửa resolution policy mâu thuẫn và proposal hash đã cập nhật. Route nguồn vẫn 160×120; profile mới buộc native capture 640×480. Runner xác minh manifest, metadata frame đầu, K, cùng shape RGB/depth thực tế trước khi cho P2 chạy. Không sửa route/config/run lịch sử.

Run ID ban đầu đã có manifest `failed` ở capture trước frame đầu do Habitat Python 3.9 import phải module contracts dùng `dataclass(slots=True)` (chỉ có từ Python 3.10). Đây là lỗi tương thích, không phải thiếu tài nguyên. Đường calibration đã được tách thành kernel nhẹ dùng chung; smoke import bằng Habitat Python 3.9 đạt. Run thất bại được giữ nguyên; lượt chạy sau dùng ID mới và được tính riêng.

`artifacts/quality_baselines/hm3d00802_main29_640_v1/effective_c2_config.json`

Manifest config: `artifacts/quality_baselines/hm3d00802_main29_640_v1/proposal_manifest.json`.

Đề xuất chạy **một C2 duy nhất**:

```bash
./run_scene.sh fresh-replica \
  --config artifacts/quality_baselines/hm3d00802_main29_640_v1/effective_c2_config.json \
  --run-id hm3d00802_main29_640_quality_v1_attempt2 \
  --overall-timeout-s 1000
```

Sau khi C2 completed, mở viewer bằng:

```bash
./run_scene.sh open hm3d00802_main29_640_quality_v1 hybrid
```

C2 đủ để đo object hypotheses, filter/postprocess, lifting, room replay và unified object graph. Chưa đề xuất D1 vì D1 sẽ chạy thêm một lượt object mapping mà không cần để lập baseline object quality; simultaneous viewer gate là mục tiêu khác.

### Stage plan và runtime/bộ nhớ

Mỗi stage sẽ được ghi riêng: preflight; capture; P2; P3 mask/depth→3D lifting + clustering; OccuSG room processing; image features; text features; ConceptGraphs mapping; unified graph/viewer. Runner hiện lấy elapsed time và đã bổ sung sampled peak RSS của process tree từng stage (250 ms cadence; không phải host-wide peak). Preflight RAM/disk gate hiện có vẫn áp dụng.

Bằng chứng runtime đã đo:

- Hai C2 run 29-frame 160×120 trên route main hoàn tất trong khoảng 267–283 giây tổng stage. OccuSG mất 167–177 giây; P3 1.1–1.8 giây, RSS P3 ghi nhận 62.8 MiB trong một run. Đây chỉ là baseline thời gian thấp-resolution, không dùng để cam kết 640×480.
- Ba frame 640×480 trước đây: capture 19.66 giây, peak RSS 1.48 GiB; P2 3.20 giây, peak RSS 194 MiB. Chỉ dùng để ước lượng capture/P2; không dùng thay cho runtime lifting/mapping.
- 29-frame 640×480 chưa chạy nên P3, OccuSG và ConceptGraphs runtime/RSS chưa biết. Ước lượng tổng sơ bộ 10–30 phút, độ tin cậy thấp do pixel/point volume tăng và số P2 proposals có thể khác.

Ngân sách được duyệt tối đa **60 phút** cho đúng một C2, gồm preflight và các stage; runner dừng process-group stage nếu chạm deadline. Không retry. Số lần thực thi dự kiến: một preflight, một lần cho mỗi stage capture/P2/P3/OccuSG/image features/text features/ConceptGraphs, một lần capture validation gate và một lần unified graph/viewer. Không D1, không scene khác.

Output quality report sẽ giữ riêng hypotheses trước filter, final retained objects, reason/provenance filter/merge, observation lineage, raw-class/evidence status và graph output hashes. Identity/retention metrics chỉ được chấm khi có annotation khớp source-image hashes; nếu chưa có thì ghi unavailable thay vì nối annotation cũ theo IDs.

## Tests và gate

Đã chạy targeted tests: `22 passed`. Các test kiểm tra profile mặc định/explicit legacy, không sửa route nguồn, adapter intrinsics, fingerprint đổi khi resolution/sensor/RGB/preprocessing đổi, cache RGB-depth-mask provenance, menu/CLI cùng resolver và stage RSS sampler. `py_compile` và `./run_scene.sh scene-run --help` cũng đạt. Không có producer/run thử nào được thực thi.

Sau baseline, báo riêng object identity/retention, semantic evidence/conflict/unsupported và runtime; không coi object count là accuracy. Real-data room membership/labels vẫn blocked cho đến khi spatial-scope gate được giải quyết bằng bằng chứng phù hợp.

### Kết quả thực chạy

Baseline C2 hoàn tất tại `hm3d00802_main29_640_quality_v1_attempt2`; một attempt trước bị lỗi Python 3.9/3.10 trước frame đầu và được giữ lại. Hash, calibration thực tế, per-frame detections, lineage, stage runtime/RSS, kết quả 40→6 và trạng thái viewer được ghi trong [execution report](../artifacts/quality_baselines/hm3d00802_main29_640_v1/c2_execution_report.vi.md) và [proposal manifest](../artifacts/quality_baselines/hm3d00802_main29_640_v1/proposal_manifest.json). Pipeline completed, nhưng GUI visual check chưa được thực hiện do browser URL policy; không gọi đây là GUI pass. C2 manifest xác nhận 1,653 output file hashes, tất cả được kiểm tra hậu kỳ. Room membership/semantics vẫn unavailable.

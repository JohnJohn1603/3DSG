# So sánh geometry fusion trên assignments có sẵn

Lệnh `fuse-objects` tạo hình học tích lũy từ assignments đã được một replay quyết định. Nó không chạy lại association, không cập nhật graph/room scores và không tự chứng minh hình học hay association chính xác hơn.

## Chạy source replay hiện có

Source replay `u5m9_20261011T035542.851355Z_b39b87` chỉ chứa graph/replay outputs. Launcher đọc `source_cache_run` trong manifest và xác minh hash capture, P2, P3 cùng P3 manifest trước khi dùng cache. Nó đọc lineage source nguyên bản; không tạo quyết định từ first/last endpoints.

```bash
./run_scene.sh fuse-objects u5m9_20261011T035542.851355Z_b39b87 --check
./run_scene.sh fuse-objects u5m9_20261011T035542.851355Z_b39b87
```

Source result đã tạo trong lần kiểm chứng:

- Fusion ID: `fusion_20261011T150649.478186Z_f660e6`
- 45 observations được đưa vào geometry; 23 tracks; 14 tracks có nhiều observations.
- Source `same_class_match` và `same_pose_suppressed` giữ `object_id` source và được tính là assigned evidence trong sidecar. `same_pose_suppressed` không bị đổi thành dedup alias.
- Selected-point sidecar được tái sử dụng; không chạy capture, detector, P3 hoặc native replay.

## So sánh với candidate

Candidate đã có sẵn:

- `fusion_20261011T145550.870662Z_b1991d` — profile `geometry-only-dedup-v2`.

Tạo comparison mới và mở trang:

```bash
./run_scene.sh compare-object-fusions fusion_20261011T150649.478186Z_f660e6 fusion_20261011T145550.870662Z_b1991d
./run_scene.sh open fusion_compare_20261011T151013.750114Z_91862d fusion-comparison
```

Comparison ID được tạo mới mỗi lần chạy. Trang hiện tại: `artifacts/object_fusions/comparisons/fusion_compare_20261011T151013.750114Z_91862d/index.html`.

## Đọc kết quả này

- Capture/P2/P3 hashes khớp; có cùng 45 observation IDs trong membership comparison.
- Source có 23 tracks; candidate có 26 track groups. Có 20 thành phần liên kết theo observation membership; 6 thành phần có partition khác nhau: 3 source-to-many-candidate và 1 many-source-to-candidate trong số đó.
- Candidate có 41 observations fused vào geometry và 4 alias cùng-frame được gắn vào membership canonical nhưng không góp point/voxel evidence. Source fused đủ 45; một observation `same_pose_suppressed` vẫn là assigned evidence theo source contract.
- Trang hiển thị RGB/mask evidence và bốn cloud panels cho first/fused source/candidate. Bốn panel trong mỗi nhóm dùng cùng giới hạn trục world; points/voxels chỉ là số lượng, không phải geometry accuracy.
- Đây là so sánh output fixed-assignment, không phải kết quả accuracy hoặc tuyên bố candidate tốt hơn. GT/evaluator metrics không được chạy trong bước này.


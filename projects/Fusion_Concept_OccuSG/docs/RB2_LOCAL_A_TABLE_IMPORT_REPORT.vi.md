# RB2 local import — bảng A và static diagnostic

Ngày: 2026-10-15

## Phạm vi

Đã dùng artifact Qwen RB2 đã tải về, không chạy lại model, perception,
mapping hoặc capture. Kaggle session đã được dừng ở mức UI: Draft Session
hiển thị `off (run a cell to start)` và `Session stopped.` Không mở session mới.

Artifact nguồn:

`artifacts/room_belief/rb2_kaggle_diagnostics/full_a_20261015/run_7c43877ac67928df_artifacts.zip`

SHA-256: `0e30cc12058ec22372fe8b2df49279ec6a917027bef74182f8831374a7418997`.

## Import và kiểm tra contract

Lệnh tái lập:

```bash
python3 scripts/import_rb2_a_tables.py \
  --archive artifacts/room_belief/rb2_kaggle_diagnostics/full_a_20261015/run_7c43877ac67928df_artifacts.zip \
  --readiness artifacts/room_belief/readiness/u6_00808_hybrid_v3_static_v4/readiness.json \
  --output artifacts/room_belief/rb2_local_import_20261015
```

Output chính:

- Bốn bảng, mỗi bảng đủ 11 categories × 8 room types (88 cặp).
- Giá trị hữu hạn, ontology/canonical categories, `T=1.0`, epsilon và
  provenance model/scoring đều hợp lệ.
- `max_token_aggregation_delta = 8.34e-7`; neutral subtraction,
  normalization và A log-ratio đều khớp contract (delta 0).
- Prompt display strings là các canonical strings đã khóa (`bed`, `sofa`,
  `dining_table`, …, `tv`).
- Import runtime không phụ thuộc Qwen hoặc Transformers.

Manifest, quality JSON, heatmap SVG, HTML report và static diagnostic nằm tại:

`artifacts/room_belief/rb2_local_import_20261015/`

## Phân bố room thắng

Đây là top-1 theo bảng, không phải nhãn room đã commit:

- `mean_no_neutral`: living_room 6, kitchen 3, bedroom 1, bathroom 1.
- `mean_neutral`: kitchen 8, bathroom 3.
- `sum_no_neutral`: kitchen 5, living_room 2, bedroom 3, bathroom 1.
- `sum_neutral`: kitchen 6, bathroom 3, living_room 1, dining_room 1.

Vì vậy dữ liệu thực tế là kitchen đứng đầu 8/11 category trong
`mean_neutral` (không phải 9/11). Các hàng cần theo dõi gồm:

- `bed → bathroom` (margin khoảng 0.0137; kitchen đứng thứ hai),
- `sofa → kitchen` (margin khoảng 0.0386),
- `tv → kitchen` (margin khoảng 0.1943).

Đối chiếu raw token cho thấy đây không phải lỗi ghép room ID hay công thức:
mean/sum lấy đúng token log-probs, neutral được trừ đúng room, sau đó
normalization và log-ratio A khớp. Mẫu kitchen-heavy là tác động hợp lệ
nhưng chưa hiệu chuẩn của neutral correction; các normalized scores không
được diễn giải là xác suất. Không chọn biến thể thắng và không sửa bảng.

## Static diagnostic trên readiness cache

Readiness cache: `u6_00808_hybrid_v3_static_v4`, một room đã kiểm tra.
Canonicalization dùng `couch → sofa` và `dining table → dining_table`.

- 9 hypotheses được đọc; 2 retained, 7 filtered.
- Chỉ một hypothesis retained, semantic ổn định (`sofa`), đủ điều kiện
  category cho room.
- Hypothesis retained nhưng semantic conflicted bị loại khỏi evidence;
  filtered hypotheses không được dùng làm room evidence.
- Vì chỉ có 1 eligible category và policy yêu cầu tối thiểu 2,
  room giữ `abstain` với lý do `insufficient_support`, không hạ ngưỡng.
- Candidate top-1 từ bảng chỉ là diagnostic; không commit label.

Đây là static diagnostic trên dữ liệu đã có, không phải causal online
integration và không phải room-label accuracy benchmark.

## Tests

```bash
python3 -m pytest -q tests/test_rb2_a_table_import.py tests/test_qwen_room_scorer.py tests/test_room_belief_readiness.py
```

Kết quả: `16 passed, 1 skipped`. Test bị skip là import torch cho Qwen
scorer trong môi trường local model-free; không ảnh hưởng các kiểm tra
import/formula/static diagnostic.

## Trạng thái

RB2 đã hoàn tất về mặt kỹ thuật ở bước tạo/import bảng A. Quality gate,
calibration/selection giữa bốn biến thể và causal online room labeling vẫn
đang pending. Không thay matcher, baseline, default launcher, room geometry
hoặc room semantic scores.

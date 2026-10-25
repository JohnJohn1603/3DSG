# Trạng thái U3 — association diagnostic

Ngày: 07-10-2026. **U3 diagnostic hoàn tất; chưa promote và dừng trước U4.** Không chạy lại YOLO/P3/native mapping; không sửa `ObjectAssociationManager`, graph baseline, class hay room scores.

## Input đã khóa

- Chọn 47 active records theo `preprocessing_fingerprint` trong U2 `extraction_report.json`; loại 94 manifest rows lịch sử. Input index bất biến và SHA-256: [`u2_input_index.json`](../artifacts/audits/u3_association/u2_input_index.json), [`u2_input_index.sha256`](../artifacts/audits/u3_association/u2_input_index.sha256). Index khóa observation/detection ID, crop pixel SHA + file SHA, mask/RGB SHA, feature SHA, cache key, model/preprocessing fingerprints.
- U2 encoder/crop/preprocessing không đổi. Không cài dependency hay tải checkpoint mới. Cache chỉ được đọc; prediction backend không nhận evaluator GT, semantic GT, room label hoặc baseline track ID.
- Trong cùng frame có **2 mask trùng/gần trùng** (mask IoU ≥0.98); chúng được loại trước cả ba mode để không cộng thành evidence riêng. Còn 45 observations duy nhất.
- U1 kết luận không đổi: exact point-set parity toàn bộ cache cũ **chưa chứng minh**; chỉ có parity trên fixture nhỏ.

## Backend và nguồn

Ba mode nhận cùng observations, theo thứ tự frame/detection cố định:

1. **Source:** `ObjectAssociationManager` nguyên trạng, effective config `same_class_radius=0.75 m`, `same_pose_suppression_cap=0.15 m`; state rỗng ở đầu local diagnostic.
2. **Geometry-only:** spatial/layer gating, AABB 3D IoU, center radius và extent consistency; greedy one-to-one.
3. **Geometry + appearance:** cùng spatial/geometry gates trước CLIP cosine; chỉ ghép khi có 3D overlap và cosine qua gate. Khoảng cách/cosine không tự quyết định. Ties gần nhau (`<0.05`) defer.

Threshold/config đã khóa trước khi xem hậu kiểm ở [`association_config.json`](../artifacts/audits/u3_association/association_config.json). Candidate scores, gate failures và decisions: [`candidate_ledger.jsonl`](../artifacts/audits/u3_association/candidate_ledger.jsonl), [`decisions.jsonl`](../artifacts/audits/u3_association/decisions.jsonl). Đây là backend thử nghiệm độc lập, chưa nối graph.

ConceptGraphs pinned main cung cấp cảm hứng cho 3D spatial similarity, CLIP visual similarity và feature aggregation. U3 adaptation là AABB-based gating, one-to-one assignment, tie defer, same-frame duplicate-mask filter và ledger đầy đủ; không tuyên bố source parity. Không port hoặc sửa source manager.

## So sánh dataset 224–230

Pairwise metrics tính trên 36 detections có evaluator match và khác frame; có 9 evaluator semantic instances. Deferred detections làm giảm recall. Fragmentation chỉ đếm evaluator IDs gắn vào nhiều predicted tracks; deferred không được giả như track/fragment. Mọi nhãn evaluator chỉ được join **sau khi prediction đã đóng băng**.

| Mode | Pair precision | Pair recall | TP / FP / FN | False-merge tracks | Fragmented evaluator IDs / excess tracks |
|---|---:|---:|---:|---:|---:|
| Source | 0.702 | 0.766 | 59 / 25 / 18 | 1 | 2 / 3 |
| Geometry-only | 1.000 | 0.740 | 57 / 0 / 20 | 0 | 0 / 0 |
| Geometry + appearance | 1.000 | 0.740 | 57 / 0 / 20 | 0 | 0 / 0 |

Geometry-only và geometry+appearance cho **cùng assignment** trên 45 observations: appearance không tạo gain đo được ở đoạn này. Năm decisions được defer ở mỗi mode. Đây là local diagnostic từ state rỗng, không tương đương run thật đã có prefix state.

Evaluator identity là semantic instance annotation, không bảo đảm physical identity. Các số fragmentation/false merge vì vậy được hiểu theo evaluator lineage đủ match, không khẳng định danh tính vật thể vật lý; các cặp annotation có thể split/merge khác physical object cần review.

### Bed/table và vật lân cận

- Với evaluator ID `417` (GT category `table`), detector đổi `dining_table` → `bed` ở frame 230 được geometry-only và combined association nối với các observation trước của cùng evaluator instance. Đây chỉ là identity association; U3 không sửa hoặc xác nhận class `bed`.
- Detection `dining_table` khác tại frame 230, evaluator ID `357`, không bị nhập vào track đó. Không có false merge với evaluator IDs khác trong hai mode thử nghiệm.
- Đây chưa phải xác nhận physical identity; evaluator instance `417` là cơ sở hậu kiểm, còn crop/case phải được người review. Xem [`bed_table_case_posthoc.csv`](../artifacts/audits/u3_association/bed_table_case_posthoc.csv) và [`case_review_posthoc.csv`](../artifacts/audits/u3_association/case_review_posthoc.csv).

Contact sheets có 14 cặp cross-class/same evaluator instance, 39 cặp same-class/different evaluator instances và 29 cặp có false merge ở ít nhất một mode. “Likely false merge” là hậu kiểm evaluator-level, không phải physical-identity truth. Các sheet: [`cross-class`](../artifacts/audits/u3_association/contact_cross_class_same_evaluator_instance_01.png), [`same-class different instance`](../artifacts/audits/u3_association/contact_same_class_different_evaluator_instance_01.png), [`potential false merge`](../artifacts/audits/u3_association/contact_likely_false_merge_posthoc_01.png).

## Synthetic fixtures

[`fixture_matrix.json`](../artifacts/audits/u3_association/fixture_matrix.json) và tests bao phủ:

- Class flip trên một vật: cả geometry modes match; source dùng same-pose suppression.
- Hai vật cùng class ở gần: source nhập do radius; custom geometry modes giữ riêng khi không có box overlap.
- Hai vật khác class gần/box overlap: geometry-only nhập, combined appearance tách vật có vector khác nhau.
- Partial view còn overlap: cả custom modes match.
- Duplicate masks cùng frame: common prefilter chỉ đưa một observation vào cả mode.
- Hai candidate đồng hạng: custom modes defer; source nearest-class strategy ghép theo thứ tự node.

Fixture synthetic kiểm tra hành vi, không phải dataset evidence.

## Runtime, holdout và giới hạn

- Lệnh U3 diagnostic hoàn chỉnh chạy trong khoảng **2.4 s elapsed**, peak RSS **~80 MiB** (cache feature/mask load, association, hậu kiểm và contact sheet; không inference model). Association-only nhanh hơn tổng này; không ghi claim realtime.
- U2 benchmark 0.099 s/crop sau warm-up, cold model/checkpoint setup 2.04 s, peak RSS 1.495 GiB. Prefix tới frame 230 có 583 accepted observations trong lineage hiện có: ước tính tối thiểu ~60 s cho load+inference, gần **65–75 s** gồm crop/preprocess/ghi cache, cùng khoảng 1.5 GiB peak RAM; không tính full prefix state replay và I/O scene. Chưa mở rộng extraction.
- Holdout được khóa riêng gồm 39 saved detections ở frames 231–238, [`holdout_outside_224_230.json`](../artifacts/audits/u3_association/holdout_outside_224_230.json). Chưa có appearance features, chưa chạy/chấm holdout. Không promote trước khi holdout được trích features theo đúng U2 lock và chấm trên state/protocol đã định trước.
- Full project suite, end-to-end workspace runtime, replay prefix và holdout chưa chạy. U3 không chứng minh tương đương stateful real run.

## Tests và tái lập

- `18 passed`: U3 fixtures + source M6 regression tests.
- `py_compile` cho runner/backend pass; fixture matrix và 47-record experiment chạy pass.
- Không chạy full suite.

```bash
./.venv-appearance/bin/python scripts/run_u3_association_experiment.py
./.venv-appearance/bin/python scripts/run_u3_fixture_matrix.py
/usr/bin/pytest -q tests/test_u3_experimental_association.py tests/test_m6_object_association.py
```

## Bước tiếp theo

Review contact sheets và bed/table cases. Trước khi promotion, trích appearance cho holdout đã khóa, chuẩn bị prefix state/features nếu mục tiêu là equivalent real run, rồi đánh giá theo protocol có evaluator lineage đủ tin cậy. Chỉ sau đó mới cân nhắc U4 class confirmation; lượt này chưa thay đổi class, association trong runtime, graph hoặc room score.

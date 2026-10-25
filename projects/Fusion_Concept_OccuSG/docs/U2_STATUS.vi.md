# Trạng thái U2 — appearance extraction

Ngày: 07-10-2026. **U2 hoàn tất; dừng trước U3.** Không sửa môi trường Habitat/perception/baseline, không chạy lại detector/P3/native mapping, không đổi association/class/room scores.

## U1 point-set gate — kết luận giữ nguyên

Cache P3 cũ không lưu selected XYZ hay pixel indices, nên không thể khẳng định exact point-set parity với toàn bộ cache cũ. Trên 47 sidecar observations của frames 224–230, selected point/pixel counts khớp `largest_cluster_points` trong ledger 47/47; center sai số tối đa `2.38e-7 m`.

Hai clustering backends được so trên fixture 512 điểm liên tiếp từ candidate cloud thật frame 230. Với `eps=0.02 m`, cKDTree và all-pairs tạo component index sets giống hệt nhau: 16 components, largest 123 điểm. Đây là fixture parity, không chứng minh parity trên toàn bộ cloud. Chi tiết interpreter/config/input hashes: [`u1_pointset_backend_parity.json`](../artifacts/audits/dining_table_bed_224_230/u1_pointset_backend_parity.json); script tái lập: [`verify_u1_pointset_parity.py`](../scripts/verify_u1_pointset_parity.py).

## Dependency và checkpoint lock

- Tạo `.venv-appearance` trong workspace với CPython 3.12.3. Các pin đã cài đúng: `torch==2.6.0+cpu`, `torchvision==0.21.0+cpu`, `open_clip_torch==3.2.0`; `pip check` pass. Không cài vào Habitat, perception hoặc baseline.
- Lock có 31 package runtime/transitive với SHA-256: [`appearance_dependencies.lock.json`](../locks/appearance_dependencies.lock.json), [`appearance_requirements.lock.txt`](../locks/appearance_requirements.lock.txt). `pip==24.0` là tool bootstrap từ `venv` và được ghi riêng trong JSON lock.
- Tải đúng file `open_clip_model.safetensors` từ `timm/vit_base_patch32_clip_224.openai`, revision `a6f597a30f7b82c51704746581f9a4e41421e878`; không tải repository snapshot. Size `605,143,284` bytes; SHA-256 `e6d1bd7789aa45192b3bf90570a789b478bae1b74ebcce7eddd908e83a2b7c31`. Source URL, ETag và thời điểm tải nằm trong `checkpoint_metadata.json`.
- Weight load `strict=True` pass. Checkpoint/model: OpenCLIP ViT-B-32, 512 chiều, CPU-only.

Direct pins, package hashes và checkpoint SHA được lưu tại [`appearance_encoder_proposal.json`](../locks/appearance_encoder_proposal.json). Dung lượng dùng sau cài: env 997 MB, checkpoint 578 MiB trên đĩa, wheel cache 171 MB; workspace còn 320 GiB trống.

## Preprocessing thực tế và provenance

Crop tái hiện helper ConceptGraphs đã audit: bbox float, đệm tối đa 20 px mỗi cạnh, clip ở biên ảnh, truyền padded float box vào `PIL.Image.crop` (PIL tự làm tròn pixel edge). Crop dùng RGB; mask chỉ được băm/ghi provenance, không black-out pixels. Transform runtime thực tế từ `open_clip.create_model_and_transforms('ViT-B-32', pretrained=None)`:

```text
Resize(size=224, interpolation=bicubic, antialias=True)
CenterCrop(224)
RGB conversion → ToTensor
Normalize(mean=(0.48145466, 0.4578275, 0.40821073),
          std=(0.26862954, 0.26130258, 0.27577711))
```

Preprocessing fingerprint ổn định: `dac4e88760f9f3b423eb8cbc104a79fd5e981a1458b11ba20c8b74fa3788587b`. Cache key gồm RGB file SHA, mask SHA, bbox/crop policy, crop pixel SHA, checkpoint SHA/revision và preprocessing fingerprint. Mỗi feature NPZ chỉ có `image_feature float32[512]`, đọc bằng `allow_pickle=False`; thiếu feature không được thay bằng vector zero.

Phần lấy từ ConceptGraphs pinned main là bbox pad 20 px và normalized CLIP image embedding. Adaptation là encoder ViT-B-32 thay vì ViT-H-14, CPU env riêng, reused RGB detections và hash-keyed NPZ/JSONL cache. Không import stack SAM/GroundingDINO/SLAM.

## Benchmark, feature và similarity

Benchmark sample 16 crop được chọn cách đều theo observation ID đã khóa, không dùng class/GT/track để chọn sample. Kết quả [`benchmark_report.json`](../artifacts/audits/dining_table_bed_224_230/appearance/benchmark_report.json):

- Cold process start đến report: 8.04 s; import/model/checkpoint setup: 2.04 s; first inference: 0.153 s.
- Sau warm-up: 16 crops / 1.585 s, trung bình 0.0991 s/crop (10.10 crops/s). Peak RSS 1,494.8 MiB; host trước benchmark có khoảng 7.7 GiB RAM available.
- 16/16 vectors finite, shape 512, float32; L2 norm sai khác không quá `1e-5`. Cùng crop lặp lại đạt `allclose(atol=1e-5, rtol=1e-5)` và cosine `1.0` (ngưỡng ghi trước `>=0.999999`). Vector benchmark đầu tiên giống chính xác qua process riêng với vector trong cache extraction.

Sau benchmark, đã extract 47/47 detection crops đã lưu từ frames 224–230; không chạy lại YOLO, P3, capture hay mapping. Extraction mất khoảng 19.6 s wall time, peak RSS 1,493 MiB. Cache/crops/manifest ở [`appearance/`](../artifacts/audits/dining_table_bed_224_230/appearance/).

[`similarity_all_pairs.csv`](../artifacts/audits/dining_table_bed_224_230/appearance/similarity_all_pairs.csv) có đủ 1,081 cặp, mỗi cặp chứa detection IDs, crop refs/hashes, cosine, detector class và M6 ID/decision chỉ để review. Các pair relation theo metadata review: 40 same-class/different-M6-object, 104 same-class/same-M6-object, 937 different-class/different-M6-object. Không có GT trong feature manifest/NPZ; M6 lineage chỉ được join sau inference để review.

Để xem cả same-class/different physical instances và cross-class/same physical instance, [`similarity_pair_review_posthoc_gt.csv`](../artifacts/audits/dining_table_bed_224_230/appearance/similarity_pair_review_posthoc_gt.csv) gắn evaluator strata **sau khi đã cố định toàn bộ 1,081 cặp**; nó giữ nguyên tất cả cặp. 20 cặp different detector classes được evaluator gán cùng GT instance; 21 cặp same detector class, khác M6 object và khác GT instance; 4 cặp same class nhưng M6 object khác lại cùng GT instance. Ngoài ra có 20 cặp same class/same M6 object nhưng evaluator IDs khác. Các con số chỉ là strata để review; không suy ra lỗi identity từ riêng cosine. Hậu kiểm này không được dùng tạo feature, cache key, lọc pair hay chọn match. Cosine chỉ là độ giống appearance; không gán identity từ cosine.

Một lần kiểm tra cache ban đầu phát hiện transform `repr` chứa memory address không ổn định; đã chuẩn hóa trước khi chốt fingerprint. JSONL là append-only nên còn 94 dòng lịch sử với fingerprint tạm ban đầu và 47 dòng active với fingerprint ổn định. Validator chỉ nhận 47 active rows; các feature/file lịch sử được giữ để audit, không được dùng trong similarity hiện tại.

## Kiểm tra và lệnh đã chạy

- `pip check`: pass.
- Benchmark 16 crops: pass, strict checkpoint load pass.
- Extraction: 47 features, mọi vector finite/normalized, 1,081 cosine tính từ cached vectors.
- `validate_u2_appearance_artifacts.py`: pass; kiểm hash ảnh/crop/feature, schema NPZ, norm, đủ pair, cosine tái tính sai số 0, và cross-process repeatability.
- `export_u2_posthoc_pair_review.py`: pass; giữ đủ 1,081 cặp và chỉ thêm evaluator strata hậu nghiệm.
- Không chạy full project suite hoặc end-to-end runtime; không làm U3.

```bash
./.venv-appearance/bin/python -m pip check
./.venv-appearance/bin/python -B scripts/extract_u2_appearance.py benchmark
./.venv-appearance/bin/python -B scripts/extract_u2_appearance.py extract
PYTHONDONTWRITEBYTECODE=1 ./.venv-appearance/bin/python -B scripts/validate_u2_appearance_artifacts.py
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/export_u2_posthoc_pair_review.py
```

## Bước tiếp theo

Review toàn bộ pair CSV/crops và kết quả hậu kiểm. Sau khi review, có thể giao milestone U3 riêng cho association/fusion; U2 không tự thay source association hoặc bất kỳ room/class score nào.

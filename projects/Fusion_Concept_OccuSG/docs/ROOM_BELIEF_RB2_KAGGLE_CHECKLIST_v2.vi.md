# RB2 Kaggle checklist — Qwen3-4B-Base trên một T4

Notebook dùng cho lượt đã duyệt: `notebooks/room_belief_qwen3_rb2_v2.ipynb`.
Gói input mới: `artifacts/room_belief/room_belief_qwen_rb2_inputs_v2.zip`.

## Trước khi chạy

1. Đưa riêng ZIP input v2 lên Kaggle thành **private Dataset**. Gói chỉ gồm ontology, 11 category strings, contract, lock và scorer. Không đưa capture HM3D, room snapshot, cache, GT hay annotations lên.
2. Dùng **private Notebook**, gắn Dataset đó. Giữ output private. Bật internet để cell D lấy checkpoint chính thức đã pin; tắt internet lại sau cell D nếu muốn.
3. Chọn GPU T4. Notebook chỉ dùng GPU 0, không chia sang GPU 1. Phải còn ít nhất 12 GiB VRAM, 12 GiB RAM khả dụng và 10 GiB disk trống.
4. Chạy từng cell theo thứ tự A → H; không bỏ qua gate hoặc đổi model/revision/dtype/scoring.

## Các cell thực tế

| Cell | Hành động | Tác động |
|---|---|---|
| A | Đọc Python/Torch/CUDA, GPU, VRAM, RAM, disk và dependency versions | Chỉ kiểm tra |
| B | Kiểm tra hash gói input và đọc ontology/category/contract/lock | Chỉ đọc |
| C | Cài bốn dependency pin; import và `pip check` | Cài package; dừng nếu lỗi |
| D | Tải Qwen3-4B-Base cùng tokenizer tại revision `906bfd4b4dc7f14ee4320094d8b41684abff8539` | Tải model qua Hugging Face |
| E | Kiểm tra Qwen3 config và 96 ranh giới tokenizer (11 categories + neutral × 8 room labels) | Chỉ đọc checkpoint đã tải |
| F | Load checkpoint nguồn BF16 thành runtime FP16 trên T4 GPU 0 | Nạp model; không quantize/offload |
| G | Smoke hai category, toàn bộ room strings và neutral; kiểm tra finite, lặp lại và batch-vs-single | Inference scoring |
| H | Nếu G pass, tạo đủ A table 11 × 8 × 4 biến thể | Inference scoring |

Scoring dùng continuation likelihood với label-token-only loss, causal shift, leading space theo ontology, không thêm BOS, không chấm EOS; right padding có attention mask. Log-softmax và cộng điểm chạy FP32. Smoke tolerance là `atol=0.002`, `rtol=0.0001`; đây là kiểm tra lặp số, không phải ngưỡng chất lượng.

Checkpoint nguồn vẫn là BF16; FP16 chỉ là runtime cast đã được duyệt. Không có upstream SHA manifest, vì vậy hash file sau download là local integrity, không phải xác thực nguồn độc lập.

## Kết quả cần tải về

Lấy toàn bộ `/kaggle/working/room_belief_qwen_rb2_output_v2/` về local, gồm thư mục `smoke/` và `full_a/`: `manifest.json`, `raw_token_scores.json`, `a_table.json`, `a_table.csv`, `category_room_scores_by_variant.json` và hashes output. Gửi thêm output A–F: probe, versions/`pip check`, revision/tokenizer boundaries, RAM/VRAM và smoke checks.

`mean_neutral` là candidate chính để thử, chưa được chứng minh thắng. Bảng chỉ bao phủ 11 class strings đã khóa; không đại diện toàn bộ vocabulary detector hoặc open-vocabulary. A và normalized compatibility là điểm LM chưa calibration, không phải xác suất room label đúng.

Sau khi có output, bước local sẽ xác minh manifest/hash với ontology/contract, rồi chạy static readiness trên cache đã có. Lượt đó không dùng GT, không chạy perception/mapping mới, và không tuyên bố room accuracy nếu chưa có room GT được review.

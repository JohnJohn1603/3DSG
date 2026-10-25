# RB2 Qwen notebook — checklist thao tác

Checkpoint này chỉ chuẩn bị notebook và input. Chưa upload, cài package, tải model hoặc chạy GPU.

1. Mở `notebooks/room_belief_qwen3_rb2_v1.ipynb` trên Kaggle.
2. Tạo một Kaggle Dataset private từ duy nhất `artifacts/room_belief/room_belief_qwen_rb2_inputs_v1.zip`; notebook nhận ZIP hoặc các file package đã extract. Không đính kèm U6/HM3D, room snapshot, GT hay review CSV.
3. Attach dataset với mount mặc định `/kaggle/input/room-belief-qwen-rb2-v1`. Nếu Kaggle dùng slug khác, sửa `INPUT_ROOT` ở cell B.
4. Chạy riêng cell A (probe) và B (hashes). Không Run All.
5. Dừng và review output trước khi bật cell C. Cell C cài bốn direct pin bằng `pip --no-deps`, sau đó yêu cầu phiên bản đúng và `pip check`; transitive environment vẫn cần xác minh.
6. Cell D chỉ kiểm tra model/tokenizer đã có trong cache bằng `local_files_only=True`; không tải. Nếu thiếu snapshot, dừng để xin quyền tải.
7. Cell E chỉ load BF16 từ cache và cần GPU hỗ trợ BF16 cùng ≥12 GiB VRAM trống. T4 không qua gate BF16; không tự cast FP16.
8. Chỉ sau khi smoke được duyệt và qua tokenizer boundary, chạy cell F smoke trước; review logs rồi mới tạo A table. Chạy đủ 11 category strings × 8 room categories, giữ bốn biến thể score riêng.

Output dự kiến ở `/kaggle/working/room_belief_qwen_rb2_output_v1/<UTC timestamp>/`; gồm `raw_token_scores.json`, `a_table.csv`, `a_table.json`, `category_room_scores_by_variant.json`, `manifest.json`. File mapping giữ riêng mean/sum × neutral/no-neutral và có thể chuyển sang `RoomBeliefLedger` sau khi chọn variant. Các hashes tính ở Kaggle là integrity hashes cục bộ; revision upstream vẫn là provenance pin.
# Lịch sử

Checklist này mô tả notebook RB2 v1 trước khi duyệt FP16 T4. Dùng checklist cập nhật tại [ROOM_BELIEF_RB2_KAGGLE_CHECKLIST_v2.vi.md](ROOM_BELIEF_RB2_KAGGLE_CHECKLIST_v2.vi.md) và notebook `room_belief_qwen3_rb2_v2.ipynb` cho lượt hiện tại.

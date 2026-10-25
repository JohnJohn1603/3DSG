# Changelog bundle v12

- Giữ nguyên bundle v11 và tạo notebook v12 vì probe Kaggle thực tế cho thấy kernel Python 3.13 / Torch 2.11 cu128 không khớp recipe Python 3.10 / Torch 2.0.1 cu118.
- Notebook v12 giữ kernel Kaggle nguyên trạng; thêm một micromamba 2.9.0-0 environment riêng tại `/kaggle/working/cg_q1_env_v12`. Binary micromamba được pin theo release asset và SHA-256 do release chính thức công bố.
- Mọi cài package, import preflight, reference loader và inference runner sau bước tạo env gọi rõ `CG_PYTHON`; không dùng pip trên kernel Python 3.13.
- Tạo env là opt-in (`CREATE_ISOLATED_ENVIRONMENT=False`). Cell A chỉ probe kernel. Không có lệnh nào tự cài dependencies, tải checkpoint, inference hoặc pilot khi Run All.
- Cell 1 cho phép bundle được mount để probe mà chưa mount capture; capture package chỉ được xử lý nếu được mount. Bundle/capture hashes vẫn phải được đối chiếu với manifest trước khi giải nén.
- RAM checkpoint được pin tới `xinyu1205/recognize_anything_model`, revision `ebc52dc741e86466202a5ab8ab22eae6e7d48bf1`, file `ram_swin_large_14m.pth`; expected SHA-256 là metadata do Hugging Face công bố. Hash cục bộ vẫn chỉ được tính sau khi file được người dùng cho phép đưa vào phiên.
- Không tải checkpoint, cài package, upload dữ liệu, chạy inference, pilot, hay xác minh Kaggle runtime trong quá trình tạo bundle v12.
- Runtime compatibility của micromamba solve, CUDA extension build, reference loader và model smoke vẫn pending Kaggle.

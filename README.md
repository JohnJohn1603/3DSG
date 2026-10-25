# 3DSG — Handoff để kết hợp

Mã nghiên cứu tập trung: **3DSG frontier-based + OccuSG standalone không ROS + ConceptGraphs CPU hybrid**.

- [Tải gói đầy đủ đã kiểm chứng (~464 MiB)](https://github.com/JohnJohn1603/3DSG/releases/tag/research-handoff-2026-10-26): có source, models, scene và native workers.
- [Hướng dẫn cài đặt/chạy demo bằng tiếng Việt](README.vi.md).
- [Điều kiện sử dụng các thành phần bên thứ ba](THIRD_PARTY_NOTICES.md).

Không kèm run cũ, cache hoặc virtualenv. Một checkpoint OpenCLIP 605 MB được lưu trong gói Release; các source và tài sản nhỏ hơn nằm trong Git.

Sau khi clone repository:

```bash
python3 -B tools/fetch_demo_assets.py
python3 -B tools/verify_bundle.py
```

Hoặc dùng archive đã tải: `python3 -B tools/fetch_demo_assets.py --archive /path/to/3DSG_Research_Bundle.tar.gz`.
Muốn kiểm tra riêng source trước khi tải model: `python3 -B tools/verify_bundle.py --source-only`.
Sau đó tạo các môi trường theo README.vi.md và chạy `demo.py`.

Gói đã được chạy từ thư mục di chuyển: native ConceptGraphs/OccuSG thật, 19 paired graph updates; desktop RGB/map/graph hoạt động; 193 tests passed. Continuity audit ghi nhận một đoạn khựng điều hướng 36.73 s; giới hạn này được giữ và mô tả trong hướng dẫn nghiên cứu.

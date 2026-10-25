# Changelog bundle v11

- Giữ nguyên các bundle v8, v9 và v10; phát hành v11 sau khi hoàn thiện package pins trực tiếp của Grounded-SAM trong dependency lock và cập nhật notebook cùng bundle-version guard.
- Nội dung tích hợp giữ nguyên so với E: upstream detector/mapping algorithms không đổi; runner chỉ tạo file compatibility `TkAgg` → `Agg`, không copy cả repository.
- Lock liệt kê thêm các package được tham chiếu từ ConceptGraphs `environment.yml`/pinned GSA requirements cho selected call path. GSA transitive/runtime compatibility vẫn pending Kaggle smoke.
- `chamferdist` vẫn được pin riêng cho reference evaluation và không được fetch/install trong notebook.
- Không capture, cài package, tải weights, upload, hoặc chạy Kaggle/GPU.

# Changelog bundle v10

- Giữ nguyên v9; phát hành bundle v10 vì thay đổi nội dung notebook, runner và dependency scope.
- Runner không còn copy toàn bộ checkout ConceptGraphs vào từng run. Nó tạo một file compatibility `compat/generate_gsa_results.py`, đổi riêng Matplotlib backend `TkAgg` → `Agg`, rồi import các module còn lại từ checkout pinned qua `PYTHONPATH`.
- Dependency lock phân loại GradSLAM/PyTorch3D là core-required theo call path của recipe; chamferdist là reference-evaluation-only. Notebook không còn fetch/install chamferdist hoặc chạy `Grounded-SAM/requirements.txt` tổng thể.
- GroundedDINO và SAM source package chỉ được cài bằng cell opt-in với `--no-deps`; đây không phải bằng chứng dependency closure hoặc runtime đã đạt.
- Thêm `CONCEPTGRAPHS_MINIMAL_CORE_MAP.vi.md` mô tả function/dependency closure, phần giữ/loại và compatibility boundary.
- Không cài/tải/upload, không chạy GPU/Kaggle. Kaggle reference loader, smoke, parity vẫn pending.

# Kiểm chứng lượt chuẩn bị — 07-10-2026

## Đã pass

- 283 file được seed, 2.361.452 bytes payload: hash của cả source baseline và bản copy khớp manifest khi kiểm lại cuối lượt, 0 mismatch.
- OccuSG reference mới đúng `2bca2fa06af87fd9dd0542957039be8f580f0dca`, worktree sạch.
- ConceptGraphs reference mới đúng `93277a02bd89171f8121e84203121cf7af9ebb5d`, worktree sạch.
- OccuSG reference trong baseline cũ cũng báo sạch.
- JSON locks parse hợp lệ; link tương đối trong tài liệu mới không có link hỏng; code fences cân bằng.
- `./run_scene.sh help` exit 0. Launcher tạo duy nhất `.launcher.lock` rỗng dưới `artifacts/runs/` mới; không copy old runs.
- Targeted source regression **25 passed trong 0,52s**:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src python3 -B -m pytest -q -p no:cacheprovider \
  tests/test_m6_object_association.py \
  tests/test_room_semantics.py \
  tests/test_region_object_room_semantics.py
```

Lệnh chạy tại root Fusion_Concept_OccuSG, dùng system Python đã có pytest, không cài package.

## Kiểm tra không pass và cách đọc

Thử cùng nhóm test bằng baseline `.venv-perception/bin/python` dừng ngay vì **No module named pytest**. Không sửa env hoặc cài pytest vào đó. Đây là khác biệt môi trường test/runtime phải ghi trong U0; targeted tests dùng system Python không chứng minh dependency runtime fusion đầy đủ.

## Chưa chạy / chưa tuyên bố

- Full suite, native CMake rebuild, Habitat capture/replay trong root mới.
- ConceptGraphs feature inference, fusion association, class correction hoặc cải thiện room metrics.
- Không chạy dense 280 frames hay evaluation mới; không thay dataset/weights/env.
- Checksum bảo toàn là trên allowlist source seed, không phải checksum mọi byte của toàn baseline artifacts.

Workspace mới khoảng 38 MiB gồm hai repo reference và source seed, không gồm dataset/model/env.

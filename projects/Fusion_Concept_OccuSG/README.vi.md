# Fusion_Concept_OccuSG

## Kiến trúc đang phát triển

```text
Habitat local: RGB-D, calibration, poses
        ├── ConceptGraphs trên Kaggle: object map
        └── OccuSG local: room map
                         ↓
                 adapter local
                         ↓
                 unified graph
```

ConceptGraphs sở hữu detection, masks, features, lifting và object association/fusion. OccuSG sở hữu occupancy, room segmentation và adjacency. Adapter chuyển tọa độ, lifecycle và object-to-room membership. Không chạy lại M6/K4 để associate objects của ConceptGraphs.

## Điểm bắt đầu

1. [Kiến trúc active](docs/ACTIVE_ARCHITECTURE.vi.md)
2. [Kế hoạch rebuild hiện hành](docs/CONCEPTGRAPHS_OCCUSG_REBUILD_PLAN.vi.md)
3. [Trạng thái và preflight R0/R1](docs/REBUILD_R0_R1_PREFLIGHT.vi.md)
4. [R2/R3 Kaggle handoff](docs/R2_R3_KAGGLE_HANDOFF.vi.md)
5. [Notebook pilot 00808](notebooks/conceptgraphs_detect_q1_00808.ipynb)
6. [Cleanup plan](docs/PROJECT_CLEANUP_PLAN.vi.md), [cleanup candidates — proposal only](docs/CLEANUP_CANDIDATES_CURRENT_2026-10-12.json)
7. [Legacy index](docs/LEGACY_INDEX.vi.md)

Package pilot hiện có: `artifacts/integration_v2/packages/q1_00808_cg_v6.zip`. Bundle code/notebook hiện hành: `artifacts/integration_v2/kaggle_bundle/conceptgraphs_r2_bundle_v8.zip`. Xem handoff để biết hashes, thao tác Kaggle và các blocker trước khi chạy.

## Trạng thái thực tế

- R0/R1 đã hoàn tất; R2 CPU-side package/adapter và bộ chuẩn bị notebook R3 đã được kiểm chứng cục bộ.
- Kaggle chưa được upload/chạy. Quyền chuyển capture HM3D sang dịch vụ bên thứ ba chưa xác minh; một số dependency source pins còn thiếu.
- Adapter chưa nối vào unified graph runtime. Launcher `run_scene.sh` và default cũ vẫn là luồng OccuSG trước đây; không coi chúng đã chạy ConceptGraphs backend.
- Source association là default cũ; geometry-only và các U/K/Q experiments là đối chứng/lịch sử, không phải architecture path hiện hành.

## Lịch sử nghiên cứu

Các plan/status U0–U6, K0–K6, Q0–Q4 và U4-OV vẫn được giữ để truy vết kết quả, annotation và giới hạn đã học. Chúng không còn xác định thứ tự phát triển chủ đạo. Mục lục và phân loại nằm trong [LEGACY_INDEX.vi.md](docs/LEGACY_INDEX.vi.md); không xóa hoặc di chuyển tài liệu trong lượt cleanup này.

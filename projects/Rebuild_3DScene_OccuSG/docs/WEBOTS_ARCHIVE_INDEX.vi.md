# Chỉ mục archive Webots lịch sử

Hoàn tất ngày 2026-10-05. Archive nằm ngoài workspace tại:

`/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG_webots_archive_20261005`

Các đường dẫn tương đối bên trong archive giữ nguyên cấu trúc dưới workspace. Có 18 root được chuyển, 1,508,790,212 byte logic, 0 symlink. Archive cùng filesystem với workspace nên việc chuyển **không giải phóng dung lượng ổ đĩa**. Không có gì bị xóa vĩnh viễn. Checksum SHA-256 cây sau khi chuyển được ghi trong [cleanup_manifest.json](../cleanup_manifest.json).

| Đường dẫn cũ tương đối | Đường dẫn archive tương ứng |
|---|---|
| `artifacts/gui_two_room_capture` | `<archive-root>/artifacts/gui_two_room_capture` |
| `artifacts/m12_complete_apartment_coverage_capture` | `<archive-root>/artifacts/m12_complete_apartment_coverage_capture` |
| `artifacts/m12_complete_apartment_coverage_capture_v2` | `<archive-root>/artifacts/m12_complete_apartment_coverage_capture_v2` |
| `artifacts/m12_complete_apartment_coverage_capture_v3` | `<archive-root>/artifacts/m12_complete_apartment_coverage_capture_v3` |
| `artifacts/m12_complete_apartment_record_01_capture` | `<archive-root>/artifacts/m12_complete_apartment_record_01_capture` |
| `artifacts/m12_kitchen_record_01_capture` | `<archive-root>/artifacts/m12_kitchen_record_01_capture` |
| `artifacts/m12_two_rooms_current_graph` | `<archive-root>/artifacts/m12_two_rooms_current_graph` |
| `artifacts/m12_webots_two_rooms_capture` | `<archive-root>/artifacts/m12_webots_two_rooms_capture` |
| `artifacts/m12_webots_two_rooms_motor_capture` | `<archive-root>/artifacts/m12_webots_two_rooms_motor_capture` |
| `artifacts/runs/complete_apartment_276_extended_unknown_boundary` | `<archive-root>/artifacts/runs/complete_apartment_276_extended_unknown_boundary` |
| `artifacts/runs/complete_apartment_276_repeatability` | `<archive-root>/artifacts/runs/complete_apartment_276_repeatability` |
| `artifacts/runs/complete_apartment_276_replay_checkpoint_20260930` | `<archive-root>/artifacts/runs/complete_apartment_276_replay_checkpoint_20260930` |
| `artifacts/runs/complete_apartment_276_source_replay` | `<archive-root>/artifacts/runs/complete_apartment_276_source_replay` |
| `artifacts/runs/complete_apartment_276_source_replay_nogrid` | `<archive-root>/artifacts/runs/complete_apartment_276_source_replay_nogrid` |
| `artifacts/runs/complete_apartment_276_source_replay_v2` | `<archive-root>/artifacts/runs/complete_apartment_276_source_replay_v2` |
| `artifacts/runs/complete_apartment_276_source_replay_v3` | `<archive-root>/artifacts/runs/complete_apartment_276_source_replay_v3` |
| `artifacts/runs/complete_apartment_keyboard_unsaved_276_source_20260930` | `<archive-root>/artifacts/runs/complete_apartment_keyboard_unsaved_276_source_20260930` |
| `artifacts/runs/complete_apartment_latest` | `<archive-root>/artifacts/runs/complete_apartment_latest` |

## Phục hồi

Khôi phục từng cây về đúng vị trí cũ trước khi mở/replay. Lệnh dưới đây cố ý từ chối nếu đích đã tồn tại để không ghi đè:

```bash
cd /home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG
ARCHIVE=/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG_webots_archive_20261005
REL='artifacts/runs/complete_apartment_latest'  # thay bằng một đường dẫn trong bảng
test ! -e "$REL" && mkdir -p "$(dirname "$REL")" && cp -a "$ARCHIVE/$REL" "$REL"
```

Sau khi phục hồi, xác minh tree hash với `cleanup_manifest.json`. Không chạy trực tiếp từ archive: một số manifest/log có thể chứa đường dẫn tuyệt đối cũ. Chỉ phục hồi output cần thiết; không copy đè cả `artifacts/`.

## Đã giữ lại

Webots controllers, PROTO/worlds, configs, runners/helpers, tests, calibration fixtures và route fixture dùng chung/regression vẫn ở workspace. Webots hiện là backend legacy, không thuộc workflow phát triển chính. Đặc biệt, `artifacts/m12_sensor_calibration` được giữ vì runner còn đọc nó.


# M11 — minimal reproducer cho jamb sample (2026-09-28)

Phạm vi: chẩn đoán riêng cột world `(4.40, 1.45)`, lấy từ M2 snapshot revision 44. Không chạy lại route 44 frame; không thay đổi run tham chiếu, tham số/config, M2/M3 runtime, DuDe kernel hay M8. Đây là fixture chẩn đoán, không được đưa vào builder.

## Provenance và fixture

- Snapshot nguồn: `artifacts/m11_two_room_diagnostics_m3_controls/milestones/final_revisit_m2_snapshot.json.gz`.
- MapConversion checkout khớp `locks/sources.json`: commit `14b8cb9e6534ba21e84f624c94d20960f6613f3c`.
- Fixture nhỏ: `milestones/jamb_column_minimal_leaves.txt` và phần giải thích từng leaf trong `milestones/jamb_column_minimal_fixture.json`.
- Fixture gồm 192 leaf đóng góp vào cùng output Map2D cell (100 occupied, 92 free), cùng 6 leaf thật ở biên tree để giữ metric extent gốc; tổng 198. Giữ OctoMap key, log-odds, state và leaf size 0,05 m. Các leaf biên không được dùng làm scan hay biến thành occupancy giả.
- SHA-256: leaf text `03dd1d569ea33ee8f3dff54bad70a2dcd1e1066850ed2a5abedf34d7b478d097`; fixture JSON `3551408ca15331e56ab526f431c532399f2a174187e1ee8157e598417ebf5c62`.

## Kết quả truy nguồn

Sample là M3 free (`0`), height floor `0.0499499996 m`, ceiling `2.3500500004 m`. Từ 92 free leaf có 46 mức Z riêng (tâm `0.075000003`–`2.325000048 m`), mỗi mức xuất hiện ở hai dải Y. Không phải một leaf lớn: các khoảng Z sau epsilon/union ghép thành một dải `0.0499500026`–`2.3500500481 m`, cao khoảng `2.30010 m`, vượt clearance `0.3 m`. Một leaf riêng chỉ đóng góp xấp xỉ `0.0501 m`; sau khi sáu mức liên tiếp được hợp, dải đã vượt ngưỡng (`~0.3001 m`). `HeightRangeMap::removeFreeRanges` giữ dải chỉ khi cao hơn `minZ`.

Nguồn gốc hình học/raster:

- Cả 92 free leaf đều có tâm X `4.4749999046 m` và AABB X `[4.4499999043, 4.4999999050] m`. Chúng **không giao mặt phẳng vách x=4.4 m**; AABB gần nhất cách mặt phẳng khoảng `0.0499999043 m` về phía đông.
- Các lower-corner XY thực tế lặp lại ở bốn vị trí: `(4.4000001904,1.4500000235)`, `(4.4000001904,1.4999999758)`, `(4.4499999043,1.4500000235)`, `(4.4499999043,1.4999999758)`; mỗi vị trí có 48 đóng góp. Với Map2D origin `(0,-2)`, resolution `0.05 m`, quy tắc `floor((position-offset)/resolution)` đưa cả bốn về cùng sample `(col,row)=(88,69)`. Hai X source ở hai phía của biên bin `4.45 m`; giá trị float32 của leaf half-size làm lower-corner phía đông thành `4.4499999043`, ngay dưới biên toán học, nên bị floor vào bin `x=4.40`.
- Trong cùng output cell có 100 occupied intervals. Occupied union bao gần toàn khoảng Z (`-0.00005`–`2.40005 m`), nhưng source giữ free/occupied intervals riêng; free interval đủ clearance được ghi vào map trước. Với cell này, neighbor-occupation không tạo ra free result và không phải nhánh làm ô thành free. Neighbor pass xử lý ô không có free ranges dựa trên free/occupied ranges cạnh bên.

Đối chiếu với pinned source: `MapConverter.cpp` tính `sizeIndex`, lower-corner local positions và `floor` để rasterize; nới Z interval bằng `resolution*0.001`; sau đó gọi `removeFreeRanges`. `HeightRangeMap.hh` hợp các interval chạm/giao nhau và lọc clearance. Đây là semantics của pinned source, không phải logic adapter mới.

## Direct-source / standalone reproducer

`native/occupancy/jamb_column_repro_fixed.cpp` dựng OcTree mới từ đúng các key/log-odds trong fixture (không bắn lại scan), lấy leaves từ tree đó, rồi:

1. Gọi direct pinned `MapConverter::updateMap()` trên chính các leaves và metric bounds.
2. Gọi `ProjectionBackend` standalone trên cùng OcTree với converter mới hoàn toàn.
3. So sánh geometry/origin, toàn bộ occupancy values, floor/ceiling và sample jamb.

Build và chạy đã dùng:

```bash
g++ -std=c++17 -O2 -w \
  -Inative/occupancy \
  -Ibuild/deps/octomap-prefix/usr/include \
  -I/home/kiettran05/Documents/Codex/2026-09-22/c-c/research/occusg_phase1/upstream/mapconversion/mapconversion/include \
  -I/usr/include/eigen3 \
  native/occupancy/jamb_column_repro_fixed.cpp \
  build/native-m3/occupancy/libstandalone_sg_occupancy.a \
  -Lbuild/deps/octomap-prefix/usr/lib/x86_64-linux-gnu \
  -loctomap -loctomath \
  -o build/native-m3/occupancy/standalone_sg_jamb_column_repro

LD_LIBRARY_PATH=build/deps/octomap-prefix/usr/lib/x86_64-linux-gnu \
  build/native-m3/occupancy/standalone_sg_jamb_column_repro \
  artifacts/m11_two_room_diagnostics_m3_controls/milestones/jamb_column_minimal_leaves.txt
```

Kết quả: `geometry_match=true`, occupancy/floor/ceiling mismatches đều `0`; source và standalone cùng trả sample value `0`, floor `0.0499499996`, ceiling `2.3500500004 m`. Cây dựng lại có 198 leaf, depth 16; metric bounds giữ theo các boundary leaves thật. Vì vậy, reproducer nhỏ này không cho thấy sai khác adapter/source.

## Sensitivity control — chỉ phép chiếu

`native/occupancy/jamb_alignment_sensitivity_fixed.cpp` dịch cứng reduced voxel evidence và bounds cùng `(+0.01,+0.01,+0.01) m`, rồi query cùng vị trí hình học đã dịch. Kết quả direct pinned projection đổi từ free (`0`) sang unknown (`-1`), không có height hợp lệ. Đây chỉ là sensitivity control tại phép chiếu trên evidence đã lưu; **không** phải replay toàn scene/camera/route, không chứng minh DuDe sẽ tách phòng, và không phải đề xuất sửa baseline.

## Kết luận theo bằng chứng

- **Đã xác minh:** free clearance tại jamb được hợp từ 92 leaf free nhỏ nằm phía đông vách. Các lower-corner của leaf ở sát biên raster bị floor vào cùng output cell với leaf phía vách; dải free sau union vượt ngưỡng clearance. Direct pinned source và standalone cho cùng kết quả trên bộ leaf tối thiểu.
- **Cơ chế được tái hiện:** voxel/lower-corner placement cộng với Map2D `floor`/biên bin; độ nhạy sub-voxel được thấy trong control riêng.
- **Không được kết luận:** chưa chứng minh toàn bộ wall barrier mất chỉ vì một quy tắc duy nhất; chưa phân rã định lượng đóng góp của coverage, ray/endpoint placement và source interval semantics trên cả vách; không gọi đây là lỗi M3 adapter. Cũng chưa chứng minh thay đổi alignment sẽ cải thiện số region hoặc đúng tổng quát.
- Không thêm occupied-wins, wall inflation, reset, full rebuild, thay đổi kernel hay policy vào reference backend. Run tham chiếu không đổi.

Regression liên quan đã chạy: `PYTHONPATH=src:tests python3 -m pytest -q tests/test_m3_native_integration.py` — **6 passed**. Không chạy full suite hoặc route 44 frame trong lượt này.

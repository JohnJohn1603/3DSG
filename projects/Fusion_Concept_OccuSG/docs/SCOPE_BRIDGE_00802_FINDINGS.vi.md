# Scope Bridge 00802 — kết quả truy nguyên

Ngày: 2026-10-16  
Phạm vi: đọc code và artifacts đã có; không chạy native replay, ConceptGraphs,
perception hoặc model; không thay room/object geometry, A tables, thresholds,
admission hay baseline.

## Kết luận

Scope Bridge **chưa verified**. Room producer hiện tạo room polygon trên world
XY từ occupancy đã chiếu 2D, không xuất layer/elevation scope đủ để nối an
toàn với hybrid object clouds. Object side có tọa độ world XYZ và provenance
RGB-D riêng, nhưng chưa có room-side scope tương ứng. Không gán membership
hoặc room belief bằng `floor_0` hay bằng pose Z.

## Room producer: đường thực thi và bằng chứng

Room replay được kiểm tra là
`artifacts/runs/hm3d_00802_20261011T025309.067351Z_4f3ace/replay/`:

- Scene `00802-wcojb4TFT35`, sequence `hm3d-v02-00802-main-route-01`, 29
  frames/poses; 5 rooms cuối.
- Replay manifest SHA-256:
  `43984bce827c932e4c26e46ce550b3b5eba13ce06c06b3d42be6b94c1aac9b1f`.
- Capture manifest SHA-256:
  `1576cfd90c1ad8432dccc2312725fa703949f0232691a1c2cddcc96b0554d0d2`.
- Config `configs/reference_code_mp3d.yaml`, SHA-256
  `210124b16fd3651c06fd548a1704f27595f61761dd1bfb4db4099e51895d1523`.
- Pinned MapConversion source commit `14b8cb9e6534ba21e84f624c94d20960f6613f3c`;
  M2/M3 binary SHA-256
  `bd171a8ac9974eec18957af97e6fc369729358ca62985390eb994c26f2040b29`.

Code path:

1. Habitat camera/world transforms are normalized by
   `adapters/habitat/frames.py` and `src/standalone_sg/geometry/simulator_frames.py`.
   Habitat world Y-up becomes canonical world Z-up (`x=H.x`, `y=-H.z`,
   `z=H.y`). RGB-D depth is axial metres; M1 back-projects with intrinsics and
   `T_W_Copt`, then M2 integrates XYZ world points.
2. `MapConverter::updateMap` builds a 2D grid on world XY. It keeps vertical
   free/occupied ranges internally. Config `projection.min_free_height_m=0.3`
   removes free intervals shorter than 0.3 m; it does **not** select one floor
   or one fixed Z band. A cell becomes free when it has surviving free range(s).
   The exported height values use the bottom of the lowest free interval and
   top of the highest free interval, so they are an envelope if intervals are
   disjoint. The XY projection can therefore combine multiple elevations.
3. `OctoMapWorker.project()` returns `values`, `z_floor`, and `z_ceiling`, but
   `DuDeWorkerClient.integrate()` sends only grid dimensions, resolution,
   `origin_xy`, and flattened `values`. M4/region and room contracts store
   polygons/centroids in XY and source revisions, without Z bounds or floor ID.

Artifact check:

- Across the 29 saved room revisions there are 155 room-node instances; **0**
  have `floor_id`, `z_bounds`, `elevation`, or spatial-scope attributes.
- The saved replay has no grid-trace/height-array artifact. `frame_diagnostics`
  stores `m3_changed_bounds_xyz` per update, which is not a room-specific
  floor interval. The run used no `GridTraceWriter` output.
- The replay runs the pinned 2D room path with source preprocessing; polygons
  are not tied to a selected layer. Whole-scene single-floor status is
  unproven, and the room/object subset is not currently proven compatible.

## Đính chính status single-floor của static v5

`m4_causal_hybrid_00802_v5/manifest.json` ghi
`verified_single_observed_floor_scope`, nhưng cùng record ghi
`scope_contract: null`, P3 layer coverage `45/67`, profile `floor_0`, source
object layer `floor_0`, và P2 rows “inherit” profile. Các dữ liệu đó không mô
tả cách room polygons được tạo theo elevation. `multi_floor_safety` cũng là
`false`.

Vì vậy v5 đã mở XY membership dựa trên agreement của profile/P3/source object
layer, không phải dựa trên scope của room producer. Không sửa artifact cũ,
nhưng **không coi status v5 là chứng minh single-floor**. A.1 hiện hành
`m4_causal_hybrid_00802_a1_v6` giữ gate đóng với
`room_scope_contract_missing_for_2d_polygons`; đó là trạng thái đúng với bằng
chứng đã lưu.

## ConceptGraphs hybrid object scope

Hybrid mapping được kiểm tra trong
`scripts/run_conceptgraphs_cached_hybrid.py` và
`artifacts/scene_graph_timeline/m0_m1_00802/recorder_room_1/mapping/`:

- Mỗi frame dùng P2 masks cùng RGB/depth đã cache; `to_dets(..., depth, K,
  trans_pose=T)` lift bằng RGB-D và `T_W_Copt` dựng từ capture record.
  P3 poses chỉ được cross-check nếu có; P3 accepted record không phải điều
  kiện để map observation P2 đó.
- Export ghi units `m`, transform
  `T_capture_world_from_cg_world = identity`, và frame contract
  `conceptgraphs_world_from_capture_contract`. Mapping contract dùng cùng
  capture manifest hash như room run.
- Do đó 22 observations không có P3 accepted/layer record vẫn có thể có
  hybrid RGB-D geometry; không loại chúng khỏi object map vì thiếu P3. Nhưng
  không tự suy ra `floor_id` cho chúng. Per-object scope phải được đánh giá
  từ prefix hybrid XYZ/lineage và room scope có provenance.
- Export final map có
  `identity_scope=final_map_only_no_persistent_lifecycle_claim`; causal
  membership phải đọc cloud/observations đúng prefix revision, không dùng
  final object cloud để backfill quá khứ.

## Adapter/test status

A.1 hiện kiểm tra profile/P3 layers như provenance đối chiếu và fail-closed
khi room scope contract thiếu. Tuy nhiên, nhánh xác minh cũ còn yêu cầu P3
layer coverage đầy đủ trước khi mở một scope contract. Đây không thể là điều
kiện thay cho hybrid object scope: khi bridge được thiết kế tiếp, scope room
phải tới từ room producer, object scope từ prefix hybrid geometry, còn P3 chỉ
là cross-check khi tồn tại. Object có scope unknown phải riêng lẻ
`unavailable`; không được chặn các object khác đã có bằng chứng hợp lệ.

Đã thêm test hồi quy xác nhận evaluation profile + P3 `floor_0` nhất quán vẫn
không mở room XY membership nếu room node không có scope và không có contract.
Targeted tests:

```bash
PYTHONPATH=src:tests python3 -m pytest -q -p no:cacheprovider \
  tests/test_causal_correctness_a.py \
  tests/test_causal_hybrid_room_belief.py \
  --basetemp /tmp/scope-bridge-tests
```

Kết quả: **16 passed**. Không chạy mapping/pipeline; các tests kiểm tra contract
và gate hiện có.

## Đường tiếp theo nhỏ nhất cần duyệt

Không cần thêm capture hay chạy lại perception/ConceptGraphs. Để xác định
scope theo chính producer, cần một **diagnostic native replay** trên 29-frame
capture đã khóa và instrumentation chỉ đọc để lưu vertical free intervals
theo M3 grid cell/revision, gắn với `source_grid_revision` của room geometry.
`--grid-output` hiện có chỉ lưu `z_floor/z_ceiling` envelope, nên riêng nó
chưa giải quyết được cell có nhiều disjoint intervals.

Sau replay, dùng các intervals đó để kiểm tra từng room polygon có một scope
đứng tương thích hay bị nhiều lớp chồng lên; object points lấy từ đúng prefix
hybrid timeline. Chỉ rooms/objects có scope duy nhất mới vào greedy
voxel-support; scope chồng/thiếu vẫn unavailable. Nếu polygon bị gộp qua nhiều
lớp, phép đo sẽ báo blocker và khi đó mới cần thiết kế projection/room
stratification riêng. Không dùng trajectory Z/profile floor làm nguồn duy nhất.

Replay nền đã lưu mất khoảng `210.55 s` cho 29 frames; instrumentation,
scope extraction, adapter/tests và semantic rerun làm tổng công tiếp theo ước
tính **60–90 phút**. Chưa chạy replay này. Command nền hiện có thể tái lập
room path và lưu grid trace (trace đó chưa đủ để chứng minh multi-layer scope):

```bash
python3 scripts/run_habitat_native_replay.py \
  --capture artifacts/runs/hm3d_00802_20261011T025309.067351Z_4f3ace/capture \
  --output artifacts/runs/hm3d_00802_20261011T025309.067351Z_4f3ace/replay_scope_probe_v1 \
  --pipeline-config configs/reference_code_mp3d.yaml \
  --m2-m3-binary /home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/build/native-m3/occupancy/standalone_sg_octomap_fixture_runner \
  --dude-binary /home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/build/native-m4-dude/occusg_dude_worker \
  --resource-manifest locks/external_resources.json \
  --grid-output artifacts/runs/hm3d_00802_20261011T025309.067351Z_4f3ace/replay_scope_probe_v1/grid_trace \
  --no-determinism-check
```

Đây là command chạy được cho grid trace hiện hành, **không phải** command đã
triển khai cho full interval scope. Trước khi chạy Scope Bridge thật cần duyệt
ngân sách 60–90 phút cho diagnostic instrumentation + một native room replay +
adapter/semantic verification. Không có thay đổi room projection hoặc polygon
algorithm được đề xuất trong phạm vi này.

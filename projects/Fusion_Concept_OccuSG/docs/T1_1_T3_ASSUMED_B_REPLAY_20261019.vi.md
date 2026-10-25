# T3 semantic replay B — HM3D 00802 T1.1, 13 prefixes

**Kết quả:** một lượt semantic replay B hoàn tất; A verified-only giữ nguyên làm đối chứng. B dùng giả định XY-compatible đã được duyệt, với `evidence_status=assumed_not_verified`. Đây không phải xác minh cùng tầng, membership đúng, room-label accuracy hay kết quả có thể chuyển sang run khác.

## Phạm vi và provenance

- Mapper run: `hm3d00802_t1_1_all13_mapper_only_20261019_v1`. Scene: `00802-wcojb4TFT35`; sequence: `hm3d-v02-00802-t1-1-turn-continue-v1_smooth_smooth_t1_1_hm3d00802_turn_continue_20261018_v2`.
- Prefix indices: `0, 26, 33, 40, 50, 61, 68, 75, 82, 89, 96, 103, 110`; full frame IDs, packet/object/room/unified revision refs, per-source hashes và transform provenance nằm trong declaration JSON.
- Mỗi prefix ghép đúng frame ID; unified revisions `1–13`, object revisions `1,27,34,41,51,62,69,76,83,90,97,104,111`, room revisions `1–13`. Không dùng future state.
- Declaration: `assumed_scope_declaration_v2.json`, ID `hm3d00802_t1_1_all13_assumed_single_level_demo_v1`, SHA-256 `a96f42a7924e539b86e48dede6ea6a4042783adbaec275b4f6bf5de30d27d78c`.
- Source mapper manifest SHA-256 `d30dc27759bf8eb29ae87fccb1037fa5b2b944d54bbdacc44a622ae3d950fa58`; capture manifest SHA-256 `bd83c4e9b5def51090b017da29a885713665af36c5d64d82d582a59663241bc9`; effective prefix-set SHA-256 `2e03811d1acf455b28ae6a552f06177f5eb09ce327916bc8e79e421c0ec8b5cc`; room config hash `210124b16fd3651c06fd548a1704f27595f61761dd1bfb4db4099e51895d1523`.
- A-table archive SHA-256 `0e30cc12058ec22372fe8b2df49279ec6a917027bef74182f8831374a7418997`; A control manifest SHA-256 `2de39e307de5935636e1e643f063c84f806262b4484c4b05d00b3c12805bcd77`; B manifest SHA-256 `2759a4b7fd393521b38509c2d8d888feccaac334f62005a23443d62553ca7338`.
- 13 camera poses dùng world Z-up metric XYZ; depth axial meters. Các `camera_pose_W_Copt` khớp phép đổi `T_W_Copt = T_W_H @ T_H_S @ diag(1,-1,-1)`; object XYZ hữu hạn. Room chỉ là polygon XY và không có elevation/unit/frame fields riêng. Không thấy mâu thuẫn transform/units đã biết, nhưng tính tương thích XY vẫn là **giả định**, không phải scope verified.

## Kết quả gate theo prefix

Bảng dưới tách admission, semantic consensus, giao đủ ba gate và membership. `unknown` là trạng thái consensus được ghi trong output; không đổi tên thành provisional. Admission/provisional là gate riêng. Tie không xuất hiện trong các prefix.

| Frame index | Hypotheses | Admission đủ | Consensus stable / conflicted / unknown | Admission ∩ stable ∩ vocabulary | Membership assigned / unassigned | Ties |
|---:|---:|---:|---:|---:|---:|---:|
| 0 | 5 | 0 | 0 / 0 / 5 | 0 | 4 / 1 | 0 |
| 26 | 6 | 0 | 0 / 0 / 6 | 0 | 5 / 1 | 0 |
| 33 | 6 | 1 | 1 / 0 / 5 | 1 | 5 / 1 | 0 |
| 40 | 7 | 2 | 2 / 0 / 5 | 2 | 6 / 1 | 0 |
| 50 | 12 | 2 | 2 / 0 / 10 | 2 | 11 / 1 | 0 |
| 61 | 12 | 3 | 1 / 2 / 9 | 1 | 11 / 1 | 0 |
| 68 | 12 | 3 | 1 / 2 / 9 | 1 | 11 / 1 | 0 |
| 75 | 12 | 3 | 2 / 1 / 9 | 2 | 11 / 1 | 0 |
| 82 | 12 | 3 | 1 / 2 / 9 | 1 | 11 / 1 | 0 |
| 89 | 12 | 3 | 1 / 2 / 9 | 1 | 11 / 1 | 0 |
| 96 | 14 | 4 | 2 / 2 / 10 | 2 | 13 / 1 | 0 |
| 103 | 15 | 4 | 2 / 2 / 11 | 2 | 14 / 1 | 0 |
| 110 | 15 | 6 | 4 / 2 / 9 | 4 | 14 / 1 | 0 |

Ở prefix cuối có 15 hypotheses; 6 đạt admission, 4 có consensus stable và supported category đồng thời đạt admission, 2 bị conflicted, 9 ở trạng thái consensus `unknown`. Membership theo giả định B: 14 assigned, 1 unassigned, 0 ties. Đây là số trạng thái trong run này, không phải quality metric.

## Room belief theo variant ở prefix cuối

Belief là normalized score nội bộ từ A, không phải xác suất accuracy. Full per-prefix scores, support, exclusions, contribution vectors và transitions ở `diagnostic/room_variant_prefixes.csv`; object-level gate ledger ở `diagnostic/t3_assumed_b_report.json`.

| Variant | Room | Eligible categories | Candidate (top belief) | Kết quả cuối | Top belief | Lý do / transitions |
|---|---|---|---|---|---:|---|
| mean_no_neutral | room:1 | sofa, tv | living_room | committed: living_room | 0.8534 | —; ∅→living_room@2 |
| mean_no_neutral | room:2 | chair | living_room | abstain: — | 0.4958 | insufficient_support; không có transition |
| mean_no_neutral | room:4 | — | — | abstain: — | 0.1250 | insufficient_support; không có transition |
| sum_no_neutral | room:1 | sofa, tv | living_room | committed: living_room | 0.6547 | —; ∅→living_room@2 |
| sum_no_neutral | room:2 | chair | living_room | abstain: — | 0.4490 | insufficient_support; không có transition |
| sum_no_neutral | room:4 | — | — | abstain: — | 0.1250 | insufficient_support; không có transition |
| mean_neutral | room:1 | sofa, tv | kitchen | abstain: — | 0.5315 | belief_below_threshold; không có transition |
| mean_neutral | room:2 | chair | kitchen | abstain: — | 0.4164 | insufficient_support; không có transition |
| mean_neutral | room:4 | — | — | abstain: — | 0.1250 | insufficient_support; không có transition |
| sum_neutral | room:1 | sofa, tv | living_room | abstain: — | 0.4563 | belief_below_threshold; không có transition |
| sum_neutral | room:2 | chair | dining_room | abstain: — | 0.3607 | insufficient_support; không có transition |
| sum_neutral | room:4 | — | — | abstain: — | 0.1250 | insufficient_support; không có transition |

- `room:1`: unique eligible categories `sofa`, `tv`; sofa có nhiều object ledger entries nhưng chỉ một contribution theo category cap. `mean_no_neutral` và `sum_no_neutral` đều commit candidate `living_room` ở frame 110 (mỗi variant một transition). `mean_neutral` abstain `belief_below_threshold` với candidate `kitchen`; `sum_neutral` abstain cùng lý do với candidate `living_room`.
- `room:2`: chỉ category `chair` eligible (một contribution); cả bốn variants abstain `insufficient_support`, vì policy cần hai unique categories. Candidate lần lượt: `living_room`, `living_room`, `kitchen`, `dining_room` theo thứ tự bảng.
- `room:4`: không có eligible category; bốn variants abstain `insufficient_support`.
- Cumulative room-state/commit-transition khác nhau: chỉ hai transitions trong toàn B run (mỗi no-neutral variant commit một lần vào room:1); neutral variants không commit. Không cộng các variants thành bốn room labels độc lập.

### A verified-only control

A giữ `unavailable_elevation_scope`: membership và room scoring không chạy vì scope gate đóng. Đây là **scoring unavailable**, không phải abstain sau khi tính score. A không được thay bằng B khi chọn mode trong viewer.

## Thực thi và kiểm tra

- Semantic replay: đúng 1 lần, 13 revisions, trạng thái `completed`, runtime `28.603 s`. Capture/perception/encoder/Qwen/ConceptGraphs mapping/OccuSG replay trong lượt này: 0.
- Preparation/model-free: các lỗi ban đầu về import path, cách đối chiếu raw-vs-packet metadata hash, và capture run ID khác tên thư mục đã được sửa trước replay; preparation cuối đạt. Không retry semantic replay.
- Viewer-only rebuild V5 dùng A/B outputs hiện có; không chạy lại semantic replay. Receipt liệt kê 244 outputs; toàn bộ SHA-256 đã đối chiếu khớp. Build failure cũ được giữ ở `viewer_build_failure.json`; trạng thái sau rebuild ở `viewer_resolution_v5.json`.
- Targeted tests theo lượt triển khai: `12 passed` (declaration sparse-prefix, viewer A/B provenance và T3 viewer tests). Đây là kiểm thử logic, không phải bằng chứng quality của scene.
- Browser automation hiện không phát hiện browser/app; GUI acceptance đang pending. Không coi file/hash validation là GUI pass.

## Mở viewer và checklist GUI

```bash
python3 scripts/serve_causal_timeline_viewer.py --root artifacts --viewer room_belief/t1_1_all13_t3_assumed_b_20261019_v2/viewer_package_v5/viewer --port 8783
```

URL: `http://127.0.0.1:8783/room_belief/t1_1_all13_t3_assumed_b_20261019_v2/viewer_package_v5/viewer/index.html`

1. Mở viewer; kiểm tra slider có 13 prefix và chọn được scope A/B cùng bốn variants.
2. Chọn A: thấy scope/membership/room score unavailable; không thấy cạnh/label B.
3. Chọn B: badge assumed và declaration ID hiển thị; xem frame 0, frame 61 và 110.
4. Chọn room:1 để thấy categories sofa/tv và contribution ledger; nhiều sofa vẫn chỉ một category contribution.
5. Kiểm tra room:2 abstain vì insufficient support, room:4 không có eligible categories.
6. Seek ngược từ frame 110 về frame 0 và xác nhận label/contribution tương ứng prefix, không giữ state tương lai.

## Interface tối thiểu đề xuất cho camera-live T4 (chưa triển khai)

Gọi một API duy nhất sau unified graph commit: `update_after_unified_commit(GraphCommitEnvelope)`. Input cần `run_id`, `frame_id`, `unified_revision`, object/room revision IDs + immutable snapshot URIs/hashes, prefix admission/consensus records, scope declaration/status và policy/A-variant IDs. Belief service giữ ledger theo object để retract, tập category-presence theo room (một contribution/category/room), cùng hysteresis state cho từng variant xuyên run. Output là semantic overlay gắn cùng `unified_revision` và input hashes, chứa memberships/status, eligible/excluded objects, categories, contributions, raw scores, beliefs, candidate/committed label và reason. Graph coordinator chỉ publish cặp graph+overlay atomically; nếu semantic update lỗi thì publish không có label mới với `semantic_status=failed/unavailable` hoặc fail toàn commit theo contract—không ghép graph mới với label stale. Terminal retract/finalize là revision riêng; không backfill final state vào prefix.

Room-function GT không có cho các 13 prefixes này; do đó accuracy/F1 không được báo. B là integration experiment dưới giả định được duyệt, không scope validation, không quality promotion và không thay default.

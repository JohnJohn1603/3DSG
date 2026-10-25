# Kế hoạch Frontier Exploration + 3D Scene Graph cho ObjectNav

Ngày: 24-10-2026. Baseline Git đã được tạo (`15b78ec`). Viewer atomicity, blocked-edge replan và shared shutdown deadline đã được sửa. OccuSG M2 compact response cùng binary v2 codec/C++ fixture có golden/replay parity. **Cập nhật:** binary thật hiện đã được nối vào OctoMapWorker; golden packet frame 146 và replay 9 keyframe có exact M2/M3 cùng room/object parity, sau đó combined live 120 s xác nhận cả primary/auxiliary worker báo `transport_mode=binary`. Những live run trước đó mang nhãn “binary” chỉ dựa vào biến môi trường và thực tế dùng ASCII; không dùng chúng làm bằng chứng binary performance. Combined live binary đầu tiên cải thiện graph age/M2 runtime nhưng chưa qua movement hoặc map freshness gate. Tám combined 120 s run trước đã hoàn tất trên spawn 0/2; sáu qua movement gate, nhưng quality gate (hai viewpoint cách nhau ≥`0,5 m`) mới đạt ở một run. Map cadence 2/3 Hz không cải thiện; với `nice=2`, run chẩn đoán mới giảm map-age p95 còn `799 ms`, policy stale waits còn 57/604 và graph-age p95/max `13,81/15,79 s`, song hai viewpoint đạt chỉ cách nhau `0,05 m`. Candidate telemetry cho thấy ở tick tới viewpoint thứ hai có phương án reachable cách `0,5 m` với path cost `0,4 m`; đây là bằng chứng để thử ranking ưu tiên đa dạng có kiểm soát, chưa đủ để kết luận nguyên nhân hoặc đổi policy mặc định. Replay catch-up 12 frame khớp room/object state hash tại committed frame 514 và tích hợp pending frame 592 trong session mới. Chưa đánh giá semantic/room accuracy bằng annotation độc lập.

**Cập nhật sau ablation:** hiện có chín combined 120 s run (các run trước đây được gắn nhãn “binary” nhưng thực tế gửi ASCII); sáu qua movement gate và chỉ một qua overall quality gate. Thử nghiệm radius 0,5 m chưa đánh giá được spatial ranking vì movement gate fail (`0,078 m`, 0 viewpoint), map-age p95 `1,252 s`, policy stale `304/528`, graph-age p95 `37,64 s`; bộ lọc chưa kích hoạt. Xem checkpoint mới nhất trong [implementation status](implementation_status.md). Tạm ưu tiên xác thực binary thật, giảm map/graph backlog và phục hồi freshness; chưa chạy soak.

Replay cùng ba packet live `0/72/208` cho thấy primary M2 wall `4,45 s` median; Python CPU `3,52 s`, native child CPU khoảng `2,63 s`, read/response wall `2,13 s`. Room/object state hashes ở frame 208 trùng tuyệt đối với live. Auxiliary M2 chỉ khoảng `0,36 s`. Giảm primary stride và gộp M3 từng đổi graph, vì vậy giữ nguyên tất cả mẫu. Thử A/B constructor `ScanPoint` với cùng packet và đổi thứ tự chạy xác nhận residual tạo điểm thấp hơn ở cả hai cặp, nhưng tổng M1–M2–M3 không cải thiện nhất quán; chưa coi đó là tăng tốc end-to-end. Combined ASCII 120 giây `native-live-scanpoint-trusted-ab-120s-20261023-a` **FAIL movement gate**: `0,1585 m`, 0 viewpoint; 95 controller + 260 policy NavMap-stale ticks, 114 map pending drops, capture `4,03 FPS`; trong 541 tick map đã khởi tạo, cả 93 controller stale ticks đều trùng lúc map worker pending; 7 paired graph revisions, graph cuối trễ 81 frame, RSS peak `3,65 GiB`. Hai control cùng scene/profile cho thấy navigation-only đạt `0,908 m`/4 viewpoint với 4 drops; P2 + graph demo đạt `0,485 m`/4 viewpoint với 9 drops. Diagnostics OccuSG frame 146 ghi primary M2 `13,04 s` trên `307.200` điểm. Các con số này là lịch sử trước binary transport. Binary input và exact replay parity sau đó đã hoàn tất; ba binary combined runs đã qua movement gate cơ bản và event-time freshness audit. Lặp spawn 2 xác nhận độ phủ gần viewpoint chưa đạt separation `0,5 m`; map-age p95 `1,014 s` (gần ngưỡng freshness 1 s), map drops counter tối đa `71`, policy NavMap-stale `238/580` tick. Vì vậy ưu tiên tiếp theo là giảm map backlog/stale waits và tăng tính đa dạng không gian của goal, giữ nguyên safety gates; sau đó mới chạy binary soak. Xem [implementation status](implementation_status.md), [M2 compact/debug parity](../runs/occusg-m2-compact-parity-frame146-20261024-a/report.json), [binary transport parity](../runs/occusg-m2-transport-parity-20261024-a/report.json), [spawn 0 movement/freshness](../runs/native-live-m2-binary-ab-120s-20261024-b/movement_gate.json), [spawn 2 lượt đầu](../runs/native-live-m2-binary-spawn2-120s-20261024-a/movement_gate.json), [spawn 2 lượt lặp](../runs/native-live-m2-binary-spawn2-120s-20261024-b/movement_gate.json), [spawn 2 freshness](../runs/native-live-m2-binary-spawn2-120s-20261024-b/navigation_freshness_audit.json), [spawn 0 catch-up](../runs/native-live-m2-binary-catchup-15frames-20261024-a/pending_catchup_audit.json), [spawn 2 lượt đầu catch-up](../runs/native-live-m2-binary-spawn2-catchup-14frames-20261024-a/pending_catchup_audit.json), [spawn 2 lượt lặp catch-up](../runs/native-live-m2-binary-spawn2-catchup-12frames-20261024-d/pending_catchup_audit.json), [10-minute two-spawn soak cũ](../runs/native-soak-10m-multispawn-20261023.json).

**Trạng thái mới nhất:** sau khi binary thật được xác nhận, combined run `native-live-m2-binary-spawn2-nice5-120s-20261024-a` giữ nguyên cấu hình/gates nhưng nâng nice của graph worker lên 5. Movement gate cơ bản đạt (`0,350 m`, hai goal chạm), spatial quality fail vì hai viewpoint chỉ cách `0,05 m`. Map-age p95 `0,850 s`, map-drop counter `32`, stale command `23`; đây là tín hiệu tốt từ một cặp run tuần tự, chưa đủ kết luận nhân quả. Hai shutdown-pending frame (`617`, `622`) đã được replay binary và catch-up audit pass trên chuỗi 16 frame; room/object hash khớp ở prefix frame 507, auxiliary depth được dùng. Recovery đạt, live shutdown vẫn chưa ack hai frame và cần đo shutdown riêng. Chi tiết ở [implementation status](implementation_status.md). Chưa chạy soak.

**Bổ sung mới nhất (24-10-2026):** lặp spawn 2 có hai lượt combined 120 s qua movement/spatial gate nhưng freshness chưa hoàn hảo; spawn 0 holdout fail quality với `270/603` policy NavMap-stale waits và graph-age p95 `22,94 s`. Randomized ray-chunk replay trên hai snapshot không cho thấy chunk size mới cải thiện ổn định; traversability/floor/clearance parity giữ nguyên. Telemetry pha online trong run chẩn đoán 60 s xác nhận ray integration chiếm phần lớn service time: median/p95 wall `160/531 ms`, thread CPU `52/87 ms`; ở cửa sổ nặng pending ratio `0,97`, ray wall p95 `695 ms`, map stale `71/85` ticks. Đây chưa phải movement acceptance và chưa tách được OS scheduler, GIL hay native thread contention. Tiếp theo đo A/B cùng spawn/profile khi bật/tắt native graph, không đổi semantics/cadence/safety; rồi chỉ tối ưu ray path nếu packet replay chứng minh được cải thiện exact-parity. Chi tiết và artifacts ở [implementation status](implementation_status.md). Không chạy soak 10 phút khi spawn holdout/freshness còn fail.

**Kết quả lặp mới nhất:** run nice=5 thứ hai `native-live-m2-binary-spawn2-nice5-repeat-120s-20261024-b` không tái hiện freshness tốt: movement `0,156 m` (FAIL), map-age p95 `1,347 s`, drops `108`, stale commands `103`, graph-age p95/max `14,014/18,507 s`. Một pending frame 523 được binary replay thành công; shutdown kéo dài khoảng `8,29 s` dưới timeout 30 s nhưng đã preserve frame để catch-up. Do đó nice=5 chưa ổn định, không chọn mặc định; tiếp tục điều tra map contention và goal diversity.

**Ablation ưu tiên mapper:** hạ riêng ConceptGraphs/OccuSG xuống nice=5, giữ P2 mặc định, không cải thiện exploration: `0,157 m`/0 viewpoint, map-age p95 `1,074 s`, 79 drops, 326 policy stale waits; graph-age p95/max tăng lên `26,279/32,573 s`. Không dùng niceness làm fix; cần phân tích queue theo cửa sổ thời gian và tối ưu runtime đã định lượng.

**Profile theo cửa sổ:** bốn binary run cho thấy từ khoảng giây 40, NavMap worker thường pending `0,85–1,0`, map service p95 `0,6–0,96 s` so với submit cadence `0,2 s`, rồi drops và stale waits tăng. Grid chỉ tăng vừa phải. Đã thêm thread CPU và queue timing; run profile mới đo wall p50/p95 `120/456 ms` so với worker-thread CPU `48/84 ms`, queue wait p95 `39 ms`, submit→publish p95 `486 ms`. Chưa phân biệt được scheduler/GIL wait với native NumPy threads. Run đạt movement (`0,345 m`) và map freshness p95 `768 ms`, nhưng spatial quality vẫn fail. Chi tiết/catch-up ở [implementation status](implementation_status.md). Chưa chạy soak.

**A/B tải 24-10 — chưa kết luận:** cặp đầu (combined rồi nav-only) cho update p95 `700/410 ms`; cặp đảo thứ tự cho `140/38 ms`. Cả cặp đều ghi combined chậm hơn, nhưng độ chênh thay đổi mạnh giữa các lượt tuần tự; tải/thermal host chưa được ghi nên chưa thể tách tác động P2+graph. Map-age p95 ở combined của lượt đảo `503 ms`, policy stale `1/428`, graph-age p95 `7,81 s`; có 2 frame pending lúc shutdown. Bổ sung host CPU/load telemetry trước khi tiếp tục A/B hoặc chọn tối ưu. [Bảng 4 lượt](../runs/native-live-map-phase-combined-spawn0-reverse-60s-20261024-a/diagnostics/ab_order_reversal_summary.json).

**Checkpoint mới nhất 24-10:** combined 120 s trên spawn 0 nay pass movement/spatial gate (`0,850 m`, 5 arrivals, ≥2 viewpoint cách ≥`0,5 m`) và NavMap freshness (p95 `437 ms`, 1 stale tick, 0 drops); native graph p95 age `7,86 s`, 18/18 paired multisensor records dùng binary M2 cả hai sensor. Shutdown có một pending frame 841; replay + 19-frame catch-up đạt exact room/object state hash tại committed frame 797, auxiliary depth có mặt và worker exit 0. Đây xác nhận recovery và một run acceptance, chưa xác nhận semantic graph accuracy hoặc soak. Artifact/các giới hạn xem [implementation status](implementation_status.md). Kế tiếp là lặp fixed combined ở các spawn đã chọn và chuẩn bị annotation độc lập cho object/room/membership trước soak 10 phút.

## Trạng thái triển khai hiện tại

- Cập nhật mới hơn checkpoint 8-run bên dưới: đã thử profile opt-in `visited_viewpoint_radius_m=0,5`. Run không đạt movement (`0,078 m`, 0 viewpoint), map-age p95 `1,252 s`, policy NavMap-stale `304/528`, graph-age p95 `37,64 s`; spatial filter chưa kích hoạt nên chưa đánh giá được hiệu quả. Không đổi policy mặc định. Workspace suite hiện **89 passed**; phục hồi freshness/backlog trước lần ablation kế tiếp.
- Checkpoint hiện tại: binary stack trên ConceptGraphs + OccuSG đạt movement threshold ở một số spawn, nhưng quality gate độc lập yêu cầu ≥2 viewpoint cách nhau `0,5 m`; chỉ một trong tám combined runs hiện đạt overall gate. Map cadence 2/3 Hz không cải thiện freshness. `nice=2` giữ graph-age dưới 20 s; run chẩn đoán mới đạt map-age p95 `799 ms`, 57 policy NavMap-stale waits và graph-age p95/max `13,81/15,79 s`, nhưng hai viewpoint đạt chỉ cách nhau `0,05 m`. Candidate telemetry ghi tối đa 8 reachable alternatives: tại tick tới viewpoint thứ hai, một goal cách `0,5 m` có path cost `0,4 m`, còn goal gần được báo reached; cần thử spatial-diversity ranking opt-in trước khi thay mặc định. Catch-up replay 12 frame pass với hash phòng/object khớp tại committed prefix và auxiliary depth ở pending frame. Evaluator có ba kết quả riêng: movement, spatial coverage, overall; free-cell gain/path length là progress diagnostics, không phải semantic ground truth. Workspace suite: **87 passed** với Qt offscreen; M2/M3 regression: **16 passed**. Binary vẫn opt-in. Chưa có accuracy annotation hoặc soak 10 phút trên binary stack. Tiếp theo chạy một ablation ranking có diversity với safety/freshness không đổi, rồi một 120 s combined run cố định trước khi xét soak.

- Đã sửa loader hash navmesh, frame index của producer, depth safety gate, path remap khi grid đổi origin, lookahead và thời hạn lệnh qua pipeline map/planner đồng bộ.
- YAML profile đã được kiểm tra và nối vào runtime; thêm initial scan tại chỗ, recovery xoay giới hạn sau va chạm, map ray integration theo batch NumPy.
- Regression fixtures theo các mốc trước lượt P2 correction: navigation suite ban đầu `18 passed`; sau graph bridge suite `20`, baseline đầu lượt này `23 passed`. Sau P2 collision/freshness correction là `26 passed`; sau vector hóa viewpoint search là `27 passed`; planner-worker prototype là `28 passed`; sau async Map/planner, stale-result rebase, world-space goal, queue-drop và NavMesh vertical-projection regression là `32 passed`; sau validated-start-anchor full suite `34 passed` (Python 3.12.3, NumPy/fake-session fixtures). Habitat smoke dùng Habitat-Sim 0.3.3 trên llvmpipe.
- Các run async cũ ở stride 4/8 từng fail vì freshness/backpressure; sau ray aggregation, validated-start-anchor và worker tuning, run gate gần nhất ghi `nav_stale` rất ít và không drop map frame. Blocker hiện chuyển sang navigation sau scan: `no_valid_path` kéo dài và reachable component có thể co về một cell. Benchmark offline không thay thế online acceptance.
- Multi-snapshot profiling qua 8 saved runs (7 lặp mỗi map) ghi ở `runs/navmap-profile-20261022.json`: NavMap stride 4 replay median-of-medians `199 ms` (range `190–228`), stride 8 `50 ms` (`48–62`); known-free flood `71 ms`, frontier extraction `98 ms`, A* connected maps `3,2–3,4 ms/goal`. Online llvmpipe run stride 4 vẫn chậm hơn rõ (`741/882 ms` median/p95), cho thấy contention runtime khuếch đại chi phí mapper.
- Run stride 8 `p2-async-map-plan-stride8-120s-20261022-a`: 120,27 s, scan complete, 9–13 frontier, 5 goal IDs nhưng `241 nav_stale`, 92 pending map drops, sáu filter blocks, chỉ 5 movement pulses / `0,040 m`, không viewpoint reached. Hậu kiểm 6/6 block là sai khác endpoint theo trục đứng `1,1–1,8 mm` dù NavMesh xác nhận navigable; executor nay phân biệt ngang/đứng và model-free suite đạt `32 passed`. Giữ artifacts, không chạy lại trong pass này.
- Multi-snapshot replay cho thấy mapper stride 4 tốn khoảng `199 ms` median-of-medians qua 8 saved run, stride 8 khoảng `50 ms`; flood-fill/frontier/A* thấp hơn, dù connected-map flood-fill có lúc ~`85 ms`. Validation sau endpoint correction (`p2-stride8-navmesh-y-fix-120s-20261022-a`) không có filter blocks và chỉ 2 `nav_stale`, nhưng dừng sau 10 bước nhỏ: displacement `0,079 m`, 0 reached viewpoint, 554 tick `robot_not_on_observed_free_cell`. Pose cell cuối FREE nhưng footprint erosion không an toàn do evidence lân cận thiếu. P2 vẫn chưa đạt; không nới unknown/footprint policy.
- Validated-start-anchor regression chứng minh chỉ ô pose hiện tại đã được host xác nhận mới được dùng làm search seed; successor vẫn cần footprint-traversable, unknown và corner-cut rule giữ nguyên. Full suite `34 passed`. Run `p2-start-anchor-120s-20261022-a`: 120,28 s / 720 frames, scan xong, 9 frontier / một goal; không còn `robot_not_on_observed_free_cell`, nhưng chỉ 9 movement pulses, net displacement `0,071 m`, 0 viewpoint reached và 299 tick `nav_stale`. Trên 269 map integrations, worker p50/p95/max `201/872/1077 ms`; queue pending-drop counter tối đa 125; stale planner result 145 tick. P2 fail, artifacts giữ nguyên; blocker hiện là mapper freshness/backpressure dưới Habitat llvmpipe.
- Tối ưu tích hợp ray bằng cách gộp các lần chạm trùng cell trước khi cập nhật odds/evidence. Trên 9 saved RGB-D tails × 5 replay stride 8, thời gian update median giảm `61,5→42,0 ms` (1,46×); state, floor, clearance và footprint-traversable trùng, max log-odds delta `4,8e-6`. Trên 8 snapshot × 7 lần lặp, stride 4 median-of-medians giảm `199,1→126,2 ms` (1,58×), stride 8 `49,7→30,7 ms` (1,62×). Artifact mới: `runs/navmap-profile-after-ray-aggregation-20261022.json`.
- Episode P2 duy nhất sau tối ưu (`p2-ray-aggregation-120s-20261022-a`) vẫn fail: 120,31 s / 710 frames, scan xong, 10–13 frontier / một goal, 10 movement pulses, net `0,079 m`, 0 viewpoint reached, 0 collision/filter block. Có 216 `nav_stale`, 150 lệnh `no_valid_path`; map worker p50/p95/max `186/711/904 ms`, planner worker `571/649/764 ms`, pending-drop counter tối đa 118. Ray aggregation cải thiện offline/runtime map latency nhưng không đủ để qua freshness/locomotion gate; giữ artifacts và dừng trong pass này.
- Thêm telemetry lý do revalidation fail và chạy đúng một navigation-only episode mới `p2-path-revalidation-telemetry-120s-20261022-a`: 120,32 s / 722 frame, scan xong, 9 frontier / một goal, 9 movement pulses, displacement `0,0714 m`, 0 viewpoint reached, không collision/filter block. Có 213 `nav_stale`, 161 `no_valid_path`; lần đầu revalidation trả `path_blocked_by_new_map` tại tick 350, tổng cộng 3 lần. Map worker p50/p95/max `190/723/862 ms`, planner `543/622/662 ms`. P2 vẫn fail; giữ artifacts và không chạy thêm simulator trong pass này. Chưa rõ revalidation bị chặn bởi prefix đã đi qua hay đoạn phía trước.
- Sửa revalidation: khi có pose hiện tại, remap đích world-space rồi chạy A* từ pose hiện tại trên snapshot mới; không bắt các cell path phía sau robot còn traversable hoặc vẫn nằm trong map window. Regression bao phủ prefix đã đi qua bị occupied và prefix nằm ngoài map bounds trong khi current pose/goal vẫn có mặt. Full suite hiện `38 passed`.
- Episode kiểm chứng duy nhất sau sửa `p2-current-pose-replan-120s-20261022-a`: 120,14 s / 801 frame, scan xong, 11 frontier ban đầu / một goal, 16 move pulses, không collision/filter block, displacement `0,127 m`, 435 `no_valid_path`, 4 `nav_stale`. Không còn `path_blocked_by_new_map`; revalidation một lần trả `no_known_free_path` ở tick 366. Mapper p50/p95/max `159/464/652 ms`, planner `260/512/710 ms`, pending drops tối đa 6. P2 vẫn **fail** (>0,2 m + hai viewpoints). Hậu kiểm final map có đúng 1 reachable cell ở pose robot; ba frontier cuối đều chọn viewpoint trùng cell pose, path độ dài 0. Không chạy thêm simulator trong pass này.
- Offline audit trên 8 saved maps (`scripts/audit_navmap_footprint_gaps.py`, artifact `runs/navmap-footprint-gap-audit-20261022.json`) tìm thấy cùng floor-support hole ở `(0,875; 4,575)` trên cả stride 4 và 8: state UNKNOWN, floor support 0, clearance evidence dương, occupancy odds `-5`. Ở map mới nhất cell này chặn footprint các bước cardinal; chỉ một what-if offline (không lưu/không đổi state) làm reachable component từ 1 lên 11.937 nếu coi cell là free.
- Follow-up coverage chiếu depth frame cuối của 14 run qua đủ 64 tổ hợp pha sampling stride 8: không có floor-return ray nào raster qua cell trên. Ở frame mới nhất, cell lệch `74,8°` khỏi camera forward, ngoài FOV ngang 90°. Điều này không xác nhận sampling phase là nguyên nhân và không cho biết initial scan từng thấy cell chưa, vì chỉ RGB-D frame cuối mỗi run được lưu. Đã thêm recorder vào runner: lưu tối đa một cặp RGB-D cho mỗi sector yaw 30° (12 cặp, payload tối đa 32 MiB) trong `runs/<run>/diagnostics/initial_scan/`, manifest ghép frame/pose/intrinsics/floor/revision. Ghi file diễn ra sau khi lệnh bước tương ứng đã được gửi tới simulator để không làm lệnh hiện tại hết hạn; chi phí capture có thể làm chậm tick kế tiếp và cần đo. Capture chưa được kiểm chứng bằng Habitat. Bước kế tiếp là xem coverage history từ artifact đó trước khi sửa mapper; giữ nguyên unknown/footprint/freshness gates.
- Fresh benchmark trên 14 snapshot × 7 lần lặp (`runs/navmap-profile-gate-anchor-20261022.json`) đã sửa để bật validated-start-anchor đúng như Habitat host: median qua median/snapshot là NavMap stride 4 `98,0 ms` (79–103), stride 8 `26,1 ms` (21–32); known-free flood `40,4 ms`, frontier extraction `75,4 ms`, footprint flood `8,1 ms` (map connected ~6,4K cells), A* `1,95 ms/goal` median trên 9 connected maps. Các số thấp hơn 1 s freshness limit; map/planner worker hiện đã tách, snapshot immutable và safety gate kiểm tra dữ liệu mới. Đây là offline timing, không thay online llvmpipe gate.
- Một navigation-only gate run mới `p2-gate-120s-20261022-a` chạy đủ 120,08 s/1042 frame nhưng P2 fail: displacement `0,143 m`, 18 forward actions, 0 viewpoint reached, 691 `no_valid_path`, 2 `nav_stale`, 0 collision/filter block, 0 pending map drops. Map worker p50/p95/max `38,7/181/258,8 ms`, planner `156,9/168,8/307,2 ms`, total tick p95 `7,3 ms`. Final map có 1 reachable cell và 3 frontier trùng viewpoint. Không bật graph/P2.
- Habitat exact-pair diagnostic `scan-pair-diag-35s-20261022-a`: 35,13 s / 349 frame; initial scan xong khoảng giây 29, có 9 frontier và 9 viewpoint. Dịch chuyển từ spawn `0,095 m`, nên đây chỉ là chẩn đoán ngắn, không phải gate 120 s. Recorder lưu 12/12 frame và cả 12 được NavMap tích hợp; audit đủ pixel đếm 54.829 floor-ray samples nhưng không ray nào đi qua ô `(0,875; 4,575) m`, vẫn UNKNOWN với `floor_support=0`. Final map vẫn có 6.509 footprint-reachable cells / 9 unique viewpoints, nên ô này chưa giải thích riêng việc connectivity sụp ở episode dài. Map worker p50/p95/max `32,6/101/189 ms`, không drop frame. Trace P2: reachable giữ quanh 6.405 khi robot tới `(0,982;4,607)` m; robot sau đó dừng quanh `(0,917;4,640)` m, rồi ở frame 460 reachable chuyển về 1 dù pose không đổi và mapper vẫn tích hợp. Snapshot lịch sử thiếu nên chưa biết thay đổi map cell nào làm đứt đường. Đối chiếu/replay vùng tick này trước; chưa chạy lại 120 s hay thay safety semantics. Artifacts: [coverage](../runs/initial-scan-coverage-scan-pair-diag-35s-20261022-a.json), [footprint audit](../runs/navmap-footprint-gap-scan-pair-diag-35s-20261022-a.json).
- Recorder thử nghiệm cũ thu 12/12 yaw sector (`25,8 MB`), nhưng chỉ 3/12 frame khớp NavMap as-of stream. Runner đã đổi sang cache scan frames và chỉ lưu khi snapshot `as_of_frame_id` bằng chính xác RGB-D `frame_id`; regression full suite `41 passed`, compile checks sạch.
- Static PathFinder probe trên navmesh khóa hash xác nhận cell/điểm cardinal lân cận navigable và có đường cuối-pose→spawn geodesic `0,127 m`. Đây là bằng chứng nghiêng về thiếu floor observation trong NavMap thay vì vật cản thật; không dùng NavMesh để thay exploration planner, không free hóa cell và không nới unknown mask. Chưa có lịch sử RGB-D/map revision để khẳng định vì sao floor ray không chạm cell.
- Desktop vertical slice đã được thêm: PyQt5 3-panel shell, file protocol phiên bản `3dsg-live-state/v1`, RGB-D/NavMap publisher, 3D RGB-D cloud/graph overlay và Start/Pause/Resume/Stop cho runner. Habitat smoke 30 giây (`desktop-live-smoke-20261021-a`) đã đưa RGB-D/NavMap thật vào stream; integrated 120 giây bên dưới xác nhận graph và frontier cùng chạy. UI đã render offscreen, chưa xác nhận thao tác trực tiếp trên desktop trong episode đang chạy.
- API module `3dsg-objectnav-module/v1` tách task/observation cùng ports navigation, perception, graph và target selection khỏi Habitat. Habitat launcher nay có tùy chọn P2 riêng, worker persistent chạy trong env ONNX của dự án cũ, keyframe scheduler/store và graph prefix publisher. `RGBDObjectGraphSession` lift mask qua depth, duy trì object hypotheses và `observed_from`; `FrontierNavGraphSession` đưa robot/frontier/goal vào graph. Đây là backend demo provisional, không tuyên bố parity ConceptGraphs/OccuSG, room membership hay object identity đã được xác nhận.
- Desktop checkbox bật provisional RGB-D graph mặc định; `--navigation-only` tắt detector. Python worker path được giữ nguyên để venv symlink vẫn dùng site-packages. Khởi tạo worker có cleanup ngay cả khi lỗi trước khi episode loop bắt đầu.
- Một smoke P2 đầu tiên phát hiện hai lỗi tích hợp thật: Python venv symlink bị `Path.resolve()` bỏ qua và `event` provenance trùng trường event journal; cả hai đã sửa. Run `p2-rgbd-graph-smoke-20261021-c` hoàn tất: 30,3 s, 86 camera ticks, 5/5 admitted keyframes processed, 5 graph revisions, final 17 nodes/23 edges, worker shutdown sạch. Offscreen desktop đọc được frame 85, NavMap `222x122`, revision 5 và render 1.163 cloud voxels. Đây chỉ là workflow smoke: không có reachable frontier/movement trong run và object predictions đều provisional.
- Sau khi tách composition sang factory plugin, `graph-plugin-smoke-20261021-d` chạy 12,1 s, 47 ticks, xử lý 3/3 keyframes và publish 3 revisions; final 13 nodes/14 relations, không worker error. Run `integrated-graph-frontier-120s-20261021` xác nhận manifest ghi plugin/P2 config: 120,2 s, 378 ticks, initial scan xong ~70 s, sinh 4–11 frontier, vào MOVE và tới RECOVERY/no-valid-path sau 1 filter block. Có 2 bước tiến, một goal ID, chưa reached; net translation chỉ ~1,6 cm. Graph xử lý đủ 32/32 keyframes, 32 revisions, 68 nodes/130 relations, drain sạch. Đây là kết quả phối hợp thật nhưng frontier locomotion gate vẫn fail.
- API này giữ ranh giới extension rõ: Habitat chỉ host hiện tại; worker perception, navigation, scene graph và target selector đều có thể thay qua port/transport. Graph runtime được tách thành plugin `module:factory` (`--graph-plugin`); `--enable-graph` cho phép graph plugin khác chạy mà không khởi động P2. Backend hiện tại là cầu nối demo để xem graph dần, trong khi adapter chính thức ConceptGraphs/OccuSG vẫn là việc tiếp theo.
- Tiếp tục P2 offline trên snapshot navmap cuối: đã vector hóa tìm viewpoint annulus theo lô với kiểm thử parity nested-loop; cùng snapshot giữ nguyên 11 frontier ID/gain/viewpoint, thời gian `extract()` đo lặp giảm từ `614–753 ms` xuống `154–183 ms`. Full model-free suite `27 passed`. Đây chưa phải benchmark realtime hay bằng chứng locomotion; Habitat không được chạy lại trong checkpoint này. Flood-fill/connectivity là hotspot tiếp theo theo cProfile.
- Thêm prototype `LatestPlanWorker` qua `async_planning` profile: planner xử lý snapshot read-only ở worker đơn, bounded, kết quả sai map revision bị loại; depth/map freshness vẫn được kiểm tra ngay trong tick điều khiển. Model-free suite `28 passed`; chưa chạy Habitat nên chưa có bằng chứng P2/freshness sau tách planner. NavMap update và Habitat capture vẫn đồng bộ, chưa gọi là camera độc lập/realtime.
- Một navigation-only Habitat run kiểm tra planner worker trước safe-rebase logic (`p2-async-planner-120s-20261022-a`): 120,48 s, 354 frame; 38 lệnh `depth_stale`, 14 `no_valid_path`, không goal/movement/collision. NavMap median/p95 `232/310 ms`, capture interval `332/536 ms`, planner output stale revision 43/44 lần. Final map khi offline có 6.458 reachable cells và 10 frontiers; live không nhận frontiers vì policy worker result bị loại. Không retry episode.
- Từ evidence đó, map integration cũng được chuyển vào `LatestMapWorker` bounded FIFO/latest pending; mapper single-writer xuất immutable snapshot hoàn chỉnh. Planner worker có thể nhận target từ revision gần nhất nhưng runtime kiểm tra age/scope/geometry rồi A* replan từ robot pose hiện tại trên current map; stale observation-only result bị bỏ. Viewpoint world XY giữ đúng khi origin dịch. CPU profile bật `async_mapping` + `async_planning`; worker map drops, latency, pending, error và planner stale/rebase/error được ghi. Shutdown drain trước final map. Model-free suite `31 passed`. Habitat run `p2-async-map-plan-120s-20261022-a` đã chạy 120,37 s / 857 tick nhưng fail freshness/frontier: 633 `nav_stale`, 249 pending map frames bị drop, 0 frontier/goal/movement. Mapper latency median/p95/max `741/882/1119 ms`; controller/depth p95 khoảng 8 ms. Đã chuyển profile thử `depth_stride=4→8` để giảm tải tia, nhưng coverage chưa được xác nhận; giữ nguyên artifacts và không retry trong pass này.
- Offline diagnostic của final map cùng run: 12.271 cell free / 767 occupied; có 1.730 raw frontier cells nhưng chỉ 1 frontier component và 1 footprint-reachable free cell. Initial scan không hoàn tất do stale map; stride 8 là thử nghiệm kế tiếp, chưa xác nhận độ phủ.
- Smoke 30 giây (`navigation-smoke-30s-scan2`): 106 tick, quay thực khoảng 127°, 132→645 ô free, 0 collision; chưa đủ 360° scan.
- Smoke 120 giây (`navigation-smoke-120s-scan1`): 390 tick, scan đủ 360° sau khoảng 95 giây, 0 collision; không tìm thấy reachable frontier và agent chưa tịnh tiến. Tốc độ xử lý xấp xỉ 3,2 tick/s ở map lớn, thấp hơn mục tiêu camera 25 FPS.
- Chẩn đoán trước sửa floor-ray (`navigation-diagnostic-30s-20261021`): tới 644 ô known-free và 528 raw frontier cells nhưng chỉ 1 reachable free cell sau inflation footprint. Snapshot grid được lưu từ run mới để hậu kiểm.
- Sửa NavMap: floor return nay đóng góp corridor evidence 2D dọc tia không bị che; thêm fixture xác nhận đường free liên tục. Nửa cell inflation margin (`0,025 m`) cho phép erosion phù hợp với độ phân giải 5 cm mà vẫn giữ navmesh radius 10 cm.
- Smoke sau sửa (`navigation-margin-halfcell-120s-20261021`): scan 360° hoàn tất, sinh 1–13 frontier; runtime vào MOVE 27 tick, có 4 bước `move_forward` (mỗi bước 8 mm) rồi collision filter chặn một bước và runtime vào RECOVERY. Tổng một collision/filter block, không có hành động đi xuyên vật cản; agent chỉ dịch chuyển khoảng 3,3 cm nên P2 vẫn **chưa đạt** yêu cầu di chuyển có tiến triển và xác nhận ít nhất hai goal. Recovery chiều quay đã được sửa theo hướng ngược lệnh yaw trước va chạm, có fixture; cần smoke xác nhận thêm sau lần sửa này.
- Thêm collision evidence vào NavMap, cooldown theo ID goal và cooldown theo vùng va chạm để tránh candidate mới có ID khác nhưng cùng hướng bị chặn. Các run navigation cũ giữ P2 locomotion gate fail. Integrated graph+frontier run nay cũng vào MOVE, nhưng ghi 1 filter block, chỉ 2 lệnh tiến, 1 goal ID và 1,6 cm displacement; chưa tới goal. Graph đã nối để quan sát đồng thời, nhưng demo ObjectNav chỉ được nghiệm thu sau khi movement gate >0,2 m và ít nhất hai reached viewpoints được xác nhận.
- Collision-filter cells nay được giữ occupied qua các lượt tích hợp depth tiếp theo; fixture xác nhận free-ray odds không xóa chúng. Run Habitat `navigation-sticky-collision-120s-20261021` xác nhận còn 1 filter block thay vì 3, sau đó agent tránh lặp lại hướng đó nhưng planner báo `no_valid_path`. Net displacement vẫn 3,2 cm; P2 **chưa đạt**. Cần sửa khả năng lập đường qua vùng quan sát chưa đủ/đối chiếu NavMap với collision filter, rồi mới xác nhận đủ tiến triển và hai goal.
- Reachability flood-fill đã được đổi để cấm đi chéo cắt góc như A*; policy từng ghi rõ khi cooldown tạm loại hết candidate. Tại mốc run `navigation-corner-reachability-120s-20261021`, suite là `23 passed`; run chỉ tạo 1 goal ID, ghi 1 filter block và 4 bước tiến (net 3,2 cm). Hậu kiểm khi đó cho thấy footprint-safe component 3 cell. Đây là lịch sử trước P2 correction; root cause collision occupancy/cooldown được xử lý trong checklist dưới đây. Movement acceptance vẫn **chưa đạt**.
- Sweep offline trên snapshot cho thấy vùng footprint-safe connected nhảy từ 3 cell tại margin 1,5 cm lên 6.687 cell tại margin ≤1 cm; tuy nhiên smoke `navigation-margin-1cm-120s-20261021` ghi 2 filter block, 2 bước tiến và chỉ 1,6 cm net displacement. Vì margin thấp không cải thiện chuyển động và làm tái xuất hiện block, profile đã trả về 2,5 cm; không xem sweep offline là bằng chứng an toàn.
- Run artifacts nằm dưới `runs/` (được ignore khỏi source control); run ID mới được dùng cho mỗi lần chạy.

### Checklist P2 hiện tại

- [x] Regression model-free sau các chỉnh sửa gần nhất: `41 passed` (`tests/test_navigation_regressions.py`). Một episode navigation-only 120 s `nav-only-post-live-120s-20261023-a` hoàn tất: displacement ròng `0,90949 m`, 2 viewpoint duy nhất, 75 forward actions; evaluator PASS, RSS peak `1,483 GiB` dưới cap `6 GiB`. Có 3 collision/filter blocks và 2 `nav_stale`; hai viewpoint chỉ cách nhau khoảng `7 cm`, nên gate số lượng đạt nhưng độ phủ không gian còn cần tiêu chí độc lập.
- [x] Audit live native run do người dùng khởi chạy `my-native-live-002`: RGB 873, NavMap 871, graph revision 9/frame 817; 9/9 graph publications có frame/revision khớp paired commit và hash RGB/depth đúng state. Đây là cập nhật live theo frame, nhưng không đạt drain sạch: graph cuối chậm 56 frame, worker lỗi ở frame 873 và episode báo `budget_exhausted;flush_timeout`. Movement evaluator riêng PASS `1,634 m`/4 viewpoint. Không dùng run này làm bằng chứng 10-minute readiness.
- [x] Nối auxiliary depth vào capture record và adapter native OccuSG: lưu file riêng có intrinsics, pose, range, calibration ID/hash và file hash; tích hợp ray riêng với sensor origin riêng. Replay thật `runs/native-aux-depth-replay-my-native-live-002-frame13-20261023-f/` qua ConceptGraphs + OccuSG pass với paired ack/finalize. OccuSG audit ghi `auxiliary_sensor_integrated=true`, 19.200 điểm, frame 13 identity/hash khớp; ConceptGraphs vẫn dùng camera RGB-D chính. Đây là kiểm chứng plumbing trên một frame, chưa đánh giá ảnh hưởng room accuracy.
- [x] Kiểm chứng bounded native shutdown trên `native-live-close-policy-30s-20261023-a`: FINISHED, stop action OK, graph worker failure null; 3/3 paired revisions đã publish (frame 0/112/175), in-flight hoàn tất trong flush window, 2 pending keyframes 184/198 được ghi rõ và giữ trên đĩa để replay. RSS peak `3,595 GiB` dưới cap `6 GiB`; live RGB frame 198, NavMap 196, graph frame 175/revision 3; file hashes hợp lệ. Đây là graceful close trên một run ngắn; chưa inject process crash hoặc xác nhận memory slope/10 phút đa scene.
- [x] Native live 120 s sau adapter multisensor: `native-live-multisensor-120s-20261023-a` đạt movement gate `1,9715 m`/3 viewpoint, 0 collision/filter blocks, 4 controller `nav_stale` stops. Đạt 11 paired revisions; 11/11 OccuSG audit rows xác nhận auxiliary depth integrated với identity/calibration/hash khớp. Kết thúc `FINISHED`, graph worker failure null; 1 pending durable keyframe 791 được giữ để replay. RSS peak `3,723 GiB`/cap `6 GiB`, MemAvailable min `4,240 GiB`. Hash audit pass; RGB/NavMap/graph cuối 796/794/779 (revision 11), lag 17 camera frames. Đây là native integrated acceptance; room/object accuracy vẫn chưa được đánh giá.
- [x] Tổng soak 10 phút trên hai spawn cùng scene HM3D 00802: `native-soak-spawn1-300s-20261023-a` + `native-soak-spawn2-300s-20261023-a`, aggregate elapsed `610,66 s`. Cả hai run `FINISHED`; movement gate pass lần lượt `1,433 m`/7 viewpoints và `0,878 m`/2 viewpoints. Graph paired 23+20 revisions; aux-depth integration 23/23 + 20/20; graph workers close sạch và pending persisted keyframes được liệt kê. RSS peak `3,787`/`3,946 GiB`, cap `6 GiB`; MemAvailable min `4,960`/`4,672 GiB`. RSS median mỗi phút tăng khoảng `0,3 GiB` tới plateau. Tuy vậy có tổng 29 collision/filter blocks, 8+95 controller nav-stale stops, 44+221 policy nav-stale waits; camera/graph lag cuối 104/93 frames. Soak artifact: `runs/native-soak-10m-multispawn-20261023.json`. Đây chỉ là resource/worker soak trên hai spawn cùng scene, không nghiệm thu movement quality hay frame-freshness.

- [x] Xác nhận root cause collision evidence: filter reject một target tâm robot cách 8 mm, cùng cell 5 cm với spawn; code cũ đẩy nó 15 cm rồi ép occupied, và spatial cooldown 0,75 m có thể loại mọi frontier. Cell tương ứng trong map cuối có floor/clearance evidence mạnh và log-odds `-5`, nhưng sticky collision flag vẫn giữ OCCUPIED.
- [x] Sửa collision-filter center không còn ghi occupancy; depth ray vẫn tạo obstacle evidence. Bỏ cooldown vùng rộng; chỉ cooldown frontier ID thất bại.
- [x] Regressions trước sửa thất bại, sau sửa model-free suite `26 passed`; freshness được kiểm tra lại sau synchronous planning và action hết hạn theo evidence timestamp.
- [x] Chạy đúng một navigation-only episode 120 s, không P2/graph worker: `p2-nav-collision-contract-120s-20261021-a`, 519 frames. Không có bước tiến/goal reached; P2 acceptance vẫn **chưa đạt**.
- [x] Offline probe NavMap cuối: frontier extraction `382–440 ms`; A* cho 11 candidates `38–50 ms`. Hotspot hiện rõ nhất là extraction/viewpoint generation; đây chưa phải realtime benchmark.
- [x] Profile 24 snapshot và vector hóa viewpoint generation có parity; full model-free suite sau các sửa đổi là `30 passed`.
- [x] Thêm bounded async NavMap + planner workers, immutable snapshot publication, current-pose/current-map A* replan và giữ nguyên freshness gate.
- [x] Navigation-only worker experiment `p2-async-planner-120s-20261022-a` hoàn tất nhưng fail: 38 `depth_stale`, 14 `no_valid_path`, không movement; nó chạy trước async NavMap và safe rebase.
- [x] Async Map+planner run stride 4 `p2-async-map-plan-120s-20261022-a` chạy 120 s nhưng fail freshness/frontier; 633 stale tick, 249 pending drops, không movement.
- [x] Multi-snapshot benchmark qua 8 saved run, 7 lặp/map: NavMap stride 4 median ~199 ms, stride 8 ~50 ms; flood-fill/frontier/A* cũng được đo. Artifact JSON được giữ trong `runs/navmap-profile-20261022.json`.
- [x] Navigation-only stride 8 `p2-async-map-plan-stride8-120s-20261022-a`: frontier có, nhưng 0,040 m displacement do false NavMesh vertical-projection filter; sửa endpoint kiểm tra ngang/đứng riêng, regression suite hiện `32 passed`.
- [x] Post-fix run `p2-stride8-navmesh-y-fix-120s-20261022-a`: không filter block, map age đa số fresh, nhưng agent dừng sau 0,079 m vì current pose không footprint-traversable theo observed map; giữ artifacts, không retry pass này.
- [x] Thêm validated-start-anchor hẹp; regression giữ nguyên unknown/footprint successor và corner-cut. Full suite `34 passed`.
- [x] Episode `p2-start-anchor-120s-20261022-a`: scan/frontier/goal thành công nhưng fail P2 do 299 `nav_stale`, mapper p95 `872 ms`, 9 movement pulses, net `0,071 m`, 0 reached viewpoint. Giữ artifacts, không retry trong pass này.
- [x] Gộp duplicate ray-cell updates; model-free suite `35 passed`, replay parity qua 9 snapshot giữ nguyên states/floor/clearance/traversable masks. Benchmark stride 4 `199,1→126,2 ms`, stride 8 `49,7→30,7 ms` trên 8 snapshot.
- [x] Episode `p2-ray-aggregation-120s-20261022-a`: map p95 `711 ms`, planner p95 `649 ms`, 216 `nav_stale`, 150 `no_valid_path`, net `0,079 m`, 0 viewpoints. Giữ artifacts, không retry/tune trong pass này.
- [x] Thêm telemetry `path_revalidation_failure_reason` và kiểm tra bằng regression; suite hiện tại `36 passed`. Không đổi hành vi planner/controller.
- [x] Episode `p2-path-revalidation-telemetry-120s-20261022-a`: fail P2, `0,0714 m`, 0 viewpoints; lần revalidation fail đầu tiên là `path_blocked_by_new_map` (tick 350), lặp 3 lần. Giữ artifacts; không retry/tune trong pass vừa fail.
- [x] Revalidation hiện chạy A* từ pose hiện tại tới cùng goal trên map revision mới; regression prefix-behind-robot và map-window shift pass; full suite `38 passed`.
- [x] Một episode sau thay đổi `p2-current-pose-replan-120s-20261022-a`: chạy đủ 120 s nhưng fail P2 (`0,127 m`, không đủ hai viewpoints). Revalidation không còn báo prefix blocked; map cuối chỉ có một footprint-safe reachable cell tại pose robot, candidate viewpoints trùng cell này; artifacts giữ nguyên, không retry trong pass.
- [x] Offline audit 8 maps và static NavMesh probe: xác nhận lặp lại một cell thiếu floor support dù có clearance/free-ray evidence và cell navigable; thêm audit script/artifact. Counterfactual không ghi vào map và không dùng làm traversability.
- [x] Thêm bounded initial-scan RGB-D recorder theo 12 sector yaw và manifest đồng bộ với frame ID/index, camera pose, intrinsics, floor height, NavMap revision. Trần payload 32 MiB; chỉ ghi khi command là `initial_scan`, sau khi simulator nhận lệnh bước tương ứng; lỗi recorder được journal và không chặn episode. Ảnh hưởng timing của tick tiếp theo chưa đo.
- [x] Benchmark lại 14 saved snapshots × 7 repeats, dùng validated-start-anchor tương tự host; median-of-snapshot medians: NavMap stride 4 `98 ms`, stride 8 `26 ms`, known-free flood `40 ms`, frontier `75 ms`; A* đo trên 9 map có goal hợp lệ, median `1,95 ms/goal`. Sửa profiler, không đổi navigation semantics.
- [x] Model-free regression sau thay đổi recorder/cache và worker contract: `41 passed`; `compileall` và Habitat Python `py_compile` sạch. Các test vẫn giữ unknown/footprint/corner-cut và expiry safety.
- [x] Chạy đúng một navigation-only episode 120 s: `p2-gate-120s-20261022-a`. Fail gate: `0,143 m`, 0 viewpoint, 691 `no_valid_path`; không collision/filter block, chỉ 2 `nav_stale`. Giữ artifacts; không retry trong pass.
- [x] Kiểm chứng exact-pair recorder: hai soak run có manifest initial-scan với 24 sample mỗi run; recorder chỉ lưu frame khi `as_of_frame_id` khớp. Đây xác nhận pairing/capture, chưa chứng minh floor-gap đã được khắc phục.
- [x] P2 movement acceptance đạt ở `p2-split-depth-fast-motion-120s-20261022-a`: net displacement `0,780 m`, hai viewpoint reached duy nhất theo tọa độ, 0 collision/filter block; regression `48 passed`. Run giữ lại `233/701 nav_stale` tick, do đó đây là locomotion acceptance chứ chưa phải throughput/realtime acceptance.
- [x] Recheck live graph + frontier sau P2: `integrated-split-depth-motion-graph-120s-20261022-a` đạt `1,485 m`, hai viewpoint, 93 movement pulses, 0 collision/filter block; worker xử lý và publish `60/60` admitted keyframes/revisions, final graph 132 nodes/231 edges. Backend còn provisional; có 45 `nav_stale` + 12 `depth_stale` tick.
- [x] Kiểm chứng dashboard ba panel live và source age/frame lag qua UI smoke; soak mới ghi RGB/NavMap/graph frame identity. Thao tác dài trực tiếp trên desktop có display và freshness stalls vẫn là backlog.
- [x] Bắt đầu preflight adapter ConceptGraphs/OccuSG: các Python worker riêng, checkpoint OpenCLIP, source ConceptGraphs, config OccuSG và binary M2/M3 + DUDE đều có mặt; import smoke của cả ba môi trường thành công. Script read-only: `scripts/preflight_legacy_mappers.py`.
- [x] Replay một keyframe lưu qua P2 ONNX, P3 depth geometry, OpenCLIP ViT-B/32 và hai session native persistent bằng `scripts/replay_saved_native_keyframe.py`. Run `runs/native-keyframe-replay-frame652-20261023-e/`: frame `integrated-floor-calibrated-graph-120s-20261023-a:00000652`, P2 2 detections, P3 2/2 accepted, OpenCLIP 2/2 features; ConceptGraphs và OccuSG preflight đều pass, mỗi nhánh publish đúng một revision cùng source frame/hash, nhận paired barrier ack và finalize exit 0. ConceptGraphs update khoảng 169 ms, OccuSG khoảng 5,49 s. Packet giữ đúng scene asset, clocks và capture hashes. Artefact mới nằm trong 3DSG; repo tham khảo không bị sửa. Đây là single-frame native replay, chưa phải plugin stream trực tiếp trong exploration và `graph_join_performed=false`.
- [x] Nối worker streaming native vào graph plugin/live episode: P2 masks + P3 geometry + OpenCLIP features → packet có identity/hash → ConceptGraphs/OccuSG persistent workers → paired barrier → viewer graph revision. Native live và soak đã xử lý nhiều revisions; native snapshot vẫn giữ hai nhánh riêng, chưa semantic-join.
- [x] Navigation gate giữ nguyên: displacement `>0,2 m` và ít nhất hai viewpoints reached; run mới nhất đạt `0,909 m`/2 viewpoint (navigation-only), `1,972 m`/3 viewpoint (native live), và cả hai spawn soak 300 s đều pass. Không nới unknown/footprint/corner-cut rule; collision/filter blocks vẫn được theo dõi.
- [x] Dashboard ba khung live, frame age/provenance và auxiliary-depth capture/integration đã được nối; replay thêm một keyframe pending thật. **Còn mở:** semantic join có identity/provenance policy, graph accuracy review độc lập, desktop display-attached dài và validation đa scene.

## 1. Quyết định kiến trúc và phạm vi

Xây một ứng dụng desktop khởi động từ terminal, cho agent tự khám phá bằng frontier từ RGB-D quan sát được, chuyển động chậm nhưng mượt, đồng thời cập nhật graph Room–Object–RobotNav. Tái sử dụng Habitat, persistent perception, ConceptGraphs, OccuSG và renderer của dự án cũ; bổ sung bản đồ phục vụ điều khiển, planner, controller, scheduler keyframe và desktop shell.

**Điểm quyết định:** vòng di chuyển không chờ object/room mapper. Navigation đọc một bản đồ depth nhẹ, cập nhật nhanh; graph dùng keyframe có chọn lọc và cập nhật bất đồng bộ. Khi mapper không theo kịp, giảm tốc hoặc dừng quan sát có chủ đích, không để hàng đợi kéo dài và không giật camera theo từng lần inference.

Phần đầu đồ án gồm:

- Khám phá chủ động trong một tầng, từ một vị trí spawn hợp lệ, không có danh sách waypoint đặt trước.
- Xây object geometry/identity, room geometry, RobotNav và các quan hệ có chứng cứ; cho phép unknown/unassigned khi chưa đủ dữ liệu.
- Một cửa sổ desktop có ba khung chính: RGB live, 2D topdown occupancy/navigation và 3D scene graph.
- Mỗi episode tối đa 600 giây thời gian thực kể từ READY → RUNNING. Có thể kết thúc sớm khi hết frontier hoặc không còn tiến triển. Warmup hiển thị riêng, không tính vào 600 giây.
- Dừng chuyển động ở deadline; lưu/flush với timeout riêng tối đa 30 giây. Vì vậy hoàn tất lưu có thể tới khoảng 10 phút 30 giây sau RUNNING. Nếu cần đóng toàn bộ đúng phút thứ 10, dùng profile 570 giây exploration + 30 giây shutdown.

Chưa làm ở giai đoạn này: target-object search, policy học tăng cường, LLM chọn đích, SLAM thực, điều hướng nhiều tầng/cầu thang, robot vật lý. Dùng pose simulator và ghi rõ đây là **exploration với định vị lý tưởng**, không phải đã giải quyết localization.

Định nghĩa “liên tục”: RGB cập nhật độc lập; bản đồ điều hướng tăng dần; graph có nhiều revision xuất hiện trong khi agent còn hoạt động. Không yêu cầu detector hoặc graph inference ở mọi camera frame. Định nghĩa “chính xác”: có đo geometry, identity, membership và quan hệ bằng dữ liệu đối chiếu; không dùng số node hay hình ảnh đẹp thay cho accuracy.

## 2. Cơ sở đã kiểm tra

### 2.1. Tài liệu, source và artifact local

Root cũ: `/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG`.

Đã đọc tài liệu bàn giao `PROJECT_HANDOFF_TO_NEW_DIRECTION_2026-10-21.vi.md`, đối chiếu architecture, local environment, source điều phối/session/route/unified graph và phần navigation trong builder. Đã đọc manifest của run `t4_camera_live_00802_geometry_candidate_20261020_v5`. Đây là rà soát các đường quan trọng phục vụ thiết kế, không phải audit toàn bộ repository hay benchmark mới.

| Bằng chứng hiện tại | Hệ quả cho hướng mới |
|---|---|
| T4 v5 completed; 1.362 frame camera; 31 candidates, 5 paired keyframes/revisions theo handoff; ba commit đầu trong capture | Pipeline live đã có nền; cần tăng giá trị quan sát được xử lý, không xây lại mọi thứ |
| Producer manifest: khoảng 23,29 FPS ở 640×480 trên route gần 58,44 giây mô phỏng | Có cơ sở thử 20–25 FPS, chưa đủ cam kết cho episode 10 phút có planner |
| `CameraLiveCoordinator` khóa `max_pending=1`, thay pending bằng frame mới nhất | Không chỉ tăng queue; cần admission có bảo vệ keyframe quan trọng và backpressure |
| Route runner đọc `route_points`, resolve toàn đường bằng PathFinder, rồi lấy các target trên polyline | Phải thay nguồn goal và vòng execution; chỉ thay tên route không tạo autonomous exploration |
| ConceptGraphs/OccuSG đã có session sống xuyên run, thứ tự packet và xử lý session lỗi | Giữ session API và paired commit; không load model theo frame |
| Unified graph dùng `canonical_world_z_up`, đơn vị mét | Navigation mới phải dùng đúng transform đã có |
| Room polygons thiếu elevation provenance; verified membership đang unavailable | Cần milestone scope thật, không bật assumed mode để làm demo trông đầy đủ |
| M7 tạo `navigation` node và `navigable_path`, nhưng ghi `robot_footprint_passability=False` | Không sử dụng các cạnh structural hiện có như đường robot chắc chắn đi được |
| Bản đồ/navmesh v5 có `include_static_objects=false` và physics disabled | Không suy zero collision ở route cũ thành bảo đảm tránh mọi vật thể |
| Handoff ghi chưa phát hiện Git repository | Thiết lập version control cho code mới, lưu baseline hash, tránh sửa/xóa artifacts cũ |

Source nên tái sử dụng/đối chiếu:

- `run_scene.sh`, `scripts/scene_launcher.py`, `integration_v2/scene_registry.py`.
- `integration_v2/camera_live.py`, `camera_live_startup.py`, `camera_live_contract.py`, `camera_pacing.py`, `live_evidence.py`.
- `scripts/run_habitat_smooth_route_preview.py`, `integration_v2/smooth_route.py`: tái sử dụng calibration, spawn, bước simulator, pacing và checks; tách bỏ phụ thuộc route cố định khỏi producer mới.
- `integration_v2/perception_session.py`, `perception_feature_sessions.py`, `conceptgraphs_session.py`, `occusg_session.py`.
- `integration_v2/unified_scene_graph.py`, `unified_incremental_timeline.py`, `room_belief_commit_session.py`.
- `src/standalone_sg/pipeline/builder.py`, `navigation/blocks.py`, room lifecycle và `semantics/room_belief_v1.py`.
- `scripts/causal_timeline_viewer.html`, `scripts/xyz_renderer.js`, viewer build/serve hiện có.

### 2.2. Cấu hình máy kiểm tra trực tiếp

| Thành phần | Kết quả ngày 21-10-2026 |
|---|---|
| OS | Ubuntu 24.04.4 LTS |
| CPU | Intel Core i5-13420H, 8 core / 12 logical CPU |
| RAM | Khoảng 15 GiB usable; lúc kiểm tra available khoảng 8,6 GiB |
| Swap | 4 GiB, lúc kiểm tra chưa dùng |
| Đồ họa PCI nhìn thấy | Intel Raptor Lake-P UHD Graphics |
| NVIDIA | `nvidia-smi` không có; PCI output không thấy NVIDIA; chưa có bằng chứng CUDA usable |
| Ổ đĩa | Phân vùng khoảng 468 GiB, trống khoảng 300 GiB |

Chọn thiết kế CPU-first. Giữ YOLO11n-seg ONNX + OpenCLIP ViT-B/32 theo baseline và ConceptGraphs mapping core hiện hữu. Chưa chuyển sang SAM lớn/GroundingDINO hoặc LLM online. Intel iGPU còn chia sẻ RAM và render với desktop; cần đo OpenGL renderer thực tế lúc preflight để phát hiện software rendering. Không gộp các Python environment cũ; desktop có environment riêng nếu cần.

Giới hạn tài nguyên ban đầu: tổng RSS của process tree dự án mục tiêu ≤6 GiB; duy trì MemAvailable ≥2 GiB; ngưỡng cảnh báo khi available <2 GiB, dừng an toàn nếu <1 GiB kéo dài 3 giây. Đây là ngân sách thiết kế cần xác nhận, không phải kết quả đo pipeline. ONNX/BLAS worker bắt đầu 1–2 thread mỗi process, đo tổng CPU để tránh mỗi worker chiếm cả 12 logical CPU. Không bật thêm nhiều worker mapping song song theo frame.

## 3. Dự án tham khảo và cách sử dụng

Các nguồn web đã xem ngày 21-10-2026. Trước khi lấy code cần khóa commit, kiểm tra license từng file, ghi attribution; không cài cả stack vào environment đang hoạt động.

| Nguồn chính thức | Nội dung hữu ích | Quyết định |
|---|---|---|
| [m-explore-ros2](https://github.com/robo-friends/m-explore-ros2), [frontier_search.cpp](https://raw.githubusercontent.com/robo-friends/m-explore-ros2/main/explore/src/frontier_search.cpp), [explore.cpp](https://raw.githubusercontent.com/robo-friends/m-explore-ros2/main/explore/src/explore.cpp) | BFS frontier, clustering, cost, quản lý goal và progress | Tham khảo thuật toán và failure handling; viết core thuần Python/NumPy trước, không kéo ROS2/Nav2 vào demo |
| [Habitat PathFinder API](https://aihabitat.org/docs/habitat-sim/habitat_sim.nav.PathFinder.html) | Navmesh, collision-filtered movement, shortest path | Dùng simulator và local movement filter; không dùng shortest path toàn scene làm planner mặc định |
| [ConceptGraphs](https://github.com/concept-graphs/concept-graphs) | Object mapping và graph từ quan sát RGB-D | Giữ adapter/session đã có; không gọi hybrid YOLO+ViT-B/32 là parity upstream |
| [HOV-SG](https://github.com/hovsg/HOV-SG) | Cấu trúc phân cấp không gian và scene graph | Tham khảo tổ chức floor/room/object; không thay pipeline online bằng reconstruction offline |
| [Frontier Semantic Exploration](https://github.com/ybgdgh/Frontier-Semantic-Exploration) | Frontier và semantic policy trên Habitat | Tham khảo cho phần ObjectNav sau; repo ghi môi trường Python 3.7/CUDA 10.0/challenge-2022 nên không sao chép environment |
| [VLFM](https://github.com/rai-opensource/vlfm) | Frontier gắn với thông tin ngôn ngữ cho ObjectNav | Giữ interface goal scorer để nối về sau; chưa đưa model của hệ này vào runtime |
| [Qt QWebEngineView](https://doc.qt.io/qtforpython-6/PySide6/QtWebEngineWidgets/QWebEngineView.html) | Nhúng nội dung web trong cửa sổ Qt | Dùng PySide6 desktop shell chứa viewer local, kiểm tra GPU/RAM thực tế |

Lựa chọn riêng của kế hoạch là frontier hình học + A* trên observed costmap + controller giới hạn vận tốc. Không cần học policy để giải quyết route cứng. Giá trị từ graph chỉ bổ sung ưu tiên quan sát sau khi baseline frontier đã ổn định.

## 4. Kiến trúc runtime

```text
Desktop supervisor / episode clock / resource watchdog
  │
  ├─ Habitat process: owner duy nhất của sim, pose và actions
  │    ├─ RGB latest stream ───────────────────────────> RGB panel
  │    ├─ depth + K + pose ─> fast navigation mapper
  │    │                       └─ observed costmap + version
  │    │                             ├─ frontier + A* ─> local controller ─> sim
  │    │                             └────────────────> 2D panel
  │    └─ keyframe proposals ─> admission + bounded queue
  │                              └─ persistent perception
  │                                   ├─ ConceptGraphs session
  │                                   └─ OccuSG session
  │                                         │ paired frame barrier
  └────────────────────────────────── unified revision + semantics
                                            └──────────> 3D panel
```

### Ranh giới module ObjectNav

`exploration/module_api.py` là host contract của 3DSG (`3dsg-objectnav-module/v1`): task có thể là frontier exploration hiện tại hoặc object-nav về sau; observation đóng gói RGB, RGB-D, pose và collision; backend ports cho navigation, perception, scene graph và target selection được inject. Habitat chỉ là một host adapter, không phải giả định của planner/graph API. Desktop đọc transport `3dsg-live-state/v1` nên renderer cũng thay thế được mà không đổi simulator hoặc mapper. Giữ schema này ổn định và version hóa khi thêm field/breaking semantics.

Trong bản hiện tại, `NavigationRuntime` là implementation cụ thể của geometry/frontier loop và `GraphIntegrationWorker` nhận session/converter callbacks; graph plugin factory đã được nối vào Habitat launcher và desktop. Chưa có `ObjectNavModule` composition class cấp cao đóng gói task + navigation + graph thành một host độc lập, và target selector cho ObjectNav vẫn là extension. Không để ConceptGraphs, OccuSG, ONNX hay Qt types lọt vào core `contracts.py`.

Tách process cho sim/UI và các worker CPU nặng; không cho inference blocking trên Qt main thread. Giữ IPC file-backed cho keyframe nếu thuận lợi; dùng latest buffer/shared memory hoặc socket local cho preview/depth để không ghi mọi frame xuống đĩa. Chỉ một process ghi trạng thái simulator, một coordinator ghi graph head.

Hai bản đồ có vai trò khác nhau:

1. **NavMap**: depth-only, cập nhật nhanh, phục vụ obstacle/frontier/path; lưu `nav_revision` và `as_of_frame_id` riêng.
2. **SceneMap/Graph**: object + OccuSG room/structural map theo keyframe; dùng paired revisions hiện có.

Cả hai chia sẻ calibration, world frame, floor scope và frame identity; không ép bằng revision hoặc âm thầm gọi nav map là room map. Chênh lệch là hợp lệ và phải hiển thị. Controller chỉ sử dụng NavMap đã kiểm tra freshness.

## 5. Navigation map: làm đúng trước khi frontier

### 5.1. Tọa độ và đầu vào

Packet bắt buộc: `run_id`, `scene_id`, `frame_id`, `frame_index`, simulator time, host monotonic timestamp/domain, RGB/depth refs, depth unit/convention, K, camera extrinsics, agent pose và calibration hash.

Habitat thường dùng Y-up; graph hiện dùng Z-up. Tái sử dụng transform của dự án, không thêm phép đổi trục riêng bằng suy đoán. Test pixel → camera → world → grid bằng các điểm known fixture, camera quay 90° và round-trip; kiểm tra depth là optical-axis depth hay range theo sensor hiện dùng. Tính intrinsics lại khi resize; không trộn mask 640×480 với depth profile khác.

### 5.2. Biểu diễn và cập nhật

- Grid 0,05 m/cell, world-aligned, mở rộng theo dữ liệu quan sát; origin và revision tường minh. Không lấy full-scene bounding box/navmesh occupancy làm bản đồ đã biết.
- Lưu unknown/free/occupied, log-odds hoặc evidence counts có clamp, last-observed, floor-support và clearance status. Unknown không phải free.
- Ray integration từ depth hợp lệ; invalid/NaN/zero depth không được đánh dấu vùng phía trước là free. Tránh ray xuyên vật cản và artifact ở biên depth.
- Giữ height bins/column evidence trong vùng robot: tia trên bàn không được xóa chân bàn; nhìn tường cao không chứng minh mặt sàn đi được. Floor, vật cản thấp, tabletop/overhang và thiếu sàn phải có test riêng.
- Traversable yêu cầu sàn hỗ trợ và khoảng trống phù hợp footprint/chiều cao agent. Phần chưa biết đủ clearance giữ unknown/conservative. Vùng spawn chỉ khởi tạo footprint hợp lệ của agent, không mở free một đĩa lớn tùy ý.
- Obstacle inflation theo bán kính robot trong geometry contract + margin khởi đầu 0,05 m; không tự chọn radius khác navmesh. Tách inflation planner với radius-eroded navmesh để không hiểu sai distance-to-boundary là clearance vật lý.
- Để giữ tải CPU thấp, dùng depth subsample cho nav (ví dụ stride 4), nhưng kiểm tra obstacle nhỏ; không downsample capture gốc. Bắt đầu 5 Hz, giảm 2 Hz chỉ nếu giới hạn tốc độ/stopping distance vẫn an toàn.
- Depth check cục bộ phía trước chạy cùng control tick hoặc theo buffer mới nhất để bắt vật cản giữa hai lần cập nhật global grid. Depth stale → phanh.

### 5.3. Không dùng thông tin chưa quan sát để exploration

Planner không đọc semantic GT, full occupancy, navmesh polygon toàn cảnh hay `PathFinder.find_path` để tính route xuyên vùng chưa thấy. Cho phép simulator dùng navmesh làm bộ lọc một bước chuyển động và kiểm tra spawn; đây là simulator constraint được ghi rõ trong manifest. Bản đồ GT/navmesh để đánh giá chỉ dùng offline hoặc overlay debug mặc định tắt, không gửi sang policy.

## 6. Frontier, chọn goal và đường đi

### 6.1. Frontier extraction

Định nghĩa dùng nhất quán: frontier là các **free cells có unknown neighbor**. Một số implementation tham khảo chọn unknown boundary cells; adapter phải chuyển về điểm đứng trong known free space, không đặt robot vào unknown centroid.

1. Flood-fill các free cells mà footprint robot có thể tiếp cận từ pose hiện tại.
2. Trên biên free/unknown, gom connected components; loại component nhiễu quá nhỏ, khởi đầu dài <0,20 m.
3. Chọn nhiều viewpoint ứng viên nằm trong free space đã inflate, cách biên khoảng 0,3–0,6 m nếu hình học cho phép.
4. Kiểm tra path và clearance của từng viewpoint; không dùng centroid nếu nằm trong tường/unknown.
5. Ước lượng information gain bằng raycast trên **bản đồ hiện có**, với FOV/range sensor; không biết vật thật nằm trong unknown.
6. Lưu `frontier_id`, map revision, candidate poses, predicted gain, path cost, rejection reason và visited/failed history.

### 6.2. Goal score và chống dao động

Baseline đầu tiên: nearest reachable frontier theo path cost. Sau khi baseline đạt mới bật:

`score = w_gain * normalized_gain - w_path * normalized_path_length - w_turn * normalized_yaw_change - w_risk * risk - w_repeat * recent_visit_penalty`

Mọi term chuẩn hóa theo scale cấu hình cố định, không trộn mét với cell count tùy tiện. Khởi đầu weights tương đối 1,0 / 0,4 / 0,2 / 0,5 / 0,3; đây là giả thuyết cần ablation. Semantic/object-view bonus mặc định 0; sau đó giới hạn không quá 20% score để không biến exploration thành quay quanh một object.

Chỉ đổi goal khi goal hết hợp lệ, đạt đích, path bị chặn, stuck, hoặc goal mới tốt hơn ít nhất 20% sau hold time khoảng 3 giây. Giữ ID theo overlap/spatial matching, không reset failure count mỗi map update.

### 6.3. Global/local planning

- A* 8-connected trên inflated observed free grid; cấm corner cutting chéo hai ô bị chặn.
- Simplify đường bằng line-of-sight kiểm tra toàn swept footprint. Không fit spline cắt góc tường.
- Replan 1 Hz hoặc theo obstacle/goal event, không mỗi RGB frame.
- Planner chạy trên immutable map snapshot; controller revalidate đoạn gần trước execution. Map cũ hoặc path mất hợp lệ → giảm tốc/dừng, không chờ planner vô hạn.
- Nếu CPU A* vượt budget, crop vùng connected component/local window, cache distance transform; chỉ chuyển C++ sau profiling chứng minh cần.

### 6.4. Recovery và kết thúc

State machine: `WARMUP → INITIAL_SCAN → PLAN → MOVE → OBSERVE`, có `RECOVER`, `PAUSED`, `STOPPING`, `FINISHED/FAILED`.

- Initial scan quay chậm để tạo free map đầu tiên; scan góc nhỏ/360° là hành vi cảm biến tại chỗ, không phải route cho cả scene.
- Đang ra lệnh đi nhưng tiến triển <0,05 m trong 3 giây → stuck; loại thời gian OBSERVE/PAUSED khỏi detector này.
- Recovery: phanh → scan hạn chế → cập nhật obstacle → replan. Lùi chỉ trên đoạn đường vừa đi đã kiểm tra; không lùi mù.
- Sau hai thất bại với một goal: blacklist tạm theo region/map version; bỏ blacklist khi có evidence mới đủ lớn, không khóa vĩnh viễn mọi cửa hẹp.
- Hết frontier: scan xác nhận, thử lại trên vài map updates. Phân biệt `explored_observed_component`, `no_reachable_frontier`, `stalled`, `budget_exhausted`, `worker_failed`, `user_stop`.
- Không gọi “khám phá toàn scene” chỉ vì detector không tìm frontier. Unknown hoặc vùng disconnected vẫn có thể tồn tại.

## 7. Chuyển động mượt và giới hạn 10 phút

Controller theo path online, velocity-limited hoặc pure pursuit với lookahead khởi đầu 0,3–0,5 m. Vận tốc 0,10–0,20 m/s; yaw 15–30°/s; acceleration khởi đầu 0,15 m/s² và yaw acceleration 45°/s². Giảm tốc theo curvature, clearance và độ cũ của depth/map.

Control/render target 20–25 Hz. Ở 25 Hz, 0,15 m/s tương ứng bước khoảng 0,006 m; 20°/s tương ứng 0,8°. Dùng actual dt có clamp, không nhân một dt lớn sau stall để robot nhảy xa. Giữ mọi pose thay đổi qua collision-filtered simulator movement. `set_state` chỉ dùng initial spawn; không teleport theo waypoint hoặc interpolate ảnh để giả chuyển động.

Nếu action API cũ chỉ hỗ trợ turn/forward riêng: trước hết dùng bước nhỏ, ramp tốc độ và turn-in-place có chủ đích. Sau đó thử adapter velocity điều khiển translation + rotation trên từng tick, vẫn gọi movement filter và đo collision. Không hứa pure-pursuit chạy được chỉ bằng sửa UI.

Điều kiện stop: `d_stop = v²/(2*a_brake) + v*(sensor_age + control_latency) + margin`. Nếu khoảng trống quan sát không đủ d_stop hoặc NavMap/depth quá cũ, giảm v về 0. Ngưỡng stale khởi đầu depth 0,3 s, map 1 s; chỉnh theo kết quả P2. Watchdog độc lập với inference gửi stop ngay khi vượt deadline hoặc mất heartbeat.

Đến 600 giây: hủy goal, ngừng nhận keyframe mới, chuyển STOPPING, hoàn thành/đánh dấu timeout task đang chạy, commit revision hoàn chỉnh cuối cùng, ghi manifest và giữ cửa sổ để xem lại. Không tự quay về spawn vì sẽ cần thêm ngân sách và không thuộc yêu cầu.

## 8. Scheduler keyframe để scene graph cập nhật có ích

### 8.1. Không tiếp tục pure latest-wins cho mọi quan sát

V5 bỏ 26/31 candidates trước processing. Điều đó kiểm soát bộ nhớ nhưng có thể mất góc nhìn quan trọng ở cửa phòng hoặc object chỉ thấy một lần. Scheduler mới được version hóa riêng; không sửa hành vi baseline cũ âm thầm.

- Camera preview: latest-wins luôn hợp lý; có thể bỏ frame hiển thị khi UI chậm và ghi dropped count.
- Nav stream: lấy latest depth/pose đồng bộ, có freshness gate.
- Graph queue: một in-flight + tối đa hai pending, byte cap khởi đầu 32 MiB cho raw packet payload. Tổng bộ nhớ còn phải tính intermediate features, clouds và models.
- Tối đa một pending được bảo vệ cho first-view/doorway/new-region/đa góc nhìn chưa được xử lý. Pending thường thay thế theo novelty/quality.
- Tất cả frame khi dispatch phải tăng dần index: frame cũ đã bị vượt qua phải bỏ có reason; không xử lý priority mới rồi quay về keyframe cũ làm hỏng mapper session.
- Admission ACK chỉ sau packet được persist/validate. Coordinator paired commit vẫn yêu cầu hai mapper nhận cùng frame identity/order/hash.

### 8.2. Trigger và backpressure

Candidate khi dịch chuyển khoảng 0,25 m, quay khoảng 20°, có vùng mới đáng kể, hoặc đến viewpoint; không bắt buộc inference chỉ vì quá một khoảng thời gian mà ảnh giống hệt nhau. Candidate dựa geometry/depth trước detector để không chạy model trên mọi frame.

Dùng EWMA processing latency và graph age để giảm tốc. Queue đầy kéo dài hoặc graph age >20 s → ưu tiên OBSERVE sau khi phanh mượt. Có thể tiếp tục preview và nav update khi đứng yên. OBSERVE chờ ACK/commit với cap khoảng 15 s; worker hung phải báo degraded/failed chứ không đứng vô hạn. Lần quan sát khác chỉ tăng support khi có baseline/góc nhìn mới; lặp lại cùng ảnh không được tính đủ `min_detections`.

Mục tiêu graph revision mỗi 5–15 s nếu phần cứng đáp ứng; đây là mục tiêu đo. Nếu latency p95 vượt 20 s: profile riêng perception/OccuSG/serialization, giảm point payload và renderer LOD trước; sau đó giảm tốc và mật độ candidate. Không hạ threshold association hoặc đổi detector để che thiếu tài nguyên.

Không thay `min_detections=3` chỉ để có nhiều object. Giữ provisional nodes để viewer cho thấy tiến trình, phân biệt admitted/confirmed và không cho provisional góp semantic evidence. Confirmed còn cần kiểm tra chất lượng, không tự đồng nghĩa đúng GT.

## 9. Room–Object–RobotNav và tính đúng của graph

### 9.1. Schema đề xuất

| Thành phần | Thuộc tính/quan hệ |
|---|---|
| Room | ID ổn định, polygon, floor/elevation scope, lifecycle revision, label hoặc unknown |
| Object | ConceptGraphs ID, geometry ref, class belief, view support, provisional/eligible, source observations |
| RobotNav | Reuse node navigation của M7; bổ sung edge validation cho footprint, map version, clearance và trạng thái |
| Robot | Pose live riêng và pose as-of graph; không trộn hai thời điểm |
| `room_contains` | Room→Object/Nav/Robot chỉ với membership hợp lệ cho loại entity và scope tương ứng |
| `room_adjacency` | Quan hệ geometry/portal có provenance; adjacency không tự chứng minh robot đi qua được |
| `navigable_path` | Giữ structural relation cũ và đánh dấu rõ chưa kiểm tra footprint |
| `traversable_path` mới | Chỉ tạo khi đường được kiểm tra trên observed inflated map; có version, min clearance và invalidation |
| `observed_from` mới | Object→keyframe/nav viewpoint; lưu evidence compact, hỗ trợ ObjectNav về sau |

Tên mới là đề xuất schema v2, cần adapter/migration và test; không giả chúng đã có trong source. Room adjacency, path edge, object identity không được suy từ khoảng cách Euclidean đơn thuần. Khi vật cản mới xuất hiện, edge traversable phải stale/retract. Trước execution vẫn kiểm tra map mới nhất.

### 9.2. Sửa elevation scope là hạng mục bắt buộc

Vấn đề cũ nằm ở việc projection collapse các free-height intervals thành envelope, không chỉ thiếu một field JSON. Kế hoạch:

1. Giữ height/level provenance từ occupancy projection qua M3 → M4 → Room revision, không phục hồi bằng final graph sau run.
2. V1 chỉ một floor được quan sát: xác định floor support từ depth/geometry, lưu interval, confidence/uncertainty, source frames và connected support. Không lấy camera gần cùng độ cao làm bằng chứng đủ.
3. Segmentation chạy trên floor-specific grid. Polygon cắt vùng có nhiều tầng chồng XY phải split hoặc unavailable; không dùng union vertical envelope.
4. Room snapshot mang `scope_id`, world frame, unit, z interval/level support, evidence refs/hash, validity prefix và revision. Schema check chỉ xác nhận provenance; geometric consistency cần test và review riêng.
5. Membership dùng object voxel support trong polygon + vertical compatibility. Zero support → unassigned; ambiguous → không tạo edge. Không ép gán bằng nearest-room hoặc center-only ở biên.
6. Room merge/split/retire cập nhật membership và semantic ledger atomic; giữ lineage ID. Không backfill nhãn hoặc cạnh từ tương lai vào prefix.
7. Giữ verified-only mặc định. Nếu scope chưa đạt, demo vẫn thấy Room/Object/Nav geometry riêng nhưng phải báo membership unavailable; chưa nghiệm thu graph quan hệ hoàn chỉnh.

Room functional labels dùng RB1 hiện có, category cap, retract và hysteresis; cho phép unknown. Không coi room label là dự đoán độc lập từ một RGB frame. Không bật assumed declaration của sequence cũ cho episode mới.

### 9.3. Object quality

Giữ model/matcher baseline để tách lỗi scheduler với lỗi association. Hoàn tất review 75 observations 640×480 đã có trước khi kết luận quality baseline; không gán annotation 160×120 cho sequence này. Với exploration mới, tạo review set riêng.

Ưu tiên kiểm tra mask-depth alignment, clipping depth discontinuity, outlier filtering, metric scale, association đa góc, duplicate/fragmentation, false merge và retained observations. Object mất khỏi FOV không đồng nghĩa đã bị xóa khỏi scene. Geometry/history có cap và voxel downsampling versioned; không xóa evidence cần cho evaluation.

Nếu closed-set YOLO bỏ nhiều loại đồ vật, ghi rõ taxonomy hạn chế. Model open-vocabulary lớn là thí nghiệm offline sau, không mặc định giải pháp runtime cho máy này.

## 10. Desktop app ba khung

Dùng desktop shell native đọc giao thức live file-backed. Checkpoint ban đầu dùng PyQt5 đã có trong system Python và QPainter, không phụ thuộc PyQtWebEngine (hiện máy chưa cài module này); RGB, map và graph renderer ở widget riêng. `LiveDashboardWidget` không import simulator hoặc graph backend. Có thể thay bằng PySide6/QWebEngine sau nếu profile GPU phù hợp, nhưng host/renderer chỉ giao tiếp qua schema live đã version hóa. Không thay yêu cầu desktop bằng chỉ mở tab browser.

```text
┌ Scene / Start / Pause / Stop / elapsed / state / worker status ┐
│ RGB live                 │                                  │
│ frame_id, FPS, timestamp  │ 3D scene graph                    │
├──────────────────────────┤ clouds + Room/Object/Nav + edges   │
│ 2D topdown               │ orbit / select / layers / inspect │
│ unknown/free/occupied    │ graph revision, as-of frame, lag   │
│ robot/path/frontiers     │                                  │
└──────────────────────────┴──────────────────────────────────┘
```

- RGB nhận camera stream độc lập, không đợi graph revision; giữ ảnh cuối khi đứng yên, hiển thị paused/stale đúng trạng thái.
- 2D có grid metric, footprint + heading, trajectory, frontier candidates, goal đang chọn, A* path, obstacle inflation, room overlay tùy chọn. Phân biệt observed map với debug GT.
- 3D hiển thị clouds/bboxes, Room outlines theo độ cao hợp lệ, Nav nodes/edges và Robot. Không bịa room volume từ polygon khi scope thiếu.
- Toggle graph edges và point cloud; chọn object hiện evidence, class, membership status; chọn frontier hiện score/rejection; click inspect không đổi autonomous goal.
- Object ID và màu ổn định; graph swap atomic, giữ camera orbit/selection qua revision; render LOD khoảng 100k–300k points khởi đầu, dữ liệu evaluation giữ nguyên.
- Hiển thị `latest_camera_frame_id`, `nav_as_of_frame_id`, `graph_as_of_frame_id`, `graph_revision`, graph age theo host monotonic, queue length và worker status.
- RGB/Nav mới hơn graph là bình thường. Robot live marker khác màu với graph-as-of pose. Historical seek chuyển sang REVIEW rõ ràng; không gán ảnh live cho graph lịch sử.
- Server bind localhost; shell gửi control có session/run ID. Đóng cửa sổ hoặc Ctrl+C chạy shutdown có timeout; chỉ dừng process tree thuộc run, không kill server cũ không rõ ownership.

## 11. Profile cấu hình ban đầu

Các số dưới đây là **starting hypotheses**, phải khóa lại theo benchmark P0–P4.

```yaml
mode: frontier_live
runtime:
  exploration_wall_budget_s: 600
  shutdown_timeout_s: 30
  pose_source: simulator
  floor_mode: observed_single_level
  semantics_policy: verified_only
camera:
  width: 640
  height: 480
  target_fps: 25
  fallback_target_fps: 20
navigation:
  grid_resolution_m: 0.05
  map_update_hz: 5
  planner_hz: 1
  control_hz: 25
  depth_stride: 4
  nominal_speed_mps: 0.15
  max_speed_mps: 0.20
  max_yaw_degps: 30
  acceleration_mps2: 0.15
  yaw_acceleration_degps2: 45
  inflation_margin_m: 0.05
  depth_stale_s: 0.3
  nav_stale_s: 1.0
  goal_hold_s: 3
  goal_switch_improvement: 0.20
keyframes:
  min_translation_m: 0.25
  min_yaw_deg: 20
  max_pending: 2
  max_protected_pending: 1
  raw_pending_byte_cap_mib: 32
  graph_age_slowdown_s: 20
resources:
  target_process_tree_rss_gib: 6
  warning_mem_available_gib: 2
  stop_mem_available_gib: 1
  raw_keyframe_disk_cap_gib: 2
  run_disk_cap_gib: 8
```

Robot radius/height, HFOV, near/far depth, extrinsics, class vocabulary, model hashes, mapper parameters phải resolve từ calibration/resource/geometry contract và ghi vào effective config, không dùng default ngầm.

Lưu RGB-D keyframes + tất cả pose/actions nhỏ + policy events; không lưu toàn bộ raw RGB-D ở 25 FPS. 15.000 frames × 640×480 × 7 bytes (RGB uint8 + depth float32) đã khoảng 32,3 GB decimal chưa overhead. Có thể ghi RGB video nén tùy chọn nếu CPU cho phép; không tính video recording vào yêu cầu cốt lõi. Khi disk cap gần đầy: ngừng nhận evidence mới và kết thúc có reason, không xóa capture cần cho audit.

## 12. Tổ chức code và giao diện

Workspace extension hiện nằm trong `3DSG` và đã được khởi tạo Git riêng; project sibling được dùng làm nguồn tham chiếu read-only. Cây dưới đây thể hiện trạng thái: các module navigation/desktop/keyframe/graph/plugin đã tồn tại; `room_scope.py`, `nav_graph_bridge.py` và `scripts/evaluate_exploration.py` còn là thành phần kế hoạch cần triển khai hoặc hoàn thiện.

```text
exploration/
  contracts.py            # frame/map/goal/action/state contracts
  habitat_online.py       # producer nhận action online, không cần route_points
  nav_mapper.py           # depth -> observed grid/height evidence
  frontier.py             # components/viewpoint candidates
  planner.py              # A*, collision-aware simplification
  controller.py           # smooth tracking, speed/stale gates
  policy.py               # goal score/hysteresis/state machine/recovery
  keyframe_scheduler.py   # bounded chronological novelty-aware admission
  graph_bridge.py         # ordered session calls + paired graph commits
  module_api.py           # ObjectNav host/task/backend plugin contracts
  live_dashboard.py       # versioned latest-state + graph revision stream
  room_scope.py           # planned: scope/elevation evidence and membership
  nav_graph_bridge.py     # planned: structural vs traversable edge validation
  supervisor.py           # process ownership, deadline, watchdog, cleanup
  telemetry.py            # monotonic events, metrics, provenance
exploration/desktop.py    # three-panel native desktop shell
scripts/run_desktop.py
scripts/run_frontier_habitat.py
configs/exploration/      # CPU profile + scene/spawn config
scripts/evaluate_exploration.py # planned evaluation entrypoint
tests/                    # contract, geometry, causal/failure tests
```

Interface tối thiểu:

- `NavMapper.update(depth, K, T_world_camera, frame_meta) -> NavSnapshot`.
- `FrontierExtractor.extract(nav_snapshot, robot_pose) -> FrontierSet`.
- `Planner.plan(nav_snapshot, start, candidate) -> PathResult`.
- `Controller.tick(pose, depth_status, nav_snapshot, path, dt) -> ActionCommand`.
- `KeyframeScheduler.offer(packet, novelty) -> AdmissionDecision`.
- `GraphBridge.update(admitted_packet) -> PairedRevision` qua session hiện có.
- `Supervisor.stop(reason)` idempotent; không retry mapper đã mutate sau lỗi.

Command UX đích, chỉ có sau khi triển khai:

```bash
./run_explore.sh --scene 00802-wcojb4TFT35 --profile cpu_smooth --duration 600
./run_explore.sh --scene apt_1 --profile cpu_smooth --duration 600
./run_explore.sh --replay runs/<run_id>
```

Scene registry có spawn hoặc seed hợp lệ, không cần route. Cùng seed chỉ tái lập khởi tạo; policy online vẫn phụ thuộc observations/timing. Để replay chính xác lưu action log, admitted frame sequence và revisions; không hứa bitwise identical giữa nhiều live runs khác lịch CPU.

## 13. Lộ trình triển khai và acceptance theo mốc

Ước lượng cho một người đã quen code: khoảng 18–30 ngày làm việc, phụ thuộc mạnh vào room scope và Qt/OpenGL. Đây là ước lượng công sức, không phải cam kết thời hạn. Chốt từng gate trước khi kéo dài demo; không triển khai tất cả rồi mới kiểm tra.

| Mốc | Công việc và đầu ra | Acceptance bắt buộc | Ước lượng |
|---|---|---|---|
| P0: đóng baseline | source/resource hashes, environment probe, schema, CPU/RAM/OpenGL profiling ngắn, scene/spawn | Đọc được scene và calibration; không đổi artifact cũ; xác định peak process-tree RAM | 1–2 ngày |
| P1: nav sensing | online Habitat producer, transform tests, height-aware NavMap, 2D debug | Unknown đúng nghĩa; vật cản thấp/bàn/tường/cửa hẹp; ray không xuyên obstacle; không dùng GT planner | 2–4 ngày |
| P2: frontier moving | extraction, A*, controller, recovery, budget | Chạy 2 phút chỉ navigation, tự sinh ít nhất hai goal nếu scene cho phép; không teleport, không xuyên obstacle trong fixtures | 3–4 ngày |
| P3: graph workflow live | bounded scheduler/store, session bridge, provider plugin, telemetry, native graph revisions | Nhiều revision trong capture; xử lý hết admitted keyframes, drain sạch, paired frame identity/hash hợp lệ | **Workflow đạt** với provisional và native paired backend; semantic join, auxiliary depth và accuracy chưa đạt |
| P4: desktop vertical slice | ba panel, live RGB/Nav/Graph stream, Start/Pause/Resume/Stop, attach/replay | RGB stream độc lập graph; offscreen render; interactive run và lag/clock targets cần xác nhận | **Đã triển khai/render**, desktop tương tác và FPS gate chưa xác nhận |
| P5: graph correctness | room elevation propagation, membership, nav edge validation, object review | Scope thật qua prefix; lifecycle/retract đúng; không gán Object/Room sai tầng trong fixture; review quan hệ thực | 4–7 ngày |
| P6: demo 10 phút | tuning CPU, repeated runs, resource/failure tests | đạt technical gates dưới đây; output replay được; report giữ cả fail | 2–3 ngày |
| P7: quality report | annotation exploration mới, metrics, holdout, ablation | Tách workflow pass với quality, có denominator/coverage/limitations | 2–4 ngày |

P5 có thể bắt đầu sau P1 vì cần height evidence, nhưng demo Room–Object hoàn chỉnh chỉ nghiệm thu sau P5. Nếu P5 chưa đạt, kết quả P4 là demo exploration + geometry graph, phải gọi đúng mức hoàn thiện.

### Gate kỹ thuật cho demo mục tiêu

- Deadline dừng motion ≤600 s + 1 control tick khi hệ thống khỏe; watchdog phát hiện stall có deadline riêng. Flush ≤30 s; mọi timeout ghi rõ, không dùng finalization treo vô hạn.
- RGB target ≥20 FPS trung bình ở full workload; p95 displayed inter-frame interval ≤100 ms, không UI freeze >0,5 s trong trạng thái khỏe. Nếu không đạt, đo nguyên nhân, giảm tải renderer/nav trước; không tự hạ capture native 640×480.
- Navigation update ≥2 Hz, planner không chiếm control thread; depth stale luôn gây giảm tốc/stop.
- Chuyển động không có teleport; tốc độ/gia tốc thực đo trong envelope. Bất thường vượt bound phải liên kết action/frame.
- Graph target ít nhất 20 paired commits trong episode 600 s có đủ quan sát mới; ít nhất 90% khoảng cách giữa commit ≤20 s khi queue có việc và worker khỏe. Scene ít evidence/hết frontier sớm báo ngoại lệ, không tạo revision rỗng để đủ số.
- RGB/Nav/Graph đều có frame identity, timestamp và trạng thái riêng; thử worker sleep/hang để xác nhận RGB/UI không bị khóa và motion phanh đúng.
- Queue/RAM/disk có cap; không swap thrashing/OOM; so RSS phút 2 và phút 10 sau warmup để phát hiện tăng không giới hạn.
- Không half-commit, dangling edges hoặc dùng future observations; khôi phục viewer từ last good revision nếu finalize timeout.

Các target này là tiêu chí cần đo, chưa có bằng chứng máy đáp ứng đồng thời. Nếu không đạt sau profiling, report giới hạn thực và điều chỉnh profile minh bạch, không gọi realtime hoặc demo pass theo tiêu chí cũ.

## 14. Đánh giá exploration và chất lượng scene graph

### 14.1. Scene và trình tự thử

1. Fixtures nhỏ: phòng kín, L-corridor, cửa hẹp, vật cản thấp, bàn có gầm, hai độ cao trùng XY, disconnected component.
2. HM3D 00802: development cho tính liên tục vì đã có integration evidence; chọn spawn hợp lệ từ config cũ nhưng bỏ toàn bộ route waypoint.
3. ReplicaCAD apt_1: kiểm tra generality và static-object collision/navmesh generation; không suy room GT sẵn có nếu chưa inspect metadata.
4. Một HM3D scene chưa dùng tune, chọn sau kiểm kê assets/calibration: holdout. Mỗi scene thử ít nhất ba spawn/seed với budget như nhau khi đủ tài nguyên.

Không chạy tất cả 101 scenes. Tăng runtime theo 30 s → 2 phút → 10 phút sau khi gate trước đạt. Run lỗi giữ nguyên manifest/log, retry có run ID mới.

### 14.2. Metrics và ground truth

| Nhóm | Metric/định nghĩa |
|---|---|
| Exploration | Diện tích observed reachable free-space theo thời gian, số frontiers/goal thành công, quãng đường, revisit ratio, progress plateau, reason kết thúc |
| Coverage có GT | Diện tích free được quan sát giao GT reachable area / GT reachable area cùng tầng và spawn component; chỉ tính offline, không feed GT vào policy |
| Motion | Collision/filter-block count, recovery count, achieved speed/yaw/acceleration, frame jitter, thời gian MOVE/OBSERVE/WAIT/RECOVER |
| Throughput | Capture FPS, nav latency, perception/CG/OccuSG/serialization latency p50/p95, graph age, processed/superseded/protected keyframe, commit intervals |
| Object identity | Pairwise association precision/recall/F1, ID switch, false merge, fragmentation theo annotation; không dùng object count thay precision |
| Object geometry/class | 3D overlap/geometry error với GT khi có; mask/depth review; class scores sau taxonomy mapping đã khóa, báo unmapped riêng |
| Room | Polygon IoU/boundary error, split/merge/stability; labels đánh giá riêng nếu có room-function GT |
| Membership/edges | Room–Object precision/recall trên eligible annotated population; unassigned/unavailable rate; false traversable edges và invalidation |
| Resources | Process-tree peak RSS, MemAvailable, CPU, disk write rate/run size, cleanup/orphan workers |

GT annotations phải độc lập output: nếu scene thiếu object-instance/room-function GT phù hợp thì tạo annotation rõ phạm vi; chưa có GT thì báo not_scored, không tự gán accuracy. Với object recall toàn scene cần exhaustive GT hoặc chỉ báo recall trong visible annotated population. Review prefix early/middle/late và final để tìm lỗi bị final merge che mất. Ưu tiên false merge, cạnh phòng sai và cạnh nav xuyên obstacle hơn việc ép tăng số object.

Quality gate ban đầu: không có false edge trong fixtures; trên dữ liệu thật đo baseline rồi đặt ngưỡng promotion trước experiment, không bịa target accuracy chưa có annotation. Báo precision đi cùng recall và unassigned coverage để tránh đạt precision bằng cách không tạo quan hệ nào. Chưa có quality evidence thì chỉ nghiệm thu workflow.

### 14.3. Ablation hữu ích

- A: fixed route baseline cũ, giữ đúng điều kiện và ghi rõ khác budget/input nếu không matched; chỉ dùng để hiểu continuity hiện có.
- B: nearest-frontier + scheduler baseline trên cùng spawn/budget mới.
- C: information-gain frontier + scheduler bảo vệ novelty.
- D: C + bounded observation/revisit cho graph quality.

So sánh B/C/D trên cùng model, taxonomy, threshold, floor policy và run budget; ghi tốc độ, số keyframe processed và idle ratio. Frontier làm coverage tốt hơn không tự bảo đảm identity/room accuracy tốt hơn. Không dùng 58 giây của v5 đối đầu 600 giây mới để kết luận thuật toán vượt trội.

## 15. Failure handling và artifacts bàn giao

| Tình huống | Phản ứng |
|---|---|
| Mapper chậm | Backpressure, giảm tốc, observation pause; preview vẫn sống |
| Mapper mutate rồi lỗi | Mark failed/replay_required; không retry cùng packet trên state không rõ |
| Depth/map stale | Controller phanh; scan/recover chỉ khi dữ liệu mới hợp lệ |
| UI đóng/crash | Supervisor dừng episode, cleanup process owned; lưu last valid snapshot |
| Goal bị chặn | Replan/blacklist tạm, không teleport qua obstacle |
| Room scope không rõ | Membership unavailable, không bịa edge hoặc label |
| CPU/RAM/disk vượt ngân sách | Giảm tải được phép, rồi controlled stop có reason |
| Deadline gặp inference chưa xong | Hủy admission, bounded drain; không chờ vô hạn |

Mỗi run xuất: effective config + hashes/version, scene/spawn/seed, dependency versions, clock domains, pose/action log, nav/map/frontier decisions, raw admitted RGB-D và packet hashes, paired graph revisions/geometry refs, semantic ledger, metrics, stop reason và viewer replay entrypoint. Tách immutable evidence inventory với mutable logs/live pointers để tránh lỗi whole-tree hash của pilot cũ. Không rewrite failed runs hoặc backfill prefix từ final graph.

Deliverable triển khai hoàn chỉnh: launcher, desktop app ba panel, exploration module, scope/membership cải tiến, config CPU, bộ tests thiết yếu, demo replay 10 phút, report tài nguyên và quality có giới hạn rõ, hướng dẫn chạy và khôi phục.

## 16. Việc tiếp theo theo trạng thái hiện tại

1. Đã profile lại 18 snapshot cuối của các run đã lưu, mỗi snapshot lặp 5 lần; artifact `runs/navmap-audit-20261023.json`. Median trên các p95 theo snapshot: NavMap stride 4 khoảng 139 ms (51,7–184 ms), stride 8 khoảng 35,8 ms (12,7–61,1 ms), known-free flood 55,8 ms (26,4–88,1 ms), footprint flood 21,9 ms (0,02–61,7 ms), frontier extraction 103,7 ms (47,6–200,3 ms), A* 24,5 ms/goal trên 12 map có goal reachable (0,67–46,6 ms). Đây là replay của một final snapshot mỗi run, không phải timeline replay; timing phụ thuộc host và không chứng minh realtime. Artifact floor-neighbor counterfactual là `runs/navmap-floor-support-audit-all-20261023.json`.
2. Planner worker và NavMap worker đã tách khỏi runtime tick; map snapshots bất biến, bounded pending/drop có telemetry, planner goal từ revision cũ chỉ dùng sau age/scope check và A* replan trên current map/pose; freshness/safety gate vẫn tại tick. Run duy nhất của pass này (`p2-async-planner-120s-20261022-a`) fail: 38 `depth_stale`, 14 `no_valid_path`, 0 m và không goal; nó chạy trước async mapping/safe rebase. Không tự retry sau run fail; giữ artifacts và dùng một validation pass mới để xác minh implementation mới.
3. P2 locomotion đã đạt qua hai episode navigation-only. Run `p2-split-depth-fast-motion-120s-20261022-a` có report `movement_gate_report.json`. Validation mới nhất `p2-floor-calibrated-footprint-reach-120s-20261023-a` chạy 120,38 s/810 frame, displacement 0,834 m, 2 viewpoint distinct trong tolerance 0,10 m, 107 forward actions, 0 collision/filter block; evaluator `runs/p2-floor-calibrated-footprint-reach-120s-20261023-a/movement_gate.json` báo PASS. Journal có 24 controller stop ticks do `nav_stale`, 2 do `depth_stale`; ngoài ra policy ghi 71 tick `nav_stale` khi chưa có/không kịp snapshot mới. Vì vậy chỉ nghiệm thu locomotion gate, chưa nghiệm thu freshness/realtime. Evaluator đã tách controller stop và policy wait để không gộp hai chỉ số này.
4. Live graph gate đã chạy 120 s trong cùng tiến trình exploration ở run `runs/integrated-split-depth-motion-graph-120s-20261022-a/`: 60 paired commits, 2 viewpoint arrivals. Run tích hợp lặp lại sau floor calibration ở `runs/integrated-floor-calibrated-graph-120s-20261023-a/`: P2 worker và provisional graph plugin chạy live; 59/59 keyframe được persist/admit/process, publish 59 revision, 0 reject/replacement/worker failure, queue drain về 0; graph cuối có 117 nodes/235 edges. Median/p95 gap giữa revision khoảng 2,07/2,91 s. Run kết thúc FINISHED sạch; live state giữ frame RGB 664, NavMap đến frame 663/revision 381 và graph revision 59 đến frame 652. Tuy nhiên integrated movement gate **fail**: 1,072 m net nhưng 0 viewpoint arrivals; target gần nhất cách robot 0,24 m. Đây không phủ định locomotion-only P2 pass; combined P2+graph load/policy chưa qua cùng acceptance. Backend vẫn là provisional P2+RGB-D hypotheses, chưa phải native ConceptGraphs/OccuSG và chưa chứng minh graph quality. Desktop interactive có display với Start/Pause/Resume/Stop, frame age/lag và capture FPS vẫn cần kiểm tra; artifact offscreen không thay cho bước này.
5. Đã có compatibility exporter `exploration/legacy_capture.py`, clock/hash packet builder `exploration/legacy_mapper_packets.py`, và round-trip qua Habitat frame adapter cũ. Preflight read-only xác nhận interpreter và asset cho native mapper đều hiện diện (xem `scripts/preflight_legacy_mappers.py`). Runtime dùng ba môi trường Python riêng: ConceptGraphs 3.10/CPU, OpenCLIP 3.12/CPU, OccuSG 3.12/NumPy 2.5; không import chúng chung vào Habitat Python 3.9. Packet fixture đã kiểm tra frame/clock/hash và joined P2/P3/feature ID, nhưng builder chưa nối vào plugin và chưa có producer P3/OpenCLIP live. Tiếp theo: chạy persistent worker client riêng, tạo P3/OpenCLIP rows từ cùng keyframe, validate bằng worker `--preflight`, rồi replay native OccuSG trước paired live publication. Giữ project tham khảo read-only, provisional backend làm fallback; không gán room membership khi scope chưa đủ.
6. Thêm room/elevation scope và membership chỉ từ evidence; đánh giá object/room/relations bằng GT độc lập, rồi chạy 10 phút/resource gates và mở rộng target selector ObjectNav.
7. Theo rà soát tích hợp native: keyframe store lưu cả depth phụ cùng intrinsics/pose/range/hash; adapter OccuSG đã tích hợp depth đó, còn ConceptGraphs dùng RGB-D chính. Policy chỉ chốt baseline sau scheduler ACK; graph worker giữ commit counter/frame ID; live stream gắn generation ID và SHA-256. Floor evidence đã được hiệu chỉnh theo endpoint và config floor riêng cho scene/spawn HM3D 00802. RSS cap 6 GiB và resource monitor đang chạy; peak gần nhất là 3,595 GiB. Native graceful shutdown đã pass run 30 s với in-flight commit hoàn tất và pending IDs được giữ để replay; vẫn cần kiểm tra process crash, memory slope và soak dài.
8. Policy/controller/evaluator nay dùng tolerance đến viewpoint phù hợp robot footprint và độ phân giải grid; policy ghi sự kiện `frontier_viewpoint_reached` có goal ID để evaluator không phải suy diễn từ lệnh vận tốc. Regression mới nhất: `55 passed`; `compileall` và `git diff --check` sạch. Gate 120 s chưa đánh giá số viewpoint gần nhau theo chất lượng semantic; evaluator dedupe theo vị trí lượng tử 5 cm.
9. Trạng thái chuyển tiếp mới nhất: regression `41 passed`; P2 navigation-only `0,909 m`/2 viewpoint. Native replay thật qua ConceptGraphs + OccuSG pass; auxiliary depth được tích hợp 19.200 điểm với calibration riêng. Native 120 s live đạt `1,972 m`/3 viewpoint, 11 paired revisions/aux integrations, graph lag 17 frames, RSS peak `3,723 GiB`. Tiếp theo chạy soak `2×300 s` trên hai spawn cùng HM3D 00802 đã hoàn tất: movement gates pass, graph 43 paired revisions và 43 aux integrations, worker shutdowns sạch; RSS peak `3,946 GiB` dưới cap. Còn 29 collision/filter blocks, 316 policy NavMap-stale waits, graph lag cuối 93–104 frames. Artifact `runs/native-soak-10m-multispawn-20261023.json`. Các điểm tiếp theo là giảm collision/recovery và map wait, cải thiện graph freshness, chấm room/object accuracy và semantic join; hiện chưa nghiệm thu multi-scene hoặc accuracy.
10. Trên HM3D 00808, thêm telemetry goal-selection và phát hiện cùng frontier ID có thể dịch viewpoint xa tới `0,85 m`; giữ target cũ nếu nó còn FREE/reachable khi cùng ID nhảy hơn `0,60 m`. Regression mới nhất `46 passed`; navigation-only episode 120 s `native-live-hm3d-00808-120s-stable-goal-20261023-d` PASS với `1,904 m` và 2 viewpoint. Kế đó chạy native P2 + ConceptGraphs/OccuSG 120 s: 7 paired commit, 7/7 frame identity/source hash khớp, OccuSG nhận auxiliary depth đủ 7 frame; worker khỏe và peak RSS `3,874 GiB`. Tuy nhiên combined movement gate FAIL (`0,348 m`, 0 viewpoint), có 54 controller/175 policy NavMap-stale ticks; graph cuối trễ 91 frame. Artifact `runs/native-live-hm3d-00808-stable-goal-native-graph-validation-20261023.json`. Chưa nghiệm thu UI display-attached, semantic join, graph accuracy hoặc đa-scene mới. Tiếp theo profile CPU contention và điều tiết worker/admission để chạy lại combined gate trước soak; không giảm độ phân giải hoặc tắt sensor nếu chưa kiểm tra chất lượng geometry.
11. M2 transport workspace: golden packet đầu thiếu `projection_mode`; đã sửa spec, thêm `exploration/native_m2_wire.py` và parser tham chiếu C++ `native/m2_v2_codec.hpp`. Regression hiện **72 passed** gồm fixture codec và shutdown-deadline checks. Golden frame 146: `3.724.964 B`, text `17.947.434 B` (`4,82×` nhỏ hơn), encode median `6,74 ms`; 307.200 điểm decode qua Python và C++ fixture, nhưng fixture không có no-return. Text encode dao động `1,74–2,00 s` trong lần đo 3 lượt; chưa coi encode wall là runtime kết luận. Đây chưa phải OccuSG integration, chưa có server parser/response hoặc map parity. Đã rà read-only upstream path: cần sửa `native_pipeline.py`, `native.py` và `native/occupancy/fixture_runner.cpp`, rồi build binary; các file này ở sibling checkout ngoài writable root. Bước này cần quyền ghi/build riêng; sau đó replay golden + 11 frame để so exact M2 keys, M3 grids, room/object hashes và NavMap trước live. Không giảm stride/nới freshness.
12. Correctness pass trong workspace: viewer xác minh SHA của các array trước khi commit cả generation; planner rebase giữ blocked-edge overlay; native graph provenance kiểm tra auxiliary-depth diagnostics/hash; run summary phân biệt backend native; graph/runtime/P2/provider dùng cùng shutdown deadline và P2 terminate/kill không cộng timeout vượt deadline. `py_compile` sạch. Regression liên quan viewer, planner/runtime, graph worker, P2 và M2 codec: **72 passed**, gồm fixture hash mismatch, blocked-edge rebase và deadline forwarding. Git baseline đã được tạo: `15b78ec Establish 3DSG source baseline`; run artifacts vẫn bị ignore. Chưa chạy Habitat simulator sau sửa.
13. Movement evaluator báo path length/efficiency, goal switches, wait/stale fractions, `total_tick` latency và khoảng cách viewpoint; PASS threshold không đổi. Đã chạy offline trên ba run lưu: nav-only `0,908 m` net / `1,414 m` path, efficiency `0,642`, 2 greedy-separated viewpoints ≥0,5 m; P2/demo `0,485/1,640 m`, efficiency `0,296`, 2 viewpoint; native paired `0,159/0,160 m`, 260/543 policy waits, 95 stale-command ticks, 0 viewpoint. Hai control PASS gate; native FAIL. `total_tick` latency không đại diện latency worker/native. Bước tiếp là giảm native M2 processing/backpressure; chỉ lặp combined 120 s sau thay đổi có parity evidence, không chạy lại cùng cấu hình đang fail.
14. Kiểm soát lại trên HM3D 00802 spawn 2 cho thấy navigation-only 120 s PASS movement gate cơ bản (`0,453 m`, 5 viewpoint) nhưng FAIL spatial quality (chỉ 1 viewpoint cách ≥`0,5 m`); map-age p95 `750 ms`, planner-result stale 25. Combined native P2 + ConceptGraphs/OccuSG trên cùng cấu hình FAIL movement gate (`0,317 m`, 0 viewpoint), có 63 command NavMap-stale ticks, map-age p95 `1,223 ms`, 104 map drops tối đa và graph-age p95/max `19,343/25,188 s`. **Đính chính:** run mang tên `*-binary-*` chỉ yêu cầu binary qua biến môi trường; upstream OctoMapWorker vẫn serialize ASCII vì không đọc biến và không truyền `--m2-input-mode binary`. Vì vậy live/replay này là ASCII; binary fixture/parity riêng không chứng minh binary native live. Paired graph revisions=9, queue cuối không còn pending, peak RSS `3,360 GiB`; viewer state cuối có RGB/depth frame 562, NavMap frame 559, graph revision 9 với graph age riêng. Branch timing: OccuSG rooms processing median/p95/max `6,882/7,550/8,472 ms`; ConceptGraphs objects `298/539/545 ms`. Replay đúng 9 frame `[0,25,65,167,188,221,226,393,461]` cho room/object hash exact-match tại frame 461, 9/9 paired ack và worker finalize sạch; tái hiện room processing median `6,814 ms`, primary M2 `3,881 ms`, auxiliary M2 `216 ms`. Đối chứng cùng frame với constructor `standard` cũng exact-match; một lượt A/B chưa cho thấy constructor thắng ổn định. Artifacts: `runs/native-m2-profile-spawn2-control-replay-20261024-a/summary.json` và `runs/native-m2-profile-spawn2-control-standard-replay-20261024-a/summary.json` cùng hai live run ở trên; chi tiết trong [implementation_status.md](implementation_status.md). **Không chạy soak 10 phút hoặc đổi ranking goal lúc này.** Bước kế là nối binary input thật vào worker/backend được phê duyệt, replay golden + chuỗi keyframe với M2/M3/room/object parity, rồi lặp combined 120 s gate. Chỉ sau khi combined movement + freshness ổn định mới làm display-attached three-pane QA và 10-minute soak.
15. Binary M2 hiện đã nối và được xác nhận end-to-end: build M2/M3, OccuSG tests `15 passed`, Fusion M2 worker tests `10 passed`, workspace `91 passed`; golden frame 146 cho exact M2/M3 parity; replay 9 frame bằng binary exact-match room/object hash với live ASCII ở frame 461. Combined live thật `native-live-m2-binary-spawn2-actual-120s-20261024-a`: 12 frame diagnostics xác nhận primary+auxiliary đều binary; primary M2 median `1,612 ms`, OccuSG rooms `4,928 ms`, graph-age p95/max `13,457/17,725 s`, 12 paired revisions, RSS peak `3,344 GiB`. So với control ASCII cùng scene/spawn/profile, M2 median `3,302 ms`, room `6,882 ms`, graph-age p95 `19,343 s`, revisions 9; đây là một lượt A/B có thứ tự cố định, không randomized. Run mới vẫn FAIL movement (`0,373 m`, 1 viewpoint; cần 2), spatial coverage (chỉ 1 cách ≥`0,5 m`) và map freshness (p95 `1,151 s`, 60 command NavMap-stale, max map-drop counter 108). Artifacts ở `runs/native-live-m2-binary-spawn2-actual-120s-20261024-a/` và `runs/native-m2-binary-spawn2-control-replay-20261024-a/`; chi tiết trong [implementation_status.md](implementation_status.md). Bước kế: giữ binary mode; profile/replay tối ưu primary M2/room processing có parity, rồi một combined 120 s rerun sau thay đổi. Không chạy soak 10 phút hoặc đổi safety/freshness gates cho đến khi movement/spatial/map freshness pass.

16. Run nice=5 `native-live-m2-binary-spawn2-nice5-120s-20261024-a` giảm map-age p95 `1,151→0,850 s`, drops `108→32`, stale commands `60→23`, policy stale `259→82`; movement cơ bản đạt `0,350 m`, nhưng spatial quality fail (viewpoint separation `0,05 m`). Hai pending frames `617/622` được catch-up 16-frame replay, room/object hash exact-match tại frame 507. **Lặp cùng cấu hình** `native-live-m2-binary-spawn2-nice5-repeat-120s-20261024-b` không tái hiện kết quả: movement `0,156 m` (FAIL), map-age p95 `1,347 s`, drops `108`, stale commands `103`, graph-age p95/max `14,014/18,507 s`. Pending frame `523` được replay binary; shutdown khoảng `8,29 s` dưới timeout `30 s` và frame được preserve để catch-up. Nice=5 chưa ổn định, không đặt làm mặc định. Kế tiếp chẩn đoán NavMap contention và goal diversity với gates không đổi; chưa chạy soak 10 phút.
17. Ablation `THREEDSG_NATIVE_MAPPER_NICE=5` (P2 mặc định) trên cùng spawn/profile/binary không đạt: `0,157 m`, 0 viewpoint, map-age p95 `1,074 s`, 79 drops, 326 stale waits; graph-age p95/max `26,279/32,573 s` vượt 20 s. Không tiếp tục dò niceness hoặc đổi default. Lập phân tích latency theo cửa sổ cho map/planner/graph và CPU usage, rồi chọn một thay đổi thuật toán/runtime có bằng chứng. Giữ nguyên freshness/safety thresholds; không soak.
18. Offline profile bốn binary run theo cửa sổ 20 s cho thấy map backlog tái hiện sau khoảng 40 s. Thay đổi nice không ổn định; mapper-only nice=5 còn đẩy graph age vượt ngưỡng. Không đổi cadence, stride hay gates.
19. Bổ sung `LatestMapWorker` telemetry wall time, `time.thread_time()`, queue wait và submit→publish; workspace **91 passed**. Combined binary run nice=2: 645 frames, movement `0,345 m`/3 reached viewpoints (spatial quality fail, chỉ 1 viewpoint cách ≥`0,5 m`), map-age p95 `768 ms`, 14 command-stale, 23 map drops, graph-age p95/max `11,182/13,125 s`. Trong 378 completions, map wall p50/p95 `120/456 ms`, thread CPU `48/84 ms`, median CPU/wall ratio `0,43`, queue-wait p95 `39 ms`, submit→publish p95 `486 ms`. Đây cho thấy thread CPU không giải thích toàn bộ wall time; `thread_time()` không tính CPU của NumPy/native worker threads nên chưa kết luận scheduler hay GIL. Hai pending frames `601/627` được catch-up 18-frame replay; room/object hash exact-match ở prefix 589, auxiliary depth tích hợp. Tiếp theo instrument/measure native thread activity hoặc profile scheduler/GIL; không đổi gates hoặc chạy soak trước spatial quality.

Kế hoạch này được cập nhật cùng implementation; các thông số đề xuất vẫn là giả thuyết tới khi benchmark. Module 3DSG cần giữ geometry/navigation graph hữu ích ngay cả khi perception/semantics tắt, và phải cho ObjectNav cấp trên thay target selector hoặc graph backend qua ports thay vì fork vòng điều khiển.

### Kết quả mới nhất — 24-10-2026

Combined native run `native-live-m2-binary-spawn2-progress-check-20261024-01` đạt đồng thời movement (`1,470 m`, 6 goal arrivals), spatial diversity (ít nhất 2 viewpoint cách nhau ≥`0,5 m`) và NavMap freshness (age p95 `394 ms`, 0 dropped map frames). 16/16 graph diagnostics xác nhận M2 binary cho cả depth chính và phụ; graph-age p95 `10,85 s`; peak RSS `2,295 GiB`. Hai frame chưa live-ack được replay cùng prefix: 18-frame catch-up pass và room/object hashes khớp tại committed frame 1157.

Đây mới là một lượt đạt, chưa đủ để gọi ổn định hoặc bắt đầu soak. Ưu tiên kế tiếp: chạy lại 120 s trên spawn khác đã xác nhận hợp lệ với cùng safety gates và cấu hình; đo thời gian shutdown/flush riêng; sau khi lặp lại đạt mới chạy soak 10 phút. Độc lập với workflow pass, room/object/membership accuracy vẫn cần annotation GT; kiểm tra desktop interactive trên màn hình thật vẫn chưa được nghiệm thu. Artifacts và giới hạn được ghi ở [implementation status](implementation_status.md).

Lượt lặp `native-live-m2-binary-spawn2-repeat-progress-check-20261024-b` cũng PASS movement/spatial (`0,996 m`, 4 arrivals, 2 viewpoint tách ≥`0,5 m`), map-age p95 `832 ms`, graph-age p95 `11,34 s`, RSS `3,352 GiB`; shutdown `5,34 s`, không còn pending frame. Tuy nhiên policy vẫn chờ NavMap ở `103/629` tick. Cả hai lượt thành công hiện đều dùng spawn 2; không có config spawn độc lập đã hiệu chuẩn. Vì vậy bước tiếp theo không phải soak ngay: tạo/kiểm tra spawn thứ hai trên navmesh, xác nhận floor/camera calibration bằng initial scan, rồi chạy cùng 120 s gate. Khi spawn holdout qua gates mới mở soak 10 phút. Xem chi tiết và giới hạn trong [implementation status](implementation_status.md).

Đã tạo config spawn 0 từ route start lịch sử và chạy thử. Preflight ngắn nạp map thành công; combined 120 s trên vị trí này **FAIL** spatial gate (1 viewpoint), policy NavMap-stale wait `270/603` tick, map-age p95 `1,051 ms`, 63 map drops, planner p95 `1,27 s`, graph-age p95 `22,94 s`. Giữ đây làm stress case. Không giảm freshness threshold hoặc tiếp tục soak; bước kế là phân tích cửa sổ thời gian để xác định tương quan giữa map queue, stale waits, goal hysteresis và graph scheduler, rồi chỉ thay đổi đúng bottleneck được chứng minh. Spawn 2 đạt hai lượt; generalization sang spawn 0 chưa đạt.

Phân tích cửa sổ 20 s cho thấy spawn 0 bắt đầu bão hòa sau 40–60 s: map-worker pending `0,89–0,99`, map-age p95 `1,28–1,70 s`, stale ticks `66–93/20 s`; queue-wait p95 tối đa `278 ms`, planner p95 tối đa `1,29 s`. Lượt spawn 2 lặp vẫn có pressure nhưng hồi phục ở cửa sổ cuối, và đạt movement/spatial. Decision logs cho thấy sau goal đầu có 6 frontier nhưng freshness gate dừng motion; goal hysteresis giữ mục tiêu gần, vì vậy evidence hiện tại ưu tiên xử lý queue/freshness trước khi chỉnh ranking. Artifact theo cửa sổ được lưu ở `diagnostics/windowed_runtime_profile.json` trong hai run; chi tiết tại [implementation status](implementation_status.md). Tiếp theo profile sâu map update / planner compute trên đúng snapshot và replay offline để tìm tối ưu giữ nguyên traversability/corner-cut; regression NavMap phải qua trước combined rerun. Chưa soak 10 phút.

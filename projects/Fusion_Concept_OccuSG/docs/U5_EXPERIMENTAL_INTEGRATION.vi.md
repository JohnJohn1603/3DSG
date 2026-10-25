# U5 experimental integration — M9 backend

## Phạm vi theo plan

U5 trong `FUSION_UPGRADE_PLAN.vi.md` nối object backend qua protocol `.update/.snapshot` và adapter M9; giữ source strategy, kiểm tra lifecycle/reconciliation, failure contract, M5/M8, snapshot/delta, fixed M5 partition và replay 29/280 frame. Lượt này thực hiện backend thật qua M9, source wrapper parity, geometry-only opt-in, và replay 29 frame từ cache. Không triển khai merge/alias/retirement, không thay room semantics, không navigation, không promote thuật toán.

## Implementation

- `SourceAssociationBackend` trong `src/standalone_sg/objects/u5_backends.py` bọc `ObjectAssociationManager`; source vẫn là default. Adapter M9 nhận protocol object backend từ `src/standalone_sg/pipeline/builder.py`.
- `GeometryOnlyAssociationBackend` là profile opt-in, nhận P3 theo thứ tự thời gian, dùng cấu hình U3 đã khóa và P2 mask aliases đã xác minh. M9 tạo object nodes từ assignment thực tế; unmatched/defer không tạo node. Không dùng class làm hard gate. Geometry+appearance chưa phải backend graph.
- U4-OV sidecar chỉ được nối vào observation diagnostics, kèm raw class tách khỏi fused label/status, evidence/config/checkpoint provenance và causal validation. Sidecar không đi vào association, graph class hay room scores. Cache 29 frame không có U4-OV sidecar tương thích nên lượt replay 29 frame chạy `semantic=off`; không báo raw fallback là fusion.
- Lifecycle protocol v1 định nghĩa active-canonical snapshot và append-only event history; M9/M10 event plumbing xử lý create/update/canonical-alias/retire, canonical ID, node/edge removal, membership reconciliation, provenance và causal revision. Alias/retirement có synthetic scripted-backend coverage, không phải policy merge/retire. Source và geometry backend thật hiện chỉ phát create/update; chưa có policy quyết định merge/retire và lượt này không quan sát event thật. Gate U5 vì vậy còn **partial**, với policy deferred. Snapshot reload khôi phục graph cùng lifecycle history, không khôi phục toàn bộ trạng thái runtime bên trong backend.
- Contract lỗi của M9 vẫn giữ nguyên: graph candidate chỉ commit sau khi mọi bước fallible xong; exception sau khi stage/backend mutation bắt đầu giữ snapshot graph đã commit và đặt builder vào `failed/replay-required`. Backend rollback không được hứa.
- Runner kiểm tra native binaries theo `locks/external_resources.json`, hash, executable bit; `check --native-resources` cũng xác nhận `ldd` không có dependency thiếu và chạy probe. Manifest mỗi run lưu path/hash/size và lock hash. Không rebuild/copy build tree/install hoặc sửa baseline.

## Launcher

Từ workspace:

```bash
python3 scripts/scene_launcher.py check --native-resources
python3 scripts/scene_launcher.py replay-cached /home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/artifacts/runs/hm3d_integration_20261005/00802/main --profile source
python3 scripts/scene_launcher.py replay-cached /home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/artifacts/runs/hm3d_integration_20261005/00802/main --profile geometry-only
python3 scripts/scene_launcher.py results <run-id>
python3 scripts/scene_launcher.py open <run-id> u5
```

`replay-cached` verifies capture/P2/P3 manifests and hashes, reuses those caches, passes both locked binary paths explicitly to native replay, and writes a unique output under `artifacts/runs/<run-id>`. It does not rerun capture, P2, P3, YOLO, or OpenCLIP inference. It **does rerun native M1–M3 RGB-D mapping** from the cached capture; cached perception is not a mapping cache. `--semantic u4ov` is available only when a provenance-compatible cached sidecar exists. Existing commands remain available. `open <run-id> u5` selects the sidecar HTML when present; otherwise it opens the graph result.

## Results

Native resources from the read-only baseline:

| Resource | SHA-256 | Size | Runtime dependency check |
|---|---|---:|---|
| M2/M3 | `bd171a8ac9974eec18957af97e6fc369729358ca62985390eb994c26f2040b29` | 422,552 B | `ldd`: pass; no missing dependencies |
| DuDe M4 | `7868c892f3584faa17712e1ede0660006d97531b05be5c258fe303015f1ef959` | 940,344 B | `ldd`: pass; no missing dependencies |

Source manager and wrapper now produce identical graph snapshots/deltas in a fresh M9 parity regression, including lifecycle state. The earlier 5-frame byte-parity run predates lifecycle serialization; current snapshots carry the v1 lifecycle record and are not claimed byte-identical to older baseline artifacts. Source wrapper with the compatible U4-OV sidecar retained provenance, while the sidecar was excluded from graph and room-score inputs.

On the locked 29-frame HM3D 00802 cache:

| Profile | Objects | Runtime | Peak RSS | Fresh replay / reload / delta |
|---|---:|---:|---:|---|
| Source wrapper | 9 | 129.9 s | 543,352 KiB | pass / pass / pass |
| Geometry-only | 11 | 144.8 s | 549,008 KiB | pass / pass / pass |

The cache is locked in `locks/u5_m9_00802_29frame.lock.json`; no perception was rerun. Native M1–M3 mapping was rerun from cached RGB-D. Geometry-only assignments matched an independent empty-state U3 geometry replay on the same P3 packets: 12/12, zero mismatches. M3 grid arrays and M4 result fields were equal across profiles on all 29 frames; M5 room geometry matched after excluding dynamic object membership. Both graphs had zero dangling edges, self edges, and conflicting room-owner targets. Source wrapper and geometry-only each retained 5 rooms. Reproduction details are saved at `artifacts/runs/u5m9_20261009T063525.490715Z_455818/replay/backend_comparison_m2_m5.json`.

The smoke capture in `artifacts/runs/u5_m9_native_smoke` separately exercised semantic sidecar provenance and geometry replay: geometry assignments matched the offline reference 30/30. These are technical integration checks, not evidence of quality improvement.

## Tests and gate status

- Targeted backend/lifecycle/launcher/resource regression — **81 passed**; focused post-fix backend/lifecycle/serialization/resource regression — **25 passed**. Source wrapper parity compares complete M9 snapshots/deltas. `results` reports backend, semantic status and lifecycle-policy deferral.
- Resource-bound integration suite: `python3 -m pytest --integration-resources -q` — **363 passed, 0 failed, 0 skipped** in 86.51 s. It requires the baseline lock, native binaries and direct-source oracle through the shared resolver; missing resources fail before collection. The earlier 19 failures and 20 skips are itemized in [resource/lifecycle handoff](U5_RESOURCE_LIFECYCLE_HANDOFF.vi.md). All skips were run and passed; the M11 output-manifest issue was fixed without weakening parity assertions.
- No 280-frame replay was run. The 29-frame run took about 2.5 minutes per profile; dense replay should be considered only after the remaining lifecycle/reconciliation gate is addressed and a runtime budget is agreed.

**Status:** U5 backend plumbing is integrated and exercised end-to-end through M9 for source wrapper and opt-in geometry-only. Source is still default. Geometry-only is experimental; appearance is ablation-only. Semantic sidecar remains diagnostics-only. M9/M10 lifecycle reconciliation has synthetic contract coverage; real backends do not yet emit alias/retirement and policy remains unsupported/deferred. U5 plan gate is **partial**. No claim of improved association/class/room quality.

Backlog remains separate: reduce U4-OV abstention; expand vocabulary under its own protocol; address false merge/fragmentation. These were not changed in this integration pass.

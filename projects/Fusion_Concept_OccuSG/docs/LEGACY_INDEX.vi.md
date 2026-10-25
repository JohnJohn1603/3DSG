# Legacy index — lịch sử U/K/Q và các prototype trước kiến trúc active

Ngày cập nhật: 2026-10-12.

> **Trạng thái:** nội dung bên dưới là kế hoạch, kết quả thử nghiệm hoặc handoff lịch sử. Không dùng để chọn nhiệm vụ phát triển tiếp theo. Kiến trúc và thứ tự hiện hành chỉ nằm trong [CONCEPTGRAPHS_OCCUSG_REBUILD_PLAN.vi.md](CONCEPTGRAPHS_OCCUSG_REBUILD_PLAN.vi.md). Tài liệu được giữ nguyên tại chỗ để bảo toàn relative links, artifact paths và lịch sử nghiệm thu.

## Tài liệu active

- [Architecture overview](ACTIVE_ARCHITECTURE.vi.md)
- [ConceptGraphs → OccuSG rebuild plan](CONCEPTGRAPHS_OCCUSG_REBUILD_PLAN.vi.md)
- [R0/R1 preflight](REBUILD_R0_R1_PREFLIGHT.vi.md)
- [R2/R3 Kaggle handoff](R2_R3_KAGGLE_HANDOFF.vi.md)
- [Cleanup plan](PROJECT_CLEANUP_PLAN.vi.md)
- [Current exact-path cache candidates](CLEANUP_CANDIDATES_CURRENT_2026-10-12.json)

## Historical plans and handoffs

| File | Historical scope |
|---|---|
| [FUSION_UPGRADE_PLAN.vi.md](FUSION_UPGRADE_PLAN.vi.md) | U0–U4 baseline upgrade plan, superseded as active roadmap. |
| [NEXT_AGENT_TASK.vi.md](NEXT_AGENT_TASK.vi.md) | Handoff for an earlier U4/U5 transition. |
| [OBJECT_GRAPH_IMPROVEMENT_PLAN.vi.md](OBJECT_GRAPH_IMPROVEMENT_PLAN.vi.md) | K-series quality work, including K4 accumulated geometry. |
| [K4_ACCUMULATED_GEOMETRY_EXPERIMENT.vi.md](K4_ACCUMULATED_GEOMETRY_EXPERIMENT.vi.md) | Offline K4 experiment; not the current object backend. |
| [OBJECT_GRAPH_QUALITY_RECOVERY_PLAN.vi.md](OBJECT_GRAPH_QUALITY_RECOVERY_PLAN.vi.md) | Q-series quality-recovery plan, superseded as active roadmap. |
| [OBJECT_GRAPH_QUALITY_RECOVERY_Q0_Q1_HANDOFF.vi.md](OBJECT_GRAPH_QUALITY_RECOVERY_Q0_Q1_HANDOFF.vi.md) | Q0/Q1 offline association runner and pilot handoff. |
| [OBJECT_FUSION_QUICKSTART.vi.md](OBJECT_FUSION_QUICKSTART.vi.md) | Fixed-assignment legacy geometry-fusion utility. |

## Historical experiment records

These reports preserve measurements, config hashes, human annotations and limitations. Their conclusions remain valid only for the stated datasets/configurations; they do not authorize resuming the old roadmap.

- **U0–U2:** [U0/U1](U0_U1_STATUS.vi.md), [U2](U2_STATUS.vi.md), [U4-OV prototype](U4_OV_PROTOTYPE.vi.md).
- **U3 association:** [U3 status](U3_STATUS.vi.md), [causal prefix](U3_CAUSAL_STATUS.vi.md), [duplicate review](U3_DUPLICATE_PARTITION_REVIEW.vi.md), [candidate v2 design](U3_DUPLICATE_CANDIDATE_V2_DESIGN.vi.md), [candidate v2 evaluation](U3_DUPLICATE_CANDIDATE_V2_EVALUATION.vi.md), [same-frame candidate report](U3_SAME_FRAME_DUPLICATE_CANDIDATE_REPORT.vi.md).
- **U4 semantics:** [U4 status](U4_STATUS.vi.md), [pre-review scope](U4_PREREVIEW_SCOPE.vi.md), [human review/temporal test](U4_USER_REVIEW_AND_TEMPORAL_TEST.vi.md), [00800 diagnostic](U4OV_HM3D_00800_DEV_DIAGNOSTIC.vi.md), [00800 resolution/multiview](U4OV_HM3D_00800_RESOLUTION_MULTIVIEW_REPORT.vi.md), [00800 test report](U4OV_HM3D_00800_TEST_REPORT.vi.md), [user review score](U4OV_HM3D_00800_USER_REVIEW_SCORE.vi.md).
- **U5 integration:** [experimental integration](U5_EXPERIMENTAL_INTEGRATION.vi.md), [resource/lifecycle handoff](U5_RESOURCE_LIFECYCLE_HANDOFF.vi.md), [duplicate-v2 M9 report](U5_DUPLICATE_V2_M9_INTEGRATION_REPORT.vi.md).
- **U6 evaluation:** [protocol preparation](U6_HM3D_00808_PROTOCOL_PREP.vi.md), [protocol v1.1](U6_HM3D_00808_PROTOCOL_v1.1.vi.md), [quality report](U6_HM3D_00808_QUALITY_REPORT.vi.md), plus review artifacts under `artifacts/reviews/` and `artifacts/audits/`.
- **Fixed-assignment / visibility diagnostics:** [comparison](FIXED_ASSIGNMENT_OBJECT_FUSION_COMPARE.vi.md) and the pixel-visibility diagnostic records in `artifacts/audits/`.
- **Baseline reference:** `docs/baseline_reference/` remains historical source/contract context and is not the active ConceptGraphs integration plan.

## Preservation rules

- Do not delete or move these documents as part of navigation cleanup. Relative links and hard-coded paths have not been comprehensively rewritten.
- Preserve associated captures, annotations, review manifests, cache/sidecar lineage, evaluator outputs and resource locks. A report may be the only explanation of a later metric or user decision.
- Source/default behavior is unchanged. The old launcher is not a ConceptGraphs integration entrypoint.
- Cache cleanup candidates are only proposals; see [the exact-path manifest](CLEANUP_CANDIDATES_CURRENT_2026-10-12.json). No deletion or archive action has been performed.

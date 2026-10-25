# Navigation progress fixes — 2026-10-24

Follow-up: the next longer run exposed an unresolved front-brake/heading
cycle. See [the corner recovery correction](frontier_corner_fix_20261024.md)
for the reproduced failure, subsequent fix, and continuity audit.

## Problem confirmed from the journal

`native-live-m2-binary-spawn2-progress-check-20261024-05` traveled 3.82 m
but finished only 0.462 m from its spawn. Its last successful forward action
was at 175.11 s, followed by about 65 s without translation. NavMesh rejected
attempts that advanced only 1.9–2.7 mm of a requested 20 mm. Only eight
commands stopped for stale depth/map evidence. The existing evaluator's small
movement threshold did not exclude this local wandering.

## Changes

- The controller checks straight segments to lookahead points against the
  planner's footprint-safe observed grid, including diagonal side cells. It
  falls back to nearer path waypoints instead of cutting blocked corners.
  Heading alignment immediately stops linear motion. Arrival uses the final
  path endpoint, not an intermediate lookahead point.
- Runtime keeps its semantic goal while displaying temporary WAIT/RECOVERY
  states. A real arrival clears the local path, requests another plan, and
  sends the exact reached world position to the background policy. Arrival
  results are checked against the current pose even on the same map revision.
- Reached-point spatial memory is enabled at 0.30 m in `cpu_p2_motion.yaml`.
  Failed-target memory is enabled at 0.20 m and expires after 100 map revisions.
  The memory survives frontier component ID changes. In-flight outcomes for
  a recently failed endpoint are rejected by the runtime too.
- Translation requests without 0.05 m of displacement time out after 8 s of
  accumulated commanded time. Alignment/evidence waits pause this timer.
- A persistent front-obstacle brake triggers goal rejection and bounded
  rotation recovery after 1 s, only with fresh observed depth and map data.
  Repeated obstacle recovery turns consistently rather than undoing its last
  rotation. Stale evidence continues to stop all movement.
- Commands carry a host-time integration interval capped at 0.10 s. Habitat
  still executes at most one filtered 20 mm move or 6 degree turn for each
  observation, and its simulation timestamp still advances by 0.04 s.
  This distinction matters when interpreting wall-time speed versus simulator
  time. Angular remainder is bounded symmetrically in both turn directions.
- The desktop supports `--autostart`; Pause and Stop retain their existing
  behavior.

## Verification

`QT_QPA_PLATFORM=offscreen python3 -m pytest -q tests`: **138 passed**.
Habitat Python compilation and `git diff --check` passed.

The first 120 s implementation trial, before exact-arrival messages and
host-paced action integration, had net displacement 0.422 m, path length
0.460 m, and one collision/filter block. Its spatial-coverage gate failed;
it is not evidence that the final implementation passed that gate.

An intermediate live demo reached net displacement 2.719 m and path length
3.120 m, then exposed a persistent front-obstacle brake. That recording
(`desktop-20261024-225840-2fe136`) predates the brake recovery fix.

Live verification `runs/desktop-20261024-231018-0f200f` completed with a
controlled user stop after 119.73 s: path length **5.190 m**, net displacement
**3.342 m**, and **5** spatially distinct reached viewpoints at 0.5 m separation.
Movement, spatial coverage, and overall evaluator gates passed. There were
3 filter blocks and 1 stale navigation command. A front-brake episode lasted
1.073 s, then recovery resumed movement. The graph worker shut down cleanly.
Artifacts: `movement_gate.json`, `progress_observation.json`, `run_summary.json`.

After this run, a final unit-tested guard also ensures a front obstacle must
still be present in the current frame before triggering timeout recovery.
The interactive demo was restarted with that guard. A short verification is
not a completed 10 minute soak. Graph counts and free-cell area are not
ground-truth accuracy.

The restarted demo is `runs/desktop-20261024-231311-072081`; its live dashboard
audit confirms RGB/map loaded, graph revision 1, and no snapshot hash mismatch.
`live/demo_dashboard.png` is rendered directly from the dashboard widget,
without capturing any foreground application.

## Launch the live desktop

```bash
THREEDSG_GRAPH_WORKER_NICE=2 OCCUSG_M2_TRANSPORT=binary \
  /home/kiettran05/miniconda3/envs/habitat/bin/python scripts/run_desktop.py \
  --autostart \
  --scene-config configs/exploration/hm3d_00802_frontier_spawn_2.json \
  --profile configs/exploration/cpu_p2_motion.yaml \
  --duration-s 600 \
  --graph-plugin exploration.native_graph_plugin:create_plugin
```

The window owns its simulator process. Use **Pause** or **Stop** in its toolbar.
The initial 360 degree scan is intentional. Capture the dashboard widget
directly for screenshots: screen grabs of an occluded X11 window can contain
the foreground application instead.

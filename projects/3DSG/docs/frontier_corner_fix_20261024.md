# Frontier corner recovery: 2026-10-24

## Reproduced failure

The user's latest run, `desktop-20261024-231311-072081`, reached
`(2.843859, 6.237678)` and stopped translating after about 33 seconds.
The remaining approximately 405 seconds repeatedly alternated heading
alignment, front-obstacle braking, goal rejection and recovery rotation.
Changing distant frontier goals did not change the required local turn.

The saved map still has a valid observed-grid A* route. Replaying the last
camera depth gives a nearest frontal obstacle of **0.247869 m**, just inside
the 0.25 m translation brake. The lookahead is `(2.542157, 6.507843)`, to the
robot's left. The old controller checked the frontal brake before computing
the path heading and stopped both translation and yaw. Its recovery rotated
away from the desired route; subsequent alignment rotated back into the
same brake. A 20 degree recovery could therefore repeat indefinitely.

Saved replay evidence is in
`runs/corner-navigation-diagnosis-20261024/saved_corner_controller_replay.json`.
This uses saved observed map/depth, not navigation ground truth.

## Correction

- Freshness, depth coverage, map revision, and path/segment validity still gate
  every control command.
- A frontal obstruction prevents translation, while a valid route can request
  in-place yaw as `obstacle_align_heading`. This continues below the normal
  50 degree alignment threshold until a 6 degree deadband, so the robot can
  finish turning out of a corner. Linear velocity is zero immediately.
- Once aligned, a still-blocked route produces the existing front brake and
  bounded recovery. The runtime now evaluates that timeout **after this
  tick's controller command**, so an old brake timer cannot cancel a newly
  selected route that requires heading alignment.
- Frontier extraction and A* remain the source of goals/routes; this fix adds
  no route script, random walk, or ground-truth goal selection.

The cylindrical Habitat agent's footprint is invariant under in-place yaw;
the real simulator executor continues to check every forward step.

## Verification

All **155 tests passed**, including new blocked-front alignment tests at
90 and +/-40 degrees, the transition below 50 degrees, zero residual forward
velocity, freshness/path gates, and a regression for an expired brake timer
with a newly selected turn. Habitat Python compilation and diff whitespace
checks passed.

`codex-corner-escape-check-20261024-a` starts at the actual recorded corner
position and yaw with a freshly observed map. It ran P2, native ConceptGraphs
and OccuSG, and binary M2 transport for a 90 second exploration budget:

- path length **2.450 m**, net displacement **1.528 m**;
- **4** reached viewpoints spatially separated by at least 0.5 m;
- movement/spatial/overall gates passed;
- longest active-frontier stationary episode **7.712 s**, continuity PASS.

This physical reproduction resets mapping and policy memory; the saved-data
controller replay above separately verifies the original final map and goal.

## Continuity audit

The new read-only audit accepts complete and in-progress journals:

```bash
python3 scripts/audit_navigation_progress.py runs/RUN_ID --output /tmp/progress.json
```

It fails if the robot remains within a 0.05 m anchor radius for more than
30 seconds while frontier candidates remain active. Initial scan and periods
with no frontier candidates are excluded. It reports reason counts and
30 second motion windows. The user's failed run correctly fails this check
with a stationary episode of about **405.3 seconds**.

Continuity is an additional diagnostic: it does not establish complete scene
coverage, semantic accuracy, or exploration optimality. Sustained movement
can still revisit known regions, so spatial viewpoint and coverage proxies
must also be inspected.

## Frontier candidate exhaustion discovered during the longer trial

The controller-only follow-up, `desktop-20261024-233027-440592`, stopped at
`(2.541062, 6.139142)` after reaching three local viewpoints. Its two frontier
components each exposed only one viewpoint; both were inside visited disks.
Telemetry reported two spatially suppressed candidates, no reachable goal,
and zero path failures. This was candidate-generation exhaustion, not an A*
failure or proof that the scene had been explored.

The extractor now supports up to four spatially separated viewpoints per
component in `cpu_p2_motion.yaml`. Already reached world positions are passed
from the policy-owning thread to extraction **before** sampling the complete
reachable candidate pool. Sampling repeats on every map, allowing a long
boundary to supply fresh viewpoints after its earlier samples are visited.
Alternates have their own world-coordinate target IDs. The global limit of
64 candidates remains, and each component receives its first view before
additional views consume that limit. Other profiles retain the default of
one viewpoint per component.

Replaying the exhausted map with its actual three reached points reproduces
the old failure (2 candidates, both suppressed). The new configuration
produces 8 unvisited candidates and selects `(2.325, 5.975)` with a valid A*
path. Saved replay extraction/planning took approximately 66/4 ms on this
machine; this is one diagnostic sample, not a runtime performance guarantee.
Evidence: `saved_frontier_alternatives_replay.json` in the diagnosis folder.

The combined implementation passes **164 tests**, including candidate
separation, footprint safety, rolling-origin identity, resampling after the
first sample batch is visited, and worker arrival/exclusion propagation.

## Planning load near the dining table

The multi-view trial `desktop-20261024-233651-a52f4b` exposed a CPU scheduling
failure after the candidate count grew to 55. After 120 s, 49 planner results
were all rejected as stale; worker duration median/p95/max was
1.828/2.669/3.280 s against the unchanged 1 s navigation freshness limit.
Map integration used about 0.099 s CPU but took up to 1.88 s at p95 in wall
time, consistent with contention while repeatedly searching for each goal.

The table screenshot corresponds to this run. The saved robot pose is only
0.173 m from its old target, but that target is not footprint-traversable in
the updated map (`goal_not_traversable`). Its center is FREE; a nearby blocked
cell prevents fitting the complete footprint. The current forward depth
minimum is 0.816 m, and there were no collision/filter rejections after 120 s.
Thus the evidence supports an invalidated goal and failure to receive a
fresh replacement plan; it does not establish physical contact with the table.

`AStarPlanner.plan_many` now computes all candidate shortest paths with one
shared Dijkstra wavefront, retaining footprint inflation, corner and blocked
edge rules. Single-goal A* remains available for runtime path revalidation.
Policy still ranks actual path costs and applies its previous hysteresis;
the held exact target is included in the same batch. No freshness limit is
relaxed. The dashboard displays WAIT when an active goal has lost its route.

Saved-map benchmark with 64 candidates selected the same target and the same
0.562132 m path cost: sequential policy planning **0.863 s**, shared planning
**0.0755 s** (extraction separately about 0.161 s). This is a diagnostic sample,
not a claim about every scene. Cost parity and geometry/budget regressions
bring the full test suite to **173 passed**. Independent review found no
blocking issue in the shared search or thread/cache ownership.

Additional diagnosis artifacts are `table_goal_occupancy_audit.json` and
`multi_goal_planning_benchmark.json` in the diagnosis folder. The previous
multi-view trial failed the continuity gate and is not reported as a pass.

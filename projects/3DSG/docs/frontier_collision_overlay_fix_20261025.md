# Frontier collision overlay correction — 2026-10-25

## Evidence from the latest user run

`native-live-m2-binary-spawn2-progress-check-20261024-08` ran for 602.22 s.
Its recorded path length is 20.113 m, net displacement 2.313 m, and it reports
42 viewpoint arrival episodes. It does not remain permanently stationary:
the longest active-frontier stationary episode is 18.765 s. That episode
contains 49 heading-alignment, 26 recovery, 37 tracking and 6 no-path ticks.
Several different goals retry zero-progress 20 mm steps from approximately
`(1.656194, 6.742326)`. Passing the 30 s continuity gate alone therefore does
not establish efficient exploration.

The observed-grid planner avoids a temporarily rejected edge, but the
controller previously checked its straight lookahead only against the
footprint mask. It could shortcut the A* detour through the rejected edge.
Also, translation follows the robot's current yaw, which can still point at
that edge while the chosen lookahead is safe and less than 50 degrees away.

`runs/frontier-recovery-diagnosis-20261025/saved_map_overlay_replay.json`
replans eight recorded rejected-step poses/targets on the **final** observed
map. Six old lookaheads cross the corresponding rejected edge. All eight
corrected lookaheads respect that edge. The final map is not the original
per-tick map; this replay establishes the contract violation, not an exact
reproduction of the whole original episode.

## Correction

- Runtime supplies the same temporary blocked-edge overlay to controller
  lookahead and to A* planning/revalidation.
- Supercover segment checks reject cardinal and diagonal edge crossings.
- When the actual pose is off-center, the controller can recenter inside its
  current free cell before following a detour whose first segment would
  otherwise cross the rejected edge.
- Before translating, the controller checks a short segment along actual yaw
  against both the footprint mask and the overlay. If blocked, it aligns in
  place even below the usual 50 degree threshold, with zero linear velocity.

Frontier extraction, shortest-path goal ranking, freshness deadlines,
footprint constraints and simulator collision filtering remain in use.
This change supplies no route, random goal, or ground-truth frontier.

## Separate observation

The source run also hits its configured 2 GiB keyframe persistence limit at
frame 1294 and subsequently records 1873 persistence failures. This affects
graph freshness and I/O cost; it is separate from the demonstrated local
controller/planner mismatch and is not corrected by this navigation patch.

## Live integration verification

`frontier-overlay-native-20261025-b` uses the source spawn/profile, P2 and
native ConceptGraphs + OccuSG with binary transport. It completed a 240 s
budget (244.93 s recorded including shutdown):

- path length 3.520 m; net displacement 2.077 m;
- 10 reached viewpoints, 5 separated by at least 0.5 m;
- 1 collision/filter block;
- movement, spatial and overall evaluator gates PASS;
- longest active-frontier stationary episode 15.632 s, continuity PASS;
- native graph connected/healthy and shutdown succeeded.

The longest wait contains 21 stale-map stops and 24 tracking ticks. CPU
rendering/mapping freshness still limits wall-clock speed. This is a bounded
integration check, not proof of global exploration optimality or a matched
before/after performance benchmark. The original 600 s run has a different
trajectory and process timing.

Evaluation artifacts are `native_movement.json` and `native_continuity.json`
in `runs/frontier-recovery-diagnosis-20261025/`.

## Recorded-corner physical check

`corner-overlay-final-20261025-c` starts at the actual recorded pose of frame
750 in the source run, including yaw and NavMesh height. Mapping and policy
memory start fresh; the saved-map replay above covers the existing map
separately. The scene configuration contains a spawn only, with no route.
P2/graph are disabled in this focused navigation check.

Over 120.18 s it traveled 4.016 m, ended 1.896 m from that corner, and reached
11 viewpoints (5 separated by at least 0.5 m). Four collision/filter blocks
were recovered. Movement, spatial, overall and continuity gates PASS; the
longest stationary active-frontier episode was 8.806 s. Reports:
`corner_movement.json` and `corner_continuity.json` in the diagnosis folder.

A final guard added after these live checks rejects a local segment when
actual forward motion is blocked and heading error is within the 6 degree
alignment deadband. This requests another plan rather than allowing a
narrow corridor to cause indefinite discrete-turn oscillation. That guard
is covered by deterministic regression tests; these two live runs predate
that final guard. All earlier core overlay/recentering changes are present
in both live runs.

## Fresh-planning follow-up

`corner-overlay-guard-20261025-d` (60.30 s) tests the final deadband guard.
It escaped 0.412 m, traveled 0.580 m and passed continuity, but **failed the
spatial evaluator** (only one viewpoint at 0.5 m separation). It is not a
successful exploration-quality run. Its last 17 s waited after an arrival:
worker source-map ages were about 1.0–1.9 s against the unchanged 1 s limit,
with worker durations around 0.4–1.0 s. The new small-angle rejection did not
occur in this trial; stale plan results caused the wait.

The reachable-component flood now uses optional SciPy binary propagation
for standard 4/8 neighborhoods and a true seed. For 8-connectivity with
corner cutting forbidden, 4-connectivity is equivalent: each permitted
diagonal has a free cardinal two-step alternative. Nonstandard/false-seed
cases and environments without SciPy retain the original BFS. Component
ordering, candidate ranking and goal scoring are unchanged.

On the failed short run's final map, three-repeat extraction medians were
122.67 ms (reference) / 60.96 ms (accelerated). Both produced 40 candidates,
866 footprint-reachable cells and goal `(1.325, 7.025)`. This is a saved-map
benchmark, not a live speed guarantee. Evidence: `flood_benchmark.json`.

Regression verification: **193 tests passed** with
`QT_QPA_PLATFORM=offscreen python3 -m pytest -q tests`, including 15 new
controller/overlay cases and 5 flood-equivalence cases. Python compilation
and `git diff --check` passed. Independent Sol review found no blocking
issue in the collision contract or reachable-set equivalence.

## Final source verification after acceleration

`frontier-corner-accelerated-20261025-e` includes **all** changes above,
including the small-angle guard and flood acceleration. From the same
recorded corner it completed 120.17 s:

- path length **3.260 m**, net displacement **1.562 m**;
- **9** reached viewpoints, **3** separated by at least 0.5 m;
- **5** collision/filter blocks, with subsequent recovery;
- movement, spatial, overall and continuity gates **PASS**;
- longest active-frontier stationary episode **12.255 s**.

Reports: `final_corner_movement.json`, `final_corner_continuity.json` and
`planner_freshness_comparison.json` in the diagnosis folder. This focused
run disables P2/graph. The earlier 244.93 s native integration check covers
the controller/overlay/recentering changes before flood acceleration; the
accelerated flood's equivalence is separately covered by tests and review.

The final check demonstrates escape and continued frontier navigation, not
complete scene coverage or optimal goal choice. The nearest-path-cost
frontier policy remains the baseline. Visibility-aware gain ranking and
world-coordinate storage for collision edges across rolling-map origin
changes remain possible follow-ups; this patch does not claim to solve them.

# T4 geometry route preparation — contract blocker

Date: 2026-10-20  
Status: **stopped before route search**. No route/config was created; no capture, renderer, perception, mapper, semantic replay, browser, or producer was run.

## Why route search stopped

The current clearance contract has a mismatch between its name/configuration and what the query measures:

- The 00802 navmesh loaded read-only through Habitat-Sim 0.3.3 reports `agent_radius=0.1000000015 m`, `cell_size=0.0500000007 m`, `agent_height=1.5 m`, and `include_static_objects=false`. Its SHA-256 is `ae5ddd8ca77480eea3c401059844ae1ccc42d9cce75770a25b0d79b02d077232`.
- Habitat-Sim 0.3.3 converts the serialized agent radius to Recast `walkableRadius=ceil(agentRadius/cellSize)` and erodes the walkable area by that radius during navmesh construction. With the saved settings this is a two-cell, nominal 0.1 m erosion. [Version-pinned PathFinder.cpp](https://github.com/facebookresearch/habitat-sim/blob/v0.3.3/src/esp/nav/PathFinder.cpp#L3194-L3199) [erosion step](https://github.com/facebookresearch/habitat-sim/blob/v0.3.3/src/esp/nav/PathFinder.cpp#L3349-L3358)
- `distance_to_closest_obstacle` reports distance to the closest non-navigable location / navmesh wall; it is not an independent query against the original scene mesh. Habitat's API documents that meaning and its max-radius saturation. [Version-pinned PathFinder.h](https://github.com/facebookresearch/habitat-sim/blob/v0.3.3/src/esp/nav/PathFinder.h#L2524-L2544) The implementation delegates to the navmesh distance-to-wall query. [Version-pinned PathFinder.cpp](https://github.com/facebookresearch/habitat-sim/blob/v0.3.3/src/esp/nav/PathFinder.cpp)
- Local config says `radius_m=0.1`, `clearance_margin_m=0.0`, and says the radius comes from the serialized navmesh. But `validate_navmesh_polyline` requires the reported distance from the already-eroded navmesh boundary to be at least `radius + margin`, i.e. another 0.1 m. [smooth_route.py](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/integration_v2/smooth_route.py:132) [timebase policy](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/configs/habitat/smooth_route_t1_paced_v5_timebase_a.json:2)

Therefore, the implemented 0.1 m check behaves as an **additional clearance margin from the radius-eroded navmesh boundary**, while the config presents it as the agent footprint requirement and sets the explicit additional margin to zero. It does not represent only the 0.1 m footprint requirement. This is the specific contract mismatch that prevents using the current gate to search and label a route as passing without a policy decision. I did not change the threshold or infer a replacement.

The helper correctly treats finite zero/epsilon as a real measured clearance and fails it; only a missing or non-finite result can use the serialized-footprint fallback. Its query radius is `radius + margin + 0.05 m`, so the API's capped return can still distinguish the current 0.1 m threshold from an unavailable query.

## Other geometry checks clarified

| Question | Current implementation evidence | Meaning / limit |
|---|---|---|
| Does the executor require one fixed Y for every waypoint? | `find_waypoint_polyline` resolves each consecutive waypoint pair and checks endpoint error; it does not require equal waypoint Y. The runner follows targets in XZ and checks navigability. The old continuation search itself projected candidates to the locked `route_slice_habitat_y_m=0.124`. | Fixed Y was a search/config choice for that route, not a general executor invariant. The route config field is not read by the runner. |
| What does `vertical_navmesh_tolerance_m=0.05` mean? | Runtime passes it as `PathFinder.is_navigable(..., max_y_delta=...)` after predicted and actual steps. Habitat documents `isNavigable` as snapping to the navmesh, requiring no XZ displacement and at most `maxYDelta` vertical displacement. [PathFinder.h](https://github.com/facebookresearch/habitat-sim/blob/v0.3.3/src/esp/nav/PathFinder.h#L2557-L2582) | A 5 cm vertical snap tolerance for movement checks; not a floor ID, layer scope, or proof of single-level geometry. The sampled preflight calls `is_navigable(point)` with the API default 0.5 m, although its points originate from the navmesh path and waypoint endpoint checks separately constrain 3D error. |
| What does `try_step_no_sliding` establish? | Preflight and runtime call PathFinder's navmesh movement filter. The runtime also reports physics disabled. | It checks the selected navmesh route and does not independently establish collision clearance against scene instances excluded from this navmesh. The loaded 00802 navmesh records `include_static_objects=false`; static-object collision assurance is unavailable from these checks. |

The route's constant-Y selection and the configured 5 cm runtime snap check must not be used as evidence that the scene is single-floor or that room/object spatial scope is verified.

## Search and handoff

Because the clearance contract mismatch is a direct gate, the dependent bounded route search was not started. Thus there is no preferred candidate, route drawing, route length/turn total, or new frame/keyframe/timeout guard to report. This does not show that HM3D 00802 has no suitable route.

The earlier T4 preflight result remains a record of what the then-current code accepted/rejected. Its `0.1 m` values should be interpreted as distance from the radius-eroded navmesh boundary, not as raw physical wall clearance. The old route/config and artifacts were not modified.

## Inputs and code fingerprints

| Input | SHA-256 |
|---|---|
| `integration_v2/smooth_route.py` | `1cc0d666630ff199fa5d0843645834cdbd68912610720c2c1561af06cfc0d590` |
| `scripts/run_habitat_smooth_route_preview.py` | `a706e338cb382c774a362757126327a8495635a173767554dd559f6a4075a778` |
| `configs/habitat/smooth_route_t1_paced_v5_timebase_a.json` | `74ddfd4399af8cbb84e3623492df2a90fd4f53c84fc5b8bef5e2223d7a580765` |
| `configs/habitat/hm3d_minival_v02_00802_t1_1_turn_continue_route.json` | `f76c124bc80b7a9a895666a7802b00abd48ef5ba20a2d692d6e02703abbd20a0` |
| HM3D 00802 navmesh | `ae5ddd8ca77480eea3c401059844ae1ccc42d9cce75770a25b0d79b02d077232` |

No implementation or configuration was changed and no route-search attempt was executed in this checkpoint. T4 graph/camera overlap remains pending; room scope and labels are unchanged.

## Decision needed before route preparation resumes

Clarify whether the current 0.1 m gate is intended to mean:

1. the agent-radius footprint already baked into the navmesh, in which case the separate clearance query must have a separately defined margin contract; or
2. an additional 0.1 m safety margin from the eroded navmesh boundary.

This checkpoint does not select between those interpretations or alter either value. Once the contract is resolved, a new bounded geometry-only route search can proceed without changing speed, navmesh, or route geometry based on semantic output.

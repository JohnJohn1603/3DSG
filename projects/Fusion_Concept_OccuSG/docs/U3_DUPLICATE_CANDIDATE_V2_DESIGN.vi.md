# U3 duplicate candidate v2 — locked design before paired evaluation

## Guard

The guard applies only to containment-based duplicate candidates: mask IoU is below the existing direct-IoU threshold while the existing smaller-mask containment condition passes. It uses the two cached binary masks, their pixel areas, and the existing cached P3/depth support and 3D gate evidence. It uses no class, GT identity, room label, or later observation.

The single new config value is `max_larger_to_smaller_mask_area_ratio = 1.5`. A larger/smaller mask ratio above 1.5 returns `ambiguous_keep_both_no_alias`; it does not claim the detections are different objects. This distinguishes only “containment with modest expansion, existing gates pass” from “expansion too large to suppress safely.” A mask that is more complete for the same object can still cross the cap and become ambiguous. If depth/selected-point support or usable 3D geometry is missing, the containment pair is also ambiguous. Existing direct-IoU behavior and all cross-frame thresholds remain unchanged.

## Preflight and cost

The locked v1 candidate-pair ledgers contain 1,065 pair rows for 00802; two eligible pairs pass via containment (frames 234 and 235). 00800 and 00808 have none. The already completed R23 cached reconstruction took about 1.5 seconds for five accepted detections in one frame. V2 itself adds no point-set reconstruction: it reads mask areas and cache support already loaded by the existing offline runner. Expected overhead is negligible and well below 30 minutes. No full-cache reclustering is planned.

Preflight shows the reviewed same-object containment pair at frame 234 has area ratio about 1.32; R23 is about 1.80. The 1.5 cap is frozen as one conservative ambiguity rule, without searching multiple thresholds. Paired evaluation must use this exact file hash; no changes are allowed after scoring begins.

Locked config: `configs/association/u3_same_frame_duplicate_candidate_v2.json`  
SHA-256: `257dd354c7b562fed4b8d2b6548fcf38fe67946aef2e95ae1c084d79044d3ec3`

## Limit

The cap cannot tell a complete mask of one large object from a mask that includes part of another object. It abstains on large expansion; downstream association can still join the two retained observations. “Keep both” is therefore not treated as a clean graph repair. V2 reports the actual downstream assignments and ambiguity separately.

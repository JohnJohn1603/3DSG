# U4 pre-review boundary (before class fusion scoring)

This review uses existing U3 posthoc crops/evaluator metadata only to identify uncertain examples. It does not modify the evaluator labels, assignments, or U3 thresholds. Physical identity remains **uncertain** where an evaluator instance or crop is insufficient to settle the object interpretation.

## Cases reviewed

- Evaluator instance 357 has crop labels `dining_table`, `sofa`, and `bench` across frames. Contact sheets show views of a table-like surface, but the annotation's shared evaluator ID is not treated as proof of a single physical object. Physical identity: **uncertain**.
- Evaluator instance 417 includes earlier `dining_table`/`sofa` crops and a frame-232 crop labeled `bed`; the frame-232 crop visibly contains a table and chairs. The evaluator category is `table`; the class mapping `table`→`dining_table` remains unconfirmed. Physical identity/category mapping: **uncertain**.
- The geometry-only false merge pair from frame 230 (`dining_table`, evaluator ID 357/category `table`) to frame 238 (`suitcase`, evaluator ID 351/category `printer`) is retained as a false merge under the evaluator identity protocol. The crop looks like a dark printer-like object on the tabletop; this review does not relabel either GT or the detector output. Physical identity: **uncertain**.
- The geometry+appearance false merge involving the frame-230 table track and frame-238 `suitcase` detection assigned evaluator ID 352/category `computer tower` is likewise retained as a false merge. Physical identity: **uncertain**.

The reviewed artifact set is `artifacts/audits/u3_causal_prefix/holdout_contact_cross_class_same_evaluator_instance_*.png`, `holdout_contact_likely_false_merge_posthoc_*.png`, `holdout_observations_posthoc.csv`, and `holdout_pair_review_posthoc.csv`. These examples were inspected before U4 result generation. They are descriptive review strata, not a development set for changing U3 or the U4 evidence rules.

The U4 rule lock is separate at `artifacts/audits/u4_semantic_fusion/u4_config.locked.json`; it uses one capped contribution per quantized view bin, two independent bins, a fixed score margin/fraction, and no GT/room input. Frames 231–238 were already seen during U3 and remain **seen data**, not a new untouched holdout for U4 tuning or promotion.

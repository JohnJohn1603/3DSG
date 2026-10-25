from pathlib import Path

from standalone_sg.config import load_config


ROOT = Path(__file__).resolve().parents[1]


def test_replay_effective_association_values_are_from_pipeline_profile():
    phase = load_config(ROOT / "configs/reference_code_mp3d.yaml")
    assert phase.object_association.same_class_radius_m == 0.75
    assert phase.object_association.same_pose_suppression_cap_m == 0.15


def test_perception_profile_association_metadata_is_not_runtime_source():
    import json

    profile = json.loads((ROOT / "configs/perception/yolo11n_seg_replica_cad_dev.json").read_text())
    assert profile["association"]["same_class_radius_m"] == 0.50
    assert profile["association"]["same_pose_suppression_cap_m"] == 0.10
    # The P4 runner must use the pipeline profile above; this assertion keeps
    # the historical metadata visible without silently treating it as runtime.
    assert profile["association"]["policy"] == "reference/code M6"

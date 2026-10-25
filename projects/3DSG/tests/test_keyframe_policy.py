from exploration.contracts import RobotPose2
from exploration.keyframe_policy import KeyframePolicy


def test_policy_state_advances_only_after_admission_ack():
    policy = KeyframePolicy(min_translation_m=0.25)
    start = RobotPose2(0.0, 0.0, 0.0)
    moved = RobotPose2(0.3, 0.0, 0.0)

    assert policy.propose(start, None, {"f0"}) == (True, "first_view", 1.0)
    # No commit models a persisted keyframe rejected by backpressure.
    assert policy.propose(start, None, {"f0"}) == (True, "first_view", 1.0)
    policy.commit(start, {"f0"})

    candidate = policy.propose(moved, None, {"f0", "f1"})
    assert candidate[0] and candidate[1] == "novel_view"
    # A rejected candidate leaves the admitted pose/frontier baseline intact.
    repeated = policy.propose(moved, None, {"f0", "f1"})
    assert repeated[0] and repeated[1] == "novel_view"
    policy.commit(moved, {"f0", "f1"})
    assert policy.propose(moved, None, {"f0", "f1"}) == (False, None, 0.0)

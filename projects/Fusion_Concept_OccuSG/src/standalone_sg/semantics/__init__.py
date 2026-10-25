"""Explainable object-supported semantics over regions and M8 rooms."""

from .adapters import evidence_from_m5_regions, evidence_from_m8_snapshot
from .evidence import RegionEvidence, RegionObjectEvidence
from .room_labeler import RoomSemanticState, RuleBasedRoomLabeler
from .room_belief_v1 import (
    ConsensusPolicy,
    ObjectSemanticConsensus,
    RoomBeliefEvent,
    RoomBeliefLedger,
    RoomBeliefPolicy,
    RoomBeliefState,
    RoomObjectEvidence,
    SemanticObservation,
    VoxelMembership,
    aggregate_object_semantics,
    greedy_voxel_support_membership,
)

__all__ = [
    "RegionEvidence", "RegionObjectEvidence", "RoomSemanticState", "RuleBasedRoomLabeler",
    "evidence_from_m5_regions", "evidence_from_m8_snapshot",
    "ConsensusPolicy", "ObjectSemanticConsensus", "SemanticObservation",
    "aggregate_object_semantics", "VoxelMembership", "greedy_voxel_support_membership",
    "RoomObjectEvidence", "RoomBeliefEvent", "RoomBeliefPolicy", "RoomBeliefState", "RoomBeliefLedger",
]

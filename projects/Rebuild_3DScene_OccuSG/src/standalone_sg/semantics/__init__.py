"""Explainable object-supported semantics over regions and M8 rooms."""

from .adapters import evidence_from_m5_regions, evidence_from_m8_snapshot
from .evidence import RegionEvidence, RegionObjectEvidence
from .room_labeler import RoomSemanticState, RuleBasedRoomLabeler

__all__ = [
    "RegionEvidence", "RegionObjectEvidence", "RoomSemanticState", "RuleBasedRoomLabeler",
    "evidence_from_m5_regions", "evidence_from_m8_snapshot",
]

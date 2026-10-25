"""Deterministic single-writer frame ordering for SceneGraphBuilder."""
from __future__ import annotations
from dataclasses import dataclass
from ..contracts.errors import ContractError
from .contracts import FramePacket

@dataclass(frozen=True,slots=True)
class SchedulerCursor:
    scene_id:str|None=None
    sequence_id:str|None=None
    frame_index:int=-1
    timestamp_s:float|None=None
    frame_ids:frozenset[str]=frozenset()

class SequenceScheduler:
    def __init__(self): self._cursor=SchedulerCursor()
    @property
    def cursor(self): return self._cursor
    def validate(self,packet:FramePacket):
        c=self._cursor
        if c.scene_id is not None and (packet.scene_id!=c.scene_id or packet.sequence_id!=c.sequence_id):
            raise ContractError("SceneGraphBuilder accepts one scene/sequence per instance")
        if packet.frame_id in c.frame_ids: raise ContractError("duplicate frame_id in sequence")
        if packet.frame_index<=c.frame_index: raise ContractError("frame_index must increase strictly")
        if c.timestamp_s is not None and packet.timestamp_s<c.timestamp_s: raise ContractError("packet timestamp must not decrease")
    def candidate(self,packet:FramePacket)->SchedulerCursor:
        """Prepare the next cursor without mutating scheduler state."""
        self.validate(packet)
        c=self._cursor
        return SchedulerCursor(packet.scene_id,packet.sequence_id,packet.frame_index,
            packet.timestamp_s,c.frame_ids|{packet.frame_id})
    def commit_candidate(self,cursor:SchedulerCursor):
        if not isinstance(cursor,SchedulerCursor): raise ContractError("scheduler candidate has wrong type")
        self._cursor=cursor
    def commit(self,packet:FramePacket):
        self.commit_candidate(self.candidate(packet))
    def validate_flush(self,timestamp_s):
        c=self._cursor
        if timestamp_s is not None and c.timestamp_s is not None and timestamp_s<c.timestamp_s:
            raise ContractError("flush timestamp must not precede last packet")

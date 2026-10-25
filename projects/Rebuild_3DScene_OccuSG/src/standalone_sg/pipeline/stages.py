"""Optional injected stage adapters; no ROS/simulator imports."""
from __future__ import annotations
import time
from typing import Protocol
from ..mapping.projection import FreeSpaceGrid2D
from ..regions.dude_client import DuDeWorkerClient,DuDeUpdateResult
from ..regions.pipeline import RegionUpdateProcessor

class RegionStage(Protocol):
    def update(self,grid:FreeSpaceGrid2D,*,timestamp_s:float)->DuDeUpdateResult: ...

class MappingStage(Protocol):
    def build_grid(self,packet)->FreeSpaceGrid2D: ...

class DudeM5RegionStage:
    """Bind the pinned local DuDe worker to its independent M5 guard/tracker."""
    def __init__(self,worker:DuDeWorkerClient,processor:RegionUpdateProcessor, *, capture_preprocessed_mask: bool = False):
        if type(capture_preprocessed_mask) is not bool:
            raise TypeError("capture_preprocessed_mask must be a boolean")
        self.worker=worker; self.processor=processor; self.capture_preprocessed_mask=capture_preprocessed_mask
        self.last_response=None; self.last_result=None
        self.last_timing={"m4_m5_runtime_ms": 0.0}
    def update(self,grid:FreeSpaceGrid2D,*,timestamp_s:float)->DuDeUpdateResult:
        started=time.perf_counter()
        _response,result=self.worker.update_regions(
            self.processor, grid, timestamp_s=timestamp_s,
            capture_preprocessed_mask=self.capture_preprocessed_mask,
        )
        self.last_timing={**getattr(self.worker, "last_timing", {}),
                          "m4_m5_runtime_ms": (time.perf_counter()-started)*1000.0}
        self.last_response=_response; self.last_result=result
        return result
    def close(self): self.worker.close()

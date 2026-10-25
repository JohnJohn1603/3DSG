"""Provider boundary; detector implementations stay outside the core."""

from __future__ import annotations

from typing import Protocol

from ...contracts.objects import DetectionBatch


class DetectionProvider(Protocol):
    """Return a batch for the requested frame; absent and empty are distinct."""

    @property
    def provider_id(self) -> str: ...

    def get_detections(self, frame_id: str, timestamp_s: float) -> DetectionBatch: ...

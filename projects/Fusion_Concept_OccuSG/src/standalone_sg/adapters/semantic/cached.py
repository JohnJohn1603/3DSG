"""Deterministic replay provider for cached/synthetic DetectionBatch values."""

from __future__ import annotations

import math
from numbers import Real
from typing import Mapping

from ...contracts.errors import ContractError
from ...contracts.objects import DetectionBatch


class CachedDetectionProvider:
    """Immutable frame-keyed provider; cache misses emit explicit absent batches."""

    def __init__(self, batches: Mapping[str, DetectionBatch], *, provider_id: str = "cached") -> None:
        if type(provider_id) is not str or not provider_id.strip():
            raise ContractError("provider_id must be a non-empty string")
        self._provider_id = provider_id.strip()
        self._batches = dict(batches)
        if any(type(key) is not str or not key.strip() for key in self._batches):
            raise ContractError("cached frame keys must be non-empty strings")
        for key, batch in self._batches.items():
            if not isinstance(batch, DetectionBatch) or batch.frame_id != key:
                raise ContractError("each cached batch frame_id must match its cache key")
            if batch.provider_id != self._provider_id:
                raise ContractError("cached batch provider_id must match provider_id")

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def get_detections(self, frame_id: str, timestamp_s: float) -> DetectionBatch:
        if type(frame_id) is not str or not frame_id.strip():
            raise ContractError("frame_id must be a non-empty string")
        if isinstance(timestamp_s, bool) or not isinstance(timestamp_s, Real) or not math.isfinite(float(timestamp_s)):
            raise ContractError("timestamp_s must be finite")
        key = frame_id.strip()
        batch = self._batches.get(key)
        if batch is None:
            return DetectionBatch(self._provider_id, key, float(timestamp_s), None,
                                  {"provider_kind": "cache", "cache_hit": False})
        if not math.isclose(batch.timestamp_s, float(timestamp_s), rel_tol=0.0, abs_tol=1e-9):
            raise ContractError("requested timestamp does not match cached frame timestamp")
        return batch

"""Semantic detection provider interfaces and cache adapters."""

from .base import DetectionProvider
from .cached import CachedDetectionProvider

__all__ = ["DetectionProvider", "CachedDetectionProvider"]

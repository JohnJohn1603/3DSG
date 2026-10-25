"""Reference/code BasicRegionTracker port for synthetic or supplied observations."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

from ..config.schema import RegionTrackerConfig
from ..contracts.errors import ContractError
from ..contracts.regions import CanonicalRegion, RegionObservation, TrackedRegion


@dataclass(slots=True)
class _Track:
    persistent_id: int
    observation: RegionObservation
    area_m2: float
    bounds: tuple[float, float, float, float]
    first_seen_s: float
    miss_count: int = 0


@dataclass(frozen=True, slots=True)
class TrackerStep:
    current_regions: tuple[CanonicalRegion, ...]
    retained_tracks: tuple[TrackedRegion, ...]
    matched_ids: tuple[int, ...]
    new_ids: tuple[int, ...]
    expired_ids: tuple[int, ...]


class RegionTracker:
    """Greedy one-to-one matcher with source bbox-IoU / centroid / area gates."""

    def __init__(self, config: RegionTrackerConfig | None = None) -> None:
        self.config = config or RegionTrackerConfig()
        self.reset()

    def reset(self) -> None:
        self._tracks: list[_Track] = []
        self._next_id = 1
        self._accepted_update_index = 0

    @property
    def accepted_update_index(self) -> int:
        return self._accepted_update_index

    def snapshot(self) -> tuple[TrackedRegion, ...]:
        return tuple(self._view(track) for track in self._tracks)

    def update(self, observations: Sequence[RegionObservation]) -> TrackerStep:
        raw_ids = [observation.raw_id for observation in observations]
        if len(set(raw_ids)) != len(raw_ids):
            raise ContractError("raw region IDs must be unique within a snapshot")

        self._accepted_update_index += 1
        features = [self._features(observation) for observation in observations]
        # The C++ source sorts by: score desc, IoU desc, distance asc,
        # canonical ID asc, then input vector index asc. This is a total order.
        candidates: list[tuple[float, float, float, int, int, int]] = []
        for track_index, track in enumerate(self._tracks):
            if track.area_m2 <= 0.0:
                continue
            for current_index, feature in enumerate(features):
                area, bounds, centroid = feature
                if area <= 0.0:
                    continue
                area_ratio = area / track.area_m2
                if not (self.config.min_area_ratio <= area_ratio <= self.config.max_area_ratio):
                    continue
                iou = self._bbox_iou(track.bounds, bounds)
                distance = math.hypot(
                    track.observation.centroid_xy[0] - centroid[0],
                    track.observation.centroid_xy[1] - centroid[1],
                )
                if not (iou >= self.config.min_bbox_iou or distance <= self.config.max_centroid_distance_m):
                    continue
                centroid_score = (
                    max(0.0, 1.0 - distance / self.config.max_centroid_distance_m)
                    if self.config.max_centroid_distance_m > 0.0
                    else 0.0
                )
                area_score = min(area_ratio, 1.0 / area_ratio)
                score = 2.0 * iou + centroid_score + 0.25 * area_score
                candidates.append(
                    (-score, -iou, distance, track.persistent_id, current_index, track_index)
                )
        candidates.sort()

        track_matched = [False] * len(self._tracks)
        current_matched = [False] * len(observations)
        canonical_ids: list[int | None] = [None] * len(observations)
        matched_ids: list[int] = []
        for _, _, _, canonical_id, current_index, track_index in candidates:
            if track_matched[track_index] or current_matched[current_index]:
                continue
            track = self._tracks[track_index]
            area, bounds, _ = features[current_index]
            track.observation = observations[current_index]
            track.area_m2 = area
            track.bounds = bounds
            track.miss_count = 0
            canonical_ids[current_index] = canonical_id
            track_matched[track_index] = True
            current_matched[current_index] = True
            matched_ids.append(canonical_id)

        for index, matched in enumerate(track_matched):
            if not matched:
                self._tracks[index].miss_count += 1

        new_ids: list[int] = []
        for current_index, matched in enumerate(current_matched):
            if matched:
                continue
            canonical_id = self._next_id
            self._next_id += 1
            canonical_ids[current_index] = canonical_id
            area, bounds, _ = features[current_index]
            # Faithful to source: malformed/zero-area features receive an output
            # ID but do not become retained tracks.
            if area > 0.0:
                self._tracks.append(
                    _Track(
                        canonical_id, observations[current_index], area, bounds,
                        observations[current_index].timestamp_s,
                    )
                )
            new_ids.append(canonical_id)

        expired: list[int] = []
        kept: list[_Track] = []
        for track in self._tracks:
            if track.miss_count > self.config.max_missed_updates:
                expired.append(track.persistent_id)
            else:
                kept.append(track)
        self._tracks = kept

        raw_to_canonical = {
            observation.raw_id: canonical_ids[index]
            for index, observation in enumerate(observations)
        }
        current: list[CanonicalRegion] = []
        for index, observation in enumerate(observations):
            canonical_id = canonical_ids[index]
            assert canonical_id is not None
            adjacency = tuple(
                sorted(
                    {
                        int(raw_to_canonical[raw_id])
                        for raw_id in observation.adjacent_raw_ids
                        if raw_id in raw_to_canonical and raw_to_canonical[raw_id] is not None
                    }
                )
            )
            current.append(
                CanonicalRegion(
                    persistent_id=canonical_id,
                    raw_id=observation.raw_id,
                    polygon=observation.polygon,
                    centroid_xy=observation.centroid_xy,
                    area_m2=features[index][0],
                    timestamp_s=observation.timestamp_s,
                    source_grid_revision=observation.source_grid_revision,
                    adjacent_region_ids=adjacency,
                    metadata=observation.metadata,
                )
            )
        return TrackerStep(
            current_regions=tuple(current),
            retained_tracks=self.snapshot(),
            matched_ids=tuple(matched_ids),
            new_ids=tuple(new_ids),
            expired_ids=tuple(expired),
        )

    @staticmethod
    def _features(
        observation: RegionObservation,
    ) -> tuple[float, tuple[float, float, float, float], tuple[float, float]]:
        return (
            observation.reference_area_m2,
            observation.reference_bounds,
            observation.centroid_xy,
        )

    @staticmethod
    def _bbox_iou(
        lhs: tuple[float, float, float, float],
        rhs: tuple[float, float, float, float],
    ) -> float:
        ix = max(0.0, min(lhs[2], rhs[2]) - max(lhs[0], rhs[0]))
        iy = max(0.0, min(lhs[3], rhs[3]) - max(lhs[1], rhs[1]))
        intersection = ix * iy
        lhs_area = max(0.0, lhs[2] - lhs[0]) * max(0.0, lhs[3] - lhs[1])
        rhs_area = max(0.0, rhs[2] - rhs[0]) * max(0.0, rhs[3] - rhs[1])
        union = lhs_area + rhs_area - intersection
        return intersection / union if union > 0.0 else 0.0

    @staticmethod
    def _view(track: _Track) -> TrackedRegion:
        observation = track.observation
        return TrackedRegion(
            persistent_id=track.persistent_id,
            polygon=observation.polygon,
            centroid_xy=observation.centroid_xy,
            area_m2=track.area_m2,
            first_seen_s=track.first_seen_s,
            last_seen_s=observation.timestamp_s,
            miss_count=track.miss_count,
            status="temporarily_missing" if track.miss_count else "current",
            last_raw_id=observation.raw_id,
            last_source_grid_revision=observation.source_grid_revision,
        )

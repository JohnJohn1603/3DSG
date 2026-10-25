"""Guard-before-tracker update order, isolated from any segmentation backend."""

from __future__ import annotations

from typing import Sequence

from ..config.schema import RegionGuardConfig, RegionTrackerConfig
from ..contracts.regions import CanonicalRegion, RegionObservation, RegionUpdateResult
from .guard import RegionUpdateGuard
from .tracker import RegionTracker


class RegionUpdateProcessor:
    """Applies guard first; rejected snapshots never age or refresh tracker IDs."""

    def __init__(
        self,
        tracker_config: RegionTrackerConfig | None = None,
        guard_config: RegionGuardConfig | None = None,
    ) -> None:
        self.tracker_config = tracker_config or RegionTrackerConfig()
        self.guard = RegionUpdateGuard(guard_config)
        self.tracker = RegionTracker(self.tracker_config)

    def update(
        self,
        observations: Sequence[RegionObservation],
        *,
        free_cells: int,
        unknown_cells: int,
        expected_cells: int,
    ) -> RegionUpdateResult:
        decision = self.guard.evaluate(
            observations,
            free_cells=free_cells,
            unknown_cells=unknown_cells,
            expected_cells=expected_cells,
        )
        if not decision.accepted:
            stale_regions = self.guard.last_published_regions if decision.stale else ()
            return RegionUpdateResult(
                accepted=False,
                reason=decision.reason,
                stale=decision.stale,
                current_regions=stale_regions,
                retained_tracks=self.tracker.snapshot(),
                guard=decision,
            )

        if self.tracker_config.enabled:
            step = self.tracker.update(observations)
            current_regions = step.current_regions
            tracks = step.retained_tracks
            matched_ids = step.matched_ids
            new_ids = step.new_ids
            expired_ids = step.expired_ids
        else:
            current_regions = tuple(
                CanonicalRegion(
                    persistent_id=None,
                    raw_id=observation.raw_id,
                    polygon=observation.polygon,
                    centroid_xy=observation.centroid_xy,
                    area_m2=observation.reference_area_m2,
                    timestamp_s=observation.timestamp_s,
                    source_grid_revision=observation.source_grid_revision,
                    adjacent_region_ids=observation.adjacent_raw_ids,
                    metadata=observation.metadata,
                )
                for observation in observations
            )
            tracks = self.tracker.snapshot()
            matched_ids = new_ids = expired_ids = ()

        self.guard.commit(decision, current_regions)
        return RegionUpdateResult(
            accepted=True,
            reason=None,
            stale=False,
            current_regions=current_regions,
            retained_tracks=tracks,
            matched_ids=matched_ids,
            new_ids=new_ids,
            expired_ids=expired_ids,
            guard=decision,
        )

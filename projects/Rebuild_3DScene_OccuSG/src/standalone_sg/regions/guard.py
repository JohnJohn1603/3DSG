"""Python-side port of OccuSG's sanity gate; no DuDe rollback is implied."""

from __future__ import annotations

from typing import Sequence

from ..config.schema import RegionGuardConfig
from ..contracts.errors import ContractError
from ..contracts.regions import CanonicalRegion, GuardDecision, RegionObservation


class RegionUpdateGuard:
    def __init__(self, config: RegionGuardConfig | None = None) -> None:
        self.config = config or RegionGuardConfig()
        self._has_valid_update = False
        self._last_region_count = 0
        self._last_region_area_m2 = 0.0
        self._last_published_regions: tuple[CanonicalRegion, ...] = ()

    @property
    def has_valid_update(self) -> bool:
        return self._has_valid_update

    @property
    def last_published_regions(self) -> tuple[CanonicalRegion, ...]:
        return self._last_published_regions

    def reset(self) -> None:
        self._has_valid_update = False
        self._last_region_count = 0
        self._last_region_area_m2 = 0.0
        self._last_published_regions = ()

    def evaluate(
        self,
        observations: Sequence[RegionObservation],
        *,
        free_cells: int,
        unknown_cells: int,
        expected_cells: int,
    ) -> GuardDecision:
        for name, value in (
            ("free_cells", free_cells),
            ("unknown_cells", unknown_cells),
            ("expected_cells", expected_cells),
        ):
            if type(value) is not int or value < 0:
                raise ContractError(f"{name} must be a non-negative integer")
        if free_cells > expected_cells or unknown_cells > expected_cells:
            raise ContractError("free_cells and unknown_cells cannot exceed expected_cells")

        region_count = len(observations)
        # validate_region_update sums only positive finite Region2D.area fields;
        # the tracker has a separate polygon-area fallback when area <= 0.
        total_area = sum(
            observation.area_m2
            for observation in observations
            if observation.area_m2 is not None and observation.area_m2 > 0.0
        )
        unknown_ratio = unknown_cells / expected_cells if expected_cells else 0.0
        free_ratio = free_cells / expected_cells if expected_cells else 0.0
        min_regions = max(0, self.config.min_valid_region_count)

        # Source behavior: before the first accepted update, only min count is
        # checked. Unknown ratio and drop gates begin after state is established.
        if not self._has_valid_update:
            reasons = (
                ("initial_update_below_min_region_count",)
                if region_count < min_regions
                else ()
            )
        else:
            found: list[str] = []
            if region_count == 0 and self._last_region_count > 0:
                found.append("zero_regions_after_valid_update")
            elif region_count < min_regions:
                found.append("below_min_valid_region_count")

            if unknown_ratio > self.config.max_unknown_ratio_for_update:
                found.append("unknown_ratio_too_high")

            if self._last_region_area_m2 > 0.0:
                area_ratio = total_area / self._last_region_area_m2
                min_area_ratio = max(0.0, 1.0 - max(0.0, self.config.max_region_area_drop_ratio))
                if area_ratio < min_area_ratio:
                    found.append("region_area_drop_too_large")

            if self._last_region_count > 0:
                count_ratio = region_count / self._last_region_count
                min_count_ratio = max(0.0, 1.0 - max(0.0, self.config.max_region_count_drop_ratio))
                if count_ratio < min_count_ratio:
                    found.append("region_count_drop_too_large")
            reasons = tuple(found)

        accepted = not reasons
        stale = (
            not accepted
            and self._has_valid_update
            and self.config.preserve_previous_on_invalid_update
            and self.config.publish_stale_regions_on_reject
        )
        return GuardDecision(
            accepted=accepted,
            reason=";".join(reasons) if reasons else None,
            reasons=tuple(reasons),
            region_count=region_count,
            total_region_area_m2=total_area,
            unknown_ratio=unknown_ratio,
            free_ratio=free_ratio,
            stale=stale,
        )

    def commit(
        self,
        decision: GuardDecision,
        published_regions: Sequence[CanonicalRegion],
    ) -> None:
        """Remember only a validated and successfully canonicalized update."""
        if not decision.accepted:
            raise ContractError("cannot commit a rejected region update")
        self._has_valid_update = True
        self._last_region_count = decision.region_count
        self._last_region_area_m2 = decision.total_region_area_m2
        self._last_published_regions = tuple(published_regions)

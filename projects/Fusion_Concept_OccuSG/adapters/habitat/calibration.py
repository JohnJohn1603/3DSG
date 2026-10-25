"""Dependency-light, version-stable Habitat pinhole calibration kernel."""
from __future__ import annotations

import math


def habitat_intrinsics_values(width: int, height: int, hfov_deg: float) -> dict[str, float]:
    """Return the exact numeric K used by the Habitat frame adapter.

    Kept free of standalone graph contracts so Habitat's Python 3.9 capture
    process can record calibration without importing unrelated Python 3.10
    dataclass contracts.
    """
    if type(width) is not int or type(height) is not int or width < 2 or height < 2:
        raise ValueError("Habitat sensor resolution must be integer dimensions >= 2")
    hfov = math.radians(float(hfov_deg))
    if not math.isfinite(hfov) or not 0.0 < hfov < math.pi:
        raise ValueError("Habitat horizontal FOV must be finite and in (0, 180) degrees")
    fx = width / (2.0 * math.tan(hfov / 2.0))
    fy = height / (2.0 * math.tan(math.atan(math.tan(hfov / 2.0) * height / width)))
    return {"fx": fx, "fy": fy, "cx": (width - 1) / 2.0, "cy": (height - 1) / 2.0}

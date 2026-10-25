"""Deterministic local FreeSpaceGrid2D -> pinned DuDe -> M5 visualization."""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as PolygonPatch
import numpy as np

from standalone_sg.mapping.projection import FreeSpaceGrid2D
from standalone_sg.regions.dude_client import DuDeWorkerClient
from standalone_sg.regions.pipeline import RegionUpdateProcessor


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/m4_local_demo"
BIN = ROOT / "build/native-m4-dude/occusg_dude_worker"
W = H = 160


def build_sequence() -> list[tuple[str, np.ndarray]]:
    t0 = np.full((H, W), -1, dtype=np.int8)
    t1 = t0.copy(); t1[25:135, 18:85] = 0
    t2 = t0.copy(); t2[18:142, 12:98] = 0
    t3 = t2.copy(); t3[18:142, 110:150] = 0; t3[70:90, 95:116] = 0
    return [("t0_unknown", t0), ("t1_partial_free", t1),
            ("t2_room_expanded", t2), ("t3_doorway_revealed", t3),
            ("t4_recovery", t1.copy())]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    processor = RegionUpdateProcessor()
    summary = {
        "demo_kind": "synthetic deterministic segmentation/tracking; not robot trajectory",
        "source_commit": "2bca2fa06af87fd9dd0542957039be8f580f0dca",
        "protocol_version": 1, "resolution_m": 0.05, "origin_xy": [-4.0, -2.0],
        "config": {"decomp_threshold_m": 1.5, "safety_distance_m": 0.5},
        "updates": [],
    }
    with DuDeWorkerClient(BIN) as worker:
        for revision, (name, values) in enumerate(build_sequence(), 1):
            np.save(OUT / f"{name}.npy", values)
            z = np.full((H, W), np.nan)
            grid = FreeSpaceGrid2D(values, 0.05, (-4.0, -2.0), z, z, revision)
            response, result = worker.update_regions(
                processor, grid, timestamp_s=float(revision), capture_preprocessed_mask=True,
            )
            mask = response["diagnostics"].get("preprocessed_mask")
            if mask is not None:
                np.save(OUT / f"{name}_preprocessed_mask.npy", np.asarray(mask, dtype=np.bool_))
            update = {
                "name": name, "request_revision": revision,
                "accepted_observation_revision": result.accepted_observation_revision,
                "stale_snapshot": result.stale, "branch": response["branch"],
                "algorithm_status": response["status"],
                "m5_observation_policy": response["m5_observation_policy"],
                "kernel_invoked": response["diagnostics"]["kernel_invoked"],
                "preprocessed_free_pixels": response["diagnostics"]["preprocessed_free_pixels"],
                "raw_regions": response["regions"], "guard_accepted": result.accepted,
                "guard_reason": result.reason,
                "current_regions": [
                    {"persistent_id": r.persistent_id, "raw_id": r.raw_id,
                     "outer": r.polygon.outer, "adjacent_region_ids": r.adjacent_region_ids,
                     "area_m2": r.area_m2}
                    for r in result.current_regions
                ],
                "retained_tracks": [
                    {"persistent_id": t.persistent_id, "status": t.status,
                     "miss_count": t.miss_count, "last_seen_s": t.last_seen_s}
                    for t in result.retained_tracks
                ],
            }
            summary["updates"].append(update)
            fig, ax = plt.subplots(figsize=(8, 7), dpi=120)
            display = np.ma.masked_where(values < 0, values)
            ax.imshow(display, origin="lower", cmap="Greys", vmin=0, vmax=100,
                      extent=(-4, -4 + W*.05, -2, -2 + H*.05), interpolation="nearest")
            ax.imshow(np.where(values < 0, 0.25, np.nan), origin="lower", cmap="gray",
                      vmin=0, vmax=1, alpha=0.22,
                      extent=(-4, -4 + W*.05, -2, -2 + H*.05), interpolation="nearest")
            for region in result.current_regions:
                ring = np.asarray(region.polygon.outer)
                ax.add_patch(PolygonPatch(ring, closed=True, fill=False, linewidth=2,
                                          edgecolor="tab:orange"))
                ax.text(*region.centroid_xy, f"ID {region.persistent_id}", color="navy",
                        fontsize=9, ha="center", va="center",
                        bbox={"facecolor":"white", "alpha":0.8, "edgecolor":"none"})
            ax.set_title(f"{name}: {response['branch']} / M5 accepted={result.accepted}")
            ax.set_xlabel("world X (m)"); ax.set_ylabel("world Y (m)"); ax.set_aspect("equal")
            fig.tight_layout(); fig.savefig(OUT / f"{name}.png"); plt.close(fig)
    # Separate successful geometry-change sequence: a hidden second room is
    # revealed behind a narrow doorway. This has its own live DuDe/M5 state.
    geometry_dir = OUT / "doorway_geometry"
    geometry_dir.mkdir(parents=True, exist_ok=True)
    hidden = np.full((180, 180), -1, dtype=np.int8); hidden[40:140, 15:90] = 0
    revealed = hidden.copy(); revealed[50:130, 110:175] = 0; revealed[85:95, 90:111] = 0
    geometry_summary = {"sequence_kind": "synthetic hidden-room reveal through doorway",
                        "resolution_m": 0.05, "origin_xy": [-4.0, -2.0], "updates": []}
    geometry_processor = RegionUpdateProcessor()
    with DuDeWorkerClient(BIN) as worker:
        for revision, (name, values) in enumerate((("hidden_room", hidden),
                                                    ("doorway_revealed", revealed)), 1):
            z = np.full(values.shape, np.nan)
            grid = FreeSpaceGrid2D(values, 0.05, (-4.0, -2.0), z, z, revision)
            np.save(geometry_dir / f"{name}.npy", values)
            response, result = worker.update_regions(
                geometry_processor, grid, timestamp_s=float(revision), capture_preprocessed_mask=True,
            )
            np.save(geometry_dir / f"{name}_preprocessed_mask.npy",
                    np.asarray(response["diagnostics"]["preprocessed_mask"], dtype=np.bool_))
            geometry_summary["updates"].append({
                "name": name, "request_revision": revision,
                "accepted_observation_revision": result.accepted_observation_revision,
                "branch": response["branch"], "algorithm_status": response["status"],
                "guard_accepted": result.accepted,
                "region_count": len(result.current_regions),
                "regions": [{"persistent_id": r.persistent_id, "raw_id": r.raw_id,
                             "outer": r.polygon.outer,
                             "adjacent_region_ids": r.adjacent_region_ids}
                            for r in result.current_regions],
                "retained_tracks": [{"persistent_id": t.persistent_id,
                                     "last_seen_s": t.last_seen_s,
                                     "miss_count": t.miss_count}
                                    for t in result.retained_tracks],
            })
    (geometry_dir / "summary.json").write_text(
        json.dumps(geometry_summary, indent=2, allow_nan=False) + "\n")
    summary["doorway_geometry_sequence"] = geometry_summary
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    print(OUT)


if __name__ == "__main__":
    main()

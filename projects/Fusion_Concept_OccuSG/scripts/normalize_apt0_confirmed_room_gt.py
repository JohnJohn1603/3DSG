#!/usr/bin/env python3
"""Create a separate topology-normalized copy of the user-confirmed apt_0 GT.

The original Download and raw version are never edited. GEOS performs exact
polygon intersection/difference; lower numeric room ID owns any positive-area
overlap, as explicitly authorized by the user and the improvement plan.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPolygon

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "evaluation/gt/apt_0/versions/apt0_room_gt_user_confirmed_20261004_v1/raw_submission/room_polygons.geojson"
OUT = ROOT / "evaluation/gt/apt_0/versions/apt0_room_gt_user_confirmed_20261004_normalized_v1"
EXPECTED_SHA256 = "078b6c8370403f8d2d9e77ee2db21df88407ce905447fa6b9f4b7de0eea1ee31"
LATTICE_M = 0.05
UNION_AREA_TOLERANCE_M2 = 1e-9


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class Geos:
    def __init__(self):
        library = ctypes.util.find_library("geos_c")
        if not library:
            raise RuntimeError("GEOS C library is required for exact polygon difference")
        self.lib = ctypes.CDLL(library)
        L = self.lib
        L.GEOS_init_r.restype = ctypes.c_void_p
        self.ctx = L.GEOS_init_r()
        signatures = {
            "GEOSWKTReader_create_r": ([ctypes.c_void_p], ctypes.c_void_p),
            "GEOSWKTReader_read_r": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p], ctypes.c_void_p),
            "GEOSWKTWriter_create_r": ([ctypes.c_void_p], ctypes.c_void_p),
            "GEOSWKTWriter_write_r": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p),
            "GEOSIntersection_r": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p),
            "GEOSDifference_r": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p),
            "GEOSUnion_r": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p),
            "GEOSSymDifference_r": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p),
            "GEOSArea_r": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_double)], ctypes.c_int),
            "GEOSisValid_r": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_byte),
            "GEOSisEmpty_r": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_byte),
            "GEOSGeomTypeId_r": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
            "GEOSGetNumGeometries_r": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
            "GEOSGetGeometryN_r": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int], ctypes.c_void_p),
            "GEOSGetExteriorRing_r": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p),
            "GEOSGetNumInteriorRings_r": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
            "GEOSGetInteriorRingN_r": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int], ctypes.c_void_p),
            "GEOSGeom_getCoordSeq_r": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p),
            "GEOSCoordSeq_getSize_r": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)], ctypes.c_int),
            "GEOSCoordSeq_getX_r": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_double)], ctypes.c_int),
            "GEOSCoordSeq_getY_r": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_double)], ctypes.c_int),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(L, name); fn.argtypes = args; fn.restype = result
        L.GEOSGeom_destroy_r.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        L.GEOS_finish_r.argtypes = [ctypes.c_void_p]
        L.GEOSversion.restype = ctypes.c_char_p
        self.reader = L.GEOSWKTReader_create_r(self.ctx)
        self.writer = L.GEOSWKTWriter_create_r(self.ctx)
        self.version = L.GEOSversion().decode("ascii")

    def read_ring(self, ring):
        body = ",".join(f"{float(x):.17g} {float(y):.17g}" for x, y in ring)
        geom = self.lib.GEOSWKTReader_read_r(self.ctx, self.reader, f"POLYGON(({body}))".encode())
        if not geom: raise RuntimeError("GEOS could not parse a submitted polygon")
        return geom

    def area(self, geom):
        value = ctypes.c_double()
        if not self.lib.GEOSArea_r(self.ctx, geom, ctypes.byref(value)):
            raise RuntimeError("GEOS area calculation failed")
        return value.value

    def rings(self, geom):
        if self.lib.GEOSisEmpty_r(self.ctx, geom): return []
        kind = self.lib.GEOSGeomTypeId_r(self.ctx, geom)
        polygons = [geom] if kind == 3 else [self.lib.GEOSGetGeometryN_r(self.ctx, geom, i)
                                                for i in range(self.lib.GEOSGetNumGeometries_r(self.ctx, geom))]
        output = []
        for polygon in polygons:
            if self.lib.GEOSGeomTypeId_r(self.ctx, polygon) != 3:
                raise RuntimeError("Expected polygonal GEOS difference result")
            ring_geoms = [self.lib.GEOSGetExteriorRing_r(self.ctx, polygon)]
            ring_geoms.extend(self.lib.GEOSGetInteriorRingN_r(self.ctx, polygon, i)
                              for i in range(self.lib.GEOSGetNumInteriorRings_r(self.ctx, polygon)))
            polygon_rings = []
            for ring in ring_geoms:
                seq = self.lib.GEOSGeom_getCoordSeq_r(self.ctx, ring)
                size = ctypes.c_uint()
                if not self.lib.GEOSCoordSeq_getSize_r(self.ctx, seq, ctypes.byref(size)):
                    raise RuntimeError("GEOS coordinate sequence read failed")
                coords = []
                for i in range(size.value):
                    x, y = ctypes.c_double(), ctypes.c_double()
                    if not self.lib.GEOSCoordSeq_getX_r(self.ctx, seq, i, ctypes.byref(x)) or not self.lib.GEOSCoordSeq_getY_r(self.ctx, seq, i, ctypes.byref(y)):
                        raise RuntimeError("GEOS coordinate read failed")
                    coords.append([x.value, y.value])
                polygon_rings.append(coords)
            output.append(polygon_rings)
        return output

    def close(self):
        self.lib.GEOS_finish_r(self.ctx)


def main():
    if not SOURCE.is_file() or sha256(SOURCE) != EXPECTED_SHA256:
        raise SystemExit("Locked raw GT is missing or its SHA-256 differs; refusing normalization")
    if OUT.exists():
        raise SystemExit(f"Refusing to overwrite existing normalized GT: {OUT}")
    doc = json.loads(SOURCE.read_text(encoding="utf-8"))
    features = doc["features"]
    ids = [str(f["properties"]["room_id"]) for f in features]
    if len(ids) != 4 or len(set(ids)) != len(ids):
        raise SystemExit("Expected exactly four unique submitted room IDs")
    numeric = lambda rid: int(rid.rsplit("_", 1)[1])
    ordered = sorted(ids, key=numeric)
    geos = Geos()
    by_id = {str(f["properties"]["room_id"]): geos.read_ring(f["geometry"]["coordinates"][0]) for f in features}
    initial_union = None
    for rid in ordered:
        initial_union = by_id[rid] if initial_union is None else geos.lib.GEOSUnion_r(geos.ctx, initial_union, by_id[rid])
    original_areas = {rid: geos.area(g) for rid, g in by_id.items()}
    ownership = []
    normalized = dict(by_id)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            intersection = geos.lib.GEOSIntersection_r(geos.ctx, normalized[a], normalized[b])
            overlap_area = geos.area(intersection)
            if overlap_area > UNION_AREA_TOLERANCE_M2:
                # a has the smaller numeric ID, so subtract only from b.
                normalized[b] = geos.lib.GEOSDifference_r(geos.ctx, normalized[b], intersection)
                ownership.append({"overlap_pair": [a, b], "owner_room_id": a, "trimmed_room_id": b,
                                  "overlap_area_m2": overlap_area,
                                  "method": "exact GEOS intersection + difference; no buffer or convex hull"})
    normalized_areas = {rid: geos.area(g) for rid, g in normalized.items()}
    final_union = None
    for rid in ordered:
        g = normalized[rid]
        if not geos.lib.GEOSisValid_r(geos.ctx, g) or geos.lib.GEOSisEmpty_r(geos.ctx, g) or normalized_areas[rid] <= 1e-8:
            raise RuntimeError(f"Normalized room {rid} is invalid or empty")
        final_union = g if final_union is None else geos.lib.GEOSUnion_r(geos.ctx, final_union, g)
    symdiff = geos.lib.GEOSSymDifference_r(geos.ctx, initial_union, final_union)
    union_area_change = geos.area(symdiff)
    if union_area_change > UNION_AREA_TOLERANCE_M2:
        raise RuntimeError(f"Normalization changed union area/geometric support by {union_area_change:.12g} m2")
    # Check exact post-normalization overlaps before the raster protocol check.
    remaining = []
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            inter = geos.lib.GEOSIntersection_r(geos.ctx, normalized[a], normalized[b])
            area = geos.area(inter)
            if area > UNION_AREA_TOLERANCE_M2: remaining.append({"rooms": [a,b], "area_m2": area})
    if remaining: raise RuntimeError(f"Positive-area overlap remains after normalization: {remaining}")
    out_doc = json.loads(json.dumps(doc))
    out_doc["metadata"]["status"] = "human_verified_spatial_partition_normalized_overlap_ownership"
    out_doc["metadata"]["normalization"] = {
        "status": "user_authorized_geometric_normalization",
        "rule": "for each positive-area overlap, smaller numeric room ID owns the intersection; subtract it from the larger numeric ID",
        "implementation": "GEOS C API exact polygon intersection/difference",
        "geos_version": geos.version,
        "source_raw_sha256": EXPECTED_SHA256,
        "union_support_symmetric_difference_area_m2": union_area_change,
        "allowed_edit_scope": "only positive-area overlap regions; existing unannotated gaps are not filled",
    }
    change_by_room = {}
    for feature in out_doc["features"]:
        rid = str(feature["properties"]["room_id"])
        polygons = geos.rings(normalized[rid])
        if len(polygons) != 1:
            raise RuntimeError(f"{rid} became a MultiPolygon; evaluator format requires review, no automatic flattening")
        feature["geometry"] = {"type": "Polygon", "coordinates": polygons[0]}
        feature["properties"]["annotation_status"] = "human_verified_spatial_partition_normalized_overlap_ownership"
        feature["properties"]["normalization_rule"] = "authorized_smaller_numeric_room_id_owns_overlap"
        change_by_room[rid] = {"area_before_m2": original_areas[rid], "area_after_m2": normalized_areas[rid],
                               "area_removed_m2": original_areas[rid] - normalized_areas[rid]}
    OUT.mkdir(parents=True)
    (OUT / "room_polygons_normalized.geojson").write_text(json.dumps(out_doc, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    # Raw source is copied byte-for-byte into the normalized GT version.
    (OUT / "raw_room_polygons.geojson").write_bytes(SOURCE.read_bytes())
    validation = {"source_sha256": EXPECTED_SHA256, "raw_copy_sha256": sha256(OUT / "raw_room_polygons.geojson"),
                  "geos_version": geos.version, "resolution_m": LATTICE_M, "overlap_ownership": ownership,
                  "room_area_change": change_by_room, "union_support_symmetric_difference_area_m2": union_area_change,
                  "remaining_exact_positive_area_overlaps": remaining,
                  "existing_gaps_preserved": True, "room_4_geometry_unchanged": normalized_areas["room_4"] == original_areas["room_4"],
                  "functional_labels_status": "unconfirmed", "adjacency_status": "unconfirmed",
                  "object_room_assignments_status": "unconfirmed"}
    (OUT / "normalization_validation.json").write_text(json.dumps(validation, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    provenance = {"kind": "user_confirmed_room_gt_normalized_version", "created_by": "scripts/normalize_apt0_confirmed_room_gt.py",
                  "source_raw_sha256": EXPECTED_SHA256, "user_confirmation_date": "2026-10-04",
                  "user_authorized_overlap_resolution": True, "geometry_edit_policy": "smaller numeric room ID owns positive-area overlap",
                  "algorithm": "GEOS exact polygon difference", "geos_version": geos.version,
                  "functional_labels_or_adjacency_inferred": False, "builder_received_gt": False}
    (OUT / "provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    fig, axes = plt.subplots(1, 2, figsize=(13, 11), sharex=True, sharey=True)
    palette = {"room_1":"#ffd166", "room_2":"#06d6a0", "room_3":"#4cc9f0", "room_4":"#c77dff"}
    for ax, features_to_draw, title in ((axes[0], features, "Raw user-confirmed polygons"),
                                        (axes[1], out_doc["features"], "Normalized: lower numeric ID owns overlap")):
        for f in features_to_draw:
            rid = str(f["properties"]["room_id"]); ring = f["geometry"]["coordinates"][0]
            ax.add_patch(MplPolygon(ring, closed=True, facecolor=palette[rid], edgecolor=palette[rid],
                                    linewidth=1.8, alpha=.28, label=rid))
        ax.set_title(title); ax.set_xlabel("canonical X (m)"); ax.grid(True, color="#ddd", linewidth=.4)
        ax.set_aspect("equal", adjustable="box"); ax.set_xlim(-2.95,4.85); ax.set_ylim(-8.45,5.1)
    axes[0].set_ylabel("canonical Y (m)"); axes[0].legend(loc="lower left")
    fig.suptitle("Overlap handling only — no gaps filled; room_4 unchanged", y=.99)
    fig.tight_layout(); fig.savefig(OUT / "overlap_normalization_before_after.png", dpi=170, bbox_inches="tight")
    geos.close()
    print(json.dumps({"normalized_gt": str(OUT), "source_sha256": EXPECTED_SHA256,
                      "union_support_symmetric_difference_area_m2": union_area_change,
                      "overlaps": ownership, "room_area_change": change_by_room}, indent=2))


if __name__ == "__main__": main()

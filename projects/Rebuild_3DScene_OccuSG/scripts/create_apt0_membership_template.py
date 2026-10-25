#!/usr/bin/env python3
"""Create a human-review-only object-to-room membership annotation template."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
gt = ROOT / "evaluation/gt/apt_0"
registry = json.loads((gt / "object_registry.json").read_text())
out = gt / "object_room_membership.json"
if out.exists():
    raise FileExistsError(f"refusing to replace existing membership annotations: {out}")
items = []
for obj in registry["instances"]:
    geom = obj.get("geometry", {})
    bounds = geom.get("aabb_world_canonical_xyz_m")
    center = None if not bounds else [(float(a) + float(b)) / 2 for a, b in zip(bounds[0], bounds[1])]
    items.append({"gt_instance_id": obj["gt_instance_id"], "gt_room_id": None,
                  "geometry_center_candidate_W_xyz_m": center,
                  "method": "candidate_reference_point_is_full_mesh_aabb_center; not assigned until room polygons and doorway/open-plan convention are reviewed",
                  "status": "uncertain", "reviewer": None, "note": "Review after room GT; use uncertain at doorway/open-plan/ambiguous locations."})
payload = {"schema_version": 1, "scene_id": "apt_0", "status": "not_annotated",
           "coordinate_frame": "canonical_world_z_up_xy_m", "units": "m",
           "source_object_registry_sha256": __import__("hashlib").sha256((gt / "object_registry.json").read_bytes()).hexdigest(),
           "membership_rule": "independent room ID; point-in-room based on reviewed architectural footprints and documented center rule; ambiguous cases are not forced",
           "builder_input": False, "items": items}
out.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")
print(json.dumps({"output": str(out), "instance_count": len(items), "status": payload["status"]}, indent=2))

"""Dataset-neutral scene/route resolution backed by the launcher's single registry.

Local scene discovery is delegated to the registered dataset adapters. The
menu consumes these resolved specs; it does not keep its own scene list.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "configs/launcher/scenes.json"
ENV_PATH = ROOT / "configs/launcher/local_environment.json"


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _external_input(value: str | Path, habitat_root: Path, baseline_root: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    if path.parts[:2] == ("artifacts", "habitat_data"):
        return (baseline_root / path).resolve()
    return (habitat_root / path).resolve()


@dataclass(frozen=True)
class RouteSpec:
    route_id: str
    config_path: str
    sequence_id: str
    pose_count: int
    resolution_px: tuple[int, int] | None
    horizontal_fov_deg: float | None
    frame_id_prefix: str | None


@dataclass(frozen=True)
class SceneSpec:
    dataset_id: str
    scene_id: str
    display_name: str
    adapter: str
    dataset_version: str
    scene_asset: str
    navmesh: str | None
    scene_dataset_config: str | None
    asset_status: str
    routes: tuple[RouteSpec, ...]
    route_block_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    doc = _read(path)
    catalog = doc.get("catalog")
    if doc.get("schema_version") != 1 or not isinstance(catalog, dict) or catalog.get("schema_version") != 1:
        raise ValueError("launcher registry must contain schema_version=1 and catalog.schema_version=1")
    if not isinstance(catalog.get("datasets"), list) or not catalog["datasets"]:
        raise ValueError("launcher registry catalog.datasets must be a non-empty list")
    ids: set[str] = set()
    for dataset in catalog["datasets"]:
        required = {"id", "display_name", "adapter", "version", "scene_glob", "navmesh_pattern",
                    "scene_dataset_config", "route_glob", "c2_type"}
        if not isinstance(dataset, dict) or not required <= set(dataset):
            raise ValueError("dataset catalog entry is incomplete")
        if dataset["id"] in ids:
            raise ValueError(f"duplicate dataset id: {dataset['id']}")
        ids.add(dataset["id"])
        if dataset["adapter"] not in {"habitat_replica_cad_v1", "habitat_hm3d_minival_v02_v1"}:
            raise ValueError(f"unsupported dataset adapter: {dataset['adapter']}")
    if not isinstance(catalog.get("c2_template"), str):
        raise ValueError("catalog.c2_template is required")
    profiles = catalog.get("capture_profiles")
    if (not isinstance(profiles, dict) or not isinstance(profiles.get("profiles"), dict)
            or profiles.get("default_profile_id") not in profiles["profiles"]):
        raise ValueError("catalog.capture_profiles must define a valid default profile and profile paths")
    return doc


def _resolve_capture_profile(registry: dict[str, Any], profile_id: str | None = None) -> dict[str, Any]:
    catalog = registry["catalog"]["capture_profiles"]
    selected = profile_id or catalog["default_profile_id"]
    try:
        relative = catalog["profiles"][selected]
    except KeyError as exc:
        raise ValueError(f"unknown capture profile {selected!r}; choose one of {sorted(catalog['profiles'])}") from exc
    path = (ROOT / str(relative)).resolve()
    profile = _read(path)
    if profile.get("schema") != "habitat-capture-profile/v1" or profile.get("profile_id") != selected:
        raise ValueError(f"capture profile schema/id mismatch: {path}")
    resolution = profile.get("resolution_px")
    if (not isinstance(resolution, list) or len(resolution) != 2
            or any(type(v) is not int or v < 2 for v in resolution)):
        raise ValueError(f"capture profile has invalid resolution_px: {path}")
    if profile.get("intrinsics_adapter") != "adapters.habitat.frames.habitat_camera_intrinsics":
        raise ValueError(f"unsupported capture intrinsics adapter in {path}")
    return {**profile, "profile_path": str(path), "profile_sha256": _sha(path)}


def _dataset_entry(registry: dict[str, Any], dataset_id: str) -> dict[str, Any]:
    for item in registry["catalog"]["datasets"]:
        if item["id"] == dataset_id:
            return item
    raise KeyError(f"unknown dataset {dataset_id!r}")


def _route_alias(path: Path, dataset: dict[str, Any], scene_id: str) -> str:
    stem = path.stem
    if dataset["adapter"] == "habitat_hm3d_minival_v02_v1":
        match = re.match(r"^hm3d_minival_v02_\d{5}_(.+)$", stem)
        suffix = match.group(1) if match else stem
    else:
        prefix = f"m12_replica_cad_{scene_id.replace('_', '')}_"
        suffix = stem[len(prefix):] if stem.startswith(prefix) else stem
    suffix = re.sub(r"_route(?:_v\d+)?$", "", suffix)
    return suffix or stem


def _route_specs(dataset: dict[str, Any], scene_id: str) -> tuple[RouteSpec, ...]:
    route_root = ROOT
    route_glob = str(dataset["route_glob"])
    routes: list[RouteSpec] = []
    for path in sorted(route_root.glob(route_glob)):
        try:
            doc = _read(path)
        except (OSError, json.JSONDecodeError):
            continue
        if str(doc.get("scene_id", "")) != scene_id or not isinstance(doc.get("route"), list) or not doc["route"]:
            continue
        stated_type = doc.get("dataset_type")
        if stated_type and stated_type not in {dataset["c2_type"], "replica_cad" if dataset["id"] == "replica_cad" else "hm3d"}:
            continue
        resolution = doc.get("resolution_px")
        routes.append(RouteSpec(
            route_id=_route_alias(path, dataset, scene_id),
            config_path=str(path.resolve()),
            sequence_id=str(doc.get("sequence_id", "")),
            pose_count=len(doc["route"]),
            resolution_px=tuple(int(v) for v in resolution) if isinstance(resolution, list) and len(resolution) == 2 else None,
            horizontal_fov_deg=float(doc["horizontal_fov_deg"]) if doc.get("horizontal_fov_deg") is not None else None,
            frame_id_prefix=str(doc["frame_id_prefix"]) if doc.get("frame_id_prefix") else None,
        ))
    # Filenames can have different suffixes which normalize to one alias.
    seen: set[str] = set()
    unique: list[RouteSpec] = []
    for route in routes:
        if route.route_id in seen:
            route = RouteSpec(route.route_id + "_" + Path(route.config_path).stem.rsplit("_", 1)[-1],
                              route.config_path, route.sequence_id, route.pose_count,
                              route.resolution_px, route.horizontal_fov_deg, route.frame_id_prefix)
        seen.add(route.route_id)
        unique.append(route)
    return tuple(unique)


def discover_scenes(dataset_id: str | None = None, *, registry_path: Path = REGISTRY_PATH,
                    env_path: Path = ENV_PATH) -> list[SceneSpec]:
    registry = load_registry(registry_path)
    env = _read(env_path)
    habitat_root = Path(env["habitat_data_root"]).expanduser().resolve()
    datasets = registry["catalog"]["datasets"]
    if dataset_id is not None:
        datasets = [_dataset_entry(registry, dataset_id)]
    result: list[SceneSpec] = []
    for dataset in datasets:
        adapter = dataset["adapter"]
        for asset in sorted(habitat_root.glob(dataset["scene_glob"])):
            if adapter == "habitat_replica_cad_v1":
                scene_id = asset.name.removesuffix(".scene_instance.json")
                asset_stem = scene_id
            else:
                scene_id = asset.parent.name
                asset_stem = asset.name.removesuffix(".basis.glb")
            navmesh = habitat_root / str(dataset["navmesh_pattern"]).format(
                scene_id=scene_id, asset_stem=asset_stem)
            scene_dataset_config = habitat_root / str(dataset["scene_dataset_config"])
            routes = _route_specs(dataset, scene_id)
            status = "ready_assets" if navmesh.is_file() and scene_dataset_config.is_file() else "missing_required_asset"
            reason = None
            if not routes:
                reason = "No registered route config matches this scene; Run is blocked until a route is explicitly added and preflighted."
            result.append(SceneSpec(
                dataset_id=dataset["id"], scene_id=scene_id, display_name=f"{dataset['display_name']} · {scene_id}",
                adapter=adapter, dataset_version=str(dataset["version"]), scene_asset=str(asset.resolve()),
                navmesh=str(navmesh.resolve()) if navmesh.is_file() else None,
                scene_dataset_config=str(scene_dataset_config.resolve()) if scene_dataset_config.is_file() else None,
                asset_status=status, routes=routes, route_block_reason=reason))
    return sorted(result, key=lambda row: (row.dataset_id, row.scene_id))


def resolve_scene_route(dataset_id: str, scene_id: str, route_id: str, *, registry_path: Path = REGISTRY_PATH,
                        env_path: Path = ENV_PATH, capture_profile_id: str | None = None) -> tuple[SceneSpec, RouteSpec, dict[str, Any]]:
    registry = load_registry(registry_path)
    profile = _resolve_capture_profile(registry, capture_profile_id)
    scene = next((s for s in discover_scenes(dataset_id, registry_path=registry_path, env_path=env_path)
                  if s.scene_id == scene_id), None)
    if scene is None:
        raise KeyError(f"scene {scene_id!r} is not present in the local dataset resources for {dataset_id}")
    if scene.asset_status != "ready_assets":
        raise ValueError(f"{scene.display_name}: required scene/navmesh/scene-dataset asset is missing")
    route = next((r for r in scene.routes if r.route_id == route_id), None)
    if route is None:
        raise ValueError(scene.route_block_reason or f"route {route_id!r} is not registered for {scene_id}")
    dataset = _dataset_entry(registry, dataset_id)
    route_doc = _read(Path(route.config_path))
    if route_doc.get("scene_id") != scene_id or str(route_doc.get("sequence_id", "")) != route.sequence_id:
        raise ValueError("route scene/sequence identity changed since discovery")
    template_path = ROOT / registry["catalog"]["c2_template"]
    config = _read(template_path)
    ds = config["dataset"]
    env = _read(env_path)
    habitat_root = Path(env["habitat_data_root"]).expanduser().resolve()
    baseline_root = Path(env["baseline_read_root"]).expanduser().resolve()
    route_scene_config = route_doc.get("scene_dataset_config") or scene.scene_dataset_config
    route_navmesh = route_doc.get("navmesh_file") or scene.navmesh
    ds.update({"type": dataset["c2_type"], "version": route_doc.get("dataset_version", dataset["version"]),
               "scene_id": scene_id, "sequence_id": route.sequence_id,
               "scene_dataset_config": str(_external_input(route_scene_config, habitat_root, baseline_root))
                   if route_scene_config else None,
               "navmesh_file": str(_external_input(route_navmesh, habitat_root, baseline_root))
                   if route_navmesh else None,
               "provenance": route_doc.get("scene_provenance", f"Local {dataset['display_name']} scene {scene_id}"),
               "license_note": route_doc.get("scene_license_note", "Local dataset license applies")})
    if dataset["c2_type"] == "replica_cad":
        ds.pop("scene_asset", None)
        ds["scene_instance"] = scene.scene_asset
    else:
        ds.pop("scene_instance", None)
        ds["scene_asset"] = scene.scene_asset
    config["route_config"] = route.config_path
    config["capture_profile"] = profile
    width, height = profile["resolution_px"]
    config["mapping"]["resolution_policy"] = (
        f"native Habitat sensor capture at {width}x{height}; ignore route source resolution; "
        "retain route HFOV, sensor extrinsics, depth range/convention, and detector preprocessing; "
        "compute intrinsics with the declared Habitat calibration adapter"
    )
    config["mapping"]["room_semantics_status"] = "unavailable_membership_scope"
    config["mapping"]["membership_policy"] = "unavailable_elevation_scope"
    return scene, route, config


def manifest_acceptance(scene_id: str, sequence_id: str, *, workspace_root: Path = ROOT) -> dict[str, Any]:
    """Report evidence only from completed run manifests, never from file presence/configs."""
    c2_root = workspace_root / "artifacts/conceptgraphs_hybrid_runs"
    d1_root = workspace_root / "artifacts/live_processing_runs"
    c2: list[dict[str, str]] = []
    d1: list[dict[str, str]] = []
    for path in c2_root.glob("*/c2_run_manifest.json") if c2_root.is_dir() else ():
        try:
            doc = _read(path)
        except (OSError, json.JSONDecodeError):
            continue
        if doc.get("status") == "completed" and doc.get("scene_id") == scene_id and doc.get("sequence_id") == sequence_id:
            c2.append({"run_id": str(doc.get("run_id", path.parent.name)), "manifest": str(path)})
    for path in d1_root.glob("*/run_manifest.json") if d1_root.is_dir() else ():
        try:
            doc = _read(path)
        except (OSError, json.JSONDecodeError):
            continue
        if doc.get("status") == "completed" and doc.get("scene_id") == scene_id and doc.get("sequence_id") == sequence_id:
            gate = doc.get("viewer_start_gate", {})
            xyz = doc.get("viewer", {}).get("d2_xyz", {})
            d1.append({"run_id": str(doc.get("run_id", path.parent.name)), "manifest": str(path),
                       "simultaneous_d2": str(bool(gate.get("producer_started") and gate.get("confirmed_browser_ready")
                                                    and xyz.get("status") not in {"not_requested", "failed"}))})
    return {"c2": sorted(c2, key=lambda r: r["run_id"]), "d1": sorted(d1, key=lambda r: r["run_id"]),
            "c2_status": "accepted_by_manifest" if c2 else "no_completed_manifest_evidence",
            "d1_status": "accepted_by_manifest" if d1 else "no_completed_manifest_evidence",
            "d2_simultaneous_status": "accepted_by_manifest" if any(x["simultaneous_d2"] == "True" for x in d1)
                else "no_completed_simultaneous_manifest_evidence"}

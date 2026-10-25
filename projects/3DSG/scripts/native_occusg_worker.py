#!/usr/bin/env python3
"""3DSG-owned OccuSG worker shim for calibrated multi-sensor RGB-D input.

The upstream mapper worker and both native repositories remain unmodified.
This shim delegates session/protocol handling to the checked-out mapper worker
and injects a project-owned OccuSG MappingStage adapter.
"""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback


def main() -> int:
    conceptgraphs = Path(os.environ["THREEDSG_CONCEPTGRAPHS_ROOT"]).resolve(strict=True)
    project = Path(os.environ["THREEDSG_PROJECT_ROOT"]).resolve(strict=True)
    worker_path = conceptgraphs / "scripts/run_t2_d_cached_packet_worker.py"
    sys.path.insert(0, str(project))
    spec = importlib.util.spec_from_file_location("_3dsg_cached_packet_worker", worker_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load native mapper worker: {worker_path}")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    original_init = worker._init_occusg
    original_packet = worker._occ_packet

    def init_occusg(source: Path, run_root: Path):
        session, mapping, region = original_init(source, run_root)
        binary = mapping.worker.binary
        mapping.close()
        from exploration.native_multisensor_mapping import MultiSensorNativeMappingStage
        mapping = MultiSensorNativeMappingStage(binary)
        session.builder.mapping_stage = mapping
        original_update = session.update_frame

        def update_frame(packet):
            result = original_update(packet)
            records = mapping.records
            if records:
                path = run_root / "diagnostics" / "occusg_multisensor" / f"frame_{packet.frame_index:08d}.json"
                worker.atomic_write_json(path, {
                    "schema": "3dsg-occusg-multisensor-integration/v1",
                    "frame_id": packet.frame_id, "frame_index": packet.frame_index,
                    "mapping_record": records[-1],
                })
            return result

        session.update_frame = update_frame
        return session, mapping, region

    def occ_packet(packet: dict, source: Path):
        result = original_packet(packet, source)
        shared_stride = int(os.environ.get("THREEDSG_OCCUSG_BACKPROJECTION_STRIDE", "1"))
        primary_stride = int(os.environ.get(
            "THREEDSG_OCCUSG_PRIMARY_BACKPROJECTION_STRIDE", str(shared_stride)))
        auxiliary_stride = int(os.environ.get(
            "THREEDSG_OCCUSG_AUXILIARY_BACKPROJECTION_STRIDE", str(shared_stride)))
        if primary_stride not in {1, 2, 4} or auxiliary_stride not in {1, 2, 4}:
            raise ValueError("OccuSG primary/auxiliary backprojection stride must be 1, 2, or 4")
        metadata = dict(result.source_metadata)
        metadata["backprojection_stride"] = primary_stride
        metadata["m2_identity"] = _m2_identity(packet, sensor_id="rgbd_primary")
        descriptor = packet.get("capture_record", {}).get("auxiliary_depth")
        if descriptor is None:
            return __import__("dataclasses").replace(result, source_metadata=metadata)
        capture_root = Path(packet.get("capture_root", source / "capture")).resolve()
        record = packet["capture_record"]
        file_path = (capture_root / record["frame_dir"] / descriptor["file"]).resolve(strict=True)
        expected_file = record.get("raw_files_sha256", {}).get(descriptor["file"])
        if not expected_file or expected_file != descriptor.get("file_sha256"):
            raise ValueError("auxiliary depth record hash does not match captured file hash")
        calibration = {key: value for key, value in descriptor.items()
                       if key not in {"file_sha256", "calibration_sha256"}}
        metadata.update({
            "source_hashes": dict(packet["source_hashes"]),
            "future_manifest_required": False,
            "scope_semantics": "unavailable",
            "depth_units": "m", "depth_representation": "axial",
            "depth_scale_to_m": 1.0,
            "min_depth_m": 0.1, "max_depth_m": 10.0,
            "auxiliary_depth": {
            "path": str(file_path), "file_sha256": expected_file,
            "calibration": calibration,
            "calibration_sha256": descriptor["calibration_sha256"],
            "backprojection_stride": auxiliary_stride,
            },
        })
        metadata["auxiliary_depth"]["m2_identity"] = _m2_identity(
            packet, sensor_id="navigation_depth_camera",
            additional_hashes={
                "depth_file_sha256": expected_file,
                "calibration_sha256": descriptor["calibration_sha256"],
            },
        )
        return __import__("dataclasses").replace(result, source_metadata=metadata)

    worker._init_occusg = init_occusg
    worker._occ_packet = occ_packet
    original_run_worker = worker.run_worker

    def traced_run_worker(*args, **kwargs):
        try:
            return original_run_worker(*args, **kwargs)
        except BaseException:
            traceback.print_exc(file=sys.stderr)
            raise

    worker.run_worker = traced_run_worker
    return worker.main()


def _m2_identity(packet: dict, *, sensor_id: str,
                 additional_hashes: dict[str, str] | None = None) -> dict:
    """Build an OccuSG M2 identity from the validated source frame record.

    The digest binds the sorted immutable file hashes and sensor-specific
    calibration/depth hashes; it never hashes a newly serialized point cloud.
    The monotonic timestamp is kept in its declared host clock domain.
    """
    source_hashes = packet.get("source_hashes")
    if not isinstance(source_hashes, dict) or not source_hashes:
        raise ValueError("M2 binary transport requires source frame hashes")
    capture_monotonic_s = packet.get("capture_monotonic_s")
    clock_domain = packet.get("capture_monotonic_domain")
    if isinstance(capture_monotonic_s, bool) or not isinstance(capture_monotonic_s, (int, float)):
        raise ValueError("M2 binary transport requires capture_monotonic_s")
    if not isinstance(clock_domain, str) or not clock_domain.startswith("host_monotonic:"):
        raise ValueError("M2 binary transport requires a host monotonic clock domain")
    hash_record = {
        "source_hashes": dict(sorted(source_hashes.items())),
        "sensor_id": sensor_id,
        "additional_hashes": dict(sorted((additional_hashes or {}).items())),
    }
    canonical = json.dumps(hash_record, sort_keys=True, separators=(",", ":"),
                           allow_nan=False).encode("utf-8")
    return {
        "m2_frame_id": str(packet["frame_id"]),
        "m2_frame_index": int(packet["frame_index"]),
        "m2_capture_monotonic_ns": round(float(capture_monotonic_s) * 1_000_000_000),
        "m2_capture_monotonic_domain": clock_domain,
        "m2_package_sha256": hashlib.sha256(canonical).hexdigest(),
        "m2_sensor_id": sensor_id,
    }


if __name__ == "__main__":
    raise SystemExit(main())

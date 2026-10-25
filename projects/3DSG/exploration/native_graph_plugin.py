"""Live native ConceptGraphs/OccuSG provider backed by isolated workers.

The mapper repositories remain external/read-only dependencies. This plugin
owns only per-run adapters, immutable packets, and paired publication. The
viewer snapshot is sourced from OccuSG's durable prefix; ConceptGraphs state is
retained with branch provenance until an explicit cross-backend join policy is
implemented.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable

from .legacy_capture import LegacyCaptureStore
from .legacy_mapper_packets import build_current_mapper_packet
from .native_perception_client import NativePerceptionWorkerClient
from .process_priority import graph_worker_nice_from_env
from .scene_graph_provider import GraphProviderCapabilities, SceneGraphSnapshot


ROOT = Path(__file__).resolve().parents[1]
PROJECTS_ROOT = ROOT.parent
DEFAULT_CG = Path(os.environ.get(
    "3DSG_CONCEPTGRAPHS_ROOT", PROJECTS_ROOT / "Fusion_Concept_OccuSG"))
DEFAULT_OCC = Path(os.environ.get(
    "3DSG_OCCUSG_ROOT", PROJECTS_ROOT / "Rebuild_3DScene_OccuSG"))
from .handoff_paths import config_root
DEFAULT_SOURCE = config_root()


class NativeConceptOccuGraphPlugin:
    """Frame-aligned provider using persistent native mapper sessions."""

    provider_id = "conceptgraphs+occusg:native_paired/v1"
    backend_name = provider_id

    def __init__(self, *, p2_client: Any, live_publisher: Any,
                 event_sink: Callable[[str, dict[str, Any]], None],
                 conceptgraphs_root: Path = DEFAULT_CG, occusg_root: Path = DEFAULT_OCC,
                 source: Path = DEFAULT_SOURCE) -> None:
        if p2_client is None:
            raise ValueError("native graph provider requires the persistent P2 worker")
        self.p2_client = p2_client
        self.live_publisher = live_publisher
        self.event_sink = event_sink
        self.cg_root = Path(conceptgraphs_root).resolve(strict=True)
        self.occ_root = Path(occusg_root).resolve(strict=True)
        self.source = Path(source).resolve(strict=True)
        self.run_root = Path(live_publisher.root).resolve().parent
        self.output = self.run_root / "native_graph"
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / "logs").mkdir(parents=True, exist_ok=True)
        self.packet_root = self.output / "packets"
        self.packet_root.mkdir(exist_ok=True)
        self.capabilities = GraphProviderCapabilities(
            provider_id=self.provider_id,
            input_modalities=("rgb", "primary_axial_depth", "auxiliary_axial_depth", "camera_pose", "p2_masks",
                              "p3_geometry", "openclip_features"),
            entity_types=("agent", "navigation", "object", "object_observation", "room"),
            relation_types=("navigable_path", "room_contains"),
            supports_incremental_updates=True,
            supports_auxiliary_depth=True,
            metadata={
                "provider_role": "native_dual_mapper_adapter",
                "semantic_quality": "unscored",
                "object_branch": "ConceptGraphs native session; preserved as provenance",
                "snapshot_branch": "OccuSG native room/navigation prefix",
                "cross_backend_graph_join": False,
                "auxiliary_depth_status": "OccuSG integrates independently calibrated auxiliary depth; ConceptGraphs remains primary-RGBD only",
            },
        )
        self._revision = 0
        self._last_frame_index = -1
        self._last_frame_id: str | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._aborted = False
        self._pair_revision = 0
        self._mapper_workers: list[tuple[str, Any]] = []
        self._perception: NativePerceptionWorkerClient | None = None
        try:
            self._start_workers()
        except BaseException:
            self.close()
            raise

    def _start_workers(self) -> None:
        from scripts.replay_saved_native_keyframe import (
            atomic_json, launch_mapper, load_source_paths, sha256_file,
        )

        args = SimpleNamespace(conceptgraphs_root=self.cg_root, occusg_root=self.occ_root)
        paths = load_source_paths(args)
        for name in ("cg_python", "occ_python", "appearance_python", "p2_worker",
                     "p2_weights", "p2_profile", "p2_names", "p3_profile",
                     "feature_contract", "checkpoint", "worker_script", "route_config"):
            paths[name].resolve(strict=True)
        self._paths = paths
        self._perception = NativePerceptionWorkerClient(
            python=paths["appearance_python"], worker_script=ROOT / "scripts/native_perception_worker.py",
            conceptgraphs_root=self.cg_root, occusg_root=self.occ_root,
            output_root=self.output, p3_profile=paths["p3_profile"],
            feature_contract=paths["feature_contract"], checkpoint=paths["checkpoint"],
            stderr_path=self.output / "logs/perception.stderr.log")
        route_hash = sha256_file(paths["route_config"])
        bootstrap = f"bootstrap-{hashlib.sha256((self.live_publisher.run_id + route_hash).encode()).hexdigest()[:16]}"
        self._bootstrap_id = bootstrap
        graph_nice = graph_worker_nice_from_env()
        mapper_nice = graph_nice
        legacy_nice_text = os.environ.get("THREEDSG_NATIVE_MAPPER_NICE")
        if mapper_nice is None and legacy_nice_text is not None:
            try:
                mapper_nice = int(legacy_nice_text)
            except ValueError as error:
                raise ValueError("THREEDSG_NATIVE_MAPPER_NICE must be in 0..19") from error
            if not 0 <= mapper_nice <= 19:
                raise ValueError("THREEDSG_NATIVE_MAPPER_NICE must be in 0..19")
        atomic_json(self.output / "scheduler_policy.json", {
            "schema": "3dsg-native-worker-scheduling/v1",
            "graph_worker_nice": graph_nice,
            "native_mapper_nice": mapper_nice,
            "scope": "P2 and P3/OpenCLIP use graph_worker_nice; ConceptGraphs and OccuSG use native_mapper_nice; native descendants inherit",
            "default": "no niceness override" if graph_nice is None and mapper_nice is None else None,
        })
        for kind, python in (("conceptgraphs", paths["cg_python"]),
                             ("occusg", paths["occ_python"])):
            process = launch_mapper(
                kind, python=python, worker=paths["worker_script"], output=self.output,
                packet_root=self.packet_root, source=self.source,
                run_id=self.live_publisher.run_id, bootstrap_id=bootstrap,
                route_hash=route_hash,
                stderr_path=self.output / "logs" / f"{kind}.worker.stderr.log",
                occusg_root=self.occ_root, nice_value=mapper_nice,
            )
            self._mapper_workers.append((kind, process))

    def update(self, observation: Any) -> SceneGraphSnapshot:
        from scripts.replay_saved_native_keyframe import atomic_json

        with self._lock:
            if self._closed:
                raise RuntimeError("native graph provider is closed")
            index, frame_id = observation.keyframe.frame_index, observation.keyframe.frame_id
            if index <= self._last_frame_index:
                raise ValueError("native graph provider requires strictly increasing keyframes")
            record = self._capture_store().export(observation)
            p2_result = self.p2_client.update_frame(
                frame_index=index, frame_id=frame_id,
                timestamp_s=observation.packet.simulator_time_s,
                rgb_file=observation.packet.rgb_ref)
            p2_rows = self._persist_p2_masks(p2_result, frame_id=frame_id, frame_index=index)
            seed = {"frame_id": frame_id, "frame_index": index,
                    "timestamp_s": float(observation.packet.simulator_time_s),
                    "capture_record": record, "capture_root": str((self.output / "capture").resolve()),
                    "asset_root": str((self.output / "assets").resolve()),
                    "source_hashes": dict(record["raw_files_sha256"]),
                    "p2_detections": p2_rows}
            seed_path = self.output / "perception_inputs" / f"frame_{index:08d}.json"
            atomic_json(seed_path, seed)
            assert self._perception is not None
            perception = self._perception.process_frame(seed_path, frame_id=frame_id,
                                                        frame_index=index)
            p3_doc = json.loads(Path(perception["p3_path"]).read_text(encoding="utf-8"))
            feature_doc = json.loads(Path(perception["feature_path"]).read_text(encoding="utf-8"))
            packet = build_current_mapper_packet(
                observation=observation, capture_root=self.output / "capture",
                capture_record=record, asset_root=self.output / "assets",
                p2_detections=p2_rows, p3_detections=p3_doc["detections"],
                features=feature_doc["features"])
            packet_path = self.packet_root / f"packet-{index:06d}.json"
            atomic_json(packet_path, packet)
            commit, events = self._dispatch_pair(packet_path, packet)
            auxiliary_depth_consumed = self._auxiliary_depth_consumed(
                frame_id=frame_id, frame_index=index, capture_record=record)
            snapshot = self._read_occ_snapshot(
                commit, frame_id=frame_id, frame_index=index,
                run_id=observation.keyframe.run_id, scene_id=observation.keyframe.scene_id,
                p3_doc=p3_doc, auxiliary_depth_consumed=auxiliary_depth_consumed)
            self._revision += 1
            self._last_frame_index, self._last_frame_id = index, frame_id
            self.event_sink("native_graph_pair_committed", {
                "provider_id": self.provider_id, "frame_id": frame_id,
                "frame_index": index, "revision": self._revision,
                "object_branch_event_sha256": events["objects"]["event_sha256"],
                "room_branch_event_sha256": events["rooms"]["event_sha256"],
                "graph_join_performed": False,
            })
            return snapshot

    def _auxiliary_depth_consumed(self, *, frame_id: str, frame_index: int,
                                  capture_record: dict[str, Any]) -> bool:
        descriptor = capture_record.get("auxiliary_depth")
        if not isinstance(descriptor, dict):
            return False
        expected_hash = str(descriptor.get("file_sha256", ""))
        if len(expected_hash) != 64:
            raise ValueError("captured auxiliary depth is missing its SHA-256")
        diagnostic_path = (self.output / "diagnostics" / "occusg_multisensor"
                           / f"frame_{frame_index:08d}.json")
        diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
        record = diagnostic.get("mapping_record", {})
        if (diagnostic.get("frame_id") != frame_id
                or int(diagnostic.get("frame_index", -1)) != frame_index
                or record.get("auxiliary_sensor_integrated") is not True
                or record.get("auxiliary_file_sha256") != expected_hash):
            raise ValueError("OccuSG auxiliary-depth diagnostic does not match this keyframe")
        return True

    def _capture_store(self) -> LegacyCaptureStore:
        if hasattr(self, "_capture_store_instance"):
            return self._capture_store_instance
        effective = json.loads((self.run_root / "effective_config.json").read_text(encoding="utf-8"))
        scene_file = Path(effective["scene_config"]["scene_asset"]).resolve(strict=True)
        self._capture_store_instance = LegacyCaptureStore(
            self.output / "capture", scene_file=scene_file, byte_cap=2 * 1024**3)
        return self._capture_store_instance

    def _persist_p2_masks(self, result: dict[str, Any], *, frame_id: str,
                          frame_index: int) -> list[dict[str, Any]]:
        from scripts.replay_saved_native_keyframe import sha256_file
        import shutil

        if result.get("frame_id") != frame_id or int(result.get("frame_index", -1)) != frame_index:
            raise ValueError("P2 response identity differs from graph keyframe")
        mask_root = self.output / "assets/detection_2d/masks"
        mask_root.mkdir(parents=True, exist_ok=True)
        rows = []
        for ordinal, raw in enumerate(result.get("detections", [])):
            source = Path(raw["mask_file"]).resolve(strict=True)
            target = mask_root / f"{frame_index:08d}_{ordinal:04d}.npy"
            shutil.copyfile(source, target)
            rows.append({
                "detection_id": str(raw["detection_id"]),
                "class_id": int(raw["class_id"]), "class_raw": str(raw["class_raw"]),
                "class_canonical": str(raw["class_canonical"]),
                "score": float(raw["score"]), "score_raw": float(raw["score"]),
                "bbox_xyxy": [float(x) for x in raw["bbox_xyxy"]],
                "mask_file": str(target.resolve()), "mask_sha256": sha256_file(target),
                "mask_pixels": int(raw["mask_pixels"]),
            })
        return rows

    def _dispatch_pair(self, packet_path: Path,
                       packet: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        from scripts.replay_saved_native_keyframe import (
            _read_paired_events_and_ack, read_json_line, send_frame,
        )

        errors: list[BaseException] = []
        result: dict[str, Any] = {}

        def acknowledge() -> None:
            try:
                result.update(_read_paired_events_and_ack(
                    self.output, frame_id=packet["frame_id"],
                    frame_index=packet["frame_index"],
                    source_hashes=packet["source_hashes"],
                    scene_id=packet["scene_id"], sequence_id=packet["sequence_id"],
                    joined_revision=self._pair_revision + 1))
            except BaseException as exc:
                errors.append(exc)

        for _kind, process in self._mapper_workers:
            send_frame(process, packet_path)
        thread = threading.Thread(target=acknowledge, name="native-graph-pair-ack", daemon=True)
        thread.start()
        responses = {}
        try:
            for kind, process in self._mapper_workers:
                response = read_json_line(process)
                if (response.get("status") != "frame_published"
                        or response.get("kind") != kind
                        or int(response.get("frame_index", -1)) != packet["frame_index"]):
                    raise RuntimeError(f"native {kind} worker returned a mismatched frame: {response}")
                responses[kind] = response
            thread.join(timeout=10.0)
            if errors:
                raise errors[0]
            if thread.is_alive() or not result:
                raise TimeoutError("native paired commit barrier did not finish")
        except BaseException:
            from scripts.replay_saved_native_keyframe import cancel_workers
            try:
                cancel_workers(self.output, self._mapper_workers,
                               f"paired_dispatch_failed:{packet['frame_id']}", timeout_s=20.0)
            except BaseException:
                pass
            thread.join(timeout=3.0)
            raise
        self._pair_revision += 1
        return result["commit"], result["events"]

    def _read_occ_snapshot(self, commit: dict[str, Any], *, frame_id: str,
                           frame_index: int, run_id: str, scene_id: str,
                           p3_doc: dict[str, Any],
                           auxiliary_depth_consumed: bool) -> SceneGraphSnapshot:
        from scripts.replay_saved_native_keyframe import sha256_file

        relative = commit["durable_state_paths"]["rooms"]
        path = (self.output / str(relative)).resolve(strict=True)
        if not path.is_relative_to(self.output.resolve()):
            raise ValueError("OccuSG snapshot path escaped native graph run directory")
        state = json.loads(path.read_text(encoding="utf-8"))
        graph = state.get("snapshot", {})
        if (graph.get("scene_id") != scene_id or int(graph.get("revision", -1)) < 0
                or not graph.get("nodes")):
            raise ValueError("OccuSG durable snapshot has invalid scene/revision/nodes")
        nodes = []
        positions: dict[str, list[float]] = {}
        floor_z = next((float(row.get("attributes", {}).get("pose_W", [0, 0, 0])[2])
                        for row in graph["nodes"]
                        if row.get("kind") == "agent"
                        and len(row.get("attributes", {}).get("pose_W", [])) == 3), 0.0)
        for raw in graph["nodes"]:
            attrs = dict(raw.get("attributes", {}))
            node_id = str(raw.get("node_id", ""))
            position = (attrs.get("pose_W") or attrs.get("center_W")
                        or attrs.get("representative_center_W"))
            if position is None:
                xy = attrs.get("center_xy") or attrs.get("centroid_xy")
                if xy is not None and len(xy) == 2:
                    position = [float(xy[0]), float(xy[1]), floor_z]
            if position is None or len(position) != 3:
                continue
            position = [float(value) for value in position]
            if not all(math.isfinite(value) for value in position):
                raise ValueError(f"OccuSG node has non-finite position: {node_id}")
            positions[node_id] = position
            nodes.append({"id": node_id, "kind": str(raw.get("kind", "unknown")),
                          "label": str(attrs.get("name") or attrs.get("class_name")
                                       or raw.get("kind", "")),
                          "position": position, "attributes": attrs,
                          "source_backend": "occusg"})
        # P3 detections are frame observations, not persistent object identities.
        # Include them as explicitly provisional observation nodes; do not claim
        # they are the ConceptGraphs canonical object hypotheses.
        for row in p3_doc.get("detections", []):
            detection = row.get("detection_3d")
            if row.get("status") != "accepted" or not isinstance(detection, dict):
                continue
            node_id = f"observation:{detection['detection_id']}"
            position = [float(value) for value in detection["center_W"]]
            if len(position) != 3:
                raise ValueError("P3 accepted detection center must be XYZ")
            positions[node_id] = position
            nodes.append({"id": node_id, "kind": "object_observation",
                          "label": str(detection.get("class_name") or "object"),
                          "position": position, "status": "provisional_observation",
                          "attributes": detection, "source_backend": "p3_geometry"})
        edges = []
        for raw in graph.get("edges", []):
            source = str(raw.get("source_id", "")); target = str(raw.get("target_id", ""))
            if source in positions and target in positions:
                edges.append({"source": source, "target": target,
                              "relation": str(raw.get("relation", "related")),
                              "directed": bool(raw.get("directed", True)),
                              "provenance": raw.get("provenance", "occusg")})
        object_event = self.output / "branches/objects/events.jsonl"
        return SceneGraphSnapshot(
            provider_id=self.provider_id, run_id=run_id, scene_id=scene_id,
            frame_id=frame_id, frame_index=frame_index, revision=self._revision + 1,
            nodes=tuple(nodes), edges=tuple(edges), status="provisional",
            provenance={
                "paired_commit_revision": int(commit["joined_revision"]),
                "source_hashes": dict(commit["source_hashes"]),
                "branch_state_paths": dict(commit["durable_state_paths"]),
                "object_branch_events_sha256": _event_hash_for_frame(
                    object_event, frame_id, expected=commit["branch_event_sha256"]["objects"]),
                "room_branch_state_sha256": sha256_file(path),
                "snapshot_semantics": "OccuSG prefix plus current P3 observation nodes",
                "cross_backend_graph_join": False,
                "conceptgraphs_branch": "durable native state retained; not merged into viewer snapshot",
                "room_membership_quality": "unscored",
                "auxiliary_depth_consumed_by_occusg": auxiliary_depth_consumed,
            })

    def publish(self, commit: Any) -> None:
        snapshot = commit.graph_snapshot
        if snapshot is None:
            raise ValueError("native graph provider publication requires its snapshot")
        payload = snapshot.to_dict()
        payload.update({"revision": commit.dispatch_sequence,
                        "host_monotonic_s": commit.committed_monotonic_s,
                        "backend": self.provider_id, "status": "live_provisional"})
        self.live_publisher.publish_graph(payload)
        self.event_sink("graph_revision_published", {
            "revision": commit.dispatch_sequence, "frame_id": commit.frame_id,
            "frame_index": commit.frame_index, "node_count": len(snapshot.nodes),
            "edge_count": len(snapshot.edges), "backend": self.provider_id,
        })

    def abort(self, reason: str = "provider_abort", *,
              deadline_monotonic_s: float | None = None) -> None:
        """Interrupt native processes after the graph drain deadline expires."""
        if self._aborted:
            return
        self._aborted = True
        from scripts.replay_saved_native_keyframe import cancel_workers
        if self._mapper_workers:
            remaining = (5.0 if deadline_monotonic_s is None else
                         max(0.001, deadline_monotonic_s - time.monotonic()))
            cancel_workers(self.output, self._mapper_workers, reason,
                           timeout_s=min(5.0, remaining), terminate_grace_s=1.0)
        if self._perception is not None:
            self._perception.abort(deadline_monotonic_s=deadline_monotonic_s)
        try:
            self.event_sink("native_graph_provider_aborted", {
                "provider_id": self.provider_id, "reason": reason,
                "last_completed_frame_id": self._last_frame_id,
            })
        except BaseException:
            pass

    def close(self, *, deadline_monotonic_s: float | None = None) -> None:
        if self._closed:
            return
        deadline = (time.monotonic() + 30.0 if deadline_monotonic_s is None
                    else float(deadline_monotonic_s))

        def remaining(maximum_s: float) -> float:
            return min(maximum_s, max(0.0, deadline - time.monotonic()))

        lock_timeout = remaining(5.0)
        if not self._lock.acquire(timeout=lock_timeout):
            self.abort("provider_close_wait_timeout", deadline_monotonic_s=deadline)
            lock_timeout = remaining(5.0)
            if not self._lock.acquire(timeout=lock_timeout):
                raise TimeoutError("native provider update did not stop after abort")
        self._closed = True
        failures: list[str] = []
        try:
            if self._perception is not None:
                try:
                    timeout = remaining(30.0)
                    if timeout <= 0.0:
                        self._perception.abort(deadline_monotonic_s=deadline)
                        raise TimeoutError("shutdown deadline expired before perception finalize")
                    self._perception.close(timeout_s=timeout, finalize=not self._aborted,
                                           deadline_monotonic_s=deadline)
                except BaseException as exc:
                    failures.append(f"perception:{type(exc).__name__}:{exc}")
            if self._mapper_workers and not self._aborted:
                from scripts.replay_saved_native_keyframe import cancel_workers, read_json_line
                try:
                    if remaining(120.0) <= 0.0:
                        raise TimeoutError("shutdown deadline expired before mapper finalize")
                    for _kind, process in self._mapper_workers:
                        if process.poll() is None and process.stdin is not None:
                            process.stdin.write('{"op":"finalize"}\n'); process.stdin.flush()
                    for kind, process in self._mapper_workers:
                        if process.poll() is not None:
                            continue
                        timeout = remaining(120.0)
                        if timeout <= 0.0:
                            raise TimeoutError("shutdown deadline expired while awaiting mapper finalize")
                        result = read_json_line(process, timeout_s=timeout)
                        if result.get("status") != "finalized" or result.get("kind") != kind:
                            raise RuntimeError(f"native {kind} finalize failed: {result}")
                        timeout = remaining(30.0)
                        if timeout <= 0.0:
                            raise TimeoutError("shutdown deadline expired while waiting for mapper exit")
                        process.wait(timeout=timeout)
                    for _kind, process in self._mapper_workers:
                        for stream_name in ("stdin", "stdout", "_3dsg_log", "_3dsg_stdout_noise"):
                            stream = getattr(process, stream_name, None)
                            if stream is not None and not stream.closed:
                                stream.close()
                except BaseException as exc:
                    failures.append(f"mapper_finalize:{type(exc).__name__}:{exc}")
                    self._aborted = True
                    try:
                        timeout = max(0.001, remaining(20.0))
                        cancel_workers(self.output, self._mapper_workers,
                                       "native_provider_close_failure", timeout_s=timeout)
                    except BaseException as cancel_exc:
                        failures.append(f"mapper_cancel:{type(cancel_exc).__name__}:{cancel_exc}")
        finally:
            self._lock.release()
        if failures:
            raise RuntimeError("native graph provider did not close cleanly: " + ";".join(failures))


def _event_hash_for_frame(path: Path, frame_id: str, *, expected: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("source_frame_id") == frame_id:
            if row.get("event_sha256") != expected:
                raise ValueError("ConceptGraphs event hash differs from paired commit")
            return expected
    raise ValueError("ConceptGraphs event for paired frame is missing")


def create_plugin(*, p2_client, live_publisher, event_sink):
    return NativeConceptOccuGraphPlugin(p2_client=p2_client,
                                       live_publisher=live_publisher,
                                       event_sink=event_sink)

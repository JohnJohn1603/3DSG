"""Causal, read-only timeline recording for the cached ConceptGraphs mapper.

The ConceptGraphs reference mapper owns its mutable state and does not expose a
GraphDelta.  This module therefore records immutable prefix snapshots at the
frame boundary without pretending that the hypotheses have persistent IDs.  A
snapshot contains revision-local hypothesis IDs plus observation lineage and a
geometry file for that revision.  It never feeds data back into the mapper.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import time
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from .live_revision_io import AcknowledgedPublisher, atomic_write_bytes


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False,
                      allow_nan=False) + "\n"


def _safe(value: str) -> str:
    return value.replace(":", "_").replace("/", "_")


class HybridCausalTimelineRecorder:
    """Record mapper state at frame boundaries without mutating mapper state."""

    schema = "conceptgraphs-causal-object-timeline/v1"

    def __init__(self, root: Path, *, scene_id: str, sequence_id: str,
                 cache_root: Path, mapping_output: Path, input_hashes: Mapping[str, str],
                 config_hash: str, seed: int, room_snapshot: Path | None,
                 runner_sha256: str, live_publish_root: Path | None = None):
        self.root = Path(root).resolve()
        if self.root.exists() and any(self.root.iterdir()):
            raise FileExistsError(f"timeline output must be new and empty: {self.root}")
        self.root.mkdir(parents=True, exist_ok=False)
        self.snapshots = self.root / "snapshots"
        self.geometry = self.root / "geometry"
        self.snapshots.mkdir(); self.geometry.mkdir()
        self.scene_id = str(scene_id)
        self.sequence_id = str(sequence_id)
        self.cache_root = Path(cache_root).resolve()
        self.mapping_output = Path(mapping_output).resolve()
        self.input_hashes = dict(input_hashes)
        self.config_hash = str(config_hash)
        self.seed = int(seed)
        self.room_snapshot = Path(room_snapshot).resolve() if room_snapshot else None
        self.runner_sha256 = runner_sha256
        self.live_publish_root = Path(live_publish_root).resolve() if live_publish_root else None
        self.publisher = (AcknowledgedPublisher(self.live_publish_root, "objects")
                          if self.live_publish_root else None)
        self.started = time.perf_counter()
        self.rows: list[dict[str, Any]] = []
        self.deltas: list[dict[str, Any]] = []
        self.previous_hypotheses: set[str] = set()
        self._last_frame = -1
        # In a live join the room producer owns its own prefix state. Reading
        # the cached terminal room delta here would leak future room geometry.
        self.room_revision_by_frame = ({} if self.publisher else self._prepare_room_timeline())

    def _prepare_room_timeline(self) -> dict[int, str]:
        """Materialize existing native room deltas without running native code."""
        if self.room_snapshot is None:
            return {}
        delta_path = self.room_snapshot.parent / "delta_log.json"
        if not delta_path.is_file():
            return {}
        from standalone_sg.pipeline import SceneGraphSnapshot
        from standalone_sg.serialization.json_graph import load_delta_log, load_snapshot, replay_deltas, write_snapshot
        final_room = load_snapshot(self.room_snapshot)
        deltas = load_delta_log(delta_path)
        initial = SceneGraphSnapshot(final_room.schema_version, None, None, 0, None, (), (),
                                     final_room.config_hash, {})
        room_dir = self.root / "room_revisions"
        room_dir.mkdir(parents=True, exist_ok=True)
        result: dict[int, str] = {}
        for ordinal, delta in enumerate(deltas, start=1):
            frame_index = delta.diagnostics.get("frame_index")
            if frame_index is None:
                # The terminal flush has no frame prefix and is not used for a
                # frame snapshot.
                continue
            state = replay_deltas(initial, deltas[:ordinal])
            if (state.scene_id, state.sequence_id) != (self.scene_id, self.sequence_id):
                raise ValueError("room delta scene/sequence differs from object cache")
            path = room_dir / f"rev-{int(frame_index):06d}.json"
            write_snapshot(path, state)
            result[int(frame_index)] = str(path.relative_to(self.root))
        return result

    def record_frame(self, *, frame_index: int, frame_id: str, timestamp_s: float,
                     objects, filtered_ids_by_frame: Mapping[int, list[str]],
                     p2_frames: list[dict[str, Any]], poses_by_frame_id: Mapping[str, Any],
                     processing_ms: float | None = None) -> None:
        """Serialize a prefix state; no reference to ``objects`` is retained."""
        if int(frame_index) <= self._last_frame:
            raise ValueError("timeline frames must be strictly increasing")
        # Local import avoids a dependency cycle and reuses the existing
        # non-mutating lineage extraction helper.
        from scripts.run_conceptgraphs_cached_hybrid import _stage_lineage
        rows = _stage_lineage(objects, filtered_ids_by_frame, p2_frames, poses_by_frame_id,
                              "online_hypotheses")
        revision = len(self.rows) + 1
        snapshot_objects: list[dict[str, Any]] = []
        for row, obj in zip(rows, objects):
            hypothesis_id = "hypothesis_" + str(row["lineage_key"])
            cloud_rel = Path("geometry") / f"rev-{revision:06d}" / f"{_safe(hypothesis_id)}.npz"
            cloud_path = self.root / cloud_rel
            cloud_path.parent.mkdir(parents=True, exist_ok=True)
            # Copy arrays out of the live reference object before writing.  The
            # recorder never calls a mapper method and never consumes RNG.
            points = np.array(obj["pcd"].points, dtype=np.float32, copy=True)
            colors = np.array(obj["pcd"].colors, dtype=np.float32, copy=True)
            if not len(points) or points.ndim != 2 or points.shape[1] != 3:
                raise ValueError(f"invalid online cloud for {hypothesis_id}")
            if len(colors) != len(points):
                colors = np.zeros((len(points), 3), dtype=np.float32)
            buffer = io.BytesIO()
            np.savez_compressed(buffer, xyz=points, rgb=np.clip(colors * 255, 0, 255).astype(np.uint8))
            atomic_write_bytes(cloud_path, buffer.getvalue())
            snapshot_objects.append({**row, "hypothesis_id": hypothesis_id,
                                     "id_semantics": "revision_local_content_lineage; not a persistent postprocess ID",
                                     "geometry_revision": f"online_frame_{int(frame_index)}",
                                     "cloud_npz": str(cloud_rel), "cloud_sha256": _sha(cloud_path)})
        pose = poses_by_frame_id.get(str(frame_id))
        pose_value = np.asarray(pose, dtype=np.float64).tolist() if pose is not None else None
        obs = [oid for row in snapshot_objects for oid in row.get("observation_ids", [])]
        # Prefix guard is part of the artifact, not just a test assertion.
        if any(_observation_frame(oid) > int(frame_index) for oid in obs):
            raise ValueError(f"future observation leaked into frame {frame_index}")
        room_ref = self.room_revision_by_frame.get(int(frame_index))
        room_status = ("prefix_state_only_native_replay_reference" if room_ref else
                       "awaiting_causal_room_revision" if self.publisher else "fixed_final_room_map")
        snapshot = {
            "schema": self.schema, "scene_id": self.scene_id, "sequence_id": self.sequence_id,
            "revision": revision, "frame_index": int(frame_index), "frame_id": str(frame_id),
            "timestamp_s": float(timestamp_s), "stage": "online_hypotheses",
            "objects": snapshot_objects, "camera_pose_W_Copt": pose_value,
            "observation_ids": sorted(set(obs)), "future_observations_used": False,
            "prefix_contract": "only observations with frame_index <= this frame",
            "rooms": {"status": room_status, "causal": bool(room_ref),
                      "state_ref": room_ref,
                      "reason": ("existing native replay room delta; room object IDs are not ConceptGraphs hybrid IDs"
                                 if room_ref else "awaiting same-frame OccuSG producer event" if self.publisher else
                                 "cached room snapshot is final-only; no room delta was available")},
            "membership": {"status": "unavailable_hybrid_object_membership", "causal": False,
                           "reason": "native room delta membership belongs to source backend, not hybrid object hypotheses"},
            "room_semantics": {"status": "unavailable", "causal": False},
        }
        snapshot_path = self.snapshots / f"rev-{revision:06d}.json"
        atomic_write_bytes(snapshot_path, _json(snapshot).encode("utf-8"))
        current_ids = {row["hypothesis_id"] for row in snapshot_objects}
        self.deltas.append({"schema": self.schema, "kind": "replace_snapshot",
                            "base_revision": revision - 1, "new_revision": revision,
                            "frame_index": int(frame_index), "frame_id": str(frame_id),
                            "snapshot": str(snapshot_path.relative_to(self.root)),
                            "added_or_updated_hypotheses": sorted(current_ids),
                            "removed_revision_local_hypotheses": sorted(self.previous_hypotheses - current_ids)})
        self.rows.append({"revision": revision, "frame_index": int(frame_index),
                          "frame_id": str(frame_id), "snapshot": str(snapshot_path.relative_to(self.root)),
                          "object_count": len(snapshot_objects), "observation_count": len(set(obs)),
                          "snapshot_sha256": _sha(snapshot_path)})
        self.previous_hypotheses = current_ids
        self._last_frame = int(frame_index)
        if self.publisher:
            relative_assets = {
                path.relative_to(self.live_publish_root).as_posix(): _sha(path)
                for path in (self.root / row["cloud_npz"] for row in snapshot_objects)
            }
            self.publisher.publish({
                "schema": "scene-graph-live-branch-event/v1",
                "scene_id": self.scene_id, "sequence_id": self.sequence_id,
                "source_frame_index": int(frame_index), "source_frame_id": str(frame_id),
                "source_timestamp_s": float(timestamp_s), "source_revision": revision,
                "revision_kind": "conceptgraphs_online_hypotheses",
                "processing_ms": processing_ms,
            }, state_path=snapshot_path, referenced_files=relative_assets)

    def finalize(self, *, final_lineage_path: Path | None, final_export_root: Path | None,
                 peak_rss_mib: float | None = None) -> dict[str, Any]:
        delta_path = self.root / "delta_log.json"
        delta_path.write_text(_json({"schema": self.schema, "kind": "delta_log",
                                     "deltas": self.deltas}), encoding="utf-8")
        index_path = self.root / "frame_index.json"
        index_path.write_text(_json({"schema": self.schema, "frames": self.rows}), encoding="utf-8")
        manifest = {
            "schema": self.schema, "status": "complete", "scene_id": self.scene_id,
            "sequence_id": self.sequence_id, "frame_count": len(self.rows),
            "revision_count": len(self.rows), "input_hashes": self.input_hashes,
            "config_hash": self.config_hash, "seed": self.seed,
            "runner_sha256": self.runner_sha256, "cache_root": str(self.cache_root),
            "mapping_output": str(self.mapping_output),
            "causality": {"objects": "prefix_state_only",
                          "rooms": ("prefix_state_only_native_replay_reference" if self.room_revision_by_frame else "fixed_final_room_map"),
                          "membership": "not_causal_for_hybrid_objects",
                          "room_semantics": "unavailable"},
            "stage_semantics": {
                "online_hypotheses": "reference association/fusion after each frame, before final denoise/filter/merge",
                "prefix_admission": "not separately exposed by the reference mapper; unavailable",
                "final_postprocess": "not inserted into prefix snapshots; see locked final lineage artifact"},
            "room_snapshot": (str(self.room_snapshot) if self.room_snapshot else None),
            "room_snapshot_sha256": (_sha(self.room_snapshot) if self.room_snapshot and self.room_snapshot.is_file() else None),
            "room_delta_source": (str((self.room_snapshot.parent / "delta_log.json").resolve())
                                  if self.room_revision_by_frame else None),
            "room_revision_files": self.room_revision_by_frame,
            "final_lineage_reference": str(final_lineage_path.resolve()) if final_lineage_path else None,
            "final_export_reference": str(final_export_root.resolve()) if final_export_root else None,
            "snapshot_index": str(index_path.relative_to(self.root)),
            "delta_log": str(delta_path.relative_to(self.root)),
            "recorder_runtime_s": time.perf_counter() - self.started,
            "peak_rss_mib": peak_rss_mib,
            "storage_bytes": sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file()),
            "read_only": True,
        }
        (self.root / "manifest.json").write_text(_json(manifest), encoding="utf-8")
        return manifest


def _observation_frame(observation_id: str) -> int:
    # Cache IDs use hm3d-...-NNNN:det-NNNN.  Reject unknown IDs rather than
    # silently allowing a possible future observation into a prefix snapshot.
    try:
        left = str(observation_id).split(":det-", 1)[0]
        return int(left.rsplit("-", 1)[1])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"cannot determine frame index from observation ID {observation_id!r}") from exc


def replay_timeline(root: Path) -> list[dict[str, Any]]:
    """Reload snapshots through the delta log and return their metadata."""
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    log = json.loads((root / manifest["delta_log"]).read_text())
    expected = 0; rows = []
    for delta in log["deltas"]:
        if delta["base_revision"] != expected or delta["new_revision"] != expected + 1:
            raise ValueError("timeline delta revision chain is discontinuous")
        snap_path = root / delta["snapshot"]
        snap = json.loads(snap_path.read_text())
        if snap["revision"] != delta["new_revision"]:
            raise ValueError("delta snapshot revision mismatch")
        rows.append(snap); expected += 1
    if expected != manifest["revision_count"]:
        raise ValueError("timeline revision count mismatch")
    return rows

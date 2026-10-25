"""Strict clock/provenance contracts shared by the camera-live runtime.

This module is intentionally model-free.  It is imported by the Habitat
orchestrator and exercised by IPC contract tests; it never manufactures
timestamps or source hashes.
"""
from __future__ import annotations

import math
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any


SIMULATION_CLOCK = "habitat_simulation"


class CameraLiveContractError(ValueError):
    pass


def host_monotonic_domain() -> str:
    from pathlib import Path
    boot_id_path = Path("/proc/sys/kernel/random/boot_id")
    if not boot_id_path.is_file():
        raise CameraLiveContractError("cannot identify host monotonic clock domain")
    boot_id = boot_id_path.read_text(encoding="ascii").strip()
    if not boot_id:
        raise CameraLiveContractError("host monotonic clock boot ID is empty")
    return f"host_monotonic:{boot_id}"


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise CameraLiveContractError(f"{field} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise CameraLiveContractError(f"{field} must be a finite number") from exc
    if not math.isfinite(result):
        raise CameraLiveContractError(f"{field} must be a finite number")
    return result


def validate_clock_fields(record: Mapping[str, Any], *, context: str) -> dict[str, Any]:
    """Require source simulation, wall, and monotonic clocks without fallback."""
    required = (
        "timestamp_s", "timestamp_clock_domain", "capture_wall_time_unix_s",
        "capture_monotonic_s", "capture_monotonic_domain",
    )
    missing = [field for field in required if field not in record]
    if missing:
        raise CameraLiveContractError(f"{context} missing source clock fields: {', '.join(missing)}")
    if record["timestamp_clock_domain"] != SIMULATION_CLOCK:
        raise CameraLiveContractError(
            f"{context} timestamp_clock_domain must be {SIMULATION_CLOCK!r}"
        )
    domain = record["capture_monotonic_domain"]
    if not isinstance(domain, str) or not domain.startswith("host_monotonic:"):
        raise CameraLiveContractError(f"{context} capture_monotonic_domain is not a host monotonic clock ID")
    return {
        "timestamp_s": _finite_number(record["timestamp_s"], f"{context}.timestamp_s"),
        "timestamp_clock_domain": SIMULATION_CLOCK,
        "capture_wall_time_unix_s": _finite_number(
            record["capture_wall_time_unix_s"], f"{context}.capture_wall_time_unix_s"
        ),
        "capture_monotonic_s": _finite_number(
            record["capture_monotonic_s"], f"{context}.capture_monotonic_s"
        ),
        "capture_monotonic_domain": domain,
    }


def validate_source_hashes(value: Any, *, context: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise CameraLiveContractError(f"{context} requires non-empty source_hashes")
    result: dict[str, str] = {}
    for name, digest in value.items():
        if not isinstance(name, str) or not name:
            raise CameraLiveContractError(f"{context} has an invalid source hash name")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
            raise CameraLiveContractError(f"{context} has an invalid SHA-256 for {name}")
        result[name] = digest.lower()
    return result


def validate_frame_identity(packet: Mapping[str, Any], *, context: str) -> dict[str, Any]:
    for field in ("frame_id", "frame_index"):
        if field not in packet:
            raise CameraLiveContractError(f"{context} missing {field}")
    frame_id = packet["frame_id"]
    if not isinstance(frame_id, str) or not frame_id:
        raise CameraLiveContractError(f"{context}.frame_id must be a non-empty string")
    index = packet["frame_index"]
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise CameraLiveContractError(f"{context}.frame_index must be a non-negative integer")
    return {"frame_id": frame_id, "frame_index": index}


def validate_current_source_packet(packet: Mapping[str, Any], *, context: str,
                                   verify_files: bool = True) -> dict[str, Any]:
    identity = validate_frame_identity(packet, context=context)
    clocks = validate_clock_fields(packet, context=context)
    hashes = validate_source_hashes(packet.get("source_hashes"), context=context)
    capture_record = packet.get("capture_record")
    if not isinstance(capture_record, Mapping):
        raise CameraLiveContractError(f"{context} missing capture_record")
    if str(capture_record.get("frame_id")) != identity["frame_id"]:
        raise CameraLiveContractError(f"{context} capture_record frame_id mismatch")
    if capture_record.get("frame_index") != identity["frame_index"]:
        raise CameraLiveContractError(f"{context} capture_record frame_index mismatch")
    for field, expected in clocks.items():
        if capture_record.get(field) != expected:
            raise CameraLiveContractError(f"{context} capture_record {field} mismatch")
    raw_hashes = validate_source_hashes(capture_record.get("raw_files_sha256"),
                                        context=f"{context}.capture_record")
    if raw_hashes != hashes:
        raise CameraLiveContractError(f"{context} capture_record source hashes mismatch")
    if verify_files:
        capture_root = packet.get("capture_root")
        if not capture_root:
            raise CameraLiveContractError(f"{context} missing capture_root")
        frame_dir = __import__("pathlib").Path(capture_root) / str(capture_record.get("frame_dir", ""))
        validate_event_file_hashes(frame_dir, raw_hashes)
    return {**identity, **clocks, "source_hashes": hashes}


def frame_stage_input(packet: Mapping[str, Any], payload: Mapping[str, Any], *, stage: str) -> dict[str, Any]:
    """Create the actual session input while preserving source clocks verbatim."""
    identity = validate_frame_identity(packet, context=stage)
    clocks = validate_clock_fields(packet, context=stage)
    hashes = validate_source_hashes(packet.get("source_hashes"), context=stage)
    return {**identity, **clocks, **dict(payload), "source_hashes": hashes}


def validate_stage_response(source_packet: Mapping[str, Any], response: Mapping[str, Any], *, stage: str) -> None:
    identity = validate_frame_identity(source_packet, context=f"{stage} input")
    clocks = validate_clock_fields(source_packet, context=f"{stage} input")
    hashes = validate_source_hashes(source_packet.get("source_hashes"), context=f"{stage} input")
    for field, expected in identity.items():
        if response.get(field) != expected:
            raise CameraLiveContractError(f"{stage} response {field} mismatch")
    for field, expected in clocks.items():
        if response.get(field) != expected:
            raise CameraLiveContractError(f"{stage} response {field} mismatch")
    if validate_source_hashes(response.get("source_hashes"), context=f"{stage} response") != hashes:
        raise CameraLiveContractError(f"{stage} response source_hashes mismatch")


def validate_observation_populations(p2: Sequence[Mapping[str, Any]],
                                     p3: Sequence[Mapping[str, Any]],
                                     features: Sequence[Mapping[str, Any]]) -> None:
    p2_ids = [str(row["detection_id"]) for row in p2]
    if len(p2_ids) != len(set(p2_ids)):
        raise CameraLiveContractError("P2 contains duplicate observation IDs")
    p3_ids = [str(row["detection_id"]) for row in p3]
    if len(p3_ids) != len(set(p3_ids)) or set(p3_ids) != set(p2_ids):
        raise CameraLiveContractError("P3 output must report each P2 observation exactly once, including rejected rows")
    feature_ids = [str(row["observation_id"]) for row in features]
    if len(feature_ids) != len(set(feature_ids)) or set(feature_ids) != set(p2_ids):
        raise CameraLiveContractError("feature output must cover every P2 observation exactly once")


def build_joined_perception_packet(base: Mapping[str, Any], p2_response: Mapping[str, Any],
                                   p3_response: Mapping[str, Any],
                                   feature_response: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and join stage outputs used by the live coordinator."""
    validate_stage_response(base, p2_response, stage="P2")
    validate_stage_response(base, p3_response, stage="P3")
    validate_stage_response(base, feature_response, stage="OpenCLIP")
    p2 = p2_response.get("detections")
    p3 = p3_response.get("detections")
    features = feature_response.get("rows")
    if not all(isinstance(rows, list) for rows in (p2, p3, features)):
        raise CameraLiveContractError("perception stage outputs must contain row lists")
    validate_observation_populations(p2, p3, features)
    return {**dict(base), "p2_detections": p2, "p3_detections": p3,
            "features": features,
            "perception_status": "empty_detections" if not p2 else "completed"}


def validate_mapper_pair_events(packet: Mapping[str, Any], object_event: Mapping[str, Any],
                                room_event: Mapping[str, Any]) -> None:
    source = validate_current_source_packet(packet, context="mapper pair source", verify_files=False)
    expected = {"source_frame_index": source["frame_index"],
                "source_frame_id": source["frame_id"],
                "source_timestamp_s": source["timestamp_s"],
                "source_timestamp_clock_domain": source["timestamp_clock_domain"],
                "source_capture_wall_time_unix_s": source["capture_wall_time_unix_s"],
                "source_capture_monotonic_s": source["capture_monotonic_s"],
                "source_capture_monotonic_domain": source["capture_monotonic_domain"],
                "source_hashes": source["source_hashes"]}
    for branch, event in (("ConceptGraphs", object_event), ("OccuSG", room_event)):
        for field, value in expected.items():
            if event.get(field) != value:
                raise CameraLiveContractError(f"{branch} branch event {field} differs from dispatched frame")


def send_mapper_pair(send_fn, workers: Sequence[Any], message: Mapping[str, Any]) -> None:
    """Enqueue the identical immutable packet to both mapper workers first."""
    if len(workers) != 2:
        raise CameraLiveContractError("exactly two persistent mapper workers are required")
    immutable = dict(message)
    send_fn(workers[0], immutable)
    send_fn(workers[1], immutable)


def evaluate_live_overlap(capture_finished_monotonic_s: Any, capture_clock_domain: Any,
                          perception_starts: Sequence[Mapping[str, Any]],
                          unified_commits: Sequence[Mapping[str, Any]],
                          captured_frames: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Compare only timestamps from the same monotonic host clock domain."""
    capture_end = _finite_number(capture_finished_monotonic_s, "capture_finished_monotonic_s")
    if not isinstance(capture_clock_domain, str) or not capture_clock_domain.startswith("host_monotonic:"):
        raise CameraLiveContractError("capture end must identify a host monotonic clock domain")
    perception = min(perception_starts, key=lambda row: _finite_number(
        row.get("started_monotonic_s"), "perception.started_monotonic_s"), default=None)
    commit = min(unified_commits, key=lambda row: _finite_number(
        row.get("committed_monotonic_s"), "unified.committed_monotonic_s"), default=None)
    p_same = bool(perception and perception.get("clock_domain") == capture_clock_domain)
    c_same = bool(commit and commit.get("clock_domain") == capture_clock_domain)
    p_started = _finite_number(perception["started_monotonic_s"], "perception.started_monotonic_s") if p_same else None
    c_committed = _finite_number(commit["committed_monotonic_s"], "unified.committed_monotonic_s") if c_same else None
    p2_starts = [row for row in perception_starts if row.get("stage") == "P2"]
    first_p2 = min(p2_starts, key=lambda row: _finite_number(
        row.get("started_monotonic_s"), "P2.started_monotonic_s"), default=None)
    matching_capture = next((row for row in captured_frames
        if first_p2 and row.get("frame_id") == first_p2.get("frame_id")), None)
    capture_to_p2_s = None
    if (matching_capture and first_p2
            and matching_capture.get("clock_domain") == first_p2.get("clock_domain") == capture_clock_domain):
        capture_to_p2_s = (_finite_number(first_p2["started_monotonic_s"], "P2.started_monotonic_s")
                           - _finite_number(matching_capture["captured_monotonic_s"], "frame.captured_monotonic_s"))
    perception_to_commit_s = None
    matching_commit = next((row for row in unified_commits
        if perception and row.get("frame_id") == perception.get("frame_id")), None)
    if (matching_commit and perception
            and matching_commit.get("clock_domain") == perception.get("clock_domain") == capture_clock_domain):
        perception_to_commit_s = (_finite_number(matching_commit["committed_monotonic_s"], "unified.committed_monotonic_s")
                                  - _finite_number(perception["started_monotonic_s"], "perception.started_monotonic_s"))
    return {"capture_finished_monotonic_s": capture_end, "capture_clock_domain": capture_clock_domain,
            "first_perception_start": perception,
            "first_unified_commit": commit,
            "perception_started_before_capture_end": bool(p_same and p_started < capture_end),
            "unified_revision_committed_before_capture_end": bool(c_same and c_committed < capture_end),
            "first_keyframe_capture_to_p2_start_s": capture_to_p2_s,
            "first_perception_start_to_same_frame_unified_commit_s": perception_to_commit_s,
            "latency_comparison_clock_domain": capture_clock_domain if p_same and c_same else None,
            "simulation_time_compared_to_wall_time": False}


def validate_event_file_hashes(frame_dir: Any, declared_hashes: Mapping[str, str]) -> None:
    """Reject altered raw keyframe files before any perception session call."""
    from pathlib import Path
    import hashlib

    root = Path(frame_dir)
    for name, expected in declared_hashes.items():
        path = root / str(name)
        if not path.is_file():
            raise CameraLiveContractError(f"published keyframe file missing: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != str(expected).lower():
            raise CameraLiveContractError(f"published keyframe source hash mismatch: {name}")


def validate_published_event(event: Mapping[str, Any]) -> None:
    if event.get("schema") != "camera-live-keyframe-event/v1":
        raise CameraLiveContractError("unsupported published keyframe event schema")
    packet = event.get("packet")
    if not isinstance(packet, Mapping) or not isinstance(packet.get("metadata"), Mapping):
        raise CameraLiveContractError("published keyframe event has no source packet metadata")
    metadata = packet["metadata"]
    clocks = validate_clock_fields(metadata, context="published keyframe event")
    if str(packet.get("frame_id")) != str(metadata.get("frame_id")):
        raise CameraLiveContractError("published event frame_id differs from source metadata")
    published_domain = event.get("published_monotonic_domain")
    if published_domain != clocks["capture_monotonic_domain"]:
        raise CameraLiveContractError("published event monotonic clock domain differs from capture")
    _finite_number(event.get("published_monotonic_s"), "published_monotonic_s")
    _finite_number(event.get("published_wall_time_unix_s"), "published_wall_time_unix_s")
    result = event.get("result")
    if not isinstance(result, Mapping) or result.get("frame_id") != packet.get("frame_id"):
        raise CameraLiveContractError("published event result identity mismatch")


def read_complete_jsonl(path: Any, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Read only newline-terminated journal records; leave a partial tail pending."""
    import json
    from pathlib import Path
    target = Path(path)
    if not target.is_file():
        return [], offset
    with target.open("rb") as stream:
        stream.seek(offset)
        payload = stream.read()
    last_newline = payload.rfind(b"\n")
    if last_newline < 0:
        return [], offset
    complete = payload[:last_newline + 1]
    rows = [json.loads(line) for line in complete.splitlines() if line.strip()]
    return rows, offset + len(complete)


def assert_no_incomplete_jsonl_tail(path: Any, offset: int) -> None:
    from pathlib import Path
    target = Path(path)
    size = target.stat().st_size if target.is_file() else 0
    if size != offset:
        raise CameraLiveContractError(
            f"journal ended with an incomplete record: consumed={offset} bytes, file_size={size}"
        )


class BoundedPublishedKeyframeQueue:
    """One in-flight consumer plus one replaceable, pre-dispatch packet.

    Replaced packets are explicitly logged as superseded before mapper
    dispatch. Once ``get`` returns a packet, it is immutable and must proceed
    to both mapper branches or fail the run.
    """

    def __init__(self, emit):
        self._emit = emit
        self._condition = threading.Condition()
        self._pending = None
        self._closed = False
        self._failure = None
        self._seen: set[str] = set()
        self.queued = self.admitted = self.superseded = self.processed = 0

    @staticmethod
    def _frame_id(event):
        try:
            return str(event["packet"]["frame_id"])
        except (KeyError, TypeError) as exc:
            raise CameraLiveContractError("published keyframe event lacks frame identity") from exc

    def offer(self, event: dict[str, Any]) -> None:
        fid = self._frame_id(event)
        with self._condition:
            if self._closed:
                raise RuntimeError("published keyframe queue is closed")
            if fid in self._seen:
                raise CameraLiveContractError(f"duplicate published keyframe event: {fid}")
            self._seen.add(fid)
            replaced = self._pending
            self._pending = event
            try:
                if replaced is not None:
                    old_id = self._frame_id(replaced)
                    self.superseded += 1
                    self._emit({"event": "superseded", "frame_id": old_id,
                                "replacement_frame_id": fid,
                                "reason": "latest_wins_before_processing_dispatch"})
                self.queued += 1
                self._emit({"event": "queued", "frame_id": fid,
                            "queue_depth": 1, "max_pending": 1})
            except BaseException as exc:
                self._failure = exc
                self._pending = None
                self._closed = True
                self._condition.notify_all()
                raise
            self._condition.notify_all()

    def get(self, timeout_s: float = 0.1):
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._condition:
            if self._failure is not None:
                raise RuntimeError("published keyframe queue logging failed") from self._failure
            while self._pending is None and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            if self._pending is None:
                return None
            event, self._pending = self._pending, None
            self.admitted += 1
            try:
                self._emit({"event": "admitted_to_processing", "frame_id": self._frame_id(event),
                            "queue_depth": 0, "max_pending": 1})
            except BaseException as exc:
                self._failure = exc
                self._closed = True
                self._condition.notify_all()
                raise RuntimeError("published keyframe admission logging failed") from exc
            return event

    def mark_processed(self, frame_id: str) -> None:
        with self._condition:
            if frame_id not in self._seen:
                raise CameraLiveContractError(f"cannot mark unknown keyframe processed: {frame_id}")
            try:
                self.processed += 1
                self._emit({"event": "processed", "frame_id": str(frame_id),
                            "processed_count": self.processed})
            except BaseException as exc:
                self._failure = exc
                self._closed = True
                self._condition.notify_all()
                raise RuntimeError("published keyframe processed logging failed") from exc

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    @property
    def has_pending(self) -> bool:
        with self._condition:
            return self._pending is not None

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from standalone_sg.contracts.objects import Detection3D, DetectionBatch
from standalone_sg.objects.u5_backends import GeometryOnlyDuplicateV2AssociationBackend
from standalone_sg.pipeline import FramePacket, SceneGraphBuilder


ROOT = Path(__file__).resolve().parents[1]
LOCKED = json.loads((ROOT / "configs/association/u3_same_frame_duplicate_candidate_v2.json").read_text())
DUPLICATE_CONFIG = LOCKED["candidate"]
GEOMETRY_CONFIG = LOCKED["baseline_config"]["geometry_only"]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class U5DuplicateV2BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.mask_dir = Path(self.temp.name)

    def detection(self, did: str, mask: np.ndarray, *, cls="chair", x=0.0,
                  support_missing=False) -> Detection3D:
        path = self.mask_dir / f"{did}.npy"
        np.save(path, mask.astype(bool), allow_pickle=False)
        metadata = {
            "u5_duplicate_v2_mask_path": str(path),
            "u5_duplicate_v2_mask_sha256": sha(path),
        }
        if not support_missing:
            metadata.update({
                "u5_duplicate_v2_valid_depth_pixels": int(mask.sum()),
                "u5_duplicate_v2_cluster_points": max(100, int(mask.sum())),
                "u5_duplicate_v2_detector_score": 0.8,
            })
        return Detection3D(
            detection_id=did, timestamp_s=0.0, center_W=(x, 0.0, 0.0),
            center_method="fixture", class_name=cls, class_id=cls, confidence=0.8,
            frame_id="world", layer_id="floor_0", extents_m=(1.0, 1.0, 1.0),
            provider_metadata=metadata,
        )

    def backend(self):
        return GeometryOnlyDuplicateV2AssociationBackend(
            duplicate_config=DUPLICATE_CONFIG,
            max_center_m=GEOMETRY_CONFIG["candidate_center_radius_m"],
            geom_min_iou=GEOMETRY_CONFIG["appearance_iou_min"],
            geom_only_min_iou=GEOMETRY_CONFIG["geometry_only_iou_min"],
            ambiguity_margin=0.05,
        )

    def test_alias_provenance_is_observation_local_and_not_graph_track_merge(self):
        mask = np.ones((16, 16), dtype=bool)
        detections = (self.detection("a", mask, cls="chair"),
                      self.detection("b", mask, cls="table"))
        backend = self.backend()
        result = backend.update(DetectionBatch("p3", "frame-0", 0.0, detections))

        self.assertEqual(len(result.objects), 1)
        self.assertEqual(backend.last_duplicate_aliases[0]["duplicate_of"], "a")
        alias_event = next(row for row in backend.last_lifecycle_events if row["event"] == "observation_alias")
        self.assertEqual(alias_event["event_scope"], "same_frame_observation")
        self.assertFalse(alias_event["graph_track_merge"])
        self.assertFalse(alias_event["counts_as_independent_view"])
        self.assertEqual(alias_event["semantic_vote_count"], 0)
        self.assertEqual([event.kind for event in result.lifecycle_events], ["create"])
        self.assertFalse(any(event.kind in {"canonical_alias", "merge"} for event in result.lifecycle_events))

    def test_ambiguous_is_passed_to_geometry_backend_not_automatically_deferred(self):
        small = np.zeros((20, 20), dtype=bool)
        small[:10, :10] = True
        large = small.copy()
        large[10:20, :10] = True
        detections = (self.detection("small", small), self.detection("large", large, cls="table"))
        backend = self.backend()
        result = backend.update(DetectionBatch("p3", "frame-0", 0.0, detections))

        self.assertEqual(backend.last_ambiguous_detection_ids, ("large", "small"))
        self.assertEqual(backend.last_duplicate_aliases, ())
        self.assertEqual(len(backend.last_decisions), 2)
        self.assertTrue(all(row["decision"] == "created" for row in backend.last_decisions))
        self.assertEqual(len(result.objects), 2)
        self.assertEqual(backend.last_deferred_detection_ids, ())

    def test_missing_p3_support_blocks_alias_but_keeps_observation_for_association(self):
        mask = np.ones((12, 12), dtype=bool)
        detections = (self.detection("a", mask, support_missing=True),
                      self.detection("b", mask, support_missing=True))
        backend = self.backend()
        backend.update(DetectionBatch("p3", "frame-0", 0.0, detections))
        self.assertEqual(backend.last_duplicate_aliases, ())
        self.assertTrue(backend.last_ambiguous_detection_ids)
        self.assertEqual(len(backend.last_decisions), 2)

    def test_m9_builder_snapshot_contains_no_alias_track_event(self):
        mask = np.ones((16, 16), dtype=bool)
        detections = (self.detection("a", mask), self.detection("b", mask, cls="table"))
        backend = self.backend()
        builder = SceneGraphBuilder(objects=backend, config_hash="duplicate-v2-fixture")
        packet = FramePacket("scene", "seq", "frame-0", 0, 0.0, "regions",
                             regions=(), detections=DetectionBatch("p3", "frame-0", 0.0, detections))
        snapshot, delta = builder.update(packet)
        self.assertEqual(len([node for node in snapshot.nodes if node.kind == "object"]), 1)
        self.assertFalse(any("canonical_alias" in event or "merge" in event for event in delta.events))
        self.assertTrue(any(row["event"] == "observation_alias" for row in backend.last_lifecycle_events))

    def test_mask_hash_failure_after_mutation_requires_replay_and_keeps_committed_snapshot(self):
        from standalone_sg.contracts.errors import ContractError

        backend = self.backend()
        builder = SceneGraphBuilder(objects=backend, config_hash="duplicate-v2-failure-fixture")
        mask = np.ones((16, 16), dtype=bool)
        first = self.detection("first", mask)
        packet0 = FramePacket(
            "scene", "seq", "frame-0", 0, 0.0, "regions", regions=(),
            detections=DetectionBatch("p3", "frame-0", 0.0, (first,)),
        )
        committed, _ = builder.update(packet0)

        second = self.detection("second", mask)
        mask_path = Path(second.provider_metadata["u5_duplicate_v2_mask_path"])
        np.save(mask_path, np.zeros_like(mask), allow_pickle=False)
        packet1 = FramePacket(
            "scene", "seq", "frame-1", 1, 1.0, "regions", regions=(),
            detections=DetectionBatch("p3", "frame-1", 1.0, (second,)),
        )
        with self.assertRaisesRegex(ValueError, "mask.*hash|hash.*mask"):
            builder.update(packet1)

        self.assertTrue(builder.failed)
        self.assertTrue(builder.replay_required)
        self.assertEqual(builder.snapshot(), committed)
        with self.assertRaisesRegex(ContractError, "replay required"):
            builder.update(packet1)


if __name__ == "__main__":
    unittest.main()

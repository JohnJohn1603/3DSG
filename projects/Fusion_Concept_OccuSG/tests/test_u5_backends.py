import unittest

from standalone_sg.config.schema import ObjectAssociationConfig
from standalone_sg.contracts.objects import Detection3D, DetectionBatch
from standalone_sg.objects.association import ObjectAssociationManager
from standalone_sg.objects.u5_backends import GeometryOnlyAssociationBackend, SourceAssociationBackend
from standalone_sg.pipeline import FramePacket, SceneGraphBuilder


def detection(did, frame, t, cls, x):
    return Detection3D(detection_id=did, timestamp_s=t, center_W=(x, 0.0, 0.0),
        center_method="fixture", class_name=cls, class_id=cls, confidence=0.8,
        frame_id="world", extents_m=(1.0, 1.0, 1.0))


class U5BackendTests(unittest.TestCase):
    def test_source_wrapper_is_transparent_for_update_and_snapshot(self):
        config = ObjectAssociationConfig(0.75, 0.15)
        raw = ObjectAssociationManager(config)
        wrapped = SourceAssociationBackend(ObjectAssociationManager(config))
        batches = [
            DetectionBatch("p3", "f0", 0.0, (detection("d0", "f0", 0.0, "chair", 0.0),)),
            DetectionBatch("p3", "f1", 1.0, (detection("d1", "f1", 1.0, "chair", 0.2),)),
            DetectionBatch("p3", "f2", 2.0, ()),
        ]
        for batch in batches:
            self.assertEqual(raw.update(batch), wrapped.update(batch))
            self.assertEqual(raw.snapshot(), wrapped.snapshot())

    def test_source_manager_and_wrapper_graph_and_deltas_match(self):
        def run(objects):
            builder = SceneGraphBuilder(objects=objects, config_hash="source-parity")
            output = []
            for i, cls, x in ((0, "chair", 0.0), (1, "chair", 0.2), (2, "table", 0.2)):
                batch = DetectionBatch("p3", f"f{i}", float(i),
                    (detection(f"d{i}", f"f{i}", float(i), cls, x),))
                packet = FramePacket("scene", "seq", f"f{i}", i, float(i), "regions",
                    regions=(), detections=batch)
                output.append(builder.update(packet))
            output.append(builder.flush(3.0))
            return output

        direct = run(ObjectAssociationManager(ObjectAssociationConfig(0.75, 0.15)))
        wrapped = run(SourceAssociationBackend(ObjectAssociationManager(ObjectAssociationConfig(0.75, 0.15))))
        self.assertEqual(direct, wrapped)

    def test_geometry_backend_associates_cross_class_by_geometry_and_is_causal(self):
        backend = GeometryOnlyAssociationBackend(max_center_m=0.75, geom_min_iou=0.01,
            geom_only_min_iou=0.05, ambiguity_margin=0.05)
        first = detection("d0", "f0", 0.0, "chair", 0.0)
        second = detection("d1", "f1", 1.0, "piano", 0.05)
        result0 = backend.update(DetectionBatch("p3", "f0", 0.0, (first,)))
        result1 = backend.update(DetectionBatch("p3", "f1", 1.0, (second,)))
        self.assertEqual(result0.new_object_ids, ("u3-0001",))
        self.assertEqual(result1.updated_object_ids, ("u3-0001",))
        self.assertEqual(result1.associations[0].decision, "geometry_match")
        self.assertEqual(backend.snapshot()[0].class_name, "chair")
        self.assertEqual(backend.snapshot()[0].last_detection_id, "d1")
        with self.assertRaises(ValueError):
            backend.update(DetectionBatch("p3", "f0-late", 0.5, ()))

    def test_deferred_geometry_match_does_not_fabricate_graph_node(self):
        backend = GeometryOnlyAssociationBackend(max_center_m=0.75, geom_min_iou=0.01,
            geom_only_min_iou=0.05, ambiguity_margin=0.05)
        backend.update(DetectionBatch("p3", "f0", 0.0, (
            detection("d0", "f0", 0.0, "chair", -0.1),
            detection("d1", "f0", 0.0, "table", 0.1),
        )))
        before = len(backend.snapshot())
        result = backend.update(DetectionBatch("p3", "f1", 1.0,
            (detection("d2", "f1", 1.0, "lamp", 0.0),)))
        self.assertEqual(len(backend.snapshot()), before)
        self.assertEqual(result.associations, ())
        self.assertEqual(backend.last_deferred_detection_ids, ("d2",))
        self.assertEqual(backend.last_lifecycle_events[0]["event"], "deferred")


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from scripts.u5_experimental import DEFAULT_RUN, build_sidecar, verify_inputs


class U5ExperimentalSidecarTests(unittest.TestCase):
    def test_locked_cached_pilot_has_verified_provenance_and_causal_sidecar(self):
        run = DEFAULT_RUN.resolve()
        input_hashes = verify_inputs(run)
        self.assertEqual(input_hashes["observation_count"], 30)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            output.mkdir()
            result = build_sidecar(run, output, "source", input_hashes)
            self.assertEqual(result["track_count"], 12)
            self.assertFalse(result["raw_fallback_used"])
            self.assertFalse(result["association_applied_to_graph"])
            self.assertEqual(len(result["observations"]), 30)
            self.assertTrue(all(row["raw_detector_class"] is not None for row in result["observations"]))
            self.assertTrue(all(row["future_observations_used"] is False for row in result["observations"]))
            self.assertTrue(all(row["config_sha256"] == input_hashes["u4ov_config_sha256"] for row in result["observations"]))
            for row in result["observations"]:
                if row["fused_semantic_status"] in ("unknown", "conflicted"):
                    self.assertIsNone(row["fused_semantic_label"])
                self.assertLessEqual(row["evidence_end_frame_index"], row["frame_index"])
            stored = json.loads((output / "u5_sidecar.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["profile"], "source")
            self.assertTrue((output / "index.html").is_file())


if __name__ == "__main__":
    unittest.main()

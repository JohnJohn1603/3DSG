#!/usr/bin/env python3
"""Score locked cached U4/U4-OV semantic predictions on confirmed labels."""
from __future__ import annotations
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/audits/object_graph_improvement/q4_semantic_baseline_v1_20261012"
sys.path.insert(0, str(ROOT / "scripts"))
from q4_semantic_evaluation import score  # noqa: E402


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
                             for k, v in row.items()})


def main() -> int:
    result = score(ROOT, OUT)
    timeline = result.pop("observation_timeline_rows")
    write_csv(OUT / "q4_observation_timeline.csv", timeline)
    group_rows = result["identity_conditioned_final_object_diagnostic"]["groups"]
    write_csv(OUT / "q4_identity_group_diagnostic.csv", group_rows)
    track_rows = []
    for mode, payload in result["predicted_track_final_map"].items():
        for row in payload["tracks"]:
            track_rows.append({"assignment_mode": mode, **row})
    write_csv(OUT / "q4_predicted_track_final_map.csv", track_rows)
    (OUT / "q4_semantic_baseline_report.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n")
    print(json.dumps({"status": result["status"], "population": result["population"],
        "observation_causal_exact_metrics": result["observation_causal_exact_metrics"],
        "identity_conditioned_final_object_diagnostic": result["identity_conditioned_final_object_diagnostic"]["metrics"],
        "predicted_track_final_map_counts": {m: {k:v for k,v in p.items() if k != "tracks"}
            for m,p in result["predicted_track_final_map"].items()},
        "vocabulary_support": result["vocabulary_support"],
        "report": str(OUT / "q4_semantic_baseline_report.json")}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

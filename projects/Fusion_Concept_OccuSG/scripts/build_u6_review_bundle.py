#!/usr/bin/env python3
"""Build a blinded deterministic primary review bundle for the locked U6 set."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--semantic-dir", type=Path, required=True)
    ap.add_argument("--matches", type=Path, required=True)
    ap.add_argument("--primary-observations", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--seed", default="u6-hm3d-00808-protocol-v1.1-primary-review")
    args = ap.parse_args()
    out = args.output.resolve()
    if out.exists(): raise FileExistsError(out)
    out.mkdir(parents=True)
    run = args.run_dir.resolve(); sem = args.semantic_dir.resolve()
    cap = json.loads((run / "capture/capture_manifest.json").read_text())
    frame = {r["frame_id"]: r for r in cap["frames"]}
    gt = [json.loads(x) for x in args.primary_observations.read_text().splitlines() if x.strip()]
    matches = [json.loads(x) for x in args.matches.read_text().splitlines() if x.strip()]
    matched_index = {(r["frame_id"], r["gt_semantic_id"]): r for r in matches if r.get("gt_matched")}
    groups = defaultdict(list)
    for row in gt: groups[int(row["gt_semantic_id"])].append(row)
    repeat = [sid for sid, rows in groups.items() if len(rows) >= 2]
    single = [sid for sid, rows in groups.items() if len(rows) == 1]
    key = lambda sid: hashlib.sha256(f"{args.seed}|{sid}".encode()).hexdigest()
    selected = [("multi_observation", sid) for sid in sorted(repeat, key=key)[:3]]
    selected += [("single_observation", sid) for sid in sorted(single, key=key)[:3]]

    cards = []; selected_rows = []
    for case_i, (stratum, sid) in enumerate(selected, 1):
        rows = sorted(groups[sid], key=lambda r: (r["frame_index"], r["frame_id"]))
        case = f"U6-R{case_i:02d}"
        imgs = []
        for obs_i, row in enumerate(rows, 1):
            fid = row["frame_id"]; frec = frame[fid]
            rgb_path = run / "capture" / frec["frame_dir"] / "rgb.npy"
            rgb = np.load(rgb_path, allow_pickle=False).astype(np.uint8)
            if rgb.ndim == 3 and rgb.shape[2] == 4: rgb = rgb[:, :, :3]
            sem_path = sem / "frames" / f"{fid}__semantic_id_map.npy"
            semmap = np.load(sem_path, allow_pickle=False)
            mask = semmap == sid
            overlay = rgb.copy()
            color = np.array([235, 40, 160], dtype=np.uint8)
            overlay[mask] = ((overlay[mask].astype(np.uint16) + color.astype(np.uint16)) // 2).astype(np.uint8)
            image_name = f"{case}__{obs_i:02d}.jpg"
            Image.fromarray(overlay).save(out / image_name, quality=92)
            mrow = matched_index.get((fid, sid))
            obs_id = mrow.get("detection_id") if mrow else None
            imgs.append((image_name, fid, obs_id, row["frame_index"]))
            selected_rows.append({"case_id": case, "stratum": stratum, "frame_id": fid,
                                  "frame_index": row["frame_index"], "observation_id": obs_id,
                                  "detector_match_available": obs_id is not None,
                                  "pose_revisit_only": row["frame_index"] == 4,
                                  "review_identity": "unreviewed", "review_class": "unreviewed"})
        image_html = "".join(
            f"<div class='view'><img src='{html.escape(name)}'><p>frame {idx} · {html.escape(obs_id or 'no matched detector observation')}<br><code>{html.escape(fid)}</code>" +
            (" · exact-pose revisit (not an independent view)" if idx == 4 else "") + "</p></div>"
            for name, fid, obs_id, idx in imgs)
        identity_prompt = ("Do the observations show the same physical object? Mark same / different (split per observation) / uncertain."
                           if stratum == "multi_observation" else "One observation only: identity comparison is unavailable; keep identity unreviewed.")
        cards.append(f"<article><h2>{case}</h2><p>{html.escape(identity_prompt)}</p><p>Class question: enter a free-text class for each observation, or mark uncertain. Identity and class are separate.</p><div class='views'>{image_html}</div></article>")

    page = """<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>U6 blinded primary review</title><style>body{font:15px system-ui;margin:20px;background:#f3f4f6}article{background:white;padding:14px;border:1px solid #bbc;border-radius:8px;margin:16px 0}.views{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:10px}.view img{width:100%;height:auto}.view p{font-size:13px}code{overflow-wrap:anywhere}</style><h1>U6 HM3D 00808 — blinded primary review sample</h1><p>Deterministic, candidate-independent sample from the locked primary GT population: up to 3 repeated evaluator IDs and up to 3 single-observation IDs, selected by SHA-256 stratum ordering. Predictions, track IDs, detector classes, evaluator IDs and categories are hidden. Magenta marks one evaluator semantic mask. This package is for evaluator identity/class review only; it does not change predictor inputs. Exact-pose revisit is explicitly marked and is not an independent view.</p>""" + "".join(cards) + "</html>"
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / "review_index.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in selected_rows), encoding="utf-8")
    manifest = {"status": "ready_for_user_review", "seed": args.seed, "selected_case_count": len(selected),
                "selected_gt_instance_count": len(selected), "selected_observation_count": len(selected_rows),
                "selection_strata": {"multi_observation": min(3, len(repeat)), "single_observation": min(3, len(single))},
                "selection_candidate_independent": True, "predicted_labels_or_track_ids_shown": False,
                "primary_observations_sha256": sha256(args.primary_observations.resolve()),
                "match_rows_sha256": sha256(args.matches.resolve()), "render_alignment_manifest_sha256": sha256(sem / "render_validation.json"),
                "selected_semantic_ids_internal_only": [sid for _, sid in selected]}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in manifest.items() if k != "selected_semantic_ids_internal_only"}, indent=2, ensure_ascii=False))


if __name__ == "__main__": main()

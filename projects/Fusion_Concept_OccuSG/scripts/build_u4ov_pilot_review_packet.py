#!/usr/bin/env python3
"""Build a small blinded, observation-keyed human review packet from a locked pilot."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path


OBS = "hm3d-00800-u4ov-mv-pilot-v2-{:04d}:det-{:04d}"
SELECTION = [
    ("A", 0, 3), ("A", 1, 3), ("A", 3, 2), ("A", 4, 3),
    ("B", 2, 1),
    ("C", 0, 5), ("C", 1, 7),
    ("D", 0, 1), ("D", 0, 2), ("D", 1, 1), ("D", 1, 2),
    ("D", 3, 0),
    ("E", 2, 0), ("E", 2, 2), ("E", 3, 3),
    ("E", 2, 3), ("E", 3, 4),
    ("F", 0, 4), ("F", 1, 6), ("F", 3, 5),
]
GROUP_TITLES = {
    "A": "Set A — multi-position sequence and return",
    "B": "Set B — glance from the second camera position",
    "C": "Set C — additional observations",
    "D": "Set D — nearby observations",
    "E": "Set E — nearby observations from frames 2 and 3",
    "F": "Set F — additional nearby observations",
}


class GalleryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.current: dict | None = None
        self.records: dict[str, dict] = {}
        self.capture = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "article":
            self.current = {"images": []}
        elif tag == "img" and self.current is not None:
            self.current["images"].append(a.get("src", ""))
        elif tag in {"h3", "p"} and self.current is not None:
            self.capture = tag

    def handle_endtag(self, tag: str) -> None:
        if tag == "article" and self.current is not None:
            if "observation_id" in self.current:
                self.records[self.current["observation_id"]] = self.current
            self.current = None
        if tag == self.capture:
            self.capture = ""

    def handle_data(self, data: str) -> None:
        if self.current is None or not self.capture:
            return
        value = data.strip()
        if not value:
            return
        if self.capture == "h3":
            match = re.match(r"(hm3d-[^ ]+:det-[0-9]+)\s+·\s+frame\s+(\d+)", value)
            if match:
                self.current["observation_id"] = match.group(1)
                self.current["frame_index"] = int(match.group(2))
        elif "sensor pose W [" in value:
            match = re.search(r"sensor pose W (\[[^]]+\])", value)
            if match:
                self.current["sensor_pose_world"] = json.loads(match.group(1))
        elif "view bin " in value and "P3 center" in value:
            match = re.search(r"view bin ([^;]+); P3 center (\[[^]]+\]); extents (\[[^]]+\])", value)
            if match:
                self.current["view_bin"] = match.group(1)
                self.current["p3_center_world"] = json.loads(match.group(2))
                self.current["p3_extents_m"] = json.loads(match.group(3))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    output_dir = args.output_dir.resolve()
    gallery = run_dir / "u4ov/review/index.html"
    source_html = gallery.read_text(encoding="utf-8")
    html_parser = GalleryParser()
    html_parser.feed(source_html)

    observations = {}
    for line in (run_dir / "u4ov/single_view_predictions.jsonl").read_text().splitlines():
        record = json.loads(line)
        observations[record["observation_id"]] = record

    output_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for group, frame, detection in SELECTION:
        oid = OBS.format(frame, detection)
        if oid not in observations or oid not in html_parser.records:
            raise ValueError(f"selected observation is absent from frozen pilot: {oid}")
        record = html_parser.records[oid]
        if len(record.get("images", [])) != 3:
            raise ValueError(f"expected full-scene, crop and mask assets for {oid}")
        asset_paths = [(gallery.parent / image_path).resolve() for image_path in record["images"]]
        if not all(path.is_file() for path in asset_paths):
            raise FileNotFoundError(f"missing visual asset for {oid}")
        # Copy only the three review images, retaining hashed names from the source gallery.
        asset_dir = output_dir / "assets"
        asset_dir.mkdir(exist_ok=True)
        import shutil
        for path in asset_paths:
            shutil.copy2(path, asset_dir / path.name)
        frame_id = observations[oid]["frame_id"]
        entry = {
            "group": group,
            "observation_id": oid,
            "frame_id": frame_id,
            "frame_index": frame,
            "sensor_pose_world_xyz": record.get("sensor_pose_world"),
            "view_bin_diagnostic": record.get("view_bin"),
            "p3_center_world_diagnostic": record.get("p3_center_world"),
            "p3_extents_m_diagnostic": record.get("p3_extents_m"),
            "images": [f"assets/{path.name}" for path in asset_paths],
        }
        entries.append(entry)

    parts = ["""<!doctype html><html lang="vi"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>HM3D 00800 — blinded observation review</title>
<style>body{font:16px system-ui;max-width:1450px;margin:1.5rem auto;padding:0 1rem;color:#18202a}article{border:1px solid #bbc3cc;border-radius:8px;padding:1rem;margin:1rem 0}h2{margin-top:2rem}.imgs{display:grid;grid-template-columns:2fr 1fr 1fr;gap:12px}figure{margin:0}img{max-width:100%;height:auto;border:1px solid #ccd2d9}figcaption{font-size:.9rem;color:#485260;margin:.3rem 0}.meta{font-family:monospace;overflow-wrap:anywhere}.prompt{background:#f3f6f8;padding:.8rem;border-radius:6px}.small{color:#4c5968;font-size:.92rem}@media(max-width:800px){.imgs{grid-template-columns:1fr}}</style>
<h1>HM3D 00800 — review theo observation</h1>
<p class="prompt"><b>Xin review từng observation độc lập.</b> Các mục được nhóm để xem chuỗi ảnh; nhóm không khẳng định cùng vật. Với mỗi ID, ghi một identity token tùy chọn (dùng lại token nếu bạn xác nhận cùng physical object; token khác nếu khác vật; để trống/uncertain nếu chưa rõ) và class riêng. Identity và class là hai câu trả lời độc lập. Không cần đồng ý với bất kỳ predicted track nào.</p>
<p class="small">Gói này lấy 20 observation từ pilot hiện có; không capture thêm. Ảnh RGB/crop/mask được hiển thị trước. Nhãn detector, nhãn U4-OV, scores và U3 assignments bị ẩn ở trang này để giảm priming. Pose/bin/geometry là thông tin thu thập, không phải identity evidence đã xác nhận.</p>
<h2>Observation review</h2>"""]
    for group in "ABCDEF":
        members = [entry for entry in entries if entry["group"] == group]
        if not members:
            continue
        parts.append(f"<section><h2>{html.escape(GROUP_TITLES[group])}</h2>")
        for entry in members:
            oid = html.escape(entry["observation_id"])
            full, crop, mask = [html.escape(x) for x in entry["images"]]
            parts.append(
                f"<article><h3 class='meta'>{oid}</h3>"
                f"<p>Frame {entry['frame_index']} · frame_id <span class='meta'>{html.escape(entry['frame_id'])}</span></p>"
                f"<div class='imgs'><figure><figcaption>RGB toàn cảnh với mask overlay</figcaption><img src='{full}'></figure>"
                f"<figure><figcaption>RGB crop</figcaption><img src='{crop}'></figure>"
                f"<figure><figcaption>Mask</figcaption><img src='{mask}'></figure></div>"
                f"<details><summary>Thông tin pose/geometry (không phải nhãn)</summary>"
                f"<p class='meta'>sensor pose world xyz: {html.escape(json.dumps(entry['sensor_pose_world_xyz']))}</p>"
                f"<p class='meta'>view-bin diagnostic: {html.escape(str(entry['view_bin_diagnostic']))}; "
                f"P3 center: {html.escape(json.dumps(entry['p3_center_world_diagnostic']))}; "
                f"extent m: {html.escape(json.dumps(entry['p3_extents_m_diagnostic']))}</p></details>"
                f"<p><b>a. Identity:</b> token: ____________________ &nbsp; □ confirmed &nbsp; □ uncertain</p>"
                f"<p><b>b. Class:</b> ____________________ &nbsp; □ confirmed &nbsp; □ uncertain</p>"
                f"<p>Ghi chú (nếu cần): ______________________________________________</p></article>"
            )
        parts.append("</section>")
    parts.append("</html>")
    (output_dir / "index.html").write_text("\n".join(parts), encoding="utf-8")

    with (output_dir / "annotation_template.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "observation_id", "identity_token", "identity_status", "reviewed_class", "class_status", "notes"
        ])
        writer.writeheader()
        for entry in entries:
            writer.writerow({"observation_id": entry["observation_id"]})

    source_paths = [
        run_dir / "u4ov/single_view_predictions.jsonl",
        run_dir / "association/assignments_source_raw.jsonl",
        run_dir / "association/assignments_source_dedup.jsonl",
        run_dir / "association/assignments_geometry_only.jsonl",
        run_dir / "association/assignments_geometry_appearance.jsonl",
        gallery,
    ]
    manifest = {
        "kind": "blinded_observation_review_packet",
        "scene": "HM3D 00800 development pilot; not benchmark/holdout",
        "selection_policy": "fixed observation-key list: target-region temporal sequence and return; frame-2 glance; additional observations for class output and neighboring/association-diagnostic review; exact-pose revisit retained as one observation but is not independent view support",
        "observation_count": len(entries),
        "prediction_labels_scores_and_track_ids_shown_in_html": False,
        "review_scopes": ["physical identity per observation", "class per observation"],
        "identity_token_semantics": "user-defined token shared only across observations confirmed as the same physical object; uncertain/blank allowed",
        "selected_observations": entries,
        "source_sha256": {str(p.relative_to(run_dir)): sha256(p) for p in source_paths},
    }
    (output_dir / "selection_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(entries)} observation cards to {output_dir}")


if __name__ == "__main__":
    main()

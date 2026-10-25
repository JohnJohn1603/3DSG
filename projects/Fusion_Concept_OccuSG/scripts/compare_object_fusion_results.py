#!/usr/bin/env python3
"""Create a membership-based, fixed-assignment geometry-fusion comparison page."""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def load_fusion(root: Path) -> tuple[dict, list[dict], dict[str, dict]]:
    root = root.resolve()
    manifest = json.loads((root / "fusion/manifest.json").read_text(encoding="utf-8"))
    modes = manifest.get("objects", {})
    if len(modes) != 1:
        raise ValueError(f"expected one association mode in fusion result: {sorted(modes)}")
    mode = next(iter(modes))
    objects_path = root / "fusion" / mode / "objects.json"
    objects = json.loads(objects_path.read_text(encoding="utf-8"))
    tracks: dict[str, dict] = {}
    for obj in objects:
        oid = str(obj["object_id"])
        evidence = list(obj.get("evidence", []))
        tracks[oid] = {"object_id": oid, "summary": obj, "evidence": evidence,
                       "members": {str(row["observation_id"]) for row in evidence},
                       "geometry_members": {str(row["observation_id"]) for row in evidence},
                       "aliases": []}
    for row in manifest.get("excluded_observations", {}).get(mode, []):
        if row.get("decision") != "alias_excluded_from_fusion" or not row.get("object_id"):
            continue
        oid, track_id = str(row["observation_id"]), str(row["object_id"])
        track = tracks.setdefault(track_id, {"object_id": track_id, "summary": {"observation_count": 0,
            "raw_point_count": 0, "voxel_count": 0, "timeline_png": None},
            "evidence": [], "members": set(), "geometry_members": set(), "aliases": []})
        track["members"].add(oid)
        track["aliases"].append(row)
    return manifest, list(tracks.values()), {str(key): value for key, value in modes.items()}


def membership_components(source_tracks: list[dict], candidate_tracks: list[dict]) -> list[dict]:
    """Connected components of track overlap; deliberately allows 1:N and N:M."""
    source_adj = {i: set() for i in range(len(source_tracks))}
    candidate_adj = {i: set() for i in range(len(candidate_tracks))}
    for i, source in enumerate(source_tracks):
        for j, candidate in enumerate(candidate_tracks):
            if source["members"] & candidate["members"]:
                source_adj[i].add(j)
                candidate_adj[j].add(i)
    components = []
    unseen_s, unseen_c = set(source_adj), set(candidate_adj)
    while unseen_s or unseen_c:
        todo_s = [min(unseen_s)] if unseen_s else []
        todo_c = [min(unseen_c)] if not todo_s and unseen_c else []
        seen_s, seen_c = set(), set()
        while todo_s or todo_c:
            while todo_s:
                i = todo_s.pop()
                if i in seen_s:
                    continue
                seen_s.add(i)
                todo_c.extend(source_adj[i] - seen_c)
            while todo_c:
                j = todo_c.pop()
                if j in seen_c:
                    continue
                seen_c.add(j)
                todo_s.extend(candidate_adj[j] - seen_s)
        unseen_s -= seen_s
        unseen_c -= seen_c
        sm = set().union(*(source_tracks[i]["members"] for i in seen_s)) if seen_s else set()
        cm = set().union(*(candidate_tracks[j]["members"] for j in seen_c)) if seen_c else set()
        shared = sm & cm
        source_partition = {tuple(sorted(source_tracks[i]["members"])) for i in seen_s}
        candidate_partition = {tuple(sorted(candidate_tracks[j]["members"])) for j in seen_c}
        components.append({"source_indices": sorted(seen_s), "candidate_indices": sorted(seen_c),
            "source_members": sm, "candidate_members": cm, "shared_members": shared,
            "topology_changed": source_partition != candidate_partition,
            "source_track_count": len(seen_s), "candidate_track_count": len(seen_c),
            "observation_count": len(sm | cm)})
    components.sort(key=lambda c: (not c["topology_changed"], -c["observation_count"],
                                   min(c["shared_members"] or c["source_members"] or c["candidate_members"] or {""})))
    return components


def _load_snapshot(fusion_root: Path, evidence: list[dict], which: str) -> np.ndarray:
    if not evidence:
        return np.empty((0, 3), dtype=float)
    row = evidence[0] if which == "first" else evidence[-1]
    path = fusion_root / "fusion" / row["snapshot_npz"]
    with np.load(path, allow_pickle=False) as data:
        return data["xyz_W"].astype(float, copy=True)


def render_cloud_comparison(path: Path, source_root: Path, candidate_root: Path,
                            source_tracks: list[dict], candidate_tracks: list[dict],
                            source_indices: list[int], candidate_indices: list[int]) -> None:
    panels = []
    for title, root, tracks, indices in (
        ("Source — first", source_root, source_tracks, source_indices),
        ("Source — fused", source_root, source_tracks, source_indices),
        ("Candidate — first", candidate_root, candidate_tracks, candidate_indices),
        ("Candidate — fused", candidate_root, candidate_tracks, candidate_indices)):
        clouds = []
        which = "first" if "first" in title else "fused"
        for idx in indices:
            xyz = _load_snapshot(root, tracks[idx]["evidence"], which)
            if len(xyz): clouds.append(xyz)
        panels.append((title, np.concatenate(clouds) if clouds else np.empty((0, 3))))
    all_xyz = np.concatenate([points for _, points in panels if len(points)], axis=0) if any(len(p) for _, p in panels) else np.zeros((1, 3))
    lo, hi = all_xyz.min(axis=0), all_xyz.max(axis=0)
    center = (lo + hi) / 2
    radius = max(float(np.max(hi - lo)) / 2, .05) * 1.08
    fig = plt.figure(figsize=(12, 8))
    for i, (title, xyz) in enumerate(panels, 1):
        ax = fig.add_subplot(2, 2, i, projection="3d")
        if len(xyz):
            stride = max(1, len(xyz) // 8000)
            ax.scatter(xyz[::stride, 0], xyz[::stride, 1], xyz[::stride, 2], s=1.2, alpha=.8)
        ax.set_title(f"{title} ({len(xyz):,} voxels)")
        ax.set_xlim(center[0]-radius, center[0]+radius)
        ax.set_ylim(center[1]-radius, center[1]+radius)
        ax.set_zlim(center[2]-radius, center[2]+radius)
        ax.set_xlabel("world X (m)"); ax.set_ylabel("world Y (m)"); ax.set_zlabel("world Z (m)")
        ax.view_init(elev=24, azim=-55)
    fig.tight_layout()
    fig.savefig(path, dpi=120, facecolor="white")
    plt.close(fig)


def build_comparison(source_id: str, candidate_id: str, source_root: Path,
                     candidate_root: Path, output: Path) -> dict:
    source_manifest, source_tracks, _ = load_fusion(source_root)
    candidate_manifest, candidate_tracks, _ = load_fusion(candidate_root)
    for key in ("input_capture_sha256", "input_p2_index_sha256", "p3_detection_manifest_sha256"):
        if source_manifest.get(key) != candidate_manifest.get(key):
            raise ValueError(f"fusion results do not share {key}")
    source_ids = set().union(*(x["members"] for x in source_tracks)) if source_tracks else set()
    candidate_ids = set().union(*(x["members"] for x in candidate_tracks)) if candidate_tracks else set()
    components = membership_components(source_tracks, candidate_tracks)
    output.mkdir(parents=True, exist_ok=False)
    cards = []
    changed_count = sum(c["topology_changed"] for c in components)
    for number, component in enumerate(components, 1):
        s_tracks = [source_tracks[i] for i in component["source_indices"]]
        c_tracks = [candidate_tracks[i] for i in component["candidate_indices"]]
        card_dir = output / f"group_{number:03d}"
        card_dir.mkdir()
        cloud_name = f"group_{number:03d}/clouds.png"
        render_cloud_comparison(output / cloud_name, source_root, candidate_root, source_tracks,
                                candidate_tracks, component["source_indices"], component["candidate_indices"])
        source_list = "<br>".join(f"{html.escape(t['object_id'])}: {len(t['members'])} obs / {len(t['geometry_members'])} fused" for t in s_tracks) or "—"
        candidate_list = "<br>".join(f"{html.escape(t['object_id'])}: {len(t['members'])} obs / {len(t['geometry_members'])} fused / {len(t['aliases'])} aliases" for t in c_tracks) or "—"
        relationship = f"{len(s_tracks)} source ↔ {len(c_tracks)} candidate"
        changed = "assignment grouping differs" if component["topology_changed"] else "same observation grouping"
        obs_rows = []
        obs_ids = sorted(component["source_members"] | component["candidate_members"],
                         key=lambda oid: (int(oid.rsplit(":det-", 1)[0].rsplit("-", 1)[-1]), oid))
        source_evidence = {r["observation_id"]: r for t in s_tracks for r in t["evidence"]}
        candidate_evidence = {r["observation_id"]: r for t in c_tracks for r in t["evidence"]}
        alias_ids = {str(a["observation_id"]) for t in c_tracks for a in t["aliases"]}
        for oid in obs_ids:
            ev = source_evidence.get(oid) or candidate_evidence.get(oid)
            image = "—"
            if ev and ev.get("evidence_image"):
                image_path = (source_root if oid in source_evidence else candidate_root) / "fusion" / ev["evidence_image"]
                if image_path.is_file():
                    image = f"<img class='evidence' src='{html.escape(image_path.as_uri())}' alt='RGB mask {html.escape(oid)}'>"
            framestr = ev.get("frame_id", "—") if ev else "—"
            obs_rows.append("<tr><td><code>"+html.escape(oid)+"</code></td><td>"+html.escape(str(framestr))+"</td>"
                f"<td>{'yes' if oid in source_evidence else 'no'}</td><td>{'alias: geometry excluded' if oid in alias_ids else ('yes' if oid in candidate_evidence else 'no')}</td>"
                f"<td>{image}</td></tr>")
        cards.append(f"<details {'open' if component['topology_changed'] else ''}><summary>Group {number}: {relationship}; {changed}; {len(component['shared_members'])} shared observations</summary>"
            f"<div class='cols'><div><b>Source tracks</b><p>{source_list}</p></div><div><b>Candidate tracks</b><p>{candidate_list}</p></div></div>"
            f"<img class='cloud' src='{cloud_name}' alt='first and fused cloud comparison with common axes'>"
            "<table><thead><tr><th>Observation ID</th><th>Frame</th><th>Source geometry</th><th>Candidate geometry</th><th>RGB + mask evidence</th></tr></thead><tbody>"
            + "".join(obs_rows) + "</tbody></table></details>")
    common = source_ids & candidate_ids
    html_doc = ("<!doctype html><meta charset='utf-8'><title>Fixed-assignment object fusion comparison</title>"
        "<style>body{font:14px system-ui;max-width:1450px;margin:1.5rem auto;padding:0 1rem;color:#202934}"
        "details{border:1px solid #cbd3dd;border-radius:8px;padding:1rem;margin:1rem 0}summary{font-weight:700;cursor:pointer}"
        ".cols{display:grid;grid-template-columns:1fr 1fr;gap:1rem}.cloud{width:100%;max-width:1100px}"
        ".evidence{width:190px;height:auto;border:1px solid #bbb}table{border-collapse:collapse;width:100%;margin-top:.8rem}"
        "td,th{border:1px solid #d3d8df;padding:.4rem;text-align:left;vertical-align:top}code{overflow-wrap:anywhere}</style>"
        "<h1>Object geometry fusion — source vs candidate</h1>"
        f"<p>Source <code>{html.escape(source_id)}</code> — {html.escape(source_manifest.get('association_backend','source'))}; "
        f"candidate <code>{html.escape(candidate_id)}</code> — {html.escape(candidate_manifest.get('association_backend','candidate'))}.</p>"
        f"<p>Shared capture/P2/P3 hashes verified. P3 observation universe: source={len(source_ids)}, candidate={len(candidate_ids)}, "
        f"intersection={len(common)}, union={len(source_ids|candidate_ids)}. Source objects={len(source_tracks)}; "
        f"candidate track groups={len(candidate_tracks)}. Groups with changed membership partition={changed_count}/{len(components)}.</p>"
        "<p>Groups are connected by shared observation IDs and may be one-to-many or many-to-many; object ID suffixes are labels only. "
        "Candidate aliases remain attached to their canonical track membership but are excluded from its fused geometry. "
        "Points/voxels are descriptive counts, not geometry accuracy or association quality. First/fused panels use common world-axis limits within each group.</p>"
        + "".join(cards))
    (output / "index.html").write_text(html_doc, encoding="utf-8")
    report = {"schema_version": 1, "source_fusion_id": source_id, "candidate_fusion_id": candidate_id,
        "shared_input_hashes": {k: source_manifest.get(k) for k in ("input_capture_sha256", "input_p2_index_sha256", "p3_detection_manifest_sha256")},
        "source_observation_membership": len(source_ids), "candidate_observation_membership": len(candidate_ids),
        "shared_observations": len(common), "union_observations": len(source_ids | candidate_ids),
        "source_track_count": len(source_tracks), "candidate_track_count": len(candidate_tracks),
        "membership_components": [{"source_tracks": [source_tracks[i]["object_id"] for i in c["source_indices"]],
            "candidate_tracks": [candidate_tracks[i]["object_id"] for i in c["candidate_indices"]],
            "source_members": sorted(c["source_members"]), "candidate_members": sorted(c["candidate_members"]),
            "shared_members": sorted(c["shared_members"]), "topology_changed": c["topology_changed"]} for c in components],
        "geometry_accuracy_claimed": False, "viewer": str(output / "index.html")}
    (output / "manifest.json").write_text(json.dumps(report, indent=2, sort_keys=True)+"\n", encoding="utf-8")
    report_lines = ["# Object geometry fusion comparison", "", f"- Source: `{source_id}`", f"- Candidate: `{candidate_id}`",
        f"- Shared P3 input hashes: verified; common observations: {len(common)}/{len(source_ids|candidate_ids)}",
        f"- Tracks: source {len(source_tracks)}, candidate {len(candidate_tracks)}; membership partitions changed in {changed_count}/{len(components)} components.",
        "- This compares fixed-assignment geometry outputs. It does not score geometry accuracy or prove candidate association is better.",
        f"- Viewer: `{output / 'index.html'}`", ""]
    (output / "report.vi.md").write_text("\n".join(report_lines), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_fusion_id")
    ap.add_argument("candidate_fusion_id")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--fusion-root", type=Path, default=Path("artifacts/object_fusions"))
    args = ap.parse_args()
    root = args.fusion_root.resolve()
    for fusion_id in (args.source_fusion_id, args.candidate_fusion_id):
        if Path(fusion_id).name != fusion_id or not fusion_id.startswith("fusion_"):
            raise ValueError(f"invalid fusion ID: {fusion_id}")
    report = build_comparison(args.source_fusion_id, args.candidate_fusion_id,
        root / args.source_fusion_id, root / args.candidate_fusion_id, args.output.resolve())
    print(json.dumps({k: report[k] for k in ("source_observation_membership", "candidate_observation_membership",
        "shared_observations", "source_track_count", "candidate_track_count", "viewer")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

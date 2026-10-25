import hashlib
import json
import builtins

import numpy as np
import pytest

from integration_v2.capture_package import sha256_file
from integration_v2.unified_scene_graph import build_unified_graph, import_object_export
from standalone_sg.pipeline.contracts import GraphEdge, GraphNode, SceneGraphSnapshot
from standalone_sg.serialization.json_graph import deserialize_snapshot, serialize_snapshot


def _room_snapshot():
    room=GraphNode("room:r1","room",0,1,"fixture-room",{
        "name":"room_1","room_id":1,"anchor_region_id":1,"centroid_xy":[2,2],
        "outer_ring":[[0,0],[4,0],[4,4],[0,4]],"holes":[],"member_ids":[]})
    outside=GraphNode("navigation:n1","navigation",0,1,"fixture-room",{"center_xy":[2,2],"bounds_xy":[1,1,3,3]})
    old=GraphNode("object:old","object",0,1,"old-legacy",{"center_W":[2,2,0]})
    return SceneGraphSnapshot(1,"scene","seq",1,1.0,(room,outside,old),
        (GraphEdge(room.node_id,outside.node_id,"room_contains","fixture-room"),GraphEdge(room.node_id,old.node_id,"room_contains","old-legacy")),"cfg",{})


def _export(tmp_path, objects, *, transform=None):
    root=tmp_path/"export"; root.mkdir()
    native=tmp_path/"fixture-native-map-marker.json"; native.write_text('{"fixture":true,"purpose":"contract test only"}')
    files={}; rows=[]
    for item in objects:
        p=root/f"{item['export_id']}.npz"
        center=np.asarray(item.get("center",[2,2,0]),dtype=np.float32)
        offsets=np.array([[-.05,-.05,-.05],[.05,.05,.05]],dtype=np.float32)
        points=center[None,:]+offsets
        np.savez(p,xyz=points)
        files[p.name]=sha256_file(p)
        rows.append({"export_id":item["export_id"],"cloud_npz":p.name,
            "bounds_minmax":[(center+offsets.min(0)).tolist(),(center+offsets.max(0)).tolist()],
            "membership_status":"available","raw_label":item.get("label"),
            "fused_semantic_label":None,"fused_semantic_status":"unavailable"})
    manifest={"schema":"conceptgraphs-object-export/v1","identity_scope":"final_map_only_no_persistent_lifecycle_claim",
        "coordinate_frame":"conceptgraphs_world_from_capture_contract","units":"m","scene_id":"scene","sequence_id":"seq",
        "provenance":{"source_kind":"fixture","display_label":"Fixture display label"},"files_sha256":files,
        "native_map_file":native.name,"native_map_sha256":sha256_file(native),
        "T_capture_world_from_cg_world":(np.eye(4) if transform is None else transform).tolist(),"objects":rows}
    (root/"export_manifest.json").write_text(json.dumps(manifest))
    return root


def test_import_transforms_ids_preserves_labels_and_assigns_room_idempotently(tmp_path):
    transform=np.eye(4); transform[:3,3]=[1,-1,0]
    export=_export(tmp_path,[{"export_id":"same-id","center":[1,2,0],"label":"chair"}],transform=transform)
    room=_room_snapshot()
    before_room=[n for n in room.nodes if n.kind=="room"]
    before_adj=[e for e in room.edges if e.source_id.startswith("room:") and e.target_id.startswith("room:")]
    graph,manifest=import_object_export(room,export)
    assert manifest["source_display_label"] == "Fixture display label"
    obj=next(n for n in graph.nodes if n.kind=="object")
    assert obj.node_id=="object:conceptgraphs:same-id"
    assert obj.attributes["external_object_id"]=="same-id"
    assert obj.attributes["center_W"]==pytest.approx([2,1,0])
    assert obj.attributes["raw_label"]=="chair" and obj.attributes["fused_semantic_label"] is None
    assert obj.attributes["membership_status"]=="assigned"
    assert sum(e.relation=="room_contains" and e.target_id==obj.node_id for e in graph.edges)==1
    assert [n for n in graph.nodes if n.kind=="room"]==before_room
    assert [e for e in graph.edges if e.source_id.startswith("room:") and e.target_id.startswith("room:")]==before_adj
    again,_=import_object_export(graph,export)
    assert serialize_snapshot(again)==serialize_snapshot(graph)
    assert manifest["association_rerun"] is False
    assert deserialize_snapshot(serialize_snapshot(graph))==graph


def test_unverified_spatial_scope_keeps_membership_unavailable_without_edges(tmp_path):
    export=_export(tmp_path,[{"export_id":"scope-blocked","center":[2,2,0]}])
    graph,manifest=import_object_export(_room_snapshot(),export,
        membership_policy="unavailable_elevation_scope")
    obj=next(n for n in graph.nodes if n.kind=="object")
    assert obj.attributes["membership_status"]=="unavailable"
    assert obj.attributes["membership_reason"]=="unavailable_elevation_scope"
    assert not [e for e in graph.edges if e.relation=="room_contains" and e.target_id==obj.node_id]
    assert manifest["membership_policy"]=="unavailable_elevation_scope"
    assert manifest["room_semantic_scoring_changed"] is False


def test_hybrid_reference_export_gets_explicit_viewer_label_without_losing_source_kind(tmp_path):
    export = _export(tmp_path, [{"export_id":"hybrid-object"}])
    path = export / "export_manifest.json"
    manifest = json.loads(path.read_text())
    manifest["provenance"].pop("display_label")
    manifest["provenance"]["pipeline"] = "hybrid cached YOLO11n-seg + cached ViT-B-32 image features + pinned ConceptGraphs reference mapping core"
    path.write_text(json.dumps(manifest))
    graph, imported = import_object_export(_room_snapshot(), export)
    assert imported["source_kind"] == "fixture"  # source provenance remains machine-readable
    assert imported["source_display_label"] == "Hybrid YOLO11n-seg + ViT-B-32 / ConceptGraphs reference mapping core"


def test_boundary_and_outside_objects_are_not_forced_into_rooms(tmp_path):
    export=_export(tmp_path,[
        {"export_id":"boundary","center":[0,2,0]},
        {"export_id":"outside","center":[9,9,0]},
    ])
    graph,_=import_object_export(_room_snapshot(),export)
    objects={n.attributes["external_object_id"]:n for n in graph.nodes if n.kind=="object"}
    assert objects["boundary"].attributes["membership_status"]=="ambiguous"
    assert objects["outside"].attributes["membership_status"]=="unassigned"
    assert not [e for e in graph.edges if e.relation=="room_contains" and e.target_id in {n.node_id for n in objects.values()}]


def test_scene_or_sequence_mismatch_and_duplicate_export_ids_fail(tmp_path):
    export=_export(tmp_path,[{"export_id":"one"}])
    manifest=json.loads((export/"export_manifest.json").read_text())
    manifest["sequence_id"]="wrong"; (export/"export_manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match="sequence_id mismatch"):
        import_object_export(_room_snapshot(),export)


def test_duplicate_export_ids_are_rejected_by_contract(tmp_path):
    export=_export(tmp_path,[{"export_id":"one"}])
    manifest=json.loads((export/"export_manifest.json").read_text())
    manifest["objects"].append(dict(manifest["objects"][0]))
    (export/"export_manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match="export_id values must be unique"):
        import_object_export(_room_snapshot(),export)


def test_cloud_must_be_hashed_and_stay_inside_export_root(tmp_path):
    export=_export(tmp_path,[{"export_id":"one"}])
    manifest=json.loads((export/"export_manifest.json").read_text())
    manifest["files_sha256"]={}
    (export/"export_manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match="absent from files_sha256"):
        import_object_export(_room_snapshot(),export)


def test_source_run_id_mismatch_fails_before_output(tmp_path):
    export=_export(tmp_path,[{"export_id":"one"}])
    manifest=json.loads((export/"export_manifest.json").read_text())
    manifest["source_run_id"]="other-run"
    (export/"export_manifest.json").write_text(json.dumps(manifest))
    room_path=tmp_path/"runs"/"expected-run"/"replay"/"snapshot.json"
    room_path.parent.mkdir(parents=True)
    room_path.write_text(serialize_snapshot(_room_snapshot()))
    output=tmp_path/"must-not-exist"
    with pytest.raises(ValueError,match="run_id mismatch"):
        build_unified_graph(room_path,export,output)
    assert not output.exists()


def test_build_uses_dependency_free_svg_when_matplotlib_is_missing(tmp_path, monkeypatch):
    export = _export(tmp_path, [{"export_id":"visible-object", "center":[2,2,0]}])
    room_path = tmp_path / "snapshot.json"
    room_path.write_text(serialize_snapshot(_room_snapshot()))
    output = tmp_path / "unified"
    original_import = builtins.__import__

    def import_without_matplotlib(name, *args, **kwargs):
        if name == "standalone_sg.visualization.plots":
            raise ModuleNotFoundError("No module named 'matplotlib'", name="matplotlib")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_matplotlib)
    build_unified_graph(room_path, export, output)
    manifest = json.loads((output / "manifest.json").read_text())
    viewer = (output / "index.html").read_text()
    svg = (output / "object_room_map.svg").read_text()
    assert manifest["viewer_renderer"] == "stdlib_svg_fallback_matplotlib_unavailable"
    assert "object_room_map.svg" in viewer
    assert "Fixture display label" in viewer
    assert '<polygon' in svg and '<circle' in svg

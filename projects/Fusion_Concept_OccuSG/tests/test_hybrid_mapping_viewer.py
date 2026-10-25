from __future__ import annotations

import json

from integration_v2.hybrid_mapping_viewer import _viewer_html


def test_hybrid_viewer_exposes_map_selection_filter_status_and_lineage_fields():
    data = {
        "source_label": "Hybrid YOLO11n-seg + ViT-B-32 / ConceptGraphs reference mapping core",
        "scene_id": "scene", "sequence_id": "sequence",
        "counts": {"pre_filter": {"before_filter": 2}, "final": 0},
    }
    html = _viewer_html(json.dumps(data), "<svg></svg>", "<svg></svg>")
    assert "Before filter · 2 hypotheses" in html
    assert "Final map · 0 objects" in html
    assert "Filtered; not merged into survivors" in html
    assert "Distinct camera poses" in html
    assert "not thereby a confirmed physical identity" in html
    assert "Raw class history" in html
    assert "addEventListener('click',()=>selectObject(el.dataset.object))" in html
    assert "addEventListener('keydown'" in html
    assert "membership_reason" in html


def test_hybrid_viewer_can_show_fresh_run_scope_limitation():
    data = {
        "source_label": "Hybrid YOLO11n-seg + ViT-B-32 / ConceptGraphs reference mapping core",
        "scene_id": "apt", "sequence_id": "route",
        "counts": {"pre_filter": {"before_filter": 0}, "final": 0},
        "viewer_context": {
            "display_note": "Room geometry present; elevation scope unverified.",
            "membership_status": "unavailable_elevation_scope",
            "room_semantics_status": "unavailable_membership_scope",
            "execution_mode": "fresh_scene_route_batch / final map",
        },
    }
    html = _viewer_html(json.dumps(data), "<svg></svg>", "<svg></svg>")
    assert "Room geometry present; elevation scope unverified." in html
    assert "unavailable_elevation_scope" in html
    assert "unavailable_membership_scope" in html
    assert "fresh_scene_route_batch / final map" in html

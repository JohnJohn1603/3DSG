"""Path contract for the self-contained research handoff.

Only Python interpreters may live outside the bundle. Models, source, native
workers and bootstrap configuration resolve from this relocated bundle.
"""
from __future__ import annotations
import json
import os
from pathlib import Path

BUNDLE_ROOT = Path(__file__).resolve().parents[3]
PROJECTS_ROOT = BUNDLE_ROOT / 'projects'
CG_ROOT = PROJECTS_ROOT / 'Fusion_Concept_OccuSG'
OCC_ROOT = PROJECTS_ROOT / 'Rebuild_3DScene_OccuSG'
ASSETS_ROOT = BUNDLE_ROOT / 'assets'


def runtime_config():
    configured = os.environ.get('THREEDSG_HANDOFF_RUNTIME')
    return json.loads(Path(configured).read_text()) if configured else {}


def interpreter(role):
    path = Path(runtime_config().get(role + '_python',
                BUNDLE_ROOT / '.venvs' / role / 'bin/python')).expanduser()
    return path if path.is_absolute() else BUNDLE_ROOT / path


def config_root():
    return Path(os.environ.get('THREEDSG_HANDOFF_CONFIG', BUNDLE_ROOT / 'config'))


def source_paths(conceptgraphs_root=CG_ROOT, occusg_root=OCC_ROOT):
    cg, occ = Path(conceptgraphs_root), Path(occusg_root)
    return {
        'cg_python': interpreter('conceptgraphs'),
        'appearance_python': interpreter('appearance'),
        'occ_python': interpreter('perception'),
        'p2_worker': PROJECTS_ROOT / '3DSG/scripts/p2_onnx_worker.py',
        'p2_weights': ASSETS_ROOT / 'models/yolo11n-seg.onnx',
        'p2_profile': cg / 'configs/perception/yolo11n_seg_replica_cad_dev.json',
        'p2_names': cg / 'configs/perception/coco.names',
        'p3_profile': cg / 'configs/perception/yolo11n_seg_replica_cad_dev.json',
        'checkpoint': ASSETS_ROOT / 'models/openclip/open_clip_model.safetensors',
        'feature_contract': BUNDLE_ROOT / 'config/feature_contract.json',
        'worker_script': cg / 'scripts/run_t2_d_cached_packet_worker.py',
        'route_config': config_root() / 'native_config.json',
        'scene_file': ASSETS_ROOT / 'scenes/hm3d-00802/wcojb4TFT35.basis.glb',
    }

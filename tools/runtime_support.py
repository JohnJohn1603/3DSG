"""Relocatable demo configuration; generated files and results stay outside source."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / 'projects/3DSG'
ROLES = ('habitat', 'conceptgraphs', 'appearance', 'perception')


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): digest.update(block)
    return digest.hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def runtime_paths(config=None):
    supplied = json.loads(Path(config).read_text()) if config else {}
    paths = {}
    for role in ROLES:
        path = Path(supplied.get(role+'_python', ROOT/'.venvs'/role/'bin/python')).expanduser()
        paths[role+'_python'] = str(path if path.is_absolute() else ROOT/path)
    if supplied.get('pytorch3d_source'):
        source = Path(supplied['pytorch3d_source']).expanduser()
        paths['pytorch3d_source'] = str(source if source.is_absolute() else ROOT/source)
    return paths


def prepare(output_root, run_id, runtime=None):
    output = Path(output_root).expanduser().absolute()
    if output == ROOT or ROOT in output.parents:
        raise ValueError('Output directory must be outside the clean handoff folder')
    if not run_id or Path(run_id).name != run_id or run_id in ('.', '..'):
        raise ValueError('run-id must be one plain directory name')
    bootstrap = output/'_bootstrap'/run_id
    bootstrap.mkdir(parents=True, exist_ok=True)
    paths = runtime_paths(runtime)
    write_json(bootstrap/'runtime.json', paths)
    scene = json.loads((ROOT/'config/demo_scene.template.json').read_text())
    for key in ('scene_asset', 'scene_dataset_config', 'navmesh_file'):
        scene[key] = str(ROOT/scene[key])
    scene['provenance']['source_effective_config'] = str(ROOT/scene['provenance']['source_effective_config'])
    write_json(bootstrap/'demo_scene.json', scene)
    native = json.loads((ROOT/'config/native_bootstrap.template.json').read_text())
    pipeline, m2, dude = [str(ROOT/native[k]) for k in ('pipeline_config','m2_binary','dude_binary')]
    write_json(bootstrap/'c2_run_manifest.json', {
        'schema':'3dsg-live-native-bootstrap/v1', 'purpose':'live configuration; no saved frames or graph',
        'stages': {'conceptgraphs_mapping': {'command':[paths['conceptgraphs_python']]},
                   'occusg_rooms': {'command':[paths['perception_python'], '--pipeline-config',pipeline,
                    '--m2-m3-binary',m2,'--dude-binary',dude]}}})
    write_json(bootstrap/'native_config.json', {'scene_file':scene['scene_asset'],
        'pipeline_config':pipeline, 'native_m2_m3':m2, 'native_dude':dude})
    env = os.environ.copy()
    cg = ROOT/'projects/Fusion_Concept_OccuSG'; occ = ROOT/'projects/Rebuild_3DScene_OccuSG'
    pythonpath = [ROOT/'tools/audit', PROJECT, cg, cg/'src',
                  cg/'ref_ConceptGraph/concept-graphs', occ/'src']
    if paths.get('pytorch3d_source'):
        source = Path(paths['pytorch3d_source']).resolve(strict=True)
        if not source.is_relative_to(ROOT):
            raise ValueError('Optional PyTorch3D source override must stay inside the bundle')
        pythonpath.insert(1, source)
    env.update({
        'THREEDSG_HANDOFF_RUNTIME':str(bootstrap/'runtime.json'),
        'THREEDSG_HANDOFF_CONFIG':str(bootstrap),
        '3DSG_CONCEPTGRAPHS_ROOT':str(cg), '3DSG_OCCUSG_ROOT':str(occ),
        'THREEDSG_GRAPH_WORKER_NICE':env.get('THREEDSG_GRAPH_WORKER_NICE','2'),
        'OCCUSG_M2_TRANSPORT':'binary', 'HF_HUB_OFFLINE':'1', 'TRANSFORMERS_OFFLINE':'1',
        'PYTHONDONTWRITEBYTECODE':'1', 'PYTHONNOUSERSITE':'1',
        'OMP_NUM_THREADS':env.get('OMP_NUM_THREADS','1'),
        'OPENBLAS_NUM_THREADS':env.get('OPENBLAS_NUM_THREADS','1'),
        'XDG_CACHE_HOME':str(output/'_cache'), 'MPLCONFIGDIR':str(output/'_cache/matplotlib'),
        'PYTHONPATH':os.pathsep.join(map(str, pythonpath)),
        'LD_LIBRARY_PATH':str(ROOT/'prebuilt/linux-x86_64/lib'),
        'THREEDSG_HANDOFF_BUNDLE_ROOT':str(ROOT),
        'THREEDSG_HANDOFF_OUTPUT_ROOT':str(output),
        'THREEDSG_HANDOFF_AUDIT_DIR':str(output/'_audit'/run_id),
    })
    return output, bootstrap, paths, env


def preflight(paths, env, native=True, verify_hashes=True):
    roles = ROLES if native else ('habitat',)
    if verify_hashes:
        manifest = json.loads((ROOT/'ASSET_MANIFEST.json').read_text())
        for relative, expected in manifest['files'].items():
            path = ROOT/relative
            if not path.is_file() or sha(path) != expected['sha256']:
                raise ValueError('Asset/native hash mismatch: '+relative)
    commands = {
      'habitat': 'import habitat_sim,numpy,scipy; print("Habitat",habitat_sim.__version__)',
      'conceptgraphs': 'import torch,pytorch3d,pytorch3d._C; from scripts.run_conceptgraphs_cached_hybrid import make_reference_api; make_reference_api(); print("ConceptGraphs",pytorch3d.__file__)',
      'appearance': 'import torch,open_clip,safetensors; from integration_v2.perception_feature_sessions import FeatureContract; print("OpenCLIP available")',
      'perception': 'import numpy,yaml,onnxruntime,PIL; from standalone_sg.mapping import NativeRGBDMappingStage; print("P2/OccuSG available")',
    }
    for role in roles:
        executable = paths[role+'_python']
        if not Path(executable).is_file():
            raise FileNotFoundError(f'{role} interpreter missing: {executable}; use tools/setup_environments.py or --runtime')
        result = subprocess.run([executable,'-B','-c',commands[role]], cwd=ROOT/'projects/Fusion_Concept_OccuSG',
                                env=env, capture_output=True,text=True,timeout=120)
        if result.returncode:
            raise RuntimeError(f'{role} preflight failed:\n{result.stdout}\n{result.stderr}')
        print(result.stdout.strip(),flush=True)
    if native:
        for name in ('standalone_sg_octomap_fixture_runner','occusg_dude_worker'):
            path = ROOT/'prebuilt/linux-x86_64/bin'/name
            result = subprocess.run(['ldd',str(path)],env=env,capture_output=True,text=True,check=True)
            if 'not found' in result.stdout: raise RuntimeError(result.stdout)
    print('PREFLIGHT PASS: source, model, scene and native paths resolve in this bundle.',flush=True)

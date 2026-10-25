#!/usr/bin/env python3
"""One-shot source-only seed. Never install dependencies or copy run artifacts.

Run only in the new Fusion_Concept_OccuSG workspace. Existing targets cause failure.
This script is a bootstrap artifact, not a runtime launcher.
"""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BASE = Path('/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG')
TREES = ('src', 'scripts', 'configs', 'native', 'adapters', 'tests', 'webots')
FILES = ('pyproject.toml', 'run_scene.sh', 'locks/sources.json',
         'evaluation/__init__.py', 'evaluation/metrics.py', 'evaluation/protocol.md',
         'evaluation/protocol_habitat_v1.json')
DOCS = ('PHASE1_PLAN.vi.md', 'SCENE_LAUNCHER.vi.md', 'RUN_AND_EVALUATE.vi.md',
        'M3_STATUS.vi.md', 'M4_STATUS.vi.md', 'M6_STATUS.vi.md', 'M8_STATUS.vi.md',
        'M9_STATUS.vi.md', 'REGION_OBJECT_ROOM_LABEL_STATUS.vi.md',
        'GT_SEMANTIC_OBJECT_BUILD.md')
SKIP = {'__pycache__', '.pytest_cache', '.git', 'node_modules', 'outputs', 'artifacts'}

def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()

def main():
    if ROOT.name != 'Fusion_Concept_OccuSG' or ROOT == BASE:
        raise RuntimeError('Unexpected destination')
    manifest = ROOT / 'locks/baseline_seed_manifest.json'
    if manifest.exists():
        raise RuntimeError('Already seeded: do not overwrite a developing workspace')
    pairs = []
    for tree in TREES:
        for source in sorted((BASE / tree).rglob('*')):
            rel = source.relative_to(BASE)
            if any(part in SKIP for part in rel.parts) or source.suffix in {'.pyc', '.pyo'}:
                continue
            if source.is_symlink():
                raise RuntimeError(f'Review source symlink explicitly: {source}')
            if source.is_file():
                pairs.append((source, ROOT / rel))
    pairs.extend((BASE / name, ROOT / name) for name in FILES)
    pairs.extend((BASE / 'docs' / name, ROOT / 'docs/baseline_reference' / name)
                 for name in DOCS if (BASE / 'docs' / name).is_file())
    for source, target in pairs:
        if target.exists():
            raise RuntimeError(f'Existing destination: {target}')
        if not source.is_file():
            raise RuntimeError(f'Missing source: {source}')
    records = []
    for source, target in pairs:
        before = sha(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if sha(target) != before or sha(source) != before:
            raise RuntimeError(f'Copy verification failed: {source}')
        records.append({'source': str(source), 'destination': str(target.relative_to(ROOT)),
                        'sha256': before, 'bytes': source.stat().st_size})
    doc = {'schema_version': 1, 'baseline_root': str(BASE), 'seed_root': str(ROOT),
           'status': 'source_seed_only_not_fusion_implementation',
           'root_git_available': (BASE / '.git/HEAD').is_file(),
           'files': records, 'bytes': sum(r['bytes'] for r in records),
           'excluded': ['old runs', 'datasets', 'model weights', 'venvs', 'build trees',
                        'GT renders', 'credentials', '.aws', '.codex', '.agents', 'caches'],
           'note': 'Webots source-only retained for existing imports/tests; Habitat is active scope.'}
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + '\n')
    repos = {}
    for name, rel in [('OccuSG', 'ref_OccuSG/OccuSG'), ('ConceptGraphs', 'ref_ConceptGraph/concept-graphs')]:
        path = ROOT / rel
        repos[name] = {'path': rel, 'commit': git(path, 'rev-parse', 'HEAD'),
                       'origin': git(path, 'remote', 'get-url', 'origin'),
                       'clean': not bool(git(path, 'status', '--porcelain')),
                       'submodules_initialized': False}
    (ROOT / 'locks/reference_repositories.json').write_text(json.dumps(repos, indent=2) + '\n')
    print(json.dumps({'files_copied': len(records), 'bytes': doc['bytes'], 'references': repos}, indent=2))

if __name__ == '__main__':
    main()

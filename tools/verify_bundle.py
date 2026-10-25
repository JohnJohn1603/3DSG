#!/usr/bin/env python3
"""Verify the clean source bundle and its checksums (or record intentional edits)."""
import argparse
import hashlib
import json
import os
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MANIFEST=ROOT/'BUNDLE_MANIFEST.json'
BAD={'.git','.aws','.agents','.codex','__pycache__','.pytest_cache','runs','artifacts'}


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def inventory():
    files={};links={};problems=[]
    for parent,dirs,names in os.walk(ROOT,followlinks=False):
        if Path(parent)==ROOT:
            dirs[:]=[name for name in dirs if name!='.git']
            names[:]=[name for name in names if name!='.git']
        for name in dirs:
            if name in BAD or name.startswith('.venv'):problems.append(str(Path(parent,name).relative_to(ROOT)))
        for name in dirs+names:
            p=Path(parent,name)
            if p.is_symlink():
                if not p.exists() or not p.resolve().is_relative_to(ROOT):problems.append('external/broken symlink: '+str(p))
                links[str(p.relative_to(ROOT))]=os.readlink(p)
        for name in names:
            p=Path(parent,name)
            if p==MANIFEST or p.is_symlink():continue
            files[str(p.relative_to(ROOT))]={'bytes':p.stat().st_size,'sha256':sha(p)}
    if problems:raise ValueError('Unclean bundle: '+'; '.join(problems))
    return {'schema':'3dsg-clean-research-bundle/v1','files':files,'internal_symlinks':links}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-manifest',action='store_true',help='Record current files after intentional development changes')
    parser.add_argument('--source-only',action='store_true',help='Verify tracked source while demo assets are not yet downloaded')
    args=parser.parse_args();actual=inventory()
    if args.write_manifest:MANIFEST.write_text(json.dumps(actual,indent=2,ensure_ascii=False)+'\n')
    else:
        expected=json.loads(MANIFEST.read_text())
        if args.source_only:
            actual['files']={k:v for k,v in actual['files'].items() if not k.startswith('assets/')}
            expected['files']={k:v for k,v in expected['files'].items() if not k.startswith('assets/')}
        if actual!=expected:
            diff=[k for k in actual['files'].keys()|expected['files'].keys() if actual['files'].get(k)!=expected['files'].get(k)]
            raise SystemExit('Manifest mismatch: '+', '.join(sorted(diff)[:25]))
    print('PASS:',len(actual['files']),'files;',sum(v['bytes'] for v in actual['files'].values()),'bytes; no runs, caches, external symlinks or virtualenvs; mode='+('source-only' if args.source_only else 'complete'))

if __name__=='__main__':main()

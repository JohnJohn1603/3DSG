#!/usr/bin/env python3
"""Restore missing demo assets from the checksum-pinned complete handoff Release."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RELEASE_URL = 'https://github.com/JohnJohn1603/3DSG/releases/download/research-handoff-2026-10-26/3DSG_Research_Bundle.tar.gz'
ARCHIVE_SHA256 = 'd37365aaef4cbdc0c4ea31fdfb4df0f037ac2abaf23ceb3d6d8b3683bf2e0c5d'


def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def restore(archive, required):
    if sha(archive)!=ARCHIVE_SHA256:raise ValueError('Release archive SHA-256 mismatch')
    remaining=dict(required)
    with tarfile.open(archive,'r:gz') as stream:
        for member in stream:
            prefix='3DSG_Research_Bundle/'
            if not member.name.startswith(prefix):continue
            relative=member.name[len(prefix):]
            if relative not in remaining:continue
            if not member.isfile():raise ValueError('Expected regular asset file: '+relative)
            destination=ROOT/relative
            if not destination.resolve().is_relative_to(ROOT):raise ValueError('Asset path escaped checkout')
            expected=remaining[relative]
            if member.size!=expected['bytes']:raise ValueError('Asset size mismatch: '+relative)
            destination.parent.mkdir(parents=True,exist_ok=True)
            temporary=None
            try:
                with tempfile.NamedTemporaryFile(dir=destination.parent,prefix='.download-',delete=False) as output:
                    temporary=Path(output.name)
                    source=stream.extractfile(member)
                    if source is None:raise ValueError('Missing asset payload')
                    with source:shutil.copyfileobj(source,output,1024*1024)
                if sha(temporary)!=expected['sha256']:raise ValueError('Asset checksum mismatch: '+relative)
                os.replace(temporary,destination);temporary=None
            finally:
                if temporary is not None:temporary.unlink(missing_ok=True)
            remaining.pop(relative)
            print('Restored',relative,flush=True)
    if remaining:raise ValueError('Release missing required assets: '+', '.join(remaining))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,help='Use an already downloaded complete Release .tar.gz')
    parser.add_argument('--check',action='store_true',help='Only verify asset presence/hashes; do not download')
    args=parser.parse_args()
    manifest=json.loads((ROOT/'ASSET_MANIFEST.json').read_text())['files']
    missing={name:record for name,record in manifest.items() if name.startswith('assets/')
             and (not (ROOT/name).is_file() or sha(ROOT/name)!=record['sha256'])}
    if not missing:
        print('PASS: all demo assets are present and checksum-verified');return 0
    if args.check:
        print('Missing or changed:',', '.join(missing));return 1
    if args.archive:restore(args.archive.expanduser().absolute(),missing)
    else:
        with tempfile.TemporaryDirectory(prefix='3dsg-release-assets-') as folder:
            archive=Path(folder)/'handoff.tar.gz'
            print('Downloading complete Release archive (~464 MiB):',RELEASE_URL,flush=True)
            with urllib.request.urlopen(RELEASE_URL,timeout=120) as response,archive.open('wb') as output:
                shutil.copyfileobj(response,output,1024*1024)
            restore(archive,missing)
    print('PASS: demo assets restored and checksum-verified');return 0

if __name__=='__main__':raise SystemExit(main())

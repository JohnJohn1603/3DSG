#!/usr/bin/env python3
"""Rebuild the two bundled non-ROS native workers from verified bundled sources."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {'standalone_sg_octomap_fixture_runner': 'occupancy', 'occusg_dude_worker': 'dude'}


def verify_mapconversion():
    manifest = json.loads((ROOT / 'tools/native_cmake/mapconversion-tree.json').read_text())
    lock = json.loads((ROOT / 'projects/Rebuild_3DScene_OccuSG/locks/sources.json').read_text())
    expected = next(item['commit'] for item in lock['submodules'] if item['name'].startswith('Map-Conversion'))
    if manifest['commit'] != expected:
        raise SystemExit('MapConversion manifest commit differs from project source lock')
    source = ROOT / manifest['source']
    actual = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(source.rglob('*')) if p.is_file()}
    if actual != manifest['files']:
        changed = sorted(k for k in actual.keys() | manifest['files'].keys() if actual.get(k) != manifest['files'].get(k))
        raise SystemExit('MapConversion vendored source verification failed: ' + ', '.join(changed))
    print(f'Verified MapConversion {expected}: {len(actual)} files', flush=True)


def run(command, dry_run):
    print('+', shlex.join(map(str, command)), flush=True)
    if not dry_run:
        subprocess.run(list(map(str, command)), check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, default=Path(tempfile.gettempdir()) / '3dsg-native-build')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'prebuilt/linux-x86_64')
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'AMD64'):
        parser.error('Vendored FIST/static libraries target Linux x86_64 only')
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    verify_mapconversion()
    build = args.build_dir.expanduser().absolute()
    output = args.output_dir.expanduser().absolute()
    if build == ROOT or ROOT in build.parents:
        parser.error('--build-dir must be outside the clean source bundle')
    run(['cmake', '-S', ROOT / 'tools/native_cmake', '-B', build, f'-DBUNDLE_ROOT={ROOT}', '-DCMAKE_BUILD_TYPE=Release'], args.dry_run)
    run(['cmake', '--build', build, '--parallel', args.jobs, '--target', *TARGETS], args.dry_run)
    if args.dry_run:
        print(f'Would copy final workers to {output / "bin"} and GMP/MPFR shared libraries to {output / "lib"}')
        return 0
    (output / 'bin').mkdir(parents=True, exist_ok=True)
    (output / 'lib').mkdir(parents=True, exist_ok=True)
    for name, folder in TARGETS.items():
        target = output / 'bin' / name
        shutil.copy2(build / folder / name, target)
        if shutil.which('strip'):
            run(['strip', '--strip-unneeded', target], False)
    # Dereference symlinks so archive extraction cannot retain an absolute link.
    libraries = ROOT / 'third_party/m4-prefix/usr/lib/x86_64-linux-gnu'
    for pattern in ('libgmp.so.*', 'libgmpxx.so.*', 'libmpfr.so.*'):
        for source in libraries.glob(pattern):
            shutil.copy2(source.resolve(), output / 'lib' / source.name)
    files = [p for directory in ('bin', 'lib') for p in sorted((output / directory).iterdir()) if p.is_file()]
    checksums = {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    (output / 'SHA256SUMS.json').write_text(json.dumps(checksums, indent=2) + '\n')
    for name in TARGETS:
        result = subprocess.run(['ldd', str(output / 'bin' / name)], text=True, capture_output=True, check=True)
        if 'not found' in result.stdout:
            raise SystemExit(f'{name}: missing runtime libraries:\n{result.stdout}')
    asset_manifest = ROOT / 'ASSET_MANIFEST.json'
    if asset_manifest.exists() and output == ROOT / 'prebuilt/linux-x86_64':
        assets = json.loads(asset_manifest.read_text())
        assets['files'] = {key: value for key, value in assets['files'].items()
                           if not key.startswith('prebuilt/linux-x86_64/')}
        for path in sorted(output.rglob('*')):
            if path.is_file():
                assets['files'][str(path.relative_to(ROOT))] = {
                    'bytes': path.stat().st_size,
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        asset_manifest.write_text(json.dumps(assets, indent=2) + '\n')
    print(f'Built and checked {len(TARGETS)} workers in {output}; SHA256SUMS.json refreshed')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())

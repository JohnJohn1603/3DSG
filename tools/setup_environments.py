#!/usr/bin/env python3
"""Recreate isolated Linux x86_64 environments; downloads require network access."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ROLES = ('habitat', 'conceptgraphs', 'appearance', 'perception')


def run(command, dry_run=False, **kwargs):
    print('+', shlex.join(map(str, command)), flush=True)
    if not dry_run:
        subprocess.run(list(map(str, command)), check=True, **kwargs)


def resolve_program(value):
    found = shutil.which(value)
    if not found:
        raise SystemExit(f'Executable not found: {value}. Supply its path with the matching option.')
    return Path(found).absolute()


def ensure_python(program, expected, dry_run):
    if dry_run:
        return
    actual = subprocess.check_output([str(program), '-c', 'import sys; print("%s.%s" % sys.version_info[:2])'], text=True).strip()
    if actual != expected:
        raise SystemExit(f'{program}: need Python {expected}, found {actual}')


def create_venv(role, destination, base_python, version, args, versions):
    python = destination / 'bin/python'
    if not args.dry_run:
        ensure_python(base_python, version, False)
    if not python.exists():
        run([base_python, '-m', 'venv', destination], args.dry_run)
    ensure_python(python, version, args.dry_run)
    installed = versions[role]['packages']
    tooling = [f'{name}=={installed[name]}' for name in ('pip', 'setuptools', 'wheel') if name in installed]
    if tooling:
        run([python, '-m', 'pip', 'install', *tooling], args.dry_run)
    requirements = ROOT / 'environments' / f'{role}-requirements.txt'
    lines = requirements.read_text().splitlines()
    torch_lines = [line for line in lines if line.lower().startswith(('torch==', 'torchvision=='))]
    if torch_lines:
        run([python, '-m', 'pip', 'install', '--index-url', 'https://download.pytorch.org/whl/cpu', *torch_lines], args.dry_run)
    # Keep PyPI resolution away from the CPU-only PyTorch index. Every remaining
    # distribution is pinned; torch/torchvision are already installed above.
    remainder = '\n'.join(line for line in lines if line not in torch_lines) + '\n'
    if args.dry_run:
        print(f'+ {python} -m pip install -r <{role} pinned requirements excluding torch/torchvision>')
    else:
        with tempfile.TemporaryDirectory(prefix='3dsg-requirements-') as tmp:
            req = Path(tmp) / 'requirements.txt'
            req.write_text(remainder)
            run([python, '-m', 'pip', 'install', '-r', req])
    if role == 'conceptgraphs':
        env = os.environ.copy()
        env.update(MAX_JOBS=str(args.jobs), FORCE_CUDA='0', PYTORCH3D_FORCE_NO_CUDA='1')
        # Non-editable wheel installation prevents references to another machine.
        if args.dry_run:
            print(f'+ {python} -m pip install --no-build-isolation --no-deps <temporary clean copy of third_party/pytorch3d-v0.7.4>')
        else:
            with tempfile.TemporaryDirectory(prefix='3dsg-pytorch3d-') as tmp:
                source = Path(tmp) / 'pytorch3d'
                shutil.copytree(ROOT / 'third_party/pytorch3d-v0.7.4', source,
                                ignore=shutil.ignore_patterns('.git', 'build', '*.so', '__pycache__', '*.egg-info'))
                run([python, '-m', 'pip', 'install', '--no-build-isolation', '--no-deps', source], env=env)
    imports = {'conceptgraphs': 'torch,torchvision,numpy,open3d,faiss,pytorch3d,pytorch3d._C',
               'appearance': 'torch,torchvision,open_clip',
               'perception': 'numpy,onnxruntime,PIL,yaml'}
    run([python, '-c', 'import ' + imports[role]], args.dry_run)
    run([python, '-m', 'pip', 'check'], args.dry_run)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--roles', nargs='+', choices=ROLES, default=list(ROLES))
    parser.add_argument('--venvs-dir', type=Path, default=ROOT / '.venvs')
    parser.add_argument('--runtime-config', type=Path, required=True, help='Write launcher runtime JSON here (absolute interpreter paths).')
    parser.add_argument('--python310', default='python3.10', help='CPython 3.10 for ConceptGraphs')
    parser.add_argument('--python312', default='python3.12', help='CPython 3.12 for appearance/perception')
    parser.add_argument('--conda', default='conda')
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--dry-run', action='store_true', help='Print install commands; no downloads or writes.')
    args = parser.parse_args()
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'AMD64'):
        parser.error('These captured specs target Linux x86_64 only.')
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    base = args.venvs_dir.expanduser().absolute()
    versions = json.loads((ROOT / 'environments/installed-versions.json').read_text())
    for role in args.roles:
        destination = base / role
        if role == 'habitat':
            conda = args.conda if args.dry_run else resolve_program(args.conda)
            if not (destination / 'bin/python').exists():
                run([conda, 'create', '--yes', '--prefix', destination, '--file', ROOT / 'environments/habitat-linux-64.explicit.txt'], args.dry_run)
            python = destination / 'bin/python'
            run([python, '-m', 'pip', 'install', '-r', ROOT / 'environments/habitat-pip-overrides.txt'], args.dry_run)
            ensure_python(python, '3.9', args.dry_run)
            run([python, '-c', 'import habitat_sim,numpy,PIL; print(habitat_sim.__version__)'], args.dry_run)
        else:
            raw_python, version = (args.python310, '3.10') if role == 'conceptgraphs' else (args.python312, '3.12')
            executable = raw_python if args.dry_run else resolve_program(raw_python)
            create_venv(role, destination, executable, version, args, versions)
    config = {f'{role}_python': str(base / role / 'bin/python') for role in ROLES}
    if args.dry_run:
        print('Runtime config:', json.dumps(config, indent=2))
        print('Would write:', args.runtime_config.absolute())
    else:
        output = args.runtime_config.expanduser().absolute()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(config, indent=2) + '\n')
        print('Wrote', output)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())

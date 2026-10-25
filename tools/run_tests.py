#!/usr/bin/env python3
"""Run 3DSG core regression tests, without writing caches into the handoff."""
import argparse
import os
from pathlib import Path
import subprocess
import shutil
import sys
from runtime_support import ROOT,PROJECT

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--python',default=sys.executable,help='Python with pytest/numpy/scipy/PyQt5')
args=parser.parse_args()
args.python=shutil.which(args.python) or str(Path(args.python).expanduser().absolute())
env=os.environ.copy()
env.update(PYTHONDONTWRITEBYTECODE='1',QT_QPA_PLATFORM='offscreen',PYTHONNOUSERSITE='1',
           PYTHONPATH=str(PROJECT))
raise SystemExit(subprocess.run([args.python,'-B','-m','pytest','-q','-p','no:cacheprovider','tests'],
                               cwd=PROJECT,env=env).returncode)

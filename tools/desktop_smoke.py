#!/usr/bin/env python3
"""Run the real desktop/runner and capture the live widget to external output."""
import argparse
from pathlib import Path
import subprocess
import shutil
import sys
from runtime_support import ROOT,PROJECT,prepare,preflight

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--runtime',type=Path)
parser.add_argument('--output-root',type=Path,required=True)
parser.add_argument('--ui-python',default=sys.executable)
parser.add_argument('--run-id',default='desktop-verification')
args=parser.parse_args()
args.ui_python=shutil.which(args.ui_python) or str(Path(args.ui_python).expanduser().absolute())
output,bootstrap,paths,env=prepare(args.output_root,args.run_id,args.runtime)
preflight(paths,env)
command=[args.ui_python,'-B',str(PROJECT/'scripts/capture_live_dashboard_smoke.py'),
         '--runner-python',paths['habitat_python'],'--scene-config',str(bootstrap/'demo_scene.json'),
         '--profile',str(PROJECT/'configs/exploration/cpu_p2_motion.yaml'),
         '--output-root',str(output),'--duration-s','90','--capture-after-s','25',
         '--minimum-graph-revision','2']
raise SystemExit(subprocess.run(command,cwd=PROJECT,env=env).returncode)

#!/usr/bin/env python3
"""Run the frontier + optional native ConceptGraphs/OccuSG demo from this folder."""
from __future__ import annotations
import argparse
from datetime import datetime
from pathlib import Path
import subprocess
import shutil
import sys
from tools.runtime_support import ROOT, PROJECT, prepare, preflight


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, help='JSON with four *_python interpreter paths; default .venvs/<role>/bin/python')
    parser.add_argument('--mode',choices=('native','navigation'),default='native')
    parser.add_argument('--desktop',action='store_true',help='Open the original live PyQt dashboard')
    parser.add_argument('--ui-python',default=sys.executable,help='Python with PyQt5 for --desktop')
    parser.add_argument('--duration-s',type=float,default=120)
    parser.add_argument('--output-root',type=Path,default=ROOT.parent/'3DSG_demo_outputs')
    parser.add_argument('--run-id',default=None)
    parser.add_argument('--check-only',action='store_true')
    parser.add_argument('--skip-preflight',action='store_true',help='Use only after checking this exact runtime and asset manifest')
    args=parser.parse_args()
    if args.duration_s <= 0: parser.error('--duration-s must be positive')
    args.ui_python = shutil.which(args.ui_python) or str(Path(args.ui_python).expanduser().absolute())
    run_id=args.run_id or 'handoff-'+datetime.now().strftime('%Y%m%d-%H%M%S')
    output,bootstrap,paths,env=prepare(args.output_root,run_id,args.runtime)
    if not args.skip_preflight: preflight(paths,env,native=args.mode=='native')
    if args.check_only: return 0
    profile=PROJECT/'configs/exploration/cpu_p2_motion.yaml'
    common=['--scene-config',str(bootstrap/'demo_scene.json'),'--profile',str(profile),
            '--output-root',str(output)]
    if args.desktop:
        command=[args.ui_python,'-B',str(PROJECT/'scripts/run_desktop.py'),
                 '--runner-python',paths['habitat_python'],'--autostart',*common,'--duration-s',str(args.duration_s)]
        if args.mode=='native': command+=['--graph-plugin','exploration.native_graph_plugin:create_plugin']
        else: command+=['--navigation-only']
    else:
        command=[paths['habitat_python'],'-B',str(PROJECT/'scripts/run_frontier_habitat.py'),
                 *common,'--budget-s',str(args.duration_s),'--run-id',run_id]
        if args.mode=='native':
            command+=['--enable-p2','--enable-graph','--graph-plugin','exploration.native_graph_plugin:create_plugin']
    print('Demo output:',output,flush=True)
    return subprocess.run(command,cwd=PROJECT,env=env).returncode

if __name__=='__main__': raise SystemExit(main())

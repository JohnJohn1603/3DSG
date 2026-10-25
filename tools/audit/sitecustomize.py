"""Import/file-origin audit, with optional forbidden-source enforcement.

The demonstration can reuse installed interpreters while rejecting source and
asset reads from the original checkouts. Native libraries are separately
checked with ldd/RUNPATH. No tracing/ptrace privilege is required.
"""
import atexit
import json
import os
import sys
from pathlib import Path

bundle = Path(os.environ.get('THREEDSG_HANDOFF_BUNDLE_ROOT', '/nonexistent')).absolute()
output = Path(os.environ.get('THREEDSG_HANDOFF_OUTPUT_ROOT', '/nonexistent')).absolute()
forbidden = [Path(p).absolute() for p in json.loads(os.environ.get('THREEDSG_HANDOFF_FORBIDDEN_ROOTS','[]'))]
allowed = [bundle, output, Path(sys.prefix).absolute(), Path(sys.base_prefix).absolute()]
runtime = os.environ.get('THREEDSG_HANDOFF_RUNTIME')
if runtime:
    allowed.extend(Path(p).absolute().parent.parent for p in json.loads(Path(runtime).read_text()).values())
violations = []
checked_bundle_opens = 0
busy = False


def inside(path, parent):
    return path == parent or parent in path.parents


def audit(event, args):
    global busy, checked_bundle_opens
    if busy or not forbidden: return
    if event not in ('open','import','os.listdir','os.scandir'): return
    raw = (args[1] if len(args)>1 else None) if event=='import' else (args[0] if args else None)
    if not isinstance(raw,(str,bytes)): return
    busy=True
    try:
        path=Path(os.fsdecode(raw)).absolute()
        if event=='open' and inside(path,bundle): checked_bundle_opens += 1
        if any(inside(path,root) for root in forbidden) and not any(inside(path,root) for root in allowed):
            violations.append({'event':event,'path':str(path)})
            raise PermissionError('Handoff verification forbids original source/asset access: '+str(path))
    finally:
        busy=False

sys.addaudithook(audit)


def save_imports():
    target=os.environ.get('THREEDSG_HANDOFF_AUDIT_DIR')
    if not target: return
    try:
        root=Path(target);root.mkdir(parents=True,exist_ok=True)
        modules={name:str(Path(module.__file__).absolute())
                 for name,module in list(sys.modules.items()) if getattr(module,'__file__',None)}
        outside={name:path for name,path in modules.items()
                 if any(inside(Path(path),r) for r in forbidden)
                 and not any(inside(Path(path),r) for r in allowed)}
        doc={'executable':sys.executable,'argv':sys.argv,'modules':modules,
             'forbidden_source_violations':violations,'forbidden_module_origins':outside,
             'checked_bundle_opens':checked_bundle_opens,
             'scope':'Python audit events/import origins; native runtime checked separately with ldd/RUNPATH'}
        (root/('imports-'+str(os.getpid())+'.json')).write_text(json.dumps(doc,indent=2)+'\n')
    except Exception as error:
        print('handoff import audit failed:',error,file=sys.stderr)

atexit.register(save_imports)

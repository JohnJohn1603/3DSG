#!/usr/bin/env python3
"""Read-only capability audit for the existing ConceptGraphs/OccuSG workers.

The legacy project intentionally keeps its Python environments separate from
the Habitat process. This script records whether those pinned environments,
model files and native workers are present before a live mapper episode starts.
It does not install packages, start native workers, or write into the legacy
projects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe(python: Path, code: str, *, env: dict[str, str] | None = None) -> dict[str, Any]:
    if not python.is_file():
        return {"python": str(python), "ok": False, "error": "interpreter_missing"}
    try:
        result = subprocess.run([str(python), "-c", code], text=True,
                                capture_output=True, check=False, timeout=30,
                                env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"python": str(python), "ok": False,
                "error": f"{type(exc).__name__}: {exc}"}
    return {"python": str(python), "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip()[-3000:]}


def resource(path: Path, *, executable: bool = False) -> dict[str, Any]:
    row: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if path.is_file():
        stat = path.stat()
        row.update({"bytes": stat.st_size, "sha256": sha256(path),
                    "executable": os.access(path, os.X_OK)})
    if executable and not row["exists"]:
        row["executable"] = False
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", type=Path, default=Path(
        "/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG"))
    parser.add_argument("--baseline-root", type=Path, default=Path(
        "/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG"))
    args = parser.parse_args()
    legacy, baseline = args.legacy_root.resolve(), args.baseline_root.resolve()
    config_path = legacy / "configs/launcher/local_environment.json"
    report: dict[str, Any] = {
        "schema": "3dsg-legacy-mapper-preflight/v1",
        "read_only": True,
        "host_python": {"executable": sys.executable, "version": sys.version.split()[0]},
        "legacy_root": str(legacy), "baseline_root": str(baseline),
    }
    if not config_path.is_file():
        report["fatal"] = f"missing local environment config: {config_path}"
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2
    config = json.loads(config_path.read_text(encoding="utf-8"))
    cg_python = legacy / ".venv-cg-cpu/bin/python"
    appearance_python = legacy / ".venv-appearance/bin/python"
    occ_python = baseline / ".venv-perception/bin/python"
    conceptgraph_ref = legacy / "ref_ConceptGraph/concept-graphs"
    cg_env = os.environ.copy()
    cg_env["PYTHONPATH"] = os.pathsep.join((str(legacy), str(legacy / "src"),
                                               str(conceptgraph_ref),
                                               cg_env.get("PYTHONPATH", "")))
    occ_env = os.environ.copy()
    occ_env["PYTHONPATH"] = os.pathsep.join((str(legacy), str(legacy / "src"),
                                               str(baseline / "src"),
                                               occ_env.get("PYTHONPATH", "")))

    report["interpreters"] = {
        "conceptgraphs": probe(cg_python,
            "import sys, torch, omegaconf; import conceptgraph; "
            "print({'python':sys.version.split()[0],'torch':torch.__version__,"
            "'cuda':torch.version.cuda,'conceptgraph':conceptgraph.__file__})", env=cg_env),
        "openclip_features": probe(appearance_python,
            "import sys, torch, open_clip; "
            "print({'python':sys.version.split()[0],'torch':torch.__version__,"
            "'open_clip':open_clip.__version__})"),
        "occusg": probe(occ_python,
            "import sys, numpy; from standalone_sg.pipeline import SceneGraphBuilder; "
            "from standalone_sg.mapping import NativeRGBDMappingStage; "
            "from standalone_sg.regions.dude_client import DuDeWorkerClient; "
            "print({'python':sys.version.split()[0],'numpy':numpy.__version__,"
            "'builder':SceneGraphBuilder.__module__})", env=occ_env),
    }
    env_config = json.loads(config_path.read_text(encoding="utf-8"))
    model_root = Path(env_config.get("model_root", baseline / "artifacts/models"))
    if not model_root.is_absolute():
        model_root = baseline / model_root
    checkpoint = legacy / "artifacts/models/openclip/vit-b-32-openai-a6f597a30f7b82c51704746581f9a4e41421e878/open_clip_model.safetensors"
    pipeline_config = legacy / "configs/reference_code_mp3d.yaml"
    report["resources"] = {
        "conceptgraphs_reference": {"path": str(conceptgraph_ref),
                                     "exists": (conceptgraph_ref / "conceptgraph").is_dir()},
        "openclip_checkpoint": resource(checkpoint),
        "occusg_pipeline_config": resource(pipeline_config),
        "native_m2_m3": resource(Path(env_config["native_m2_m3"]), executable=True),
        "native_dude": resource(Path(env_config["native_dude"]), executable=True),
        "yolo_onnx": resource(baseline / "artifacts/models/ultralytics/yolo11n-seg.onnx"),
        "configured_model_root": str(model_root),
    }
    report["integration_readiness"] = {
        "conceptgraphs_worker": "ready" if report["interpreters"]["conceptgraphs"]["ok"] else "blocked_by_environment_probe",
        "openclip_feature_worker": "ready" if report["interpreters"]["openclip_features"]["ok"] and report["resources"]["openclip_checkpoint"]["exists"] else "blocked_by_environment_or_checkpoint",
        "occusg_worker": "ready" if report["interpreters"]["occusg"]["ok"] else "blocked_by_environment_probe",
        "p3_stream": "not_yet_connected_to_3dsg_plugin",
        "paired_live_publication": "not_yet_connected_to_3dsg_plugin",
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

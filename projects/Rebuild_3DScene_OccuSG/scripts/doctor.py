#!/usr/bin/env python3
"""Read-only M0 environment and baseline integrity check."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path


EXPECTED_COMMIT = "2bca2fa06af87fd9dd0542957039be8f580f0dca"


def command_version(command: str, flag: str = "--version") -> str | None:
    path = shutil.which(command)
    if path is None:
        return None
    result = subprocess.run([path, flag], capture_output=True, text=True, check=False)
    first_line = (result.stdout or result.stderr).splitlines()
    return first_line[0] if first_line else path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", default="ref_OccuSG/OccuSG")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    baseline = Path(args.baseline)
    result: dict[str, object] = {
        "baseline": str(baseline),
        "expected_commit": EXPECTED_COMMIT,
        "toolchain": {
            "cxx": command_version("c++"),
            "cmake": command_version("cmake"),
            "ninja": command_version("ninja"),
        },
        "headers": {
            "opencv4": Path("/usr/include/opencv4/opencv2/core.hpp").exists(),
            "eigen3": Path("/usr/include/eigen3/Eigen/Core").exists(),
            "octomap": Path("/usr/include/octomap/OcTree.h").exists(),
            "cgal": Path("/usr/include/CGAL/Exact_predicates_inexact_constructions_kernel.h").exists(),
        },
        "libraries": {
            "octomap_runtime": any(Path(path).glob("liboctomap.so*") for path in (
                "/usr/lib/x86_64-linux-gnu",
                "/usr/local/lib",
            )),
        },
    }
    if baseline.exists():
        commit = subprocess.run(
            ["git", "-C", str(baseline), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        result["actual_commit"] = commit
        result["baseline_commit_matches"] = commit == EXPECTED_COMMIT
    else:
        result["actual_commit"] = None
        result["baseline_commit_matches"] = False

    print(json.dumps(result, indent=2, sort_keys=True))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result["baseline_commit_matches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

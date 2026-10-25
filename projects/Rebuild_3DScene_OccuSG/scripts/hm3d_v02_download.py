#!/usr/bin/env python3
"""Securely preflight and download Habitat-Sim's HM3D minival v0.2 group.

Credentials are read with getpass, held only in memory, and passed to curl over
stdin config. They are never put in shell history, process argv, logs, or files.
Run with the pinned Habitat-Sim Python environment.
"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "artifacts" / "habitat_data"
MINIVAL_GROUP = "hm3d_minival_v0.2"
EXPECTED_HABITAT_VERSION = "0.3.3"
MIN_FREE_BYTES = 5 * 1024**3


def curl_config_user(user: str, password: str) -> str:
    # JSON string quoting is compatible with curl's double-quoted config values.
    return "user = " + json.dumps(user + ":" + password) + "\n"


def hidden_curl(args: list[str], user: str, password: str) -> subprocess.CompletedProcess:
    """Run curl with credentials on stdin, never in argv or its output."""
    if any("--user" == arg or arg.startswith("--user=") or arg == "-u" for arg in args):
        raise RuntimeError("refusing curl argv that contains an auth option")
    safe_args = ["curl", "--config", "-", *args]
    return subprocess.run(
        safe_args,
        input=curl_config_user(user, password),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def access_probe(url: str, user: str, password: str) -> Tuple[Optional[int], Optional[str], Optional[int], int]:
    """Probe with a bounded GET because Matterport may reject HEAD (405)."""
    result = hidden_curl(
        ["--silent", "--show-error", "--location", "--range", "0-0",
         "--max-filesize", "4096", "--max-time", "30", "--output", os.devnull,
         "--write-out", "%{http_code}\t%{content_type}\t%{size_download}", url],
        user,
        password,
    )
    output = result.stdout.strip().split("\t")
    try:
        status = int(output[0]) if output and output[0] else None
    except ValueError:
        status = None
    content_type = output[1].lower() if len(output) > 1 and output[1] else None
    try:
        size = int(float(output[2])) if len(output) > 2 and output[2] else None
    except ValueError:
        size = None
    return status, content_type, size, result.returncode


def access_failure(status: Optional[int], content_type: Optional[str], curl_code: int) -> str:
    if status == 401:
        return "HTTP 401: token ID/Secret was rejected or expired."
    if status == 403:
        return "HTTP 403: access is not authorized for this account/resource; a token alone does not grant HM3D approval."
    if status == 404:
        return "HTTP 404: endpoint/UID is unavailable; check downloader version or Matterport resource status."
    if content_type and ("text/html" in content_type or "text/plain" in content_type):
        return f"HTTP {status} returned {content_type}, not a dataset archive; this may be an access page or service response."
    if curl_code in (6, 7, 28, 35, 52, 56):
        return f"curl network/TLS/connection failure (exit {curl_code}); this is not an access-denied response."
    return f"no successful HTTP preflight (HTTP {status}, curl exit {curl_code}); no archive was downloaded."


def main() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Run this command in an interactive Terminal so credentials can be hidden.")
        return 2
    if not shutil.which("curl"):
        print("curl is required by the installed Habitat-Sim HM3D downloader.")
        return 2

    try:
        import habitat_sim
        import habitat_sim.utils.datasets_download as downloader
    except Exception as exc:
        print(f"Habitat-Sim import failed ({type(exc).__name__}); no environment changes were made.")
        return 2
    version = str(getattr(habitat_sim, "__version__", "unknown"))
    if version != EXPECTED_HABITAT_VERSION:
        print(f"Expected pinned Habitat-Sim {EXPECTED_HABITAT_VERSION}, found {version}; refusing to upgrade or continue.")
        return 2

    data_root = str(DATA_PATH.resolve()) + os.sep
    downloader.initialize_test_data_sources(data_root)
    uids = downloader.data_groups.get(MINIVAL_GROUP)
    if not uids:
        print(f"Installed Habitat-Sim downloader lacks {MINIVAL_GROUP}; no fallback was selected.")
        return 3

    target = DATA_PATH / "versioned_data" / "hm3d-0.2" / "hm3d"
    link = DATA_PATH / "scene_datasets" / "hm3d"
    if target.exists() or os.path.lexists(str(link)):
        print("HM3D target/link already exists. Refusing to replace or merge it; inspect it first.")
        return 4
    free_bytes = shutil.disk_usage(DATA_PATH).free
    if free_bytes < MIN_FREE_BYTES:
        print(f"Only {free_bytes / 1024**3:.1f} GiB free; require at least 5 GiB before minival extraction.")
        return 5

    username = getpass.getpass("Matterport Token ID (hidden): ")
    password = getpass.getpass("Matterport Token Secret (hidden): ")
    if not username or not password:
        print("Token ID and Secret must both be non-empty; nothing was downloaded.")
        return 6

    print(f"Preflighting {len(uids)} HM3D minival v0.2 archives with authenticated Range GET probes (max 4 KiB each)…")
    for uid in uids:
        source = downloader.data_sources[uid]
        status, content_type, size, curl_code = access_probe(source["source"], username, password)
        archive_type = bool(content_type and content_type.startswith(("application/", "binary/")))
        # 206 means the server honored bytes=0-0. Some servers ignore Range:
        # a 200 plus curl's size-cap exit (63) proves HTTP access without
        # receiving the archive body. Tiny config responses may fit under cap.
        probe_ok = (
            (status == 206 and curl_code == 0 and archive_type)
            or (status == 200 and curl_code == 63 and archive_type)
            or (status == 200 and curl_code == 0 and archive_type and size is not None and size <= 4096)
        )
        if not probe_ok:
            print(f"Preflight failed for {uid}: {access_failure(status, content_type, curl_code)}")
            print("No example dataset was selected and no archive was downloaded.")
            return 10
        print(f"  {uid}: HTTP {status}, {content_type or 'content type unavailable'}, probe body {size if size is not None else 'capped'} bytes")

    # Recheck current free space after preflight, then ask before extracting the
    # roughly 631 MB published package set (not including expansion).
    free_bytes = shutil.disk_usage(DATA_PATH).free
    print(f"Preflight passed. Free space: {free_bytes / 1024**3:.1f} GiB; published habitat + semantic archives are about 631 MB compressed.")
    print("The official README documents minival folders with .basis.glb, .basis.navmesh, .semantic.glb and .semantic.txt files.")
    if input("Download and extract only HM3D minival v0.2 now? [y/N] ").strip().lower() != "y":
        print("Stopped before download; no archive was downloaded.")
        return 0

    original_check_call = downloader.subprocess.check_call

    def safe_check_call(command, *args, **kwargs):
        if isinstance(command, str):
            import shlex
            argv = shlex.split(command)
        else:
            argv = list(command)
        if not argv or Path(argv[0]).name != "curl":
            raise RuntimeError("unexpected non-curl subprocess request from HM3D package downloader")
        rewritten = []
        index = 0
        while index < len(argv):
            token = argv[index]
            if token in ("--user", "-u"):
                # Remove the exact credential token from the external process argv.
                if index + 1 >= len(argv):
                    raise RuntimeError("malformed downloader auth arguments")
                supplied = argv[index + 1]
                if ":" not in supplied:
                    raise RuntimeError("malformed downloader auth value")
                supplied_user, supplied_password = supplied.split(":", 1)
                if supplied_user != username or supplied_password != password:
                    raise RuntimeError("downloader auth value differs from the hidden prompt")
                index += 2
                continue
            if token.startswith("--user="):
                supplied = token.partition("=")[2]
                if ":" not in supplied:
                    raise RuntimeError("malformed downloader auth value")
                supplied_user, supplied_password = supplied.split(":", 1)
                if supplied_user != username or supplied_password != password:
                    raise RuntimeError("downloader auth value differs from the hidden prompt")
                index += 1
                continue
            rewritten.append(token)
            index += 1
        if "--location" not in rewritten:
            raise RuntimeError("unexpected HM3D request; expected a Matterport curl download")
        rewritten.extend(["--fail-with-body", "--config", "-"])
        result = subprocess.run(
            rewritten,
            input=curl_config_user(username, password),
            text=True,
            check=False,
        )
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, rewritten)
        return 0

    try:
        downloader.subprocess.check_call = safe_check_call
        # Invoke the installed downloader's package API with credentials as
        # in-memory parameters, not as CLI argv; check_call above sanitizes the
        # downloader's generated curl command before creating a child process.
        for uid in uids:
            downloader.download_and_place(uid, data_root, username, password, replace=False)
    except subprocess.CalledProcessError as exc:
        code = exc.returncode
        if code in (6, 7, 18, 28, 35, 52, 56):
            print(f"Download stopped due to network/TLS/transfer failure (curl exit {code}); credentials were not logged.")
        elif code == 22:
            print("Download endpoint returned an HTTP error after preflight; check Matterport permissions or service status.")
        else:
            print(f"Download subprocess failed (exit {code}); command arguments were sanitized.")
        return 20
    except SystemExit as exc:
        print(f"Habitat-Sim downloader stopped (exit {exc.code}); credentials were not logged.")
        return 21
    except Exception as exc:
        print(f"HM3D extraction/setup failed ({type(exc).__name__}); credentials were not logged.")
        return 22
    finally:
        downloader.subprocess.check_call = original_check_call

    receipt = {
        "schema_version": 1,
        "kind": "hm3d_minival_v0.2_download_receipt",
        "status": "downloaded",
        "habitat_sim_version": version,
        "dataset_version": "HM3D v0.2",
        "split": "minival",
        "download_group": MINIVAL_GROUP,
        "uids": uids,
        "data_path": str(DATA_PATH),
        "scene_dataset_config": str(target / "hm3d_annotated_basis.scene_dataset_config.json"),
        "raw_glb_or_obj_full_archives_requested": False,
        "credentials_saved": False,
        "license": "https://matterport.com/legal/matterport-end-user-license-agreement-academic-use-model-data",
    }
    run_dir = ROOT / "artifacts" / "runs" / "hm3d_integration_20261005"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "download_receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(f"Download complete. Non-secret receipt: {run_dir / 'download_receipt.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

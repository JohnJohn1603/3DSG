#!/usr/bin/env python3
"""Fetch exactly one pinned OpenCLIP safetensors file; no repo snapshot clone."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

REVISION = "a6f597a30f7b82c51704746581f9a4e41421e878"
REPO = "timm/vit_base_patch32_clip_224.openai"
FILENAME = "open_clip_model.safetensors"
EXPECTED_SIZE = 605143284
URL = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{FILENAME}"


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    target = args.output_dir / FILENAME
    partial = args.output_dir / (FILENAME + ".partial")

    head = urllib.request.Request(URL, method="HEAD", headers={"User-Agent": "FusionConceptOccuSG-U2/1.0"})
    with urllib.request.urlopen(head, timeout=90) as response:
        content_length = int(response.headers.get("Content-Length", "-1"))
        etag = response.headers.get("ETag")
        if response.status != 200 or content_length != EXPECTED_SIZE:
            raise RuntimeError(f"pinned file metadata mismatch: status={response.status}, size={content_length}")

    existing = partial.stat().st_size if partial.exists() else 0
    if existing > EXPECTED_SIZE:
        raise RuntimeError(f"partial download is larger than expected ({existing} > {EXPECTED_SIZE})")
    request_headers = {"User-Agent": "FusionConceptOccuSG-U2/1.0"}
    if existing:
        request_headers["Range"] = f"bytes={existing}-"
    request = urllib.request.Request(URL, headers=request_headers)
    with urllib.request.urlopen(request, timeout=600) as response:
        if existing and response.status == 206:
            mode = "ab"
        elif response.status == 200:
            mode = "wb"
            existing = 0
        else:
            raise RuntimeError(f"unexpected checkpoint response status: {response.status}")
        written = existing
        last_report = time.monotonic()
        with partial.open(mode) as stream:
            while True:
                # Small reads make progress durable even on a slow streaming proxy.
                block = response.read(64 * 1024)
                if not block:
                    break
                stream.write(block)
                written += len(block)
                now = time.monotonic()
                if now - last_report >= 20:
                    print(json.dumps({"downloaded_bytes": written, "expected_bytes": EXPECTED_SIZE}), flush=True)
                    last_report = now
            stream.flush()
            os.fsync(stream.fileno())
    if partial.stat().st_size != EXPECTED_SIZE:
        raise RuntimeError(f"incomplete checkpoint file: {partial.stat().st_size} != {EXPECTED_SIZE}")
    os.replace(partial, target)
    file_sha256 = hash_file(target)
    metadata = {
        "source_url": URL,
        "repository": REPO,
        "revision": REVISION,
        "filename": FILENAME,
        "size_bytes": target.stat().st_size,
        "etag": etag,
        "sha256": file_sha256,
        "downloaded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "download_method": "single resolve URL for pinned file; no repository snapshot download",
    }
    (args.output_dir / "checkpoint_metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

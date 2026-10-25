#!/usr/bin/env python3
"""Resumable download of the exact approved CPython 3.12 CPU torch wheel."""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path

URL = "https://download.pytorch.org/whl/cpu/torch-2.6.0%2Bcpu-cp312-cp312-linux_x86_64.whl"
EXPECTED_SIZE = 178578006
EXPECTED_ETAG = '"c81f6125d383e97601cb613ef250af84"'
NAME = "torch-2.6.0+cpu-cp312-cp312-linux_x86_64.whl"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    output_dir = Path("artifacts/cache/appearance_wheels")
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / NAME
    partial = output_dir / (NAME + ".partial")

    head = urllib.request.Request(URL, method="HEAD", headers={"User-Agent": "FusionConceptOccuSG-U2/1.0"})
    with urllib.request.urlopen(head, timeout=90) as response:
        size = int(response.headers.get("Content-Length", "-1"))
        etag = response.headers.get("ETag")
        accepts_ranges = "bytes" in response.headers.get("Accept-Ranges", "").lower()
    if size != EXPECTED_SIZE or etag != EXPECTED_ETAG or not accepts_ranges:
        raise RuntimeError(f"wheel metadata changed: size={size} etag={etag} ranges={accepts_ranges}")

    attempts = 0
    while not partial.exists() or partial.stat().st_size < EXPECTED_SIZE:
        attempts += 1
        if attempts > 50:
            raise RuntimeError("wheel transfer exceeded 50 resumable attempts")
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > EXPECTED_SIZE:
            raise RuntimeError(f"partial wheel is too large: {offset}")
        headers = {"User-Agent": "FusionConceptOccuSG-U2/1.0"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        req = urllib.request.Request(URL, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                if offset and response.status == 206:
                    content_range = response.headers.get("Content-Range", "")
                    if not content_range.startswith(f"bytes {offset}-"):
                        raise RuntimeError(f"wrong range response: {content_range}")
                    mode = "ab"
                elif response.status == 200:
                    offset = 0
                    mode = "wb"
                else:
                    raise RuntimeError(f"unexpected response status: {response.status}")
                written = offset
                with partial.open(mode) as stream:
                    while True:
                        block = response.read(4 * 1024 * 1024)
                        if not block:
                            break
                        stream.write(block)
                        written += len(block)
                        print(json.dumps({"downloaded_bytes": written, "expected_bytes": EXPECTED_SIZE, "attempt": attempts}), flush=True)
                    stream.flush()
                    os.fsync(stream.fileno())
        except Exception as exc:
            print(json.dumps({"attempt": attempts, "offset_bytes": offset, "retryable_error": f"{type(exc).__name__}: {exc}"}), flush=True)
            time.sleep(min(10, attempts))
    if partial.stat().st_size != EXPECTED_SIZE:
        raise RuntimeError(f"wheel size mismatch: {partial.stat().st_size}")
    os.replace(partial, target)
    metadata = {
        "url": URL,
        "size_bytes": target.stat().st_size,
        "etag": EXPECTED_ETAG,
        "sha256": sha256_file(target),
        "download_attempts": attempts,
    }
    (output_dir / "torch_wheel_metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

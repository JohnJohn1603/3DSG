#!/usr/bin/env python3
"""Refresh hashes in the newly created U4 user-review version manifest."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
version = root / "artifacts/audits/u4_user_review/versions/u4-review-20261008-v1"
manifest_path = version / "manifest.json"
manifest = json.loads(manifest_path.read_text())
manifest["files"] = {
    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(version.iterdir()) if path.is_file() and path.name != "manifest.json"
}
manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
print(json.dumps({"annotation_version": manifest["annotation_version"],
                  "files": manifest["files"]}, indent=2, ensure_ascii=False))

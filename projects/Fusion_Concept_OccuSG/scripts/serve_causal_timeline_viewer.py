#!/usr/bin/env python3
"""Serve a built causal timeline viewer with Python's stdlib HTTP server.

The viewer uses fetch() for immutable timeline/derived assets, so opening the
HTML with file:// is intentionally unsupported.  This server is read-only and
does not mutate the timeline or viewer output.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class ManifestPointerHandler(http.server.SimpleHTTPRequestHandler):
    """Redirect legacy viewer/index.html requests through the D2 pointer."""

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        candidate = Path(self.translate_path(parsed.path)).resolve()
        if candidate.name == "index.html" and candidate.parent.name == "viewer":
            pointer_path = candidate.parent / "d2_viewer_entrypoint.json"
            if pointer_path.is_file():
                try:
                    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
                    if (pointer.get("schema") != "d2-viewer-entrypoint/v1"
                            or pointer.get("status") not in {"ready", "completed", "failed", "cancelled"}):
                        raise ValueError("pointer schema/status is not ready")
                    entrypoint = pointer.get("entrypoint")
                    manifest_name = pointer.get("manifest_path")
                    if (not isinstance(entrypoint, str) or Path(entrypoint).name != entrypoint
                            or not isinstance(manifest_name, str) or Path(manifest_name).name != manifest_name):
                        raise ValueError("pointer contains unsafe paths")
                    manifest_path = candidate.parent / manifest_name
                    entrypoint_path = candidate.parent / entrypoint
                    if not manifest_path.is_file() or not entrypoint_path.is_file():
                        raise ValueError("pointer target is missing")
                    manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                    entry_hash = hashlib.sha256(entrypoint_path.read_bytes()).hexdigest()
                    if manifest_hash != pointer.get("manifest_sha256") or entry_hash != pointer.get("entrypoint_sha256"):
                        raise ValueError("pointer target hash mismatch")
                    location = urlunsplit(("", "", f"{parsed.path.rsplit('/', 1)[0]}/{entrypoint}", parsed.query or "live=1", ""))
                    self.send_response(302)
                    self.send_header("Location", location)
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    payload = f"Viewer entrypoint unavailable: {exc}. Inspect the D2 viewer pointer and run manifest.\n".encode()
                    self.send_response(409)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
        super().do_GET()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True,
                        help="directory that contains the viewer directory")
    parser.add_argument("--viewer", default="timeline_viewer_v1",
                        help="viewer directory under --root")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    root = args.root.resolve()
    viewer = root / args.viewer
    index = viewer / "index.html"
    if not index.is_file():
        raise SystemExit(f"viewer index not found: {index}; build it first")
    handler = functools.partial(ManifestPointerHandler,
                                directory=str(root))
    server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Causal timeline viewer: http://{args.host}:{args.port}/{args.viewer}/index.html",
          flush=True)
    print(f"Serving read-only root: {root}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

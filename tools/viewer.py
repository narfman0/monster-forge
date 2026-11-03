#!/usr/bin/env python3
"""monster-forge viewer: local progress dashboard and three.js asset/animation viewer.

Stdlib only. Serves viewer/ as static files, out/ under /out/, and a live index at
/api/index.json (rebuilt from the registry + out/ on every request).

    python3 -I tools/viewer.py [--port 8787] [--source srd-5.2.1] [--no-open]

The stage-status convention is documented in viewer/README.md and implemented in
stage_status() below; keep the two in sync.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import socketserver
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEWER = ROOT / "viewer"
OUT_ROOT = ROOT / "out"
REGISTRY_DIR = ROOT / "data" / "registry"
STAGES = ("ref", "mesh", "qa", "rig", "anim")
RECORD_KEYS = ("id", "name", "source", "size", "type", "tags", "cr", "body_plan",
               "body_plan_confidence", "rig_template", "animation_set", "scale_m", "speeds")


def load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _ok(stage) -> bool:
    """A manifest stage entry counts when it is a non-empty object without an 'error' key."""
    return isinstance(stage, dict) and bool(stage) and not stage.get("error")


def stage_status(d: Path, man: dict) -> dict:
    """Derive {stages, found_clips, files} for one out/<source>/<id>/ directory."""
    exists = d.is_dir()
    refs = sorted(p.name for p in d.glob("ref_*.png")) if exists else []
    has = (lambda n: (d / n).is_file()) if exists else (lambda n: False)

    ref = has("ref.png") or bool(refs)
    mesh = has("model_raw.glb") or has("model.glb")
    qa = has("model.glb") and _ok(man.get("qa"))

    rig_m = man.get("rig")
    rig = _ok(rig_m) or man.get("has_skeleton") is True

    # Clips: embedded in model.glb (manifest anim.embedded_clips) + anims/<clip>.glb files
    # + anything listed in manifest anim.clips.
    anim_m = man.get("anim") if isinstance(man.get("anim"), dict) else {}
    embedded = [str(c) for c in anim_m.get("embedded_clips", []) or []]
    external = sorted(p.stem for p in (d / "anims").glob("*.glb")) if exists else []
    listed = [str(c) for c in anim_m.get("clips", []) or []]
    found = sorted(set(embedded) | set(external) | set(listed))
    anim = bool(found) and not anim_m.get("error")

    model = "model.glb" if has("model.glb") else ("model_raw.glb" if has("model_raw.glb") else None)
    return {
        "stages": {"ref": ref, "mesh": mesh, "qa": qa, "rig": rig, "anim": anim},
        "found_clips": found,
        "external_clips": external,
        "embedded_clips": embedded,
        "files": {
            "model": model, "thumb": "thumb.png" if has("thumb.png") else None,
            "crop": "crop.png" if has("crop.png") else None,
            "ref": "ref.png" if has("ref.png") else None, "refs": refs,
            "manifest": "manifest.json" if has("manifest.json") else None,
        },
    }


def build_index(source: str) -> dict:
    reg = load_json(REGISTRY_DIR / f"{source}.json")
    monsters = reg.get("monsters", [])
    out = []
    totals = {s: 0 for s in STAGES}
    totals["anim_complete"] = 0
    by_plan: dict[str, dict] = {}
    for rec in monsters:
        d = OUT_ROOT / source / rec["id"]
        man = load_json(d / "manifest.json") if d.is_dir() else {}
        st = stage_status(d, man)
        expected = list(rec.get("animation_set", []))
        missing = [c for c in expected if c not in st["found_clips"]]
        entry = {k: rec.get(k) for k in RECORD_KEYS}
        entry.update(st)
        entry["expected_clips"] = expected
        entry["missing_clips"] = missing
        entry["anim_complete"] = bool(expected) and not missing
        entry["tri_count"] = man.get("tri_count")
        entry["dir"] = f"/out/{source}/{rec['id']}/"
        entry["errors"] = len(man.get("errors", []) or [])
        out.append(entry)
        plan = by_plan.setdefault(rec.get("body_plan", "?"), {"total": 0, **{s: 0 for s in STAGES}})
        plan["total"] += 1
        for s in STAGES:
            if st["stages"][s]:
                totals[s] += 1
                plan[s] += 1
        totals["anim_complete"] += entry["anim_complete"]
    return {
        "source": source,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "stages": list(STAGES),
        "body_plans": reg.get("body_plans", sorted(by_plan)),
        "summary": {"total": len(out), "stages": totals, "by_body_plan": by_plan},
        "monsters": out,
    }


class Handler(http.server.SimpleHTTPRequestHandler):
    source = "srd-5.2.1"
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".js": "text/javascript", ".mjs": "text/javascript",
                      ".glb": "model/gltf-binary", ".json": "application/json"}

    def translate_path(self, path: str) -> str:
        p = urllib.parse.unquote(urllib.parse.urlsplit(path).path)
        if p.startswith("/out/"):
            base, rel = OUT_ROOT, p[len("/out/"):]
        else:
            base, rel = VIEWER, p.lstrip("/")
        target = (base / rel).resolve()
        if base.resolve() not in (target, *target.parents):
            return str(base / "__forbidden__")
        return str(target)

    def do_GET(self):
        if urllib.parse.urlsplit(self.path).path == "/api/index.json":
            body = json.dumps(build_index(self.source)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        # Assets get regenerated in place (--force); never let the browser serve stale ones.
        if self.path.startswith("/out/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        if "--quiet" not in sys.argv:
            super().log_message(fmt, *args)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--source", default="srd-5.2.1")
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    ap.add_argument("--quiet", action="store_true", help="no request log")
    args = ap.parse_args()
    if not (REGISTRY_DIR / f"{args.source}.json").exists():
        print(f"no registry for source {args.source!r}", file=sys.stderr)
        return 2
    Handler.source = args.source
    srv = Server((args.host, args.port), functools.partial(Handler, directory=str(VIEWER)))
    url = f"http://{args.host}:{args.port}/"
    print(f"monster-forge viewer: {url}  (source {args.source}, Ctrl-C to stop)", flush=True)
    if not args.no_open:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""monster-forge viewer: local progress dashboard and three.js asset/animation viewer.

Stdlib only. Serves viewer/ as static files, out/ under /out/, a live index at
/api/index.json (rebuilt from the registry + out/ on every request), per-monster
overrides (GET/POST /api/override/<id>, data/overrides/<source>/<id>.json) and a re-run
job queue (POST /api/run/<id>, GET /api/jobs) that runs tools/forge.py one job at a time.

    python3 -I tools/viewer.py [--port 8787] [--source srd-5.2.1] [--no-open]

Binds 127.0.0.1 only. POSTs must be JSON from a same-origin page.

The stage-status convention is documented in viewer/README.md and implemented in
stage_status() below; keep the two in sync.
"""
from __future__ import annotations

import argparse
import collections
import functools
import http.server
import itertools
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import overrides  # noqa: E402  (tools/overrides.py)

ROOT = Path(__file__).resolve().parent.parent
VIEWER = ROOT / "viewer"
OUT_ROOT = ROOT / "out"
REGISTRY_DIR = ROOT / "data" / "registry"
STAGES = ("ref", "mesh", "qa", "lod", "rig", "anim")
RUN_STAGES = ("ref", "mesh", "qa", "lod")  # what a re-run job may redo
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

    # lod: lods.json lists the chain and every file it names exists.
    lods_m = load_json(d / "lods.json") if has("lods.json") else {}
    lod_files = [v.get("file") for v in (lods_m.get("lods") or {}).values() if isinstance(v, dict)]
    lod = bool(lod_files) and all(has(f) for f in lod_files if f)
    lods = {k: {"file": v.get("file"), "tris": v.get("tris"), "ratio": v.get("ratio")}
            for k, v in sorted((lods_m.get("lods") or {}).items()) if isinstance(v, dict) and v.get("file")
            and has(v["file"])} if lod else {}

    model = "model.glb" if has("model.glb") else ("model_raw.glb" if has("model_raw.glb") else None)
    return {
        "stages": {"ref": ref, "mesh": mesh, "qa": qa, "lod": lod, "rig": rig, "anim": anim},
        "lods": lods,
        "found_clips": found,
        "external_clips": external,
        "embedded_clips": embedded,
        "files": {
            "model": model, "thumb": "thumb.png" if has("thumb.png") else None,
            "thumb_head": "thumb_head.png" if has("thumb_head.png") else None,
            "thumb_ground": "thumb_ground.png" if has("thumb_ground.png") else None,
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
        entry["has_override"] = overrides.path_for(source, rec["id"]).exists()
        entry["job"] = JOBS.state_of(rec["id"])
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


# ---------------------------------------------------------------- re-run jobs
class JobQueue:
    """One forge.py subprocess at a time; FIFO queue; finished jobs kept for inspection."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.jobs: dict[int, dict] = {}
        self.queue: collections.deque[int] = collections.deque()
        self.ids = itertools.count(1)
        self.source = "srd-5.2.1"
        self.proc = None
        threading.Thread(target=self._worker, daemon=True, name="forge-jobs").start()

    def submit(self, mid: str, stages: list[str], force: bool) -> dict:
        with self.cv:
            for j in self.jobs.values():  # the same request already waiting: return it
                if j["mid"] == mid and j["status"] == "queued" and j["stages"] == stages:
                    return self._public(j)
            jid = next(self.ids)
            cmd = [sys.executable, "-I", str(ROOT / "tools" / "forge.py"), "run", mid,
                   "--source", self.source, "--stages", ",".join(stages)] + (["--force"] if force else [])
            j = {"id": jid, "mid": mid, "stages": stages, "force": force, "cmd": cmd, "status": "queued",
                 "queued_at": time.time(), "started_at": None, "ended_at": None, "rc": None,
                 "log": collections.deque(maxlen=400)}
            self.jobs[jid] = j
            self.queue.append(jid)
            self.cv.notify()
            return self._public(j)

    def cancel(self, jid: int) -> dict | None:
        with self.cv:
            j = self.jobs.get(jid)
            if not j:
                return None
            if j["status"] == "queued":
                self.queue.remove(jid)
                j["status"], j["ended_at"] = "cancelled", time.time()
            elif j["status"] == "running" and self.proc is not None:
                j["status"] = "cancelling"
                self.proc.terminate()
            return self._public(j)

    def state_of(self, mid: str) -> str | None:
        with self.lock:
            st = [j["status"] for j in self.jobs.values() if j["mid"] == mid]
        if "running" in st or "cancelling" in st:
            return "running"
        return "queued" if "queued" in st else None

    def snapshot(self, mid: str | None = None, tail: int = 40) -> dict:
        with self.lock:
            js = [self._public(j, tail) for j in self.jobs.values() if mid is None or j["mid"] == mid]
            return {"queue": list(self.queue), "jobs": sorted(js, key=lambda j: -j["id"])[:50]}

    @staticmethod
    def _public(j: dict, tail: int = 40) -> dict:
        out = {k: v for k, v in j.items() if k not in ("log", "cmd")}
        out["cmd"] = " ".join(Path(c).name if i < 3 else c for i, c in enumerate(j["cmd"]))
        out["log_tail"] = list(j["log"])[-tail:]
        return out

    def _worker(self):
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                j = self.jobs[self.queue.popleft()]
                j["status"], j["started_at"] = "running", time.time()
            log_path = ROOT / "out" / self.source / j["mid"] / "forge_job.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(log_path, "w") as lf:
                    lf.write("$ " + " ".join(j["cmd"]) + "\n")
                    proc = subprocess.Popen(j["cmd"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"})
                    with self.lock:
                        self.proc = proc
                    for line in proc.stdout:
                        lf.write(line)
                        lf.flush()
                        with self.lock:
                            j["log"].append(line.rstrip("\n"))
                    rc = proc.wait()
            except Exception as e:  # spawn failure: report on the job, keep the worker alive
                rc = -1
                j["log"].append(f"viewer: {e}")
            with self.lock:
                self.proc = None
                j["rc"], j["ended_at"] = rc, time.time()
                j["status"] = "cancelled" if j["status"] == "cancelling" else ("done" if rc == 0 else "failed")


JOBS = JobQueue()


def registry_ids(source: str) -> set[str]:
    return {m["id"] for m in load_json(REGISTRY_DIR / f"{source}.json").get("monsters", [])}


def registry_record(source: str, mid: str) -> dict | None:
    for m in load_json(REGISTRY_DIR / f"{source}.json").get("monsters", []):
        if m["id"] == mid:
            return m
    return None


def override_view(source: str, mid: str) -> dict:
    """Effective override values for the panel; ref_pick and style defaults come from what
    the last run used (manifest) and the house style file."""
    rec = registry_record(source, mid)
    style = load_json(ROOT / "data" / "style.json").get("name")
    eff = overrides.effective(rec, source, style)
    man = load_json(OUT_ROOT / source / mid / "manifest.json")
    picked = (man.get("ref") or {}).get("picked")
    if picked is not None:
        eff["ref_pick"]["default"] = picked
        if eff["ref_pick"]["override"] is None:
            eff["ref_pick"]["value"] = picked
    refs = sorted((OUT_ROOT / source / mid).glob("ref_*.png"))
    return {"id": mid, "file": str(overrides.path_for(source, mid).relative_to(ROOT)),
            "exists": overrides.path_for(source, mid).exists(), "fields": eff,
            "ref_candidates": len(refs), "run_stages": list(RUN_STAGES)}


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

    def send_json(self, obj, code: int = 200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _mid(self, path: str, prefix: str) -> str | None:
        """The registry id at the end of the path, or None (after sending a 404)."""
        mid = urllib.parse.unquote(path[len(prefix):])
        if not overrides.ID_RE.match(mid) or mid not in registry_ids(self.source):
            self.send_json({"error": f"unknown id {mid!r}"}, 404)
            return None
        return mid

    def _local_ok(self) -> bool:
        """Loopback client, and for POSTs a same-origin JSON request (blocks other sites
        from driving the job queue through the browser)."""
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self.send_json({"error": "loopback only"}, 403)
            return False
        if self.command == "POST":
            port = self.server.server_address[1]
            allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
            host_ok = self.headers.get("Host", "") in {a[len("http://"):] for a in allowed}
            origin = self.headers.get("Origin")
            if not host_ok or (origin is not None and origin not in allowed):
                self.send_json({"error": "cross-origin request refused"}, 403)
                return False
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self.send_json({"error": "Content-Type must be application/json"}, 415)
                return False
        return True

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if not self._local_ok():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/index.json":
            self.send_json(build_index(self.source))
            return
        if path.startswith("/api/override/"):
            mid = self._mid(path, "/api/override/")
            if mid:
                self.send_json(override_view(self.source, mid))
            return
        if path == "/api/jobs":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            mid = (q.get("id") or [None])[0]
            self.send_json(JOBS.snapshot(mid, tail=int((q.get("tail") or ["40"])[0])))
            return
        if path.startswith("/api/"):
            self.send_json({"error": "not found"}, 404)
            return
        super().do_GET()

    def do_POST(self):
        if not self._local_ok():
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            body = self.read_json()
        except ValueError as e:
            self.send_json({"error": f"bad JSON body: {e}"}, 400)
            return
        if path.startswith("/api/override/"):
            mid = self._mid(path, "/api/override/")
            if not mid:
                return
            errs = overrides.validate(body)
            if errs:
                self.send_json({"error": "invalid override", "details": errs}, 400)
                return
            overrides.save(self.source, mid, body)
            self.send_json(override_view(self.source, mid))
            return
        if path.startswith("/api/run/"):
            mid = self._mid(path, "/api/run/")
            if not mid:
                return
            stages = body.get("stages") if isinstance(body, dict) else None
            if not (isinstance(stages, list) and stages and all(s in RUN_STAGES for s in stages)):
                self.send_json({"error": f"stages must be a non-empty subset of {list(RUN_STAGES)}"}, 400)
                return
            stages = [s for s in RUN_STAGES if s in stages]
            self.send_json(JOBS.submit(mid, stages, bool(body.get("force", True))))
            return
        if path.startswith("/api/jobs/") and path.endswith("/cancel"):
            try:
                jid = int(path[len("/api/jobs/"):-len("/cancel")])
            except ValueError:
                jid = -1
            res = JOBS.cancel(jid)
            self.send_json(res if res else {"error": "no such job"}, 200 if res else 404)
            return
        self.send_json({"error": "not found"}, 404)

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
    ap.add_argument("--source", default="srd-5.2.1")
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    ap.add_argument("--quiet", action="store_true", help="no request log")
    ap.add_argument("--out-root", help="serve another out/ tree (testing; re-run jobs still write to out/)")
    args = ap.parse_args()
    if args.out_root:
        global OUT_ROOT
        OUT_ROOT = Path(args.out_root).resolve()
    if not (REGISTRY_DIR / f"{args.source}.json").exists():
        print(f"no registry for source {args.source!r}", file=sys.stderr)
        return 2
    Handler.source = args.source
    JOBS.source = args.source
    srv = Server(("127.0.0.1", args.port), functools.partial(Handler, directory=str(VIEWER)))
    url = f"http://127.0.0.1:{args.port}/"
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

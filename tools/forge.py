#!/usr/bin/env python3
"""monster-forge job runner: reference image -> Pixal3D -> low-poly GLB -> Blender QA.

Stdlib only. Drives the ComfyUI already running on 8188 over its HTTP API and runs
Blender headless for QA. Every stage is skipped when its outputs exist (use --force).

    python3 -I tools/forge.py run goblin-warrior wolf
    python3 -I tools/forge.py ref owlbear --n 3
    python3 -I tools/forge.py mesh owlbear --pick 2 --force
    python3 -I tools/forge.py qa owlbear --yaw 180 --force
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / "workflows"
REGISTRY_DIR = ROOT / "data" / "registry"
OUT_ROOT = ROOT / "out"
BLENDER = os.environ.get("BLENDER", "/usr/bin/blender")
# Any Python with Pillow; the ComfyUI venv has it.
PIL_PYTHON = os.environ.get("PIL_PYTHON", str(Path.home() / "workspace/ComfyUI/.venv/bin/python"))
COMFY = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
CLIENT_ID = f"monster-forge-{uuid.uuid4().hex[:8]}"

# Low-poly budget by size category (triangles).
DECIMATE_BY_SIZE = {"tiny": 2000, "small": 2000, "medium": 3000, "large": 5000,
                    "huge": 6500, "gargantuan": 8000}
# Body plans whose scale_m is a standing height; everything else is nose-to-tail length.
UPRIGHT_PLANS = {"biped", "winged_biped", "centauroid", "merfolk_naga"}
# Plans whose reference prompt asks for a front view (model already faces the camera).
FRONT_VIEW_PLANS = UPRIGHT_PLANS | {"floating"}

REF_PROMPT_PREFIX = "single creature, centered, "
DEFAULT_MESH = {
    "remesh_resolution": 256,
    "remesh_smooth_iters": 2,
    "texture_size": 1024,
    "uv_resolution": 1024,
    "ao_samples": 64,
    "normal_cage": 0.05,
    "upsample": 0,  # 0 = pure 512 pipeline; 1024 = Trellis2UpsampleStage cascade
}


# ---------------------------------------------------------------- helpers
def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_registry(source: str) -> dict[str, dict]:
    data = json.loads((REGISTRY_DIR / f"{source}.json").read_text())
    return {m["id"]: m for m in data["monsters"]}


def out_dir(source: str, mid: str) -> Path:
    d = OUT_ROOT / source / mid
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_manifest(d: Path) -> dict:
    p = d / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_manifest(d: Path, man: dict) -> None:
    tmp = d / "manifest.json.tmp"
    tmp.write_text(json.dumps(man, indent=2, sort_keys=False) + "\n")
    tmp.replace(d / "manifest.json")


def base_seed(mid: str) -> int:
    return zlib.crc32(mid.encode()) & 0x7FFFFFFF


# ---------------------------------------------------------------- ComfyUI API
def http(method: str, path: str, body: bytes | None = None, headers: dict | None = None,
         timeout: float = 60) -> bytes:
    req = urllib.request.Request(COMFY + path, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(path: str):
    return json.loads(http("GET", path))


def wait_idle(poll: float = 5.0) -> None:
    """Wait until nobody else's job is running or pending."""
    announced = False
    while True:
        q = get_json("/queue")
        if not q.get("queue_running") and not q.get("queue_pending"):
            return
        if not announced:
            log("ComfyUI queue busy; waiting for it to drain")
            announced = True
        time.sleep(poll)


def upload_image(path: Path, subfolder: str) -> str:
    boundary = uuid.uuid4().hex
    parts = []
    for name, val in (("subfolder", subfolder), ("type", "input"), ("overwrite", "true")):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{path.name}\"\r\n"
        f"Content-Type: image/png\r\n\r\n".encode() + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    res = json.loads(http("POST", "/upload/image", b"".join(parts),
                          {"Content-Type": f"multipart/form-data; boundary={boundary}"}))
    return f"{res['subfolder']}/{res['name']}" if res.get("subfolder") else res["name"]


def run_prompt(graph: dict, label: str, timeout: float = 1800) -> dict:
    """Submit a graph, poll /history until done, return its outputs dict."""
    wait_idle()
    body = json.dumps({"prompt": graph, "client_id": CLIENT_ID}).encode()
    try:
        res = json.loads(http("POST", "/prompt", body, {"Content-Type": "application/json"}))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{label}: prompt rejected: {e.read().decode()[:2000]}") from None
    pid = res["prompt_id"]
    log(f"{label}: queued {pid}")
    t0 = time.time()
    while True:
        time.sleep(2)
        hist = get_json(f"/history/{pid}")
        if pid in hist:
            entry = hist[pid]
            status = entry.get("status", {})
            if status.get("status_str") == "error" or not status.get("completed", True):
                msgs = [m for m in status.get("messages", []) if m[0] == "execution_error"]
                detail = msgs[0][1] if msgs else status
                if isinstance(detail, dict):
                    detail = {k: detail.get(k) for k in ("node_id", "node_type", "exception_type", "exception_message")}
                raise RuntimeError(f"{label}: execution failed: {detail}")
            return entry.get("outputs", {})
        if time.time() - t0 > timeout:
            raise RuntimeError(f"{label}: timed out after {timeout}s")


def fetch_output(item: dict, dest: Path) -> None:
    q = urllib.parse.urlencode({"filename": item["filename"], "subfolder": item.get("subfolder", ""),
                                "type": item.get("type", "output")})
    dest.write_bytes(http("GET", f"/view?{q}", timeout=300))


def load_workflow(name: str) -> dict:
    return json.loads((WORKFLOWS / name).read_text())


# ---------------------------------------------------------------- stages
def load_prompt_tweaks() -> dict:
    """data/prompt_tweaks.json: {id: {prompt | prepend, negative_append, note}} for re-rolls."""
    p = ROOT / "data" / "prompt_tweaks.json"
    return json.loads(p.read_text()) if p.exists() else {}


def stage_ref(mon: dict, d: Path, args) -> None:
    man = load_manifest(d)
    refs = sorted(d.glob("ref_*.png"))
    if refs and (d / "ref.png").exists() and not args.force:
        log(f"{mon['id']}: ref exists, skip")
        return
    for p in refs:
        p.unlink()
    wf = load_workflow("ref_image.api.json")
    tweak = load_prompt_tweaks().get(mon["id"], {})
    prompt = tweak.get("prompt") or (tweak.get("prepend", "") + REF_PROMPT_PREFIX + mon["reference_prompt"])
    if tweak.get("negative_append"):
        wf["neg"]["inputs"]["text"] += ", " + tweak["negative_append"]
    seed0 = args.seed if args.seed is not None else base_seed(mon["id"])
    wf["pos"]["inputs"]["text"] = prompt
    wf["sample"]["inputs"]["seed"] = seed0
    wf["latent"]["inputs"]["batch_size"] = args.n
    wf["save"]["inputs"]["filename_prefix"] = f"monster-forge/{mon['source']}/{mon['id']}/ref"
    t0 = time.time()
    outputs = run_prompt(wf, f"{mon['id']} ref")
    images = outputs["save"]["images"]
    for i, img in enumerate(images):
        fetch_output(img, d / f"ref_{i}.png")
    pick = min(args.pick or 0, len(images) - 1)
    shutil.copyfile(d / f"ref_{pick}.png", d / "ref.png")
    dt = round(time.time() - t0, 1)
    man.update({"id": mon["id"], "name": mon["name"], "source": mon["source"],
                "size": mon["size"], "body_plan": mon["body_plan"], "scale_m": mon["scale_m"]})
    man["ref"] = {"model": wf["ckpt"]["inputs"]["ckpt_name"], "prompt": prompt,
                  "negative": wf["neg"]["inputs"]["text"], "seed": seed0, "n": len(images),
                  "picked": pick, "steps": wf["sample"]["inputs"]["steps"],
                  "cfg": wf["sample"]["inputs"]["cfg"], "sampler": wf["sample"]["inputs"]["sampler_name"],
                  "size": [1024, 1024], "seconds": dt}
    man.setdefault("timings", {})["ref"] = dt
    save_manifest(d, man)
    log(f"{mon['id']}: ref done in {dt}s ({len(images)} candidates, picked {pick})")


def build_mesh_graph(mon: dict, image_name: str, seed: int, settings: dict) -> dict:
    wf = load_workflow("image_to_lowpoly.api.json")
    wf["load_image"]["inputs"]["image"] = image_name
    wf["ss_sample"]["inputs"]["seed"] = seed
    wf["shape_sample"]["inputs"]["seed"] = seed + 1
    wf["tex_sample"]["inputs"]["seed"] = seed + 2
    wf["remesh"]["inputs"]["resolution"] = settings["remesh_resolution"]
    wf["remesh"]["inputs"]["smooth_iters"] = settings["remesh_smooth_iters"]
    wf["decimate"]["inputs"]["target_face_count"] = settings["target_tris"]
    wf["unwrap"]["inputs"]["resolution"] = settings["uv_resolution"]
    wf["bake_pbr"]["inputs"]["texture_size"] = settings["texture_size"]
    wf["bake_normal"]["inputs"]["resolution"] = settings["texture_size"]
    wf["bake_normal"]["inputs"]["cage_distance"] = settings["normal_cage"]
    wf["bake_ao"]["inputs"]["resolution"] = settings["texture_size"]
    wf["bake_ao"]["inputs"]["samples"] = settings["ao_samples"]
    if settings.get("upsample"):
        # 512 -> N cascade: upsample stage + a second shape sampler, as in the template.
        wf["up_stage"] = {"class_type": "Trellis2UpsampleStage", "inputs": {
            "positive": ["shape_stage", 0], "negative": ["shape_stage", 1],
            "shape_latent": ["shape_sample", 0], "vae": ["shape_vae", 0],
            "target_resolution": settings["upsample"]}}
        wf["up_sample"] = {"class_type": "KSampler", "inputs": {
            "model": ["shape_rescale", 0], "positive": ["up_stage", 0], "negative": ["up_stage", 1],
            "latent_image": ["up_stage", 2], "seed": seed + 3, "steps": 12, "cfg": 7.5,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}}
        wf["shape_decode"]["inputs"]["samples"] = ["up_sample", 0]
        wf["tex_stage"]["inputs"].update({"positive": ["up_stage", 0], "negative": ["up_stage", 1],
                                          "shape_latent": ["up_sample", 0]})
    prefix = f"monster-forge/{mon['source']}/{mon['id']}"
    for nid, node in wf.items():
        fp = node["inputs"].get("filename_prefix")
        if fp is not None:
            node["inputs"]["filename_prefix"] = prefix + "/" + fp.split("/")[-1]
    return wf


def lowpoly_faces(outputs: dict) -> int | None:
    txt = (outputs.get("info_low") or {}).get("text") or [""]
    for line in txt[0].splitlines():
        if line.startswith("Faces:"):
            return int(line.split()[1].replace(",", ""))
    return None


def stage_mesh(mon: dict, d: Path, args) -> None:
    if (d / "model_raw.glb").exists() and not args.force:
        log(f"{mon['id']}: mesh exists, skip")
        return
    ref = d / "ref.png"
    if args.pick is not None and (d / f"ref_{args.pick}.png").exists():
        shutil.copyfile(d / f"ref_{args.pick}.png", ref)
    if not ref.exists():
        raise RuntimeError(f"{mon['id']}: no ref.png; run `ref` first")
    man = load_manifest(d)
    settings = dict(DEFAULT_MESH)
    settings["target_tris"] = args.tris or DECIMATE_BY_SIZE[mon["size"]]
    for k in ("remesh_resolution", "texture_size", "upsample"):
        v = getattr(args, k, None)
        if v is not None:
            settings[k] = v
    seed = args.seed if args.seed is not None else base_seed(mon["id"]) % 100000
    name = upload_image(ref, f"monster-forge/{mon['source']}/{mon['id']}")
    wf = build_mesh_graph(mon, name, seed, settings)
    (d / "mesh.api.json").write_text(json.dumps(wf, indent=1))
    t0 = time.time()
    outputs = run_prompt(wf, f"{mon['id']} mesh")
    # DecimateMesh collapses edges in parallel batches and overshoots non-linearly.
    # Re-run with a corrected target; ComfyUI caches everything upstream of decimate.
    target = settings["target_tris"]
    attempts = []
    for _ in range(3):
        faces = lowpoly_faces(outputs)
        attempts.append([wf["decimate"]["inputs"]["target_face_count"], faces])
        if faces is None or 0.85 * target <= faces <= 1.05 * target:
            break
        wf["decimate"]["inputs"]["target_face_count"] = int(
            wf["decimate"]["inputs"]["target_face_count"] * target / max(faces, 1) * 0.98)
        outputs = run_prompt(wf, f"{mon['id']} mesh retarget")
    dt = round(time.time() - t0, 1)
    fetch_output(outputs["save_glb"]["3d"][0], d / "model_raw.glb")
    tex = d / "textures"
    tex.mkdir(exist_ok=True)
    for nid, fname in (("save_base", "base_color"), ("save_metal", "metallic"), ("save_rough", "roughness"),
                       ("save_normal", "normal"), ("save_ao", "ao"), ("save_crop", "crop")):
        if nid in outputs:
            fetch_output(outputs[nid]["images"][0], (d if fname == "crop" else tex) / f"{fname}.png")
    info = {nid: outputs[nid]["text"][0] for nid in ("info_hi", "info_remesh", "info_low")
            if nid in outputs and outputs[nid].get("text")}
    man["mesh"] = {"model": wf["unet"]["inputs"]["unet_name"], "seed": seed, "settings": settings,
                   "decimate_attempts": attempts, "mesh_info": info, "seconds": dt,
                   "ref_picked": (args.pick if args.pick is not None else man.get("ref", {}).get("picked"))}
    man.setdefault("timings", {})["mesh"] = dt
    man.pop("qa", None)
    save_manifest(d, man)
    for stale in ("model.glb", "thumb.png"):
        (d / stale).unlink(missing_ok=True)
    log(f"{mon['id']}: mesh done in {dt}s")


def stage_qa(mon: dict, d: Path, args) -> None:
    if (d / "thumb.png").exists() and (d / "model.glb").exists() and not args.force:
        log(f"{mon['id']}: qa exists, skip")
        return
    if not (d / "model_raw.glb").exists():
        raise RuntimeError(f"{mon['id']}: no model_raw.glb; run `mesh` first")
    man = load_manifest(d)
    yaw = args.yaw if args.yaw is not None else man.get("qa", {}).get("yaw_override")
    job = {
        "src": str(d / "model_raw.glb"), "dst": str(d / "model.glb"), "thumb": str(d / "thumb.png"),
        "report": str(d / "qa.json"), "scale_m": mon["scale_m"],
        "scale_axis": "height" if mon["body_plan"] in UPRIGHT_PLANS else "length",
        "front_view": mon["body_plan"] in FRONT_VIEW_PLANS, "yaw_override": yaw,
        "forward": args.forward, "label": f"{mon['name']} ({mon['body_plan']})",
    }
    (d / "qa.json").unlink(missing_ok=True)
    t0 = time.time()
    proc = subprocess.run([BLENDER, "-b", "--factory-startup", "-P", str(ROOT / "tools" / "blender_qa.py"),
                           "--", json.dumps(job)], capture_output=True, text=True, timeout=600)
    dt = round(time.time() - t0, 1)
    if proc.returncode != 0 or not (d / "qa.json").exists():
        sys.stderr.write(proc.stdout[-4000:] + proc.stderr[-4000:])
        raise RuntimeError(f"{mon['id']}: blender QA failed (rc={proc.returncode})")
    rep = json.loads((d / "qa.json").read_text())
    rep["seconds"] = dt
    if yaw is not None:
        rep["yaw_override"] = yaw
    man["qa"] = rep
    man["tri_count"] = rep["tris"]
    man.setdefault("timings", {})["qa"] = dt
    save_manifest(d, man)
    log(f"{mon['id']}: qa done in {dt}s, {rep['tris']} tris, dims {rep['dims_m']}")


STAGES = {"ref": stage_ref, "mesh": stage_mesh, "qa": stage_qa}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["ref", "mesh", "qa", "run", "contact"])
    ap.add_argument("ids", nargs="+", help="monster ids (registry slugs)")
    ap.add_argument("--source", default="srd-5.2.1")
    ap.add_argument("--force", action="store_true", help="redo stages whose outputs exist")
    ap.add_argument("--seed", type=int, help="override the per-monster seed")
    ap.add_argument("--n", type=int, default=1, help="reference candidates to generate")
    ap.add_argument("--pick", type=int, help="which ref_<i>.png feeds the mesh stage")
    ap.add_argument("--tris", type=int, help="override the decimate target")
    ap.add_argument("--remesh-resolution", dest="remesh_resolution", type=int)
    ap.add_argument("--texture-size", dest="texture_size", type=int)
    ap.add_argument("--upsample", type=int, help="0 = 512 only, 1024 = cascade upsample")
    ap.add_argument("--yaw", type=float, help="QA: degrees to rotate about up (overrides auto facing)")
    ap.add_argument("--name", default="contact", help="contact: sheet name (_<name>.png)")
    ap.add_argument("--forward", default="-Z", choices=["-Z", "+Z"], help="glTF facing direction")
    args = ap.parse_args()

    reg = load_registry(args.source)
    if args.cmd == "contact":
        dst = OUT_ROOT / args.source / f"_{args.name}.png"
        dirs = [str(OUT_ROOT / args.source / mid) for mid in args.ids]
        return subprocess.run([PIL_PYTHON, "-I", str(ROOT / "tools" / "contact_sheet.py"), str(dst), *dirs]).returncode
    stages = ["ref", "mesh", "qa"] if args.cmd == "run" else [args.cmd]
    failures = []
    for mid in args.ids:
        if mid not in reg:
            log(f"unknown id {mid!r} in {args.source}")
            failures.append(mid)
            continue
        mon = reg[mid]
        d = out_dir(args.source, mid)
        for st in stages:
            try:
                STAGES[st](mon, d, args)
            except Exception as e:  # keep going with the next monster
                log(f"{mid}: {st} FAILED: {e}")
                man = load_manifest(d)
                man.setdefault("errors", []).append({"stage": st, "error": str(e)[:2000],
                                                     "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
                save_manifest(d, man)
                failures.append(mid)
                break
    if failures:
        log(f"failed: {' '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

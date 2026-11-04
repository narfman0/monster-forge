#!/usr/bin/env python3
"""monster-forge job runner: reference image -> Pixal3D -> low-poly GLB -> Blender QA
-> multiview paint -> LOD chain -> rig -> walk clip.

Stdlib only. Drives the ComfyUI already running on 8188 over its HTTP API and runs
Blender headless for QA. Every stage is skipped when its outputs exist (use --force).

    python3 -I tools/forge.py run goblin-warrior wolf
    python3 -I tools/forge.py ref owlbear --n 3
    python3 -I tools/forge.py mesh owlbear --pick 2 --force
    python3 -I tools/forge.py qa owlbear --yaw 180 --force
    python3 -I tools/forge.py run harpy --stages mesh,qa,lod --force
    python3 -I tools/forge.py lod wolf
    python3 -I tools/forge.py rig wolf --rig-reroll
    python3 -I tools/forge.py paint harpy --force      # then: lod harpy --force

Per-monster settings live in data/overrides/<source>/<id>.json (tools/overrides.py);
command-line flags win over them.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import overrides  # noqa: E402  (tools/overrides.py: per-monster overrides, budgets)
import paint  # noqa: E402  (tools/paint.py: multiview paint stage)
import rig  # noqa: E402  (tools/rig.py: SkinTokens rig stage)
import anim  # noqa: E402  (tools/anim.py: procedural clips)

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / "workflows"
REGISTRY_DIR = ROOT / "data" / "registry"
OUT_ROOT = ROOT / "out"
BLENDER = os.environ.get("BLENDER", "/usr/bin/blender")
# Any Python with Pillow; the ComfyUI venv has it.
PIL_PYTHON = os.environ.get("PIL_PYTHON", str(Path.home() / "workspace/ComfyUI/.venv/bin/python"))
COMFY = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
CLIENT_ID = f"monster-forge-{uuid.uuid4().hex[:8]}"

# LOD0 budget by size category (triangles). See docs/lowpoly-density.md and docs/lods.md.
DECIMATE_BY_SIZE = overrides.TRI_BUDGET_BY_SIZE
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
    "normal_cage": 0.015,  # fraction of the bbox diagonal; 0.05 grabbed across features
    # 1024 = Trellis2UpsampleStage cascade (default: LOD0 quality, sharper voxel colour on
    # faces); 0 = pure 512 pipeline, the automatic fallback when a 1024 run fails.
    "upsample": 1024,
    # importance-weighted decimation (tools/blender_decimate.py)
    "vg_factor": 0.5,
    "w_curv": 0.6,
    "w_head": 0.7,
    "w_ext": 0.35,
}
# Sizes that get 2048 textures/UV atlas instead of 1024.
BIG_TEXTURE_SIZES = overrides.BIG_TEXTURE_SIZES
LOD_FILES = ("lods.json", "model_lod1.glb", "model_lod2.glb", "model_lod3.glb")


def ov_for(mon: dict) -> dict:
    """data/overrides/<source>/<id>.json, validated (a bad file fails the stage loudly)."""
    ov = overrides.load(mon["source"], mon["id"])
    errs = overrides.validate(ov)
    if errs:
        raise RuntimeError(f"{overrides.path_for(mon['source'], mon['id'])}: {'; '.join(errs)}")
    return ov


def clear_lods(d: Path) -> None:
    for f in d.glob("model_lod*.glb"):
        f.unlink()
    (d / "lods.json").unlink(missing_ok=True)


# ---------------------------------------------------------------- helpers
def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_registry(source: str) -> dict[str, dict]:
    data = json.loads((REGISTRY_DIR / f"{source}.json").read_text())
    return {m["id"]: m for m in data["monsters"]}


def out_dir(source: str, mid: str, suffix: str = "") -> Path:
    """out/<source>/<id><suffix>/. A suffixed (experiment) dir is seeded with the base
    dir's reference images and manifest so `mesh` can run without redoing `ref`."""
    d = OUT_ROOT / source / (mid + suffix)
    d.mkdir(parents=True, exist_ok=True)
    base = OUT_ROOT / source / mid
    if suffix and base.is_dir() and not (d / "ref.png").exists():
        for p in base.glob("ref*.png"):
            shutil.copyfile(p, d / p.name)
        if (base / "manifest.json").exists() and not (d / "manifest.json").exists():
            man = load_manifest(base)
            save_manifest(d, {k: man[k] for k in ("id", "name", "source", "size", "body_plan",
                                                  "scale_m", "ref", "timings") if k in man})
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


class ResourceSampler:
    """Peak system RAM, swap, ComfyUI RSS and VRAM while a stage runs (1 s polling).
    RAM/swap from /proc/meminfo, VRAM from ComfyUI's /system_stats (whole device, so other
    GPU users are included), RSS of the process listening as ComfyUI (main.py)."""

    def __init__(self, interval: float = 1.0):
        self.interval, self.peak, self._stop = interval, {}, threading.Event()
        self.comfy_pid = self._find_comfy()
        self.thread = threading.Thread(target=self._run, daemon=True)

    @staticmethod
    def _find_comfy() -> int | None:
        for p in Path("/proc").iterdir():
            if not p.name.isdigit():
                continue
            try:
                cmd = (p / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if any(c.endswith(b"main.py") for c in cmd) and b"8188" in b" ".join(cmd):
                return int(p.name)
        return None

    def sample(self) -> dict:
        out = {}
        try:
            mi = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in Path("/proc/meminfo").read_text().splitlines()}
            out["ram_used_gb"] = (mi["MemTotal"] - mi["MemAvailable"]) / 2**30
            out["swap_used_gb"] = (mi["SwapTotal"] - mi["SwapFree"]) / 2**30
            out["ram_total_gb"] = mi["MemTotal"] / 2**30
        except (OSError, KeyError, ValueError):
            pass
        if self.comfy_pid:
            try:
                for l in Path(f"/proc/{self.comfy_pid}/status").read_text().splitlines():
                    if l.startswith("VmRSS:"):
                        out["comfy_rss_gb"] = int(l.split()[1]) * 1024 / 2**30
            except OSError:
                pass
        try:
            dev = get_json("/system_stats")["devices"][0]
            out["vram_used_gb"] = (dev["vram_total"] - dev["vram_free"]) / 2**30
            out["vram_total_gb"] = dev["vram_total"] / 2**30
        except Exception:
            pass
        return out

    def _run(self):
        while not self._stop.is_set():
            for k, v in self.sample().items():
                self.peak[k] = max(self.peak.get(k, 0.0), v)
            self._stop.wait(self.interval)

    def __enter__(self):
        self.t0 = time.time()
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self.thread.join(timeout=5)
        self.result = {"peak_" + k if not k.endswith("total_gb") else k: round(v, 2) for k, v in self.peak.items()}
        self.result["seconds"] = round(time.time() - self.t0, 1)
        return False


def upload_file(path: Path, subfolder: str, name: str | None = None,
                content_type: str = "image/png") -> str:
    """Upload into ComfyUI's input dir (any file type; /upload/image does not check)."""
    boundary = uuid.uuid4().hex
    parts = []
    for name, val in (("subfolder", subfolder), ("type", "input"), ("overwrite", "true")):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name or path.name}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n".encode() + path.read_bytes() + b"\r\n")
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


def load_style(path: str | None) -> dict:
    """House art style (data/style.json by default); '' or 'none' = unstyled pilot prompt."""
    if not path or path.lower() == "none":
        return {}
    p = Path(path)
    return json.loads((p if p.is_absolute() else ROOT / p).read_text())


def apply_style(wf: dict, style: dict, prompt: str, mon: dict) -> str:
    """Patch the ref graph with the style's sampler, LoRAs, negative and (for upright plans)
    an OpenPose A-pose ControlNet. Returns the styled positive prompt."""
    for s in style.get("strip_from_subject", []):
        prompt = prompt.replace(s, "")
    prompt = " ".join(prompt.split()).strip().rstrip(",. ")
    if style.get("positive_suffix"):
        prompt += ", " + style["positive_suffix"]
    if style.get("negative"):
        wf["neg"]["inputs"]["text"] += ", " + style["negative"]
    if style.get("checkpoint"):
        wf["ckpt"]["inputs"]["ckpt_name"] = style["checkpoint"]
    smp = wf["sample"]["inputs"]
    for key, field in (("cfg", "cfg"), ("steps", "steps"), ("sampler", "sampler_name"), ("scheduler", "scheduler")):
        if key in style:
            smp[field] = style[key]
    for key in ("width", "height"):
        if key in style:
            wf["latent"]["inputs"][key] = style[key]
    model, clip = ["ckpt", 0], ["ckpt", 1]
    for i, lora in enumerate(style.get("lora") or []):
        wf[f"lora{i}"] = {"class_type": "LoraLoader", "inputs": {
            "model": model, "clip": clip, "lora_name": lora["file"],
            "strength_model": lora["strength"], "strength_clip": lora.get("strength_clip", lora["strength"])}}
        model, clip = [f"lora{i}", 0], [f"lora{i}", 1]
    smp["model"] = model
    wf["pos"]["inputs"]["clip"] = clip
    wf["neg"]["inputs"]["clip"] = clip
    cn = style.get("controlnet")
    if cn and mon["body_plan"] in cn.get("body_plans", []):
        imgs = cn["pose_images"]
        img = ROOT / imgs.get(mon["size"], imgs["default"])
        wf["cn_img"] = {"class_type": "LoadImage", "inputs": {
            "image": upload_file(img, "monster-forge/pose")}}
        wf["cn_load"] = {"class_type": "ControlNetLoader", "inputs": {"control_net_name": cn["model"]}}
        wf["cn_type"] = {"class_type": "SetUnionControlNetType", "inputs": {
            "control_net": ["cn_load", 0], "type": cn.get("type", "openpose")}}
        wf["cn_apply"] = {"class_type": "ControlNetApplyAdvanced", "inputs": {
            "positive": ["pos", 0], "negative": ["neg", 0], "control_net": ["cn_type", 0],
            "image": ["cn_img", 0], "strength": cn["strength"], "start_percent": cn.get("start", 0.0),
            "end_percent": cn.get("end", 1.0), "vae": ["ckpt", 2]}}
        smp["positive"], smp["negative"] = ["cn_apply", 0], ["cn_apply", 1]
    return prompt


def resolve_style(name: str | None) -> str:
    """Style CLI/override value -> path for load_style: None = the house style
    (data/style.json), 'none' = unstyled, a .json path, or a style name (data/styles/<name>.json,
    or the house style's own "name")."""
    if not name:
        return "data/style.json"
    if name.lower() == "none" or name.endswith(".json"):
        return name
    if (ROOT / "data" / "styles" / f"{name}.json").exists():
        return f"data/styles/{name}.json"
    house = ROOT / "data" / "style.json"
    if house.exists() and json.loads(house.read_text()).get("name") == name:
        return "data/style.json"
    raise RuntimeError(f"unknown style {name!r} (no data/styles/{name}.json)")


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
    ov = ov_for(mon)
    if ov.get("prompt_extra"):
        prompt = prompt.rstrip(" ,.") + ", " + ov["prompt_extra"]
    style = load_style(resolve_style(args.style or ov.get("style")))
    if style:
        prompt = apply_style(wf, style, prompt, mon)
    if mon.get("unarmed_reference") and style.get("unarmed_suffix"):
        # weapons are runtime props (docs/equipment.md): draw empty, open hands
        prompt = prompt.rstrip(" ,.") + ", " + style["unarmed_suffix"]
        if style.get("unarmed_negative"):
            wf["neg"]["inputs"]["text"] += ", " + style["unarmed_negative"]
    if tweak.get("negative_append"):
        wf["neg"]["inputs"]["text"] += ", " + tweak["negative_append"]
    if ov.get("negative_extra"):
        wf["neg"]["inputs"]["text"] += ", " + ov["negative_extra"]
    seed0 = args.seed if args.seed is not None else ov.get("seed_ref", base_seed(mon["id"]))
    wf["pos"]["inputs"]["text"] = prompt
    wf["sample"]["inputs"]["seed"] = seed0
    wf["latent"]["inputs"]["batch_size"] = args.n
    wf["save"]["inputs"]["filename_prefix"] = f"monster-forge/{mon['source']}/{mon['id']}/ref"
    t0 = time.time()
    outputs = run_prompt(wf, f"{mon['id']} ref")
    images = outputs["save"]["images"]
    for i, img in enumerate(images):
        fetch_output(img, d / f"ref_{i}.png")
    pick = min(args.pick if args.pick is not None else ov.get("ref_pick", 0), len(images) - 1)
    shutil.copyfile(d / f"ref_{pick}.png", d / "ref.png")
    dt = round(time.time() - t0, 1)
    man.update({"id": mon["id"], "name": mon["name"], "source": mon["source"],
                "size": mon["size"], "body_plan": mon["body_plan"], "scale_m": mon["scale_m"]})
    man["ref"] = {"model": wf["ckpt"]["inputs"]["ckpt_name"], "prompt": prompt,
                  "negative": wf["neg"]["inputs"]["text"], "seed": seed0, "n": len(images),
                  "picked": pick, "steps": wf["sample"]["inputs"]["steps"],
                  "cfg": wf["sample"]["inputs"]["cfg"], "sampler": wf["sample"]["inputs"]["sampler_name"],
                  "size": [wf["latent"]["inputs"]["width"], wf["latent"]["inputs"]["height"]],
                  "style": style.get("name") if style else None,
                  "lora": [[n["inputs"]["lora_name"], n["inputs"]["strength_model"]]
                           for k, n in sorted(wf.items()) if k.startswith("lora")],
                  "controlnet": ({"image": wf["cn_img"]["inputs"]["image"],
                                  "strength": wf["cn_apply"]["inputs"]["strength"]}
                                 if "cn_apply" in wf else None),
                  "seconds": dt}
    man.setdefault("timings", {})["ref"] = dt
    save_manifest(d, man)
    log(f"{mon['id']}: ref done in {dt}s ({len(images)} candidates, picked {pick})")


def build_mesh_graph(mon: dict, image_name: str, seed: int, settings: dict, dirname: str = "") -> dict:
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
    prefix = f"monster-forge/{mon['source']}/{dirname or mon['id']}"
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


OUTPUT_NODES = ("save_glb", "save_base", "save_metal", "save_rough", "save_normal", "save_ao",
                "save_crop", "info_hi", "info_remesh", "info_low")


def prune_graph(wf: dict, outputs) -> dict:
    """Keep only the nodes the given output nodes depend on."""
    keep, stack = set(), [o for o in outputs if o in wf]
    while stack:
        nid = stack.pop()
        if nid in keep:
            continue
        keep.add(nid)
        for v in wf[nid]["inputs"].values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] in wf:
                stack.append(v[0])
    return {k: v for k, v in wf.items() if k in keep}


def hi_poly_graph(wf: dict) -> dict:
    """Pass 1 of the weighted path: generation + remesh, saving the remeshed high-poly."""
    g = copy.deepcopy(wf)
    prefix = g["save_glb"]["inputs"]["filename_prefix"].rsplit("/", 1)[0]
    g["save_hi"] = {"class_type": "SaveGLB", "inputs": {"mesh": ["remesh", 0], "filename_prefix": prefix + "/hi"}}
    return prune_graph(g, ("save_hi", "save_crop", "info_hi", "info_remesh"))


def bake_from_file_graph(wf: dict, lo_name: str) -> dict:
    """Pass 2: the same graph with DecimateMesh replaced by a GLB loaded from ComfyUI's input
    dir. Every node upstream of the remesh has identical inputs, so ComfyUI serves it from
    cache and only unwrap + bakes run."""
    g = copy.deepcopy(wf)
    g["load_lo"] = {"class_type": "Load3DAdvanced", "inputs": {
        "model_file": lo_name, "viewport_state": {}, "width": 1024, "height": 1024}}
    g["lo_mesh"] = {"class_type": "Get3DComponents", "inputs": {"model_3d": ["load_lo", 0]}}
    g["smooth"]["inputs"]["mesh"] = ["lo_mesh", 0]
    del g["decimate"]
    return prune_graph(g, OUTPUT_NODES)


def blender_decimate(mon: dict, d: Path, settings: dict) -> dict:
    job = {"src": str(d / "model_hi.glb"), "dst": str(d / "model_lo.glb"), "report": str(d / "decimate.json"),
           "target_tris": settings["target_tris"], "body_plan": mon["body_plan"],
           "vg_factor": settings["vg_factor"], "w_curv": settings["w_curv"], "w_head": settings["w_head"],
           "w_ext": settings["w_ext"], "heatmap": str(d / "importance.png"), "topo": True}
    (d / "decimate.json").unlink(missing_ok=True)
    proc = subprocess.run([BLENDER, "-b", "--factory-startup", "-P", str(ROOT / "tools" / "blender_decimate.py"),
                           "--", json.dumps(job)], capture_output=True, text=True, timeout=900)
    if proc.returncode != 0 or not (d / "decimate.json").exists():
        sys.stderr.write(proc.stdout[-4000:] + proc.stderr[-4000:])
        raise RuntimeError(f"{mon['id']}: blender decimate failed (rc={proc.returncode})")
    return json.loads((d / "decimate.json").read_text())


def mesh_settings(mon: dict, args) -> dict:
    settings = dict(DEFAULT_MESH)
    settings["decimator"] = args.decimator
    settings["target_tris"] = args.tris or DECIMATE_BY_SIZE[mon["size"]]
    if mon["size"] in BIG_TEXTURE_SIZES:
        settings["texture_size"] = settings["uv_resolution"] = 2048
    ov = ov_for(mon)
    if ov.get("tri_budget") and not args.tris:
        settings["target_tris"] = ov["tri_budget"]
    for k in ("vg_factor", "upsample", "normal_cage", "texture_size"):
        if ov.get(k) is not None:
            settings[k] = ov[k]
    if ov.get("texture_size"):
        settings["uv_resolution"] = ov["texture_size"]
    settings["overrides"] = {k: v for k, v in ov.items() if k != "notes"}
    for k in ("remesh_resolution", "texture_size", "uv_resolution", "normal_cage", "upsample", "vg_factor"):
        v = getattr(args, k, None)
        if v is not None:
            settings[k] = v
    if args.texture_size is not None and args.uv_resolution is None:
        settings["uv_resolution"] = args.texture_size
    return settings


def stage_mesh(mon: dict, d: Path, args) -> None:
    if (d / "model_raw.glb").exists() and not args.force:
        log(f"{mon['id']}: mesh exists, skip")
        return
    ref = d / "ref.png"
    ov = ov_for(mon)
    pick = args.pick if args.pick is not None else ov.get("ref_pick")
    if pick is not None and (d / f"ref_{pick}.png").exists():
        shutil.copyfile(d / f"ref_{pick}.png", ref)
    if not ref.exists():
        raise RuntimeError(f"{mon['id']}: no ref.png; run `ref` first")
    man = load_manifest(d)
    settings = mesh_settings(mon, args)
    seed = args.seed if args.seed is not None else ov.get("seed_mesh", base_seed(mon["id"]) % 100000)
    sub = f"monster-forge/{mon['source']}/{d.name}"
    name = upload_file(ref, sub)
    wf = build_mesh_graph(mon, name, seed, settings, d.name)
    target = settings["target_tris"]
    attempts, times = [], {}
    t0 = time.time()
    dec, hi_out, used = None, {}, settings["decimator"]
    if settings["decimator"] == "weighted":
        hi_out = run_prompt(hi_poly_graph(wf), f"{mon['id']} mesh (hi-poly)")
        fetch_output(hi_out["save_hi"]["3d"][0], d / "model_hi.glb")
        times["generate"] = round(time.time() - t0, 1)
        t1 = time.time()
        dec = blender_decimate(mon, d, settings)
        times["decimate"] = round(time.time() - t1, 1)
        if dec.get("fallback"):
            used = "comfy (fallback: target below 1.6 x topology floor %d)" % dec["topology_floor"]
            log(f"{mon['id']}: {used}")
        else:
            log(f"{mon['id']}: decimated {dec['faces_in']} -> {dec['faces_out']} faces "
                f"(floor {dec['topology_floor']}), non-manifold edges lo {dec['topo_lo']['multi']}")
            t1 = time.time()
            lo = d / "model_lo.glb"
            digest = zlib.crc32(lo.read_bytes()) & 0xFFFFFFFF  # Load3D has no change check: new name per mesh
            lo_name = upload_file(lo, "3d/" + sub, f"lo_{digest:08x}.glb", "model/gltf-binary")
            wf = bake_from_file_graph(wf, lo_name)
            (d / "mesh.api.json").write_text(json.dumps(wf, indent=1))
            outputs = run_prompt(wf, f"{mon['id']} mesh (bake)")
            times["bake"] = round(time.time() - t1, 1)
            attempts.append([target, lowpoly_faces(outputs)])
            lo.unlink(missing_ok=True)
    if not used.startswith("weighted"):
        (d / "mesh.api.json").write_text(json.dumps(wf, indent=1))
        outputs = run_prompt(wf, f"{mon['id']} mesh")
        # DecimateMesh collapses edges in parallel batches and overshoots non-linearly.
        # Re-run with a corrected target; ComfyUI caches everything upstream of decimate.
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
        src = outputs.get(nid) or hi_out.get(nid)
        if src:
            fetch_output(src["images"][0], (d if fname == "crop" else tex) / f"{fname}.png")
    info = {}
    for nid in ("info_hi", "info_remesh", "info_low"):
        src = outputs.get(nid) or hi_out.get(nid)
        if src and src.get("text"):
            info[nid] = src["text"][0]
    man["mesh"] = {"model": wf["unet"]["inputs"]["unet_name"], "seed": seed, "settings": settings,
                   "decimate_attempts": attempts, "mesh_info": info, "seconds": dt,
                   "ref_picked": (pick if pick is not None else man.get("ref", {}).get("picked"))}
    man["mesh"]["decimator_used"] = used
    if dec is not None:
        man["mesh"]["weighted_decimate"] = dec
        man["mesh"]["phase_seconds"] = times
    man.setdefault("timings", {})["mesh"] = dt
    man.pop("qa", None)
    paint.clear_paint(d, man)
    rig.clear_rig(d, man)
    save_manifest(d, man)
    clear_lods(d)
    for stale in ("model.glb", "thumb.png", "thumb_head.png", "thumb_ground.png"):
        (d / stale).unlink(missing_ok=True)
    log(f"{mon['id']}: mesh done in {dt}s ({used}, {attempts[-1][1]} faces)")


def stage_mesh_guarded(mon: dict, d: Path, args) -> None:
    """stage_mesh with peak RAM/VRAM sampling (manifest mesh.resources) and, when the 1024
    upsample cascade fails (OOM, ComfyUI error, timeout), one retry at upsample 0
    (manifest mesh.upsample_fallback). An explicit --upsample is never second-guessed."""
    if (d / "model_raw.glb").exists() and not args.force:
        log(f"{mon['id']}: mesh exists, skip")
        return
    fallback = None
    with ResourceSampler() as rs:
        try:
            stage_mesh(mon, d, args)
        except Exception as e:
            ups = args.upsample if args.upsample is not None else mesh_settings(mon, args).get("upsample")
            if not ups or args.upsample is not None:
                raise
            fallback = {"from": ups, "to": 0, "error": str(e)[:1000]}
            log(f"{mon['id']}: mesh at upsample {ups} failed ({str(e)[:200]}); retrying at upsample 0")
            retry = copy.copy(args)
            retry.upsample, retry.force = 0, True
            stage_mesh(mon, d, retry)
    man = load_manifest(d)
    man.setdefault("mesh", {})["resources"] = rs.result
    if fallback:
        man["mesh"]["upsample_fallback"] = fallback
    save_manifest(d, man)
    r = rs.result
    log(f"{mon['id']}: mesh peak RAM {r.get('peak_ram_used_gb')} GB (ComfyUI RSS {r.get('peak_comfy_rss_gb')}), "
        f"swap {r.get('peak_swap_used_gb')} GB, VRAM {r.get('peak_vram_used_gb')} GB, {r['seconds']} s")


def stage_qa(mon: dict, d: Path, args) -> None:
    if all((d / f).exists() for f in ("thumb.png", "thumb_head.png", "thumb_ground.png", "model.glb")) \
            and not args.force:
        log(f"{mon['id']}: qa exists, skip")
        return
    if not (d / "model_raw.glb").exists():
        raise RuntimeError(f"{mon['id']}: no model_raw.glb; run `mesh` first")
    man = load_manifest(d)
    ov = ov_for(mon)
    # yaw: flag > override file > the pin persisted by an earlier `qa --yaw`
    yaw = args.yaw if args.yaw is not None else ov.get("yaw", man.get("qa", {}).get("yaw_override"))
    job = {
        "src": str(d / "model_raw.glb"), "dst": str(d / "model.glb"), "thumb": str(d / "thumb.png"),
        "thumb_head": str(d / "thumb_head.png"), "body_plan": mon["body_plan"],
        "report": str(d / "qa.json"), "scale_m": mon["scale_m"],
        "scale_axis": "height" if mon["body_plan"] in UPRIGHT_PLANS else "length",
        "front_view": mon["body_plan"] in FRONT_VIEW_PLANS, "yaw_override": yaw,
        "forward": args.forward or ov.get("forward") or "-Z", "label": f"{mon['name']} ({mon['body_plan']})",
        "thumb_ground": str(d / "thumb_ground.png"), "id": mon["id"], "source": mon["source"],
        # stance leveling: flags > override file (read by blender_qa.py) > body-plan defaults
        "level": False if args.no_level else None, "level_tilt": args.level_tilt,
    }
    clear_lods(d)  # model.glb changes: the LOD chain is stale
    paint.clear_paint(d, man)  # ... and QA re-exports the voxel albedo: paint is stale too
    rig.clear_rig(d, man)  # ... and the skeleton was fitted to the old frame
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
    man.pop("lod", None)
    man.setdefault("timings", {})["qa"] = dt
    save_manifest(d, man)
    lv = rep.get("level", {})
    log(f"{mon['id']}: qa done in {dt}s, {rep['tris']} tris, dims {rep['dims_m']}, "
        f"level {lv.get('method')} tilt {lv.get('tilt_deg')} deg")


def stage_lod(mon: dict, d: Path, args) -> None:
    """LOD1-3 from model.glb (tools/blender_lods.py); see docs/lods.md."""
    lods = d / "lods.json"
    if lods.exists() and not args.force:
        files = [v.get("file") for v in json.loads(lods.read_text()).get("lods", {}).values()]
        if all((d / f).exists() for f in files if f):
            log(f"{mon['id']}: lod exists, skip")
            return
    if not (d / "model.glb").exists():
        raise RuntimeError(f"{mon['id']}: no model.glb; run `qa` first")
    ov = ov_for(mon)
    ratios = ov.get("lod_ratios") or overrides.LOD_RATIOS
    clear_lods(d)
    rig.clear_rig_outputs(d)
    job = {"src": str(d / "model.glb"), "out_dir": str(d), "report": str(lods),
           "ratios": {str(i + 1): r for i, r in enumerate(ratios)}, "min_tris": 300}
    t0 = time.time()
    proc = subprocess.run([BLENDER, "-b", "--factory-startup", "-P", str(ROOT / "tools" / "blender_lods.py"),
                           "--", json.dumps(job)], capture_output=True, text=True, timeout=600)
    dt = round(time.time() - t0, 1)
    if proc.returncode != 0 or not lods.exists():
        sys.stderr.write(proc.stdout[-4000:] + proc.stderr[-4000:])
        raise RuntimeError(f"{mon['id']}: blender LODs failed (rc={proc.returncode})")
    rep = json.loads(lods.read_text())
    man = load_manifest(d)
    man["lod"] = {"seconds": dt, "ratios": ratios,
                  "tris": {k: v["tris"] for k, v in rep["lods"].items()}}
    man.setdefault("timings", {})["lod"] = dt
    save_manifest(d, man)
    log(f"{mon['id']}: lod done in {dt}s, tris {man['lod']['tris']}")


def stage_paint(mon: dict, d: Path, args) -> None:
    """Multiview SDXL paint of LOD0's albedo (tools/paint.py, docs/paint-stage.md)."""
    if getattr(args, "auto_paint", False):  # part of a default `run`: only when enabled
        s = paint.settings(mon, ov_for(mon), args)
        if not s.get("enabled"):
            log(f"{mon['id']}: paint disabled by override paint.enabled, skip")
            return
    paint.stage_paint(sys.modules[__name__], mon, d, args)


def stage_rig(mon: dict, d: Path, args) -> None:
    rig.stage_rig(sys.modules[__name__], mon, d, args)


def stage_anim(mon: dict, d: Path, args) -> None:
    anim.stage_anim(sys.modules[__name__], mon, d, args)


STAGES = {"ref": stage_ref, "mesh": stage_mesh_guarded, "qa": stage_qa, "paint": stage_paint, "lod": stage_lod,
          "rig": stage_rig, "anim": stage_anim}
RUN_ORDER = ["ref", "mesh", "qa", "paint", "lod", "rig", "anim"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["ref", "mesh", "qa", "paint", "lod", "rig", "anim", "run", "contact"])
    ap.add_argument("ids", nargs="+", help="monster ids (registry slugs)")
    ap.add_argument("--source", default="srd-5.2.1")
    ap.add_argument("--force", action="store_true", help="redo stages whose outputs exist")
    ap.add_argument("--seed", type=int, help="override the per-monster seed")
    ap.add_argument("--n", type=int, default=1, help="reference candidates to generate")
    ap.add_argument("--pick", type=int, help="which ref_<i>.png feeds the mesh stage")
    ap.add_argument("--style", default=None,
                    help="ref: house art style JSON ('none' = unstyled pilot prompts)")
    ap.add_argument("--tris", type=int, help="override the decimate target")
    ap.add_argument("--remesh-resolution", dest="remesh_resolution", type=int)
    ap.add_argument("--texture-size", dest="texture_size", type=int)
    ap.add_argument("--uv-resolution", dest="uv_resolution", type=int)
    ap.add_argument("--normal-cage", dest="normal_cage", type=float,
                    help="BakeNormalMapFromMesh search band, fraction of the bbox diagonal")
    ap.add_argument("--upsample", type=int, help="1024 = cascade upsample (default), 0 = 512 only")
    ap.add_argument("--decimator", default="weighted", choices=["weighted", "comfy"],
                    help="weighted: Blender importance-weighted collapse; comfy: uniform GPU DecimateMesh")
    ap.add_argument("--vg-factor", dest="vg_factor", type=float, help="weighted: vertex_group_factor")
    ap.add_argument("--out-suffix", dest="out_suffix", default="",
                    help="write to out/<source>/<id><suffix>/ (experiments); refs are copied from <id>/")
    ap.add_argument("--yaw", type=float, help="QA: degrees to rotate about up (overrides auto facing)")
    ap.add_argument("--name", default="contact", help="contact: sheet name (_<name>.png)")
    ap.add_argument("--forward", default=None, choices=["-Z", "+Z"],
                    help="glTF facing direction (default -Z, or the override file's)")
    ap.add_argument("--no-level", dest="no_level", action="store_true", help="QA: no stance leveling")
    ap.add_argument("--level-tilt", dest="level_tilt", type=float,
                    help="QA: largest tilt (deg) stance leveling may apply (default 20, insectoid 40)")
    ap.add_argument("--stages", help="run: comma list of stages (default ref,mesh,qa,paint,lod,rig,anim; paint "
                    "is skipped when disabled by override, an explicit --stages paint always runs it)")
    for k, h in (("denoise", "img2img denoise"), ("ip_weight", "IP-Adapter weight"),
                 ("cn_strength", "depth ControlNet strength"), ("normal_strength", "normal ControlNet (0 = off)"),
                 ("color_match", "per-view colour match to the voxel render, 0-1")):
        ap.add_argument("--paint-" + k.replace("_", "-"), dest="paint_" + k, type=float, help="paint: " + h)
    ap.add_argument("--paint-views", dest="paint_views", help="paint: comma list of views")
    ap.add_argument("--rig-rolls", type=int, default=None, help="rig: SkinTokens rolls to score (default 4)")
    ap.add_argument("--rig-reroll", action="store_true", help="rig: roll again, replacing rig/chosen.glb")
    args = ap.parse_args()

    reg = load_registry(args.source)
    if args.cmd == "contact":
        dst = OUT_ROOT / args.source / f"_{args.name}.png"
        dirs = [str(OUT_ROOT / args.source / (mid + args.out_suffix)) for mid in args.ids]
        return subprocess.run([PIL_PYTHON, "-I", str(ROOT / "tools" / "contact_sheet.py"), str(dst), *dirs]).returncode
    if args.cmd == "run":
        want = args.stages.split(",") if args.stages else RUN_ORDER
        bad = [x for x in want if x not in RUN_ORDER]
        if bad:
            ap.error(f"--stages: unknown {bad}; choose from {RUN_ORDER}")
        stages = [x for x in RUN_ORDER if x in want]
        args.auto_paint = not args.stages
    else:
        stages = [args.cmd]
    failures = []
    for mid in args.ids:
        if mid not in reg:
            log(f"unknown id {mid!r} in {args.source}")
            failures.append(mid)
            continue
        mon = reg[mid]
        d = out_dir(args.source, mid, args.out_suffix)
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

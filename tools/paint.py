"""Multiview paint stage: re-texture LOD0's albedo from house-style SDXL views.

Stage `paint` of tools/forge.py (after qa, before lod). See docs/paint-stage.md.

1. tools/blender_paint.py "views": orthographic depth / normal / voxel-colour renders of
   model.glb from N cameras (cameras.json records them).
2. ComfyUI (workflows/paint_view.api.json): juggernautXL + controlnet-union (depth, optional
   normal) + IP-Adapter Plus on ref.png + img2img from the voxel-colour render, per view.
3. tools/blender_paint.py "bake": project the views back (visibility by depth test, facing
   within ~70 deg, cos-weighted with soft silhouette falloff), fill the rest from the voxel
   albedo, write textures/base_color_painted.png and re-export model.glb with it.

Stdlib only; uses tools/forge.py's ComfyUI helpers (passed in as `fg`).

    python3 -I tools/forge.py paint harpy [--force]
    python3 -I tools/paint.py compare harpy goblin-warrior     # before/after sheets
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import overrides  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BLENDER_PAINT = ROOT / "tools" / "blender_paint.py"

# Is the stage part of `forge.py run` by default? (see docs/paint-stage.md for the verdict)
PAINT_DEFAULT_ENABLED = overrides.PAINT_DEFAULTS["enabled"]

UPRIGHT = {"biped", "winged_biped", "centauroid", "merfolk_naga"}
TOP_VIEW_PLANS = {"quadruped", "winged_quadruped", "insectoid", "centauroid"}
HEAD_END = {"quadruped", "winged_quadruped", "insectoid", "avian", "serpentine", "aquatic"}

# name -> (yaw deg from the model's front, + = toward its left; elevation deg; prompt hint)
VIEW_SPECS = {
    "front": (0, 8, "front view"),
    "back": (180, 8, "back view, seen from behind"),
    "left": (90, 5, "side view, profile facing left"),
    "right": (-90, 5, "side view, profile facing right"),
    "front_left": (45, 25, "three-quarter view from the front left, seen slightly from above"),
    "front_right": (-45, 25, "three-quarter view from the front right, seen slightly from above"),
    "top": (0, 80, "top-down view from directly above"),
    "head": (0, 8, "front view"),
    "head_left": (40, 8, "three-quarter view facing left"),
    "head_right": (-40, 8, "three-quarter view facing right"),
}
BACK_NEGATIVE = "face, eyes, mouth, front view, looking at viewer"
HEAD_NEG_DROP = ("cropped", "out of frame", "cut off limbs", "partial body")
VIEW_STRIP = [r"\bfull body\b,?\s*", r"\b(side|front|back|top|three-quarter) view( of)?\b,?\s*",
              r"\bA-pose\b,?\s*", r"\bneutral (standing )?stance\b,?\s*"]


def settings(mon: dict, ov: dict, args=None) -> dict:
    s = dict(overrides.PAINT_DEFAULTS)
    s.update({k: v for k, v in (ov.get("paint") or {}).items() if v is not None})
    if not s.get("views"):
        v = ["front", "back", "left", "right", "front_left", "front_right"]
        if mon["body_plan"] in TOP_VIEW_PLANS:
            v.append("top")
        if s.get("head_views"):
            v += ["head", "head_left", "head_right"]
        s["views"] = v
    for k in ("denoise", "ip_weight", "cn_strength", "normal_strength", "color_match"):
        val = getattr(args, "paint_" + k, None) if args is not None else None
        if val is not None:
            s[k] = val
    if args is not None and getattr(args, "paint_views", None):
        s["views"] = args.paint_views.split(",")
    unknown = [v for v in s["views"] if v not in VIEW_SPECS]
    if unknown:
        raise RuntimeError(f"unknown paint views {unknown}; choose from {sorted(VIEW_SPECS)}")
    return s


def subject_prompt(man: dict, head: bool) -> str:
    p = (man.get("ref") or {}).get("prompt") or ""
    for pat in VIEW_STRIP:
        p = re.sub(pat, "", p)
    p = re.sub(r"\s+,", ",", re.sub(r"\s{2,}", " ", p)).strip(" ,.")
    if head:
        p = "close-up portrait of the head and face, " + p
    else:
        p = "full body, " + p
    return p


def fwd_vec(qa: dict) -> list:
    # QA faces the model +Y in Blender for glTF -Z (the default), -Y for +Z.
    return [0.0, -1.0, 0.0] if qa.get("forward") == "+Z" else [0.0, 1.0, 0.0]


def head_box(qa: dict, plan: str) -> dict | None:
    hr = qa.get("head_region")
    if not hr:
        return None
    # QA frames the face loosely (up to 0.45 x height); the paint close-up wants the head
    # to fill the frame, so it uses 0.7 x that window around the same centre.
    return {"center": hr["center"], "side": hr["side"] * 0.7}


def head_view_dir(qa: dict, plan: str) -> list:
    import math
    f = fwd_vec(qa)
    if (qa.get("head_region") or {}).get("rule") == "front_end_box":
        a = math.radians(35)
        return [math.cos(a) * f[0] - math.sin(a) * f[1], math.sin(a) * f[0] + math.cos(a) * f[1], 0.0]
    return f


def blender(fg, job: dict, timeout=900) -> str:
    proc = subprocess.run([fg.BLENDER, "-b", "--factory-startup", "-P", str(BLENDER_PAINT), "--", json.dumps(job)],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0 or "_OK" not in proc.stdout:
        sys.stderr.write(proc.stdout[-4000:] + proc.stderr[-4000:])
        raise RuntimeError(f"blender_paint {job['mode']} failed (rc={proc.returncode})")
    return proc.stdout


def build_view_graph(fg, man: dict, view: str, cam: dict, s: dict, names: dict, seed: int, prefix: str) -> dict:
    wf = fg.load_workflow("paint_view.api.json")
    ref = man.get("ref") or {}
    head = bool(cam.get("head"))
    prompt = subject_prompt(man, head) + ", " + VIEW_SPECS[view][2]
    neg = ref.get("negative") or wf["neg"]["inputs"]["text"]
    if head:
        neg = ", ".join(t for t in neg.split(", ") if t.strip() not in HEAD_NEG_DROP)
    if view == "back":
        neg += ", " + BACK_NEGATIVE
    if ref.get("model"):
        wf["ckpt"]["inputs"]["ckpt_name"] = ref["model"]
    wf["pos"]["inputs"]["text"] = prompt
    wf["neg"]["inputs"]["text"] = neg
    wf["ref_img"]["inputs"]["image"] = names["ref"]
    wf["depth_img"]["inputs"]["image"] = names["depth"]
    wf["init_img"]["inputs"]["image"] = names["init"]
    wf["ipa"]["inputs"]["weight"] = s["ip_weight"]
    wf["cn_apply"]["inputs"]["strength"] = s["cn_strength"]
    smp = wf["sample"]["inputs"]
    smp.update({"seed": seed, "denoise": s["denoise"], "steps": ref.get("steps", 30), "cfg": ref.get("cfg", 5.5)})
    if ref.get("sampler"):
        smp["sampler_name"] = ref["sampler"]
    if s["denoise"] >= 0.999:  # pure txt2img with the controls
        wf["empty"] = {"class_type": "EmptyLatentImage", "inputs": {"width": cam["width"], "height": cam["height"],
                                                                    "batch_size": 1}}
        smp["latent_image"] = ["empty", 0]
        del wf["init_latent"], wf["init_img"]
    if s.get("ip_weight", 0) <= 0:
        smp["model"] = ["ckpt", 0]
        for k in ("ipa", "ipa_model", "clip_vision", "ref_img"):
            del wf[k]
    if s.get("normal_strength", 0) > 0:
        wf["normal_img"] = {"class_type": "LoadImage", "inputs": {"image": names["normal"]}}
        wf["cn_normal"] = {"class_type": "SetUnionControlNetType", "inputs": {"control_net": ["cn_load", 0],
                                                                             "type": "normal"}}
        wf["cn_apply2"] = {"class_type": "ControlNetApplyAdvanced", "inputs": {
            "positive": ["cn_apply", 0], "negative": ["cn_apply", 1], "control_net": ["cn_normal", 0],
            "image": ["normal_img", 0], "strength": s["normal_strength"], "start_percent": 0.0,
            "end_percent": 1.0, "vae": ["ckpt", 2]}}
        smp["positive"], smp["negative"] = ["cn_apply2", 0], ["cn_apply2", 1]
    wf["save"]["inputs"]["filename_prefix"] = f"{prefix}/{view}"
    return wf


def free_comfy(fg) -> None:
    try:
        fg.http("POST", "/free", json.dumps({"unload_models": True, "free_memory": True}).encode(),
                {"Content-Type": "application/json"})
    except Exception as e:  # noqa: BLE001
        fg.log(f"ComfyUI /free failed: {e}")


def mem_available_gb() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 2**20
    return 99.0


def clear_paint(d: Path, man: dict | None = None) -> None:
    """Drop paint outputs (called when model.glb is rewritten by mesh/qa)."""
    for f in ("base_color_painted.png", "base_color_voxel.png", "paint_coverage.png"):
        (d / "textures" / f).unlink(missing_ok=True)
    shutil.rmtree(d / "paint", ignore_errors=True)
    if man is not None:
        man.pop("paint", None)


def views_cached(pdir: Path, request: dict) -> bool:
    """The control renders depend only on model.glb's geometry (which paint never changes;
    mesh and qa delete paint/) and the camera request."""
    p = pdir / "cameras.json"
    if not p.exists():
        return False
    cj = json.loads(p.read_text())
    if json.loads(json.dumps(request)) != cj.get("request"):
        return False
    return all((pdir / f"{v['name']}_{k}").exists() for v in request["views"]
               for k in ("zbuf.npy", "depth.png", "normal.png", "color.png"))


def stage_paint(fg, mon: dict, d: Path, args) -> None:
    man = fg.load_manifest(d)
    tex = d / "textures"
    if (tex / "base_color_painted.png").exists() and isinstance(man.get("paint"), dict) \
            and man["paint"] and not man["paint"].get("error") and not args.force:
        fg.log(f"{mon['id']}: paint exists, skip")
        return
    for need in ("model.glb", "ref.png", "textures/base_color.png"):
        if not (d / need).exists():
            raise RuntimeError(f"{mon['id']}: no {need}; run mesh + qa first")
    qa = man.get("qa") or {}
    ov = fg.ov_for(mon)
    s = settings(mon, ov, args)
    pdir = d / "paint"
    pdir.mkdir(exist_ok=True)
    # the voxel albedo is textures/base_color.png (the ComfyUI bake, identical to what QA
    # embeds); keep a named copy for comparison and the viewer toggle
    voxel = tex / "base_color_voxel.png"
    shutil.copyfile(tex / "base_color.png", voxel)
    times = {}
    t0 = time.time()
    views = [{"name": v, "yaw": VIEW_SPECS[v][0], "elev": VIEW_SPECS[v][1], "hint": VIEW_SPECS[v][2],
              "head": v.startswith("head")} for v in s["views"]]
    hb = head_box(qa, mon["body_plan"])
    if any(v["head"] for v in views):
        if hb is None:
            views = [v for v in views if not v["head"]]
        else:
            hd = head_view_dir(qa, mon["body_plan"])
            import math
            base_yaw = math.degrees(math.atan2(-hd[0], hd[1]) - math.atan2(-fwd_vec(qa)[0], fwd_vec(qa)[1]))
            for v in views:
                if v["head"]:
                    v["yaw"] += base_yaw
    ts = man.get("mesh", {}).get("settings", {}).get("texture_size")
    with fg.ResourceSampler() as rs:
        request = {"views": views, "size": 1024, "fwd": fwd_vec(qa), "head_box": hb}
        if views_cached(pdir, request):
            fg.log(f"{mon['id']}: paint views cached (same cameras; qa clears them when model.glb changes)")
        else:
            blender(fg, {"mode": "views", "src": str(d / "model.glb"), "voxel_tex": str(voxel),
                         "paint_dir": str(pdir), **request})
            cj = json.loads((pdir / "cameras.json").read_text())
            cj["request"] = request
            (pdir / "cameras.json").write_text(json.dumps(cj, indent=1))
        times["views"] = round(time.time() - t0, 1)
        cams = {c["name"]: c for c in json.loads((pdir / "cameras.json").read_text())["views"]}
        if mem_available_gb() < 6.0:
            fg.log(f"{mon['id']}: {mem_available_gb():.1f} GB RAM available; asking ComfyUI to free models")
            fg.wait_idle()
            free_comfy(fg)
        t1 = time.time()
        sub = f"monster-forge/{mon['source']}/{d.name}/paint"
        ref_name = fg.upload_file(d / "ref.png", sub, "ref.png")
        seed = s.get("seed") if s.get("seed") is not None else fg.base_seed(mon["id"]) % 100000
        prompts = {}
        for v in views:
            name = v["name"]
            names = {"ref": ref_name,
                     "depth": fg.upload_file(pdir / f"{name}_depth.png", sub),
                     "normal": fg.upload_file(pdir / f"{name}_normal.png", sub),
                     "init": fg.upload_file(pdir / f"{name}_color.png", sub)}
            wf = build_view_graph(fg, man, name, cams[name], s, names, seed, sub)
            out = fg.run_prompt(wf, f"{mon['id']} paint {name}")
            fg.fetch_output(out["save"]["images"][0], pdir / f"{name}_gen.png")
            prompts[name] = wf["pos"]["inputs"]["text"]
        (pdir / "prompts.json").write_text(json.dumps(prompts, indent=1))
        times["generate"] = round(time.time() - t1, 1)
        t1 = time.time()
        tsize = ts or 2048
        rep_path = pdir / "bake.json"
        blender(fg, {"mode": "bake", "src": str(d / "model.glb"), "dst": str(d / "model.glb"),
                     "voxel_tex": str(voxel), "paint_dir": str(pdir), "texture_size": tsize,
                     "out_tex": str(tex / "base_color_painted.png"), "coverage": str(tex / "paint_coverage.png"),
                     "report": str(rep_path), "params": {"color_match": s["color_match"]}})
        times["bake"] = round(time.time() - t1, 1)
    bake = json.loads(rep_path.read_text())
    fg.clear_lods(d)  # textures changed: re-run lod (UVs are unchanged)
    man = fg.load_manifest(d)
    man["paint"] = {"settings": s, "views": [v["name"] for v in views], "seed": seed,
                    "texture": "textures/base_color_painted.png", "voxel_texture": "textures/base_color_voxel.png",
                    "texture_size": bake["texture_size"], "painted_fraction": bake["painted_fraction"],
                    "any_view_fraction": bake["any_view_fraction"], "per_view": bake["per_view"],
                    "phase_seconds": times, "resources": rs.result, "seconds": rs.result["seconds"]}
    man.pop("lod", None)
    man.setdefault("timings", {})["paint"] = rs.result["seconds"]
    fg.save_manifest(d, man)
    r = rs.result
    fg.log(f"{mon['id']}: paint done in {r['seconds']}s ({times}), painted {bake['painted_fraction']:.0%} of texels, "
           f"peak RAM {r.get('peak_ram_used_gb')} GB, VRAM {r.get('peak_vram_used_gb')} GB")


# ---------------------------------------------------------------- review sheets
def compare(fg, mon: dict, d: Path, dest: Path, tag: str = "") -> None:
    """out/<source>/_paint_cmp/<id>{tag}_head.png (voxel | painted, QA head camera),
    <id>{tag}_body.png (4 angles; voxel row on top) and <id>{tag}_views.png
    (per view: voxel render | depth | generated)."""
    man = fg.load_manifest(d)
    qa = man.get("qa") or {}
    hr = qa.get("head_region") or {}
    dest.mkdir(parents=True, exist_ok=True)
    head = {"center": hr.get("center", [0, 0, 1]), "side": hr.get("side", 0.5),
            "view": head_view_dir(qa, mon["body_plan"])}
    blender(fg, {"mode": "compare", "src": str(d / "model.glb"), "fwd": fwd_vec(qa),
                 "voxel_tex": str(d / "textures" / "base_color_voxel.png"),
                 "painted_tex": str(d / "textures" / "base_color_painted.png"), "head": head,
                 "out_head": str(dest / f"{mon['id']}{tag}_head.png"),
                 "out_body": str(dest / f"{mon['id']}{tag}_body.png")})
    views = (man.get("paint") or {}).get("views") or []
    cells = []
    for v in views:
        cells.append([str(d / "paint" / f"{v}_{k}.png") for k in ("color", "depth", "gen")])
    script = (
        "import sys,json\nfrom PIL import Image\nrows=json.loads(sys.argv[1]);dst=sys.argv[2]\n"
        "S=320\nW=S*3;H=S*len(rows)\nout=Image.new('RGB',(W*2 if len(rows)>4 else W,"
        "S*((len(rows)+1)//2) if len(rows)>4 else H),'white')\n"
        "for i,r in enumerate(rows):\n"
        "  col,row=(i%2,i//2) if len(rows)>4 else (0,i)\n"
        "  for j,p in enumerate(r):\n"
        "    out.paste(Image.open(p).convert('RGB').resize((S,S)),(col*W+j*S,row*S))\n"
        "out.save(dst)\n")
    subprocess.run([fg.PIL_PYTHON, "-I", "-c", script, json.dumps(cells), str(dest / f"{mon['id']}{tag}_views.png")],
                   check=True)


def main() -> int:
    import argparse
    import forge as fg
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["compare"])
    ap.add_argument("ids", nargs="+")
    ap.add_argument("--source", default="srd-5.2.1")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    reg = fg.load_registry(args.source)
    for mid in args.ids:
        d = fg.OUT_ROOT / args.source / mid
        compare(fg, reg[mid], d, fg.OUT_ROOT / args.source / "_paint_cmp", args.tag)
        fg.log(f"{mid}: sheets in {fg.OUT_ROOT / args.source / '_paint_cmp'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

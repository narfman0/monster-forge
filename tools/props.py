#!/usr/bin/env python3
"""monster-forge props pipeline: data/registry/props.json -> game-ready weapon and shield
GLBs in prop space (docs/equipment.md, "Prop space" and "Generating props").

    python3 -I tools/props.py run longsword shield longbow
    python3 -I tools/props.py run --all
    python3 -I tools/props.py run greataxe --stages qa,lod --force
    python3 -I tools/props.py run scimitar --stages ref,mesh,qa,lod --force --n 4
    python3 -I tools/props.py run longsword --stages ref --view vertical --out /tmp/x   # experiments
    python3 -I tools/props.py check longsword          # re-read model.glb, print the frame check
    python3 -I tools/props.py contact                   # out/props/_contact.png

Stages (same names and skip rules as tools/forge.py; --force redoes them):

- ref:  SDXL + the house style (forge.apply_style), the registry reference_prompt with a
        per-category view (blades and hafted weapons standing vertical, crossbows and
        pistols lying level and pointing right, shields front on), "single object" instead
        of forge's "single creature" prefix, and a prop negative (hands, people, racks,
        pairs). The base negative's "props" is dropped. --n candidates are scored on
        their silhouette (one connected blob, not cut off by the frame, the aspect ratio
        expected for the category) and the best becomes ref.png; the scores are logged
        in manifest.ref.scores.
- mesh: forge.stage_mesh_guarded unchanged (Pixal3D -> remesh -> Blender weighted
        decimation -> bake), with the prop's tri_budget, 512 textures and no paint stage.
- qa:   tools/blender_prop_qa.py: orient into prop space, scale to length_m, origin at the
        grip, export model.glb; then this script re-reads model.glb (stdlib GLB reader)
        and checks length, origin and the -Z side numerically (manifest prop.glb_check).
- lod:  tools/blender_lods.py on model.glb with prop ratios and a lower floor.

Output: out/props/<id>/ (ref_<i>.png, ref.png, crop.png, model_hi.glb, model_raw.glb,
model.glb, model_lod1-3.glb, lods.json, textures/, thumb.png, qa.json, manifest.json).

Per-prop overrides: data/overrides/props/<id>.json, the monster override fields of
tools/overrides.py plus PROP_FIELDS below (registered into overrides.FIELDS in this
process only, so forge's stage functions validate them).

ComfyUI is shared and memory is tight (30 GB RAM, the ComfyUI unit is capped at 16 GB):
every job waits for the queue to drain (forge.wait_idle) and for MemAvailable >= 8 GB
(calling /free while it is lower); `run` goes stage by stage over all props (all refs,
then all meshes) so SDXL and Pixal3D load once, and calls /free every --batch props.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge as fg  # noqa: E402  (ComfyUI plumbing, style, workflows, resource sampling)
import overrides  # noqa: E402

ROOT = fg.ROOT
REGISTRY = ROOT / "data" / "registry" / "props.json"
OUT = fg.OUT_ROOT / "props"
SOURCE = "props"  # overrides live in data/overrides/props/<id>.json
STAGES = ["ref", "mesh", "qa", "lod"]

PROP_FIELDS = {
    "view": (overrides._choice("vertical", "horizontal", "front"), "ref",
             "reference view: standing vertical, lying level pointing right, or front on"),
    "remesh_resolution": (overrides._choice(256, 320, 384, 512), "mesh", "RemeshMesh resolution"),
    "flip_tip": (overrides._bool, "qa", "swap the tip and butt ends (+Y)"),
    "flip_edge": (overrides._bool, "qa", "swap the edge side (-Z)"),
    "grip_frac": (overrides._num(0, 1), "qa", "grip point as a fraction of the length from the butt"),
    "roll_deg": (overrides._num(-180, 180), "qa", "extra rotation about +Y after orienting"),
    "tip_rule": (overrides._choice("thin_end", "head_end", "thick_handle", "symmetric"), "qa", "tip-end rule"),
    "edge_rule": (overrides._choice("symmetric", "convex", "extension", "blunt", "stave_forward"), "qa",
                  "edge-side rule"),
}
overrides.FIELDS.update(PROP_FIELDS)

# Defaults (docs/equipment.md "Generating props"). Upsample/remesh: see the longsword test
# in docs/equipment.md: thin blades need the finer shape to stay in one piece.
PROP_MESH = {"texture_size": 512, "upsample": 0, "remesh_resolution": 256}
LOD_RATIOS = [0.5, 0.3, 0.18]
BOW_MESH_FACTOR = 1.6
LOD_MIN_TRIS = 100
MIN_AVAILABLE_GB = 8.0

VERTICAL = "the whole object, standing vertical, flat side view"
VIEW_TEXT = {
    "vertical": "the whole object, standing upright and vertical, tip at the top, flat side view",
    "horizontal": "the whole object, lying horizontal and level, pointing to the right, flat side view",
    "front": "the whole object, front view, face toward the viewer",
    # bows: SDXL draws crossed limbs, loose strings and arrows, and Pixal3D turns a string
    # into a dangling ribbon; draw the stave alone and let blender_prop_qa.py add the string
    "bow": "the bow stave alone, without its string, standing upright and vertical, a smooth symmetric "
           "curve like a crescent, nothing else in the picture, flat side view",
}
# Longsword test (docs/equipment.md): SDXL draws the blade diagonally either way, but the
# "horizontal" prompt gave the straighter blade and tip after Pixal3D; bows stay upright
# (the string reads better), shields front on.
VIEW_BY_CATEGORY = {"bow": "bow", "shield": "front"}
DEFAULT_VIEW = "horizontal"
PROP_PREFIX = "single object, centered, "
PROP_NEGATIVE = ("hand, hands, fingers, arm, holding, person, character, figure, warrior, body, "
                 "multiple objects, two weapons, pair, set, collection, weapon rack, display stand, "
                 "mount, pedestal, wall, floor, table, scabbard, sheath, cropped, cut off")
NEGATIVE_BY_CATEGORY = {"bow": "(arrow, arrows, arrowhead, archer, bowstring, string:1.4), quiver, crossed limbs, rope", "crossbow": "(bolt, arrow, archer:1.3), quiver",
                        "shield": "sword, spear, weapon behind the shield"}
# silhouette aspect (long / short side of the main blob, PCA) the reference should have
ASPECT = {"light_blade": 10, "blade": 14, "heavy_blade": 12, "axe": 3, "heavy_axe": 3, "bludgeon": 4,
          "heavy_bludgeon": 4, "polearm": 12, "thrown": 15, "staff": 15, "implement": 10, "bow": 6,
          "crossbow": 1.6, "firearm": 3, "shield": 1.1, "flexible": 15}


def log(msg: str) -> None:
    fg.log(msg)


def load_props() -> dict[str, dict]:
    return {p["id"]: p for p in json.loads(REGISTRY.read_text())["props"]}


def as_mon(p: dict) -> dict:
    """A registry-record lookalike for forge's stage functions (source = 'props')."""
    return {"id": p["id"], "name": p["name"], "source": SOURCE, "size": "medium", "body_plan": "prop",
            "scale_m": p["length_m"], "reference_prompt": p["reference_prompt"]}


def ov_for(p: dict) -> dict:
    return fg.ov_for(as_mon(p))


# ---------------------------------------------------------------- memory gate
def mem_available_gb() -> float:
    mi = {l.split(":")[0]: int(l.split()[1]) for l in Path("/proc/meminfo").read_text().splitlines()}
    return mi["MemAvailable"] / 2**20


def comfy_free() -> None:
    try:
        fg.http("POST", "/free", json.dumps({"unload_models": True, "free_memory": True}).encode(),
                {"Content-Type": "application/json"})
    except Exception as e:  # noqa: BLE001
        log(f"/free failed: {e}")


def ensure_memory(min_gb: float = MIN_AVAILABLE_GB, max_wait: float = 1800) -> None:
    """Block until MemAvailable >= min_gb, asking ComfyUI to unload while it is lower."""
    t0 = time.time()
    while (avail := mem_available_gb()) < min_gb:
        if time.time() - t0 > max_wait:
            raise RuntimeError(f"only {avail:.1f} GB available after {max_wait:.0f} s")
        log(f"MemAvailable {avail:.1f} GB < {min_gb} GB: /free and wait")
        fg.wait_idle()
        comfy_free()
        time.sleep(30)


_run_prompt = fg.run_prompt


def guarded_run_prompt(graph: dict, label: str, timeout: float = 1800) -> dict:
    """forge.run_prompt behind the memory gate (forge's stages call it via the module)."""
    fg.wait_idle()
    ensure_memory()
    return _run_prompt(graph, label, timeout)


fg.run_prompt = guarded_run_prompt


# ---------------------------------------------------------------- ref
def ref_prompt(p: dict, ov: dict) -> tuple[str, str, str]:
    view = ov.get("view") or VIEW_BY_CATEGORY.get(p["category"], DEFAULT_VIEW)
    base = p["reference_prompt"]
    if p["category"] == "bow":  # the string is added in QA (VIEW_TEXT["bow"])
        base = re.sub(r"\s*(?:and|with) a taut string", "", base)
    if VERTICAL in base:
        base = base.replace(VERTICAL, VIEW_TEXT[view])
    else:
        base = base.rstrip(" .") + ", " + VIEW_TEXT[view] + "."
    prompt = PROP_PREFIX + base
    if ov.get("prompt_extra"):
        prompt = prompt.rstrip(" ,.") + ", " + ov["prompt_extra"]
    neg = PROP_NEGATIVE
    if NEGATIVE_BY_CATEGORY.get(p["category"]):
        neg += ", " + NEGATIVE_BY_CATEGORY[p["category"]]
    return prompt, neg, view


def stage_ref(p: dict, d: Path, args) -> None:
    man = fg.load_manifest(d)
    refs = sorted(d.glob("ref_*.png"))
    if refs and (d / "ref.png").exists() and not args.force:
        log(f"{p['id']}: ref exists, skip")
        return
    for f in refs:
        f.unlink()
    mon, ov = as_mon(p), ov_for(p)
    wf = fg.load_workflow("ref_image.api.json")
    # the base negative fights props ("props", "cropped" is fine); drop the word
    wf["neg"]["inputs"]["text"] = wf["neg"]["inputs"]["text"].replace("ground clutter, rocks, grass, props, ",
                                                                      "ground clutter, rocks, grass, ")
    prompt, neg, view = ref_prompt(p, {**ov, **({"view": args.view} if args.view else {})})
    style = fg.load_style(fg.resolve_style(args.style or ov.get("style")))
    if style:
        prompt = fg.apply_style(wf, style, prompt, mon)
    wf["neg"]["inputs"]["text"] += ", " + neg
    if ov.get("negative_extra"):
        wf["neg"]["inputs"]["text"] += ", " + ov["negative_extra"]
    seed0 = args.seed if args.seed is not None else ov.get("seed_ref", fg.base_seed(p["id"]))
    wf["pos"]["inputs"]["text"] = prompt
    wf["sample"]["inputs"]["seed"] = seed0
    wf["latent"]["inputs"]["batch_size"] = args.n
    wf["save"]["inputs"]["filename_prefix"] = f"monster-forge/{SOURCE}/{p['id']}/ref"
    with fg.ResourceSampler() as rs:
        outputs = fg.run_prompt(wf, f"{p['id']} ref")
        images = outputs["save"]["images"]
        for i, img in enumerate(images):
            fg.fetch_output(img, d / f"ref_{i}.png")
    scores = score_refs([d / f"ref_{i}.png" for i in range(len(images))], p["category"])
    if args.pick is not None or ov.get("ref_pick") is not None:
        pick = min(args.pick if args.pick is not None else ov["ref_pick"], len(images) - 1)
        method = "flag" if args.pick is not None else "override ref_pick"
    else:
        pick = max(range(len(scores)), key=lambda i: scores[i]["score"])
        method = "silhouette score"
    shutil.copyfile(d / f"ref_{pick}.png", d / "ref.png")
    man.update(prop_header(p))
    man["ref"] = {"model": wf["ckpt"]["inputs"]["ckpt_name"], "prompt": prompt,
                  "negative": wf["neg"]["inputs"]["text"], "view": view, "seed": seed0, "n": len(images),
                  "picked": pick, "pick_method": method, "scores": scores,
                  "steps": wf["sample"]["inputs"]["steps"], "cfg": wf["sample"]["inputs"]["cfg"],
                  "style": style.get("name") if style else None, "resources": rs.result,
                  "seconds": rs.result["seconds"]}
    man.setdefault("timings", {})["ref"] = rs.result["seconds"]
    fg.save_manifest(d, man)
    log(f"{p['id']}: ref done in {rs.result['seconds']}s, picked {pick} ({method}); scores "
        + " ".join(f"{s['score']:.2f}" for s in scores))


def score_refs(paths: list[Path], category: str) -> list[dict]:
    """Run silhouette_scores() under the Pillow/numpy Python (the ComfyUI venv)."""
    proc = subprocess.run([fg.PIL_PYTHON, "-I", __file__, "_score", category, *map(str, paths)],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        log(f"ref scoring failed: {proc.stderr[-500:]}")
        return [{"score": 0.0, "error": "scoring failed"} for _ in paths]
    return json.loads(proc.stdout)


def silhouette_scores(category: str, paths: list[str]) -> list[dict]:
    """Per image: foreground = pixels away from the border colour; connected blobs;
    score = main-blob share - 0.15 per extra blob (>1% of the foreground) - 0.3 if the blob
    touches the frame - 0.25 x |ln(aspect / expected)| (capped at 0.5)."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage
    out = []
    for path in paths:
        im = np.asarray(Image.open(path).convert("RGB").resize((256, 256)), dtype=np.float32)
        border = np.concatenate([im[0], im[-1], im[:, 0], im[:, -1]])
        bg = np.median(border, axis=0)
        fg_ = np.abs(im - bg).max(axis=2) > 40
        fg_ = ndimage.binary_opening(fg_, iterations=1)
        lab, n = ndimage.label(fg_, structure=np.ones((3, 3)))
        if n == 0:
            out.append({"score": -1.0, "blobs": 0})
            continue
        sizes = ndimage.sum(fg_, lab, range(1, n + 1))
        total = float(sizes.sum())
        main = int(np.argmax(sizes)) + 1
        sig = int((sizes > 0.01 * total).sum())
        ys, xs = np.nonzero(lab == main)
        touches = bool(ys.min() == 0 or xs.min() == 0 or ys.max() == 255 or xs.max() == 255)
        cov = np.cov(np.stack([xs, ys]).astype(float))
        ev = np.sort(np.linalg.eigvalsh(cov))
        aspect = float(math.sqrt(ev[1] / max(ev[0], 1e-6)))
        exp_ = ASPECT.get(category, 5)
        ap = min(0.5, 0.25 * abs(math.log(aspect / exp_)))
        main_frac = float(sizes[main - 1] / total)
        score = main_frac - 0.15 * (sig - 1) - (0.3 if touches else 0.0) - ap
        out.append({"score": round(score, 3), "main_frac": round(main_frac, 3), "blobs": sig,
                    "touches_frame": touches, "aspect": round(aspect, 2), "aspect_expected": exp_,
                    "fill": round(total / 256 ** 2, 3)})
    return out


def prop_header(p: dict) -> dict:
    return {"id": p["id"], "name": p["name"], "source": SOURCE, "category": p["category"], "grip": p["grip"],
            "length_m": p["length_m"], "tri_budget": p["tri_budget"], "grip_socket": p["grip_socket"],
            "stow_socket": p["stow_socket"]}


# ---------------------------------------------------------------- mesh
def mesh_args(p: dict, ov: dict, args) -> argparse.Namespace:
    """forge.stage_mesh arguments: the prop budget and texture size, overrides, then flags."""
    a = argparse.Namespace(**{k: None for k in ("pick", "seed", "remesh_resolution", "texture_size",
                                                "uv_resolution", "normal_cage", "upsample", "vg_factor")})
    a.force, a.decimator = args.force, args.decimator
    a.tris = args.tris or ov.get("tri_budget") or p["tri_budget"]
    if p["category"] == "bow" and not (args.tris or ov.get("tri_budget")):
        # QA cuts the string ribbons out of the decimated mesh (~40% of the faces)
        a.tris = int(a.tris * BOW_MESH_FACTOR)
    a.texture_size = args.texture_size or ov.get("texture_size") or PROP_MESH["texture_size"]
    a.uv_resolution = a.texture_size
    a.upsample = args.upsample if args.upsample is not None else ov.get("upsample", PROP_MESH["upsample"])
    a.remesh_resolution = args.remesh_resolution or ov.get("remesh_resolution") or PROP_MESH["remesh_resolution"]
    a.vg_factor = args.vg_factor
    a.normal_cage = None
    a.pick, a.seed = args.pick, args.seed
    return a


def stage_mesh(p: dict, d: Path, args) -> None:
    if (d / "model_raw.glb").exists() and not args.force:
        log(f"{p['id']}: mesh exists, skip")
        return
    ov = ov_for(p)
    a = mesh_args(p, ov, args)
    # forge retries a failed 1024 cascade at 0 only when args.upsample is None (its
    # default, or the override file, is then 1024): pass None for a non-flag 1024
    if args.upsample is None and a.upsample == 1024:
        a.upsample = None
    fg.stage_mesh_guarded(as_mon(p), d, a)
    man = fg.load_manifest(d)
    man.update(prop_header(p))
    for k in ("qa", "prop", "lod", "tri_count"):
        man.pop(k, None)
    fg.save_manifest(d, man)


# ---------------------------------------------------------------- qa
def stage_qa(p: dict, d: Path, args) -> None:
    if all((d / f).exists() for f in ("model.glb", "thumb.png", "qa.json")) and not args.force:
        log(f"{p['id']}: qa exists, skip")
        return
    if not (d / "model_raw.glb").exists():
        raise RuntimeError(f"{p['id']}: no model_raw.glb; run `mesh` first")
    ov = ov_for(p)
    job = {"src": str(d / "model_raw.glb"), "dst": str(d / "model.glb"), "report": str(d / "qa.json"),
           "thumb": str(d / "thumb.png"), "id": p["id"], "category": p["category"], "grip": p["grip"],
           "length_m": p["length_m"], "grip_frac": ov.get("grip_frac"), "flip_tip": bool(ov.get("flip_tip")),
           "flip_edge": bool(ov.get("flip_edge")), "roll_deg": ov.get("roll_deg"),
           "rules": {k: ov[f"{k}_rule"] for k in ("tip", "edge") if ov.get(f"{k}_rule")}}
    fg.clear_lods(d)
    (d / "qa.json").unlink(missing_ok=True)
    with fg.ResourceSampler() as rs:
        proc = subprocess.run([fg.BLENDER, "-b", "--factory-startup", "-P", str(ROOT / "tools" / "blender_prop_qa.py"),
                               "--", json.dumps(job)], capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not (d / "qa.json").exists():
        sys.stderr.write(proc.stdout[-4000:] + proc.stderr[-4000:])
        raise RuntimeError(f"{p['id']}: blender prop QA failed (rc={proc.returncode})")
    rep = json.loads((d / "qa.json").read_text())
    chk = check_glb(d / "model.glb", p)
    man = fg.load_manifest(d)
    man.update(prop_header(p))
    rep["resources"] = {k: v for k, v in rs.result.items() if "vram" not in k}
    man["qa"] = rep
    man["tri_count"] = rep["tris"]
    man["prop"] = {
        "frame": "glTF: origin = grip, +Y toward the tip/head, -Z = edge / striking face / arrow direction "
                 "(shields: origin at the strap, +Y top rim, -Z out of the face)",
        "grip": rep["grip"], "grip_from_butt_m": rep["grip_from_butt_m"],
        "axes": rep["decisions"], "rules": rep["rules"],
        "orientation_confidence": rep["orientation_confidence"],
        "offhand_grip": rep.get("offhand_grip_gltf"),
        "length_check": chk["length"], "glb_check": chk,
        "grip_offset": GRIP_OFFSET.get(p["category"], GRIP_OFFSET["default"]),
        "overrides": {k: ov[k] for k in PROP_FIELDS if ov.get(k) is not None},
    }
    man.pop("lod", None)
    man.setdefault("timings", {})["qa"] = rs.result["seconds"]
    fg.save_manifest(d, man)
    log(f"{p['id']}: qa done in {rs.result['seconds']}s, {rep['tris']} tris, dims {rep['dims_gltf_m']}, "
        f"conf {rep['orientation_confidence']}, check {'ok' if chk['ok'] else 'FAIL'}")


# Prop -> socket offset for Godot. The rig's grip_r/grip_l bones (docs/equipment.md) sit at
# the fist centre in prop space (+Y forward along the blade at rest, +Z up, edge down), so
# melee props need no offset there. Props whose -Z must point forward (bows, crossbows,
# pistols) or whose face must (shields) turn +90 deg about X: prop +Y (limbs, top rim,
# up) -> grip +Z (up), prop -Z (arrow, muzzle, face) -> grip +Y (forward). Checked in the
# viewer on the goblin-warrior (docs/equipment.md "Generating props"). On hand_r/hand_l
# (the wrist, +Y wrist -> fingers) a prop also needs the fist offset, ~0.085 m along the
# bone for a 1.8 m wielder; the grip bones make that unnecessary.
_TURN = {"socket": "grip", "rotation_deg_xyz": [90, 0, 0], "translation_m": [0, 0, 0]}
GRIP_OFFSET = {
    "default": {"socket": "grip", "rotation_deg_xyz": [0, 0, 0], "translation_m": [0, 0, 0],
                "note": "attach at grip_r/grip_l as is"},
    "bow": {**_TURN, "note": "bow upright, arrow direction forward"},
    "crossbow": {**_TURN, "note": "stock forward, prod up-front, +Y (top of the stock) up"},
    "firearm": {**_TURN, "note": "barrel forward, pistol grip in the fist"},
    "shield": {**_TURN, "note": "face forward, top rim up; origin is the back-surface centre (centre-grip "
                                "handle). A strapped shield would sit ~0.12 m up the forearm"},
}


# ---------------------------------------------------------------- GLB check (stdlib)
def read_glb_positions(path: Path):
    """World-space POSITION data of every mesh primitive and the named nodes' positions."""
    import array
    data = path.read_bytes()
    jlen = struct.unpack_from("<I", data, 12)[0]
    gj = json.loads(data[20:20 + jlen])
    boff = 20 + jlen + 8
    binary = data[boff:]
    nodes = gj.get("nodes", [])

    def mat_of(n):
        m = [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]
        if "matrix" in n:
            mm = n["matrix"]
            return [[mm[c * 4 + r] for c in range(4)] for r in range(4)]
        sx, sy, sz = n.get("scale", [1, 1, 1])
        qx, qy, qz, qw = n.get("rotation", [0, 0, 0, 1])
        rot = [[1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
               [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
               [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)]]
        t = n.get("translation", [0, 0, 0])
        for r in range(3):
            for c in range(3):
                m[r][c] = rot[r][c] * (sx, sy, sz)[c]
            m[r][3] = t[r]
        return m

    def mul(a, b):
        return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]

    world = {}

    def walk(i, parent):
        world[i] = mul(parent, mat_of(nodes[i]))
        for ch in nodes[i].get("children", []):
            walk(ch, world[i])

    ident = [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]
    for root in gj["scenes"][gj.get("scene", 0)]["nodes"]:
        walk(root, ident)
    pts = []
    for i, n in enumerate(nodes):
        if "mesh" not in n:
            continue
        m = world[i]
        for prim in gj["meshes"][n["mesh"]]["primitives"]:
            acc = gj["accessors"][prim["attributes"]["POSITION"]]
            bv = gj["bufferViews"][acc["bufferView"]]
            off = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
            stride = bv.get("byteStride", 12)
            raw = array.array("f")
            if stride == 12:
                raw.frombytes(binary[off:off + 12 * acc["count"]])
            else:
                for k in range(acc["count"]):
                    raw.frombytes(binary[off + k * stride:off + k * stride + 12])
            for k in range(acc["count"]):
                x, y, z = raw[3 * k], raw[3 * k + 1], raw[3 * k + 2]
                pts.append(tuple(m[r][0] * x + m[r][1] * y + m[r][2] * z + m[r][3] for r in range(3)))
    named = {n.get("name"): [round(world[i][r][3], 4) for r in range(3)] for i, n in enumerate(nodes) if "mesh" not in n}
    return pts, named


def check_glb(path: Path, p: dict) -> dict:
    """Independent check of the exported file in glTF axes: length along the length axis,
    origin (grip) position, and the -Z side by a category-specific test."""
    pts, named = read_glb_positions(path)
    xs, ys, zs = zip(*pts)
    mn, mx = (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))
    L = p["length_m"]
    cat = p["category"]
    ax = {"crossbow": 2, "firearm": 2}.get(cat, 1)
    if cat == "shield":
        meas = max(mx[0] - mn[0], mx[1] - mn[1])
    else:
        meas = mx[ax] - mn[ax]
    res = {"bounds_min": [round(v, 4) for v in mn], "bounds_max": [round(v, 4) for v in mx],
           "length": {"target_m": L, "measured_m": round(meas, 4), "axis": "-Z" if ax == 2 else
                      ("max(X,Y)" if cat == "shield" else "+Y"), "ok": abs(meas - L) <= 0.02 * L},
           "nodes": named}
    inside = all(mn[i] - 1e-4 <= 0 <= mx[i] + 1e-4 for i in range(3))
    res["origin_inside_bounds"] = inside
    if cat in ("crossbow", "firearm"):
        res["origin_from_butt_m"] = round(mx[2], 4)  # the butt is at +Z (muzzle at -Z)
    elif cat == "shield":
        res["origin_from_back_m"] = round(mx[2], 4)
    else:
        res["origin_from_butt_m"] = round(-mn[1], 4)
    # -Z side tests
    span = mx[1] - mn[1]
    if cat == "bow":
        mid = [z for x, y, z in pts if abs(y) < 0.1 * span]
        tips = [z for x, y, z in pts if abs(y) > 0.4 * span]
        ok = sum(tips) / len(tips) > sum(mid) / len(mid)
        res["edge_test"] = {"test": "tips (string side) behind the grip: mean z tips > mean z grip",
                            "z_tips": round(sum(tips) / len(tips), 4), "z_grip": round(sum(mid) / len(mid), 4), "ok": ok}
    elif cat in ("crossbow", "firearm"):
        front = [x for x, y, z in pts if z < mn[2] + 0.25 * (mx[2] - mn[2])]
        back = [x for x, y, z in pts if z > mx[2] - 0.25 * (mx[2] - mn[2])]
        wf, wb = max(front) - min(front), max(back) - min(back)
        res["edge_test"] = {"test": "front quarter (-Z) vs rear quarter: x span (prod) / y span (grip)",
                            "x_span_front": round(wf, 4), "x_span_back": round(wb, 4),
                            "ok": wf > wb if cat == "crossbow" else None}
        if cat == "firearm":
            hf = [y for x, y, z in pts if z < mn[2] + 0.3 * (mx[2] - mn[2])]
            hb = [y for x, y, z in pts if z > mx[2] - 0.3 * (mx[2] - mn[2])]
            res["edge_test"].update({"y_span_front": round(max(hf) - min(hf), 4),
                                     "y_span_back": round(max(hb) - min(hb), 4),
                                     "ok": (max(hf) - min(hf)) < (max(hb) - min(hb))})
    elif cat == "shield":
        zm = sum(z for _, _, z in pts) / len(pts)
        res["edge_test"] = {"test": "shield body in front of the strap: mean z < 0", "mean_z": round(zm, 4), "ok": zm < 0}
    else:
        # the head: upper 40% (blades, axes), upper 15% for polearms (the shaft dominates)
        hf = 0.85 if cat in ("polearm", "thrown") else 0.6
        head = [(x, z) for x, y, z in pts if y > mn[1] + hf * span]
        hx = max(x for x, _ in head) - min(x for x, _ in head)
        hz = max(z for _, z in head) - min(z for _, z in head)
        ztop, zbot = max(z for _, z in head), min(z for _, z in head)
        test = {"head_x_span": round(hx, 4), "head_z_span": round(hz, 4),
                "head_z_min": round(zbot, 4), "head_z_max": round(ztop, 4)}
        if cat in ("light_blade", "blade", "heavy_blade", "axe", "heavy_axe", "polearm"):
            test["test"] = f"upper {round(100 * (1 - hf))}%: blade/head wider along Z (edge line) than along X (flat)"
            test["ok"] = hz > hx
        else:
            test["test"] = "round head: none"
            test["ok"] = None
        if p["id"] in ("battleaxe", "handaxe", "glaive", "sickle", "scimitar", "warhammer", "light-hammer"):
            test["reach_minus_z_gt_plus_z"] = -zbot > ztop
        res["edge_test"] = test
    res["ok"] = res["length"]["ok"] and inside and res["edge_test"].get("ok") is not False
    return res


# ---------------------------------------------------------------- lod
def stage_lod(p: dict, d: Path, args) -> None:
    lods = d / "lods.json"
    if lods.exists() and not args.force:
        files = [v.get("file") for v in json.loads(lods.read_text()).get("lods", {}).values()]
        if all((d / f).exists() for f in files if f):
            log(f"{p['id']}: lod exists, skip")
            return
    if not (d / "model.glb").exists():
        raise RuntimeError(f"{p['id']}: no model.glb; run `qa` first")
    ratios = ov_for(p).get("lod_ratios") or LOD_RATIOS
    fg.clear_lods(d)
    job = {"src": str(d / "model.glb"), "out_dir": str(d), "report": str(lods),
           "ratios": {str(i + 1): r for i, r in enumerate(ratios)}, "min_tris": LOD_MIN_TRIS}
    with fg.ResourceSampler() as rs:
        proc = subprocess.run([fg.BLENDER, "-b", "--factory-startup", "-P", str(ROOT / "tools" / "blender_lods.py"),
                               "--", json.dumps(job)], capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not lods.exists():
        sys.stderr.write(proc.stdout[-4000:] + proc.stderr[-4000:])
        raise RuntimeError(f"{p['id']}: blender LODs failed (rc={proc.returncode})")
    rep = json.loads(lods.read_text())
    man = fg.load_manifest(d)
    man["lod"] = {"seconds": rs.result["seconds"], "ratios": ratios, "min_tris": LOD_MIN_TRIS,
                  "tris": {k: v["tris"] for k, v in rep["lods"].items()},
                  "resources": {k: v for k, v in rs.result.items() if "vram" not in k}}
    man.setdefault("timings", {})["lod"] = rs.result["seconds"]
    fg.save_manifest(d, man)
    log(f"{p['id']}: lod done in {rs.result['seconds']}s, tris {man['lod']['tris']}")


STAGE_FN = {"ref": stage_ref, "mesh": stage_mesh, "qa": stage_qa, "lod": stage_lod}


# ---------------------------------------------------------------- contact sheet
def contact_sheet(dst: str, dirs: list[str]) -> None:
    """Per prop: ref.png, the 4-view axes sheet and a label (Pillow; ComfyUI venv)."""
    from PIL import Image, ImageDraw
    TH, COLS, LAB = 220, 2, 34
    tw = TH + 4 * TH
    rows = (len(dirs) + COLS - 1) // COLS
    sheet = Image.new("RGB", (COLS * tw, rows * (TH + LAB)), "white")
    dr = ImageDraw.Draw(sheet)
    font = None
    for fp in ("/usr/share/fonts/google-noto/NotoSans-Regular.ttf",
               "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf"):
        if Path(fp).exists():
            from PIL import ImageFont
            font = ImageFont.truetype(fp, 18)
            break
    for i, dd in enumerate(map(Path, dirs)):
        x, y = (i % COLS) * tw, (i // COLS) * (TH + LAB)
        man = json.loads((dd / "manifest.json").read_text()) if (dd / "manifest.json").exists() else {}
        if (dd / "ref.png").exists():
            sheet.paste(Image.open(dd / "ref.png").convert("RGB").resize((TH, TH)), (x, y + LAB))
        if (dd / "thumb.png").exists():
            sheet.paste(Image.open(dd / "thumb.png").convert("RGB").resize((4 * TH, TH)), (x + TH, y + LAB))
        else:
            dr.text((x + TH + 20, y + LAB + 90), "MISSING (see manifest errors)", fill="red", font=font)
        pr = man.get("prop", {})
        lc = pr.get("length_check", {})
        label = (f"{man.get('name', dd.name)} | {man.get('category', '?')} | {man.get('tri_count', '?')} tris | "
                 f"{lc.get('measured_m', '?')}/{man.get('length_m', '?')} m | conf {pr.get('orientation_confidence')}"
                 f" | check {'ok' if pr.get('glb_check', {}).get('ok') else '--'}")
        dr.text((x + 8, y + 7), label, fill="black", font=font)
        dr.rectangle([x, y, x + tw - 1, y + TH + LAB - 1], outline=(180, 180, 180))
    sheet.save(dst)
    print(dst)


# ---------------------------------------------------------------- CLI
def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "_score":  # internal: runs under PIL_PYTHON
        print(json.dumps(silhouette_scores(sys.argv[2], sys.argv[3:])))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "_sheet":
        contact_sheet(sys.argv[2], sys.argv[3:])
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "check", "contact"])
    ap.add_argument("ids", nargs="*", help="prop ids (data/registry/props.json)")
    ap.add_argument("--all", action="store_true", help="every prop in the registry")
    ap.add_argument("--stages", default=",".join(STAGES), help="comma list of ref,mesh,qa,lod")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--n", type=int, default=3, help="reference candidates")
    ap.add_argument("--pick", type=int, help="which ref_<i>.png to use (default: best silhouette score)")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--view", choices=sorted(VIEW_TEXT), help="ref: reference view (default by category)")
    ap.add_argument("--style", default=None)
    ap.add_argument("--tris", type=int, help="override the tri budget")
    ap.add_argument("--upsample", type=int, help="1024 = cascade, 0 = 512 only (prop default 0)")
    ap.add_argument("--remesh-resolution", dest="remesh_resolution", type=int)
    ap.add_argument("--texture-size", dest="texture_size", type=int)
    ap.add_argument("--vg-factor", dest="vg_factor", type=float, help="weighted decimation factor (0 = plain collapse)")
    ap.add_argument("--decimator", default="weighted", choices=["weighted", "comfy"])
    ap.add_argument("--batch", type=int, default=4, help="ComfyUI /free after this many props per GPU stage")
    ap.add_argument("--out", default=str(OUT), help="output root (default out/props)")
    ap.add_argument("--name", default="contact", help="contact: sheet name (_<name>.png)")
    args = ap.parse_args()
    props = load_props()
    out_root = Path(args.out)
    ids = list(props) if args.all or (args.cmd == "contact" and not args.ids) else args.ids
    bad = [i for i in ids if i not in props]
    if bad:
        ap.error(f"unknown prop ids: {bad}")
    if args.cmd == "contact":
        dst = out_root / f"_{args.name}.png"
        dirs = [str(out_root / i) for i in ids if (out_root / i).is_dir()]
        return subprocess.run([fg.PIL_PYTHON, "-I", __file__, "_sheet", str(dst), *dirs]).returncode
    if args.cmd == "check":
        for i in ids:
            print(json.dumps({i: check_glb(out_root / i / "model.glb", props[i])}, indent=1))
        return 0
    want = [s for s in args.stages.split(",") if s]
    if any(s not in STAGES for s in want):
        ap.error(f"--stages: choose from {STAGES}")
    failed: set[str] = set()
    for st in [s for s in STAGES if s in want]:  # stage-major: each model family loads once
        done_gpu = 0
        for pid in ids:
            if pid in failed:
                continue
            d = out_root / pid
            d.mkdir(parents=True, exist_ok=True)
            try:
                STAGE_FN[st](props[pid], d, args)
                if st in ("ref", "mesh"):
                    done_gpu += 1
                    if done_gpu % args.batch == 0:
                        log(f"batch of {args.batch}: ComfyUI /free")
                        fg.wait_idle()
                        comfy_free()
            except Exception as e:  # keep going with the next prop
                log(f"{pid}: {st} FAILED: {e}")
                man = fg.load_manifest(d)
                man.setdefault("errors", []).append({"stage": st, "error": str(e)[:2000],
                                                     "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
                fg.save_manifest(d, man)
                failed.add(pid)
        if st in ("ref", "mesh") and done_gpu:
            fg.wait_idle()
            comfy_free()
    if failed:
        log(f"failed: {' '.join(sorted(failed))}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

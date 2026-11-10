#!/usr/bin/env python3
"""Texture a mesh by projecting clean character views onto it (an alternative to tools/paint.py's SDXL repaint).

  ~/.cache/mf-venv/bin/python tools/ref_project.py <id> --source <src>

Uses out/<src>/<id>/ref.png (front) and views/{back,left,right}.png from tools/ref_views.py, which the multi-view mesh
was built from, so the silhouettes agree. Renders the mesh from six cameras (tools/blender_paint.py), scales and flows
each view onto its render's silhouette (paint/<view>_mask.png), then runs paint.py's coverage bake. The two
three-quarter cameras have no clean view: they stay the voxel colour. Writes textures/base_color_painted.png and
model.glb; re-run `forge.py lod` and the rig afterwards. Needs numpy, scipy, opencv, Pillow (the venv python).
Measured on one character: silhouette IoU raw 0.6-0.7 -> 0.8-0.9 aligned.
"""
import os, sys, json, math, shutil
from pathlib import Path
import numpy as np, cv2
sys.path.insert(0, str(Path.home() / "workspace/monster-forge/tools"))
import forge, paint
from PIL import Image
from scipy import ndimage

import argparse
ap = argparse.ArgumentParser()
ap.add_argument("id")
ap.add_argument("--source", required=True)
a = ap.parse_args()
src, cid, tag = a.source, a.id, "views"
mon = forge.load_registry(src)[cid]
d = forge.OUT_ROOT / src / cid; pdir = d / "paint"; tex = d / "textures"
man = forge.load_manifest(d); qa = man["qa"]
pdir.mkdir(exist_ok=True)
shutil.copyfile(tex / "base_color.png", tex / "base_color_voxel.png")
views = [{"name": v, "yaw": paint.VIEW_SPECS[v][0], "elev": paint.VIEW_SPECS[v][1], "hint": paint.VIEW_SPECS[v][2], "head": False}
         for v in ("front", "back", "left", "right", "front_left", "front_right")]
hb = paint.head_box(qa, mon["body_plan"])
hd = paint.head_view_dir(qa, mon["body_plan"])
base = math.degrees(math.atan2(-hd[0], hd[1]) - math.atan2(-paint.fwd_vec(qa)[0], paint.fwd_vec(qa)[1]))
views.append({"name": "head", "yaw": paint.VIEW_SPECS["head"][0] + base, "elev": paint.VIEW_SPECS["head"][1], "hint": "front view", "head": True})
request = {"views": views, "size": 1024, "fwd": paint.fwd_vec(qa), "head_box": hb}
paint.blender(forge, {"mode": "views", "src": str(d / "model.glb"), "voxel_tex": str(tex / "base_color_voxel.png"), "paint_dir": str(pdir), **request})
cj = json.loads((pdir / "cameras.json").read_text()); cj["request"] = request; (pdir / "cameras.json").write_text(json.dumps(cj, indent=1))
shutil.copyfile(d / "ref.png", pdir / "front_gen_views.png")
for v in ("back", "left", "right"):
    shutil.copyfile(d / "views" / f"{v}.png", pdir / f"{v}_gen_views.png")
for v in ("front_left", "front_right"):
    shutil.copyfile(pdir / f"{v}_color.png", pdir / f"{v}_gen_views.png")
# head close-up: tools/head_view.py's redraw if present, aligned by the eyes; otherwise the voxel colour (a no-op in the bake)
shutil.copyfile(pdir / "head_color.png", pdir / "head_gen.png")


def fgmask(im):
    a = np.asarray(im.convert("RGB")).astype(int)
    bg = a.min(axis=2) >= 244
    lab, _ = ndimage.label(bg)
    edge = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))) - {0}
    return ~np.isin(lab, list(edge))


def eyes(img, mask=None):
    """Two iris centres from magenta/blue-violet pixels (green lowest) in the central face band. A noisy render can show one iris
    much dimmer than the other, so when a mask is given the dimmer eye is mirrored from the stronger one about the mask's centre line."""
    a = np.asarray(img.convert("RGB").resize((1024, 1024))).astype(int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    iris = (b > 110) & (r > 90) & (g < np.minimum(r, b) - 45)
    band = np.zeros_like(iris)
    band[int(0.28 * 1024):int(0.62 * 1024), int(0.2 * 1024):int(0.8 * 1024)] = True  # hair rim glow and a magenta robe pattern lie outside
    iris = ndimage.binary_opening(iris & band, iterations=1)
    ys, xs = np.nonzero(iris)
    if len(xs) < 80:
        return None
    mid = np.median(xs)
    L, R = xs < mid, xs >= mid
    if L.sum() < 40 or R.sum() < 40:
        return None
    le, re = (xs[L].mean(), ys[L].mean()), (xs[R].mean(), ys[R].mean())
    if mask is not None:
        m = np.asarray(Image.fromarray((mask * 255).astype("uint8")).resize((1024, 1024))) > 127
        rows = slice(int(ys.mean()) - 40, int(ys.mean()) + 40)
        cx = np.nonzero(m[rows])[1].mean()
        if L.sum() > R.sum():
            re = (2 * cx - le[0], le[1])
        else:
            le = (2 * cx - re[0], re[1])
    return [le, re]


def align_head(pdir):
    """Similarity transform (scale, rotation, shift) of the redraw onto the mesh's head render from the two eye centres."""
    gen = Image.open(pdir / "head_gen_qwen.png").convert("RGB")
    W = H = 1024
    gen = gen.resize((W, H), Image.LANCZOS)
    ren = Image.open(pdir / "head_color.png").convert("RGB")
    rm = np.asarray(Image.open(pdir / "head_mask.png").convert("L")) > 127
    e1, e2 = eyes(ren, rm), eyes(gen)
    f = pdir / "head_eyes.json"  # optional manual override {"render": [[x,y],[x,y]], "redraw": [[x,y],[x,y]]} in 1024 px
    if f.exists():
        o = json.loads(f.read_text())
        e1, e2 = o.get("render", e1), o.get("redraw", e2)
    if not e1 or not e2:
        print("head: eyes not found, head view skipped")
        return
    (a1, a2), (b1, b2) = [np.array(x, float) for x in e1], [np.array(x, float) for x in e2]
    va, vb = a2 - a1, b2 - b1
    sc = np.linalg.norm(va) / np.linalg.norm(vb)
    ang = np.arctan2(va[1], va[0]) - np.arctan2(vb[1], vb[0])
    R = np.array([[np.cos(ang), -np.sin(ang)], [np.sin(ang), np.cos(ang)]]) * sc
    t = (a1 + a2) / 2 - R @ ((b1 + b2) / 2)
    M = np.hstack([R, t[:, None]]).astype("float32")
    out = cv2.warpAffine(np.asarray(gen), M, (W, H), flags=cv2.INTER_LANCZOS4, borderValue=(255, 255, 255))
    Image.fromarray(out).save(pdir / "head_gen.png")  # the bake reads <view>_gen.png
    # the redraw only matches the mesh in the face: weight the close-up with a feathered ellipse from brows to chin
    ed = np.linalg.norm(a2 - a1)
    cx, cy = (a1 + a2) / 2
    yy, xx = np.mgrid[0:H, 0:W].astype("float32")
    q = np.sqrt(((xx - cx) / (0.95 * ed)) ** 2 + ((yy - (cy + 0.35 * ed)) / (0.95 * ed)) ** 2)
    wmask = np.clip((1.0 - q) / 0.25, 0, 1)
    Image.fromarray((wmask * 255).astype("uint8")).convert("RGB").save(pdir / "head_weight.png")
    print("head aligned: eye distance render %.0f px, redraw %.0f px, scale %.2f, rotate %.1f deg" % (np.linalg.norm(va), np.linalg.norm(vb), sc, np.degrees(ang)))


if (pdir / "head_gen_qwen.png").exists():
    align_head(pdir)
report = {}
for v in ("front", "back", "left", "right", "front_left", "front_right"):
    gen = Image.open(pdir / f"{v}_gen_{tag}.png").convert("RGB")
    rm = np.asarray(Image.open(pdir / f"{v}_mask.png").convert("L")) > 127
    H, W = rm.shape
    gen = gen.resize((W, H), Image.LANCZOS)
    gm = fgmask(gen)
    # match scale + offset by bounding box first (the edit model re-frames), then refine with flow
    def bb(m):
        ys, xs = np.nonzero(m); return xs.min(), ys.min(), xs.max(), ys.max()
    gb, rb = bb(gm), bb(rm)
    s = (rb[3] - rb[1]) / max(1, gb[3] - gb[1])
    M = np.array([[s, 0, rb[0] - gb[0] * s], [0, s, rb[1] - gb[1] * s]], dtype=np.float32)
    # centre horizontally on the box centres rather than the left edge
    M[0, 2] = (rb[0] + rb[2]) / 2 - s * (gb[0] + gb[2]) / 2
    g2 = cv2.warpAffine(np.asarray(gen), M, (W, H), flags=cv2.INTER_LANCZOS4, borderValue=(255, 255, 255))
    m2 = cv2.warpAffine(gm.astype("uint8") * 255, M, (W, H), flags=cv2.INTER_LINEAR) > 127
    a = cv2.GaussianBlur(ndimage.distance_transform_edt(m2).clip(0, 40).astype("float32") / 40, (0, 0), 3)
    b = cv2.GaussianBlur(ndimage.distance_transform_edt(rm).clip(0, 40).astype("float32") / 40, (0, 0), 3)
    a8, b8 = (a * 255).astype("uint8"), (b * 255).astype("uint8")
    flow = cv2.calcOpticalFlowFarneback(b8, a8, None, 0.5, 5, 41, 5, 7, 1.5, 0)  # for each render pixel, where it sits in the gen
    yy, xx = np.mgrid[0:H, 0:W].astype("float32")
    g3 = cv2.remap(g2, xx + flow[..., 0], yy + flow[..., 1], cv2.INTER_LINEAR, borderValue=(255, 255, 255))
    m3 = cv2.remap(m2.astype("uint8") * 255, xx + flow[..., 0], yy + flow[..., 1], cv2.INTER_LINEAR) > 127
    iou0 = (gm & rm).sum() / max(1, (gm | rm).sum()); iou1 = (m2 & rm).sum() / max(1, (m2 | rm).sum()); iou2 = (m3 & rm).sum() / max(1, (m3 | rm).sum())
    report[v] = [round(float(x), 3) for x in (iou0, iou1, iou2)]
    Image.fromarray(g3).save(pdir / f"{v}_gen.png")
print("silhouette IoU raw / box-aligned / flow-aligned:", json.dumps(report))
ms = int(os.environ.get("TEX_SIZE", man["mesh"]["settings"]["texture_size"]))
paint.blender(forge, {"mode": "bake", "src": str(d / "model.glb"), "dst": str(d / "model.glb"), "voxel_tex": str(tex / "base_color_voxel.png"), "paint_dir": str(pdir),
                      "texture_size": ms, "out_tex": str(tex / "base_color_painted.png"), "coverage": str(tex / "paint_coverage.png"),
                      "report": str(pdir / "bake.json"), "params": {"color_match": 0.0}})
print("painted", json.loads((pdir / "bake.json").read_text())["painted_fraction"])

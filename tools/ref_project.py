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
import sys, json, shutil
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
request = {"views": views, "size": 1024, "fwd": paint.fwd_vec(qa), "head_box": None}
paint.blender(forge, {"mode": "views", "src": str(d / "model.glb"), "voxel_tex": str(tex / "base_color_voxel.png"), "paint_dir": str(pdir), **request})
cj = json.loads((pdir / "cameras.json").read_text()); cj["request"] = request; (pdir / "cameras.json").write_text(json.dumps(cj, indent=1))
shutil.copyfile(d / "ref.png", pdir / "front_gen_views.png")
for v in ("back", "left", "right"):
    shutil.copyfile(d / "views" / f"{v}.png", pdir / f"{v}_gen_views.png")
for v in ("front_left", "front_right"):
    shutil.copyfile(pdir / f"{v}_color.png", pdir / f"{v}_gen_views.png")


def fgmask(im):
    a = np.asarray(im.convert("RGB")).astype(int)
    bg = a.min(axis=2) >= 244
    lab, _ = ndimage.label(bg)
    edge = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))) - {0}
    return ~np.isin(lab, list(edge))


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
ms = man["mesh"]["settings"]["texture_size"]
paint.blender(forge, {"mode": "bake", "src": str(d / "model.glb"), "dst": str(d / "model.glb"), "voxel_tex": str(tex / "base_color_voxel.png"), "paint_dir": str(pdir),
                      "texture_size": ms, "out_tex": str(tex / "base_color_painted.png"), "coverage": str(tex / "paint_coverage.png"),
                      "report": str(pdir / "bake.json"), "params": {"color_match": 0.0}})
print("painted", json.loads((pdir / "bake.json").read_text())["painted_fraction"])

#!/usr/bin/env python3
"""Score image-to-3D mesh candidates (docs/mesh-quality.md). Runs under forge.PIL_PYTHON (numpy, scipy, trimesh,
Pillow). Input: a JSON job {ref_mask, body_plan, front_view, candidates: [{tag, path}]}; prints one JSON line
{candidates: [{tag, score, metrics}], best}.

Every metric is read off the high-poly mesh (the remesh output, Pixal3D raw frame: glTF Y up, the reference
camera on +Z):
  * sil_iou    silhouette IoU against the reference's mask from the reference's camera (fov and elevation fitted)
  * symmetry   the best vertical mirror plane: mean reflected nearest-neighbour distance / bbox diagonal (err),
               and contrast (median over bearings / best): the facing check's mirror-plane confidence
  * legs       connected parts of horizontal cross-sections in the leg band (6-22% of the height), the mode
               over 5 slices, against the plan's leg count; arms (bipeds) parts at 45-60% height (3 = both free)
  * shards     surface area outside the largest connected piece, and the number of pieces over 0.2% of it
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree

LEGS = {"biped": (2, 2), "winged_biped": (2, 2), "quadruped": (4, 4), "winged_quadruped": (4, 4),
        "insectoid": (6, 8), "centauroid": (4, 4)}
WEIGHTS = {"sil": 0.35, "sym": 0.2, "legs": 0.25, "arms": 0.05, "clean": 0.15}


def load(path):
    import trimesh
    m = trimesh.load(path, force="mesh")
    return m


def samples(m, n=150000):
    import trimesh
    pts, _ = trimesh.sample.sample_surface(m, n, seed=0)
    return np.asarray(pts, float)


def _rot(az, el):
    a, e = np.radians(az), np.radians(el)
    ry = np.array([[np.cos(-a), 0, np.sin(-a)], [0, 1, 0], [-np.sin(-a), 0, np.cos(-a)]])
    rx = np.array([[1, 0, 0], [0, np.cos(e), -np.sin(e)], [0, np.sin(e), np.cos(e)]])
    return rx @ ry


def project(P, az=0.0, el=0.0, fov=30.0):
    Q = P @ _rot(az, el).T
    dist = 0.55 / np.tan(np.radians(fov) / 2)
    return Q[:, :2] / (dist - Q[:, 2])[:, None] * dist


def splat(xy, res=160, pad=1.1, close=2):
    lo, hi = xy.min(0), xy.max(0)
    c, h = (lo + hi) / 2, (hi - lo).max() / 2 * pad
    px = ((xy - c) / h * 0.5 + 0.5) * res
    ix = np.clip(px[:, 0].astype(int), 0, res - 1)
    iy = np.clip((res - 1 - px[:, 1]).astype(int), 0, res - 1)
    im = np.zeros((res, res), bool)
    im[iy, ix] = True
    return ndimage.binary_fill_holes(ndimage.binary_closing(im, iterations=close, border_value=0))


def mask_norm(mask, res=160, pad=1.1):
    from PIL import Image
    ys, xs = np.nonzero(mask)
    cy, cx = (ys.min() + ys.max()) / 2, (xs.min() + xs.max()) / 2
    h = max(ys.max() - ys.min(), xs.max() - xs.min()) / 2 * pad
    im = Image.fromarray((mask * 255).astype("uint8")).crop((int(cx - h), int(cy - h), int(cx + h), int(cy + h)))
    return np.asarray(im.resize((res, res), Image.BILINEAR)) > 127


def iou(a, b):
    u = (a | b).sum()
    return float((a & b).sum() / u) if u else 0.0


def sil_iou(P, ref):
    best = (0.0, None, None, None)
    for az in (0,):
        for fov in (15, 25, 35, 45, 55, 65):
            for el in (-10, 0, 10, 20, 30):
                v = iou(splat(project(P, az, el, fov)), ref)
                if v > best[0]:
                    best = (v, fov, el, az)
    return best


def symmetry(P):
    """Best vertical mirror plane through the centroid (bearing in the XZ plane, 0 = the x = 0 plane)."""
    sub = P[np.random.default_rng(0).choice(len(P), min(len(P), 30000), replace=False)]
    c = sub.mean(0)
    Q = sub - c
    tree = cKDTree(Q)
    diag = float(np.linalg.norm(P.max(0) - P.min(0)))
    errs = []
    for b in range(0, 180, 4):
        n = np.array([np.cos(np.radians(b)), 0.0, np.sin(np.radians(b))])  # plane normal
        R = Q - 2 * (Q @ n)[:, None] * n
        dd, _ = tree.query(R, k=1)
        errs.append(float(dd.mean()) / diag)
    errs = np.array(errs)
    i = int(errs.argmin())
    return {"err": round(float(errs[i]), 4), "bearing": i * 4,
            "contrast": round(float(np.median(errs) / max(errs[i], 1e-6)), 2)}


def section_parts(P, y0, y1, res=96, min_frac=0.004):
    """Connected parts of the points with y0 <= y < y1, projected onto XZ (footprint-scaled grid)."""
    sel = P[(P[:, 1] >= y0) & (P[:, 1] < y1)][:, [0, 2]]
    if len(sel) < 30:
        return 0, []
    lo, hi = P[:, [0, 2]].min(0), P[:, [0, 2]].max(0)
    span = (hi - lo).max()
    px = ((sel - lo) / span * (res - 1)).astype(int)
    im = np.zeros((res, res), bool)
    im[px[:, 1], px[:, 0]] = True
    im = ndimage.binary_fill_holes(ndimage.binary_closing(im, iterations=1, border_value=0))
    lab, n = ndimage.label(im)
    sizes = ndimage.sum(im, lab, range(1, n + 1)) if n else []
    keep = [s for s in sizes if s >= min_frac * res * res * 0.05 and s >= 3]
    return len(keep), sorted(keep, reverse=True)


def legs(P, plan):
    y0, y1 = P[:, 1].min(), P[:, 1].max()
    h = y1 - y0
    counts = []
    for f in (0.06, 0.10, 0.14, 0.18, 0.22):
        n, _ = section_parts(P, y0 + (f - 0.02) * h, y0 + (f + 0.02) * h)
        counts.append(n)
    vals, freq = np.unique(counts, return_counts=True)
    mode = int(vals[freq.argmax()])
    arms = None
    if plan in ("biped", "winged_biped"):
        a = [section_parts(P, y0 + (f - 0.02) * h, y0 + (f + 0.02) * h)[0] for f in (0.45, 0.5, 0.55, 0.6)]
        arms = {"counts": a, "free": float(np.mean([c >= 3 for c in a]))}
    want = LEGS.get(plan)
    if want is None:
        score = None
    else:  # the best of the four lowest slices (tails, wing tips and coats add or merge parts higher up) and how
        lo, hi = want  # many of all five slices agree
        miss = [0 if lo <= c <= hi else min(abs(c - lo), abs(c - hi)) for c in counts]
        score = 0.6 * max(0.0, 1.0 - 0.35 * min(miss[:4])) + 0.4 * float(np.mean([m == 0 for m in miss]))
    return {"counts": counts, "mode": mode, "want": list(want) if want else None, "score": score, "arms": arms}


def shards(m):
    """Floating shards: pieces under 5 % of the surface besides the largest (a coat or a wing meshed as its own
    shell is a part, not a shard; goblin s1 had a 29 % second shell that looked right)."""
    parts = m.split(only_watertight=False)
    if len(parts) <= 1:
        return {"pieces": 1, "big_pieces": 1, "frac": 0.0, "shard_pieces": 0}
    areas = np.array(sorted((p.area for p in parts), reverse=True))
    tot = areas.sum()
    small = areas[1:][areas[1:] < 0.05 * tot]
    return {"pieces": int(len(parts)), "big_pieces": int((areas >= 0.05 * tot).sum()),
            "frac": round(float(small.sum() / tot), 4), "shard_pieces": int((small > 0.0005 * tot).sum())}


def score_one(path, ref, plan, front_view):
    m = load(path)
    P = samples(m)
    s_iou, fov, el, az = sil_iou(P, ref) if ref is not None else (None, None, None, None)
    sym = symmetry(P)
    lg = legs(P, plan)
    sh = shards(m)
    # 0..1 parts
    sil = s_iou
    sym_s = float(np.clip(1 - (sym["err"] - 0.004) / 0.02, 0, 1)) * float(np.clip((sym["contrast"] - 1) / 5, 0, 1))
    if front_view:  # a front-view reference: the mirror plane is x = 0 (bearing 0)
        off = min(sym["bearing"], 180 - sym["bearing"])
        sym_s *= float(np.clip(1 - max(off - 10, 0) / 40, 0, 1))
    clean = float(np.clip(1 - sh["frac"] * 10, 0, 1)) * 0.97 ** min(sh["shard_pieces"], 20)
    parts = {"sil": sil, "sym": sym_s, "legs": lg["score"], "arms": (lg["arms"] or {}).get("free"), "clean": clean}
    wsum = sum(WEIGHTS[k] for k, v in parts.items() if v is not None)
    total = sum(WEIGHTS[k] * v for k, v in parts.items() if v is not None) / wsum
    return {"score": round(total, 4), "parts": {k: (None if v is None else round(v, 3)) for k, v in parts.items()},
            "metrics": {"sil_iou": None if s_iou is None else round(s_iou, 3), "sil_fov": fov, "sil_el": el, "symmetry": sym, "legs": lg,
                        "shards": sh, "faces": int(len(m.faces))}}


def main():
    job = json.loads(open(sys.argv[1]).read() if sys.argv[1].endswith(".json") else sys.argv[1])
    from PIL import Image
    rm = job.get("ref_mask")
    ref = mask_norm(np.asarray(Image.open(rm).convert("L")) > 127) if rm and Path(rm).exists() else None
    out = []
    for c in job["candidates"]:
        try:
            r = score_one(c["path"], ref, job.get("body_plan"), job.get("front_view", False))
        except Exception as e:  # a broken candidate scores 0
            r = {"score": 0.0, "error": str(e)[:300]}
        out.append({"tag": c["tag"], **r})
    best = max(range(len(out)), key=lambda i: (out[i]["score"], -i))
    print(json.dumps({"candidates": out, "best": best}))


if __name__ == "__main__":
    main()

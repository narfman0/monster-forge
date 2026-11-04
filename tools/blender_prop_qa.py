"""Headless Blender QA pass for one prop GLB: orient it into prop space, scale it, put the
origin at the grip, export model.glb and render an axes sheet.

Run by tools/props.py:  blender -b --factory-startup -P tools/blender_prop_qa.py -- '<job json>'

Prop space (docs/equipment.md), in glTF terms: origin at the grip point (centre of the
fist), +Y along the blade or shaft toward the tip (the head for maces and axes), -Z out of
the fist: the cutting edge or striking face, the way the arrow points for bows and
crossbows, out of the face for shields. Blender is +Z up and the exporter runs with
export_yup, so in Blender that frame is: glTF +Y = Blender +Z, glTF -Z = Blender +Y,
glTF +X = Blender +X. Everything below works in Blender axes and names them that way.

How the axes are found (each decision records a signal and a confidence 0-1):

1. Area-weighted surface samples, PCA: e1 (longest), e2 (widest across), e3 (thinnest).
   Tiny loose islands (< 0.5% of the area, after welding UV seams) are deleted first.
2. Tip end (which end of e1 becomes +Y), by category rule (RULES below):
   - `thin_end` (blades): the blade is flat, the hilt is round. The end third whose mean
     thickness along e3 is smaller is the blade, so the tip. Backed by the guard: the
     widest cross-section, when it sits in an end third, marks the hilt end.
   - `head_end` (axes, maces, hammers, polearms, staves, wands): the end third with the
     larger cross-section (e2 x e3 extent) is the head.
   - `thick_handle` (whip): the opposite, the thick end is the handle.
   - `symmetric` (bows): the generator's up (the reference image is drawn upright).
3. Edge direction (which way of e2 becomes -Z):
   - `symmetric` (double-edged blades, maces, spears, staves): sign of the generator's
     +X, so it is stable but arbitrary; both sides are equivalent.
   - `convex` (scimitar): the centre line of the blade bulges toward the cutting edge.
   - `extension` (axes, glaive, sickle): the head reaches farther from the haft line on
     the blade side.
   - `blunt` (hammers): the striking face is blocky, the back spike thin: the side whose
     outer third holds more surface.
   - `stave_forward` (bows): at the grip the stave is in front of the string, the tips are
     behind it (the string side): -Z = from the tips toward the grip.
4. Crossbows and firearms: e1 is the stock or barrel and becomes -Z (the prod or muzzle
   end: the end with the wider span across, the prod, for crossbows; the end without the
   hanging pistol grip for firearms), +Y is the generator's up (the reference is drawn
   level, side on), so +Y points from the grip up to the top of the stock.
5. Shields: e3 (thin) is the face normal; -Z is the face, the side toward the reference
   camera (the prompt asks for a front view); +Y is the generator's up (the top rim).
6. Grip (origin), along +Y: blades: the middle of the narrow handle between pommel and
   guard (a 1.8x jump in width ends the handle); two-handed blades: the lower hand, half a
   fist above the pommel. Hafted weapons: 0.06 + 0.04 x length m above the butt (above a
   knob). Polearms and staves: a fraction of the length (GRIP_FRAC). Bows: the stave at
   mid-length, centred in its thickness. Crossbows: the stock 30% from the butt; hand
   crossbows and pistols: the pistol grip under the rear of the stock. Shields: the back
   surface at the centre (the strap or handle).
   For blades and hafted weapons the handle's own direction is measured and the prop is
   tilted about X so +Y runs along the handle (curved blades: the blade leans).
7. Scale: the extent along the length axis (+Y; -Z for crossbows and firearms; the face's
   larger side for shields) = length_m.

Bows: only the largest island (the stave) is kept, faces more than 6 cm behind the stave
in the middle 80% (fused string ribbons) are cut, and a straight 3-sided string with its
own material is added between the limb tips (Pixal3D cannot keep a 2 mm string).
Two-handed props get an `offhand_grip` empty (child of the mesh) for the off hand's IK.

Failure modes (they are why there is an override for each decision, see tools/props.py):
reconstructions where the blade is as thick as the grip (thin_end becomes weak, then the
guard decides); double-bitted axes and symmetric hammers (edge side arbitrary, confidence
~0); a coiled whip (no long axis); bows drawn nearly straight (stave and string one blob,
stave_forward weak); a crossbow whose prod is longer than its stock (e1 = prod: wrong axis).

Job keys: src, dst, report, thumb, id, category, grip (1|2), length_m, rules (tip, edge),
grip_frac (fraction of length from the butt, overrides the measured grip), flip_tip,
flip_edge (bool), roll_deg (extra rotation about +Y), label.
"""
import json
import math
import os
import sys
import time

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector

job = json.loads(sys.argv[sys.argv.index("--") + 1])
t_start = time.time()
report = {"id": job.get("id"), "category": job.get("category")}
CAT = job["category"]
L = float(job["length_m"])

# category -> (tip rule, edge rule). Ids listed in RULE_BY_ID win.
RULES = {
    "light_blade": ("thin_end", "symmetric"), "blade": ("thin_end", "symmetric"),
    "heavy_blade": ("thin_end", "symmetric"),
    "axe": ("head_end", "extension"), "heavy_axe": ("head_end", "extension"),
    "bludgeon": ("head_end", "symmetric"), "heavy_bludgeon": ("head_end", "symmetric"),
    "polearm": ("head_end", "symmetric"), "thrown": ("head_end", "symmetric"),
    "staff": ("head_end", "symmetric"), "implement": ("head_end", "symmetric"),
    "flexible": ("thick_handle", "symmetric"), "bow": ("symmetric", "stave_forward"),
    "crossbow": ("crossbow", "crossbow"), "firearm": ("firearm", "firearm"),
    "shield": ("shield", "shield"),
}
RULE_BY_ID = {"scimitar": ("thin_end", "convex"), "sickle": ("thin_end", "extension"),
              "glaive": ("head_end", "extension"), "warhammer": ("head_end", "blunt"),
              "light-hammer": ("head_end", "blunt")}
tip_rule, edge_rule = RULE_BY_ID.get(job.get("id"), RULES.get(CAT, ("head_end", "symmetric")))
tip_rule, edge_rule = (job.get("rules") or {}).get("tip", tip_rule), (job.get("rules") or {}).get("edge", edge_rule)
report["rules"] = {"tip": tip_rule, "edge": edge_rule}
BLADES = {"light_blade", "blade", "heavy_blade"}
HAFTED = {"axe", "heavy_axe", "bludgeon", "heavy_bludgeon", "implement", "flexible"}
# fraction of the length from the butt to the (main hand) grip, and the off hand's
# distance along +Y for two-handed props (metres, docs/equipment.md: 0.3-0.6 m)
GRIP_FRAC = {"polearm": 0.4, "thrown": 0.45, "staff": 0.35}
GRIP_FRAC_2H = {"polearm": 0.28, "staff": 0.35}
GRIP_FRAC_ID = {"pike": 0.22}

# ---------------------------------------------------------------- load + clean
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=job["src"])
meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
if not meshes:
    raise SystemExit("no mesh in GLB")
for o in bpy.context.scene.objects:
    o.select_set(o.type == "MESH")
bpy.context.view_layer.objects.active = meshes[0]
bpy.ops.object.parent_clear(type="CLEAR_KEEP_TRANSFORM")
if len(meshes) > 1:
    bpy.ops.object.join()
obj = bpy.context.view_layer.objects.active
for o in list(bpy.context.scene.objects):
    if o != obj:
        bpy.data.objects.remove(o, do_unlink=True)
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
obj.name = job.get("id") or "prop"
me = obj.data


def verts_np():
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    return co.reshape(-1, 3)


def tri_arrays():
    me.calc_loop_triangles()
    t = np.empty(len(me.loop_triangles) * 3, np.int64)
    me.loop_triangles.foreach_get("vertices", t)
    p = np.empty(len(me.loop_triangles), np.int64)
    me.loop_triangles.foreach_get("polygon_index", p)
    return t.reshape(-1, 3), p


def transform(m):
    me.transform(m)
    me.update()


v = verts_np()
report["raw_dims"] = [round(float(x), 4) for x in np.ptp(v, 0)]

def drop_islands(frac, largest_only=False):
    """Loose islands: weld by position (glTF splits vertices along UV seams), union-find
    over triangle edges, delete islands under `frac` of the area (voxel floaters), or all
    but the largest. Returns (islands, area fractions, dropped islands, dropped faces)."""
    v = verts_np()
    tris, tpoly = tri_arrays()
    a, b, c = v[tris[:, 0]], v[tris[:, 1]], v[tris[:, 2]]
    tarea = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    key = np.round(v / (1e-5 * float(np.linalg.norm(np.ptp(v, 0))))).astype(np.int64)
    _, weld = np.unique(key, axis=0, return_inverse=True)
    weld = weld.ravel()
    parent = np.arange(weld.max() + 1)

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for e0, e1_ in np.concatenate([weld[tris[:, [0, 1]]], weld[tris[:, [1, 2]]]]):
        r0, r1 = find(e0), find(e1_)
        if r0 != r1:
            parent[r0] = r1
    roots = np.array([find(i) for i in weld[tris[:, 0]]])
    comp_area = {}
    for r, ar in zip(roots, tarea):
        comp_area[r] = comp_area.get(r, 0.0) + ar
    total_area = float(tarea.sum())
    if largest_only:
        keep = max(comp_area, key=comp_area.get)
        small = {r for r in comp_area if r != keep}
    else:
        small = {r for r, ar in comp_area.items() if ar < frac * total_area}
    fracs = sorted((round(ar / total_area, 4) for ar in comp_area.values()), reverse=True)[:6]
    if not small or len(small) == len(comp_area):
        return len(comp_area), fracs, 0, 0
    drop_polys = sorted(set(tpoly[np.isin(roots, list(small))].tolist()))
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.faces.ensure_lookup_table()
    bmesh.ops.delete(bm, geom=[bm.faces[i] for i in drop_polys], context="FACES")
    bmesh.ops.delete(bm, geom=[vv for vv in bm.verts if not vv.link_faces], context="VERTS")
    bm.to_mesh(me)
    bm.free()
    me.update()
    return len(comp_area), fracs, len(small), len(drop_polys)


# bows: keep only the stave (string fragments come out as dangling ribbons)
(report["islands"], report["islands_area_frac"], report["islands_dropped"],
 report["islands_dropped_faces"]) = drop_islands(0.005, largest_only=CAT == "bow")


def samples(n=60000, seed=0):
    """Area-uniform random points on the surface (the LOD0 faces are large: vertices or
    face centroids alone would give a lumpy profile)."""
    t, _ = tri_arrays()
    co = verts_np()
    a, b, c = co[t[:, 0]], co[t[:, 1]], co[t[:, 2]]
    ar = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(t), n, p=ar / ar.sum())
    r1, r2 = np.sqrt(rng.random(n)), rng.random(n)
    return (1 - r1)[:, None] * a[idx] + (r1 * (1 - r2))[:, None] * b[idx] + (r1 * r2)[:, None] * c[idx]


P = samples()
ctr = P.mean(0)
w_, vecs = np.linalg.eigh(np.cov((P - ctr).T))
order = np.argsort(w_)[::-1]
E = [vecs[:, i] for i in order]  # e1, e2, e3 (Blender coordinates, generator frame)
report["pca_sd"] = [round(float(math.sqrt(max(w_[i], 0))), 4) for i in order]
report["pca_axes_gen"] = [[round(float(x), 3) for x in e] for e in E]
GEN_UP, GEN_RIGHT, GEN_TOWARD_CAM = np.array([0, 0, 1.0]), np.array([1.0, 0, 0]), np.array([0, -1.0, 0])


def conf(x, full):
    """|signal| / full, clipped to [0, 1]."""
    return round(float(min(1.0, abs(x) / full)), 2)


def end_regions(t, lo=0.05, hi=0.35):
    t0, t1 = float(t.min()), float(t.max())
    span = t1 - t0
    a_ = (t > t0 + lo * span) & (t < t0 + hi * span)
    b_ = (t < t1 - lo * span) & (t > t1 - hi * span)
    return a_, b_


def ext(x, p=2):
    return float(np.percentile(x, 100 - p) - np.percentile(x, p)) if len(x) > 10 else 0.0


def bin_profile(t, cols, nb=48):
    """Per bin along t: (centre, {col: (p2, p98)}) for bins with enough samples."""
    t0, t1 = float(t.min()), float(t.max())
    edges = np.linspace(t0, t1, nb + 1)
    out = []
    for i in range(nb):
        m = (t >= edges[i]) & (t < edges[i + 1])
        if m.sum() < 20:
            out.append(None)
            continue
        out.append({"t": 0.5 * (edges[i] + edges[i + 1]),
                    **{k: (float(np.percentile(x[m], 2)), float(np.percentile(x[m], 98))) for k, x in cols.items()}})
    return out


decisions = {}

# ---------------------------------------------------------------- orientation
e1, e2, e3 = E
Pc = P - ctr
t, u, w = Pc @ e1, Pc @ e2, Pc @ e3
lo_end, hi_end = end_regions(t)

if CAT in ("crossbow", "firearm"):
    # e1 = stock/barrel -> forward (Blender +Y); generator up -> Blender +Z. The reference
    # is drawn level and side on, so the stock is taken as horizontal in the generator
    # frame (PCA alone tilts it toward the hanging grip and the prod).
    h = e1 - (e1 @ GEN_UP) * GEN_UP
    if np.linalg.norm(h) < 0.5:  # drawn standing on end: keep e1, up from the wider cross axis
        up = e2.copy()
        decisions["up"] = {"rule": "e2 (reference not level)", "confidence": 0.2}
    else:
        e1 = h / np.linalg.norm(h)
        up = GEN_UP.copy()
        decisions["up"] = {"rule": "generator up (drawn level)", "confidence": conf(np.linalg.norm(h), 1.0)}
    side = np.cross(up, e1)
    s = Pc @ side
    hv = Pc @ up
    if CAT == "crossbow":
        # the prod spans across (side direction, the depth of a side-on drawing)
        sa, sb = ext(s[lo_end]), ext(s[hi_end])
        fwd = e1 if sb > sa else -e1
        decisions["forward"] = {"rule": "prod end = wider span across", "span_ends_m_raw": [round(sa, 4), round(sb, 4)],
                                "confidence": conf((sb - sa) / max(sa + sb, 1e-9), 0.3)}
    else:
        # the pistol grip hangs below the rear of the barrel
        ha, hb = ext(hv[lo_end]), ext(hv[hi_end])
        fwd = e1 if hb < ha else -e1
        decisions["forward"] = {"rule": "muzzle end = no hanging grip", "height_ends_m_raw": [round(ha, 4), round(hb, 4)],
                                "confidence": conf((ha - hb) / max(ha + hb, 1e-9), 0.3)}
    Yb, Zb = fwd, up
elif CAT == "shield":
    n = e3 if (e3 @ GEN_TOWARD_CAM) > 0 else -e3  # face toward the reference camera
    decisions["face"] = {"rule": "face toward reference camera", "confidence": conf(n @ GEN_TOWARD_CAM, 0.9)}
    r = np.hypot(t, u)
    R = float(np.percentile(r, 98))
    wn = Pc @ n
    dome = float(wn[r < 0.3 * R].mean() - wn[r > 0.8 * R].mean())
    decisions["face"]["dome_m_raw"] = round(dome, 4)
    up = GEN_UP - (GEN_UP @ n) * n
    up /= np.linalg.norm(up)
    decisions["up"] = {"rule": "generator up (top rim)", "confidence": 0.8}
    Yb, Zb = n, up
else:
    # ---- tip end
    flip = 1.0
    if tip_rule == "thin_end":
        # mean thickness (e3 extent) over bins of each end third
        prof = bin_profile(t, {"u": u, "w": w})
        nb = len(prof)
        th = np.array([p["w"][1] - p["w"][0] if p else np.nan for p in prof])
        wd = np.array([p["u"][1] - p["u"][0] if p else np.nan for p in prof])
        a3, b3 = slice(int(0.05 * nb), int(0.35 * nb)), slice(int(0.65 * nb), int(0.95 * nb))
        ta, tb = float(np.nanmean(th[a3])), float(np.nanmean(th[b3]))
        sig = (ta - tb) / max(ta + tb, 1e-9)  # > 0: the high end is thinner -> tip at +e1
        g = int(np.nanargmax(wd))
        guard = 1 if g >= 0.65 * nb else (-1 if g <= 0.35 * nb else 0)  # +1: guard at the high end
        decisions["tip"] = {"rule": "thin_end", "thickness_ends_m_raw": [round(ta, 4), round(tb, 4)],
                            "guard_bin": g, "bins": nb, "guard_vote": -guard}
        if abs(sig) >= 0.08 or guard == 0:
            flip = 1.0 if sig > 0 else -1.0
            c_ = conf(sig, 0.3)
            if guard and -guard != flip:
                c_ = round(c_ * 0.5, 2)
                decisions["tip"]["note"] = "guard disagrees"
            decisions["tip"]["confidence"] = c_
        else:
            flip = -float(guard)
            decisions["tip"]["confidence"] = 0.5
            decisions["tip"]["note"] = "thickness weak; guard decides"
    elif tip_rule in ("head_end", "thick_handle"):
        sa = ext(u[lo_end]) * ext(w[lo_end])
        sb = ext(u[hi_end]) * ext(w[hi_end])
        sig = (sb - sa) / max(sa + sb, 1e-12)
        flip = 1.0 if sig > 0 else -1.0
        if tip_rule == "thick_handle":
            flip = -flip
        decisions["tip"] = {"rule": tip_rule, "section_ends_m2_raw": [round(sa, 6), round(sb, 6)],
                            "confidence": conf(sig, 0.4)}
    else:  # symmetric: keep the generator's up
        flip = 1.0 if (e1 @ GEN_UP) >= 0 else -1.0
        decisions["tip"] = {"rule": "symmetric (generator up)", "confidence": None}
    if job.get("flip_tip"):
        flip = -flip
        decisions["tip"]["override"] = "flip_tip"
    Zb = flip * e1
    t = Pc @ Zb
    lo_end, hi_end = end_regions(t)  # lo = butt / handle, hi = tip / head

    # ---- edge side (along e2)
    head = t > t.min() + 0.6 * np.ptp(t)
    handle = t < t.min() + 0.45 * np.ptp(t)
    esign = 1.0 if (e2 @ GEN_RIGHT) >= 0 else -1.0

    def haft_line(tq):
        """u of the haft's centre line at tq: a line fitted to the per-bin centres of the
        handle (an off-centre head tilts e1, so a constant would be biased)."""
        tb_ = np.linspace(t[handle].min(), t[handle].max(), 12)
        cs = [(x, np.median(u[handle & (np.abs(t - x) < 0.5 * (tb_[1] - tb_[0]))]))
              for x in tb_ if (handle & (np.abs(t - x) < 0.5 * (tb_[1] - tb_[0]))).sum() > 10]
        tt_, uu_ = np.array(cs).T
        k_, c_ = np.polyfit(tt_, uu_, 1)
        return k_ * tq + c_

    if edge_rule == "symmetric":
        decisions["edge"] = {"rule": "symmetric (generator +X)", "confidence": None}
    elif edge_rule == "convex":
        prof = bin_profile(t, {"u": u})
        pts = [(p["t"], 0.5 * (p["u"][0] + p["u"][1])) for p in prof if p]
        tt, uc = np.array(pts).T
        chord = uc[0] + (uc[-1] - uc[0]) * (tt - tt[0]) / max(tt[-1] - tt[0], 1e-9)
        bulge = float(np.mean((uc - chord)[1:-1]))
        esign = 1.0 if bulge > 0 else -1.0
        decisions["edge"] = {"rule": "convex side of the blade curve", "bulge_m_raw": round(bulge, 4),
                             "confidence": conf(bulge / np.ptp(t), 0.03)}
    elif edge_rule == "extension":
        uh = u[head] - haft_line(t[head])
        ap, an = float(np.percentile(uh, 99.5)), float(-np.percentile(uh, 0.5))
        esign = 1.0 if ap > an else -1.0
        decisions["edge"] = {"rule": "head reaches farther on the blade side", "reach_m_raw": [round(an, 4), round(ap, 4)],
                             "confidence": conf((ap - an) / max(ap + an, 1e-9), 0.3)}
    elif edge_rule == "blunt":
        uh = u[head] - haft_line(t[head])
        rp, rn = float(np.percentile(uh, 99.5)), float(-np.percentile(uh, 0.5))
        mp = int(((uh > 0.67 * rp)).sum())
        mn_ = int(((uh < -0.67 * rn)).sum())
        esign = 1.0 if mp > mn_ else -1.0
        decisions["edge"] = {"rule": "blocky face vs thin spike (surface in the outer third)",
                             "outer_samples": [mn_, mp], "confidence": conf((mp - mn_) / max(mp + mn_, 1), 0.4)}
    elif edge_rule == "stave_forward":
        mid = np.abs(t - (t.min() + 0.5 * np.ptp(t))) < 0.08 * np.ptp(t)
        tips = lo_end | hi_end
        d_ = float(u[mid].mean() - u[tips].mean())
        esign = 1.0 if d_ > 0 else -1.0
        decisions["edge"] = {"rule": "stave in front of the string", "grip_minus_tips_m_raw": round(d_, 4),
                             "confidence": conf(d_ / np.ptp(t), 0.03)}
    if job.get("flip_edge"):
        esign = -esign
        decisions["edge"]["override"] = "flip_edge"
    Yb = esign * e2
Yb = Yb - (Yb @ Zb) * Zb
Yb /= np.linalg.norm(Yb)
Xb = np.cross(Yb, Zb)
Rm = Matrix([list(Xb) + [0], list(Yb) + [0], list(Zb) + [0], [0, 0, 0, 1]])
transform(Matrix.Translation(Vector(-ctr)))
transform(Rm)
if job.get("roll_deg"):
    transform(Matrix.Rotation(math.radians(job["roll_deg"]), 4, "Z"))
    report["roll_deg"] = job["roll_deg"]
report["decisions"] = decisions

# length axis in Blender: Z (glTF +Y), Y for crossbows/firearms (glTF -Z)
LEN_AX = 1 if CAT in ("crossbow", "firearm") else 2


def rescale():
    co = verts_np()
    if CAT == "shield":
        cur = float(max(np.ptp(co[:, 0]), np.ptp(co[:, 2])))
    else:
        cur = float(np.ptp(co[:, LEN_AX]))
    s = L / cur
    transform(Matrix.Scale(s, 4))
    return s


scale = rescale()

# ---------------------------------------------------------------- grip point
P = samples(seed=1)
grip = {}
offhand = None


def section(zc, half, pts=None):
    pts = P if pts is None else pts
    return pts[np.abs(pts[:, 2] - zc) < half]


def handle_run(zlo_frac, zhi_frac, jump=1.8):
    """The narrow handle near the butt: bins (1 cm) between zlo and zhi of the length; the
    narrowest bin, grown up and down while the width stays under jump x that minimum."""
    z0 = float(P[:, 2].min())
    hb = 0.01
    zs = np.arange(z0 + zlo_frac * L, z0 + zhi_frac * L, hb)
    wds = []
    for zc in zs:
        s_ = section(zc, hb / 2)
        wds.append(max(ext(s_[:, 1], 1), ext(s_[:, 0], 1)) if len(s_) > 20 else np.nan)
    wds = np.array(wds)
    if np.all(np.isnan(wds)):
        return None
    i0 = int(np.nanargmin(wds))
    lim = jump * wds[i0]
    lo_i = i0
    while lo_i > 0 and not (wds[lo_i - 1] > lim):
        lo_i -= 1
    hi_i = i0
    while hi_i < len(zs) - 1 and not (wds[hi_i + 1] > lim):
        hi_i += 1
    return {"z_lo": float(zs[lo_i] - hb / 2), "z_hi": float(zs[hi_i] + hb / 2), "min_width": float(wds[i0]),
            "z_min_width": float(zs[i0]), "butt": z0, "reached_butt": lo_i == 0}


def tilt_to_handle(z_lo, z_hi):
    """Rotate about X so the handle's centre line runs along +Z (glTF +Y)."""
    zs = np.arange(z_lo, z_hi, 0.01)
    cs = []
    for zc in zs:
        s_ = section(zc, 0.005)
        if len(s_) > 20:
            cs.append((zc, 0.5 * (np.percentile(s_[:, 1], 2) + np.percentile(s_[:, 1], 98))))
    if len(cs) < 4:
        return 0.0
    zz, yy = np.array(cs).T
    slope = float(np.polyfit(zz, yy, 1)[0])
    ang = math.degrees(math.atan(slope))
    if abs(ang) > 25:
        return 0.0
    transform(Matrix.Rotation(math.radians(ang), 4, "X"))  # y' = y cos - z sin: removes the slope
    return ang


gfrac = job.get("grip_frac")
z0 = float(P[:, 2].min())
if CAT == "shield":
    co = P
    r = np.hypot(co[:, 0] - 0.5 * (co[:, 0].min() + co[:, 0].max()), co[:, 2] - 0.5 * (co[:, 2].min() + co[:, 2].max()))
    R = float(r.max())
    cen = r < 0.3 * R
    yc = co[cen, 1]
    back = yc[yc < np.median(yc)]
    gp = [0.5 * (co[:, 0].min() + co[:, 0].max()), float(np.median(back)) if len(back) else float(co[:, 1].min()),
          0.5 * (co[:, 2].min() + co[:, 2].max())]
    grip["method"] = "back surface at the centre (strap / handle)"
elif CAT in ("crossbow", "firearm"):
    y0 = float(P[:, 1].min())
    if gfrac is None and (CAT == "firearm" or job.get("id") == "hand-crossbow"):
        rear = P[P[:, 1] < y0 + 0.3 * L]
        below = rear[rear[:, 2] < np.median(P[:, 2])]
        pts = below if len(below) > 50 else rear
        gp = [float(np.median(pts[:, 0])), float(np.median(pts[:, 1])), float(np.median(pts[:, 2]))]
        grip["method"] = "pistol grip: rear 30%, below the stock centre line"
    else:
        f = gfrac if gfrac is not None else 0.3
        s_ = P[np.abs(P[:, 1] - (y0 + f * L)) < 0.02]
        gp = [0.5 * (np.percentile(s_[:, 0], 2) + np.percentile(s_[:, 0], 98)), y0 + f * L,
              0.5 * (np.percentile(s_[:, 2], 2) + np.percentile(s_[:, 2], 98))]
        grip["method"] = f"stock {f:.2f} of the length from the butt"
    if job.get("grip") == 2:
        offhand = [gp[0], gp[1] + 0.35 * L, gp[2]]
else:
    zg = None
    tilt = 0.0
    offhand_z = None
    if gfrac is not None:
        zg = z0 + gfrac * L
        grip["method"] = f"override grip_frac {gfrac}"
    elif CAT in BLADES:
        run = handle_run(0.0, 0.42 if job.get("grip") == 1 else 0.35)
        if run and run["z_hi"] - run["z_lo"] >= 0.04:
            tilt = tilt_to_handle(run["z_lo"], run["z_hi"])
            if tilt:
                scale *= rescale()
                P = samples(seed=2)
                z0 = float(P[:, 2].min())
                run = handle_run(0.0, 0.42 if job.get("grip") == 1 else 0.35) or run
            grip["handle_m"] = [round(run["z_lo"] - z0, 3), round(run["z_hi"] - z0, 3)]
            grip["handle_min_width_m"] = round(run["min_width"], 4)
            if job.get("grip") == 2:
                zg = run["z_lo"] + 0.055
                offhand_z = min(zg + 0.10, run["z_hi"] - 0.04)
                grip["method"] = "two-handed: half a fist above the pommel end of the handle"
            else:
                zg = 0.5 * (run["z_lo"] + run["z_hi"])
                grip["method"] = "middle of the narrow handle (pommel to guard)"
        else:
            zg = z0 + 0.12 * L
            grip["method"] = "no handle found: 12% of the length"
    elif CAT == "bow":
        zg = z0 + 0.5 * L
        grip["method"] = "mid-length, stave centre"
    elif CAT in GRIP_FRAC or job.get("id") in GRIP_FRAC_ID:
        f = GRIP_FRAC_ID.get(job.get("id")) or (GRIP_FRAC_2H if job.get("grip") == 2 else GRIP_FRAC).get(CAT, 0.4)
        zg = z0 + f * L
        grip["method"] = f"{f:.2f} of the length from the butt"
    else:  # hafted: a fist above the butt (above a knob)
        run = handle_run(0.0, 0.35)
        base = z0
        if run and not run["reached_butt"]:
            base = run["z_lo"]
        if run and run["z_hi"] - run["z_lo"] >= 0.06:
            tilt = tilt_to_handle(run["z_lo"], min(run["z_hi"], run["z_lo"] + 0.3))
            if tilt:
                scale *= rescale()
                P = samples(seed=2)
                z0 = float(P[:, 2].min())
                base = z0 if base == run["butt"] else base - run["butt"] + z0
        zg = base + 0.06 + 0.04 * L
        grip["method"] = "hafted: 0.06 + 0.04 x length above the butt" + (" (above the knob)" if base > z0 else "")
        if run:
            grip["handle_m"] = [round(run["z_lo"] - run["butt"], 3), round(run["z_hi"] - run["butt"], 3)]
    if tilt:
        grip["handle_tilt_deg"] = round(tilt, 2)
    s_ = section(zg, 0.015)
    if len(s_) < 20:
        s_ = section(zg, 0.04)
    if CAT == "bow":
        stave = s_[s_[:, 1] > s_[:, 1].max() - 0.04]  # forward-most 4 cm: the stave, not the string
        s_ = stave if len(stave) > 10 else s_
    gp = [0.5 * (np.percentile(s_[:, 0], 2) + np.percentile(s_[:, 0], 98)),
          0.5 * (np.percentile(s_[:, 1], 2) + np.percentile(s_[:, 1], 98)), zg]
    if job.get("grip") == 2:
        if offhand_z is not None:
            offhand = [gp[0], gp[1], offhand_z]
        else:
            d_ = {"polearm": 0.5, "staff": 0.54, "heavy_axe": 0.35, "heavy_bludgeon": 0.3}.get(CAT, 0.35)
            if job.get("id") == "pike":
                d_ = 0.6
            offhand = [gp[0], gp[1], zg + d_]
gp = [float(x) for x in gp]
transform(Matrix.Translation(Vector((-gp[0], -gp[1], -gp[2]))))
report["scale_factor"] = round(float(scale), 5)
co = verts_np()
mn, mx = co.min(0), co.max(0)
# report everything in glTF terms: (x, y, z)_gltf = (x, z, -y)_blender
to_gltf = lambda p: [round(float(p[0]), 4), round(float(p[2]), 4), round(float(-p[1]), 4)]
report["bounds_min_gltf"] = to_gltf([mn[0], mx[1], mn[2]])  # glTF z = -Blender y: min z from max y
report["bounds_max_gltf"] = to_gltf([mx[0], mn[1], mx[2]])
report["dims_gltf_m"] = [round(float(np.ptp(co[:, 0])), 4), round(float(np.ptp(co[:, 2])), 4), round(float(np.ptp(co[:, 1])), 4)]
report["length_m"] = L
report["length_axis_gltf"] = "-Z" if LEN_AX == 1 else ("max(X, Y)" if CAT == "shield" else "+Y")
if offhand is not None:
    report["offhand_grip_gltf"] = to_gltf([offhand[0] - gp[0], offhand[1] - gp[1], offhand[2] - gp[2]])
report["grip"] = grip
report["grip_from_butt_m"] = round(float(-mn[2]), 4) if LEN_AX == 2 else round(float(-mn[1]), 4)
confs = [d.get("confidence") for d in decisions.values() if d.get("confidence") is not None]
report["orientation_confidence"] = round(min(confs), 2) if confs else None

# ---------------------------------------------------------------- bow string
if CAT == "bow":
    # String fragments fused to the stave hang behind it as ribbons: in the middle 80% of
    # the length, delete faces more than 6 cm (for 1.8 m) behind the stave's front at
    # their height (front = forward-most surface in a 2 cm slab). The stave is ~3 cm deep.
    co = verts_np()
    me.calc_loop_triangles()
    fc = np.array([pp.center[:] for pp in me.polygons])
    zb = np.floor(co[:, 2] / 0.02).astype(int)
    front = {}
    for zi, yv in zip(zb, co[:, 1]):
        front[zi] = max(front.get(zi, -1e9), yv)
    lim = 0.06 * L / 1.8
    fz = np.floor(fc[:, 2] / 0.02).astype(int)
    bad = [i for i in range(len(fc)) if abs(fc[i, 2]) < 0.4 * L and fc[i, 1] < front.get(fz[i], 1e9) - lim]
    if bad:
        bm = bmesh.new()
        bm.from_mesh(me)
        bm.faces.ensure_lookup_table()
        bmesh.ops.delete(bm, geom=[bm.faces[i] for i in bad], context="FACES")
        bmesh.ops.delete(bm, geom=[vv for vv in bm.verts if not vv.link_faces], context="VERTS")
        bm.to_mesh(me)
        bm.free()
        me.update()
    report["bow_ribbon_faces_dropped"] = len(bad)
    report["bow_specks_dropped"] = drop_islands(0, largest_only=True)[2]
if CAT == "bow" and job.get("add_string", True):
    # Pixal3D cannot keep a 2 mm string; the reference is drawn without one (or it was
    # dropped above), so add a straight 3-sided string between the two limb tips, set
    # 1 cm inside each tip, in its own plain material.
    co = verts_np()
    top, bot = co[np.argmax(co[:, 2])], co[np.argmin(co[:, 2])]
    a_, b_ = Vector(top) + Vector((0, 0, -0.01)), Vector(bot) + Vector((0, 0, 0.01))
    r_ = 0.003
    d_ = (a_ - b_).normalized()
    n1 = d_.orthogonal().normalized()
    n2 = d_.cross(n1)
    bm = bmesh.new()
    bm.from_mesh(me)
    ring = lambda c_: [bm.verts.new(c_ + r_ * (math.cos(k * 2.094) * n1 + math.sin(k * 2.094) * n2)) for k in range(3)]
    ra, rb = ring(a_), ring(b_)
    smat = bpy.data.materials.new("bowstring")
    smat.use_nodes = True
    smat.node_tree.nodes["Principled BSDF"].inputs[0].default_value = (0.85, 0.8, 0.65, 1)
    smat.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = 0.8
    me.materials.append(smat)
    mi = len(me.materials) - 1
    for k in range(3):
        f_ = bm.faces.new((ra[k], ra[(k + 1) % 3], rb[(k + 1) % 3], rb[k]))
        f_.material_index = mi
    bm.to_mesh(me)
    bm.free()
    me.update()
    report["bow_string"] = {"top_gltf": to_gltf(a_), "bottom_gltf": to_gltf(b_), "radius_m": r_,
                            "min_z_gap_m": round(float(min(a_.y, b_.y)), 4)}

# ---------------------------------------------------------------- stats + export
report["tris"] = sum(len(p.vertices) - 2 for p in me.polygons)
report["verts"] = len(me.vertices)
report["materials"] = [m.name for m in me.materials if m]
report["textures"] = [{"name": i.name, "size": list(i.size)} for i in bpy.data.images if i.size[0] > 0]
report["has_uv"] = len(me.uv_layers) > 0
bpy.ops.object.select_all(action="DESELECT")
obj.select_set(True)
if offhand is not None:
    # marker node for the off hand's IK target (docs/equipment.md), child of the prop
    emp = bpy.data.objects.new("offhand_grip", None)
    emp.empty_display_type = "ARROWS"
    emp.empty_display_size = 0.05
    emp.location = Vector((offhand[0] - gp[0], offhand[1] - gp[1], offhand[2] - gp[2]))
    bpy.context.scene.collection.objects.link(emp)
    emp.parent = obj
    emp.select_set(True)
bpy.ops.export_scene.gltf(filepath=job["dst"], export_format="GLB", use_selection=True,
                          export_yup=True, export_apply=True)

# ---------------------------------------------------------------- axes sheet
scene = bpy.context.scene
scene.render.engine = "BLENDER_WORKBENCH"
sh = scene.display.shading
sh.light = "STUDIO"
sh.color_type = "TEXTURE"
sh.background_type = "VIEWPORT"
sh.background_color = (0.82, 0.82, 0.82)
scene.view_settings.view_transform = "Standard"
TILE = int(job.get("tile_px", 384))
scene.render.resolution_x = scene.render.resolution_y = TILE
scene.render.image_settings.file_format = "PNG"


def flat_mat(name, rgb):
    m = bpy.data.materials.new(name)
    m.diffuse_color = (*rgb, 1)
    m.use_nodes = True
    m.node_tree.nodes["Principled BSDF"].inputs[0].default_value = (*rgb, 1)
    return m


size = max(L * 0.3, 0.12)
helpers = []


def arrow(direction, rgb, label):
    """Shaft + cone from the origin along `direction` (Blender axes), with a text label."""
    mat = flat_mat(label, rgb)
    d = Vector(direction).normalized()
    rot = d.to_track_quat("Z", "Y").to_euler()
    r = size * 0.025
    bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=size * 0.8, location=d * size * 0.4, rotation=rot, vertices=12)
    sh_ = bpy.context.object
    bpy.ops.mesh.primitive_cone_add(radius1=r * 3, depth=size * 0.2, location=d * size * 0.9, rotation=rot, vertices=12)
    cone = bpy.context.object
    bpy.ops.object.text_add(location=d * size * 1.12)
    txt = bpy.context.object
    txt.data.body = label
    txt.data.size = size * 0.22
    txt.data.align_x = "CENTER"
    for o in (sh_, cone, txt):
        o.data.materials.append(mat)
        o.show_in_front = True
        helpers.append(o)
    return txt


labels = [arrow((0, 0, 1), (0.1, 0.75, 0.2), "+Y"), arrow((0, 1, 0), (0.15, 0.4, 1.0), "-Z"),
          arrow((1, 0, 0), (0.9, 0.15, 0.15), "+X")]
bpy.ops.mesh.primitive_uv_sphere_add(radius=size * 0.06, location=(0, 0, 0))
dot = bpy.context.object
dot.data.materials.append(flat_mat("grip", (1.0, 0.1, 0.9)))
dot.show_in_front = True
helpers.append(dot)
if offhand is not None:
    bpy.ops.mesh.primitive_uv_sphere_add(radius=size * 0.05, location=emp.location)
    o2 = bpy.context.object
    o2.data.materials.append(flat_mat("offhand", (1.0, 0.6, 0.0)))
    o2.show_in_front = True

cam_data = bpy.data.cameras.new("cam")
cam_data.type = "ORTHO"
cam = bpy.data.objects.new("cam", cam_data)
scene.collection.objects.link(cam)
scene.camera = cam
allco = np.concatenate([co, np.array([[0, 0, size * 1.3], [0, size * 1.3, 0], [size * 1.3, 0, 0], [0, 0, 0]])])
bmin, bmax = allco.min(0), allco.max(0)
center = Vector(((bmin + bmax) / 2).tolist())
rad = float(np.linalg.norm(bmax - bmin)) / 2
cam_data.clip_end = rad * 20

# views: (direction from the centre to the camera, camera up); in Blender axes.
# side = looking at the flat of a blade (edge -Z to the right), edge = looking at the
# cutting edge, 3/4, and down the length from above the tip.
VIEWS = [((-1, 0, 0), (0, 0, 1), "side (from -X)"), ((0, 1, 0), (0, 0, 1), "front (from -Z)"),
         ((-1, 1, 0.6), (0, 0, 1), "3/4"), ((0, 0.001, 1), (0, 1, 0), "top (from +Y)")]
tiles = []
for d, upv, name in VIEWS:
    d = Vector(d).normalized()
    cam.location = center + d * rad * 4
    cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    q = (-d).to_track_quat("-Z", "Y")
    # extent of the scene in this view, for the ortho scale
    rotm = q.to_matrix().transposed()
    pts = np.array([rotm @ Vector(p) for p in (allco - np.array(center))[:: max(1, len(allco) // 4000)]])
    cam_data.ortho_scale = float(max(np.ptp(pts[:, 0]), np.ptp(pts[:, 1]))) * 1.12
    for lb in labels:  # labels face the camera
        lb.rotation_euler = q.to_euler()
    path = job["thumb"] + f".{len(tiles)}.png"
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    img = bpy.data.images.load(path)
    px = np.array(img.pixels[:], dtype=np.float32).reshape(img.size[1], img.size[0], 4)
    tiles.append(px)
    bpy.data.images.remove(img)
    os.remove(path)
strip = np.concatenate(tiles, axis=1)
out = bpy.data.images.new("strip", width=strip.shape[1], height=strip.shape[0], alpha=True)
out.pixels[:] = strip.ravel()
out.filepath_raw = job["thumb"]
out.file_format = "PNG"
out.save()
report["thumb_views"] = [vname for _, _, vname in VIEWS]
report["seconds"] = round(time.time() - t_start, 1)
with open(job["report"], "w") as f:
    json.dump(report, f, indent=2)
print("PROP_QA_OK", json.dumps({k: report[k] for k in ("tris", "dims_gltf_m", "orientation_confidence")}))

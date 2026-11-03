"""Headless Blender QA pass for one monster GLB.

Run by tools/forge.py:  blender -b --factory-startup -P tools/blender_qa.py -- '<job json>'

Imports model_raw.glb, joins meshes, orients it (faces -Z in glTF / +Y in Blender by
default), levels the stance (pitch/roll so the feet touch the ground; docs/lods.md),
scales it to scale_m (height for upright plans, horizontal length otherwise), puts the
origin at the feet, exports model.glb, renders a 4-view contact strip, a head close-up
and a ground-level contact view (thumb_ground.png), and writes a JSON report (tri count,
bounds, non-manifold edges, orientation guess, leveling).

Job keys beyond the paths: body_plan, scale_m, scale_axis, front_view, yaw_override,
forward, level (bool), level_tilt (deg), level_feet, level_min_area, id + source (then
data/overrides/<source>/<id>.json fills yaw/forward/level/level_tilt left unset).
"""
import itertools
import json
import math
import os
import sys

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector

job = json.loads(sys.argv[sys.argv.index("--") + 1])
report = {}
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import overrides  # noqa: E402  (tools/overrides.py, stdlib only)

# data/overrides/<source>/<id>.json fills whatever the job leaves unset (job keys = CLI
# flags win over the file, the file wins over the defaults below).
if job.get("id") and job.get("source"):
    _ov = overrides.load(job["source"], job["id"])
    for jk, ok in (("yaw_override", "yaw"), ("forward", "forward"), ("level", "level"),
                   ("level_tilt", "level_tilt")):
        if job.get(jk) is None and _ov.get(ok) is not None:
            job[jk] = _ov[ok]
    report["overrides"] = _ov

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=job["src"])
meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
if not meshes:
    raise SystemExit("no mesh in GLB")

# Bake every transform into the mesh data and parent nothing.
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
obj.name = "monster"
me = obj.data


def verts_np():
    co = np.empty(len(me.vertices) * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)
    return co.reshape(-1, 3)


def transform(m):
    me.transform(m)
    me.update()


v = verts_np()
report["raw_dims"] = [round(float(x), 4) for x in (v.max(0) - v.min(0))]

# ---- orientation. Blender: +Z up; generator front (toward the camera) is -Y after the
# glTF import. Target: model faces +Y in Blender (= -Z in glTF), or -Y for "+Z".
yaw = job.get("yaw_override")
yaw = float(yaw) if yaw is not None else None
if yaw is None:
    if job["front_view"]:
        yaw = 180.0  # faces the camera (-Y) -> turn to +Y
        report["facing_method"] = "front_view"
    else:
        # Side / three-quarter view: the body's long axis is the principal axis of the
        # footprint (area-weighted face centroids). The head end carries more surface in
        # the upper 40% of the body (head and neck vs a thin, low tail). If the two ends
        # are within 1.25x of each other (spiders, blobs), assume it faces the camera.
        tri_area = np.array([p.area for p in me.polygons])
        cent = np.array([p.center[:] for p in me.polygons])
        xy = cent[:, :2] - np.average(cent[:, :2], axis=0, weights=tri_area)
        w, vecs = np.linalg.eigh(np.cov(xy.T, aweights=tri_area))
        axis = vecs[:, np.argmax(w)]
        t = xy @ axis
        lo, hi = t.min(), t.max()
        z = cent[:, 2]
        high = z > z.min() + 0.6 * (z.max() - z.min())
        a_neg = float(tri_area[(t < lo + 0.2 * (hi - lo)) & high].sum())
        a_pos = float(tri_area[(t > hi - 0.2 * (hi - lo)) & high].sum())
        report["facing_end_high_area"] = [round(a_neg, 4), round(a_pos, 4)]
        report["footprint_axis_ratio"] = round(float(math.sqrt(w.max() / max(w.min(), 1e-12))), 2)
        if max(a_neg, a_pos) < 1.25 * min(a_neg, a_pos):
            yaw = 180.0
            report["facing_method"] = "ambiguous_ends_assume_camera"
        else:
            head = axis if a_pos > a_neg else -axis
            yaw = math.degrees(math.atan2(head[0], head[1]))  # rotates head onto +Y
            report["facing_method"] = "pca_upper_mass_end"
if job.get("forward") == "+Z":
    yaw += 180.0
report["yaw_deg"] = yaw
transform(Matrix.Rotation(math.radians(yaw), 4, "Z"))

# ---- stance leveling (pitch/roll only). The generator reconstructs the model in the
# reference camera's frame, so a reference drawn from above (spiders, most "front view"
# quadrupeds) comes out pitched and its back legs float. Find the contact clusters
# (local height minima in the lower half, one per foot), pick the ground plane that puts
# the k lowest of them (k from the body plan) closest to the ground, least-squares refit
# it through those k feet, and rotate that plane to horizontal. Only the tilt that is
# under the cap is applied; the model is re-dropped to y=0 below.
# Feet per plan (plans not listed are not leveled by default) and the tilt cap: top-down
# spider references come out pitched 30-35 deg, so insectoids get 40. See tools/overrides.py.
FEET_BY_PLAN = overrides.FEET_BY_PLAN
TILT_CAP_BY_PLAN = overrides.TILT_CAP_BY_PLAN
DEFAULT_TILT_CAP = overrides.DEFAULT_TILT_CAP


def contact_clusters(v, band=0.5, rfrac=0.035):
    """Local height minima (no lower vertex within r horizontally) in the lower `band` of
    the height, merged within r (r = rfrac x footprint diagonal). Each cluster records its
    lowest point and its contact patch (vertices within r and 3% of the height above it)."""
    z0 = float(v[:, 2].min())
    H = float(np.ptp(v[:, 2]))
    r = rfrac * float(np.hypot(*np.ptp(v[:, :2], 0)))
    p = v[v[:, 2] < z0 + band * H]
    keys = np.floor(p[:, :2] / r).astype(np.int64)
    grid = {}
    for i, k in enumerate(map(tuple, keys)):
        grid.setdefault(k, []).append(i)
    grid = {k: np.array(ix) for k, ix in grid.items()}
    ismin = np.zeros(len(p), bool)
    for k, ix in grid.items():
        nb = np.concatenate([grid[q] for q in ((k[0] + a, k[1] + b) for a in (-1, 0, 1) for b in (-1, 0, 1))
                             if q in grid])
        q = p[nb]
        for i in ix:
            d = np.hypot(q[:, 0] - p[i, 0], q[:, 1] - p[i, 1])
            ismin[i] = not np.any((d < r) & (q[:, 2] < p[i, 2] - 1e-9))
    m = p[ismin]
    tips = []
    for i in np.argsort(m[:, 2]):
        if all(math.hypot(*(m[i, :2] - t[:2])) > r for t in tips):
            tips.append(m[i])
    out = []
    for t in tips:
        d = np.hypot(v[:, 0] - t[0], v[:, 1] - t[1])
        patch = v[(d < r) & (v[:, 2] < t[2] + 0.03 * H)]
        out.append({"point": t, "verts": int(len(patch)),
                    "area": float(np.prod(np.ptp(patch[:, :2], 0) + 1e-3 * H))})
    return out, H, r


def tilt_deg(n):
    return math.degrees(math.acos(min(1.0, abs(float(n[2])) / float(np.linalg.norm(n)))))


TAILED_PLANS = {"quadruped", "winged_quadruped"}


def limb_sets(v, edges, points, H, foot_r, rise=0.12):
    """For each cluster point: the set of welded vertex ids reachable from it through mesh
    edges while staying under point.z + rise x H and within foot_r horizontally. Two
    clusters on one paw (toes, claws, feather tips of the same foot) reach each other;
    clusters on different legs do not (the legs join only higher up, at the body)."""
    key = np.round(v / (1e-5 * H)).astype(np.int64)
    _, weld = np.unique(key, axis=0, return_inverse=True)
    weld = weld.ravel()
    nw = int(weld.max()) + 1
    wco = np.zeros((nw, 3))
    wco[weld] = v
    adj = [[] for _ in range(nw)]
    for a_, b_ in weld[edges].tolist():
        if a_ != b_:
            adj[a_].append(b_)
            adj[b_].append(a_)
    tree = {}
    out = []
    for pt in points:
        start = int(np.argmin(np.linalg.norm(wco - pt, axis=1)))
        zmax = pt[2] + rise * H
        seen = {start}
        stack = [start]
        while stack:
            i = stack.pop()
            for j in adj[i]:
                if j not in seen and wco[j, 2] < zmax and math.hypot(wco[j, 0] - pt[0], wco[j, 1] - pt[1]) < foot_r:
                    seen.add(j)
                    stack.append(j)
        out.append(seen)
    return out, weld, wco


def level_plane(v, k_feet, cap_deg, min_area=0.1, max_feet=20, tilt_cost=0.02, limb=2.5,
                fwd=None, edges=None):
    """J(n) = mean gap (in heights) of the k clusters nearest plane n once the model is
    re-dropped onto it, + tilt_cost x tilt (rad). Candidates: planes through every triple
    of kept clusters (tilt <= cap), then least-squares refits through the k nearest feet.
    Single-vertex clusters are never feet. For two-legged plans clusters whose contact
    patch is under min_area x the largest one are not feet either (wing and feather tips);
    multi-legged plans keep every cluster (spider leg tips are tiny next to the belly). The k feet must be distinct limbs: clusters within
    limb x r of an already picked one (toes of the same foot) do not count again. With
    `fwd` (unit forward vector, tailed plans), the rear-most cluster is a tail lying on the
    ground, not a foot, when it sits more than 15% of the length behind every other one."""
    clusters, H, r = contact_clusters(v)
    up = np.array([0.0, 0.0, 1.0])
    if not clusters:
        return {"normal": up, "method": "no_clusters", "clusters": [], "feet_idx": [], "H": H, "radius": r}
    amax = max(c["area"] for c in clusters)
    for c in clusters:
        c["kept"] = c["area"] >= min_area * amax and c["verts"] >= 2
    if fwd is not None:
        kept = [i for i, c in enumerate(clusters) if c["kept"]]
        if len(kept) >= 4:
            t = np.array([float(clusters[i]["point"][:2] @ fwd[:2]) for i in kept])
            o = np.argsort(t)
            L = float(np.ptp(v[:, :2] @ fwd[:2]))
            if t[o[1]] - t[o[0]] > 0.15 * L:
                clusters[kept[o[0]]]["kept"] = False
                clusters[kept[o[0]]]["tail"] = True
    feet_idx = [i for i, c in enumerate(clusters) if c["kept"]][:max_feet]
    P = np.array([clusters[i]["point"] for i in feet_idx])
    k = max(2, min(k_feet, len(P)))

    # Same limb: connected under the cluster within 12% of the height (and 0.3 x the
    # footprint diagonal), or, without mesh edges, closer than limb x r.
    same = np.zeros((len(P), len(P)), bool)
    if edges is not None and len(P):
        foot_r = 0.3 * float(np.hypot(*np.ptp(v[:, :2], 0)))
        sets, weld, wco = limb_sets(v, edges, P, H, foot_r)
        ids = [int(np.argmin(np.linalg.norm(wco - p_, axis=1))) for p_ in P]
        for i in range(len(P)):
            for j in range(len(P)):
                same[i, j] = ids[j] in sets[i] or ids[i] in sets[j]
    else:
        for i in range(len(P)):
            for j in range(len(P)):
                same[i, j] = math.hypot(*(P[i, :2] - P[j, :2])) <= limb * r

    def pick(h):
        """indices of the k lowest clusters that belong to distinct limbs"""
        sel = []
        for i in np.argsort(h):
            if not any(same[i, j] for j in sel):
                sel.append(int(i))
            if len(sel) == k:
                break
        return np.array(sel)

    def J(n):
        h = (P @ n - float((v @ n).min())) / H
        return float(h[pick(h)].mean()) + tilt_cost * math.radians(tilt_deg(n)), h

    j0, _ = J(up)
    best = (j0, up)
    if len(P) >= 3 and k >= 3:
        method = "plane_fit"
        for i, j, l in itertools.combinations(range(len(P)), 3):
            n = np.cross(P[j] - P[i], P[l] - P[i])
            L = float(np.linalg.norm(n))
            if L < 1e-12:
                continue
            n = n / L if n[2] > 0 else -n / L
            if tilt_deg(n) > cap_deg:
                continue
            jj, _ = J(n)
            if jj < best[0]:
                best = (jj, n)
        n = best[1]
        for _ in range(5):
            _, h = J(n)
            Q = P[pick(h)]
            a, b, _c = np.linalg.lstsq(np.c_[Q[:, 0], Q[:, 1], np.ones(len(Q))], Q[:, 2], rcond=None)[0]
            n2 = np.array([-a, -b, 1.0])
            n2 /= np.linalg.norm(n2)
            if tilt_deg(n2) > cap_deg or J(n2)[0] >= best[0] - 1e-9:
                break
            best = (J(n2)[0], n2)
            n = n2
    else:
        # fewer than 3 feet (or a two-legged plan): pitch only, through the two lowest
        # clusters, and only when they lie front/back of each other.
        method = "two_lowest_pitch"
        if len(P) >= 2:
            a_, b_ = P[np.argsort(P[:, 2])[:2]]
            dy = b_[1] - a_[1]
            if abs(dy) > 0.7 * math.hypot(b_[0] - a_[0], dy) and abs(dy) > 1e-6:
                n = np.array([0.0, -(b_[2] - a_[2]) / dy, 1.0])
                n /= np.linalg.norm(n)
                if tilt_deg(n) <= cap_deg and J(n)[0] < j0:
                    best = (J(n)[0], n)
    _, h = J(best[1])
    contact = [feet_idx[i] for i in pick(h)] if len(P) else []
    return {"normal": best[1], "method": method, "J": best[0], "J0": j0, "k": k, "contact": contact,
            "clusters": clusters, "feet_idx": feet_idx, "H": H, "radius": r}


def rotation_to_up(n):
    """Minimal rotation taking n to +Z: the axis is horizontal, so it is pitch + roll, no yaw."""
    n = np.asarray(n, float) / np.linalg.norm(n)
    ax = np.cross(n, (0.0, 0.0, 1.0))
    s = float(np.linalg.norm(ax))
    if s < 1e-12:
        return Matrix.Identity(4)
    return Matrix.Rotation(math.atan2(s, float(n[2])), 4, Vector((ax / s).tolist()))


plan = job.get("body_plan", "")
k_feet = int(job.get("level_feet") or FEET_BY_PLAN.get(plan, 0) or 3)
level_on = job.get("level")
if level_on is None:
    level_on = plan in FEET_BY_PLAN
cap = job.get("level_tilt")
cap = float(cap if cap is not None else TILT_CAP_BY_PLAN.get(plan, DEFAULT_TILT_CAP))
_fwd = np.array((0.0, 1.0 if job.get("forward", "-Z") == "-Z" else -1.0, 0.0))
_edges = np.empty(len(me.edges) * 2, dtype=np.int64)
me.edges.foreach_get("vertices", _edges)
lv = level_plane(verts_np(), k_feet, cap, fwd=_fwd if plan in TAILED_PLANS else None, edges=_edges.reshape(-1, 2),
                 min_area=float(job.get("level_min_area", 0.1 if k_feet <= 2 else 0.0)))
R_level = rotation_to_up(lv["normal"]) if level_on else Matrix.Identity(4)
report["level"] = {"enabled": bool(level_on), "method": lv["method"] if level_on else "disabled",
                   "tilt_cap_deg": cap, "k_feet": k_feet,
                   "best_tilt_deg": round(tilt_deg(lv["normal"]), 2),
                   "tilt_deg": round(tilt_deg(lv["normal"]), 2) if level_on else 0.0,
                   "normal_before": [round(float(x), 4) for x in lv["normal"]]}
_feet_pts = np.array([c["point"] for c in lv["clusters"]]) if lv["clusters"] else np.zeros((0, 3))
_v_pre = verts_np()
transform(R_level)

# ---- scale and origin at the feet
v = verts_np()
dims = v.max(0) - v.min(0)
if job["scale_axis"] == "height":
    ref = dims[2]
else:
    ref = dims[1]  # forward axis after orientation = nose-to-tail length
s = job["scale_m"] / max(ref, 1e-6)
transform(Matrix.Scale(s, 4))
v = verts_np()
mn, mx = v.min(0), v.max(0)
_T = np.array((-(mn[0] + mx[0]) / 2, -(mn[1] + mx[1]) / 2, -mn[2]))
transform(Matrix.Translation(Vector(_T.tolist())))
v = verts_np()
mn, mx = v.min(0), v.max(0)
report["scale_factor"] = round(float(s), 5)


def _gaps(points, rot):
    """Height of each cluster's lowest point above the model's lowest point, in metres,
    after rotation `rot` (3x3) and the uniform scale."""
    if not len(points):
        return []
    R3 = np.array(rot.to_3x3())
    zs = (_v_pre @ R3.T)[:, 2]
    return [round(float(((R3 @ p)[2] - zs.min()) * s), 4) for p in points]


_g_before = _gaps(_feet_pts, Matrix.Identity(3))
_g_after = _gaps(_feet_pts, R_level)
_kept = set(lv["feet_idx"])
_contact = set(lv.get("contact", []))
report["level"]["clusters"] = [
    {"point_m": [round(float(x), 3) for x in (np.array(R_level.to_3x3()) @ c["point"]) * s + _T],
     "verts": c["verts"], "area_m2": round(c["area"] * s * s, 5), "foot": i in _kept,
     "tail": bool(c.get("tail")),
     "contact": i in _contact, "gap_before_m": _g_before[i], "gap_after_m": _g_after[i]}
    for i, c in enumerate(lv["clusters"])][:24]
report["level"]["foot_gaps_m"] = [_g_after[i] for i in sorted(_contact, key=lambda i: _g_after[i])]
report["level"]["foot_gaps_before_m"] = sorted(_g_before[i] for i in _contact)
report["level"]["cluster_radius_m"] = round(lv["radius"] * s, 4)
_contact_pts = [np.array(c["point_m"]) for c in report["level"]["clusters"] if c["contact"]]


def _hull(xy):
    """2D convex hull (monotone chain), counter-clockwise."""
    pts = sorted(set(map(tuple, np.round(xy, 5))))
    if len(pts) < 3:
        return pts
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, hi = [], []
    for q in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], q) <= 0:
            lo.pop()
        lo.append(q)
    for q in reversed(pts):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], q) <= 0:
            hi.pop()
        hi.append(q)
    return lo[:-1] + hi[:-1]


# Stability check (report only): is the centre of mass (area-weighted surface centroid)
# over the support polygon (hull of the vertices within 1% of the height of the ground)?
# A pose that fails would tip over, e.g. a spider standing on its front legs with the
# abdomen and rear legs raised.
_area = np.empty(len(me.polygons))
me.polygons.foreach_get("area", _area)
_cen = np.empty(len(me.polygons) * 3)
me.polygons.foreach_get("center", _cen)
_com = (_cen.reshape(-1, 3) * _area[:, None]).sum(0) / max(_area.sum(), 1e-12)
_sup = _hull(v[v[:, 2] < mn[2] + 0.01 * (mx[2] - mn[2])][:, :2])
_inside = len(_sup) >= 3 and all(
    (b[0] - a[0]) * (_com[1] - a[1]) - (b[1] - a[1]) * (_com[0] - a[0]) >= 0
    for a, b in zip(_sup, _sup[1:] + _sup[:1]))
report["level"]["com_m"] = [round(float(x), 3) for x in _com]
report["level"]["stable"] = bool(_inside)
report["dims_m"] = [round(float(x), 3) for x in (mx - mn)]  # Blender X, Y(forward), Z(up)
report["bounds_min"] = [round(float(x), 3) for x in mn]
report["bounds_max"] = [round(float(x), 3) for x in mx]

# ---- topology stats. glTF splits vertices along UV seams, so weld a copy first.
report["verts"] = len(me.vertices)
report["tris"] = sum(len(p.vertices) - 2 for p in me.polygons)
bm = bmesh.new()
bm.from_mesh(me)
bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5 * float(np.linalg.norm(mx - mn)))
report["welded_verts"] = len(bm.verts)
report["non_manifold_edges"] = sum(1 for e in bm.edges if not e.is_manifold)
report["boundary_edges"] = sum(1 for e in bm.edges if e.is_boundary)
seen, parts = set(), 0
for vert in bm.verts:
    if vert.index in seen:
        continue
    parts += 1
    stack = [vert]
    seen.add(vert.index)
    while stack:
        cur = stack.pop()
        for e in cur.link_edges:
            o = e.other_vert(cur)
            if o.index not in seen:
                seen.add(o.index)
                stack.append(o)
report["loose_parts"] = parts
bm.free()
report["materials"] = [m.name for m in me.materials if m]
imgs = [img for img in bpy.data.images if img.size[0] > 0]
report["textures"] = [{"name": i.name, "size": list(i.size)} for i in imgs]
report["has_uv"] = len(me.uv_layers) > 0

# ---- export
bpy.ops.object.select_all(action="DESELECT")
obj.select_set(True)
bpy.ops.export_scene.gltf(filepath=job["dst"], export_format="GLB", use_selection=True,
                          export_yup=True, export_apply=True)

# ---- turntable contact strip (front, right, back, left). EEVEE shows the baked
# normal/MR maps; fall back to Workbench (base color only) if EEVEE cannot start.
scene = bpy.context.scene
world = bpy.data.worlds.new("bg")
scene.world = world
world.use_nodes = True
bg = world.node_tree.nodes["Background"]
bg.inputs[0].default_value = (0.8, 0.8, 0.8, 1.0)
bg.inputs[1].default_value = 1.0
sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun", "SUN"))
sun.data.energy = 3.0
sun.rotation_euler = (math.radians(45), 0, math.radians(30))
scene.collection.objects.link(sun)
scene.view_settings.view_transform = "Standard"
scene.render.engine = job.get("engine", "BLENDER_EEVEE")
scene.render.film_transparent = False
if scene.render.engine == "BLENDER_WORKBENCH":
    shading = scene.display.shading
    shading.light = "STUDIO"
    shading.color_type = "TEXTURE"
    shading.background_type = "VIEWPORT"
    shading.background_color = (0.8, 0.8, 0.8)
else:
    scene.eevee.taa_render_samples = 16
report["thumb_engine"] = scene.render.engine
scene.render.resolution_x = 512
scene.render.resolution_y = 512
scene.render.image_settings.file_format = "PNG"

cam_data = bpy.data.cameras.new("cam")
cam_data.type = "ORTHO"
cam = bpy.data.objects.new("cam", cam_data)
scene.collection.objects.link(cam)
scene.camera = cam
center = Vector(((mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, (mn[2] + mx[2]) / 2))
radius = float(np.linalg.norm(mx - mn)) / 2
cam_data.ortho_scale = max(mx[2] - mn[2], math.hypot(mx[0] - mn[0], mx[1] - mn[1])) * 1.08
cam_data.clip_end = radius * 10
dist = radius * 3
tiles = []
# model faces +Y (or -Y for +Z export); the "front" camera sits on the facing side.
front = Vector((0, 1, 0)) if job.get("forward", "-Z") == "-Z" else Vector((0, -1, 0))
for i, ang in enumerate((0, 90, 180, 270)):
    d = Matrix.Rotation(math.radians(ang), 3, "Z") @ front
    d = Vector((d.x, d.y, 0.25)).normalized()
    cam.location = center + d * dist
    cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    path = job["thumb"] + f".{i}.png"
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    img = bpy.data.images.load(path)
    px = np.array(img.pixels[:], dtype=np.float32).reshape(img.size[1], img.size[0], 4)
    tiles.append(px)
    bpy.data.images.remove(img)
    os.remove(path)



def save_strip(tiles, path):
    strip = np.concatenate(tiles, axis=1)
    out = bpy.data.images.new("strip", width=strip.shape[1], height=strip.shape[0], alpha=True)
    out.pixels[:] = strip.ravel()
    out.filepath_raw = path
    out.file_format = "PNG"
    out.save()
    bpy.data.images.remove(out)


def render_tile(path):
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    img = bpy.data.images.load(path)
    px = np.array(img.pixels[:], dtype=np.float32).reshape(img.size[1], img.size[0], 4)
    bpy.data.images.remove(img)
    os.remove(path)
    return px


save_strip(tiles, job["thumb"])

# ---- head close-up (thumb_head.png): shaded + wireframe overlay, so face density and
# texture sharpness are visible per monster. The head region is a heuristic per plan.
HEAD_END = {"quadruped", "winged_quadruped", "insectoid", "avian", "serpentine", "aquatic"}


def head_region():
    """Locate the face: the front-most surface (nose / snout) in the upper body.

    Upright plans: within the top 30% of the height, near the body column. Plans with a
    head end: the bounding box of the front 30% along the facing axis, above 30% of the
    height (returned directly). Others: the
    top 30%. The close-up window is a cube centred just behind that point; its side is
    0.24-0.45 x height for upright plans (1.25 x the head width) and 0.3 x max(height, length) otherwise. Returns the
    window centre, side, view direction, rule name and the tri count inside it.
    """
    nt = len(me.polygons)  # all triangles (DecimateMesh / Decimate output)
    cen = np.empty(nt * 3)
    me.polygons.foreach_get("center", cen)
    cen = cen.reshape(-1, 3)
    area = np.empty(nt)
    me.polygons.foreach_get("area", area)
    H = mx[2] - mn[2]
    L = float(np.dot(mx - mn, np.abs(np.array(front[:]))))
    plan = job.get("body_plan", "")
    fwd = np.array(front[:])
    t = cen @ fwd
    if plan in HEAD_END:
        # Snouts, beaks and spider fronts vary too much for a nose rule: frame the bounding
        # box of the front 30% of the length above the feet, seen from 35 degrees off-axis.
        sel = (t > t.max() - 0.3 * (t.max() - t.min())) & (cen[:, 2] > mn[2] + 0.3 * H)
        if not sel.any():
            sel = t > t.max() - 0.3 * (t.max() - t.min())
        lo, hi = cen[sel].min(0), cen[sel].max(0)
        center = (lo + hi) / 2
        side_ax = np.cross(fwd, (0.0, 0.0, 1.0))
        ext = max(float(hi[2] - lo[2]), float(np.ptp(cen[sel] @ side_ax)), float(np.ptp(t[sel])))
        win = min(1.1 * ext, 0.6 * max(H, L))
        view = np.array((Matrix.Rotation(math.radians(35), 3, "Z") @ Vector(fwd))[:])
        inside = np.all(np.abs(cen - center) < win / 2, axis=1)
        return center, win, view, "front_end_box", int(inside.sum())
    else:
        band = cen[:, 2] > mn[2] + 0.7 * H
        c = np.average(cen[band, :2], axis=0, weights=area[band])
        sel = band & (np.hypot(cen[:, 0] - c[0], cen[:, 1] - c[1]) < 0.15 * H)
        view, win, rule = fwd, 0.24 * H, "upright_front_top30"
    if not sel.any():
        sel = np.ones(nt, bool)
    # front-most 1% of the candidate area, area-weighted -> nose / snout
    ts = t[sel]
    tip_sel = ts >= np.percentile(ts, 99)
    tip = np.average(cen[sel][tip_sel], axis=0, weights=area[sel][tip_sel])
    center = tip - fwd * (0.4 * win)
    if rule.startswith("upright"):
        # widen the window for big heads (goblins): head width at nose height, capped
        side = np.cross(fwd, (0.0, 0.0, 1.0))
        sl = band & (np.abs(cen[:, 2] - tip[2]) < 0.03 * H) & \
            (np.hypot(cen[:, 0] - c[0], cen[:, 1] - c[1]) < 0.25 * H)
        if sl.any():
            # contiguous run of the slice around the nose (stops at gaps: arms, wings)
            u = np.sort(cen[sl] @ side)
            u0 = float(tip @ side)
            gap = 0.02 * H
            k = int(np.clip(np.searchsorted(u, u0), 0, len(u) - 1))
            lo_i = hi_i = k
            while lo_i > 0 and u[lo_i] - u[lo_i - 1] < gap:
                lo_i -= 1
            while hi_i < len(u) - 1 and u[hi_i + 1] - u[hi_i] < gap:
                hi_i += 1
            win = float(np.clip(1.25 * (u[hi_i] - u[lo_i]), 0.24 * H, 0.45 * H))
            center = tip - fwd * (0.4 * win)
        center[2] = tip[2] + 0.03 * H  # eyes sit above the nose
    inside = np.all(np.abs(cen - center) < win / 2, axis=1)
    return center, win, view, rule, int(inside.sum())


hc, win, hview, hrule, head_tris = head_region()
if job.get("head_box"):  # fixed framing, for side-by-side comparisons
    hc, win = np.array(job["head_box"]["center"]), float(job["head_box"]["side"])
report["head_region"] = {"rule": hrule, "center": [round(float(x), 3) for x in hc],
                         "side": round(float(win), 3), "tris": head_tris}
cam_data.ortho_scale = float(win)
d = Vector((hview[0], hview[1], 0.12)).normalized()
cam.location = Vector(hc.tolist()) + d * dist
cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
scene.eevee.taa_render_samples = 32
head_tiles = [render_tile(job["thumb"] + ".head0.png")]
wire = obj.modifiers.new("wire", "WIREFRAME")
wire.thickness = float(win) * 0.0025
wire.use_replace = False
wire.use_even_offset = False
wmat = bpy.data.materials.new("wire")
wmat.use_nodes = True
wmat.node_tree.nodes["Principled BSDF"].inputs[0].default_value = (0.0, 0.0, 0.0, 1.0)
me.materials.append(wmat)
wire.material_offset = len(me.materials) - 1
head_tiles.append(render_tile(job["thumb"] + ".head1.png"))
save_strip(head_tiles, job.get("thumb_head", os.path.join(os.path.dirname(job["thumb"]), "thumb_head.png")))

# ---- ground-contact view (thumb_ground.png): side and front, orthographic, camera at
# ground level, with a dark ground slab and red markers on the feet the leveling used, so
# floating feet show up as a gap between the foot and the slab.
obj.modifiers.remove(wire)
H_m = float(mx[2] - mn[2])
slab_me = bpy.data.meshes.new("ground")
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=1.0)
bm.to_mesh(slab_me)
bm.free()
slab = bpy.data.objects.new("ground", slab_me)
span = float(max(mx - mn)) * 3
slab.scale = (span, span, 0.01 * H_m)
slab.location = (0, 0, -0.005 * H_m)
gmat = bpy.data.materials.new("ground")
gmat.use_nodes = True
gmat.node_tree.nodes["Principled BSDF"].inputs[0].default_value = (0.08, 0.08, 0.09, 1.0)
slab_me.materials.append(gmat)
scene.collection.objects.link(slab)
mmat = bpy.data.materials.new("marker")
mmat.use_nodes = True
mmat.node_tree.nodes["Principled BSDF"].inputs[0].default_value = (1.0, 0.05, 0.02, 1.0)
markers = []
for pnt in _contact_pts:
    mme = bpy.data.meshes.new("marker")
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=12, v_segments=8, radius=0.012 * float(np.linalg.norm(mx - mn)))
    bm.to_mesh(mme)
    bm.free()
    mme.materials.append(mmat)
    mo = bpy.data.objects.new("marker", mme)
    mo.location = Vector(pnt.tolist())
    scene.collection.objects.link(mo)
    markers.append(mo)
ext = float(max(mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2]))
cam_data.ortho_scale = ext * 1.08
cam_data.shift_y = 0.0
side_ax = Vector((1, 0, 0))
ground_tiles = []
for d in (side_ax, front):
    # the frame's bottom edge sits just under the ground; camera exactly level
    cam.location = Vector((0, 0, 0)) + d * dist + Vector((0, 0, ext * 1.08 / 2 - 0.03 * ext))
    cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    ground_tiles.append(render_tile(job["thumb"] + f".ground{len(ground_tiles)}.png"))
save_strip(ground_tiles, job.get("thumb_ground", os.path.join(os.path.dirname(job["thumb"]), "thumb_ground.png")))

with open(job["report"], "w") as f:
    json.dump(report, f, indent=2)
print("QA_OK", json.dumps(report))

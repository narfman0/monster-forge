"""Importance-weighted decimation of a remeshed high-poly GLB, in headless Blender.

Run by tools/forge.py:  blender -b --factory-startup -P tools/blender_decimate.py -- '<job json>'

The GPU DecimateMesh node in ComfyUI is uniform: it spends the triangle budget where the
quadric error is largest, so a face (small, low-relief, but what the player looks at)
ends up with ~15 triangles. This script builds a per-vertex importance map and feeds it
to Blender's Decimate (Collapse) modifier as a vertex group, so low-importance regions
collapse first and heads, hands, claws and other high-curvature features keep density.

importance = clip(w_curv * curvature + w_head * head_prior + w_ext * extremity_prior, 0, 1)

- curvature: mean dihedral angle of the edges around each vertex, smoothed over a few
  rings (removes the dual-contouring staircase) and normalized between the median and the
  97th percentile.
- head prior: a sphere around the face, found like the QA head close-up: the front-most
  surface (nose / snout) of the head region, which depends on the body plan (registry).
  Upright plans: top 30% of the height near the body column, front = the camera side
  (front-view references). Quadruped-like plans: the head end of the footprint's long
  axis (the end with more surface high up, as in the QA facing heuristic), above the
  feet. Avian / serpentine / aquatic: the head end only. Other plans: none.
- extremity prior: thin (inward ray-cast thickness) and far from the centroid: hands,
  claws, horns, tails tips.

Blender's modifier treats the group weight as "how much this vertex may be decimated"
(0 = locked), so the group gets 1 - 0.9 * importance; vertex_group_factor scales how
strongly that cost term competes with the quadric error.

Input and output are glTF, +Y up in the file; Blender's importer/exporter convert to and
from +Z up, so the output is in exactly the input's frame (required: ComfyUI bakes the
voxel texture by position). The output has no UVs, normals or materials.
"""
import json
import math
import sys
import time

import bmesh
import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

job = json.loads(sys.argv[sys.argv.index("--") + 1])
t_start = time.time()
report = {}

UPRIGHT = {"biped", "winged_biped", "centauroid", "merfolk_naga"}
QUADRUPED_LIKE = {"quadruped", "winged_quadruped", "insectoid"}
END_ONLY = {"avian", "serpentine", "aquatic"}

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=job["src"])
meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
for o in bpy.context.scene.objects:
    o.select_set(o.type == "MESH")
bpy.context.view_layer.objects.active = meshes[0]
if len(meshes) > 1:
    bpy.ops.object.join()
obj = bpy.context.view_layer.objects.active
bpy.ops.object.parent_clear(type="CLEAR_KEEP_TRANSFORM")
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
me = obj.data
report["faces_in"] = len(me.polygons)

# The UDF remesh leaves a few hundred "pinch" edges with 4 faces (two sheets touching
# inside one voxel). Blender's collapse never touches non-manifold edges, so they keep
# dense fans and eat the budget (F ~ 2.6 V). Un-pinch them: pair the 4 faces into two
# sheets by angle around the edge, and give each sheet its own copy of the vertices.
# The result is manifold (touching, not connected, surfaces at those spots).


def unpinch(mesh):
    nv0 = len(mesh.vertices)
    co = np.empty(nv0 * 3)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    mesh.calc_loop_triangles()
    tri = np.empty(len(mesh.loop_triangles) * 3, np.int64)
    mesh.loop_triangles.foreach_get("vertices", tri)
    tri = tri.reshape(-1, 3)
    edges = {}
    for f, (i, j, k) in enumerate(tri.tolist()):
        for a_, b_ in ((i, j), (j, k), (k, i)):
            edges.setdefault((min(a_, b_), max(a_, b_)), []).append(f)
    pinch = {e: fs for e, fs in edges.items() if len(fs) > 2}
    if not pinch:
        return 0
    partner = {}  # (edge, face) -> paired face across that edge
    for (a_, b_), fs in pinch.items():
        d = co[b_] - co[a_]
        d /= max(np.linalg.norm(d), 1e-20)
        ws = []
        for f in fs:
            c = [x for x in tri[f] if x != a_ and x != b_][0]
            w = co[c] - co[a_]
            ws.append(w - d * np.dot(w, d))
        u = ws[0]
        ang = np.array([math.atan2(np.dot(np.cross(u, w), d), np.dot(u, w)) for w in ws])
        srt = np.argsort(ang)
        order = [fs[i] for i in srt]
        # Pair faces that bound the same solid wedge (face normal points away from the
        # wedge), so touching volumes come apart; geometric, hence consistent along chains.
        i0, i1 = srt[0], srt[1]
        mid = ang[i0] + ((ang[i1] - ang[i0]) % (2 * math.pi)) / 2
        v_perp = np.cross(d, u / max(np.linalg.norm(u), 1e-20))
        m = math.cos(mid) * u / max(np.linalg.norm(u), 1e-20) + math.sin(mid) * v_perp
        t0 = tri[order[0]]
        n0 = np.cross(co[t0[1]] - co[t0[0]], co[t0[2]] - co[t0[0]])
        if np.dot(n0, m) >= 0:  # wedge 0-1 is empty: rotate the pairing by one
            order = order[1:] + order[:1]
        for i in range(0, len(order) - 1, 2):
            partner[((a_, b_), order[i])] = order[i + 1]
            partner[((a_, b_), order[i + 1])] = order[i]
    pv = sorted({x for e in pinch for x in e})
    vf = {}
    for f, t in enumerate(tri.tolist()):
        for x in t:
            vf.setdefault(x, []).append(f)
    new_co = []
    out = tri.copy()  # adjacency is read from the original `tri`, edits go to `out`
    for v in pv:
        fs = vf[v]
        parent = {f: f for f in fs}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for f in fs:
            for w in tri[f].tolist():
                if w == v:
                    continue
                e = (min(v, w), max(v, w))
                if e in pinch:
                    g = partner.get((e, f))
                    if g is not None:
                        parent[find(f)] = find(g)
                else:
                    for g in edges.get(e, []):
                        if g != f and g in parent:
                            parent[find(f)] = find(g)
        groups = {}
        for f in fs:
            groups.setdefault(find(f), []).append(f)
        for gi, gfs in enumerate(groups.values()):
            if gi == 0:
                continue
            nid = nv0 + len(new_co)
            new_co.append(co[v])
            for f in gfs:
                out[f][tri[f] == v] = nid
    allco = np.concatenate([co, np.array(new_co).reshape(-1, 3)]) if new_co else co
    mesh.clear_geometry()
    mesh.from_pydata(allco.tolist(), [], out.tolist())
    mesh.update()
    return len(pinch)


report["unpinched_edges"] = unpinch(me) if not job.get("no_unpinch") else 0
# Edges the pairing could not resolve (both sheets also meet elsewhere around a vertex):
# split them outright. That leaves tiny slits, but no fan the collapse cannot touch.
bm = bmesh.new()
bm.from_mesh(me)
rest = [e for e in bm.edges if len(e.link_faces) > 2]
report["split_edges"] = len(rest)
if rest:
    bmesh.ops.split_edges(bm, edges=rest)
    bm.to_mesh(me)
bm.free()


def topo(mesh):
    bm = bmesh.new()
    bm.from_mesh(mesh)
    r = {"V": len(bm.verts), "F": len(bm.faces),
         "multi": sum(1 for e in bm.edges if len(e.link_faces) > 2),
         "boundary": sum(1 for e in bm.edges if e.is_boundary),
         "degenerate": sum(1 for f in bm.faces if f.calc_area() <= 0.0)}
    bm.free()
    return r


if job.get("topo"):
    report["topo_hi"] = topo(me)

# ---------------------------------------------------------------- mesh arrays
nv = len(me.vertices)
V = np.empty(nv * 3, np.float64)
me.vertices.foreach_get("co", V)
V = V.reshape(-1, 3)
me.calc_loop_triangles()
F = np.empty(len(me.loop_triangles) * 3, np.int64)
me.loop_triangles.foreach_get("vertices", F)
F = F.reshape(-1, 3)
nf = len(F)

e0, e1 = V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]
fn = np.cross(e0, e1)
farea = np.linalg.norm(fn, axis=1) * 0.5
fn /= np.maximum(np.linalg.norm(fn, axis=1, keepdims=True), 1e-20)
vn = np.zeros_like(V)
for k in range(3):
    np.add.at(vn, F[:, k], fn * farea[:, None])
vn /= np.maximum(np.linalg.norm(vn, axis=1, keepdims=True), 1e-20)
varea = np.bincount(F.ravel(), weights=np.repeat(farea, 3), minlength=nv) / 3.0

# edges with their (up to) two faces
he = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
hf = np.tile(np.arange(nf), 3)
he.sort(axis=1)
key = he[:, 0] * nv + he[:, 1]
order = np.argsort(key, kind="stable")
key, he, hf = key[order], he[order], hf[order]
same = key[1:] == key[:-1]
pair_a, pair_b = np.nonzero(same)[0], np.nonzero(same)[0] + 1
E = he[np.concatenate([[True], ~same])]  # unique edges
dihedral = np.arccos(np.clip((fn[hf[pair_a]] * fn[hf[pair_b]]).sum(1), -1.0, 1.0))
ev = he[pair_a]  # vertex pairs for the manifold edges


def vsum(vals_per_edge, pairs):
    out = np.bincount(pairs[:, 0], weights=vals_per_edge, minlength=nv)
    out += np.bincount(pairs[:, 1], weights=vals_per_edge, minlength=nv)
    return out


deg = vsum(np.ones(len(E)), E)


def smooth(x, iters):
    """Uniform Laplacian smoothing over the 1-ring, `iters` times."""
    for _ in range(iters):
        nb = np.bincount(E[:, 0], weights=x[E[:, 1]], minlength=nv)
        nb += np.bincount(E[:, 1], weights=x[E[:, 0]], minlength=nv)
        x = 0.5 * x + 0.5 * nb / np.maximum(deg, 1)
    return x


def smoothstep(x, a, b):
    t = np.clip((x - a) / (b - a), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def norm_pct(x, lo=50, hi=97):
    a, b = np.percentile(x, lo), np.percentile(x, hi)
    return np.clip((x - a) / max(b - a, 1e-12), 0.0, 1.0)


# ---------------------------------------------------------------- curvature
cnt = vsum(np.ones(len(ev)), ev)
curv_raw = vsum(dihedral, ev) / np.maximum(cnt, 1)
curv = norm_pct(smooth(curv_raw, job.get("curv_smooth", 6)))

# ---------------------------------------------------------------- head prior
# Locate the face the same way the QA head close-up does: the front-most surface (nose or
# snout) in the upper body, then a sphere just behind it. A plain "top 20% of the
# height" band fails on horned or winged monsters (the harpy's horns take the band).
plan = job.get("body_plan", "")
mn, mx = V.min(0), V.max(0)
H = mx[2] - mn[2]
h = (V[:, 2] - mn[2]) / max(H, 1e-9)
head = np.zeros(nv)
fwd = None
if plan in UPRIGHT:
    fwd = np.array([0.0, -1.0, 0.0])  # front-view references: the model faces the camera (-Y)
    band = h > 0.7
    c = np.average(V[band, :2], axis=0, weights=varea[band])
    cand = band & (np.hypot(V[:, 0] - c[0], V[:, 1] - c[1]) < 0.15 * H)
    win = 0.24 * H
    report["head_rule"] = "upright_front_top30"
elif plan in QUADRUPED_LIKE | END_ONLY:
    xy = V[:, :2] - np.average(V[:, :2], axis=0, weights=varea)
    wv, vecs = np.linalg.eigh(np.cov(xy.T, aweights=varea))
    axis = vecs[:, np.argmax(wv)]
    t = xy @ axis
    lo, hi = t.min(), t.max()
    high = h > 0.6
    a_neg = float(varea[(t < lo + 0.2 * (hi - lo)) & high].sum())
    a_pos = float(varea[(t > hi - 0.2 * (hi - lo)) & high].sum())
    if max(a_neg, a_pos) >= 1.25 * min(a_neg, a_pos):
        sgn = 1.0 if a_pos > a_neg else -1.0
        fwd = np.array([axis[0] * sgn, axis[1] * sgn, 0.0])
        tt = t * sgn
        cand = tt > tt.max() - 0.25 * (hi - lo)
        if plan not in END_ONLY:
            cand &= h > 0.3
        win = 0.22 * max(H, hi - lo)
        report["head_rule"] = "pca_head_end"
    else:
        report["head_rule"] = "ambiguous_none"
else:
    report["head_rule"] = "none"
if fwd is not None and cand.any():
    tproj = V[cand] @ fwd
    tip_sel = tproj >= np.percentile(tproj, 99)
    tip = np.average(V[cand][tip_sel], axis=0, weights=varea[cand][tip_sel])
    if plan in UPRIGHT:
        # big heads (goblins): 1.25 x the contiguous head width at nose height
        side = np.cross(fwd, (0.0, 0.0, 1.0))
        sl = cand & (np.abs(V[:, 2] - tip[2]) < 0.03 * H)
        if sl.any():
            u = np.sort(V[sl] @ side)
            k = int(np.clip(np.searchsorted(u, tip @ side), 0, len(u) - 1))
            lo_i = hi_i = k
            while lo_i > 0 and u[lo_i] - u[lo_i - 1] < 0.02 * H:
                lo_i -= 1
            while hi_i < len(u) - 1 and u[hi_i + 1] - u[hi_i] < 0.02 * H:
                hi_i += 1
            win = float(np.clip(1.25 * (u[hi_i] - u[lo_i]), 0.24 * H, 0.45 * H))
    # sphere centred a little behind the nose so the face (not the back of the skull)
    # sits in the flat top of the falloff
    center = tip - fwd * (0.2 * win)
    if plan in UPRIGHT:
        center[2] = tip[2] + 0.03 * H
    rad = 0.45 * win
    r = np.linalg.norm(V - center, axis=1) / rad
    head = 1 - smoothstep(r, 0.75, 1.15)
    report["head_center"] = [round(float(x), 4) for x in center]
    report["head_radius"] = round(float(rad), 4)

# ---------------------------------------------------------------- extremity prior
bvh = BVHTree.FromPolygons([tuple(v) for v in V], [tuple(f) for f in F], all_triangles=True)
diag = float(np.linalg.norm(mx - mn))
eps = 1e-4 * diag
maxd = 0.25 * diag
thick = np.full(nv, maxd)
step = max(1, nv // job.get("thickness_samples", 60000))
idx = np.arange(0, nv, step)
for i in idx:
    n = vn[i]
    o = V[i] - n * eps
    hit = bvh.ray_cast(Vector(o), Vector(-n), maxd)
    if hit[0] is not None:
        thick[i] = hit[3]
if step > 1:  # fill the unsampled vertices by smoothing from the sampled ones
    known = np.zeros(nv, bool)
    known[idx] = True
    val = np.where(known, thick, 0.0)
    wgt = known.astype(float)
    for _ in range(8):
        val = np.where(known, thick, smooth(val, 1))
        wgt = np.where(known, 1.0, smooth(wgt, 1))
    thick = np.where(known, thick, val / np.maximum(wgt, 1e-6))
thin = np.clip(1 - thick / max(np.percentile(thick, 60), 1e-9), 0, 1)
cen = np.average(V, axis=0, weights=varea)
dist = np.linalg.norm(V - cen, axis=1)
far = smoothstep(dist / max(np.percentile(dist, 99.5), 1e-9), 0.45, 0.9)
ext = smooth(thin * far, 3)
ext = np.clip(ext / max(np.percentile(ext, 99), 1e-9), 0, 1)

wc, wh, we = job.get("w_curv", 0.6), job.get("w_head", 0.7), job.get("w_ext", 0.35)
imp = np.clip(wc * curv + wh * head + we * ext, 0, 1)
imp = smooth(imp, 2)
report["importance_mean"] = round(float(imp.mean()), 3)
report["importance_frac_gt_0.5"] = round(float((imp > 0.5).mean()), 3)
report["head_frac"] = round(float((head > 0.5).mean()), 3)

# ---------------------------------------------------------------- debug heatmap
if job.get("heatmap"):
    attr = me.color_attributes.new("imp", "FLOAT_COLOR", "POINT")
    cols = np.stack([imp, 0.15 + 0.0 * imp, 1 - imp, np.ones(nv)], 1).astype(np.float32)
    attr.data.foreach_set("color", cols.ravel())
    mat = bpy.data.materials.new("heat")
    mat.use_nodes = True
    nt = mat.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    a = nt.nodes.new("ShaderNodeAttribute")
    a.attribute_name = "imp"
    em = nt.nodes.new("ShaderNodeEmission")
    outn = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(a.outputs["Color"], em.inputs["Color"])
    nt.links.new(em.outputs["Emission"], outn.inputs["Surface"])
    me.materials.clear()
    me.materials.append(mat)
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE"
    scene.view_settings.view_transform = "Standard"
    scene.render.resolution_x, scene.render.resolution_y = 1024, 512
    cd = bpy.data.cameras.new("c")
    cd.type = "ORTHO"
    cd.ortho_scale = max(mx[0] - mn[0], mx[1] - mn[1], H) * 1.05
    cam = bpy.data.objects.new("c", cd)
    scene.collection.objects.link(cam)
    scene.camera = cam
    c = Vector(((mn + mx) / 2).tolist())
    tiles = []
    for k, d in enumerate((Vector((0, -1, 0)), Vector((1, 0, 0)))):  # generator front (-Y), side
        cam.location = c + d * diag * 2
        cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
        scene.render.resolution_x = scene.render.resolution_y = 512
        scene.render.filepath = job["heatmap"] + f".{k}.png"
        bpy.ops.render.render(write_still=True)
        img = bpy.data.images.load(scene.render.filepath)
        tiles.append(np.array(img.pixels[:], np.float32).reshape(512, 512, 4))
        bpy.data.images.remove(img)
        import os
        os.remove(scene.render.filepath)
    strip = np.concatenate(tiles, axis=1)
    out = bpy.data.images.new("hm", width=1024, height=512, alpha=True)
    out.pixels[:] = strip.ravel()
    out.filepath_raw = job["heatmap"]
    out.file_format = "PNG"
    out.save()
    me.color_attributes.remove(me.color_attributes["imp"])
    me.materials.clear()

# ---------------------------------------------------------------- decimate
t0 = time.time()
vg = obj.vertex_groups.new(name="decimate")
allow = 1.0 - 0.9 * imp
if job.get("uniform"):
    allow[:] = 1.0
for val in np.unique(np.round(allow, 2)):
    sel = np.nonzero(np.round(allow, 2) == val)[0].tolist()
    vg.add(sel, float(val), "REPLACE")


def decimate(target):
    mod = obj.modifiers.new("decimate", "DECIMATE")
    mod.decimate_type = "COLLAPSE"
    mod.ratio = min(1.0, target / max(nf, 1))
    mod.use_collapse_triangulate = True
    if not job.get("uniform"):
        mod.vertex_group = "decimate"
        mod.vertex_group_factor = job.get("vg_factor", 0.5)
    if job.get("symmetry"):
        mod.use_symmetry = True
        mod.symmetry_axis = "X"
    bpy.ops.object.modifier_apply(modifier="decimate")
    return len(obj.data.polygons)


# Blender's collapse keeps the topology, so a mesh with many handles (bows, straps,
# weapons fused to hands; the UDF remesh adds more) has a floor it cannot go below, and
# as it nears that floor the collapse degenerates (the goblin turns into shards at
# ~2.8k). Probe the floor on a copy; if the target is under FLOOR_MARGIN x the floor,
# write nothing and report a fallback (forge.py then uses the uniform GPU decimator).
FLOOR_MARGIN = job.get("floor_margin", 1.6)
probe = me.copy()
obj.data = probe
floor = decimate(1)
obj.data = me
bpy.data.meshes.remove(probe)
report["topology_floor"] = floor
if job["target_tris"] < FLOOR_MARGIN * floor:
    report["fallback"] = True
    report["seconds"] = round(time.time() - t_start, 2)
    with open(job["report"], "w") as f:
        json.dump(report, f, indent=2)
    print("DECIMATE_FALLBACK", json.dumps(report))
    raise SystemExit(0)
report["fallback"] = False
target = job["target_tris"]
decimate(target)
report["effective_target"] = target
report["decimate_seconds"] = round(time.time() - t0, 2)
report["faces_out"] = sum(len(p.vertices) - 2 for p in me.polygons)
report["verts_out"] = len(me.vertices)
obj.vertex_groups.clear()
if job.get("topo"):
    report["topo_lo"] = topo(me)
bpy.ops.object.select_all(action="DESELECT")
obj.select_set(True)
bpy.ops.export_scene.gltf(filepath=job["dst"], export_format="GLB", use_selection=True,
                          export_yup=True, export_apply=True, export_normals=False,
                          export_texcoords=False, export_materials="NONE")
report["seconds"] = round(time.time() - t_start, 2)
report["vg_factor"] = job.get("vg_factor", 0.5)
report["weights"] = [wc, wh, we]
with open(job["report"], "w") as f:
    json.dump(report, f, indent=2)
print("DECIMATE_OK", json.dumps(report))

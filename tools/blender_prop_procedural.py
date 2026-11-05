"""Procedural props in headless Blender, built directly in prop space (docs/equipment.md).

Run by tools/props.py (stage `mesh` of a prop whose override sets `procedural`):
    blender -b --factory-startup -P tools/blender_prop_procedural.py -- '<job json>'

Writes `dst` (model_raw.glb) with the grip at the origin, the tip toward glTF +Y and the
"edge" side (where a hanging lash droops) toward glTF -Z, plus a 512 px base colour
texture, so tools/blender_prop_qa.py can take it as is (job `prebuilt`: no PCA, no grip
search; it only rescales to length_m, exports and draws the axes sheet).

Blender axes as in blender_prop_qa.py: glTF +Y = Blender +Z, glTF -Z = Blender +Y.

Kinds:
- `whip`: a short stiff handle (pommel knob, leather wrap, a ferrule knot where the lash
  starts) and a braided lash that tapers to a thin popper and droops slightly (it bends
  progressively toward -Z, ~20 deg at the tip), one 8-sided tube with UVs: handle and lash
  each get their own half of the texture (u), v runs along the length and repeats.
  The generated whip was folded in a U by Pixal3D, so its length axis was half the whip.

Job keys: dst, report, kind (whip), length_m, tri_budget, seed.
"""
import json
import math
import sys

import bmesh
import bpy
import numpy as np
from mathutils import Vector

job = json.loads(sys.argv[sys.argv.index("--") + 1])
KIND = job.get("kind", "whip")
L = float(job["length_m"])
rng = np.random.default_rng(int(job.get("seed", 0)))
bpy.ops.wm.read_factory_settings(use_empty=True)


def texture_whip(size=512):
    """Base colour: left half (u < 0.5) the handle's spiral leather wrap, right half the
    lash's 8-strand braid (diagonal interleave). RGBA floats, rows bottom-up."""
    h = w = size
    v, u = np.mgrid[0:h, 0:w].astype(np.float32)
    u, v = (u + 0.5) / w, (v + 0.5) / h
    img = np.zeros((h, w, 4), np.float32)
    img[..., 3] = 1
    noise = rng.random((h, w)).astype(np.float32)
    # handle: dark brown leather strips wound in a spiral, a dark seam between strips
    hu = u * 2  # 0..1 around the handle
    ph = (hu + v * 6) % 1.0
    seam = np.clip(np.abs(ph - 0.5) * 2, 0, 1) ** 6  # 1 at the strip edges
    leather = np.array([0.20, 0.11, 0.06])
    hcol = leather[None, None, :] * (1.0 - 0.55 * seam[..., None]) * (0.9 + 0.2 * noise[..., None])
    # lash: two families of diagonal strands, alternating over / under
    lu = (u - 0.5) * 2
    a = (lu * 4 + v * 8) % 1.0
    b = (lu * 4 - v * 8) % 1.0
    over = ((np.floor(lu * 4 + v * 8) + np.floor(lu * 4 - v * 8)) % 2) < 1
    strand = np.where(over, np.sin(np.pi * a), np.sin(np.pi * b))
    braid = np.array([0.36, 0.22, 0.12])
    lcol = braid[None, None, :] * (0.45 + 0.6 * strand[..., None]) * (0.92 + 0.16 * noise[..., None])
    img[..., :3] = np.where((u < 0.5)[..., None], hcol, lcol)
    return np.clip(img, 0, 1)


def build_whip():
    """One 8-sided tube along a centre line that is straight through the handle and bends
    toward Blender +Y (glTF -Z) along the lash. Returns (mesh object, stats)."""
    NS = 8
    budget = int(job.get("tri_budget", 1000))
    # handle (metres along the centre line, from the butt; scaled to 1.8 m wielder sizes)
    butt, handle_len, knot_len = 0.0, 0.26, 0.04
    grip_s = 0.11  # grip (origin): the middle of a ~9 cm fist resting on the pommel knob
    # handle profile: pommel knob, wrapped grip, flare into the ferrule knot
    hprof = [(0.0, 0.004), (0.006, 0.019), (0.02, 0.023), (0.035, 0.018), (0.05, 0.0155),
             (0.10, 0.015), (0.15, 0.0148), (0.20, 0.0145), (0.235, 0.0152), (handle_len, 0.017),
             (handle_len + 0.5 * knot_len, 0.019), (handle_len + knot_len, 0.012)]
    lash_len = 1.0  # arc length, solved below so the extent along the tip axis is L
    tip_angle = math.radians(20)
    # lash rings: as many as the budget allows (2 NS tris per segment + the caps)
    n_lash = max(16, (budget - 2 * (NS - 2)) // (2 * NS) - (len(hprof) - 1))

    def centre(s_arr, lash_len):
        """Centre line points (y, z) for arc lengths s (from the butt)."""
        s0 = butt + handle_len + knot_len
        pts = []
        y = z = 0.0
        prev = 0.0
        ds_ = np.diff(np.concatenate([[0.0], s_arr]))
        for s, ds in zip(s_arr, ds_):
            x = max(0.0, (s - s0) / lash_len)
            ang = tip_angle * x ** 1.6  # straight off the handle, bending more toward the tip
            mid = 0.5 * (prev + ang)
            y += ds * math.sin(mid)
            z += ds * math.cos(mid)
            prev = ang
            pts.append((y, z))
        return np.array(pts)

    def lash_profile(lash_len):
        s0 = butt + handle_len + knot_len
        # denser rings near the tip, where the bend and the taper are
        x = 1 - (1 - np.linspace(0, 1, n_lash + 1)[1:]) ** 1.3
        s = s0 + x * lash_len
        r = 0.011 * (1 - x) ** 0.8 + 0.0022  # thick at the knot, thin popper at the tip
        return list(zip(s.tolist(), r.tolist()))

    # solve the lash arc length so the z extent (butt to tip) is L
    for _ in range(30):
        prof = hprof + lash_profile(lash_len)
        s_arr = np.array([p[0] for p in prof])
        c = centre(s_arr, lash_len)
        ext = c[-1, 1] - c[0, 1]
        lash_len *= L / ext if ext > 0 else 1.0
        if abs(ext - L) < 1e-5:
            break
    prof = hprof + lash_profile(lash_len)
    s_arr = np.array([p[0] for p in prof])
    c = centre(s_arr, lash_len)
    radii = np.array([p[1] for p in prof])
    # tangents for the ring frames
    tang = np.gradient(c, axis=0)
    tang /= np.linalg.norm(tang, axis=1)[:, None]
    gz = float(np.interp(grip_s, s_arr, c[:, 1]))
    me = bpy.data.meshes.new("prop")
    bm = bmesh.new()
    uv = bm.loops.layers.uv.new("UVMap")
    rings = []
    n_handle = len(hprof)
    for i, ((y, z), r, (ty, tz)) in enumerate(zip(c.tolist(), radii.tolist(), tang.tolist())):
        # ring in the plane normal to the tangent (tangent in the YZ plane): axes X and N
        n = Vector((0.0, tz, -ty))
        ring = []
        for k in range(NS):
            a = 2 * math.pi * k / NS
            p = Vector((0.0, y, z - gz)) + r * (math.cos(a) * Vector((1, 0, 0)) + math.sin(a) * n)
            ring.append(bm.verts.new(p))
        rings.append(ring)
    bm.verts.ensure_lookup_table()
    for i in range(len(rings) - 1):
        handle_seg = i < n_handle - 1
        u0 = 0.0 if handle_seg else 0.5
        # v repeats: the wrap every 13 cm of handle, the braid every 8 cm of lash
        rep = 0.13 if handle_seg else 0.08
        v0, v1 = s_arr[i] / rep, s_arr[i + 1] / rep
        for k in range(NS):
            f = bm.faces.new((rings[i][k], rings[i][(k + 1) % NS], rings[i + 1][(k + 1) % NS], rings[i + 1][k]))
            for loop, (kk, vv) in zip(f.loops, ((k, v0), (k + 1, v0), (k + 1, v1), (k, v1))):
                loop[uv].uv = (u0 + 0.5 * kk / NS, vv)
    # caps: the pommel end and the popper tip, as fans
    for ring, flip in ((rings[0], True), (rings[-1], False)):
        cen = sum((v.co for v in ring), Vector()) / NS
        cv = bm.verts.new(cen)
        for k in range(NS):
            vs = (ring[k], ring[(k + 1) % NS], cv)
            f = bm.faces.new(vs[::-1] if flip else vs)
            for loop in f.loops:
                loop[uv].uv = (0.25 if flip else 0.75, 0.0)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    bmesh.ops.triangulate(bm, faces=bm.faces[:])
    bm.to_mesh(me)
    bm.free()
    for p in me.polygons:
        p.use_smooth = True
    obj = bpy.data.objects.new("prop", me)
    bpy.context.scene.collection.objects.link(obj)
    # material: one base colour texture (packed into the GLB), leather roughness
    tex = texture_whip()
    img = bpy.data.images.new("whip_base_color", tex.shape[1], tex.shape[0], alpha=False)
    img.pixels[:] = tex.ravel()
    img.pack()
    mat = bpy.data.materials.new("whip")
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    tn = nt.nodes.new("ShaderNodeTexImage")
    tn.image = img
    nt.links.new(tn.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.75
    bsdf.inputs["Metallic"].default_value = 0.0
    me.materials.append(mat)
    tip = c[-1]
    stats = {"kind": "whip", "sides": NS, "rings": len(rings), "handle_m": round(handle_len + knot_len, 3),
             "grip_from_butt_m": grip_s, "lash_arc_m": round(lash_len, 4),
             "arc_length_m": round(float(s_arr[-1]), 4), "extent_m": round(float(c[-1, 1] - c[0, 1]), 4),
             "tip_droop_m": round(float(tip[0]), 4), "tip_angle_deg": round(math.degrees(tip_angle), 1),
             "radius_m": {"pommel": 0.023, "grip": 0.0148, "lash_base": round(float(radii[n_handle]), 4),
                          "tip": round(float(radii[-1]), 4)},
             "texture": [tex.shape[1], tex.shape[0]]}
    return obj, stats


if KIND != "whip":
    raise SystemExit(f"unknown procedural kind {KIND!r}")
obj, stats = build_whip()
me = obj.data
stats["tris"] = sum(len(p.vertices) - 2 for p in me.polygons)
stats["verts"] = len(me.vertices)
bpy.ops.object.select_all(action="DESELECT")
obj.select_set(True)
bpy.ops.export_scene.gltf(filepath=job["dst"], export_format="GLB", use_selection=True, export_yup=True,
                          export_apply=True)
with open(job["report"], "w") as f:
    json.dump(stats, f, indent=2)
print("PROP_PROCEDURAL_OK", json.dumps(stats))

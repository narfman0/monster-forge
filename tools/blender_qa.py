"""Headless Blender QA pass for one monster GLB.

Run by tools/forge.py:  blender -b --factory-startup -P tools/blender_qa.py -- '<job json>'

Imports model_raw.glb, joins meshes, orients it (faces -Z in glTF / +Y in Blender by
default), scales it to scale_m (height for upright plans, horizontal length otherwise),
puts the origin at the feet, exports model.glb, renders a 4-view contact strip and
writes a JSON report (tri count, bounds, non-manifold edges, orientation guess).
"""
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
transform(Matrix.Translation(Vector((-(mn[0] + mx[0]) / 2, -(mn[1] + mx[1]) / 2, -mn[2]))))
v = verts_np()
mn, mx = v.min(0), v.max(0)
report["scale_factor"] = round(float(s), 5)
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

with open(job["report"], "w") as f:
    json.dump(report, f, indent=2)
print("QA_OK", json.dumps(report))

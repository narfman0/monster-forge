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

strip = np.concatenate(tiles, axis=1)
out = bpy.data.images.new("thumb", width=strip.shape[1], height=strip.shape[0], alpha=True)
out.pixels[:] = strip.ravel()
out.filepath_raw = job["thumb"]
out.file_format = "PNG"
out.save()

with open(job["report"], "w") as f:
    json.dump(report, f, indent=2)
print("QA_OK", json.dumps(report))

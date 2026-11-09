"""blender -b --factory-startup -P blender_ext_prep.py -- '<job json>'

Flatten an externally made character glb into a static, textured model_raw.glb for the forge's
QA stage: rig modifiers applied at the REST pose, parents baked, armatures/empties/lights/cameras
dropped, meshes over `tri_budget` decimated (collapse, UVs kept), exported with materials.
job: {src, dst, tri_budget, drop: [name substrings of mesh objects to delete], pose: REST|POSE}.
Writes <dst>.json with {tris_in, tris_out, meshes, materials, dims}.
"""
import json
import sys

import bpy
from mathutils import Vector

job = json.loads(sys.argv[sys.argv.index("--") + 1])
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=job["src"])
sc = bpy.context.scene
for a in [o for o in sc.objects if o.type == "ARMATURE"]:
    a.data.pose_position = job.get("pose", "REST")  # "POSE" for models whose bind pose is not the displayed one
bpy.context.view_layer.update()
dg = bpy.context.evaluated_depsgraph_get()

drop = [s.lower() for s in job.get("drop") or []]
for o in list(sc.objects):
    if o.type == "MESH" and any(s in o.name.lower() for s in drop):
        bpy.data.objects.remove(o, do_unlink=True)

meshes = [o for o in sc.objects if o.type == "MESH" and not o.hide_get() and o.data.polygons]
for o in meshes:
    mw = o.matrix_world.copy()
    me = bpy.data.meshes.new_from_object(o.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)
    for m in list(o.modifiers):
        o.modifiers.remove(m)
    o.vertex_groups.clear()
    o.data = me
    o.parent = None
    o.matrix_world = mw
for o in list(sc.objects):
    if o.type != "MESH":
        bpy.data.objects.remove(o, do_unlink=True)
bpy.ops.object.select_all(action="DESELECT")
for o in meshes:
    o.select_set(True)
bpy.context.view_layer.objects.active = meshes[0]
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)


def tri_count(objs):
    return sum(len(p.vertices) - 2 for o in objs for p in o.data.polygons)


tris_in = tri_count(meshes)
budget = job.get("tri_budget") or 0
if budget and tris_in > budget:
    ratio = budget / tris_in
    for o in meshes:
        # keep tiny pieces (eyes, accessories) whole; the big surfaces carry the reduction
        if len(o.data.polygons) < 400:
            continue
        md = o.modifiers.new("dec", "DECIMATE")
        md.decimate_type = "COLLAPSE"
        md.ratio = ratio
        md.use_collapse_triangulate = True
        bpy.context.view_layer.objects.active = o
        bpy.ops.object.modifier_apply(modifier=md.name)
tris_out = tri_count(meshes)

mn = Vector((1e9,) * 3); mx = Vector((-1e9,) * 3)
for o in meshes:
    for v in o.data.vertices:
        mn = Vector(map(min, mn, v.co)); mx = Vector(map(max, mx, v.co))
mats = {s.material.name for o in meshes for s in o.material_slots if s.material}
bpy.ops.export_scene.gltf(filepath=job["dst"], export_format="GLB", export_apply=False, export_yup=True,
                          export_materials="EXPORT", export_image_format="AUTO", use_selection=False)
json.dump({"tris_in": tris_in, "tris_out": tris_out, "meshes": len(meshes), "materials": len(mats),
           "dims": [round(x, 4) for x in (mx - mn)]}, open(job["dst"] + ".json", "w"))
print("EXT_PREP", tris_in, "->", tris_out, "meshes", len(meshes))

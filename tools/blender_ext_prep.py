"""blender -b --factory-startup -P blender_ext_prep.py -- '<job json>'

Flatten an externally made character glb into a static, textured model_raw.glb for the forge's
QA stage: rig modifiers applied at the REST pose, parents baked, armatures/empties/lights/cameras
dropped, meshes over `tri_budget` decimated (collapse, UVs kept), exported with materials.
job: {src, dst, tri_budget, drop: [name substrings of mesh objects to delete], pose: REST|POSE}.
Writes <dst>.json with {tris_in, tris_out, meshes, materials, dims, pose}.
pose AUTO (default): measures REST and POSE and keeps the saner bounding box.
"""
import json
import sys

import bpy
from mathutils import Matrix, Vector

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blender_ext_common as ext  # noqa: E402

job = json.loads(sys.argv[sys.argv.index("--") + 1])
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=job["src"])
sc = bpy.context.scene


POSE = ext.choose_pose(sc, job.get("pose", "AUTO"), bpy.context.evaluated_depsgraph_get)
for a in [o for o in sc.objects if o.type == "ARMATURE"]:
    a.data.pose_position = POSE  # "POSE" for models whose bind pose is not the displayed one
bpy.context.view_layer.update()
dg = bpy.context.evaluated_depsgraph_get()

drop = [s.lower() for s in (job.get("drop") or []) + ["icosphere"]]  # Sketchfab exports often carry a default Blender Icosphere
drop_mat = [s.lower() for s in job.get("drop_materials") or []]
for o in list(sc.objects):
    if o.type == "MESH" and (any(s in o.name.lower() for s in drop) or
                             any(s in (sl.material.name.lower() if sl.material else "") for sl in o.material_slots for s in drop_mat)):
        bpy.data.objects.remove(o, do_unlink=True)

def drop_cards(objs, depsgraph, max_verts=24, min_frac=0.2):
    """Delete tiny, large-footprint connected components (alpha cards / hair planes that lost their transparency in
    the glTF export): <= max_verts vertices spanning >= min_frac of the figure's height in two axes."""
    import bmesh
    boxes = {}
    ev = {o: o.evaluated_get(depsgraph) for o in objs}
    zmin, zmax = 1e9, -1e9
    for o, e in ev.items():
        me = e.to_mesh()
        for v in me.vertices:
            w = e.matrix_world @ v.co
            zmin, zmax = min(zmin, w.z), max(zmax, w.z)
        e.to_mesh_clear()
    height = max(zmax - zmin, 1e-6)
    removed = 0
    for o, e in ev.items():
        me = e.to_mesh()
        pos = [e.matrix_world @ v.co for v in me.vertices]
        e.to_mesh_clear()
        bm = bmesh.new()
        bm.from_mesh(o.data)
        bm.verts.ensure_lookup_table()
        seen, kill = set(), []
        for v in bm.verts:
            if v.index in seen:
                continue
            st, comp = [v], []
            seen.add(v.index)
            while st:
                x = st.pop()
                comp.append(x)
                for ed in x.link_edges:
                    y = ed.other_vert(x)
                    if y.index not in seen:
                        seen.add(y.index)
                        st.append(y)
            if len(comp) <= max_verts:
                ext = sorted(max(pos[c.index][i] for c in comp) - min(pos[c.index][i] for c in comp) for i in range(3))
                if ext[1] >= min_frac * height:
                    kill += comp
        if kill:
            removed += len(kill)
            bmesh.ops.delete(bm, geom=kill, context="VERTS")
            bm.to_mesh(o.data)
        bm.free()
    return removed

meshes = [o for o in sc.objects if o.type == "MESH" and not o.hide_get() and o.data.polygons]
if job.get("drop_cards"):
    print("DROPPED card verts", drop_cards(meshes, dg))
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()  # re-evaluate: the bake below must see the edited meshes
_b = ext.basis(ext.main_armature(sc), dg)
R = ext.upright(_b)  # figure stands on its side / upside down in some rips
_props = [] if job.get("keep_props") else ext.outliers(meshes, dg, _b, R)
_isl = []
meshes = [o for o in meshes if o.name not in _props]  # filter first: a removed object has no .name
for n in _props:
    bpy.data.objects.remove(bpy.data.objects[n], do_unlink=True)
if not job.get("keep_props") and meshes:
    _isl = ext.drop_prop_islands(meshes, dg, R)
    if _isl:
        bpy.context.view_layer.update()
        dg = bpy.context.evaluated_depsgraph_get()
        print("AUTO dropped islands", [(a, b, c) for a, b, c in _isl][:12])
if _props or R != Matrix.Identity(4):
    print("AUTO", "dropped props", _props, "upright", [round(x, 2) for x in R.to_euler()])
for o in meshes:
    mw = R @ o.matrix_world
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
           "dims": [round(x, 4) for x in (mx - mn)], "pose": POSE, "props_dropped": _props}, open(job["dst"] + ".json", "w"))
print("EXT_PREP", tris_in, "->", tris_out, "meshes", len(meshes))

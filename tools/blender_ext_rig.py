"""blender -b --factory-startup -P blender_ext_rig.py -- '<job json>'

The skinned counterpart of blender_ext_prep.py: keep an externally made character's OWN skeleton and
skin weights, baked to the pose that looks right, in the frame QA put model.glb in (yaw, scale, feet
on z=0, centred), so tools/ext_rig.py can map its joints onto the UAL humanoid.

job: {src, dst, pose: REST|POSE, drop: [mesh-name substrings], yaw_deg, scale, tri_budget}
  yaw_deg / scale: qa.json yaw_deg and scale_factor (QA turned the static mesh by them; the skinned
  mesh must move the same way).
Writes dst (glb, armature + skinned meshes, materials) and dst + ".json":
  {bones: {name: {parent, head, tail, deform}}, height, meshes, tris, unskinned_bound: [mesh names]}

Steps: pose the armature, bake every mesh's deformation at that pose, make that pose the rest pose
(armature_apply), bind rigid parts (eyes, accessories parented to a bone) to their bone, join all
meshes into one object named "monster" (tools/blender_rig_ual.py rigs the biggest mesh only), prune
leaf bones that carry no weight (weapon / effect / socket nodes parked at the origin), add a `<hand>_tip`
leaf where a hand has no deforming child (the glTF importer guesses bone tails; the UAL fit reads the
hand's tail as the fingertip), apply yaw/scale/translation, export. job: hands: [hand bone names]; merge: {vertex group: group to fold it into}.
"""
import json
import math
import sys

import bpy
from mathutils import Matrix, Vector

job = json.loads(sys.argv[sys.argv.index("--") + 1])
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=job["src"])
sc = bpy.context.scene
arm = max((o for o in sc.objects if o.type == "ARMATURE"), key=lambda o: len(o.data.bones))
for a in [o for o in sc.objects if o.type == "ARMATURE" and o is not arm]:
    bpy.data.objects.remove(a, do_unlink=True)
# glTF files from DCC exports carry their unit scale on EMPTY parent nodes (e.g. 0.0254 x 0.70):
# bake the armature's world transform into the object before the empties are deleted.
_mw = arm.matrix_world.copy()
arm.parent = None
arm.matrix_world = _mw
for o in sc.objects:
    if o.type == "MESH" and o.parent is None:  # loose meshes keep their world transform too
        _m = o.matrix_world.copy()
        o.matrix_world = _m
arm.data.pose_position = job.get("pose", "REST")
bpy.context.view_layer.update()

drop = [s.lower() for s in (job.get("drop") or []) + ["icosphere"]]  # Sketchfab exports often carry a default Blender Icosphere
drop_mat = [s.lower() for s in job.get("drop_materials") or []]
for o in list(sc.objects):
    if o.type == "MESH" and (any(s in o.name.lower() for s in drop) or
                             any(s in (sl.material.name.lower() if sl.material else "") for sl in o.material_slots for s in drop_mat)):
        bpy.data.objects.remove(o, do_unlink=True)
    elif o.type not in ("MESH", "ARMATURE"):
        bpy.data.objects.remove(o, do_unlink=True)
dg = bpy.context.evaluated_depsgraph_get()
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

meshes = [o for o in sc.objects if o.type == "MESH" and o.data.polygons and not o.hide_get()]
if job.get("drop_cards"):
    print("DROPPED card verts", drop_cards(meshes, dg))
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()  # re-evaluate: the bake below must see the edited meshes
rigid = []
for o in meshes:
    skinned = any(m.type == "ARMATURE" and m.object == arm for m in o.modifiers)
    if skinned:
        me = bpy.data.meshes.new_from_object(o.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)
        o.data = me  # world matrix is unchanged: the deformation is baked in object space
        continue
    # rigid part: bake its world transform, bind fully to its bone (or the nearest bone)
    mw = o.matrix_world.copy()
    bone = o.parent_bone if o.parent == arm and o.parent_type == "BONE" else None
    o.parent = None
    o.matrix_world = mw
    bpy.context.view_layer.objects.active = o
    bpy.ops.object.select_all(action="DESELECT")
    o.select_set(True)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    if bone is None:
        c = sum((v.co for v in o.data.vertices), Vector()) / max(len(o.data.vertices), 1)
        bone = min((b for b in arm.data.bones if b.use_deform),
                   key=lambda b: ((arm.matrix_world @ b.head_local) - c).length).name
    vg = o.vertex_groups.new(name=bone)
    vg.add([v.index for v in o.data.vertices], 1.0, "REPLACE")
    o.parent = arm
    o.matrix_parent_inverse = arm.matrix_world.inverted()
    md = o.modifiers.new("Armature", "ARMATURE")
    md.object = arm
    rigid.append(o.name)

# the displayed pose becomes the rest pose
bpy.context.view_layer.objects.active = arm
bpy.ops.object.select_all(action="DESELECT")
arm.select_set(True)
bpy.ops.object.mode_set(mode="POSE")
bpy.ops.pose.select_all(action="SELECT")
bpy.ops.pose.armature_apply(selected=False)
bpy.ops.object.mode_set(mode="OBJECT")
arm.data.pose_position = "REST"
bpy.context.view_layer.update()

# QA frame: yaw about Z, uniform scale, then feet on z=0 and centred in x/y
M = Matrix.Scale(job["scale"], 4) @ Matrix.Rotation(math.radians(job["yaw_deg"]), 4, "Z")
arm.matrix_world = M @ arm.matrix_world
for o in meshes:
    if o.name in bpy.data.objects and o.parent != arm:
        o.matrix_world = M @ o.matrix_world
bpy.context.view_layer.update()
dg = bpy.context.evaluated_depsgraph_get()
mn = Vector((1e9,) * 3); mx = Vector((-1e9,) * 3)
for o in meshes:
    ev = o.evaluated_get(dg)
    me = ev.to_mesh()
    for v in me.vertices:
        w = ev.matrix_world @ v.co
        mn = Vector(map(min, mn, w)); mx = Vector(map(max, mx, w))
    ev.to_mesh_clear()
T = Vector((-(mn.x + mx.x) / 2, -(mn.y + mx.y) / 2, -mn.z))
arm.location += T
for o in meshes:
    if o.parent != arm:
        o.location += T
bpy.context.view_layer.update()
bpy.ops.object.select_all(action="DESELECT")
arm.select_set(True)
for o in meshes:
    o.select_set(True)
bpy.context.view_layer.objects.active = arm
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

# one mesh object: join (vertex groups with the same name merge, every material slot is kept)
bpy.ops.object.select_all(action="DESELECT")
for o in meshes:
    o.select_set(True)
bpy.context.view_layer.objects.active = max(meshes, key=lambda o: len(o.data.vertices))
if len(meshes) > 1:
    bpy.ops.object.join()
mon = bpy.context.view_layer.objects.active
mon.name = "monster"
meshes = [mon]

# merge vertex groups (job.merge: {src: dst})
for src_n, dst_n in (job.get("merge") or {}).items():
    s_g = mon.vertex_groups.get(src_n)
    if s_g is None:
        continue
    d_g = mon.vertex_groups.get(dst_n) or mon.vertex_groups.new(name=dst_n)
    for v in mon.data.vertices:
        for g in v.groups:
            if g.group == s_g.index and g.weight > 0:
                d_g.add([v.index], g.weight, "ADD")
    mon.vertex_groups.remove(s_g)

# prune: leaf bones with no weight that are not a hand/foot/head anchor
weighted = set()
import numpy as np
for vg in mon.vertex_groups:
    weighted.add(vg.name)
used = {}
for v in mon.data.vertices:
    for g in v.groups:
        if g.weight > 1e-4:
            used[mon.vertex_groups[g.group].name] = True
keep_always = set(job.get("hands") or [])
bpy.context.view_layer.objects.active = arm
bpy.ops.object.mode_set(mode="EDIT")
eb = arm.data.edit_bones
pruned = []
changed = True
while changed:
    changed = False
    for b in list(eb):
        if not b.children and b.name not in used and b.name not in keep_always:
            pruned.append(b.name)
            eb.remove(b)
            changed = True
# hands: some exporters leave a hand bone pointing up/out (the glTF importer guesses tails from child
# positions). The UAL fit reads hand head -> tail as the hand axis, so re-aim any hand that is more than 25
# degrees off its forearm at the fingertips (farthest weighted vertex along the forearm direction).
for hn in job.get("hands") or []:
    if hn not in eb or not eb[hn].parent:
        continue
    h = eb[hn]
    fdir = (h.head - h.parent.head)
    if fdir.length < 1e-6:
        continue
    fdir.normalize()
    cur = (h.tail - h.head)
    if cur.length > 1e-6 and math.degrees(cur.angle(fdir)) <= 25:
        continue
    desc, stack = {hn}, [h]
    while stack:
        for c in stack.pop().children:
            desc.add(c.name)
            stack.append(c)
    gi = {mon.vertex_groups[n].index for n in desc if n in mon.vertex_groups}
    far = 0.0
    for v in mon.data.vertices:
        if sum(g.weight for g in v.groups if g.group in gi) > 0.5:
            far = max(far, (v.co - h.head).dot(fdir))
    far = min(max(far, 0.5 * h.parent.length), 0.18 * (mx.z - mn.z))
    for c in h.children:
        c.use_connect = False
    h.use_connect = False
    h.tail = h.head + fdir * far
    print("HAND re-aimed", hn, "was", round(math.degrees(cur.angle(fdir))) if cur.length > 1e-6 else None, "deg off; tip at", round(far, 3))
# tip leaves on hands: the forearm direction continued past the wrist
arm_mw = arm.matrix_world
for hn in job.get("hands") or []:
    if hn not in eb:
        continue
    h = eb[hn]
    if any(c.name in used for c in h.children):
        continue
    par = h.parent
    d = (h.head - par.head) if par else Vector((0, 0, -1))
    if d.length < 1e-6:
        d = Vector((0, 0, -1))
    tip = eb.new(hn + "_tip")
    tip.head = h.head + d.normalized() * 0.35 * d.length
    tip.tail = tip.head + d.normalized() * 0.02 * max(d.length, 0.01)
    tip.parent = h
    tip.use_connect = False
bpy.ops.object.mode_set(mode="OBJECT")
bpy.context.view_layer.update()

def clear_normals(objs):
    """Drop custom split normals (toon heads often carry sphere-transferred ones that no longer match the
    geometry once the mesh is baked from a posed armature) and use smooth normals from the geometry."""
    for o in objs:
        bpy.context.view_layer.objects.active = o
        bpy.ops.object.select_all(action="DESELECT")
        o.select_set(True)
        try:
            bpy.ops.mesh.customdata_custom_splitnormals_clear()
        except RuntimeError:
            pass
        bpy.ops.object.shade_smooth()

def alpha_key(mats, tol=0.06):
    """Materials whose texture has an opaque flat background that the source viewer blended away (eye / eyeshadow
    overlays on a grey sheet): key the most common colour out to alpha 0, link image alpha -> principled alpha,
    and export the material as BLEND. job.alpha_key: [material-name substrings]."""
    import numpy as np
    done = []
    for m in bpy.data.materials:
        if not m.use_nodes or not any(k.lower() in m.name.lower() for k in mats):
            continue
        tex = next((n for n in m.node_tree.nodes if n.type == "TEX_IMAGE" and n.image), None)
        bsdf = next((n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if not tex or not bsdf:
            continue
        im = tex.image
        w, h = im.size
        px = np.array(im.pixels[:], dtype=np.float32).reshape(h, w, 4)
        q = (px[..., :3] * 255).round().astype(np.int32)
        keys, counts = np.unique(q.reshape(-1, 3), axis=0, return_counts=True)
        bg = keys[counts.argmax()] / 255.0
        d = np.abs(px[..., :3] - bg).max(axis=2)
        px[..., 3] = np.clip((d - tol) / tol, 0.0, 1.0)  # soft edge, fully clear within tol of the background
        new = bpy.data.images.new(im.name + "_keyed", w, h, alpha=True)
        new.pixels = px.reshape(-1).tolist()
        new.alpha_mode = "STRAIGHT"
        new.pack()
        tex.image = new
        m.node_tree.links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
        try:
            m.surface_render_method = "BLENDED"
        except Exception:
            m.blend_method = "BLEND"
        done.append((m.name, [round(float(x), 2) for x in bg], round(float((px[..., 3] < 0.5).mean()), 2)))
    return done

if job.get("clear_normals"):
    clear_normals(meshes)
if job.get("alpha_key"):
    print("ALPHA_KEY", alpha_key(job["alpha_key"]))

budget = job.get("tri_budget") or 0
tris = sum(len(p.vertices) - 2 for o in meshes for p in o.data.polygons)
if budget and tris > budget:
    for o in meshes:
        if len(o.data.polygons) < 400:
            continue
        md = o.modifiers.new("dec", "DECIMATE")
        md.decimate_type = "COLLAPSE"; md.ratio = budget / tris; md.use_collapse_triangulate = True
        bpy.context.view_layer.objects.active = o
        keep = [m for m in o.modifiers if m.type == "ARMATURE"]
        for m in keep:
            m.show_viewport = False
        bpy.ops.object.modifier_apply(modifier=md.name)
        for m in keep:
            m.show_viewport = True
    tris = sum(len(p.vertices) - 2 for o in meshes for p in o.data.polygons)

bpy.ops.object.select_all(action="DESELECT")
arm.select_set(True)
for o in meshes:
    o.select_set(True)
bpy.context.view_layer.objects.active = arm
bpy.ops.export_scene.gltf(filepath=job["dst"], export_format="GLB", use_selection=True, export_yup=True,
                          export_apply=False, export_skins=True, export_animations=False,
                          export_materials="EXPORT", export_image_format="AUTO")
mw = arm.matrix_world
bones = {b.name: {"parent": b.parent.name if b.parent else None, "head": [round(x, 4) for x in (mw @ b.head_local)],
                  "tail": [round(x, 4) for x in (mw @ b.tail_local)], "deform": b.use_deform}
         for b in arm.data.bones}
json.dump({"weighted": sorted(used), "pruned": pruned, "bones": bones, "height": round(mx.z - mn.z, 4), "meshes": [o.name for o in meshes], "tris": tris,
           "rigid_bound": rigid}, open(job["dst"] + ".json", "w"))
print("EXT_RIG", len(bones), "bones", len(meshes), "meshes", tris, "tris")

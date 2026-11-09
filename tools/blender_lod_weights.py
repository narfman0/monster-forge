"""Skin weights from LOD0 onto a decimated LOD, and a rebuild of a monster's rigged LODs.

Imported by tools/blender_rig.py and tools/blender_rig_ual.py (`transfer_weights`, used where they
give each LOD LOD0's weights), and run by tools/relod.py:

    blender -b --factory-startup --python-exit-code 1 -P tools/blender_lod_weights.py -- <job.json>

`transfer_weights(src, dst)`: each dst vertex takes the weights of the LOD0 vertices nearest to it
**whose normal agrees** with its own (dot > NORMAL_COS among the K nearest; inverse-distance blend of
up to BLEND of them), then at most 4 influences, normalised. Blender's data_transfer
(POLYINTERP_NEAREST, used before 2026-11-08) takes the nearest surface point whatever its side: where
two sheets touch at rest (an octopus's tentacle tips curled onto its body, a tail on a flank, an arm
on a hip) a decimated vertex of one sheet sat millimetres from the other and got its weights, so it
stayed behind or tore when the parts moved apart (octopus LOD2 p95 14 cm off). The normal tells the
sheets apart: a tentacle's underside faces down, the body under it faces up.

Job (mode "rebuild"): rigged (model_rigged.glb), lods {k: static model_lod<k>.glb or null}, decimate
{k: target tris} (LODs to make by decimating the rigged LOD0 itself: for a rig whose finalize baked a
grounding pose into the rest, blender_rig.ground_feet, the static LODs no longer match LOD0), out
{k: dst path}, report. Exports like blender_rig.export_lod (LOD + armature, no images).
"""
import json
import sys

import bpy
import numpy as np
from mathutils import kdtree

K = 12            # LOD0 candidates per vertex
BLEND = 3         # of those, the nearest agreeing ones blended
NORMAL_COS = 0.3  # normals within ~73 deg agree


def _world_normals(obj):
    me = obj.data
    n = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("normal", n)
    m = np.array(obj.matrix_world.to_3x3().inverted().transposed())
    n = n.reshape(-1, 3) @ m.T
    return n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-9)


def _world_co(obj):
    me = obj.data
    c = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", c)
    mw = np.array(obj.matrix_world)
    return c.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]


def transfer_weights(src, dst) -> dict:
    """Give dst (mesh object) src's vertex-group weights, normal-aware (module doc). Replaces dst's
    groups of the same names; returns {"verts", "fallback"} (fallback: no agreeing normal among the K
    nearest, so the plain nearest was used)."""
    names = [g.name for g in src.vertex_groups]
    ng = len(names)
    sco, sn = _world_co(src), _world_normals(src)
    W = np.zeros((len(sco), ng), np.float32)
    for v in src.data.vertices:
        for ge in v.groups:
            W[v.index, ge.group] = ge.weight
    tree = kdtree.KDTree(len(sco))
    for i, p in enumerate(sco):
        tree.insert(p, i)
    tree.balance()
    dco, dn = _world_co(dst), _world_normals(dst)
    out = np.zeros((len(dco), ng), np.float32)
    fallback = 0
    for i, p in enumerate(dco):
        hits = tree.find_n(p, K)
        good = [(d, j) for (_, j, d) in hits if float(sn[j] @ dn[i]) > NORMAL_COS]
        if not good:
            good = [(d, j) for (_, j, d) in hits[:1]]
            fallback += 1
        good = sorted(good)[:BLEND]
        ws = np.array([1.0 / max(d, 1e-5) for d, _ in good])
        out[i] = (ws[:, None] * W[[j for _, j in good]]).sum(0) / ws.sum()
    # at most 4 influences, normalised (what the glTF exporter keeps anyway, made explicit)
    if ng > 4:
        cut = np.argpartition(-out, 4, axis=1)[:, 4:]
        np.put_along_axis(out, cut, 0.0, axis=1)
    out /= np.maximum(out.sum(1, keepdims=True), 1e-9)
    for g in list(dst.vertex_groups):
        if g.name in names:
            dst.vertex_groups.remove(g)
    for k, nm in enumerate(names):
        col = out[:, k]
        idx = np.nonzero(col > 0)[0]
        g = dst.vertex_groups.new(name=nm)
        for i in idx:   # add() takes one weight for all its vertices: one call per vertex
            g.add([int(i)], float(col[i]), "REPLACE")
    return {"verts": len(dco), "fallback": fallback}


# ------------------------------------------------------------------------------------------------
def _select(*objs):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]


def _import(path):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    return [o for o in bpy.data.objects if o not in before]


def rebuild(job: dict) -> dict:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    new = _import(job["rigged"])
    arm = next(o for o in new if o.type == "ARMATURE")
    me = next(o for o in new if o.type == "MESH" and o.vertex_groups)
    for o in new:
        if o not in (arm, me) and o.type == "MESH":
            bpy.data.objects.remove(o)
    rep = {"lods": {}}
    made = {}
    for k, path in (job.get("lods") or {}).items():
        objs = _import(path)
        lod = next(o for o in objs if o.type == "MESH")
        for o in objs:
            if o is not lod:
                bpy.data.objects.remove(o)
        made[k] = lod
    for k, target in (job.get("decimate") or {}).items():
        _select(me)
        bpy.ops.object.duplicate()
        lod = bpy.context.active_object
        lod.vertex_groups.clear()
        for m in list(lod.modifiers):
            lod.modifiers.remove(m)
        lod.parent = None
        lod.matrix_world = me.matrix_world.copy()
        # blender_lods.py's settings: collapse, triangulate, the ground contact locked
        co = _world_co(lod)
        zmin, h = co[:, 2].min(), co[:, 2].max() - co[:, 2].min()
        vg = lod.vertex_groups.new(name="__decimate")
        vg.add([i for i in range(len(co)) if co[i, 2] > zmin + 0.01 * h], 1.0, "REPLACE")
        vg.add([i for i in range(len(co)) if co[i, 2] <= zmin + 0.01 * h], 0.0, "REPLACE")
        mod = lod.modifiers.new("dec", "DECIMATE")
        mod.vertex_group, mod.vertex_group_factor = vg.name, 1.0
        mod.decimate_type, mod.use_collapse_triangulate = "COLLAPSE", True
        base = sum(len(p.vertices) - 2 for p in lod.data.polygons)
        ratio = target / base
        for _ in range(4):
            mod.ratio = ratio
            got = sum(len(p.vertices) - 2 for p in lod.evaluated_get(bpy.context.evaluated_depsgraph_get()).data.polygons)
            if abs(got - target) <= 0.03 * target:
                break
            ratio = min(1.0, ratio * target / max(got, 1))
        _select(lod)
        bpy.ops.object.modifier_apply(modifier=mod.name)
        lod.vertex_groups.remove(lod.vertex_groups["__decimate"])
        if lod.data.has_custom_normals:
            bpy.ops.mesh.customdata_custom_splitnormals_clear()
        lod.data.shade_smooth()
        made[k] = lod
    for k, lod in sorted(made.items()):
        for i in range(min(len(lod.data.materials), len(me.data.materials))):
            lod.data.materials[i] = me.data.materials[i]   # LOD0's material (docs/lods.md)
        r = transfer_weights(me, lod)
        lod.parent = arm
        lod.matrix_parent_inverse = arm.matrix_world.inverted()
        lod.modifiers.new("Armature", "ARMATURE").object = arm
        _select(lod, arm)
        bpy.ops.export_scene.gltf(filepath=job["out"][k], export_format="GLB", use_selection=True, export_yup=True,
                                  export_image_format="NONE")
        r["tris"] = sum(len(p.vertices) - 2 for p in lod.data.polygons)
        r["from"] = "decimated rigged LOD0" if k in (job.get("decimate") or {}) else job["lods"][k]
        rep["lods"][k] = r
        lod.hide_set(True)
    with open(job["report"], "w") as f:
        json.dump(rep, f, indent=1)
    print("RELOD_OK", json.dumps(rep))
    return rep


if __name__ == "__main__" and "--" in sys.argv:
    rebuild(json.loads(open(sys.argv[sys.argv.index("--") + 1]).read()))

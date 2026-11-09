"""Shared by blender_ext_prep.py and blender_ext_rig.py (import inside Blender): work out which way a
character stands from its own skeleton, and which meshes are not part of the body.

Game rips are messy in three ways this module absorbs:
  * the rig stands on its side or upside-down (a Z-up/Y-up export mix-up): `upright` rotates head-over-pelvis onto +Z;
  * props ship in the same file, laid out beside the figure (swords, staffs, sheaths, effect meshes): `outliers`
    flags meshes whose box is bigger than the figure or sits far from the pelvis, `auto_drop` removes them;
  * the rest pose and the displayed pose disagree, and one of them blows the mesh up: pick the pose with fewer
    outlier faces (prep and rig both call `choose_pose`, so they bake the same state).
Both scripts must come to the same answer, so every function here is deterministic in the file's contents.
"""
from __future__ import annotations

import re

import bpy
from mathutils import Matrix, Vector

NOT_HEAD = re.compile(r"(?i)top|end|tip|hair|eye|jaw|face|ear|neck|ik|ctrl|offset|twist|socket|helper")
NOT_CORE = re.compile(r"(?i)\b(ik|fk)\b|_ik|ik_|ctrl|offset|twist|end$|helper|pole|roll|socket|corrective|^c_|^cc_")


def main_armature(sc):
    arms = [o for o in sc.objects if o.type == "ARMATURE"]
    return max(arms, key=lambda o: len(o.data.bones)) if arms else None


def basis(arm, dg):
    """{pelvis, head, feet_z_along_up, up, height} in world space for the armature's evaluated state, or None."""
    if arm is None:
        return None
    ev = arm.evaluated_get(dg)
    pos = {pb.name: ev.matrix_world @ pb.head for pb in ev.pose.bones}
    names = list(pos)
    heads = [n for n in names if re.search(r"(?i)(^|[^a-z])head($|[^a-z])", n) and not NOT_HEAD.search(n)]
    hips = [n for n in names if re.search(r"(?i)hips?\b|pelvis|(^|[^a-z])hip($|[^a-z])", n) and not NOT_CORE.search(n)]
    feet = [n for n in names if re.search(r"(?i)foot|ankle", n) and not NOT_CORE.search(n) and "toe" not in n.lower()]
    if not hips:  # Auto-Rig Pro and friends: the pelvis is `root` / the first spine bone
        hips = [n for n in names if re.search(r"(?i)^root|spine_?0?1\b|spine_?a\b|spine1", n) and not re.search(r"(?i)^c_|ik|ctrl", n)] or \
               [n for n in names if re.search(r"(?i)spine_?0?1|spine_?a", n)]
    if not heads or not hips:
        return None
    head = pos[min(heads, key=len)]
    pel = pos[min(hips, key=len)]
    up = head - pel
    if up.length < 1e-6:
        return None
    up.normalize()
    low = min((pos[n].dot(up) for n in feet), default=pel.dot(up) - 0.5 * (head - pel).length)
    height = max((head.dot(up) - low) * 1.12, 1e-3)
    return {"pelvis": pel, "head": head, "up": up, "height": height}


def upright(b):
    """4x4 rotation taking the figure's up onto +Z (identity when it is already within 12 degrees)."""
    if not b:
        return Matrix.Identity(4)
    z = Vector((0, 0, 1))
    if b["up"].angle(z) < 0.21:
        return Matrix.Identity(4)
    return b["up"].rotation_difference(z).to_matrix().to_4x4()


def _vmin(vs):
    vs = list(vs)
    return Vector((min(v[0] for v in vs), min(v[1] for v in vs), min(v[2] for v in vs)))


def _vmax(vs):
    vs = list(vs)
    return Vector((max(v[0] for v in vs), max(v[1] for v in vs), max(v[2] for v in vs)))


def _box(o, dg):
    ev = o.evaluated_get(dg)
    mw = ev.matrix_world
    pts = [mw @ Vector(c) for c in ev.bound_box]
    return pts


def outliers(meshes, dg, b, R):
    """Names of meshes that are not the character body. Reference = the 'core': the biggest meshes that together hold
    60% of the faces (body, clothes, head). A mesh is a prop when
      * it is a long thin stick (a sword, staff, sheath: longest side >= 0.4 x core height, aspect >= 14:1), or
      * its box centre is further than 0.35 x core height outside the core box, or it is bigger than 1.5 x the core.
    Parts that sit inside the core box (heads, hands, hair, clothes) are never touched, whatever the skeleton says."""
    if not meshes:
        return []
    order = sorted(meshes, key=lambda o: -len(o.data.polygons))
    total = sum(len(o.data.polygons) for o in order) or 1
    core, acc = [], 0
    for o in order:
        core.append(o)
        acc += len(o.data.polygons)
        if acc >= 0.6 * total:
            break
    boxes = {}
    for o in meshes:
        pts = [R @ p for p in _box(o, dg)]
        boxes[o.name] = (_vmin(pts), _vmax(pts))
    clo = _vmin(boxes[o.name][0] for o in core)
    chi = _vmax(boxes[o.name][1] for o in core)
    H = max(chi.z - clo.z, 1e-6)
    out = []
    for o in meshes:
        if o in core and len(o.data.polygons) > 0.2 * total:
            continue
        lo, hi = boxes[o.name]
        e = hi - lo
        srt = sorted(e, reverse=True)
        c = (lo + hi) / 2
        thin = srt[0] >= 0.4 * H and srt[0] >= 14 * max(srt[1], 1e-6)
        away = max(clo.x - c.x, c.x - chi.x, clo.y - c.y, c.y - chi.y, clo.z - c.z, c.z - chi.z, 0.0) > 0.35 * H
        huge = srt[0] > 1.5 * max(chi - clo)
        if (thin or away or huge) and o not in core[:1]:
            out.append(o.name)
    return out


def score(sc, mode, dg_fn):
    """Outlier face fraction + a penalty when even the biggest mesh is far larger than the figure, in pose `mode`."""
    for a in [o for o in sc.objects if o.type == "ARMATURE"]:
        a.data.pose_position = mode
    bpy.context.view_layer.update()
    dg = dg_fn()
    arm = main_armature(sc)
    b = basis(arm, dg)
    meshes = [o for o in sc.objects if o.type == "MESH" and o.data.polygons and "icosphere" not in o.name.lower()]
    if not b or not meshes:
        return 1.0
    R = upright(b)
    bad = set(outliers(meshes, dg, b, R))
    total = sum(len(o.data.polygons) for o in meshes) or 1
    frac = sum(len(o.data.polygons) for o in meshes if o.name in bad) / total
    big = max(meshes, key=lambda o: len(o.data.polygons))
    pts = [R @ p for p in _box(big, dg)]
    lo, hi = _vmin(pts), _vmax(pts)
    over = max(0.0, max(hi - lo) / b["height"] - 1.6)  # the body mesh alone should not dwarf the skeleton
    return frac + over


def choose_pose(sc, want, dg_fn):
    """want REST | POSE | AUTO. AUTO keeps the pose whose meshes agree better with the skeleton."""
    if want in ("REST", "POSE"):
        return want
    r, p = score(sc, "REST", dg_fn), score(sc, "POSE", dg_fn)
    pick = "POSE" if p + 0.02 < r else "REST"
    print("POSE_PICK", pick, "rest", round(r, 3), "pose", round(p, 3))
    return pick


def drop_prop_islands(objs, dg, R, thin_min=0.3, aspect=6.0, away=0.15):
    """Delete loose connected components that are not the character: long thin sticks (swords, staffs, sheaths,
    chains) and pieces floating clear of the body. Works on evaluated positions (any armature deformation included)
    and edits each object's own mesh, so skin weights survive. Returns [(object, faces, why)] for the log.
    Reference body = the biggest components holding 60% of all faces. Needs the body to be one or a few big islands;
    a model that is hundreds of small islands (a mesh sliced per material) only loses its sticks."""
    import bmesh
    isl = []  # (obj, bm, verts(list of indices), faces, lo, hi)
    bms = {}
    for o in objs:
        e = o.evaluated_get(dg)
        me = e.to_mesh()
        pos = [R @ (e.matrix_world @ v.co) for v in me.vertices]
        e.to_mesh_clear()
        bm = bmesh.new()
        bm.from_mesh(o.data)
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bms[o] = bm
        seen = set()
        for v in bm.verts:
            if v.index in seen:
                continue
            st, comp = [v], []
            seen.add(v.index)
            while st:
                x = st.pop()
                comp.append(x.index)
                for ed in x.link_edges:
                    y = ed.other_vert(x)
                    if y.index not in seen:
                        seen.add(y.index)
                        st.append(y)
            pts = [pos[i] for i in comp]
            fc = sum(1 for i in comp for f in bm.verts[i].link_faces) // 3 or 1
            isl.append((o, comp, fc, _vmin(pts), _vmax(pts)))
    total = sum(i[2] for i in isl) or 1
    order = sorted(isl, key=lambda i: -i[2])
    core, acc = [], 0
    for i in order:
        core.append(i)
        acc += i[2]
        if acc >= 0.6 * total:
            break
    clo, chi = _vmin(i[3] for i in core), _vmax(i[4] for i in core)
    H = max(chi.z - clo.z, 1e-6)
    killed = []
    for i in isl:
        if i in core and i[2] > 0.1 * total:
            continue
        o, comp, fc, lo, hi = i
        e = hi - lo
        srt = sorted(e, reverse=True)
        c = (lo + hi) / 2
        thin = srt[0] >= thin_min * H and srt[0] >= aspect * max(srt[1], 1e-6)
        far = max(clo.x - c.x, c.x - chi.x, clo.y - c.y, c.y - chi.y, clo.z - c.z, c.z - chi.z, 0.0) > away * H
        if thin or far:
            killed.append((o, comp, fc, "stick" if thin else "floating"))
    by = {}
    for o, comp, fc, why in killed:
        by.setdefault(o, []).extend(comp)
    for o, bm in bms.items():
        if o in by:
            bmesh.ops.delete(bm, geom=[bm.verts[i] for i in by[o]], context="VERTS")
            bm.to_mesh(o.data)
        bm.free()
    return [(o.name, fc, why) for o, comp, fc, why in killed]

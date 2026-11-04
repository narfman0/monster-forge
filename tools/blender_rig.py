"""Rig stage helpers in headless Blender (tools/rig.py).

    blender -b --factory-startup -P tools/blender_rig.py -- '<job json>'

mode "facing": on model.glb (no skeleton yet), how far the body axis is from +Y and how
sure that is (job: src, report, plan, head_region, forward); see facing() below.
mode "score": report joint and weight stats for one SkinTokens roll, the parts
rig_topology finds, bones outside the mesh, weight bleed and a pose test (job: src,
report, sprawl, upright, plan); tools/rig.py turns them into the roll score.
mode "finalize": from the chosen rig write the game files (job: src, base_color,
lods {k: path}, out_dir, report, hands, sockets):

- model_rigged.glb: the rig with the current albedo, so a later paint needs no new rig.
  Sockets (docs/equipment.md) are added first: the hand bones are renamed hand_r/hand_l
  (or a small hand bone is added at the wrist when the arm ends at the forearm), the head
  bone becomes `head`, `grip_r`/`grip_l` are new non-deforming bones at the centre of each
  fist in the prop frame, and `back`, `hip_l`, `hip_r` are new non-deforming bones on the
  upper spine and pelvis. SkinTokens names bones bone_<n>, so without this a game has no
  way to find a hand;
- model_rigged_lod<k>.glb: each LOD with skin weights transferred from LOD0 by nearest
  surface point. All share one armature, so the joint order matches and an engine can swap
  LOD geometry under one skeleton. As with model_lod<k>.glb they carry no images.
"""
import json
import math
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Quaternion, Vector
from mathutils.bvhtree import BVHTree
from mathutils.kdtree import KDTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rig_topology  # noqa: E402

job = json.loads(sys.argv[sys.argv.index("--") + 1])
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=job["src"])
arm = me = None
if job["mode"] != "facing":
    arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    me = max((o for o in bpy.data.objects if o.type == "MESH"), key=lambda o: len(o.data.vertices))
    for o in list(bpy.data.objects):  # glTF bone display shapes
        if o.type == "MESH" and o is not me:
            bpy.data.objects.remove(o)


def bone_dicts() -> list[dict]:
    m = arm.matrix_world
    return [{"name": b.name, "parent": b.parent.name if b.parent else None,
             "head": list((m @ b.head_local)[:]), "tail": list((m @ b.tail_local)[:])} for b in arm.data.bones]


def stats() -> dict:
    bones = bone_dicts()
    z = np.array([(me.matrix_world @ v.co).z for v in me.data.vertices])
    height = float(z.max() - z.min())
    names = {g.index: g.name for g in me.vertex_groups}
    used = set()
    unweighted = 0
    for v in me.data.vertices:
        w = [g for g in v.groups if g.weight > 1e-4]
        if not w:
            unweighted += 1
        else:
            used.add(names[max(w, key=lambda g: g.weight).group])
    legs = rig_topology.find_legs(bones, height, 32, job.get("sprawl", False))
    return {"joints": len(bones), "legs": len(legs), "unweighted_verts": unweighted, "height_m": round(height, 3),
            "empty_bones": sum(1 for b in bones if b["name"] not in used),
            "zero_length_bones": sum(1 for b in arm.data.bones if b.length < 0.02 * height),
            "symmetry": round(rig_topology.symmetry(bones, height), 4)}


def select(*objs) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]


def set_albedo(path: str) -> None:
    img = bpy.data.images.load(path)
    img.colorspace_settings.name = "sRGB"
    for mat in me.data.materials:
        bsdf = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
        for link in bsdf.inputs["Base Color"].links:
            n = link.from_node
            while n.type != "TEX_IMAGE":  # through a Mix with the base colour factor, if any
                n = next(i.links[0].from_node for i in n.inputs if i.is_linked)
            n.image = img


def clamp_metallic(materials) -> None:
    """The Pixal3D metallic bake is noise per monster (all 255 on the goblin and harpy,
    0 on the wolf), which makes a painted creature read as full metal in any engine
    without an environment map. Stylised creatures are dielectric: drop the metallic
    texture, keep roughness."""
    for mat in materials:
        if not mat or not mat.use_nodes:
            continue
        bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf is None:
            continue
        for link in list(bsdf.inputs["Metallic"].links):
            mat.node_tree.links.remove(link)
        bsdf.inputs["Metallic"].default_value = 0.0


def transfer(lod_path: str, dst: Path) -> None:
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=lod_path)
    lod = next(o for o in bpy.data.objects if o not in before and o.type == "MESH")
    for i in range(min(len(lod.data.materials), len(me.data.materials))):
        lod.data.materials[i] = me.data.materials[i]  # LOD0's material, so the name matches (docs/lods.md)
    select(me, lod)
    bpy.context.view_layer.objects.active = me
    bpy.ops.object.data_transfer(data_type="VGROUP_WEIGHTS", use_create=True, vert_mapping="POLYINTERP_NEAREST",
                                 layers_select_src="ALL", layers_select_dst="NAME", mix_mode="REPLACE")
    select(lod)
    bpy.ops.object.vertex_group_limit_total(group_select_mode="ALL", limit=4)
    bpy.ops.object.vertex_group_normalize_all(group_select_mode="ALL", lock_active=False)
    lod.parent = arm
    lod.modifiers.new("Armature", "ARMATURE").object = arm
    select(lod, arm)
    bpy.ops.export_scene.gltf(filepath=str(dst), export_format="GLB", use_selection=True, export_yup=True,
                              export_image_format="NONE")
    bpy.data.objects.remove(lod)


def add_sockets() -> dict:
    """Rename and add the socket bones the registry asks for; returns {socket: source bone}."""
    bones = bone_dicts()
    z = np.array([(me.matrix_world @ v.co).z for v in me.data.vertices])
    height = float(z.max() - z.min())
    legs = rig_topology.find_legs(bones, height, 32, job.get("sprawl", False))
    want = set(job.get("sockets") or [])
    found = {}
    by_name = {b["name"]: b for b in bones}
    cx = rig_topology.centre_x(bones, legs)
    inv = arm.matrix_world.inverted()
    bpy.context.view_layer.objects.active = arm
    arm.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.data.edit_bones

    def new_bone(name: str, parent: str, head, length: float, direction=(0, 0, 1), roll_to=None) -> None:
        b = eb.new(name)
        b.head = inv @ Vector(head)
        b.tail = inv @ (Vector(head) + Vector(direction).normalized() * length)
        if roll_to is not None:  # local +Z of the bone toward this world direction
            b.align_roll(inv.to_3x3() @ Vector(roll_to))
        b.parent = eb[parent]
        b.use_deform = False
        found[name] = parent

    def grip(side: str, hand: str, head, tail) -> None:
        """grip_<side>: the prop frame (docs/equipment.md) as a bone. Origin at the centre
        of the fist, +Y forward (the blade or shaft at rest, before a clip turns the hand),
        +Z up so a prop's edge (-Z) hangs down. A BoneAttachment3D on it needs no offset.
        `hand` is the bone's current name, head/tail its world ends."""
        hh, ht = Vector(head), Vector(tail)
        new_bone(f"grip_{side}", hand, hh + (ht - hh) * 0.5, 0.05 * height, (0, 1, 0), (0, 0, 1))

    if job.get("hands"):
        for a in rig_topology.find_arms(bones, height, legs):
            name = f"hand_{a['side']}"
            if a["hand"]:
                eb[a["hand"]].name = name
                found[name] = a["hand"]
                grip(a["side"], name, by_name[a["hand"]]["head"], by_name[a["hand"]]["tail"])
            else:
                fa = by_name[a["forearm"]]  # continue the forearm: a prop's +Y then runs out of the fist
                new_bone(name, a["forearm"], fa["tail"], 0.04 * height, Vector(fa["tail"]) - Vector(fa["head"]))
                grip(a["side"], name, arm.matrix_world @ eb[name].head, arm.matrix_world @ eb[name].tail)
    if "head" in want:
        h = rig_topology.find_head(bones, height, legs)
        if h:
            eb[h].name = "head"
            found["head"] = h
    arms = rig_topology.find_arms(bones, height, legs)
    if "back" in want and arms:
        roots = {by_name[a["root"]]["parent"] for a in arms}
        spine = next(iter(roots)) if len(roots) == 1 else max(roots, key=lambda n: by_name[n]["head"][2])
        s = by_name[spine]["head"]
        new_bone("back", spine, (cx, s[1] - 0.06 * height, s[2]), 0.05 * height)
    if {"hip_l", "hip_r"} & want and legs:
        hips = {by_name[g["chain"][0]]["parent"] for g in legs if by_name[g["chain"][0]]["parent"]}
        if hips:
            pelvis = min(hips, key=lambda n: by_name[n]["head"][2])
            p = by_name[pelvis]["head"]
            zh = max(by_name[g["chain"][0]]["head"][2] for g in legs)
            for side, sx in (("l", -1), ("r", 1)):
                if f"hip_{side}" in want:
                    new_bone(f"hip_{side}", pelvis, (cx + sx * 0.12 * height, p[1], zh), 0.05 * height)
    bpy.ops.object.mode_set(mode="OBJECT")
    return found


# --- skeleton quality (mode "score"; tools/rig.py turns the terms into one number) --------
# Symmetry and leg count alone picked a 22-joint wolf over a 28-joint one and could not see
# a missing spine or tail. These terms measure what the anim stage needs: the parts
# rig_topology finds (the same finders blender_clips.py uses), bones that sit inside the
# body, weights that stay near their bone, and a pose test that bends every part found
# and measures the skinned mesh.

BEND_DEG = 30.0      # pose test: each part's root bone turns this far
FAR = 0.3            # a vertex this far (x height) from every bone of the bent part must not move
MOVED = 0.02         # ... by more than this (x height)
OUTSIDE = 0.05       # a joint this far (x height) outside the surface counts as outside
BLEED = 0.3          # weight on a vertex farther than this (x height) from its bone is bleeding


def mesh_world() -> tuple[np.ndarray, np.ndarray]:
    co = np.empty(len(me.data.vertices) * 3)
    me.data.vertices.foreach_get("co", co)
    mw = np.array(me.matrix_world)
    ed = np.empty(len(me.data.edges) * 2, dtype=np.int64)
    me.data.edges.foreach_get("vertices", ed)
    return co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3], ed.reshape(-1, 2)


def skinned() -> np.ndarray:
    dg = bpy.context.evaluated_depsgraph_get()
    ev = me.evaluated_get(dg)
    m = ev.to_mesh()
    co = np.empty(len(m.vertices) * 3)
    m.vertices.foreach_get("co", co)
    ev.to_mesh_clear()
    mw = np.array(ev.matrix_world)
    return co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]


def seg_dist(p: np.ndarray, a, b) -> np.ndarray:
    """Distance from points p (n,3) to the segment a-b."""
    a, b = np.asarray(a), np.asarray(b)
    ab = b - a
    t = np.clip(((p - a) @ ab) / max(float(ab @ ab), 1e-12), 0, 1)
    return np.linalg.norm(p - (a + t[:, None] * ab), axis=1)


def parts(bones: list[dict], height: float, cx: float) -> dict:
    """Everything rig_topology finds, as bone-name lists (also the sheet colours)."""
    sprawl, upright = job.get("sprawl", False), job.get("upright", False)
    legs = rig_topology.find_legs(bones, height, 32, sprawl)
    arms = rig_topology.find_arms(bones, height, legs) if upright else []
    head = rig_topology.find_head(bones, height, legs) if upright else \
        None if sprawl else rig_topology.find_snout(bones, height, legs)
    tail = [] if upright or sprawl else rig_topology.find_tail(bones, height, legs, min_bones=2)
    wings = rig_topology.find_wings(bones, height, legs, arms, exclude=(head,) if head else ())
    spine = rig_topology.find_spine(bones, height, legs, arms, head, upright)
    return {"legs": legs, "arms": arms, "head": head, "tail": tail, "wings": wings, "spine": spine,
            "sides": rig_topology.leg_sides(bones, legs, cx, height)}


def pose_test(bones: list[dict], found: dict, height: float, rest: np.ndarray, edges: np.ndarray) -> list[dict]:
    """Bend each part's root bone by BEND_DEG about an armature axis (legs, arms and neck
    swing about X, a tail about Z, wings flap about Y) and compare the skinned mesh with
    rest: `torn` is the share of the moving edges stretched past 2x, `stray` the share of
    vertices far from the part (FAR) that moved more than MOVED (weights bleeding into the
    body), `follow` the median ratio of how far the part's own skin moved to how far it
    would move rigidly (1 = the skin follows the bone, ~0 = it is skinned elsewhere)."""
    by_name = {b["name"]: b for b in bones}
    kids = rig_topology._children(bones)
    bends = [("leg", g["chain"][0], "X", BEND_DEG) for g in found["legs"]]
    bends += [("arm", a["upper"], "X", BEND_DEG) for a in found["arms"]]
    neck = found["spine"]["neck"]
    if neck:
        bends.append(("neck", neck[0], "X", BEND_DEG))
    if found["tail"]:
        bends.append(("tail", found["tail"][0], "Z", BEND_DEG))
    bends += [("wing", w["chain"][0], "Y", -BEND_DEG if w["side"] == "r" else BEND_DEG) for w in found["wings"]]
    rest_len = np.linalg.norm(rest[edges[:, 0]] - rest[edges[:, 1]], axis=1)
    ok_edge = rest_len > 1e-6
    world = arm.matrix_world
    out = []
    for kind, name, ax, deg in bends:
        pb = arm.pose.bones[name]
        pb.rotation_mode = "QUATERNION"
        r = (world @ pb.bone.matrix_local).to_quaternion()
        q = Quaternion(Vector({"X": (1, 0, 0), "Y": (0, 1, 0), "Z": (0, 0, 1)}[ax]), math.radians(deg))
        pb.rotation_quaternion = r.inverted() @ q @ r
        posed = skinned()
        pb.rotation_quaternion = Quaternion()
        disp = np.linalg.norm(posed - rest, axis=1)
        sub = [name] + rig_topology._descendants(name, kids)
        d = np.min([seg_dist(rest, by_name[n]["head"], by_name[n]["tail"]) for n in sub], axis=0)
        moving = (disp[edges[:, 0]] > 0.002 * height) | (disp[edges[:, 1]] > 0.002 * height)
        ratio = np.linalg.norm(posed[edges[:, 0]] - posed[edges[:, 1]], axis=1) / np.where(ok_edge, rest_len, 1)
        sel = moving & ok_edge
        far = d > FAR * height
        # rigid motion of the part's own skin about the bent joint
        piv = np.array(by_name[name]["head"])
        R = np.array(q.to_matrix())
        rigid = np.linalg.norm((rest - piv) @ R.T + piv - rest, axis=1)
        near = (d < 0.05 * height) & (rigid > 0.01 * height)
        out.append({"part": kind, "bone": name, "axis": ax, "deg": deg,
                    "torn": round(float((ratio[sel] > 2.0).sum() / max(1, sel.sum())), 4),
                    "stretch_max": round(float(ratio[sel].max()) if sel.any() else 1.0, 2),
                    "stray": round(float((far & (disp > MOVED * height)).sum() / max(1, far.sum())), 4),
                    "follow": round(float(np.median(disp[near] / rigid[near])) if near.sum() > 10 else 0.0, 3)})
    return out


def quality() -> dict:
    bones = bone_dicts()
    rest, edges = mesh_world()
    height = float(rest[:, 2].max() - rest[:, 2].min())
    cx = float(np.median(rest[:, 0]))
    found = parts(bones, height, cx)
    by_name = {b["name"]: b for b in bones}
    # bone ends outside the body: the surface normal at the nearest point says which side
    bvh = BVHTree.FromPolygons([Vector(p) for p in rest], [tuple(p.vertices) for p in me.data.polygons])
    # joints only: glTF stores no bone tails, the importer guesses a leaf bone's length
    out_d = []
    for p in (b["head"] for b in bones):
        loc, nrm, _, dist = bvh.find_nearest(Vector(p))
        out_d.append(0.0 if loc is None or (Vector(p) - loc).dot(nrm) <= 0 else dist / height)
    out_d = np.array(out_d)
    # weight bleed: weight sitting far from its bone, and side bones owning the other side
    names = {g.index: g.name for g in me.vertex_groups}
    vi, bi, ww = [], [], []
    for v in me.data.vertices:
        for g in v.groups:
            if g.weight > 0.05 and names[g.group] in by_name:
                vi.append(v.index)
                bi.append(names[g.group])
                ww.append(g.weight)
    vi, ww = np.array(vi), np.array(ww)
    bi = np.array(bi)
    far_w = side_w = cross_w = 0.0
    for n in set(bi.tolist()):
        m = bi == n
        b = by_name[n]
        d = seg_dist(rest[vi[m]], b["head"], b["tail"])
        far_w += float(ww[m][d > BLEED * height].sum())
        mx = (b["head"][0] + b["tail"][0]) / 2 - cx
        if abs(mx) > 0.1 * height:  # a side bone: weight on the far side of the centre line
            side_w += float(ww[m].sum())
            cross_w += float(ww[m][(rest[vi[m], 0] - cx) * np.sign(mx) < -0.05 * height].sum())
    poses = pose_test(bones, found, height, rest, edges)
    sp = found["spine"]
    return {
        "legs_r": found["sides"]["r"], "legs_l": found["sides"]["l"], "feet_mirror": round(found["sides"]["mirror"], 4),
        "arms": len(found["arms"]), "wings": len(found["wings"]), "tail_bones": len(found["tail"]),
        "head": found["head"], "torso_bones": len(sp["torso"]), "torso_length": round(sp["torso_length"], 3),
        "neck_bones": len(sp["neck"]),
        "outside": round(float((out_d > OUTSIDE).mean()), 4), "outside_max": round(float(out_d.max()), 3),
        "bleed_far": round(far_w / max(float(ww.sum()), 1e-9), 4),
        "bleed_cross": round(cross_w / max(side_w, 1e-9), 4),
        "pose": poses,
        "parts": {"leg": [n for g in found["legs"] for n in g["chain"]], "arm": [n for a in found["arms"] for n in a["chain"]],
                  "torso": sp["torso"], "neck": sp["neck"], "tail": found["tail"],
                  "wing": [n for w in found["wings"] for n in w["chain"]]},
    }

# --- facing check (mode "facing", on model.glb before any roll) --------------------------
# SkinTokens scrambles a skeleton when the body does not point along +Y (glTF -Z): the
# dragon gave 4 of 4 garbage rolls with its head 65 degrees off. QA's auto-facing is the
# thing that went wrong there, so this estimate shares nothing with it: the body axis is
# the vertical plane the mesh is most mirror-symmetric about (wings, legs and horns are
# paired, so they vote for the true axis instead of pulling it sideways), the head end is
# found separately per plan, and the ground footprint is a second opinion for legged
# horizontal bodies.

UPRIGHT = {"biped", "winged_biped", "centauroid", "merfolk_naga", "floating"}
HORIZONTAL = {"quadruped", "winged_quadruped", "avian", "insectoid", "serpentine", "aquatic"}
SIGN_MIN = 0.2  # head-end cue below this: report only how far the body axis is off


def surface_samples(n: int = 4000):
    """Face centres and areas of every mesh (world space), plus an area-weighted sample."""
    cen, area = [], []
    for o in bpy.data.objects:
        if o.type != "MESH":
            continue
        m = o.data
        c = np.empty(len(m.polygons) * 3)
        m.polygons.foreach_get("center", c)
        a = np.empty(len(m.polygons))
        m.polygons.foreach_get("area", a)
        mw = np.array(o.matrix_world)
        cen.append(c.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3])
        a = a * abs(np.linalg.det(mw[:3, :3])) ** (2 / 3)
        area.append(a)
    cen, area = np.concatenate(cen), np.concatenate(area)
    rng = np.random.default_rng(0)
    pick = rng.choice(len(cen), size=min(n, len(cen)), replace=False, p=area / area.sum())
    return cen, area, pick


def _wmedian(x, w):
    o = np.argsort(x)
    c = np.cumsum(w[o])
    return float(x[o][np.searchsorted(c, c[-1] / 2)])


def mirror_error(cen, area, pick, tree, phi: float, h: float):
    """Mean distance (fraction of height) from each sample mirrored across the vertical
    plane through the body that contains the direction phi (degrees from +Y toward +X) to
    the nearest surface point. The plane's offset is the area-weighted median sideways."""
    a = math.radians(phi)
    lat = np.array((math.cos(a), -math.sin(a), 0.0))
    s = cen @ lat
    best = None
    c0 = _wmedian(s, area)
    for c in (c0 - 0.02 * h, c0, c0 + 0.02 * h):
        q = cen[pick] - 2 * (s[pick] - c)[:, None] * lat
        e = sum(tree.find(p)[2] for p in q) / len(q) / h
        if best is None or e < best[0]:
            best = (e, c)
    return best


def facing() -> dict:
    plan = job.get("plan", "")
    cen, area, pick = surface_samples()
    z0, z1 = cen[:, 2].min(), cen[:, 2].max()
    h = float(z1 - z0)
    tree = KDTree(len(cen))
    for i, p in enumerate(cen):
        tree.insert(p, i)
    tree.balance()
    # coarse scan of the symmetry plane over 180 degrees, then refine around the best
    errs = {phi: mirror_error(cen, area, pick, tree, phi, h)[0] for phi in range(-90, 90, 3)}
    phi0 = min(errs, key=errs.get)
    fine = {phi0 + d: mirror_error(cen, area, pick, tree, phi0 + d, h) for d in np.arange(-3, 3.01, 0.5)}
    phi = float(min(fine, key=lambda k: fine[k][0]))
    e_best, offset = fine[phi]
    e_med = float(np.median(list(errs.values())))
    # 1 when the best plane is far better than a typical one, 0 when nothing stands out
    # (a blob, a radial spider); an asymmetric pose (a curled tail, a turned neck) raises
    # e_best and lowers it too.
    contrast = max(0.0, 1.0 - e_best / max(e_med, 1e-9))
    a = math.radians(phi)
    fwd = np.array((math.sin(a), math.cos(a), 0.0))
    lat = np.array((math.cos(a), -math.sin(a), 0.0))
    t = cen @ fwd
    side = cen @ lat - offset
    out = {"plan": plan, "symmetry_error": round(e_best, 4), "symmetry_error_median": round(e_med, 4),
           "symmetry_contrast": round(contrast, 3), "axis_deg": round(phi, 1),
           "scan": {int(k): round(v, 4) for k, v in errs.items()}}
    # second opinion for legged horizontal bodies: the long axis of the ground footprint
    low = cen[:, 2] < z0 + 0.04 * h
    if plan in HORIZONTAL and low.sum() > 20:
        xy = cen[low, :2] - np.average(cen[low, :2], axis=0, weights=area[low])
        w, vecs = np.linalg.eigh(np.cov(xy.T, aweights=area[low]))
        ax = vecs[:, int(np.argmax(w))]
        fp = math.degrees(math.atan2(ax[0], ax[1]))
        fp = (fp + 90) % 180 - 90
        out["footprint_axis_deg"] = round(fp, 1)
        out["footprint_elongation"] = round(float(math.sqrt(w.max() / max(w.min(), 1e-12))), 2)
        out["footprint_vs_symmetry_deg"] = round(abs((fp - phi + 90) % 180 - 90), 1)
    # head end, per plan
    sign, sign_conf, sign_rule = 1.0, 0.0, "none"
    span = t.max() - t.min()
    if plan in HORIZONTAL and plan != "insectoid":
        # horizontal bodies carry the head high and the tail low: surface in the top 40%
        # of the height, near the centre plane (wings are far out), at each end
        mid = np.abs(side) < 0.15 * span
        high = cen[:, 2] > z0 + 0.6 * h
        fr = float(area[mid & high & (t > t.max() - 0.3 * span)].sum())
        bk = float(area[mid & high & (t < t.min() + 0.3 * span)].sum())
        sign = 1.0 if fr >= bk else -1.0
        sign_conf = abs(fr - bk) / max(fr + bk, 1e-9)
        sign_rule = "high_centre_end"
        out["end_high_area"] = [round(bk, 4), round(fr, 4)]
    elif plan in UPRIGHT:
        # feet point forward: the sole (lowest 4%) reaches farther along the axis than the
        # ankles above it
        sole = cen[:, 2] < z0 + 0.04 * h
        shin = (cen[:, 2] > z0 + 0.08 * h) & (cen[:, 2] < z0 + 0.2 * h) & (np.abs(side) < 0.3 * h)
        if sole.sum() > 10 and shin.sum() > 10:
            d = float(np.average(t[sole], weights=area[sole]) - np.average(t[shin], weights=area[shin]))
            sign = 1.0 if d >= 0 else -1.0
            sign_conf = min(1.0, abs(d) / (0.04 * h))
            sign_rule = "toes_ahead_of_ankles"
            out["toe_offset"] = round(d / h, 4)
    fwd = fwd * sign * (-1 if job.get("forward") == "+Z" else 1)  # a +Z export faces -Y here
    ang = math.degrees(math.atan2(fwd[0], fwd[1]))
    axis_off = abs((ang + 90) % 180 - 90)  # ignoring which end is the head
    head_known = sign_conf >= SIGN_MIN
    # the yaw that would turn this estimate onto +Y: QA rotates by `yaw` about +Z, so
    # adding the measured bearing to QA's yaw lands the head (or the axis) on +Y
    turn = ang if head_known else (phi + 90) % 180 - 90
    out.update({"sign_rule": sign_rule, "sign_confidence": round(sign_conf, 3),
                "angle_deg": round(abs(ang) if head_known else axis_off, 1), "bearing_deg": round(turn, 1),
                "axis_off_deg": round(axis_off, 1), "head_known": head_known,
                "confidence": round(float(np.clip((contrast - 0.1) / 0.3, 0, 1)), 2)})
    # the head region QA framed (model space, so it already sits on the +Y side): its
    # bearing from the body centre is a third opinion for plans with a head end
    hr = job.get("head_region")
    if hr and plan in HORIZONTAL:
        c = np.average(cen, axis=0, weights=area)
        v = np.array(hr["center"][:2]) - c[:2]
        out["head_region_bearing_deg"] = round(math.degrees(math.atan2(v[0], v[1])), 1)
    return out


if job["mode"] == "facing":
    report = facing()
else:
    report = stats()
if job["mode"] == "score" and job.get("quality", True):
    report.update(quality())
if job["mode"] == "finalize":
    out = Path(job["out_dir"])
    report["sockets"] = add_sockets()
    set_albedo(job["base_color"])
    clamp_metallic(me.data.materials)  # same rule as blender_qa.py, for rigs built from older model.glb files
    select(me, arm)
    bpy.ops.export_scene.gltf(filepath=str(out / "model_rigged.glb"), export_format="GLB", use_selection=True,
                              export_yup=True)
    report["lods"] = {}
    for k, path in job["lods"].items():
        transfer(path, out / f"model_rigged_lod{k}.glb")
        report["lods"][k] = f"model_rigged_lod{k}.glb"
with open(job["report"], "w") as f:
    json.dump(report, f, indent=1)
print("_OK")

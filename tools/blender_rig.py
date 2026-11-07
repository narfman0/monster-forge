"""Rig stage helpers in headless Blender (tools/rig.py).

    blender -b --factory-startup -P tools/blender_rig.py -- '<job json>'

mode "facing": on model.glb (no skeleton yet), how far the body axis is from +Y and how
sure that is (job: src, report, plan, head_region, forward); see facing() below.
mode "score": report joint and weight stats for one SkinTokens roll, the parts
rig_topology finds, bones outside the mesh, weight bleed and a pose test (job: src,
report, sprawl, upright, plan); tools/rig.py turns them into the roll score.
mode "fallback": a generated skeleton for plans SkinTokens cannot rig (job: src = model.glb,
dst, report, kind, plan); see fallback() below. tools/rig.py RIG_STRATEGY says which plans.
mode "finalize": from the chosen rig write the game files (job: src, base_color,
lods {k: path}, out_dir, report, hands, sockets, reweight):

- add_wings (winged plans): when find_wings finds fewer than 2 wing chains, the missing
  wings are found on the mesh and wing_<side>_00.. chains inserted on the spine and
  weighted (blender_wing_weights.add_missing_wings; report key wings_added). Mode "score"
  does the same with this key, so rig/chosen.json measures the skeleton the game files
  get; rig/chosen.glb itself is not changed.
- add_tail (rig.adds_tail plans: quadruped, winged quadruped, centauroid): when find_tail
  finds fewer than 4 bones, the tail is found on the mesh and a tail_00.. chain inserted on
  the hip bone (or a shorter SkinTokens chain that owns it adopted and extended) and weighted
  (blender_tail_chain.add_missing_tail; report key tail_added). Mode "score" does the same.
- tail_adopt (the override `rig_tail_chain: {"adopt": bone}`, any plan): the chain from that
  bone is renamed tail_00.. and keeps its weights (blender_tail_chain.adopt_named), instead
  of add_tail; same report key.
- reweight (optional, the `rig_reweight` override): chains re-weighted geometrically and
  optionally fitted into the limb (tools/rig_reweight.py) before anything else but the
  wings. Only the game files see it; rig/chosen.glb and its quality score keep SkinTokens'
  weights.

- model_rigged.glb: the rig with the current albedo, so a later paint needs no new rig.
  Sockets (docs/equipment.md) are added first: the hand bones are renamed hand_r/hand_l
  (or a small hand bone is added at the wrist when the arm ends at the forearm), the head
  bone becomes `head`, `grip_r`/`grip_l` are new non-deforming bones at the centre of each
  fist in the prop frame, and `back`, `hip_l`, `hip_r` are new non-deforming bones on the
  upper spine and pelvis. SkinTokens names bones bone_<n>, so without this a game has no
  way to find a hand;
- a legged mesh QA grounded on something other than its feet (a tail fan) is lowered onto
  them and the parts below the feet are turned up, as a new rest pose (ground_feet; job
  key feet = the plan's foot count);
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
from mathutils import Matrix, Quaternion, Vector
from mathutils.bvhtree import BVHTree
from mathutils.kdtree import KDTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blender_leg_mirror  # noqa: E402
import blender_tail_chain  # noqa: E402
import blender_wing_weights  # noqa: E402
import rig_reweight  # noqa: E402
import rig_topology  # noqa: E402

job = json.loads(sys.argv[sys.argv.index("--") + 1])
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=job["src"])
arm = me = None
if job["mode"] not in ("facing", "fallback"):
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


def transfer(lod_path: str):
    """Import a static LOD and give it LOD0's weights (nearest surface point, at most 4
    influences, normalised) under the one armature; returns the LOD object (export_lod)."""
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=lod_path)
    lod = next(o for o in bpy.data.objects if o not in before and o.type == "MESH")
    for o in set(bpy.data.objects) - before - {lod}:
        bpy.data.objects.remove(o)
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
    return lod


def export_lod(lod, dst: Path) -> None:
    select(lod, arm)
    bpy.ops.export_scene.gltf(filepath=str(dst), export_format="GLB", use_selection=True, export_yup=True,
                              export_image_format="NONE")


FEET_GAP = 0.03  # metres: feet standing higher than this at rest get the body lowered onto them
CLEAR = 0.02     # x height: a chain turned up off the ground clears it by this much (room for a crouch)


def ground_feet(feet: int, objs: list) -> dict | None:
    """Stand a legged mesh on its feet. QA grounds the mesh on its lowest vertex; when that
    is not a foot (the giant eagle rests on its tail fan, its feet 0.35 m up), the rest
    pose lowers the whole body until the lowest vertex the legs own touches z = 0, and
    every other chain that then goes below the ground (a tail fan, a feather lobe) is
    turned up at its root just far enough to rest on it (about the horizontal axis across
    the chain, as the clips turn a tail). The pose is baked into the meshes (`objs`: LOD0
    and the rigged LODs, weighted in the old rest pose) and becomes the new rest pose.
    Returns what it did, or None when the feet already stand on the ground or what holds
    them up is not a chain that can be turned."""
    if feet <= 0 or job.get("sprawl"):
        return None
    bones = bone_dicts()
    by_name = {b["name"]: b for b in bones}
    kids = rig_topology._children(bones)
    rest, _ = mesh_world()
    height = float(rest[:, 2].max() - rest[:, 2].min())
    legs = rig_topology.find_legs(bones, height, feet, False)
    if len(legs) < feet:
        return None
    names = {g.index: g.name for g in me.vertex_groups}
    owner = np.array([names[max(v.groups, key=lambda g: g.weight).group] if v.groups else "" for v in me.data.vertices])
    leg_bones = {n for g in legs for n in g["chain"]}
    on_leg = np.isin(owner, sorted(leg_bones))
    ground = float(rest[:, 2].min())
    shift = float(rest[on_leg, 2].min()) - ground if on_leg.any() else 0.0
    if shift <= FEET_GAP:
        return None
    clear = CLEAR * height
    # chains (from a branch point, not legs) whose skin would go below the ground; the
    # outermost such chain is turned, so a fan with branches turns as one
    sink = []
    for b in bones:
        n = b["name"]
        if n in leg_bones or not b["parent"] or len(kids[b["parent"]]) < 2:
            continue
        chain = [n] + rig_topology._descendants(n, kids)
        if leg_bones & set(chain):
            continue
        mask = np.isin(owner, chain)
        if mask.any() and rest[mask, 2].min() - shift < ground + clear:
            sink.append((n, chain, mask))
    sink = [c for c in sink if not any(c[0] in o[1][1:] for o in sink)]
    # only when what holds the feet up can be turned out of the way: the skin below the
    # feet must belong to those chains, not to the root or the trunk (the harpy's hip
    # feathers, skinned to the root, hang 4 cm below its feet)
    below = rest[:, 2] < ground + shift - 1e-4
    covered = np.zeros(len(rest), bool)
    for _, _, mask in sink:
        covered |= mask
    if (below & ~covered).sum() > 0.02 * max(1, below.sum()):
        return None
    w = arm.matrix_world
    to_arm = w.inverted()
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="POSE")
    root = next(b["name"] for b in bones if b["parent"] is None)
    pr = arm.pose.bones[root]
    pr.matrix = to_arm @ Matrix.Translation((0, 0, -shift)) @ w @ pr.bone.matrix_local
    turned = {}
    for n, chain, mask in sink:
        pb = arm.pose.bones[n]
        pb.rotation_mode = "QUATERNION"
        h0 = Vector(by_name[n]["head"])
        end = Vector(rest[mask][np.argmin(rest[mask, 2])])  # the part that touches the ground
        d = Vector((end.x - h0.x, end.y - h0.y, 0))
        if d.length < 1e-6:
            continue
        ax = d.normalized().cross(Vector((0, 0, 1)))  # + lifts the far end
        r = (w @ pb.bone.matrix_local).to_quaternion()
        lo, hi = 0.0, 90.0
        for _ in range(14):
            mid = (lo + hi) / 2
            pb.rotation_quaternion = r.inverted() @ Quaternion(ax, math.radians(mid)) @ r
            bpy.context.view_layer.update()
            if skinned()[mask, 2].min() < ground + clear:
                lo = mid
            else:
                hi = mid
        pb.rotation_quaternion = r.inverted() @ Quaternion(ax, math.radians(hi)) @ r
        if hi >= 0.1:  # a chain the turns before it already lifted needs none
            turned[n] = round(hi, 2)
    bpy.context.view_layer.update()
    posed = skinned()
    bpy.ops.object.mode_set(mode="OBJECT")
    for o in objs:  # bake the pose into each mesh, then make it the rest pose
        mod = next(m for m in o.modifiers if m.type == "ARMATURE")
        select(o)
        bpy.ops.object.modifier_apply(modifier=mod.name)
        o.modifiers.new("Armature", "ARMATURE").object = arm
    select(arm)
    bpy.ops.object.mode_set(mode="POSE")
    bpy.ops.pose.armature_apply(selected=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    return {"shift_m": round(shift, 4), "turned_deg": turned,
            "feet_low_m": round(float(posed[on_leg, 2].min() - ground), 4),
            "min_z_m": round(float(posed[:, 2].min() - ground), 4)}


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
    alias = {}  # original name -> socket name, for bones renamed before a later socket parents to them

    def new_bone(name: str, parent: str, head, length: float, direction=(0, 0, 1), roll_to=None) -> None:
        b = eb.new(name)
        b.head = inv @ Vector(head)
        b.tail = inv @ (Vector(head) + Vector(direction).normalized() * length)
        if roll_to is not None:  # local +Z of the bone toward this world direction
            b.align_roll(inv.to_3x3() @ Vector(roll_to))
        b.parent = eb[alias.get(parent, parent)]
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
                alias[a["hand"]] = name
                found[name] = a["hand"]
                grip(a["side"], name, by_name[a["hand"]]["head"], by_name[a["hand"]]["tail"])
            else:
                fa = by_name[a["forearm"]]  # continue the forearm: a prop's +Y then runs out of the fist
                new_bone(name, a["forearm"], fa["tail"], 0.04 * height, Vector(fa["tail"]) - Vector(fa["head"]))
                grip(a["side"], name, arm.matrix_world @ eb[name].head, arm.matrix_world @ eb[name].tail)
    if "head" in want:
        h = rig_topology.find_head(bones, height, legs)
        if h:
            eb[alias.get(h, h)].name = "head"
            alias[h] = "head"
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
        "wingless": bool(job.get("wingless")),
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
SIGN_MIN = 0.2  # a head-end cue below this does not vote
# How the head end is named (docs/rig-anim.md "Facing check"). Each cue gives a sign along
# the mirror axis and a confidence 0..1; the head end is known when two cues agree (no
# cue >= SIGN_MIN against them), or one cue reaches SIGN_STRONG with nothing >= SIGN_VETO
# against it. Anything else is `unverified`: the angle is the axis alone (sign-blind) and the
# batch lists the monster for a look instead of passing it silently.
SIGN_STRONG = 0.8
SIGN_VETO = 0.5
# Cues per plan. high_centre_end: more surface in the top 40% near the centre plane at the
# head end (head and neck; wrong for heads-down boars, rhinos and badgers, which carry the
# rump higher). toes: per foot, the sole reaches farther toward the head than the leg above
# it (paws and hooves point forward, hocks bend back); not for birds (the hallux and the
# tail fan) or fins. abdomen_end: a spider's or beetle's high bulk is its abdomen, so the
# head is the other end. head_overhang (quadrupeds): the head, neck and trunk stick out past
# the forefeet, the rump sits over the hind feet and a tail has little surface; catches the
# heads-down and trunked bodies the high cue reads wrong (elephant, mammoth, polar bear).
# Not for dragons, whose tail is a second neck. Upright plans: the soles ahead of the ankles.
HEAD_CUES = {"quadruped": ("high_centre_end", "toes", "head_overhang"), "winged_quadruped": ("high_centre_end", "toes"),
             "avian": ("high_centre_end",), "aquatic": ("high_centre_end",),
             "insectoid": ("abdomen_end", "toes")}
# A single cue is not trusted alone for these: the reef shark's tall tail fin reads as a head.
SINGLE_CUE_UNTRUSTED = {"aquatic"}
# Plans with no head-end cue: a ghost has no feet (its robe train trails behind and reads as
# toes pointing backwards), and a coiled snake, blob, fungus, swarm or octopus has no end
# the surface can name. They report only how far the mirror axis is off.
NO_HEAD_CUE = {"floating", "serpentine", "amorphous", "plant_object", "swarm", "tentacled"}


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
    # head end, per plan: each cue votes (sign along fwd, confidence); combine_cues decides
    span = t.max() - t.min()
    cues = {}
    for name in HEAD_CUES.get(plan, ()):
        if name in ("high_centre_end", "abdomen_end"):
            # surface in the top 40% of the height, near the centre plane (wings are far
            # out), in the outer 30% of the length at each end
            mid = np.abs(side) < 0.15 * span
            high = cen[:, 2] > z0 + 0.6 * h
            fr = float(area[mid & high & (t > t.max() - 0.3 * span)].sum())
            bk = float(area[mid & high & (t < t.min() + 0.3 * span)].sum())
            sg = 1.0 if fr >= bk else -1.0
            cues[name] = (sg if name == "high_centre_end" else -sg, abs(fr - bk) / max(fr + bk, 1e-9))
            out["end_high_area"] = [round(bk, 4), round(fr, 4)]
        elif name == "toes":
            votes = toe_votes(cen, area, t, side, z0, h, span)
            out["toe_votes"] = [round(v, 4) for v in votes]
            if votes:
                med = float(np.median(votes))
                agree = float(np.mean(np.sign(votes) == np.sign(med)))
                # 0.02 x height of reach is a clear paw; one foot alone counts half
                cues[name] = (1.0 if med >= 0 else -1.0, min(1.0, abs(med) / 0.02) * agree * min(1.0, len(votes) / 2))
        elif name == "head_overhang":
            ov = head_overhang(cen, area, t, side, z0, h, span)
            if ov is not None:
                out["end_overhang"] = [round(ov[0], 4), round(ov[1], 4)]
                cues[name] = (1.0 if ov[1] >= ov[0] else -1.0, abs(ov[1] - ov[0]) / max(ov[0] + ov[1], 1e-9))
    if plan in UPRIGHT and plan not in NO_HEAD_CUE:
        # feet point forward: the sole (lowest 4%) reaches farther along the axis than the
        # ankles above it
        sole = cen[:, 2] < z0 + 0.04 * h
        shin = (cen[:, 2] > z0 + 0.08 * h) & (cen[:, 2] < z0 + 0.2 * h) & (np.abs(side) < 0.3 * h)
        if sole.sum() > 10 and shin.sum() > 10:
            d = float(np.average(t[sole], weights=area[sole]) - np.average(t[shin], weights=area[shin]))
            cues["toes_ahead_of_ankles"] = (1.0 if d >= 0 else -1.0, min(1.0, abs(d) / (0.04 * h)))
            out["toe_offset"] = round(d / h, 4)
    out["cues"] = {k: [int(v[0]), round(v[1], 3)] for k, v in cues.items()}
    sign, sign_conf, sign_rule, unverified = combine_cues(cues, plan)
    if job.get("front_view") and plan in UPRIGHT:
        # QA turned a front-view reference by 180 (no yaw override): the face is on +Y by
        # construction. Robe hems, cloaks, chains, tails and splayed toes fool the toe rule
        # (archmage, lich, cultist, chain-devil: all faced front), so the toe rule is only
        # recorded here and the angle is the mirror axis alone, as for a ghost.
        out["front_view_prior"] = True
        if sign < 0 and sign_conf >= SIGN_MIN:
            out["cue_disagrees"] = f"{sign_rule} says backwards ({sign_conf:.2f}); kept the front-view prior"
        sign, sign_conf, sign_rule, unverified = 1.0, 1.0, "front_view", None
    elif plan in NO_HEAD_CUE:
        unverified = None  # nothing to verify: the check is the axis alone by design
    fwd = fwd * sign * (-1 if job.get("forward") == "+Z" else 1)  # a +Z export faces -Y here
    ang = math.degrees(math.atan2(fwd[0], fwd[1]))
    axis_off = abs((ang + 90) % 180 - 90)  # ignoring which end is the head
    head_known = unverified is None and sign_rule != "none"
    # the yaw that would turn this estimate onto +Y: QA rotates by `yaw` about +Z, so
    # adding the measured bearing to QA's yaw lands the head (or the axis) on +Y
    turn = ang if head_known else (phi + 90) % 180 - 90
    out.update({"sign_rule": sign_rule, "sign_confidence": round(sign_conf, 3),
                "angle_deg": round(abs(ang) if head_known else axis_off, 1), "bearing_deg": round(turn, 1),
                "axis_off_deg": round(axis_off, 1), "head_known": head_known,
                "confidence": round(float(np.clip((contrast - 0.1) / 0.3, 0, 1)), 2)})
    if unverified:
        out["unverified"] = unverified
        if sign_conf >= SIGN_MIN and sign_rule != "none":  # the best guess, for the reviewer
            out["head_hint_bearing_deg"] = round(ang, 1)
    # the head region QA framed (model space, so it already sits on the +Y side): recorded,
    # but not a cue: QA frames the +Y end of horizontal plans, so it always points ahead
    hr = job.get("head_region")
    if hr and plan in HORIZONTAL:
        c = np.average(cen, axis=0, weights=area)
        v = np.array(hr["center"][:2]) - c[:2]
        out["head_region_bearing_deg"] = round(math.degrees(math.atan2(v[0], v[1])), 1)
    return out


def _components(xy, cell: float) -> np.ndarray:
    """Label per point: 8-connected components of the occupied grid cells."""
    ij = np.floor(xy / cell).astype(np.int64)
    keys, inv = np.unique(ij, axis=0, return_inverse=True)
    inv = np.asarray(inv).ravel()
    at = {(int(k[0]), int(k[1])): i for i, k in enumerate(keys)}
    parent = list(range(len(keys)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, k in enumerate(keys):
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                j = at.get((int(k[0]) + di, int(k[1]) + dj))
                if j is not None:
                    ra, rb = find(i), find(j)
                    if ra != rb:
                        parent[ra] = rb
    return np.array([find(i) for i in range(len(keys))])[inv]


def toe_votes(cen, area, t, side, z0: float, h: float, span: float) -> list:
    """Per foot: how far (fraction of height) the sole's centre sits ahead of the leg above
    it along t. Feet are the connected patches of the lowest 4% of the surface; patches
    longer than 0.3 x the body (a tail or a slab on the ground) or under 3% of the sole area
    are skipped, and the leg is the surface at 8-20% of the height above the patch."""
    sole = cen[:, 2] < z0 + 0.04 * h
    if sole.sum() < 10:
        return []
    si = np.flatnonzero(sole)
    lab = _components(cen[si, :2], 0.03 * span)
    shin = (cen[:, 2] > z0 + 0.08 * h) & (cen[:, 2] < z0 + 0.2 * h)
    tot = float(area[si].sum())
    votes = []
    for lb in np.unique(lab):
        ii = si[lab == lb]
        a = area[ii]
        if a.sum() < 0.03 * tot or np.ptp(t[ii]) > 0.3 * span:
            continue
        c = np.average(cen[ii, :2], axis=0, weights=a)
        r = max(0.08 * span, 0.6 * max(float(np.ptp(t[ii])), float(np.ptp(side[ii]))))
        leg = shin & (np.hypot(cen[:, 0] - c[0], cen[:, 1] - c[1]) < r)
        if leg.sum() < 5:
            continue
        votes.append(float(np.average(t[ii], weights=a) - np.average(t[leg], weights=area[leg])) / h)
    return votes


def head_overhang(cen, area, t, side, z0: float, h: float, span: float):
    """(back, front) surface past the feet along t, each face weighted by how far past: above
    30% of the height (a tail on the ground is not body), within 0.25 x the length of the
    centre plane. The feet are the 8th and 92nd area percentiles of the lowest 4%."""
    sole = cen[:, 2] < z0 + 0.04 * h
    if sole.sum() < 10:
        return None
    o = np.argsort(t[sole])
    ts, c = t[sole][o], np.cumsum(area[sole][o])
    bk_t, fr_t = ts[np.searchsorted(c, 0.08 * c[-1])], ts[np.searchsorted(c, 0.92 * c[-1])]
    up = (cen[:, 2] > z0 + 0.3 * h) & (np.abs(side) < 0.25 * span)
    fr, bk = up & (t > fr_t), up & (t < bk_t)
    return float((area[bk] * (bk_t - t[bk])).sum()), float((area[fr] * (t[fr] - fr_t)).sum())


def combine_cues(cues: dict, plan: str):
    """(sign, confidence, rule, unverified reason or None) from {cue: (sign, confidence)}."""
    if not cues:
        return 1.0, 0.0, "none", (f"no head-end cue for {plan}" if plan not in NO_HEAD_CUE else None)
    ranked = sorted(cues.items(), key=lambda kv: -kv[1][1])
    best, (sg, cf) = ranked[0]
    voting = [(k, v) for k, v in ranked if v[1] >= SIGN_MIN]
    agree = [k for k, v in voting if v[0] == sg]
    against = [(k, v[1]) for k, v in voting if v[0] != sg]
    if cf < SIGN_MIN:
        return sg, cf, best, "head-end cues too weak (" + ", ".join(f"{k} {v[1]:.2f}" for k, v in ranked) + ")"
    if len(agree) >= 2 and not against:
        return sg, cf, "+".join(agree), None
    if cf >= SIGN_STRONG and all(c < SIGN_VETO for _, c in against) and plan not in SINGLE_CUE_UNTRUSTED:
        return sg, cf, best, None
    if against:
        return sg, cf, best, "head-end cues disagree (" + ", ".join(f"{k} {v[0]:+.0f} {v[1]:.2f}" for k, v in ranked) + ")"
    return sg, cf, best, f"one head-end cue only ({best} {cf:.2f})"


# --- fallback skeletons (mode "fallback", on model.glb, no SkinTokens) --------------------
# SkinTokens gives a snake a 7-13 joint star (a root with four stubs), which no slither can
# drive. These skeletons are generated from the surface instead, with weights that follow
# the body's own length rather than straight-line distance (touching coils of a snake are
# far apart along the body):
#
# - spine_chain (serpentine, aquatic): the two surface points farthest apart along the mesh
#   (geodesic, over the welded edge graph) are the snout and tail tips. Every vertex gets
#   u = its geodesic distance from one tip / the tip-to-tip distance; the centroid of each
#   band of u is a joint, so the chain runs down the middle of the tube however it coils.
#   The higher end (or the thicker one, when level) is the head. Bones spine_00 (tail) ..
#   spine_<n-2>, head; the root is the middle bone, the chain runs from it both ways.
#   Weights: linear in u between the neighbouring bone centres (2 bones per vertex).
# - vertical (amorphous, plant_object without limbs): a chain of z-slices' centroids from
#   the base up (spine_00 at the bottom is the root), weights linear in height.
# - root (swarm): one bone from the base centre up, every vertex weight 1.
# - tentacle_hub (tentacled): body chain + one chain per tentacle tip (fallback_tentacles).

def _graph():
    """Welded vertex graph of all meshes: world positions (unique), edges, per original
    vertex its unique id, and the mesh object."""
    me = max((o for o in bpy.data.objects if o.type == "MESH"), key=lambda o: len(o.data.vertices))
    co = np.empty(len(me.data.vertices) * 3)
    me.data.vertices.foreach_get("co", co)
    mw = np.array(me.matrix_world)
    co = co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]
    key = np.round(co / 1e-5).astype(np.int64)
    uniq, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    inv = inv.reshape(-1)
    P = co[first]
    ed = np.empty(len(me.data.edges) * 2, dtype=np.int64)
    me.data.edges.foreach_get("vertices", ed)
    ed = inv[ed.reshape(-1, 2)]
    ed = ed[ed[:, 0] != ed[:, 1]]
    ed = np.unique(np.sort(ed, axis=1), axis=0)
    adj = [[] for _ in range(len(P))]
    ln = np.linalg.norm(P[ed[:, 0]] - P[ed[:, 1]], axis=1)
    for (a, b), l in zip(ed.tolist(), ln.tolist()):
        adj[a].append((b, l))
        adj[b].append((a, l))
    return me, P, adj, inv


def _dijkstra(adj, srcs, init=None):
    import heapq
    n = len(adj)
    dist = np.full(n, np.inf)
    pred = np.full(n, -1, dtype=np.int64)
    h = []
    for i, s in enumerate(srcs):
        d0 = init[i] if init is not None else 0.0
        if d0 < dist[s]:
            dist[s] = d0
            heapq.heappush(h, (d0, s))
    while h:
        d, a = heapq.heappop(h)
        if d > dist[a]:
            continue
        for b, l in adj[a]:
            nd = d + l
            if nd < dist[b]:
                dist[b] = nd
                pred[b] = a
                heapq.heappush(h, (nd, b))
    return dist, pred


def _fill(P, val, ok):
    """Copy val to the vertices outside ok (loose parts) from the nearest vertex in ok."""
    if ok.all():
        return val
    kd = KDTree(int(ok.sum()))
    idx = np.nonzero(ok)[0]
    for j, i in enumerate(idx):
        kd.insert(P[i], j)
    kd.balance()
    out = val.copy()
    for i in np.nonzero(~ok)[0]:
        out[i] = val[idx[kd.find(P[i])[1]]]
    return out


def _main_component(adj):
    d, _ = _dijkstra(adj, [0])
    best = np.isfinite(d)
    seen = best.copy()
    while not seen.all():
        s = int(np.nonzero(~seen)[0][0])
        d, _ = _dijkstra(adj, [s])
        comp = np.isfinite(d)
        seen |= comp
        if comp.sum() > best.sum():
            best = comp
    return best


def _build(me, joints: list, parents: list, names: list, weights: dict) -> None:
    """Armature from joint pairs [(head, tail)], parents (index or None), names; vertex
    groups from weights {bone index: per-original-vertex array}."""
    ad = bpy.data.armatures.new("rig")
    ao = bpy.data.objects.new("rig", ad)
    bpy.context.scene.collection.objects.link(ao)
    bpy.context.view_layer.objects.active = ao
    bpy.ops.object.mode_set(mode="EDIT")
    ebs = []
    for (h, t), nm in zip(joints, names):
        b = ad.edit_bones.new(nm)
        b.head, b.tail = Vector(h), Vector(t)
        ebs.append(b)
    for b, p in zip(ebs, parents):
        if p is not None:
            b.parent = ebs[p]
            b.use_connect = False
    bpy.ops.object.mode_set(mode="OBJECT")
    mw = me.matrix_world.copy()
    me.parent = ao
    me.matrix_world = mw
    for i, nm in enumerate(names):
        vg = me.vertex_groups.new(name=nm)
        w = weights.get(i)
        if w is None:
            continue
        for vi in np.nonzero(w > 1e-4)[0].tolist():
            vg.add([vi], float(w[vi]), "REPLACE")
    me.modifiers.new("Armature", "ARMATURE").object = ao
    bpy.ops.object.select_all(action="DESELECT")
    me.select_set(True)
    ao.select_set(True)
    bpy.ops.export_scene.gltf(filepath=job["dst"], export_format="GLB", use_selection=True, export_yup=True)


def _lin_weights(x: np.ndarray, nb: int) -> dict:
    """x in bone units (bone k spans [k, k+1]): linear between neighbouring bone centres."""
    c = np.clip(x - 0.5, 0, nb - 1)
    k0 = np.floor(c).astype(int)
    k0 = np.minimum(k0, nb - 1)
    fr = c - k0
    out = {k: np.zeros(len(x)) for k in range(nb)}
    for k in range(nb):
        out[k] += np.where(k0 == k, 1 - fr, 0)
        out[k] += np.where((k0 + 1 == k), fr, 0)
    return out


def fallback_chain(nb: int) -> dict:
    me, P, adj, inv = _graph()
    main = _main_component(adj)
    start = int(np.nonzero(main)[0][0])
    d0, _ = _dijkstra(adj, [start])
    a = int(np.argmax(np.where(main, d0, -1)))
    da, _ = _dijkstra(adj, [a])
    b = int(np.argmax(np.where(main, da, -1)))
    L = float(da[b])
    u = _fill(P, np.where(main, da / L, 0.0), main)
    h = float(P[:, 2].max() - P[:, 2].min())
    ends = []
    for lo, hi in ((0, 0.15), (0.85, 1.0001)):
        m = (u >= lo) & (u < hi)
        c = P[m].mean(0)
        ends.append({"z": float(c[2]), "r": float(np.linalg.norm(P[m] - c, axis=1).mean())})
    # head: the clearly higher end, else the thicker one (u runs from tip a to tip b)
    dz = ends[1]["z"] - ends[0]["z"]
    head_b = dz > 0 if abs(dz) > 0.1 * h else ends[1]["r"] > ends[0]["r"]
    if not head_b:
        u = 1 - u
    pts = []
    for k in range(nb + 1):
        m = np.abs(u - k / nb) < 0.5 / nb
        pts.append(P[m].mean(0) if m.any() else P[np.argmin(np.abs(u - k / nb))])
    m = nb // 2
    joints, parents, names = [], [], []
    for k in range(nb):
        if k >= m:
            joints.append((pts[k], pts[k + 1]))
            parents.append(None if k == m else k - 1)
        else:
            joints.append((pts[k + 1], pts[k]))
            parents.append(k + 1)
        names.append("head" if k == nb - 1 else f"spine_{k:02d}")
    x = u * nb
    # touching coils: the surface is fused where they meet, so the geodesic band runs
    # across into the next coil. A vertex much farther from its band's bone than the tube
    # is thick takes the position along the chain of the bone nearest in space instead.
    segs = [(np.asarray(pts[k]), np.asarray(pts[k + 1])) for k in range(nb)]
    dist = np.stack([seg_dist(P, a0, b0) for a0, b0 in segs], axis=1)  # (verts, bones)
    own = np.clip(np.floor(x).astype(int), 0, nb - 1)
    d_own = dist[np.arange(len(P)), own]
    r = float(np.median(d_own))
    far = d_own > 2.5 * r
    near = np.argmin(dist, axis=1)
    for i in np.nonzero(far)[0]:
        k = int(near[i])
        a0, b0 = segs[k]
        ab = b0 - a0
        x[i] = k + float(np.clip((P[i] - a0) @ ab / max(float(ab @ ab), 1e-12), 0, 1))
    _build(me, joints, parents, names, _lin_weights(x[inv], nb))
    return {"bones": nb, "length_m": round(L, 3), "root": names[m], "head_end": "b" if head_b else "a",
            "ends": ends, "loose_verts": int((~main).sum()), "tube_radius_m": round(r, 3),
            "reassigned_verts": int(far.sum())}


def fallback_vertical(nb: int) -> dict:
    me, P, adj, inv = _graph()
    z0, z1 = float(P[:, 2].min()), float(P[:, 2].max())
    H = z1 - z0
    x = (P[:, 2] - z0) / H * nb
    # one straight vertical line through the upper body's centroid: slice centroids wander
    # with a spread-out base (a puddle) and would bend the chain sideways
    up = P[:, 2] > z0 + 0.3 * H
    q = P[up] if up.any() else P
    c = (q.min(0) + q.max(0)) / 2  # bbox centre: splash tips and drips skew a mean
    pts = [np.array((c[0], c[1], z0 + H * k / nb)) for k in range(nb + 1)]
    joints = [(pts[k], pts[k + 1]) for k in range(nb)]
    names = [f"spine_{k:02d}" for k in range(nb)]
    _build(me, joints, [None] + list(range(nb - 1)), names, _lin_weights(x[inv], nb))
    return {"bones": nb, "height_m": round(H, 3), "root": names[0]}


def fallback_root() -> dict:
    me, P, adj, inv = _graph()
    z0, z1 = float(P[:, 2].min()), float(P[:, 2].max())
    c = P.mean(0)
    base = np.array((c[0], c[1], z0))
    _build(me, [(base, base + np.array((0, 0, 0.5 * (z1 - z0))))], [None], ["root"],
           {0: np.ones(len(inv))})
    return {"bones": 1, "root": "root"}


def fallback_tentacles(job: dict) -> dict:
    """Body + one chain per tentacle. The hub is the surface point nearest the centroid of
    the upper half (the mantle or body); tentacle tips are the local maxima of the geodesic
    distance from it, kept when they reach past half the farthest; each tentacle's joints
    follow the shortest surface path tip -> hub, pulled to the middle of the tube (the
    centroid of nearby vertices). Vertices beyond the tentacles' base distance belong to the
    tentacle whose path is nearest along the surface, weighted along it; the rest to the body."""
    nb = int(job.get("bones", 5))
    me, P, adj, inv = _graph()
    main = _main_component(adj)
    z0, z1 = float(P[:, 2].min()), float(P[:, 2].max())
    H = z1 - z0
    up = main & (P[:, 2] > z0 + 0.5 * H)
    hub_c = P[up].mean(0) if up.any() else P[main].mean(0)
    idx = np.nonzero(main)[0]
    seed = int(idx[np.argmin(np.linalg.norm(P[idx] - hub_c, axis=1))])
    g, pred = _dijkstra(adj, [seed])
    g = np.where(main, g, 0.0)
    gmax = float(g.max())
    kd = KDTree(len(P))
    for i, p in enumerate(P):
        kd.insert(p, i)
    kd.balance()
    order = np.argsort(-g)
    tips = []
    sup = 0.18 * gmax
    for i in order:
        if g[i] < 0.5 * gmax or len(tips) >= int(job.get("max_tentacles", 10)):
            break
        if any(np.linalg.norm(P[i] - P[t]) < sup for t in tips):
            continue
        # a local maximum within its neighbourhood
        if all(g[j] <= g[i] + 1e-9 for _, j, _ in kd.find_range(P[i], 0.08 * gmax)):
            tips.append(int(i))
    g_base = float(job.get("base", 0.35)) * gmax
    rr = 0.07 * gmax
    tent = []
    for t in tips:
        path = [t]
        while pred[path[-1]] >= 0 and g[path[-1]] > g_base:
            path.append(int(pred[path[-1]]))
        path = path[::-1]  # base -> tip
        gp = g[path]
        pts = []
        for k in range(nb + 1):
            target = g_base + (g[t] - g_base) * k / nb
            pi = path[int(np.argmin(np.abs(gp - target)))]
            near = [j for _, j, _ in kd.find_range(P[pi], rr)]
            near = [j for j in near if abs(g[j] - g[pi]) < rr]
            pts.append(P[near].mean(0) if near else P[pi])
        tent.append({"tip": t, "path": path, "pts": pts, "g_tip": float(g[t])})
    # vertices -> nearest tentacle path along the surface (beyond the base)
    srcs, lab = [], {}
    for j, tt in enumerate(tent):
        for v in tt["path"]:
            if v not in lab:
                lab[v] = j
                srcs.append(v)
    owner = np.full(len(P), -1)
    if srcs:
        _, pr = _dijkstra(adj, srcs)
        for i in np.nonzero(main & (g > g_base))[0]:
            v = int(i)
            while v not in lab and pr[v] >= 0:
                v = int(pr[v])
            owner[i] = lab.get(v, -1)
    owner = _fill(P, owner.astype(float), main).astype(int)
    gg = _fill(P, g, main)
    base_c = np.array((hub_c[0], hub_c[1], z0 + 0.15 * H))
    top_c = np.array((hub_c[0], hub_c[1], z1))
    joints = [(base_c, hub_c), (hub_c, top_c)]
    parents, names = [None, 0], ["root", "body"]
    weights = {0: np.zeros(len(P)), 1: np.zeros(len(P))}
    body_x = np.clip((P[:, 2] - base_c[2]) / max(top_c[2] - base_c[2], 1e-6) * 2, 0, 2)
    bw = _lin_weights(body_x, 2)
    for j, tt in enumerate(tent):
        first = len(joints)
        for k in range(nb):
            joints.append((tt["pts"][k], tt["pts"][k + 1]))
            parents.append(0 if k == 0 else len(joints) - 2)
            names.append(f"tent_{j}_{k}")
        mine = owner == j
        x = np.clip((gg - g_base) / max(tt["g_tip"] - g_base, 1e-6) * nb, 0, nb)
        lw = _lin_weights(x, nb)
        blend = np.clip(x / 0.75, 0, 1)  # the base fades into the body
        for k in range(nb):
            weights[first + k] = np.where(mine, lw[k] * blend, 0.0)
    tw = sum((w for k, w in weights.items() if k >= 2), np.zeros(len(P)))
    weights[0] = bw[0] * (1 - tw)
    weights[1] = bw[1] * (1 - tw)
    _build(me, [(a, b) for a, b in joints], parents, names, {k: w[inv] for k, w in weights.items()})
    return {"bones": len(names), "tentacles": len(tent), "hub_geodesic_max_m": round(gmax, 3),
            "tip_reach": [round(tt["g_tip"] / gmax, 2) for tt in tent]}


def fallback() -> dict:
    kind = job["kind"]
    if kind == "spine_chain":
        rep = fallback_chain(int(job.get("bones", 16)))
    elif kind == "vertical":
        rep = fallback_vertical(int(job.get("bones", 4)))
    elif kind == "root":
        rep = fallback_root()
    elif kind == "tentacle_hub":
        rep = fallback_tentacles(job)
    else:
        raise SystemExit(f"unknown fallback kind {kind}")
    rep["kind"] = kind
    return rep


legs_mirrored = None
if job["mode"] in ("score", "finalize") and job.get("fix_legs") == "mirror":
    # the rig_fix_legs override: legs the roll left out, mirrored from the other side (before the
    # wing and tail chains, which look for the legs)
    legs_mirrored = blender_leg_mirror.add_mirrored_legs(arm, me, rig_topology, rig_reweight,
                                                         int(job.get("feet", 0)))
wings_added = None
if job["mode"] in ("score", "finalize") and job.get("add_wings"):
    # before stats(), so joints and empty bones count the inserted chains
    wings_added = blender_wing_weights.add_missing_wings(arm, me, rig_topology, rig_reweight,
                                                         int(job.get("feet", 0)), bool(job.get("upright")),
                                                         seam=bool(job.get("wing_seam")))
tail_added = None
if job["mode"] in ("score", "finalize") and job.get("tail_adopt"):
    # the override rig_tail_chain {"adopt": bone}: that SkinTokens chain is the tail (tail_00..)
    tail_added = blender_tail_chain.adopt_named(arm, job["tail_adopt"])
elif job["mode"] in ("score", "finalize") and job.get("add_tail"):
    # likewise a tail chain found on the mesh when find_tail has fewer than 4 bones
    tail_added = blender_tail_chain.add_missing_tail(arm, me, rig_topology, int(job.get("feet", 0)),
                                                     isolated=not job.get("tail_fused"))
if job["mode"] == "facing":
    report = facing()
elif job["mode"] == "fallback":
    report = fallback()
else:
    report = stats()
if legs_mirrored is not None:
    report["legs_mirrored"] = legs_mirrored
if wings_added is not None:
    report["wings_added"] = wings_added
if tail_added is not None:
    report["tail_added"] = tail_added
if job["mode"] == "score" and job.get("quality", True):
    report.update(quality())
if job["mode"] == "finalize":
    out = Path(job["out_dir"])
    if job.get("reweight"):  # the rig_reweight override (tools/rig_reweight.py), before the sockets
        report["reweight"] = [rig_reweight.reweight(arm, me, spec) for spec in job["reweight"]]
    if job.get("leg_seams"):  # the rig_leg_seams override: weights relaxed where fused legs meet
        report["leg_seams"] = rig_reweight.smooth_seams(arm, me, rig_topology, int(job.get("feet", 0)),
                                                        bool(job.get("upright")))
    report["sockets"] = add_sockets()
    set_albedo(job["base_color"])
    clamp_metallic(me.data.materials)  # same rule as blender_qa.py, for rigs built from older model.glb files
    lods = {k: transfer(path) for k, path in job["lods"].items()}  # weighted in the chosen rig's rest pose
    grounded = ground_feet(int(job.get("feet", 0)), [me, *lods.values()])
    if grounded:
        report["grounded"] = grounded
    select(me, arm)
    bpy.ops.export_scene.gltf(filepath=str(out / "model_rigged.glb"), export_format="GLB", use_selection=True,
                              export_yup=True)
    report["lods"] = {}
    for k, lod in lods.items():
        export_lod(lod, out / f"model_rigged_lod{k}.glb")
        report["lods"][k] = f"model_rigged_lod{k}.glb"
with open(job["report"], "w") as f:
    json.dump(report, f, indent=1)
print("_OK")

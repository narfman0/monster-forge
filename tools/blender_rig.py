"""Rig stage helpers in headless Blender (tools/rig.py).

    blender -b --factory-startup -P tools/blender_rig.py -- '<job json>'

mode "score": report joint and weight stats for one SkinTokens roll (job: src, report,
feet, sprawl). mode "finalize": from the chosen rig write the game files (job: src,
base_color, lods {k: path}, out_dir, report, hands, sockets):

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
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rig_topology  # noqa: E402

job = json.loads(sys.argv[sys.argv.index("--") + 1])
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=job["src"])
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


report = stats()
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

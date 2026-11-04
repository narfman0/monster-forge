"""Procedural in-place walk, baked to one armature-only GLB (tools/walk.py).

    blender -b --factory-startup -P tools/blender_walk.py -- '<job json>'

The legs come from the skeleton (rig_topology.find_legs). Each foot runs a planted cycle:
it slides back along the ground for the stance and lifts forward for the swing, driven by
an IK target and held level, so feet do not skate or tilt. Biped legs alternate,
quadrupeds use a four-beat walk, more legs an alternating ripple along each side. The hips
sit low enough that the knees stay bent, and bob with the steps; biped arms swing opposite
their leg and a tail sways. Forward is +Y in Blender (the -Z glTF forward). The clip is
in place: moving the character is up to the game. job: src, dst, report, feet, sprawl,
upright.
"""
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Quaternion, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rig_topology  # noqa: E402

FPS = 30
STANCE = 0.62       # fraction of the cycle a foot is on the ground
STRIDE = 0.6        # stride as a fraction of the leg's drop
LIFT = 0.16         # foot lift as a fraction of the leg's drop
CROUCH = 0.05       # hips lowered by this fraction of the leg's drop
BOB = 0.025
ARM_SWING = 16.0    # degrees
TAIL_SWAY = 4.0     # degrees per tail bone

job = json.loads(sys.argv[sys.argv.index("--") + 1])
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=job["src"])
scene = bpy.context.scene
arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
meshes = [o for o in bpy.data.objects if o.type == "MESH"]
height = max((o.matrix_world @ Vector(c)).z for o in meshes for c in o.bound_box)
for o in meshes:
    bpy.data.objects.remove(o)  # the clip only needs the skeleton

world = arm.matrix_world
bones = [{"name": b.name, "parent": b.parent.name if b.parent else None,
          "head": list((world @ b.head_local)[:]), "tail": list((world @ b.tail_local)[:])} for b in arm.data.bones]
by_name = {b["name"]: b for b in bones}
kids = rig_topology._children(bones)
legs = rig_topology.find_legs(bones, height, 8 if job["sprawl"] else job["feet"], job["sprawl"])
if len(legs) < job["feet"]:
    raise SystemExit(f"found {len(legs)} legs, need {job['feet']}")
drop = max(g["drop"] for g in legs)
seconds = max(0.7, min(2.0, math.sqrt(drop / 0.6)))
frames = round(FPS * seconds)
scene.render.fps = FPS
scene.frame_start, scene.frame_end = 1, frames + 1


def phases() -> None:
    for g in legs:
        g["x"], g["y"] = by_name[g["foot"]]["head"][:2]
    cx = sum(g["x"] for g in legs) / len(legs)
    left = sorted((g for g in legs if g["x"] < cx), key=lambda g: -g["y"])
    right = sorted((g for g in legs if g["x"] >= cx), key=lambda g: -g["y"])
    if len(legs) == 2:
        for g, p in zip(left + right, (0.0, 0.5)):
            g["phase"] = p
    elif len(legs) == 4 and len(left) == 2 and len(right) == 2:
        # lateral sequence walk: hind, fore on one side, then the other side
        left[1]["phase"], left[0]["phase"], right[1]["phase"], right[0]["phase"] = 0.0, 0.25, 0.5, 0.75
    else:
        for i, g in enumerate(left):
            g["phase"] = 0.5 * (i % 2)
        for i, g in enumerate(right):
            g["phase"] = 0.5 * ((i + 1) % 2)


def foot_offset(t: float) -> tuple[float, float]:
    """Cycle time 0-1 -> (forward, up), in strides: stance back along the ground, swing up
    and forward."""
    if t < STANCE:
        return 0.5 - t / STANCE, 0.0
    u = (t - STANCE) / (1.0 - STANCE)
    s = u * u * (3 - 2 * u)
    return s - 0.5, math.sin(math.pi * u)


def world_rot_keys(name: str, axis: str, angle) -> None:
    """Key a pose bone with a rotation about an armature axis, on top of its rest pose."""
    pb = arm.pose.bones[name]
    pb.rotation_mode = "QUATERNION"
    rest = pb.bone.matrix_local.to_quaternion()
    ax = Vector({"X": (1, 0, 0), "Y": (0, 1, 0), "Z": (0, 0, 1)}[axis])
    for f in range(1, frames + 2):
        q = Quaternion(ax, math.radians(angle((f - 1) / frames)))
        pb.rotation_quaternion = rest.inverted() @ q @ rest
        pb.keyframe_insert("rotation_quaternion", frame=f)


def add_arms() -> list[str]:
    if not job["upright"]:
        return []
    cx = rig_topology.centre_x(bones, legs)
    out = []
    for a in rig_topology.find_arms(bones, height, legs):
        leg = next((g for g in legs if (g["x"] > cx) == (a["side"] == "r")), None)
        p = leg["phase"] if leg else 0.0
        world_rot_keys(a["upper"], "X", lambda t, p=p: ARM_SWING * math.sin(2 * math.pi * (t - p + 0.19)))
        out.append(a["upper"])
    return out


def add_tail() -> list[str]:
    """Longest chain that leaves the hips backwards and is not a leg (quadrupeds only: on
    an upright plan that test matches an arm)."""
    if job["upright"]:
        return []
    leg_bones = {n for g in legs for n in g["chain"]}
    hip_y = sum(by_name[g["chain"][0]]["head"][1] for g in legs) / len(legs)
    best = []
    for b in bones:
        if b["name"] in leg_bones or not b["parent"]:
            continue
        chain = rig_topology.longest_chain(b["name"], kids)
        if len(chain) < 4 or any(n in leg_bones for n in chain):
            continue
        ends = (Vector(by_name[chain[0]]["head"]), Vector(by_name[chain[-1]]["head"]))
        if ends[1].y < hip_y - 0.05 * height and ends[1].z < ends[0].z + 0.2 * height and len(chain) > len(best):
            best = chain
    for i, name in enumerate(best):
        world_rot_keys(name, "Z", lambda t, i=i: TAIL_SWAY * math.sin(2 * math.pi * (t - 0.09 * i)))
    return best


phases()
bpy.context.view_layer.objects.active = arm
bpy.ops.object.mode_set(mode="POSE")

for g in legs:
    shin = arm.pose.bones[g["shin"]]
    foot = arm.pose.bones[g["foot"]]
    rest = world @ arm.data.bones[g["shin"]].tail_local
    tgt = bpy.data.objects.new(f"ik_{g['foot']}", None)
    scene.collection.objects.link(tgt)
    ik = shin.constraints.new("IK")
    ik.target = tgt
    ik.chain_count = min(3, len(g["chain"]) - (1 if g["level"] else 0))
    if g["level"]:
        level = bpy.data.objects.new(f"level_{g['foot']}", None)
        scene.collection.objects.link(level)
        level.matrix_world = world @ arm.data.bones[g["foot"]].matrix_local
        foot.constraints.new("COPY_ROTATION").target = level
    stride = STRIDE * drop
    lift = LIFT * drop
    for f in range(1, frames + 2):
        t = ((f - 1) / frames + g["phase"]) % 1.0
        fwd, up = foot_offset(t)
        tgt.location = rest + Vector((0, fwd * stride, up * lift))
        tgt.keyframe_insert("location", frame=f)

root = next(b for b in arm.pose.bones if b.parent is None)
to_local = root.bone.matrix_local.to_3x3().inverted()
for f in range(1, frames + 2):
    t = (f - 1) / frames
    root.location = to_local @ Vector((0, 0, -drop * (CROUCH + BOB * (1 + math.cos(4 * math.pi * t)) / 2)))
    root.keyframe_insert("location", frame=f)
arms = add_arms()
tail = add_tail()

bpy.ops.nla.bake(frame_start=1, frame_end=frames + 1, step=1, only_selected=False, visual_keying=True,
                 clear_constraints=True, use_current_action=True, bake_types={"POSE"})
bpy.ops.object.mode_set(mode="OBJECT")
for o in list(bpy.data.objects):
    if o.type == "EMPTY":
        bpy.data.objects.remove(o)
bpy.ops.object.select_all(action="DESELECT")
arm.select_set(True)
bpy.ops.export_scene.gltf(filepath=job["dst"], export_format="GLB", use_selection=True, export_yup=True,
                          export_animations=True, export_animation_mode="ACTIONS", export_force_sampling=True)
with open(job["report"], "w") as f:
    json.dump({"legs": len(legs), "feet": [g["foot"] for g in legs], "phases": [g["phase"] for g in legs],
               "arms": arms, "tail": tail, "seconds": round(frames / FPS, 2), "frames": frames}, f, indent=1)

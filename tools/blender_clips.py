"""Procedural clips on a SkinTokens skeleton, one armature-only GLB per clip (tools/anim.py).

    blender -b --factory-startup -P tools/blender_clips.py -- '<job json>'

SkinTokens bones have no names, so nothing can be retargeted from a library: every clip is
built from the skeleton itself. rig_topology finds the legs, arms, head, tail and wings
geometrically; the neck is the head's ancestors above the front legs (or the arms), the
spine the rest of that path. Each clip is a handful of smooth curves of t (0-1 over the
clip) on those parts:

- The body is one rigid transform of the root bone about a pivot (crouch, lean, lunge,
  topple, roll over). IK targets hold each foot where it stands, position and rest
  orientation, so the legs bend to follow and the feet stay level. Keyed IK influence
  lets legs go limp (death, fly, a spider's striking leg). Arms that must reach a point
  (sword cut, bow draw) get two-bone IK with targets riding on the chest.
- Everything else is FK: rotations about an armature axis layered on the bone's rest
  pose. Forward is +Y (glTF -Z), up +Z, the creature's right +X. +X turns a nose up, leans
  an upright body back and swings a hanging arm forward; +Y rolls the right side down;
  +Z turns the head to its left.
- After keying, the skinned mesh is evaluated on every frame and corrects the body
  height (settled over a few passes): walk, idle, attack and hit raise the body where a
  loose part (tail feathers, a feather tip) would go below the ground and lower it where
  a standing foot cannot reach its target, a tail lying on the ground is turned up at its
  root instead, death lowers the body until the lying pose touches the ground, fly holds
  the whole cycle a set height above it. That is what keeps the walk's crouch from
  pushing tails through the floor. A walk or lunge that still dips more than DIP_MAX
  (a part skinned to a leg, like the owlbear's tail fan on a hind shin) is rebuilt with
  shorter steps.
- One bake per clip (visual keying, constraints cleared), the Action named after the
  clip, exported with the armature only; then the pose is reset for the next clip.

Looping clips use whole periods of t, so the first and last frames are the same pose.
job: src, out_dir, report, clips, feet, sprawl, upright, plan, optional wings (chains).
"""
import json
import math
import sys
import time
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rig_topology  # noqa: E402

FPS = 30
AXES = {"X": Vector((1, 0, 0)), "Y": Vector((0, 1, 0)), "Z": Vector((0, 0, 1))}
# walk
STANCE = 0.62       # fraction of the cycle a foot is on the ground
STRIDE = 0.6        # stride as a fraction of the leg's drop
LIFT = 0.16         # foot lift as a fraction of the leg's drop
CROUCH = 0.05       # hips lowered by this fraction of the leg's drop
BOB = 0.025
ARM_SWING = 16.0    # degrees
TAIL_SWAY = 4.0     # degrees per tail bone
# ground
FLOOR_TOL = 0.008   # metres a loose part may sit below the ground before the body is raised
FOOT_TOL = 0.004    # metres a standing leg may fall short of its IK target before the body is lowered
DIP_MAX = 0.03      # walk: deepest any vertex may go below the ground before the steps shorten
REACH_MAX = 0.015    # ... or a standing foot falls short of its target by more than this x height
HOVER = 0.32        # fly: lowest vertex over the cycle, metres above the ground

job = json.loads(sys.argv[sys.argv.index("--") + 1])
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=job["src"])
scene = bpy.context.scene
scene.render.fps = FPS
arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
meshes = [o for o in bpy.data.objects if o.type == "MESH" and o.vertex_groups]  # not the bone-shape sphere
world = arm.matrix_world.copy()
to_arm = world.inverted()
z_all = [(o.matrix_world @ v.co).z for o in meshes for v in o.data.vertices]
height = max(z_all) - min(z_all)


# --- skeleton -------------------------------------------------------------------------

bones = [{"name": b.name, "parent": b.parent.name if b.parent else None,
          "head": list((world @ b.head_local)[:]), "tail": list((world @ b.tail_local)[:])} for b in arm.data.bones]
by_name = {b["name"]: b for b in bones}
kids = rig_topology._children(bones)
legs = rig_topology.find_legs(bones, height, 8 if job["sprawl"] else job["feet"], job["sprawl"])
if len(legs) < job["feet"]:
    raise SystemExit(f"found {len(legs)} legs, need {job['feet']}")
drop = max(g["drop"] for g in legs)
cx = rig_topology.centre_x(bones, legs)
for g in legs:
    g["x"], g["y"] = by_name[g["foot"]]["head"][:2]
    g["side"] = "r" if g["x"] >= cx else "l"
    # IK ends at the foot itself, position and orientation, rather than at the shin's tail:
    # SkinTokens bones need not touch, and a foot hung 0.7 m off the shin's tail (the
    # dragon) swings up and down as the shin turns. A plantigrade "shin" that lies on the
    # ground (owlbear) is held level the same way, the foot below it follows; a spider's
    # tip-only leg drives its last bone's tip.
    g["ik_bone"] = g["foot"]
    if g["level"] and len(g["chain"]) >= 4 and g["chain"][-2] == g["shin"] \
            and by_name[g["shin"]]["head"][2] < 0.12 * height and by_name[g["shin"]]["tail"][2] < 0.04 * height:
        g["ik_bone"] = g["shin"]
ys = [p[1] for b in bones for p in (b["head"], b["tail"])]
body_len = max(ys) - min(ys)
leg_y = sum(g["y"] for g in legs) / len(legs)
front = [g for g in legs if g["y"] >= leg_y] if not job["upright"] else []
# body turns pivot at the ground under the feet (upright) or at the hind hips (horizontal),
# so a nose-up lean lifts the front instead of driving the rump and tail into the ground
hind = [g for g in legs if g["y"] < leg_y] if not job["upright"] else []
PIVOT = Vector((cx, sum(by_name[g["chain"][0]]["head"][1] for g in hind) / len(hind),
                sum(by_name[g["chain"][0]]["head"][2] for g in hind) / len(hind))) if hind else Vector((cx, leg_y, 0.0))
arms = rig_topology.find_arms(bones, height, legs) if job["upright"] else []
arm_by_side = {a["side"]: a for a in arms}
# upright: the highest bone on the centre line; horizontal: the one reaching farthest forward
head = rig_topology.find_head(bones, height, legs) if job["upright"] else \
    None if job["sprawl"] else rig_topology.find_snout(bones, height, legs)
tail = [] if job["upright"] else rig_topology.find_tail(bones, height, legs)
wings = [{"side": w[0], "chain": w[1]} for w in job["wings"]] if job.get("wings") else \
    rig_topology.find_wings(bones, height, legs, arms, exclude=(head,) if head else ())
root = next(b["name"] for b in bones if b["parent"] is None)
# a spider's chelicerae: short non-leg chains off the root that hang down
fangs = [n for n in kids[root] if job["sprawl"] and n not in {x for g in legs for x in g["chain"]}
         and by_name[rig_topology.longest_chain(n, kids)[-1]]["tail"][2] < by_name[n]["head"][2] - 0.05 * height]


def neck_and_spine() -> tuple[list[str], list[str]]:
    """Split the root-to-head path where the front legs (or arms) branch off: below is the
    spine, above the neck and head."""
    if not head:
        return [], []
    path = rig_topology.path_to_root(head, bones)
    cut = -1
    for limb in [g["chain"][0] for g in front] + [a["root"] for a in arms]:
        up = rig_topology.path_to_root(limb, bones)  # where the limb joins the head's path
        joint = max((i for i, n in enumerate(path) if n in up), default=-1)
        cut = max(cut, joint)
    if cut < 0:
        cut = max(0, len(path) - 3)
    return path[cut + 1:] or [head], path[:cut + 1]


neck, spine = neck_and_spine()
torso = [n for n in spine if n != root]  # leans the upper body; the root is the body transform
# the skull: a leaf head is a jaw or snout tip, so turn its parent instead
skull = by_name[head]["parent"] if head and not kids[head] and by_name[head]["parent"] in neck[:-1] else head


def sole_masks():
    """Mask of sole vertices, all legs and per leg: weighted (90 %+) to the bone the IK
    holds and the bones below it, so they stand exactly where they rest. Everything else,
    even near the ankle, has to keep itself above the ground."""
    owner = {}
    for i, g in enumerate(legs):
        for n in [g["ik_bone"]] + rig_topology._descendants(g["ik_bone"], kids):
            owner[n] = i
    sole = []
    for o in meshes:
        names = [g.name for g in o.vertex_groups]
        for v in o.data.vertices:
            share = {}
            for g in v.groups:
                i = owner.get(names[g.group])
                if i is not None:
                    share[i] = share.get(i, 0.0) + g.weight
            total = sum(g.weight for g in v.groups) or 1.0
            best = max(share, key=share.get, default=None)
            sole.append(best if best is not None and share[best] >= 0.9 * total else -1)
    sole = np.array(sole)
    return sole >= 0, [sole == i for i in range(len(legs))]


def tail_mask() -> np.ndarray:
    names_tail = set(tail)
    out = []
    for o in meshes:
        names = [g.name for g in o.vertex_groups]
        for v in o.data.vertices:
            g = max(v.groups, key=lambda g: g.weight, default=None)
            out.append(g is not None and names[g.group] in names_tail)
    return np.array(out, dtype=bool)


is_foot, foot_of = sole_masks()
rest_foot = [float(np.array(z_all)[m].min()) if m.any() else 0.0 for m in foot_of]
is_tail = tail_mask()
# a tail that lies on the ground is lifted by turning its root, not by raising the body
if tail:
    _r, _t = Vector(by_name[tail[0]]["head"]), Vector(by_name[tail[-1]]["tail"])
    _d = Vector((_t.x - _r.x, _t.y - _r.y, 0))
    TAIL_AXIS = _d.normalized().cross(AXES["Z"]) if _d.length > 1e-6 else -AXES["X"]
    TAIL_REACH = max(_d.length, 0.1 * height)


# --- curves ---------------------------------------------------------------------------

def smooth(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return u * u * (3 - 2 * u)


def keys(*pts):
    """Piecewise curve through (t, value) points, eased in and out on each segment."""
    def f(t: float) -> float:
        if t <= pts[0][0]:
            return pts[0][1]
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            if t <= t1:
                return v0 + (v1 - v0) * smooth((t - t0) / (t1 - t0))
        return pts[-1][1]
    return f


def vkeys(*pts):
    """keys() for points (Vectors)."""
    xs = [keys(*[(t, v[i]) for t, v in pts]) for i in range(3)]
    return lambda t: Vector((xs[0](t), xs[1](t), xs[2](t)))


def wave(amp: float, cycles: int = 1, phase: float = 0.0):
    return lambda t: amp * math.sin(2 * math.pi * (cycles * t - phase))


def ease_in(u: float) -> float:
    """A fall: slow start, fast landing."""
    u = min(1.0, max(0.0, u))
    return u * u


def sym(side: str, angle: float) -> float:
    """Roll (Y) and yaw (Z) mirror between the sides: the same motion on the left is the
    opposite angle."""
    return angle if side == "r" else -angle


# --- clip -----------------------------------------------------------------------------

class Clip:
    def __init__(self, name: str, seconds: float, loop: bool):
        self.name, self.loop = name, loop
        self.frames = round(FPS * seconds)
        self.rot: dict[str, list] = {}
        self.lift = lambda t: Vector()              # body translation, metres
        self.tilt = lambda t: Quaternion()          # body rotation about the pivot
        self.pivot = PIVOT.copy()
        self.foot = lambda g, t: Vector()           # foot target offset from rest
        self.ik = lambda g, t: 1.0                  # leg IK influence
        self.hands: dict[str, object] = {}         # side -> fn(t) -> world wrist position
        self.ground = "floor"                       # floor | rest | hover | none
        self.rest_from = 0.85                       # rest: from here on the pose lies on the ground
        self.info: dict = {}
        self.targets: list = []                     # (leg, IK empty, rest position)

    def turn(self, bone, axis: str, fn) -> None:
        if bone:
            self.rot.setdefault(bone, []).append((AXES[axis], fn))

    def turn_chain(self, chain, axis: str, fn, lag: float = 0.0) -> None:
        """Spread a rotation over a chain; `lag` delays each bone (in t), a travelling wave."""
        for i, n in enumerate(chain):
            self.turn(n, axis, (lambda t, i=i: fn(t - lag * i) / len(chain)) if lag else
                      (lambda t: fn(t) / len(chain)))


def body_matrix(clip: Clip, t: float, dz: float = 0.0) -> Matrix:
    p = clip.pivot
    return Matrix.Translation(clip.lift(t) + Vector((0, 0, dz))) @ Matrix.Translation(p) @ \
        clip.tilt(t).to_matrix().to_4x4() @ Matrix.Translation(-p)


def key_root(clip: Clip, corr: list[float]) -> None:
    pb = arm.pose.bones[root]
    rest = pb.bone.matrix_local
    for f in range(1, clip.frames + 2):
        t = (f - 1) / clip.frames
        pb.matrix = to_arm @ body_matrix(clip, t, corr[f - 1]) @ world @ rest
        pb.keyframe_insert("location", frame=f)
        pb.keyframe_insert("rotation_quaternion", frame=f)


def setup_ik(clip: Clip) -> list:
    """IK targets on every leg (and on the wrists the clip drives); returns the empties."""
    made = []
    for g in legs:
        shin = arm.pose.bones[g["ik_bone"]]
        tgt = bpy.data.objects.new(f"ik_{g['foot']}", None)
        scene.collection.objects.link(tgt)
        made.append(tgt)
        ik = shin.constraints.new("IK")
        ik.name = "leg_ik"
        ik.target = tgt
        above = g["chain"].index(g["ik_bone"])
        if g["level"]:  # the target also carries the bone's rest orientation: the foot stays level
            ik.use_rotation = True
            tgt.rotation_mode = "QUATERNION"
            tgt.rotation_quaternion = (world @ arm.data.bones[g["ik_bone"]].matrix_local).to_quaternion()
            ik.chain_count = min(3, above) + 1  # the foot and up to 3 bones above it
        else:
            ik.chain_count = min(3, above + 1)
        clip.targets.append((g, tgt, effector(g, rest=True)))
        for f in range(1, clip.frames + 2):
            ik.influence = clip.ik(g, (f - 1) / clip.frames)
            ik.keyframe_insert("influence", frame=f)
    for side, fn in clip.hands.items():
        tgt, ik = arm_ik(side)
        made += [tgt, ik.pole_target]
        for f in range(1, clip.frames + 2):
            tgt.location = fn((f - 1) / clip.frames)
            tgt.keyframe_insert("location", frame=f)
    key_feet(clip, [[0.0] * (clip.frames + 1) for _ in legs])
    return made


def chest_child(name: str, chest: str, at: Vector):
    """An empty that rides on the chest bone: its location is given in the rest pose's
    world frame, so a hand path follows the torso as it twists and leans."""
    e = bpy.data.objects.new(name, None)
    scene.collection.objects.link(e)
    e.parent, e.parent_type, e.parent_bone = arm, "BONE", chest
    b = arm.data.bones[chest]
    e.matrix_parent_inverse = (world @ b.matrix_local @ Matrix.Translation((0, b.length, 0))).inverted()
    e.location = at
    return e


def arm_ik(side: str, pole_angle: float | None = None):
    """Two-bone IK on an arm (upper arm + forearm, the wrist is the forearm's tail) with
    a pole out and behind the elbow. The pole angle depends on the bones' roll, so it is
    found once per arm: the angle that leaves the elbow where it rests."""
    a = arm_by_side[side]
    chest = by_name[a["root"]]["parent"] or a["root"]
    sh, el, wr = (Vector(by_name[a[k]]["head"]) for k in ("upper", "forearm", "forearm"))
    wr = Vector(by_name[a["forearm"]]["tail"])
    out = (el - (sh + wr) / 2)
    out = out.normalized() if out.length > 1e-4 else Vector((1 if side == "r" else -1, -0.3, 0)).normalized()
    tgt = chest_child(f"hand_{side}", chest, wr)
    pole = chest_child(f"pole_{side}", chest, el + out * 0.3 * height + Vector((0, -0.1 * height, 0)))
    ik = arm.pose.bones[a["forearm"]].constraints.new("IK")
    ik.name = "arm_ik"
    ik.target, ik.pole_target, ik.chain_count = tgt, pole, 2
    if pole_angle is None:
        if side not in POLE:
            best = None
            for deg in range(-180, 180, 15):
                ik.pole_angle = math.radians(deg)
                bpy.context.view_layer.update()
                err = ((world @ arm.pose.bones[a["forearm"]].head) - el).length
                if best is None or err < best[0]:
                    best = (err, deg)
            POLE[side] = best[1]
        pole_angle = POLE[side]
    ik.pole_angle = math.radians(pole_angle)
    return tgt, ik


POLE: dict[str, float] = {}


def effector(g: dict, rest: bool = False) -> Vector:
    """Where the leg's IK end is, posed or at rest."""
    if rest:
        return world @ arm.data.bones[g["ik_bone"]].tail_local
    return world @ arm.pose.bones[g["ik_bone"]].tail


def key_feet(clip: Clip, fix: list[list[float]]) -> None:
    """Foot targets: rest + the clip's offset + a per-frame lift (a spider's tip, or a
    swinging foot whose shin drags something through the ground)."""
    for i, (g, tgt, rest) in enumerate(clip.targets):
        for f in range(1, clip.frames + 2):
            tgt.location = rest + clip.foot(g, (f - 1) / clip.frames) + Vector((0, 0, fix[i][f - 1]))
            tgt.keyframe_insert("location", frame=f)


def key_fk(clip: Clip) -> None:
    for name, layers in clip.rot.items():
        pb = arm.pose.bones[name]
        pb.rotation_mode = "QUATERNION"
        rest = (world @ pb.bone.matrix_local).to_quaternion()
        for f in range(1, clip.frames + 2):
            t = (f - 1) / clip.frames
            q = Quaternion()
            for ax, fn in layers:  # ax None: fn gives the quaternion itself
                q = (fn(t) if ax is None else Quaternion(ax, math.radians(fn(t)))) @ q
            pb.rotation_quaternion = rest.inverted() @ q @ rest
            pb.keyframe_insert("rotation_quaternion", frame=f)


def sample(clip: Clip) -> tuple[np.ndarray, np.ndarray, list[np.ndarray]]:
    """Per frame: lowest vertex, lowest non-foot vertex, and all vertex positions."""
    lows, loose, pos = [], [], []
    clip.foot_low, clip.tail_low, clip.reach = [], [], []
    for f in range(1, clip.frames + 2):
        scene.frame_set(f)
        dg = bpy.context.evaluated_depsgraph_get()
        cos = []
        for o in meshes:
            ev = o.evaluated_get(dg)
            m = ev.to_mesh()
            co = np.empty(len(m.vertices) * 3)
            m.vertices.foreach_get("co", co)
            co = co.reshape(-1, 3) @ np.array(ev.matrix_world.to_3x3()).T + np.array(ev.matrix_world.translation)
            ev.to_mesh_clear()
            cos.append(co)
        co = np.concatenate(cos)
        pos.append(co)
        lows.append(co[:, 2].min())
        body = ~is_foot & ~is_tail
        loose.append(co[body, 2].min() if body.any() else co[:, 2].min())
        clip.tail_low.append(float(co[is_tail, 2].min()) if is_tail.any() else 1.0)
        clip.foot_low.append([float(co[m, 2].min()) if m.any() else 0.0 for m in foot_of])
        # how far each IK end falls short of its target (0 when the leg reaches)
        if clip.targets:
            clip.reach.append([(effector(g) - tgt.matrix_world.translation).length
                               for g, tgt, _ in clip.targets])
    return np.array(lows), np.array(loose), pos


def planted(clip: Clip, f: int) -> list[int]:
    """Legs standing on frame f (0-based): IK fully on and the foot not lifted."""
    t = f / clip.frames
    return [i for i, g in enumerate(legs) if clip.ik(g, t) >= 1 and clip.foot(g, t).z == 0]


def correction(clip: Clip, lows: np.ndarray, loose: np.ndarray) -> list[float]:
    """Per-frame change of the body height. floor: raise it where a loose part is below
    the ground, else lower it where a standing foot cannot reach the ground (a lunge or a
    lean overstretches the legs)."""
    n = clip.frames + 1
    if clip.ground == "floor":
        out = []
        for f, z in enumerate(loose):
            gap = max((clip.reach[f][i] for i in planted(clip, f)), default=0.0)
            if gap > FOOT_TOL:  # standing feet first
                out.append(-0.9 * gap)
            elif z < -FLOOR_TOL:  # raise, with what the legs can spare
                out.append(-z - FLOOR_TOL)
            else:
                out.append(0.0)
        return out
    if clip.ground == "hover":
        return [HOVER - float(lows.min())] * n
    if clip.ground == "rest":
        out = []
        for f in range(n):
            w = smooth((f / clip.frames - clip.rest_from + 0.12) / 0.12)
            out.append(max(0.0, -lows[f]) * (1 - w) - lows[f] * w)
        return out
    return [0.0] * n


def clear_pose() -> None:
    for pb in arm.pose.bones:
        for c in list(pb.constraints):
            pb.constraints.remove(c)
        pb.location = (0, 0, 0)
        pb.rotation_mode = "QUATERNION"
        pb.rotation_quaternion = (1, 0, 0, 0)
        pb.scale = (1, 1, 1)
    if arm.animation_data:
        arm.animation_data.action = None
    for o in list(bpy.data.objects):
        if o.type == "EMPTY":
            bpy.data.objects.remove(o)
    for a in list(bpy.data.actions):
        bpy.data.actions.remove(a)


def build(clip: Clip) -> dict:
    """Key, ground, bake and export one clip; returns its report."""
    t0 = time.time()
    clear_pose()
    scene.frame_start, scene.frame_end = 1, clip.frames + 1
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="POSE")
    arm.animation_data_create()
    arm.animation_data.action = bpy.data.actions.new(clip.name)
    setup_ik(clip)
    key_fk(clip)
    zero = [0.0] * (clip.frames + 1)
    key_root(clip, zero)
    corr = zero
    fix = [[0.0] * (clip.frames + 1) for _ in legs]
    tail_up = [0.0] * (clip.frames + 1)
    if clip.ground != "none":
        if tail and clip.ground == "floor":
            clip.rot.setdefault(tail[0], []).append((TAIL_AXIS, lambda t: tail_up[round(t * clip.frames)]))
        sole = [min(z, 0.0) for z in rest_foot]
        for _ in range(4):  # body height, tail and feet move each other a little; settle
            lows, loose, _ = sample(clip)
            corr = [a + b for a, b in zip(corr, correction(clip, lows, loose))]
            key_root(clip, corr)
            sample(clip)
            if tail and clip.ground == "floor":
                for f, z in enumerate(clip.tail_low):
                    if z < 0:
                        tail_up[f] += math.degrees(math.asin(min(1.0, 1.2 * -z / TAIL_REACH)))
                key_fk(clip)
            for i, g in enumerate(legs):  # a spider's leg tip turns with IK: keep it out of the ground
                for f in range(clip.frames + 1):
                    if clip.ik(g, f / clip.frames) <= 0.5:
                        continue
                    if not g["level"]:
                        fix[i][f] = max(0.0, fix[i][f] + sole[i] - clip.foot_low[f][i])
            key_feet(clip, fix)
    sample(clip)
    reach, clip.targets = clip.reach, []  # the empties go with the bake
    bpy.ops.nla.bake(frame_start=1, frame_end=clip.frames + 1, step=1, only_selected=False, visual_keying=True,
                     clear_constraints=True, use_current_action=True, bake_types={"POSE"})
    bpy.ops.object.mode_set(mode="OBJECT")
    for o in list(bpy.data.objects):
        if o.type == "EMPTY":
            bpy.data.objects.remove(o)
    act = arm.animation_data.action
    act.name = clip.name
    for a in list(bpy.data.actions):
        if a != act:
            bpy.data.actions.remove(a)
    lows, loose, pos = sample(clip)
    rep = {"seconds": round(clip.frames / FPS, 2), "frames": clip.frames, "loop": clip.loop,
           "min_z": round(float(lows.min()), 4), "min_z_last": round(float(lows[-1]), 4),
           "body_raise_max": round(float(max(corr)), 4),
           "tail_raise_max_deg": round(max(tail_up), 2),
           "foot_raise_max": round(float(max(max(x) for x in fix)) if fix else 0.0, 4), **clip.info}
    # planted feet: the highest any standing foot floats (stance frames, IK fully on)
    gaps = [clip.foot_low[f][i] - rest_foot[i] for f in range(clip.frames + 1) for i in planted(clip, f)]
    if gaps and clip.ground != "hover":
        rep["planted_foot_gap"] = round(max(gaps), 4)  # highest a standing foot's lowest vertex floats
        rep["ik_reach_gap"] = round(max(reach[f][i] for f in range(clip.frames + 1) for i in planted(clip, f)), 4)
    if clip.loop:
        rep["loop_delta"] = round(float(np.linalg.norm(pos[0] - pos[-1], axis=1).max()), 5)
    bpy.ops.object.select_all(action="DESELECT")
    arm.select_set(True)
    out = Path(job["out_dir"])
    bpy.ops.export_scene.gltf(filepath=str(out / f"{clip.name}.glb"), export_format="GLB", use_selection=True,
                              export_yup=True, export_animations=True, export_animation_mode="ACTIONS",
                              export_anim_slide_to_zero=True, export_force_sampling=True)  # clip starts at t=0
    rep["build_seconds"] = round(time.time() - t0, 1)
    (out / f"{clip.name}.json").write_text(json.dumps(rep, indent=1))
    return rep


# --- clips ----------------------------------------------------------------------------

def walk(scale: float = 1.0) -> Clip:
    """Planted-feet in-place walk (the gait the stage started with). `scale` shortens
    the stride and step height."""
    c = Clip("walk", max(0.7, min(2.0, math.sqrt(drop / 0.6))), True)
    left = sorted((g for g in legs if g["side"] == "l"), key=lambda g: -g["y"])
    right = sorted((g for g in legs if g["side"] == "r"), key=lambda g: -g["y"])
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

    def offset(g, t):
        u = (t + g["phase"]) % 1.0
        if u < STANCE:
            fwd, up = 0.5 - u / STANCE, 0.0
        else:
            s = (u - STANCE) / (1.0 - STANCE)
            fwd, up = smooth(s) - 0.5, math.sin(math.pi * s)
        return Vector((0, fwd * STRIDE * scale * drop, up * LIFT * scale * drop))

    c.foot = offset
    c.lift = lambda t: Vector((0, 0, -drop * (CROUCH + BOB * (1 + math.cos(4 * math.pi * t)) / 2)))
    for a in arms:
        leg = next((g for g in legs if g["side"] == a["side"]), None)
        p = leg["phase"] if leg else 0.0
        c.turn(a["upper"], "X", lambda t, p=p: ARM_SWING * math.sin(2 * math.pi * (t - p + 0.19)))
    for i, n in enumerate(tail):
        c.turn(n, "Z", lambda t, i=i: TAIL_SWAY * math.sin(2 * math.pi * (t - 0.09 * i)))
    c.info = {"stride_scale": scale, "legs": len(legs), "feet": [g["foot"] for g in legs],
              "phases": [g["phase"] for g in legs],
              "arms": [a["upper"] for a in arms], "tail": tail}
    return c


def idle() -> Clip:
    """Breathing, a slow weight shift over the feet, head looking about, tail and wings
    drifting. One breath per loop, everything else whole periods too."""
    c = Clip("idle", 2.5, True)
    sway = (0.012 if job["upright"] else 0.006) * height
    c.lift = lambda t: Vector((sway * math.sin(2 * math.pi * t), 0,
                               -drop * (0.03 + 0.012 * (1 - math.cos(2 * math.pi * t)) / 2)))
    c.turn_chain(torso, "X", lambda t: 2.0 * math.sin(2 * math.pi * t))
    c.turn_chain(neck[:-1], "Z", wave(8.0, 1, 0.1))
    c.turn_chain(neck[:-1], "X", wave(4.0, 2, 0.2))
    c.turn(skull, "Z", wave(5.0, 1, 0.15))
    for a in arms:
        c.turn(a["upper"], "X", wave(3.0, 1, 0.3))
        c.turn(a["forearm"], "X", lambda t: 4.0 + 3.0 * math.sin(2 * math.pi * (t - 0.4)))
    for i, n in enumerate(tail):
        c.turn(n, "Z", lambda t, i=i: 5.0 * math.sin(2 * math.pi * (t - 0.08 * i)))
    for w in wings:
        c.turn(w["chain"][0], "Y", lambda t, s=w["side"]: sym(s, -3.0 * math.sin(2 * math.pi * t)))
    return c


def attack_biped() -> Clip:
    """A sword cut from the right hand (the weapon is a prop at hand_r): the wrist, held by
    arm IK riding on the chest, winds up high over the right shoulder (blade back), cuts
    down and across the front of the body as the torso turns into it, follows through low
    on the left and recovers. The left arm swings back for balance."""
    c = Clip("attack_melee", 1.0, False)
    a = arm_by_side.get("r") or arms[0]
    sx = 1 if a["side"] == "r" else -1
    sh = Vector(by_name[a["upper"]]["head"])
    wr = Vector(by_name[a["forearm"]]["tail"])
    L = (Vector(by_name[a["upper"]]["tail"]) - sh).length + (wr - Vector(by_name[a["forearm"]]["head"])).length
    p = lambda x, y, z: sh + Vector((sx * x * L, y * L, z * L))
    path = [(0, wr), (0.3, p(0.2, -0.2, 0.8)), (0.42, p(0.25, -0.3, 0.85)), (0.5, p(0.05, 0.55, 0.5)),
            (0.57, p(-0.35, 0.8, -0.05)), (0.68, p(-0.6, 0.45, -0.6)), (1.0, wr)]
    c.hands[a["side"]] = vkeys(*path)
    # wrist: a hanging hand holds the blade forward, so with the forearm raised it points
    # up; the cut bends the wrist so the blade comes down in line with the forearm
    c.turn(a["hand"], "X", keys((0, 0), (0.3, 15), (0.42, 20), (0.5, -30), (0.57, -80), (0.68, -70), (1.0, 0)))
    o = arm_by_side.get("l" if a["side"] == "r" else "r")
    if o:
        c.turn(o["upper"], "X", keys((0, 0), (0.3, 20), (0.57, -25), (1.0, 0)))
    c.turn_chain(torso, "Z", keys((0, 0), (0.3, -sx * 25), (0.42, -sx * 28), (0.57, sx * 15), (0.68, sx * 22),
                                  (1.0, 0)))
    c.turn_chain(torso, "X", keys((0, 0), (0.3, 6), (0.57, -12), (0.68, -14), (1.0, 0)))
    c.lift = lambda t: Vector((0, 0.04 * height * keys((0, 0), (0.3, -0.5), (0.57, 1), (1.0, 0))(t),
                               -drop * keys((0, 0.02), (0.3, 0.04), (0.57, 0.08), (1.0, 0.02))(t)))
    c.info = {"arm": a["chain"], "weapon_socket": "hand_r"}
    return c


def lunge(name: str, seconds: float, rear: float, strike: float, reach: float, scale: float = 1.0) -> Clip:
    """Head attack for horizontal bodies: draw back and rear the head, then lunge the body
    forward on planted feet while the neck strikes down and forward, hold, recover.
    `scale` shrinks the body's part of it (the neck keeps its full strike)."""
    c = Clip(name, seconds, False)
    y = keys((0, 0), (0.3, -0.35), (0.48, 1.0), (0.62, 1.0), (1.0, 0))
    c.lift = lambda t: Vector((0, scale * reach * body_len * y(t),
                               -scale * drop * keys((0, 0.02), (0.3, 0.06), (0.5, 0.05), (1, 0.02))(t)))
    pitch = keys((0, 0), (0.3, 4), (0.48, -5), (0.62, -5), (1, 0))
    c.tilt = lambda t: Quaternion(AXES["X"], math.radians(scale * pitch(t)))
    c.info = {"body_scale": scale}
    c.turn_chain(neck, "X", keys((0, 0), (0.3, rear), (0.48, strike), (0.62, strike), (1, 0)))
    c.turn(skull, "X", keys((0, 0), (0.3, 10), (0.48, -8), (0.55, 4), (0.62, -6), (1, 0)))
    for i, n in enumerate(tail):
        c.turn(n, "X", keys((0, 0), (0.3, -2), (0.5, 4), (1, 0)))
    return c


def attack_spider() -> Clip:
    """Front pair rears up and stabs down, the body tips forward for the bite."""
    c = Clip("attack_melee", 1.0, False)
    pair = sorted(legs, key=lambda g: -g["y"])[:2]
    c.ik = lambda g, t: 1.0 - keys((0, 0), (0.12, 1), (0.8, 1), (1, 0))(t) if g in pair else 1.0
    for g in pair:
        c.turn(g["chain"][0], "X", keys((0, 0), (0.32, 45), (0.42, 50), (0.55, 8), (0.7, 8), (1, 0)))
        if len(g["chain"]) > 1:
            c.turn(g["chain"][1], "X", keys((0, 0), (0.32, 20), (0.55, -10), (1, 0)))
    c.tilt = lambda t: Quaternion(AXES["X"], math.radians(keys((0, 0), (0.32, 6), (0.55, -4), (0.7, -4), (1, 0))(t)))
    c.lift = lambda t: Vector((0, 0.03 * body_len * keys((0, 0), (0.32, 0), (0.55, 1), (0.7, 1), (1, 0))(t), 0))
    c.pivot = Vector((cx, leg_y - 0.3 * body_len, 0))
    for n in fangs:  # open, then snap shut forward
        c.turn(n, "X", keys((0, 0), (0.32, -25), (0.5, -30), (0.6, 30), (0.75, 30), (1, 0)))
    return c


def hit() -> Clip:
    """Knocked back from the front: the body snaps back and up, the head jerks, recover."""
    c = Clip("hit", 0.5, False)
    k = keys((0, 0), (0.18, 1), (1, 0))
    c.lift = lambda t: Vector((0, -0.03 * (height if job["upright"] else body_len) * k(t), -0.03 * drop * k(t)))
    if job["upright"]:
        c.turn_chain(torso, "X", lambda t: 12 * k(t))
        for a in arms:
            c.turn(a["upper"], "Y", lambda t, s=a["side"]: sym(s, -18 * k(t)))
            c.turn(a["forearm"], "X", lambda t: 25 * k(t))
    else:
        c.tilt = lambda t: Quaternion(AXES["X"], math.radians(5 * k(t)))
    c.turn_chain(neck, "X", lambda t: 14 * k(t))
    c.turn(skull, "X", keys((0, 0), (0.22, 12), (1, 0)))
    for w in wings:
        c.turn(w["chain"][0], "Y", lambda t, s=w["side"]: sym(s, -12 * k(t)))
    for n in tail:
        c.turn(n, "X", lambda t: 4 * k(t))
    return c


def death() -> Clip:
    """Collapse and hold the lying pose. Upright: knees buckle, then topple backwards
    pivoting on the heels. Four legs: buckle, then roll onto the right side with the legs
    going limp and the tail turned back flat. A spider drops on its belly with its legs
    folded up and in over it."""
    c = Clip("death", 1.8, False)
    c.ground, c.rest_from = "rest", 0.8
    buckle = keys((0, 0), (0.3, 1))
    if job["sprawl"]:
        c.ik = lambda g, t: 1 - keys((0, 0), (0.15, 1))(t)
        for g in legs:
            fx, fy = g["x"] - cx, g["y"] - leg_y
            ax = Vector((fx, fy, 0)).normalized().cross(AXES["Z"])  # + lifts the leg, - folds it down and in
            # every joint folds the leg up and out, so the tips clear the ground and the belly lands
            c.rot.setdefault(g["chain"][0], []).append((ax, keys((0, 0), (0.25, 5), (0.75, 22), (0.85, 18), (1, 18))))
            for n in g["chain"][1:]:
                c.rot.setdefault(n, []).append((ax, keys((0, 0), (0.2, 5), (0.75, 16), (0.85, 13), (1, 13))))
        for n in fangs:  # tucked forward, so the body rests on its belly, not on them
            c.turn(n, "X", keys((0, 0), (0.6, 75), (1, 75)))
        c.lift = lambda t: Vector((0, 0, -drop * keys((0, 0), (0.25, 0.3), (0.7, 1.2), (1, 1.2))(t)))
        c.rest_from = 0.7
        return c
    c.ik = lambda g, t: 1 - keys((0, 0), (0.28, 0), (0.45, 1))(t)
    c.turn_chain(neck, "X", keys((0, 0), (0.3, -15), (0.75, 10), (0.85, 5), (1, 5)))
    if job["upright"]:
        heel = min(g["y"] for g in legs) - 0.03 * height
        c.pivot = Vector((cx, heel, 0))
        fall = lambda t: 90 * ease_in((t - 0.28) / 0.5) - 6 * math.sin(math.pi * smooth((t - 0.78) / 0.14))
        c.tilt = lambda t: Quaternion(AXES["X"], math.radians(fall(t)))
        c.lift = lambda t: Vector((0, 0, -0.2 * drop * buckle(t) * (1 - smooth((t - 0.3) / 0.4))))
        c.turn_chain(torso, "X", keys((0, 0), (0.3, -12), (0.6, 4), (1, 4)))
        for a in arms:
            c.turn(a["upper"], "Y", lambda t, s=a["side"]: sym(s, keys((0, 0), (0.3, -10), (0.75, -70), (1, -70))(t)))
            c.turn(a["upper"], "X", keys((0, 0), (0.3, 20), (0.75, 40), (1, 40)))
            c.turn(a["forearm"], "X", keys((0, 0), (0.3, 30), (0.8, 15), (1, 15)))
        return c
    down = 1  # onto the right side
    wide = max(abs(g["x"] - cx) for g in legs)
    c.pivot = Vector((cx + down * wide, leg_y, 0))
    roll = lambda t: down * (88 * ease_in((t - 0.25) / 0.5) - 5 * math.sin(math.pi * smooth((t - 0.75) / 0.15)))
    c.tilt = lambda t: Quaternion(AXES["Y"], math.radians(roll(t)))
    c.info = {"falls_on": "right"}
    c.lift = lambda t: Vector((0, 0, -0.25 * drop * buckle(t)))
    for g in legs:  # legs bend a little and go slack
        c.turn(g["chain"][0], "X", keys((0, 0), (0.6, 15 if g in front else -15), (1, 15 if g in front else -15)))
    if tail:  # the tail turns back against the roll, so it lies along the ground
        c.turn(tail[0], "Y", lambda t: -0.9 * roll(t))
    for w in wings:  # wings fold up over the back, away from the ground
        c.turn(w["chain"][0], "Y", lambda t, s=w["side"]: sym(s, keys((0, 0), (0.6, -45), (1, -45))(t)))
    return c


def fly() -> Clip:
    """Hover: wings beat (a wave down each chain), legs hang limp, the body rises on the
    downstroke and sinks on the upstroke, held off the ground by the hover lift."""
    c = Clip("fly", 1.2 if height < 3 else 1.6, True)
    c.ground = "hover"
    c.ik = lambda g, t: 0.0
    bob = 0.06 if height < 3 else 0.1
    c.lift = lambda t: Vector((0, 0, bob * math.sin(2 * math.pi * (t - 0.1))))
    lean = -12 if job["upright"] else 4
    c.tilt = lambda t: Quaternion(AXES["X"], math.radians(lean + 2 * math.sin(2 * math.pi * (t - 0.3))))
    c.pivot = Vector((cx, leg_y, 0.6 * height))
    for w in wings:
        s, ch = w["side"], w["chain"]
        c.turn(ch[0], "Y", lambda t, s=s: sym(s, -28 * math.cos(2 * math.pi * t)))
        c.turn(ch[0], "Z", lambda t, s=s: sym(s, -8 * math.sin(2 * math.pi * t)))
        for i, n in enumerate(ch[1:4], 1):
            c.turn(n, "Y", lambda t, s=s, i=i: sym(s, -10 * math.cos(2 * math.pi * (t - 0.08 * i))))
    for g in legs:
        if job["upright"]:
            c.turn(g["chain"][0], "X", lambda t: 15 + 3 * math.sin(2 * math.pi * t))
            c.turn(g["chain"][1], "X", lambda t: -35 + 3 * math.sin(2 * math.pi * (t - 0.1)))
        else:
            fore = g in front
            c.turn(g["chain"][0], "X", lambda t, fore=fore: (20 if fore else -25) + 2 * math.sin(2 * math.pi * t))
    for i, n in enumerate(tail):
        c.turn(n, "X", lambda t, i=i: 3 * math.sin(2 * math.pi * (t - 0.1 * i)))
    c.turn_chain(neck, "X", lambda t: -2 * math.sin(2 * math.pi * (t - 0.2)))
    c.info = {"wings": {w["side"]: w["chain"] for w in wings}, "hover_m": HOVER}
    return c


def breath() -> Clip:
    """Rear the head back, thrust it forward and down and hold it there ~1 s for the
    game's fire, then recover."""
    c = Clip("breath", 2.2, False)
    c.turn_chain(neck, "X", keys((0, 0), (0.22, 35), (0.32, -12), (0.82, -12), (1, 0)))
    c.turn(skull, "X", keys((0, 0), (0.22, 10), (0.32, -6), (0.82, -6), (1, 0)))
    c.turn(skull, "Z", lambda t: 3 * math.sin(2 * math.pi * 3 * smooth((t - 0.32) / 0.5)))
    c.tilt = lambda t: Quaternion(AXES["X"], math.radians(keys((0, 0), (0.22, 5), (0.32, -3), (0.82, -3), (1, 0))(t)))
    c.lift = lambda t: Vector((0, body_len * 0.04 * keys((0, 0), (0.22, -1), (0.32, 0.6), (0.82, 0.6), (1, 0))(t),
                               -0.04 * drop))
    for w in wings:
        flare = keys((0, 0), (0.22, -15), (0.32, 5), (0.82, 5), (1, 0))
        c.turn(w["chain"][0], "Y", lambda t, s=w["side"], flare=flare: sym(s, flare(t)))
    return c


def aim_arm(c: Clip, a: dict, upper, fore, w, up=None) -> None:
    """FK-aim an arm: `upper`/`fore` give the upper arm's and forearm's direction (rest
    world frame, so a torso turn carries them along) and `w` how far from rest (0-1), all
    functions of t. `up` (optional) is where the grip bone's +Y (or the hand's) should
    point: a bow's limbs. Each bone's layer undoes its parent's share, so the arm lands
    on the directions whatever its rest pose (the goblin's upper arm rests sideways)."""
    vec = lambda n: Vector(by_name[n]["tail"]) - Vector(by_name[n]["head"])
    ru, rf = vec(a["upper"]), vec(a["forearm"])
    hand = a["hand"]
    grip = next((k for k in kids.get(hand, []) if k.startswith("grip_")), hand) if hand else None
    rh = arm.data.bones[grip].matrix_local.to_3x3() @ Vector((0, 1, 0)) if grip else None
    rh = world.to_3x3() @ rh if rh is not None else None

    def solve(t):
        qu = ru.rotation_difference(upper(t))
        qf_world = (qu @ rf).rotation_difference(fore(t))
        qf = qu.inverted() @ qf_world @ qu
        qh = Quaternion()
        if up is not None and rh is not None:
            tot = qu @ qf
            qh = tot.inverted() @ (tot @ rh).rotation_difference(up(t)) @ tot
        k = w(t)
        return [Quaternion().slerp(q, k) for q in (qu, qf, qh)]

    c.rot.setdefault(a["upper"], []).append((None, lambda t: solve(t)[0]))
    c.rot.setdefault(a["forearm"], []).append((None, lambda t: solve(t)[1]))
    if up is not None and hand:
        c.rot.setdefault(hand, []).append((None, lambda t: solve(t)[2]))


def attack_ranged() -> Clip:
    """Bow at hand_l (limbs along grip_l's +Y): the torso turns the left shoulder toward
    the target, the left arm comes up straight in front at shoulder height with the bow
    upright, the right hand reaches to the bow to nock, draws back to the cheek (elbow out
    and back at shoulder height), holds, releases (the hand flies back past the ear), and
    both arms return to rest. FK-aimed (aim_arm): two-bone IK found odd single-joint
    solutions on the goblin's L-shaped rest arms."""
    c = Clip("attack_ranged", 1.6, False)
    l, r = arm_by_side["l"], arm_by_side["r"]
    twist = 20.0
    pre = Quaternion(AXES["Z"], math.radians(twist))  # chest frame: undo the turn so the bow points ahead
    d = lambda x, y, z: (pre @ Vector((x, y, z))).normalized()
    bow_u, bow_f = d(-0.15, 1, 0.45), d(0.05, 1, 0.3)
    raise_l = keys((0, 0), (0.25, 1), (0.85, 1), (1, 0))
    aim_arm(c, l, lambda t: bow_u, lambda t: bow_f, raise_l, up=lambda t: Vector((0, 0, 1)))
    # right arm: reach to the bow, draw to the cheek, hold, release back past the ear
    ru = vkeys((0, d(-0.25, 1, 0.15)), (0.25, d(-0.25, 1, 0.15)), (0.55, d(1, -0.35, 0.3)), (0.72, d(1, -0.35, 0.3)),
               (0.78, d(1, -0.6, 0.15)), (0.85, d(1, -0.6, 0.15)), (1, d(1, -0.6, 0.15)))
    rf = vkeys((0, d(-0.3, 1, 0.15)), (0.25, d(-0.3, 1, 0.15)), (0.55, d(-1, 0.6, 0.2)), (0.72, d(-1, 0.6, 0.2)),
               (0.78, d(0.2, -1, 0.3)), (0.85, d(0.2, -1, 0.3)), (1, d(0.2, -1, 0.3)))
    raise_r = keys((0, 0), (0.25, 1), (0.85, 1), (1, 0))
    aim_arm(c, r, lambda t: ru(t).normalized(), lambda t: rf(t).normalized(), raise_r)
    c.turn_chain(torso, "Z", keys((0, 0), (0.25, -twist), (0.85, -twist), (1, 0)))
    c.turn(skull, "Z", keys((0, 0), (0.25, 15), (0.85, 15), (1, 0)))  # look along the arrow
    c.info = {"bow_socket": "grip_l" if "grip_l" in by_name else "hand_l"}
    return c


def plan_clips() -> dict:
    """Clip name -> maker(scale); scale < 1 tones down the body motion (see the retry)."""
    sets = {"walk": walk, "idle": lambda s: idle(), "hit": lambda s: hit(), "death": lambda s: death()}
    if arms:
        sets["attack_melee"] = lambda s: attack_biped()
    elif job["sprawl"]:
        sets["attack_melee"] = lambda s: attack_spider()
    elif neck:
        big = height > 3
        sets["attack_melee"] = lambda s: lunge("attack_melee", 1.1 if big else 0.9, 30 if big else 25,
                                               -25 if big else -22, 0.08, s)
    if wings:
        sets["fly"] = lambda s: fly()
    if neck and not job["upright"] and wings:
        sets["breath"] = lambda s: breath()
    if "l" in arm_by_side and "r" in arm_by_side:
        sets["attack_ranged"] = lambda s: attack_ranged()
    return sets


makers = plan_clips()
report = {"height": round(height, 3), "drop": round(drop, 3), "root": root, "head": head, "neck": neck,
          "spine": spine, "tail": tail, "arms": {a["side"]: a["chain"] for a in arms},
          "wings": {w["side"]: w["chain"] for w in wings}, "legs": [g["chain"] for g in legs],
          "clips": {}, "skipped": []}
for name in job["clips"]:
    if name not in makers:
        report["skipped"].append(name)
        continue
    report["clips"][name] = build(makers[name](1.0))
    if name in ("walk", "attack_melee"):  # a part skinned to a leg (owlbear: tail fan on a hind
        for k in (0.8, 0.65, 0.5):  # shin) sweeping through the ground, or feet that cannot
            r = report["clips"][name]  # reach it with the body raised: smaller steps
            if r["min_z"] >= -DIP_MAX and r.get("ik_reach_gap", 0) <= REACH_MAX * height:
                break
            report["clips"][name] = build(makers[name](k))
    print(f"clip {name}: {report['clips'][name]}")
with open(job["report"], "w") as f:
    json.dump(report, f, indent=1)
print("_OK")

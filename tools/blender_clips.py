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
IDLE_SINK = 0.03    # the idle's body, lowered this fraction of the leg's drop on its first frame;
                    # the floor one-shots start there (at_idle_height)
ARM_SWING = 16.0    # degrees
TAIL_SWAY = 4.0     # degrees per tail bone
TAIL_IDLE_DEG = 5.0  # idle: most a tail bone sways (the last bone; less toward the root) ...
TAIL_IDLE_TIP = 0.05  # ... and the tail's far end moves at most this x body length (tail_idle_amps)
# ground. The metre values are the pilots' (a 1-6.3 m body): every one is multiplied by
# SCALE (below), which is 1 for a body inside SIZE_BAND and grows or shrinks in proportion
# to the body's size outside it, so a 0.3 m sprite and a 15 m roc get the same fractions of
# their size as the smallest and the largest pilot.
FLOOR_TOL = 0.008   # metres a loose part may sit below the ground before the body is raised
FOOT_TOL = 0.004    # metres a standing leg may fall short of its IK target before the body is lowered
DIP_MAX = 0.03      # walk: deepest any vertex may go below the ground before the steps shorten
REACH_MAX = 0.015    # ... or a standing foot falls short of its target by more than this x height
HOVER = 0.32        # fly: lowest vertex over the cycle, metres above the ground
FLY_BOB = (0.06, 0.1)  # fly: body bob, metres (height under / over 3 m)
SIZE_BAND = (0.95, 6.5)  # body size (max of height and length, m) the metre values are for: the pilots' range

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
_y_all = [(o.matrix_world @ v.co).y for o in meshes for v in o.data.vertices]
mesh_len = max(_y_all) - min(_y_all)  # front-to-back extent of the surface (a blob's bones are all vertical)
SIZE = max(height, mesh_len)
SCALE = SIZE / SIZE_BAND[0] if SIZE < SIZE_BAND[0] else SIZE / SIZE_BAND[1] if SIZE > SIZE_BAND[1] else 1.0
FLOOR_TOL, FOOT_TOL, DIP_MAX, HOVER = (v * SCALE for v in (FLOOR_TOL, FOOT_TOL, DIP_MAX, HOVER))
FLY_BOB = tuple(v * SCALE for v in FLY_BOB)
# A hanging tail's end may drag this far into the ground (the floor clips' lift lets it down
# to here, the death's drape never below it): DIP_MAX x 0.8, so it never reads as DIP_MAX
# itself (Godot's check WARNs at -0.0301 on the centaur's -0.030)
TAIL_DIP = 0.8 * DIP_MAX


# --- skeleton -------------------------------------------------------------------------

bones = [{"name": b.name, "parent": b.parent.name if b.parent else None,
          "head": list((world @ b.head_local)[:]), "tail": list((world @ b.tail_local)[:])} for b in arm.data.bones]
by_name = {b["name"]: b for b in bones}
kids = rig_topology._children(bones)
# Plans without walking legs (serpentine, aquatic, floating, amorphous, ...) skip the leg
# machinery: their clips are the "legless" builders below, on the chains find_body() finds.
PLAN = job.get("plan", "")
LEGLESS = {"serpentine", "aquatic", "floating", "amorphous", "plant_object", "tentacled", "swarm", "merfolk_naga"}
legless = PLAN in LEGLESS
if legless:
    legs = []
else:
    legs = rig_topology.find_legs(bones, height, 8 if job["sprawl"] else job["feet"], job["sprawl"])
    if len(legs) < job["feet"]:
        raise SystemExit(f"found {len(legs)} legs, need {job['feet']}")
drop = max(g["drop"] for g in legs) if legs else 0.3 * height
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
leg_y = sum(g["y"] for g in legs) / len(legs) if legs else sum(b["head"][1] for b in bones) / len(bones)
front = [g for g in legs if g["y"] >= leg_y] if not job["upright"] else []
# body turns pivot at the ground under the feet (upright) or at the hind hips (horizontal),
# so a nose-up lean lifts the front instead of driving the rump and tail into the ground
hind = [g for g in legs if g["y"] < leg_y] if not job["upright"] else []
PIVOT = Vector((cx, sum(by_name[g["chain"][0]]["head"][1] for g in hind) / len(hind),
                sum(by_name[g["chain"][0]]["head"][2] for g in hind) / len(hind))) if hind else Vector((cx, leg_y, 0.0))
arms = rig_topology.find_arms(bones, height, legs) if job["upright"] or job.get("arms") else []
arm_by_side = {a["side"]: a for a in arms}
# upright: the highest bone on the centre line; horizontal: the one reaching farthest forward
head = rig_topology.find_head(bones, height, legs) if job["upright"] or (legless and arms) or PLAN == "centauroid" else \
    None if job["sprawl"] or legless else rig_topology.find_snout(bones, height, legs)
tail = [] if job["upright"] or legless else rig_topology.find_tail(bones, height, legs)
# else a short tail fan that slopes down onto the ground (a bird's): the floor clips turn it
# up at its root (below) instead of raising the whole body off it
fan = not tail and not job["upright"] and not legless
if fan:
    tail = rig_topology.find_tail(bones, height, legs, ground=True)
# A tail hangs when its root drops more to its tip than it reaches out (the wolf's, the
# owlbear's and the centaur's chains inserted by the rig stage); a level or lying one (the
# dragon's) or a ground fan (the eagle's) does not. See "Hanging tails" in docs/rig-anim.md.
HANG = False
if tail and not fan:
    _a, _b = by_name[tail[0]]["head"], by_name[tail[-1]]["tail"]
    HANG = (_a[2] - _b[2]) > math.hypot(_b[0] - _a[0], _b[1] - _a[1])
wings = [{"side": w[0], "chain": w[1]} for w in job["wings"]] if job.get("wings") else [] if legless else \
    rig_topology.find_wings(bones, height, legs, arms, exclude=(head,) if head else ())
root = next(b["name"] for b in bones if b["parent"] is None)
# a spider's chelicerae: short non-leg chains off the root that hang down
fangs = [n for n in kids[root] if job["sprawl"] and n not in {x for g in legs for x in g["chain"]}
         and by_name[rig_topology.longest_chain(n, kids)[-1]]["tail"][2] < by_name[n]["head"][2] - 0.05 * height]


def neck_and_spine() -> tuple[list[str], list[str]]:
    """Split the root-to-head path where the front legs (or arms, or a bird's wings)
    branch off: below is the spine, above the neck and head."""
    if not head:
        return [], []
    path = rig_topology.path_to_root(head, bones)
    cut = -1
    # a bird's wings are its forelimbs: the neck starts above the bone they hang from, so
    # a head turn does not swing the wings with it
    wing_roots = [w["chain"][0] for w in wings] if PLAN == "avian" else []
    for limb in [g["chain"][0] for g in front] + [a["root"] for a in arms] + wing_roots:
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


def leg_masks() -> list:
    """Per leg: the vertices whose strongest bone is in the leg chain or below it."""
    out = [[] for _ in legs]
    sets = [set(g["chain"]) | set(rig_topology._descendants(g["chain"][-1], kids)) for g in legs]
    for o in meshes:
        names = [g.name for g in o.vertex_groups]
        for v in o.data.vertices:
            g = max(v.groups, key=lambda g: g.weight, default=None)
            nm = names[g.group] if g is not None else None
            for i, st in enumerate(sets):
                out[i].append(nm in st)
    return [np.array(m, dtype=bool) for m in out]


def body_height() -> float:
    """Top of the surface not owned by a wing chain: a dragon's raised wings make `height`
    twice its body's."""
    if not wings:
        return height
    wing = {n for w in wings for n in [w["chain"][0]] + rig_topology._descendants(w["chain"][0], kids)}
    zs = [(o.matrix_world @ v.co).z for o in meshes for v in o.data.vertices
          if not v.groups or o.vertex_groups[max(v.groups, key=lambda g: g.weight).group].name not in wing]
    return (max(zs) - min(z_all)) if zs else height


body_h = body_height()
is_foot, foot_of = sole_masks()
leg_of = leg_masks()
rest_foot = [float(np.array(z_all)[m].min()) if m.any() else 0.0 for m in foot_of]
is_tail = tail_mask()
REST_V = np.array([(o.matrix_world @ v.co)[:] for o in meshes for v in o.data.vertices])
# Ground contacts: vertices lying on the floor at rest (z <= FLOOR_TOL) that no leg owns and
# no tail turn lifts: the harpy's hip-feather tips, skinned to the pelvis. A floor clip may
# lower the body for its standing feet only as far as these stay above -CONTACT_DIP; below
# that the body is raised for them and the feet fall a little short (correction()).
GROUND = (REST_V[:, 2] <= FLOOR_TOL) & ~is_foot & ~is_tail & ~np.any(leg_of, axis=0) if legs else \
    np.zeros(len(REST_V), bool)
CONTACT_DIP = DIP_MAX / 3
# Mesh edges (all meshes, the vertex order of sample()) and their rest lengths: a clip's edges
# stretched past TEAR x rest on any IDLE_STEP-th frame are counted (Clip.torn)
_E, _o = [], 0
for _m in meshes:
    _e = np.empty(len(_m.data.edges) * 2, dtype=np.int64)
    _m.data.edges.foreach_get("vertices", _e)
    _E.append(_e.reshape(-1, 2) + _o)
    _o += len(_m.data.vertices)
EDGES = np.concatenate(_E) if _E else np.zeros((0, 2), np.int64)
EDGE_L0 = np.maximum(np.linalg.norm(REST_V[EDGES[:, 0]] - REST_V[EDGES[:, 1]], axis=1), 1e-6)
TEAR = 4.0
TEAR_MAX = 20       # walk: a stride whose clip tears more edges than this is retried shorter (below)


def torn_edges(pos: list) -> int:
    """Edges stretched past TEAR x their rest length on any IDLE_STEP-th frame."""
    bad = np.zeros(len(EDGES), bool)
    for co in pos[::IDLE_STEP]:
        bad |= np.linalg.norm(co[EDGES[:, 0]] - co[EDGES[:, 1]], axis=1) > TEAR * EDGE_L0
    return int(bad.sum())
# a tail that lies on the ground is lifted by turning its root, not by raising the body
if tail:
    _r, _t = Vector(by_name[tail[0]]["head"]), Vector(by_name[tail[-1]]["tail"])
    _d = Vector((_t.x - _r.x, _t.y - _r.y, 0))
    TAIL_AXIS = _d.normalized().cross(AXES["Z"]) if _d.length > 1e-6 else -AXES["X"]
    TAIL_REACH = max(_d.length, 0.1 * height)
    if HANG:
        # a hanging tail lifts backwards in its own plane, the one through its root -> tip line
        # and the body's long axis: (root -> tip) x -Y (about -X for a straight-down tail).
        # (root -> tip, horizontal) x Z turned the centaur's tail, whose tip hangs 0.18 m to the
        # side and 0.08 m behind, 30 degrees sideways. Its reach is its length along the chain
        # (the horizontal reach, 0.24 m of a 1.6 m tail, lifted it 40 degrees).
        _d3 = _t - _r
        TAIL_AXIS = _d3.cross(-AXES["Y"]).normalized()
        # side to side like a pendulum: about the line in that plane across the tail (Y for a
        # straight-down tail; Z, a twist for a hanging one, is the level tail's sway)
        TAIL_SWAY_AXIS = _d3.cross(TAIL_AXIS).normalized()
        TAIL_REACH = max(sum((Vector(by_name[n]["tail"]) - Vector(by_name[n]["head"])).length for n in tail),
                         0.1 * height)
        TAIL_SLANT = math.degrees(math.atan2(_d.length, max(1e-6, _r.z - _t.z)))  # off the vertical


def hang_lift(up: float, dz: float) -> float:
    """Degrees more to turn a hanging tail (turned `up` degrees now) about TAIL_AXIS so its
    far end rises dz: it hangs TAIL_SLANT off the vertical, so its end sits TAIL_REACH x
    cos(slant + up) below the root. A level tail's end rises by sin (asin above); a hanging
    one's by 1 - cos, so the asin step left the centaur's floor-touching tail 4 cm under.
    The end is let down to DIP_MAX into the ground (a tail drags), not lifted clear: the
    centaur's lower tail is fused to its left hind leg and every degree pulls the sheet
    off it (walk edges past 4x: tail end at -0.03 m 114, -0.025 m 237, clear of it 250)."""
    a0 = math.radians(TAIL_SLANT + up)
    c1 = max(-1.0, min(1.0, math.cos(a0) - dz / TAIL_REACH))
    return max(0.0, math.degrees(math.acos(c1) - a0))


def tail_sway(c, n: str, fn) -> None:
    """The tail's side-to-side sway on bone n: about Z for a level tail, about TAIL_SWAY_AXIS
    (a pendulum swing) for a hanging one."""
    if HANG:
        c.rot.setdefault(n, []).append((TAIL_SWAY_AXIS, fn))
    else:
        c.turn(n, "Z", fn)


def tail_curl(c, n: str, fn) -> None:
    """The tail's up-down turn on bone n (attack, hit, fly, cast): about X for a level tail
    (+ turns it down), about TAIL_AXIS for a hanging one (+ lifts it backwards, away from the
    hind legs; about X it swung forward into them)."""
    if HANG:
        c.rot.setdefault(n, []).append((TAIL_AXIS, fn))
    else:
        c.turn(n, "X", fn)


# A hanging tail in a fall goes limp (death_drape): as the body rolls onto its side each tail
# bone eases toward the ground, root first, and what reaches the floor lies along it. Rolled
# with the body it stuck out level beside the rump (centaur: 0.87-1.47 m up with the body top
# at 1.58; wolf 0.18-0.43 m), parallel to the legs.
DRAPE_BEND = 12.0   # degrees a draped bone turns at most off the line it would hold riding with its parent
DRAPE_ROOT = 0.0    # ... the root bone off the rump. More tears the tail's base on these rigs (edges past
                    # 4x in the death, root / joint limit: wolf 0/15: 3 (as before), 0/20: 5, 10/30: 42,
                    # 20/15: 34 with the tail on the floor; owlbear 0/12: 5 (as before), 0/15: 6; centaur
                    # 0/0..0/15: 39, 0/25: 42, 20/15: 49, 40/15: 60): the wolf's rump stub bone_27 shares
                    # the tail's base, the owlbear's fan tip meets its left hind foot, the centaur's sheet is
                    # fused to its left hind leg. So the tail drops part of the way (docs/rig-anim.md).
DRAPE_BACK = 0.6    # the fall leans back (away from the rump) this much per unit down, and the part on
                    # the ground lies back along the body's line and the way the tail pointed
DRAPE_EASE = 0.3    # the idle's first-frame tail lift eases out over the buckle (to t = 0.3)
DRAPE_SLOPE = 1 / math.sqrt(1 + DRAPE_BACK ** 2)  # steepest a draped bone points down (sine)


def drape_w(t: float) -> float:
    """How limp the tail is: 0 until the roll is under way (0.3), 1 when it lands (0.75)."""
    return smooth((t - 0.3) / 0.45)


def death_drape(c) -> None:
    """Lay a hanging tail on the ground in a fall (ground "rest"): per frame quaternions
    from drape_solve, blended in by drape_w; before that the tail keeps the floor clips' lift
    (tail_up on the root, about TAIL_AXIS), so frame 0 is the idle's. Each tail bone gets one
    layer (nothing else turns it in a death)."""
    c.drape = True
    n = c.frames + 1
    c.drape_q = [[Quaternion() for _ in range(n)] for _ in tail]
    c.drape_line = [0.02 * height] * n  # height of the bone line where it lies on the ground
    c.tail_up = [FIRST.get("tail_up0", 0.0) * (1 - smooth(f / c.frames / DRAPE_EASE)) for f in range(n)]  # drape_settle
    for i, b in enumerate(tail):
        def comp(t, i=i):
            f = round(t * c.frames)
            base = Quaternion(TAIL_AXIS, math.radians(c.tail_up[f])) if i == 0 else Quaternion()
            return base.slerp(c.drape_q[i][f], drape_w(t))
        c.rot[b] = [(None, comp)]


def drape_solve(c) -> None:
    """Per frame, the tail chain from its posed root: a bone with room below it points down
    and back (DRAPE_BACK), one that would pass the bone line's ground height (drape_line)
    points at it, the rest lie along the ground; each turned at most DRAPE_BEND off the
    direction it would have riding with its parent (the root: with the rump). Read from the
    posed parent (the body's roll, the ground correction), so call it after key_root."""
    parent = by_name[tail[0]]["parent"]
    prest = (world @ arm.data.bones[parent].matrix_local).to_quaternion()
    dirs = [Vector(by_name[b]["tail"]) - Vector(by_name[b]["head"]) for b in tail]
    offs = [Vector(by_name[tail[i]]["head"]) - Vector(by_name[tail[i - 1]]["head"]) for i in range(1, len(tail))]
    for f in range(c.frames + 1):
        scene.frame_set(f + 1)
        D = (world @ arm.pose.bones[parent].matrix).to_quaternion() @ prest.inverted()
        P = world @ arm.pose.bones[tail[0]].head
        line = c.drape_line[f]
        for i in range(len(tail)):
            if i:
                P = P + D @ offs[i - 1]
            ride = (D @ dirs[i]).normalized()
            h = Vector((ride.x, ride.y, 0.0))
            u = (h.normalized() if h.length > 1e-3 else Vector()) - AXES["Y"]
            u = u.normalized() if u.length > 1e-3 else -AXES["Y"]
            dz = max(-DRAPE_SLOPE, min(1.0, (line - P.z) / dirs[i].length))
            want = u * math.sqrt(max(0.0, 1 - dz * dz)) + AXES["Z"] * dz
            maxb = math.radians(DRAPE_ROOT if i == 0 else DRAPE_BEND)
            ang = ride.angle(want, 0.0)
            if ang > maxb:
                ax = ride.cross(want)
                if ax.length > 1e-6:
                    want = Quaternion(ax.normalized(), maxb) @ ride
            Dn = ride.rotation_difference(want) @ D
            c.drape_q[i][f] = D.inverted() @ Dn
            D = Dn


def drape_settle(c) -> None:
    """After a sample: limp frames raise the ground line where the tail dips past TAIL_DIP,
    and on the lying frames (drape_w 1) set it so the lowest tail vertex touches the ground.
    Before the roll the tail keeps the idle's first-frame lift (tail_up0), so a death starts
    on the idle's tail pose; a deeper dip in the buckle raises the body (correction), as
    before: lifting the tail further (48 degrees on the centaur) tore its sheet off the hind leg."""
    for f, z in enumerate(c.tail_low):
        w = drape_w(f / c.frames)
        c.tail_up[f] = FIRST.get("tail_up0", 0.0) * (1 - smooth(f / c.frames / DRAPE_EASE))
        if w >= 1:
            c.drape_line[f] = max(0.0, c.drape_line[f] - z)
        elif w > 0 and z < -TAIL_DIP:
            c.drape_line[f] += 1.2 * (-z - TAIL_DIP)
    drape_solve(c)


def seg_dist(a0, a1, b0, b1) -> float:
    """Closest distance between segments a0-a1 and b0-b1 (numpy 3-vectors)."""
    u, v, w0 = a1 - a0, b1 - b0, a0 - b0
    A, B, C, Dd, E = u @ u, u @ v, v @ v, u @ w0, v @ w0
    den = A * C - B * B
    sc = 0.0 if den < 1e-12 else min(1.0, max(0.0, (B * E - C * Dd) / den))
    tc = min(1.0, max(0.0, (B * sc + E) / C)) if C > 1e-12 else 0.0
    sc = min(1.0, max(0.0, (B * tc - Dd) / A)) if A > 1e-12 else 0.0
    return float(np.linalg.norm(w0 + sc * u - tc * v))


HIND_BONES = [b for g in hind for b in g["chain"][1:]]


def tail_leg_dist(posed: bool = True) -> float:
    """Closest distance between the tail line below its root bone and the hind legs' joint
    paths below the hip (knee -> hock -> toe: their bones), posed on the current frame or at
    rest. The root bone and the hip bones meet at the rump (wolf: 0.02 m apart at rest)."""
    if posed:
        seg = lambda b: (np.array((world @ arm.pose.bones[b].head)[:]), np.array((world @ arm.pose.bones[b].tail)[:]))
    else:
        seg = lambda b: (np.array(by_name[b]["head"]), np.array(by_name[b]["tail"]))
    ts, ls = [seg(b) for b in tail[1:] or tail], [seg(b) for b in HIND_BONES]
    return min((seg_dist(*a, *b) for a in ts for b in ls), default=1e9)


# Hanging tail vs the hind legs (wolf walk: the tail end folded into the hind hock at 75%,
# interpenetration, not stretch): the sway is scaled down per frame where the tail line comes
# closer to a hind leg's joint path than TAIL_CLEAR x their distance at rest.
TAIL_CLEAR = 0.9
TAIL_CLEAR_MIN = 0.2  # least share of the sway kept on any frame
TAIL_FUSED = 0.02   # a tail this close (x height) to a hind leg at rest is fused to it and sways with it
                    # (centaur: 0.008 m; its walk tore 88 edges past 4x with the clearance, 83 without)


def tail_clearance(c) -> None:
    """After a sample: where the posed tail line is within the clearance of a hind leg, cut
    that frame's sway share (sway_k) in proportion, smoothed over the loop (circular, so the
    first and last frames stay the same pose)."""
    clear = TAIL_CLEAR * TAIL_REST_CLEAR
    n = c.frames
    d = []
    for f in range(n):
        scene.frame_set(f + 1)
        d.append(tail_leg_dist())
    k = np.array(c.sway_k[:n])
    for f in range(n):
        if d[f] < clear:
            k[f] = max(TAIL_CLEAR_MIN, k[f] * (d[f] / clear) ** 2)
    win = max(1, n // 12)  # a moving minimum, then a moving mean: no frame-to-frame snap
    lo = np.array([k[[(f + j) % n for j in range(-win, win + 1)]].min() for f in range(n)])
    k = np.array([lo[[(f + j) % n for j in range(-win, win + 1)]].mean() for f in range(n)])
    c.sway_k = [float(x) for x in k] + [float(k[0])]


TAIL_REST_CLEAR = tail_leg_dist(False) if HANG and hind else 0.0


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
        self.hand_ik: dict[str, object] = {}       # side -> fn(t) -> that arm IK's influence (default 1)
        self.ground = "floor"                       # floor | rest | hover | none
        self.rest_from = 0.85                       # rest: from here on the pose lies on the ground
        self.info: dict = {}
        self.targets: list = []                     # (leg, IK empty, rest position)
        self.scale: dict[str, object] = {}          # bone -> fn(t) -> local scale Vector
        self.hover = HOVER                          # hover: lowest vertex this high, metres
        self.fit = None                             # {"set", "lo", "hi", "bone"}: see fit_angle()
        self.start_lift = 0.0                       # rest: body raise at t=0, eased out (a lifted death)
        self.drape = False                          # rest: a hanging tail goes limp onto the ground (drape_solve)
        self.sway_k = None                          # per frame: share of a hanging tail's sway (tail_clearance)

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
        k = clip.hand_ik.get(side, lambda t: 1.0)
        for f in range(1, clip.frames + 2):
            tgt.location = fn((f - 1) / clip.frames)
            tgt.keyframe_insert("location", frame=f)
            ik.influence = k((f - 1) / clip.frames)
            ik.keyframe_insert("influence", frame=f)
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
    for name, fn in clip.scale.items():
        pb = arm.pose.bones[name]
        for f in range(1, clip.frames + 2):
            pb.scale = fn((f - 1) / clip.frames)
            pb.keyframe_insert("scale", frame=f)


def sample(clip: Clip) -> tuple[np.ndarray, np.ndarray, list[np.ndarray]]:
    """Per frame: lowest vertex, lowest non-foot vertex, and all vertex positions."""
    lows, loose, pos = [], [], []
    clip.foot_low, clip.tail_low, clip.reach, clip.leg_low, clip.contact_low = [], [], [], [], []
    clip.notail_low = []
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
        clip.notail_low.append(float(co[~is_tail, 2].min()) if is_tail.any() else float(co[:, 2].min()))
        clip.tail_low.append(float(co[is_tail, 2].min()) if is_tail.any() else 1.0)
        clip.contact_low.append(float(co[GROUND, 2].min()) if GROUND.any() else 1.0)
        clip.foot_low.append([float(co[m, 2].min()) if m.any() else 0.0 for m in foot_of])
        clip.leg_low.append([float(co[m, 2].min()) if m.any() else 1.0 for m in leg_of])
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
    lean overstretches the legs), but never so far that a ground contact (GROUND) goes
    below -CONTACT_DIP: there the body is raised to it and the foot falls short."""
    n = clip.frames + 1
    if clip.ground == "floor":
        out = []
        for f, z in enumerate(loose):
            gap = max((clip.reach[f][i] for i in planted(clip, f)), default=0.0)
            least = -CONTACT_DIP - clip.contact_low[f]  # the lowest change the contacts allow
            if gap > FOOT_TOL:  # standing feet first, down to the contacts' limit
                out.append(max(-0.9 * gap, least))
            elif z < -FLOOR_TOL:  # raise, with what the legs can spare
                out.append(-z - FLOOR_TOL)
            else:
                out.append(0.0)
        return out
    if clip.ground == "hover":
        return [clip.hover - float(lows.min())] * n
    if clip.ground == "contact":  # legless bodies lying on the ground: lowest vertex on it every frame
        return [-float(z) for z in lows]
    if clip.ground == "rest":
        out = []
        stand = [max(0.0, -z - FLOOR_TOL) for z in lows]
        if clip.drape:  # a limp tail is laid on the ground by drape_solve: lying, the body does not
            # lift for it; standing, it may dip TAIL_DIP like the floor clips' (the idle's frame 0)
            # and while the legs stand (IK fully on) the feet are the IK's, as in the floor clips
            # (and lower the body where a standing foot falls short: the idle's frame 0 is
            # lowered so, the centaur's by 0.016 m)
            stand = []
            for f in range(n):
                up = clip.ik(legs[0], f / clip.frames) >= 1 if legs else False
                gap = max((clip.reach[f][i] for i in planted(clip, f)), default=0.0) if up else 0.0
                stand.append(-0.9 * gap if gap > FOOT_TOL else
                             max(0.0, -(loose[f] if up else clip.notail_low[f]) - FLOOR_TOL, -clip.tail_low[f] - TAIL_DIP))
            lows = np.array(clip.notail_low)
        for f in range(n):
            w = smooth((f / clip.frames - clip.rest_from + 0.12) / 0.12)
            # standing part: as the floor clips, a part may sit FLOOR_TOL below the ground
            # (else the first frame is raised off the idle's and the legs' IK finds other knees)
            out.append(stand[f] * (1 - w) - lows[f] * w)
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


def fit_angle(clip: Clip) -> None:
    """A rooted fall (ground "none"): bisect the clip's fall angle (`fit["set"]`, between
    `lo` and `hi` degrees) until the last frame's lowest vertex not owned by `fit["bone"]`
    (the planted base) touches the ground. The root does not move, so the base stays where
    it stands."""
    f = clip.fit
    mask = np.concatenate([np.array([max(v.groups, key=lambda g: g.weight).group != o.vertex_groups[f["bone"]].index
                                     if v.groups else True for v in o.data.vertices])
                           if f["bone"] in o.vertex_groups else np.ones(len(o.data.vertices), bool) for o in meshes])
    lo, hi = f["lo"], f["hi"]
    for _ in range(12):
        mid = (lo + hi) / 2
        f["set"](mid)
        key_fk(clip)
        scene.frame_set(clip.frames + 1)
        dg = bpy.context.evaluated_depsgraph_get()
        zs = []
        for o in meshes:
            ev = o.evaluated_get(dg)
            m = ev.to_mesh()
            co = np.empty(len(m.vertices) * 3)
            m.vertices.foreach_get("co", co)
            zs.append((co.reshape(-1, 3) @ np.array(ev.matrix_world.to_3x3()).T)[:, 2] + ev.matrix_world.translation.z)
            ev.to_mesh_clear()
        z = float(np.concatenate(zs)[mask].min())
        if z > 0:
            lo = mid
        else:
            hi = mid
    f["set"](lo)
    key_fk(clip)
    clip.info["fall_deg"] = round(lo, 1)


def key_start(clip: Clip) -> None:
    """Fresh pose and action, IK set up, FK and the body keyed with no ground correction."""
    clear_pose()
    scene.frame_start, scene.frame_end = 1, clip.frames + 1
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="POSE")
    arm.animation_data_create()
    arm.animation_data.action = bpy.data.actions.new(clip.name)
    setup_ik(clip)
    key_fk(clip)
    key_root(clip, [0.0] * (clip.frames + 1))


def build(clip: Clip) -> dict:
    """Key, ground, bake and export one clip; returns its report."""
    t0 = time.time()
    key_start(clip)
    zero = [0.0] * (clip.frames + 1)
    if clip.fit:
        fit_angle(clip)
    corr = zero
    fix = [[0.0] * (clip.frames + 1) for _ in legs]
    tail_up = [0.0] * (clip.frames + 1)
    if clip.ground == "lift":  # constant, so clips do not pop against each other ...
        corr = [clip.hover] * (clip.frames + 1)
        key_root(clip, corr)
        lows, _, _ = sample(clip)  # ... except where a pose would push a part (a hem) through the ground
        if lows.min() < 0:
            corr = [h + max(0.0, -float(z)) for h, z in zip(corr, lows)]
            key_root(clip, corr)
    elif clip.ground != "none":
        if tail and clip.ground == "floor":
            clip.rot.setdefault(tail[0], []).append((TAIL_AXIS, lambda t: tail_up[round(t * clip.frames)]))
        sole = [min(z, 0.0) for z in rest_foot]
        if clip.ground == "rest" and clip.start_lift:
            # a lifted monster's death starts where its idle holds the body and eases down
            # before the lying pose is set on the ground. A death that already sinks from
            # higher up (the ghost's starts at 0.15 x height, its idle at 0.1) is pulled down
            # to the idle only over its first 0.3, so the two descents do not make a bounce
            end = 0.3 if clip.start_lift < 0 else max(0.1, clip.rest_from - 0.15)
            corr = [clip.start_lift * (1 - smooth((f / clip.frames) / end)) for f in range(clip.frames + 1)]
            key_root(clip, corr)
        if clip.drape:
            drape_solve(clip)
            key_fk(clip)
        for it in range(4):  # body height, tail and feet move each other a little; settle
            lows, loose, _ = sample(clip)
            corr = [a + b for a, b in zip(corr, correction(clip, lows, loose))]
            key_root(clip, corr)
            sample(clip)
            if clip.drape:
                drape_settle(clip)
                key_fk(clip)
            if clip.sway_k is not None and clip.ground == "floor" and it < 3:
                tail_clearance(clip)
                key_fk(clip)
            if tail and clip.ground == "floor":
                for f, z in enumerate(clip.tail_low):
                    if HANG:  # its end drags on the ground: down to TAIL_DIP in it (see hang_lift)
                        if z < -TAIL_DIP:  # aimed at 0.8 of it: the passes converge from below
                            tail_up[f] += hang_lift(tail_up[f], -z - 0.8 * TAIL_DIP)
                    elif z < 0:
                        tail_up[f] += math.degrees(math.asin(min(1.0, 1.2 * -z / TAIL_REACH)))
                    elif fan and it < 3 and z > FLOOR_TOL and tail_up[f] > 0:
                        # a drooping fan lifts unevenly and overshoots: ease it back onto
                        # the ground, which it rests on (the last pass only lifts)
                        tail_up[f] = max(0.0, tail_up[f] - 0.5 * math.degrees(math.asin(min(1.0, z / TAIL_REACH))))
                key_fk(clip)
            for i, g in enumerate(legs):  # a spider's leg tip turns with IK: keep it out of the ground
                for f in range(clip.frames + 1):
                    k = clip.ik(g, f / clip.frames)
                    if g["level"] and clip.ground == "rest" and 0 < k < 1 and clip.leg_low[f][i] < -FLOOR_TOL:
                        # a fall while the IK lets go: a half-held leg rolls with the body
                        # and its hoof or shin dips, which raising the body barely lifts (the
                        # IK still pulls the leg down); lift that foot's target by the dip
                        # (centaur death: -0.033 m at 0.33)
                        fix[i][f] += -clip.leg_low[f][i]
                        continue
                    if k <= 0.5:
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
    clip.pos = pos
    clip.torn = TORN["n"] = torn_edges(pos)
    if clip.drape:
        tail_up = clip.tail_up
    scene.frame_set(1)
    body_z = (world @ arm.pose.bones[root].head).z - by_name[root]["head"][2]  # root raise on the first frame
    rep = {"seconds": round(clip.frames / FPS, 2), "frames": clip.frames, "loop": clip.loop,
           "min_z": round(float(lows.min()), 4), "min_z_first": round(float(lows[0]), 4),
           "min_z_last": round(float(lows[-1]), 4), "body_z_first": round(float(body_z), 4),
           "body_raise_max": round(float(max(corr)), 4),
           "tail_raise_max_deg": round(max(tail_up), 2),
           "foot_raise_max": round(float(max(max(x) for x in fix)) if fix else 0.0, 4), **clip.info}
    # planted feet: the highest any standing foot floats (stance frames, IK fully on)
    gaps = [clip.foot_low[f][i] - rest_foot[i] for f in range(clip.frames + 1) for i in planted(clip, f)]
    if gaps and clip.ground != "hover":
        rep["planted_foot_gap"] = round(max(gaps), 4)  # highest a standing foot's lowest vertex floats
        rep["ik_reach_gap"] = round(max(reach[f][i] for f in range(clip.frames + 1) for i in planted(clip, f)), 4)
    if tail and clip.ground == "rest":  # where the tail ends up against the lying body
        last = pos[-1]
        rep["tail_z_last"] = [round(float(last[is_tail, 2].min()), 3), round(float(last[is_tail, 2].max()), 3)]
        rep["body_top_last"] = round(float(last[~is_tail, 2].max()), 3)
    if clip.drape:
        rep["tail_drape"] = True
    if getattr(clip, "swing", None):  # the arms' swing share from the loadout, and how far each grip's
        rep["arm_swing"] = clip.swing  # +Y turns from the idle's first frame over the clip
        dev, cyc, first = {}, {}, {}
        for f in range(1, clip.frames + 2):
            scene.frame_set(f)
            for a in arms:
                g, g0, sd = grip_world(a), FIRST.get("grip", {}).get(a["side"]), a["side"]
                if sd not in clip.swing or g is None:
                    continue
                first.setdefault(sd, g)
                cyc[sd] = max(cyc.get(sd, 0.0), math.degrees(g.angle(first[sd], 0.0)))
                if g0 is not None:
                    dev[sd] = max(dev.get(sd, 0.0), math.degrees(g.angle(g0, 0.0)))
        if dev:
            rep["grip_from_idle_deg"] = {k: round(v, 1) for k, v in dev.items()}
        if cyc:  # over the cycle, from the clip's own first frame
            rep["grip_cycle_deg"] = {k: round(v, 1) for k, v in cyc.items()}
    if clip.sway_k is not None:
        dl = []
        for f in range(1, clip.frames + 1):
            scene.frame_set(f)
            dl.append(tail_leg_dist())
        rep["tail_leg_min_m"] = round(min(dl), 4)
        rep["tail_leg_rest_m"] = round(TAIL_REST_CLEAR, 4)
        rep["tail_sway_min_k"] = round(min(clip.sway_k), 3)
    if clip.name == "idle":
        FIRST["tail_up0"] = tail_up[0]
        scene.frame_set(1)
        FIRST["grip"] = {a["side"]: grip_world(a) for a in arms}
        FIRST["idle"] = pos[0]
        FIRST["idle_cycle"] = pos[:clip.frames:IDLE_STEP]
    elif "idle" in FIRST:  # the first frame's largest vertex move from the idle's first frame
        rep["from_idle_m"] = round(float(np.linalg.norm(pos[0] - FIRST["idle"], axis=1).max()), 4)
    if "idle_cycle" in FIRST:
        # ... and from any frame of the idle (every IDLE_STEP-th): the game fires a one-shot
        # mid-idle; on the idle itself, how far it wanders from its own first frame
        rep["from_idle_max_m"] = round(max(float(np.linalg.norm(pos[0] - p, axis=1).max())
                                           for p in FIRST["idle_cycle"]), 4)
    if GROUND.any() and clip.ground == "floor":
        rep["contact_min_z"] = round(min(clip.contact_low), 4)  # lowest ground contact (GROUND)
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


def start_phase(make) -> tuple[float, float, float]:
    """(phase, jump at phase 0, jump there) for a looping clip `make(phase)`: the clip is
    built at phase 0 (ground passes included; the build is redone at the phase) and the
    frame whose skinned mesh is nearest the idle's first frame (largest vertex distance;
    the rest pose with the body IDLE_SINK down when there is no idle yet) becomes the start.
    Whole frames, so the loop stays on frames."""
    c = make(0.0)
    build(c)
    ref = FIRST["idle"] if "idle" in FIRST else REST_V - np.array([0.0, 0.0, drop * IDLE_SINK])
    dev = [float(np.linalg.norm(p - ref, axis=1).max()) for p in c.pos[:-1]]
    f = int(np.argmin(dev))
    return f / c.frames, dev[0], dev[f]


TORN: dict = {}  # "n": the last built clip's torn edges (the walk retry)
FIRST: dict = {}  # "idle": its first frame's skinned vertices, once built (start_phase, from_idle_m);
# "idle_cycle": every IDLE_STEP-th frame of it (from_idle_max_m)
IDLE_STEP = 3


# --- clips ----------------------------------------------------------------------------

def walk(scale: float = 1.0, phase: float | None = None) -> Clip:
    """Planted-feet in-place walk (the gait the stage started with). `scale` shortens
    the stride and step height. The loop starts at `phase` of the cycle (every layer is
    shifted by it, so the loop stays whole periods); None picks it: the frame of the cycle
    whose skinned mesh is nearest the idle's first frame (start_phase), so idle -> walk
    does not start mid-stride (harpy 0.175 m at the left foot)."""
    if phase is None:
        phase, dev0, dev = start_phase(lambda ph: walk(scale, ph))
        c = walk(scale, phase)
        c.info = {**c.info, "start_phase": round(phase, 4), "from_idle_phase0_m": round(dev0, 4)}
        return c
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
        u = (t + phase + g["phase"]) % 1.0
        if u < STANCE:
            fwd, up = 0.5 - u / STANCE, 0.0
        else:
            s = (u - STANCE) / (1.0 - STANCE)
            fwd, up = smooth(s) - 0.5, math.sin(math.pi * s)
        return Vector((0, fwd * STRIDE * scale * drop, up * LIFT * scale * drop))

    c.foot = offset
    c.lift = lambda t: Vector((0, 0, -drop * (CROUCH + BOB * (1 + math.cos(4 * math.pi * (t + phase))) / 2)))
    relax_arms(c)
    for a in arms:
        leg = next((g for g in legs if g["side"] == a["side"]), None)
        p = leg["phase"] if leg else 0.0
        arm_swing(c, a, lambda t: 0.0, lambda t, p=p: ARM_SWING * math.sin(2 * math.pi * (t + phase - p + 0.19)))
    if HANG and hind and TAIL_REST_CLEAR > TAIL_FUSED * height:  # kept clear of the hind legs (tail_clearance)
        c.sway_k = [1.0] * (c.frames + 1)
    k = lambda t: c.sway_k[round(t * c.frames)] if c.sway_k else 1.0
    for i, n in enumerate(tail):
        tail_sway(c, n, lambda t, i=i: k(t) * TAIL_SWAY * math.sin(2 * math.pi * (t + phase - 0.09 * i)))
    c.info = {"stride_scale": scale, "legs": len(legs), "feet": [g["foot"] for g in legs],
              "phases": [g["phase"] for g in legs],
              "arms": [a["upper"] for a in arms], "tail": tail}
    if HANG:
        c.info["tail_hangs"] = True
    return c


def idle() -> Clip:
    """Breathing, a slow weight shift over the feet, head looking about, tail and wings
    drifting. One breath per loop, everything else whole periods too. Every layer is 0 on
    the first frame, so the idle starts on the rest pose (the relaxed arm where there is
    one, the body IDLE_SINK down), where the one-shots start: the head's look-about and the
    tail's sway are in phase along their chains (the tail was a travelling wave, 0.78 m off
    rest on the dragon's first frame), the elbow bends from straight instead of about 4
    degrees."""
    c = Clip("idle", 2.5, True)
    sway = (0.012 if job["upright"] else 0.006) * height
    c.lift = lambda t: Vector((sway * math.sin(2 * math.pi * t), 0,
                               -drop * (IDLE_SINK + 0.012 * (1 - math.cos(2 * math.pi * t)) / 2)))
    c.turn_chain(torso, "X", lambda t: 2.0 * math.sin(2 * math.pi * t))
    c.turn_chain(neck[:-1], "Z", wave(8.0, 1))
    c.turn_chain(neck[:-1], "X", wave(4.0, 2))
    c.turn(skull, "Z", wave(5.0, 1))
    relax_arms(c)
    for a in arms:
        c.turn(a["upper"], "X", wave(3.0, 1, 0.5))
        c.turn(a["forearm"], "X", lambda t: 3.5 * (1 - math.cos(2 * math.pi * t)))
    for n, a in zip(tail, tail_idle_amps()):
        tail_sway(c, n, wave(a, 1))
    for w in wings:
        c.turn(w["chain"][0], "Y", lambda t, s=w["side"]: sym(s, -3.0 * math.sin(2 * math.pi * t)))
    return c


def tail_idle_amps() -> list[float]:
    """The idle's tail sway per bone (degrees, in phase about Z, so it is 0 on the first
    frame): growing from the root to the last bone (i / n), the last at most TAIL_IDLE_DEG,
    all scaled down until the farthest point of the chain moves at most TAIL_IDLE_TIP x
    body_len. An in-phase turn adds up along the chain: 5 degrees on each of the dragon's
    9 curled tail bones swung its tip 1.66 m mid-idle, which a game switch to any clip
    shows as a jump. Bound: each bone moves the chain beyond it by its angle (radians) x
    the largest horizontal distance from its head to a joint further out."""
    if not tail:
        return []
    n = len(tail)
    w = [(i + 1) / n for i in range(n)]
    k = 3 if HANG else 2  # a hanging tail swings in 3D (its horizontal lever is ~0)
    pts = [Vector(by_name[b]["tail"][:k]) for b in tail]
    lever = [max((p - Vector(by_name[b]["head"][:k])).length for p in pts[i:]) for i, b in enumerate(tail)]
    reach = sum(wi * li for wi, li in zip(w, lever))  # metres the far end moves per radian at the last bone
    amp = min(TAIL_IDLE_DEG, math.degrees(TAIL_IDLE_TIP * body_len / reach)) if reach > 1e-6 else TAIL_IDLE_DEG
    return [amp * wi for wi in w]


def attack_biped() -> Clip:
    """A sword cut from the right hand (the weapon is a prop at hand_r): the wrist, held by
    arm IK riding on the chest, winds up high over the right shoulder (blade back), cuts
    down and across the front of the body as the torso turns into it, follows through low
    on the left and recovers. The left arm swings back for balance.

    The arm IK fades in over the wind-up and out over the recovery (`hand_ik`), so the
    first and last frames are the FK arm the idle starts on (rest, or the relax layer): at
    full influence on the rest wrist the IK found its own elbow (harpy: upper arm 0.10 m,
    `grip_r` 0.13 m off the idle's first frame; goblin 0.35 m at the scimitar)."""
    c = Clip("attack_melee", 1.0, False)
    a = arm_by_side.get("r") or arms[0]
    sx = 1 if a["side"] == "r" else -1
    sh = Vector(by_name[a["upper"]]["head"])
    wr = Vector(by_name[a["forearm"]]["tail"])
    lu = (Vector(by_name[a["upper"]]["tail"]) - sh).length
    lf = (wr - Vector(by_name[a["forearm"]]["head"])).length
    L = lu + lf
    if a["side"] in RELAXED:  # the wrist where the relax layer holds it
        u0, f0, _ = RELAXED[a["side"]]
        wr = sh + lu * u0 + lf * f0
    relax_arms(c)
    p = lambda x, y, z: sh + Vector((sx * x * L, y * L, z * L))
    path = [(0, wr), (0.3, p(0.2, -0.2, 0.8)), (0.42, p(0.25, -0.3, 0.85)), (0.5, p(0.05, 0.55, 0.5)),
            (0.57, p(-0.35, 0.8, -0.05)), (0.68, p(-0.6, 0.45, -0.6)), (1.0, wr)]
    c.hands[a["side"]] = vkeys(*path)
    c.hand_ik[a["side"]] = keys((0, 0), (0.25, 1), (0.75, 1), (1.0, 0))
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
    c.info = {**c.info, "arm": a["chain"], "weapon_socket": "hand_r"}
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
    sk = AVIAN_REAR if PLAN == "avian" else 1.0
    c.turn(skull, "X", keys((0, 0), (0.3, 10 * sk), (0.48, -8), (0.55, 4), (0.62, -6), (1, 0)))
    for i, n in enumerate(tail):
        tail_curl(c, n, keys((0, 0), (0.3, -2), (0.5, 4), (1, 0)))
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
    relax_arms(c)
    if job["upright"]:
        c.turn_chain(torso, "X", lambda t: 12 * k(t))
        for a in arms:
            c.turn(a["upper"], "Y", lambda t, s=a["side"]: sym(s, -18 * k(t)))
            c.turn(a["forearm"], "X", lambda t: 25 * k(t))
    elif PLAN != "avian":  # a bird's tail fan rests on the ground: a nose-up tilt drives it through
        c.tilt = lambda t: Quaternion(AXES["X"], math.radians(5 * k(t)))
    c.turn_chain(neck, "X", lambda t: 14 * k(t))
    c.turn(skull, "X", keys((0, 0), (0.22, 12), (1, 0)))
    for w in wings:
        c.turn(w["chain"][0], "Y", lambda t, s=w["side"]: sym(s, -12 * k(t)))
    for n in tail:
        tail_curl(c, n, lambda t: 4 * k(t))
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
        relax_arms(c, keys((0, 1), (0.5, 0)))  # from the relaxed arm into the fall
        for a in arms:
            c.turn(a["upper"], "Y", lambda t, s=a["side"]: sym(s, keys((0, 0), (0.3, -10), (0.75, -70), (1, -70))(t)))
            c.turn(a["upper"], "X", keys((0, 0), (0.3, 20), (0.75, 40), (1, 40)))
            c.turn(a["forearm"], "X", keys((0, 0), (0.3, 30), (0.8, 15), (1, 15)))
        return c
    if PLAN == "avian":
        # a bird stands with its legs under it and its wings out: rolling onto the side
        # props it on a wing tip, so it pitches forward onto its breast and the wings
        # fold up and back over it
        front_y = max(g["y"] for g in legs) + 0.1 * body_len
        c.pivot = Vector((cx, front_y, 0.0))
        fall = lambda t: -80 * ease_in((t - 0.25) / 0.5) + 5 * math.sin(math.pi * smooth((t - 0.75) / 0.15))
        c.tilt = lambda t: Quaternion(AXES["X"], math.radians(fall(t)))
        c.lift = lambda t: Vector((0, 0, -0.25 * drop * buckle(t)))
        for w in wings:
            c.turn(w["chain"][0], "Y", lambda t, s=w["side"]: sym(s, keys((0, 0), (0.6, -40), (1, -40))(t)))
            c.turn(w["chain"][0], "X", keys((0, 0), (0.6, 30), (1, 30)))
        for g in legs:
            c.turn(g["chain"][0], "X", keys((0, 0), (0.6, -40), (1, -40)))
        c.info = {"falls_on": "breast"}
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
    if tail and not HANG:  # the tail turns back against the roll, so it lies along the ground
        c.turn(tail[0], "Y", lambda t: -0.9 * roll(t))
    elif HANG:  # a hanging tail goes limp onto the ground (death_drape; turned back against the
        # roll, it swung down through the floor; left with the body, it stuck out level)
        death_drape(c)
    for w in wings:  # wings fold up over the back, away from the ground
        c.turn(w["chain"][0], "Y", lambda t, s=w["side"]: sym(s, keys((0, 0), (0.6, -45), (1, -45))(t)))
    return c


def wing_beat(c: Clip, A, at: float | None = None, w=lambda t: 1.0) -> None:
    """The fly clip's beat on every wing chain (a wave down the chain, A(t) its size);
    `at` freezes it at that t, weighted by w(t): a legged flier's lifted one-shots hold its
    first frame, where its idle and fly clips start (lifted_oneshot)."""
    for wg in wings:
        s, ch = wg["side"], wg["chain"]
        T = (lambda t: t) if at is None else (lambda t: at)
        c.turn(ch[0], "Y", lambda t, s=s: sym(s, -28 * A(T(t)) * math.cos(2 * math.pi * T(t))) * w(t))
        c.turn(ch[0], "Z", lambda t, s=s: sym(s, -8 * A(T(t)) * math.sin(2 * math.pi * T(t))) * w(t))
        for i, n in enumerate(ch[1:4], 1):
            c.turn(n, "Y", lambda t, s=s, i=i: sym(s, -10 * A(T(t)) * math.cos(2 * math.pi * (T(t) - 0.08 * i))) * w(t))


def fly(name: str = "fly", amp: float = 1.0, seconds: float | None = None) -> Clip:
    """Hover: wings beat (a wave down each chain), legs hang limp, the body rises on the
    downstroke and sinks on the upstroke, held off the ground by the hover lift. `amp`
    scales the beat and the bob, `seconds` sets the loop (the lifted idle of a creature that
    only flies: 0.6 over 2.5 s, one slow beat). With amp < 1 the beat's size swells back to
    full at the loop's start (A(t) = amp + (1 - amp)(1 + cos 2 pi t) / 2, 0.92 at the bottom
    of the bob), so the idle's first frame is the fly clip's first frame: the eagle's idle
    held its wings 11 degrees off the fly's and idle -> fly moved a wing tip 1.75 m."""
    c = Clip(name, seconds or (1.2 if height < 3 else 1.6), True)
    c.ground = "hover"
    c.ik = lambda g, t: 0.0
    A = (lambda t: 1.0) if amp >= 1.0 else (lambda t: amp + (1 - amp) * (1 + math.cos(2 * math.pi * t)) / 2)
    bob = FLY_BOB[0 if height < 3 else 1]
    c.lift = lambda t: Vector((0, 0, bob * A(t) * math.sin(2 * math.pi * (t - 0.1))))
    lean = -12 if job["upright"] else 4
    c.tilt = lambda t: Quaternion(AXES["X"], math.radians(lean + 2 * math.sin(2 * math.pi * (t - 0.3))))
    c.pivot = Vector((cx, leg_y, 0.6 * height))
    wing_beat(c, A)
    for g in legs:
        if job["upright"]:
            c.turn(g["chain"][0], "X", lambda t: 15 + 3 * math.sin(2 * math.pi * t))
            c.turn(g["chain"][1], "X", lambda t: -35 + 3 * math.sin(2 * math.pi * (t - 0.1)))
        else:
            fore = g in front
            c.turn(g["chain"][0], "X", lambda t, fore=fore: (20 if fore else -25) + 2 * math.sin(2 * math.pi * t))
    for i, n in enumerate(tail):
        tail_curl(c, n, lambda t, i=i: 3 * math.sin(2 * math.pi * (t - 0.1 * i)))
    c.turn_chain(neck, "X", lambda t: -2 * math.sin(2 * math.pi * (t - 0.2)))
    c.info = {"wings": {w["side"]: w["chain"] for w in wings}, "hover_m": HOVER}
    return c


def breath() -> Clip:
    """Rear the head back, thrust it forward and down and hold it there ~1 s for the
    game's fire, then recover. Any skeleton with a head chain: four legs rear with a
    nose-up pitch of the body (the dragon's), an upright body (half-dragon, mephits) leans
    its torso back instead and thrusts it forward, the neck doing a smaller share."""
    c = Clip("breath", 2.2, False)
    up = job["upright"]
    k = 0.5 if up else 1.0
    c.turn_chain(neck, "X", keys((0, 0), (0.22, 35 * k), (0.32, -12 * k), (0.82, -12 * k), (1, 0)))
    c.turn(skull, "X", keys((0, 0), (0.22, 10), (0.32, -6), (0.82, -6), (1, 0)))
    c.turn(skull, "Z", lambda t: 3 * math.sin(2 * math.pi * 3 * smooth((t - 0.32) / 0.5)))
    if up:
        c.turn_chain(torso, "X", keys((0, 0), (0.22, 16), (0.32, -10), (0.82, -10), (1, 0)))
        relax_arms(c)
        for a in arms:  # arms swing back with the rear, forward with the thrust
            c.turn(a["upper"], "X", keys((0, 0), (0.22, -12), (0.32, 8), (0.82, 8), (1, 0)))
    else:
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
    rh = grip_dir(a)

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


def grip_dir(a: dict) -> Vector | None:
    """World direction of the grip bone's +Y at rest (the prop's shaft; the hand's own +Y
    when there is no grip bone), None for an arm that ends at the forearm."""
    hand = a["hand"]
    grip = next((k for k in kids.get(hand, []) if k.startswith("grip_")), hand) if hand else None
    return (world.to_3x3() @ (arm.data.bones[grip].matrix_local.to_3x3() @ Vector((0, 1, 0)))).normalized() \
        if grip else None


def rest_dirs(a: dict) -> tuple:
    """(upper arm, forearm, grip +Y) world directions in the rest pose."""
    vec = lambda n: (Vector(by_name[n]["tail"]) - Vector(by_name[n]["head"])).normalized()
    return vec(a["upper"]), vec(a["forearm"]), grip_dir(a)


# Arm relax: SkinTokens keeps the reference's pose as the rest pose, and an arm drawn raised
# (the merfolk's spear arm: shoulder -> wrist 39 degrees up) then holds its prop over the
# head in every clip that leaves the arm alone. Such an arm (shoulder -> wrist rising more
# than RAISED_DEG) gets a base layer in the idle, move, hit and death clips: the upper arm
# down at RELAX_DEG from the body, the forearm forward, the grip's +Y forward and up (a
# spear held upright, a blade at the ready). The melee attack starts and ends there too.
# Arms that hang or reach out sideways at rest are left as they are.
RAISED_DEG = 15.0
RELAX_DEG = 30.0


def arm_rise(a: dict) -> float:
    d = Vector(by_name[a["forearm"]]["tail"]) - Vector(by_name[a["upper"]]["head"])
    return math.degrees(math.atan2(d.z, math.hypot(d.x, d.y)))


def relaxed_dirs(a: dict) -> tuple:
    sx = 1 if a["side"] == "r" else -1
    r = math.radians(RELAX_DEG)
    return (Vector((sx * math.sin(r), 0.0, -math.cos(r))), Vector((0.1 * sx, 1.0, 0.15)).normalized(),
            Vector((0.0, 0.4, 0.92)).normalized())


RELAXED = {a["side"]: relaxed_dirs(a) for a in arms if arm_rise(a) > RAISED_DEG}


def relax_arms(c: Clip, w=lambda t: 1.0) -> None:
    """The arm-relax base layer (above) on every raised arm; `w` fades it (death)."""
    for a in arms:
        if a["side"] in RELAXED:
            u, f, g = RELAXED[a["side"]]
            aim_arm(c, a, lambda t, u=u: u, lambda t, f=f: f, w, up=lambda t, g=g: g)
    if RELAXED:
        c.info = {**c.info, "arms_relaxed": sorted(RELAXED)}


# Attack style from the prop in the right hand (job weapon_r: anim.wielded(), the first of
# the record's wields on hand_r, as the viewer shows it): a polearm (spear, trident, pike,
# glaive) or a javelin thrusts, everything else, blades, axes, clubs and an empty hand, cuts.
WEAPON = job.get("weapon_r") or {}
THRUST_CATEGORIES = {"polearm", "thrown"}
STYLE = "thrust" if WEAPON.get("category") in THRUST_CATEGORIES else "cut"
# Arm swing in the walk and swim from the same loadout (job weapon_r, weapon_l: anim.wielded on
# each hand). A long or two-handed prop (polearm, thrown, bow, crossbow, props.json grip 2) is
# held steady: the shoulder swings STEADY_SWING of the empty-hand swing, the elbow keeps its
# mean bend, and the hand turns back against the shoulder's swing so the shaft keeps its idle
# direction (the centaur's 3.5 m pike swung by the walk's 16 degrees dropped its head to the
# floor ahead at 25% and raised it over the head at 75%). Other weapons (sword, club, a
# shield) swing LIGHT_SWING of it; empty hands the full swing.
WEAPON_L = job.get("weapon_l") or {}
STEADY_CATEGORIES = THRUST_CATEGORIES | {"bow", "crossbow"}
STEADY_SWING, LIGHT_SWING = 0.2, 0.5


def swing_share(side: str) -> float:
    w = WEAPON if side == "r" else WEAPON_L
    if not w.get("id"):
        return 1.0
    return STEADY_SWING if w.get("category") in STEADY_CATEGORIES or (w.get("grip") or 1) >= 2 else LIGHT_SWING


def layers_q(c, bone: str, t: float, upto: int | None = None) -> Quaternion:
    """A bone's FK layers composed at t (key_fk's order), the first `upto` of them."""
    q = Quaternion()
    for ax, fn in c.rot.get(bone, [])[:upto]:
        q = (fn(t) if ax is None else Quaternion(ax, math.radians(fn(t)))) @ q
    return q


def arm_swing(c, a: dict, base, osc, axis: str = "X", fore=None) -> None:
    """The upper arm turns base(t) + k x osc(t) about `axis`, k = swing_share (the loadout);
    `fore` (base, osc) the same on the forearm (its swing dropped for a held prop). A held
    prop's hand (else the forearm) gets the swing's turn back, taken through the arm's other
    layers (the relax layer), so the grip keeps its direction over the cycle."""
    k = swing_share(a["side"])
    n_up = len(c.rot.get(a["upper"], []))
    c.turn(a["upper"], axis, lambda t: base(t) + k * osc(t))
    steady = k <= STEADY_SWING
    if fore:
        fb, fo = fore
        c.turn(a["forearm"], axis, (lambda t: fb(t)) if steady else (lambda t: fb(t) + k * fo(t)))
    if steady:
        hold = a["hand"] or a["forearm"]
        n_f = len(c.rot.get(a["forearm"], []))

        def held(t):
            A = layers_q(c, a["upper"], t, n_up)
            if hold != a["forearm"]:
                A = A @ layers_q(c, a["forearm"], t, n_f)
            return A.inverted() @ Quaternion(AXES[axis], math.radians(-k * osc(t))) @ A
        c.rot.setdefault(hold, []).append((None, held))
    if k < 1:
        c.swing = {**getattr(c, "swing", {}), a["side"]: k}


def grip_world(a: dict) -> Vector | None:
    """The grip bone's +Y (the prop's shaft) on the posed frame, world; None without a hand."""
    hand = a["hand"]
    grip = next((k for k in kids.get(hand, []) if k.startswith("grip_")), hand) if hand else None
    return ((world @ arm.pose.bones[grip].matrix).to_3x3() @ Vector((0, 1, 0))).normalized() if grip else None


def attack_thrust() -> Clip:
    """A spear thrust from the right hand (prop at grip_r, shaft along its +Y), FK-aimed
    (aim_arm): from the relaxed (or rest) arm the hand draws back to the side of the waist
    with the elbow back and the shaft levelled at the target, drives forward until the arm
    is straight in front of the shoulder, holds, and recovers. The shaft points along the
    line the fist travels, so the point leads and the grip slides along the prop's +Y;
    the torso winds back, then turns and leans into the thrust (its turn is taken out of
    the arm's directions so the shaft stays on line), the body steps in."""
    c = Clip("attack_melee", 1.0, False)
    a = arm_by_side.get("r") or arms[0]
    sx = 1 if a["side"] == "r" else -1
    sh = Vector(by_name[a["upper"]]["head"])
    lu = (Vector(by_name[a["upper"]]["tail"]) - sh).length
    lf = (Vector(by_name[a["forearm"]]["tail"]) - Vector(by_name[a["forearm"]]["head"])).length
    u0, f0, g0 = RELAXED.get(a["side"]) or rest_dirs(a)
    u1, f1 = Vector((0.35 * sx, -0.6, -0.5)).normalized(), Vector((0.05 * sx, 1.0, 0.05)).normalized()
    u2, f2 = Vector((0.12 * sx, 1.0, -0.1)).normalized(), Vector((0.02 * sx, 1.0, -0.02)).normalized()
    line = ((sh + lu * u2 + lf * f2) - (sh + lu * u1 + lf * f1)).normalized()  # the fist's travel
    twist = keys((0, 0), (0.3, -sx * 12), (0.48, sx * 15), (0.62, sx * 15), (1, 0))
    lean = keys((0, 0), (0.3, 4), (0.48, -8), (0.62, -8), (1, 0))
    c.turn_chain(torso, "Z", twist)
    c.turn_chain(torso, "X", lean)
    undo = lambda t: (Quaternion(AXES["X"], math.radians(lean(t))) @ Quaternion(AXES["Z"], math.radians(twist(t)))).inverted()
    T = (0, 0.3, 0.48, 0.62, 1.0)
    uk = vkeys(*zip(T, (u0, u1, u2, u2, u0)))
    fk = vkeys(*zip(T, (f0, f1, f2, f2, f0)))
    gk = vkeys(*zip(T, (g0, line, line, line, g0))) if g0 is not None else None
    aim_arm(c, a, lambda t: (undo(t) @ uk(t)).normalized(), lambda t: (undo(t) @ fk(t)).normalized(), lambda t: 1.0,
            up=(lambda t: (undo(t) @ gk(t)).normalized()) if gk else None)
    o = arm_by_side.get("l" if a["side"] == "r" else "r")
    if o:
        c.turn(o["upper"], "X", keys((0, 0), (0.3, 10), (0.48, -15), (0.62, -15), (1, 0)))
    step = keys((0, 0), (0.3, -0.4), (0.48, 1), (0.62, 1), (1, 0))
    c.lift = lambda t: Vector((0, 0.05 * height * step(t), -drop * keys((0, 0), (0.3, 0.03), (0.5, 0.05), (1, 0))(t)))
    c.info = {"arm": a["chain"], "weapon_socket": "hand_r", "style": "thrust", "weapon": WEAPON.get("id"),
              **({"arms_relaxed": sorted(RELAXED)} if RELAXED else {})}
    return c


def attack_throw() -> Clip:
    """An overhand throw from the right hand (spear, javelin at grip_r, shaft along its +Y),
    FK-aimed like the thrust: from the relaxed (or rest) arm the hand cocks back above and
    behind the shoulder with the elbow out at shoulder height and the shaft levelled at the
    target over the shoulder, the torso turning away and leaning back; then the arm whips
    over the shoulder, releases in front at head height, follows through down across the
    body and recovers. It starts and ends on the arm the idle holds (the merfolk's relaxed
    spear arm), so idle -> attack_ranged does not raise the arm in one frame."""
    c = Clip("attack_ranged", 1.0, False)
    a = arm_by_side.get("r") or arms[0]
    sx = 1 if a["side"] == "r" else -1
    u0, f0, g0 = RELAXED.get(a["side"]) or rest_dirs(a)
    v = lambda x, y, z: Vector((sx * x, y, z)).normalized()
    T = (0, 0.35, 0.5, 0.62, 1.0)  # rest, cocked, release, follow-through, rest
    U = (u0, v(0.55, -0.6, 0.35), v(0.2, 0.6, 0.75), v(-0.1, 0.75, -0.55), u0)
    F = (f0, v(0.05, -0.35, 1.0), v(0.05, 1.0, 0.45), v(-0.35, 0.85, -0.4), f0)
    G = (g0, v(0, 1, 0.3), v(0, 1, 0.15), v(-0.2, 1, -0.5), g0)
    twist = keys(*zip(T, (0, -sx * 20, sx * 12, sx * 18, 0)))
    lean = keys(*zip(T, (0, 8, -6, -12, 0)))
    c.turn_chain(torso, "Z", twist)
    c.turn_chain(torso, "X", lean)
    undo = lambda t: (Quaternion(AXES["X"], math.radians(lean(t))) @ Quaternion(AXES["Z"], math.radians(twist(t)))).inverted()
    uk, fk = vkeys(*zip(T, U)), vkeys(*zip(T, F))
    gk = vkeys(*zip(T, G)) if g0 is not None else None
    aim_arm(c, a, lambda t: (undo(t) @ uk(t)).normalized(), lambda t: (undo(t) @ fk(t)).normalized(), lambda t: 1.0,
            up=(lambda t: (undo(t) @ gk(t)).normalized()) if gk else None)
    o = arm_by_side.get("l" if a["side"] == "r" else "r")
    if o:  # the free arm points at the target while cocked, swings down with the throw
        c.turn(o["upper"], "X", keys(*zip(T, (0, 20, 0, -20, 0))))
    step = keys(*zip(T, (0, -0.4, 1, 1, 0)))
    c.lift = lambda t: Vector((0, 0.04 * height * step(t), -drop * keys(*zip(T, (0, 0.03, 0.05, 0.05, 0)))(t)))
    c.info = {"arm": a["chain"], "weapon_socket": "hand_r", "style": "throw", "weapon": WEAPON.get("id"),
              **({"arms_relaxed": sorted(RELAXED)} if RELAXED else {})}
    return c


def attack_melee_armed() -> Clip:
    """The right-hand attack in the style of the wielded prop (above)."""
    if STYLE == "thrust":
        return attack_thrust()
    c = attack_biped()
    c.info["style"] = "cut"
    if WEAPON.get("id"):
        c.info["weapon"] = WEAPON["id"]
    return c


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


# --- cast, swim, burrow, levitate (2026-11-04) ---------------------------------------------
# cast: gather (0-0.3), hold with a small pulse (0.3-0.58), release (0.58-0.7), hold the
# release (0.7-0.8), recover (0.8-1). Every clip below starts on the rest pose (the relaxed
# arm where the idle has one); burrow alone ends elsewhere (sunk into the ground).
CAST_T = (0, 0.3, 0.58, 0.7, 0.8, 1.0)


def fold_wings(c: Clip, w) -> None:
    """Wings swept back along the body and up a little (swim, burrow); `w(t)` 0-1."""
    for wg in wings:
        s = wg["side"]
        c.turn(wg["chain"][0], "Z", lambda t, s=s: sym(s, -40 * w(t)))
        c.turn(wg["chain"][0], "Y", lambda t, s=s: sym(s, -15 * w(t)))
        for n in wg["chain"][1:3]:
            c.turn(n, "Z", lambda t, s=s: sym(s, -15 * w(t)))


def cast_arms(c: Clip) -> None:
    """Spell gesture on the arms (FK-aimed, like the thrust): the casting hand (hand_r)
    rises forward and up over the head, holds with a small pulse, then throws forward at
    shoulder height, holds the release and recovers; the other hand comes up in front of
    the chest and pushes forward with it. The grip's +Y (a staff, a wand, the hand) points
    up while raised and forward on the release."""
    a = arm_by_side.get("r") or arms[0]
    pulse = lambda t: 0.06 * math.sin(2 * math.pi * 3 * smooth((t - 0.3) / 0.28)) if 0.3 < t < 0.58 else 0.0
    for arm_, main in [(a, True)] + [(o, False) for o in arms if o is not a][:1]:
        sx = 1 if arm_["side"] == "r" else -1
        u0, f0, g0 = RELAXED.get(arm_["side"]) or rest_dirs(arm_)
        if main:
            u1, f1 = Vector((0.3 * sx, 0.55, 0.78)), Vector((0.05 * sx, 0.35, 0.94))
            u2, f2 = Vector((0.15 * sx, 1.0, 0.2)), Vector((0.0, 1.0, 0.08))
        else:
            u1, f1 = Vector((0.3 * sx, 0.45, -0.35)), Vector((-0.45 * sx, 0.85, 0.3))
            u2, f2 = Vector((0.2 * sx, 1.0, 0.0)), Vector((0.05 * sx, 1.0, 0.0))
        uk = vkeys(*zip(CAST_T, (u0, u1, u1, u2, u2, u0)))
        fk = vkeys(*zip(CAST_T, (f0, f1, f1, f2, f2, f0)))
        up1, up2 = Vector((0.0, 0.3, 0.95)), Vector((0.0, 0.95, 0.3))
        gk = vkeys(*zip(CAST_T, (g0, up1, up1, up2, up2, g0))) if g0 is not None else None
        aim_arm(c, arm_, lambda t, uk=uk: (uk(t) + Vector((0, 0, pulse(t)))).normalized(),
                lambda t, fk=fk: fk(t).normalized(), lambda t: 1.0,
                up=(lambda t, gk=gk: gk(t).normalized()) if gk else None)
    c.turn_chain(torso, "X", keys(*zip(CAST_T, (0, 5, 5, -7, -7, 0))))
    c.turn(skull, "X", keys(*zip(CAST_T, (0, 8, 8, -4, -4, 0))))
    c.info = {**c.info, "cast": "arms", "hand": a["side"], **({"arms_relaxed": sorted(RELAXED)} if RELAXED else {})}


def cast_head(c: Clip, scale: float = 1.0) -> None:
    """Spell gesture without arms (dragons, beasts): the head and neck rear up, hold with a
    small pulse, then snap forward and down on the release; the body rises a little (four
    legs: a nose-up pitch about the hind hips, the floor pass keeps the front feet planted;
    upright and floating bodies: 0.03 x drop up), wings
    lift and tail curls up with the same timing. `scale` shrinks the body's part (the
    DIP_MAX retry, as the lunge)."""
    pulse = lambda t: 3 * math.sin(2 * math.pi * 3 * smooth((t - 0.3) / 0.28)) if 0.3 < t < 0.58 else 0.0
    rear = keys(*zip(CAST_T, (0, 28, 28, -8, -8, 0)))
    c.turn_chain(neck, "X", lambda t: rear(t) + pulse(t))
    c.turn(skull, "X", keys(*zip(CAST_T, (0, 10, 10, -6, -6, 0))))
    if not job["upright"] and PLAN != "avian":  # a bird's tail fan rests on the ground (hit)
        c.tilt = lambda t: Quaternion(AXES["X"], math.radians(scale * keys(*zip(CAST_T, (0, 5, 5, -1, -1, 0)))(t)))
    elif job["upright"]:
        c.turn_chain(torso, "X", keys(*zip(CAST_T, (0, 8, 8, -5, -5, 0))))
    rise = keys(*zip(CAST_T, (0, 1, 1, 0.3, 0.3, 0)))
    if job["upright"] or legless:  # four legs rise by the pitch alone: a lift on top stretched the
        c.lift = lambda t: Vector((0, 0, scale * 0.03 * drop * rise(t)))  # owlbear's front paws 4.5 cm into the ground
    for wg in wings:
        c.turn(wg["chain"][0], "Y", lambda t, s=wg["side"]: sym(s, -20 * rise(t)))
    for n in tail:
        tail_curl(c, n, lambda t: 3 * rise(t))
    c.info = {**c.info, "cast": "head", "body_scale": scale}


def cast(base=None, scale: float = 1.0) -> Clip:
    """`cast` (1.2 s, once): the arms when the plan has them, else the head (above)."""
    c = base("cast", 1.2, False) if base else Clip("cast", 1.2, False)
    if arms:
        cast_arms(c)
    else:
        cast_head(c, scale)
    return c


SWIM_LEGS = 0.5     # legged swim: the body is lifted this fraction of the leg's drop


def swim_legged() -> Clip:
    """Swim in place for legged plans: the body is held SWIM_LEGS x drop up (`lift`, the
    aquatic swim's mode, so a monster that only swims idles at the same height), pitched a
    little (nose up for four legs, leaning forward for upright bodies), the legs paddle in a
    slow alternating stroke with IK off (diagonal pairs; sprawled legs sweep fore and aft),
    the arms stroke, wings fold back, the tail sways as a rudder, a slow bob."""
    c = Clip("swim", 1.8, True)
    c.ground, c.hover = "lift", SWIM_LEGS * drop
    c.ik = lambda g, t: 0.0
    up = job["upright"]
    pitch = -35.0 if up else 6.0
    c.pivot = Vector((cx, leg_y, 0.5 * height)) if up else PIVOT.copy()
    c.tilt = lambda t: Quaternion(AXES["X"], math.radians(pitch + 2 * math.sin(2 * math.pi * t)))
    c.lift = lambda t: Vector((0, 0, 0.012 * height * math.sin(2 * math.pi * 2 * t)))
    for g in legs:
        if job["sprawl"]:
            i = sorted((x for x in legs if x["side"] == g["side"]), key=lambda x: -x["y"]).index(g)
            ph = 0.5 * ((i + (g["side"] == "r")) % 2)
            c.turn(g["chain"][0], "Z", lambda t, ph=ph, s=g["side"]: sym(s, 14 * math.sin(2 * math.pi * (t - ph))))
            continue
        fore = g in front
        ph = 0.0 if (g["side"] == "r") == (fore or up) else 0.5
        if up:  # flutter kick, knees bending on the up-beat
            c.turn(g["chain"][0], "X", lambda t, ph=ph: -6 + 16 * math.sin(2 * math.pi * (t - ph)))
            if len(g["chain"]) > 1:
                c.turn(g["chain"][1], "X", lambda t, ph=ph: -18 - 12 * math.cos(2 * math.pi * (t - ph)))
            continue
        c.turn(g["chain"][0], "X", lambda t, ph=ph, k=1 if fore else -1:
               (8 * k) + 24 * math.sin(2 * math.pi * (t - ph)))
        if len(g["chain"]) > 1:  # the paw curls on the forward (recovery) swing
            c.turn(g["chain"][1], "X", lambda t, ph=ph, k=-1 if fore else 1:
                   k * 22 * max(0.0, math.cos(2 * math.pi * (t - ph))))
    relax_arms(c)
    for a in arms:  # dog paddle, alternating (a held prop: steadier, arm_swing)
        ph = 0.0 if a["side"] == "r" else 0.5
        arm_swing(c, a, lambda t: 45.0, lambda t, ph=ph: 30 * math.sin(2 * math.pi * (t - ph)),
                  fore=(lambda t: 20.0, lambda t, ph=ph: 15 * math.cos(2 * math.pi * (t - ph))))
    fold_wings(c, lambda t: 1.0)
    c.turn_chain(neck, "X", lambda t: (30.0 if up else 6.0) + 2 * math.sin(2 * math.pi * (t - 0.2)))
    for i, n in enumerate(tail):
        tail_sway(c, n, lambda t, i=i: 6 * math.sin(2 * math.pi * (t - 0.08 * i)))
        tail_curl(c, n, lambda t: 2.0)
    c.info = {"swim_lift_m": round(c.hover, 4), "pitch_deg": pitch}
    return c


BURROW_SINK = 0.5   # burrow: the body ends this fraction of the height lower (half sunk)


def burrow() -> Clip:
    """Dig in (1.5 s, once; ends sunk, hold the last frame): crouch, the front limbs (front
    legs, a spider's front pair, a biped's arms) dig in three alternating strokes that
    drive below the ground and pull back, the head goes down, then the body sinks by
    BURROW_SINK x its height (wings left out: `body_h`) with the feet going down with it. Ground mode `none`: this is the
    one clip whose vertices go below the ground (`min_z` is reported as it is)."""
    c = Clip("burrow", 1.5, False)
    c.ground = "none"
    crouch = keys((0, 0), (0.2, 1), (1, 1))
    sink = keys((0, 0), (0.62, 0), (1, 1))
    depth = BURROW_SINK * body_h
    env = lambda t: smooth((t - 0.12) / 0.1) * (1 - smooth((t - 0.66) / 0.1))
    strokes = lambda t: 3 * min(1.0, max(0.0, (t - 0.12) / 0.62))
    diggers = [] if job["upright"] else sorted(legs, key=lambda g: -g["y"])[:2] if job["sprawl"] else front

    def foot(g, t):
        down = Vector((0, 0, -depth * sink(t)))
        if g not in diggers:
            return down
        ph = 0.5 if g["side"] == "l" else 0.0
        a = 2 * math.pi * (strokes(t) - ph)
        # up and forward over the top, down below the ground and back: a digging stroke
        return down + env(t) * drop * Vector((0, 0.14 * math.sin(a - math.pi / 2), 0.2 * math.sin(a)))

    c.foot = foot
    c.lift = lambda t: Vector((0, 0, -drop * 0.15 * crouch(t) - depth * sink(t)))
    if job["upright"]:
        c.turn_chain(torso, "X", keys((0, 0), (0.2, -25), (0.7, -30), (1, -20)))
        relax_arms(c, lambda t: 1 - smooth(t / 0.2))
        for a in arms:
            ph = 0.5 if a["side"] == "l" else 0.0
            c.turn(a["upper"], "X", lambda t, ph=ph: env(t) * (55 + 35 * math.sin(2 * math.pi * (strokes(t) - ph))))
            c.turn(a["forearm"], "X", lambda t: 25 * env(t))
    else:
        c.tilt = lambda t: Quaternion(AXES["X"], math.radians(keys((0, 0), (0.2, -4), (0.62, -8), (1, -14))(t)))
    c.turn_chain(neck, "X", keys((0, 0), (0.25, -18), (1, -25)))
    fold_wings(c, keys((0, 0), (0.3, 1), (1, 1)))
    c.info = {"sinks_m": round(depth, 4), "diggers": [g["foot"] for g in diggers] or [a["upper"] for a in arms],
              "below_ground": "allowed"}
    return c


def levitate() -> Clip:
    """`fly` with no wing chains (giants, efreet, a flying horse): rise to HOVER and float,
    a slow bob and sway, legs hanging and trailing a little, arms held slightly out."""
    c = Clip("fly", 2.0, True)
    c.ground = "hover"
    c.ik = lambda g, t: 0.0
    bob = 0.5 * FLY_BOB[0 if height < 3 else 1]  # half the winged bob
    c.lift = lambda t: Vector((0, 0, bob * math.sin(2 * math.pi * t)))
    lean = -6 if job["upright"] else 3
    c.pivot = Vector((cx, leg_y, 0.6 * height))
    c.tilt = lambda t: Quaternion(AXES["X"], math.radians(lean + 1.5 * math.sin(2 * math.pi * (t - 0.25))))
    for g in legs:
        if job["upright"]:
            ph = 0.0 if g["side"] == "r" else 0.5
            c.turn(g["chain"][0], "X", lambda t, ph=ph: -4 + 3 * math.sin(2 * math.pi * (t - ph)))
            if len(g["chain"]) > 1:
                c.turn(g["chain"][1], "X", lambda t, ph=ph: -14 + 3 * math.sin(2 * math.pi * (t - ph - 0.1)))
        elif job["sprawl"]:
            c.turn(g["chain"][0], "X", lambda t: -6 + 2 * math.sin(2 * math.pi * t))
        else:
            fore = g in front
            c.turn(g["chain"][0], "X", lambda t, fore=fore: (15 if fore else -20) + 2 * math.sin(2 * math.pi * t))
    relax_arms(c)
    for a in arms:
        c.turn(a["upper"], "Y", lambda t, s=a["side"]: sym(s, -10 - 3 * math.sin(2 * math.pi * (t - 0.3))))
        c.turn(a["forearm"], "X", lambda t: 10 + 3 * math.sin(2 * math.pi * (t - 0.4)))
    for i, n in enumerate(tail):
        tail_curl(c, n, lambda t, i=i: 3 * math.sin(2 * math.pi * (t - 0.1 * i)))
    c.turn_chain(neck, "X", lambda t: 2 * math.sin(2 * math.pi * (t - 0.2)))
    c.info = {"wings": {}, "hover_m": round(HOVER, 4), "levitate": True}
    return c


AVIAN_REAR = 0.35   # avian lunge: share of the neck's wind-up rear (and the skull's)


def plan_clips() -> dict:
    """Clip name -> maker(scale); scale < 1 tones down the body motion (see the retry)."""
    sets = {"walk": walk, "idle": lambda s: idle(), "hit": lambda s: hit(), "death": lambda s: death()}
    if arms:
        sets["attack_melee"] = lambda s: attack_melee_armed()
    elif job["sprawl"]:
        sets["attack_melee"] = lambda s: attack_spider()
    elif neck:
        big = height > 3
        # a bird's neck rises between its wings: rearing it the full 25-30 degrees drew the
        # eagle's head back behind the near wing and tore the neck (Godot round 6)
        rear = (30 if big else 25) * (AVIAN_REAR if PLAN == "avian" else 1.0)
        sets["attack_melee"] = lambda s: lunge("attack_melee", 1.1 if big else 0.9, rear,
                                               -25 if big else -22, 0.08, s)
    sets["fly"] = (lambda s: fly()) if wings else lambda s: levitate()
    if neck:
        sets["breath"] = lambda s: breath()
    if "l" in arm_by_side and "r" in arm_by_side:
        sets["attack_ranged"] = lambda s: attack_ranged()
    sets["cast"] = lambda s: cast(scale=s)
    sets["swim"] = lambda s: swim_legged()
    sets["burrow"] = lambda s: burrow()
    return sets


# --- legless plans ----------------------------------------------------------------------
# A body chain from the tail end to the head end (the fallback skeleton's spine_00 ..
# head, or the two leaves farthest apart along the bones), each bone with the side of its
# head joint it turns: +1 when its parent is toward the tail (it turns everything ahead),
# -1 when its parent is toward the head (a bone on the tail side of a mid-body root turns
# everything behind it). A lateral wave is then a curvature wave: the heading change at
# joint s is A sin(2 pi (k s + t)) and each bone takes +-that about world Z, so the shape
# travels from head to tail while the root (mid-body) holds its place and heading.

def find_body() -> dict:
    names = set(by_name)
    sp = sorted((n for n in names if n.startswith("spine_")), key=lambda n: int(n.split("_")[1]))
    if sp and "head" in names and len(sp) >= 3:
        path = sp + ["head"]
    else:
        leaves = [n for n in by_name if not kids[n]]
        plen = lambda pth: sum(rig_topology._dist(by_name[a]["head"], by_name[b]["head"]) for a, b in zip(pth, pth[1:]))
        best = max(((a, b) for i, a in enumerate(leaves) for b in leaves[i + 1:]),
                   key=lambda ab: plen(rig_topology.path_between(ab[0], ab[1], bones)), default=None)
        path = rig_topology.path_between(best[0], best[1], bones) if best else [root]
        end = lambda n: Vector(by_name[n]["tail"])
        if (end(path[0]).z + 0.1 * end(path[0]).y) > (end(path[-1]).z + 0.1 * end(path[-1]).y):
            path = path[::-1]  # the higher (or forward) end is the head
    n = len(path)
    dirs, svals = [], []
    for i, nm in enumerate(path):
        par = by_name[nm]["parent"]
        d = 1 if i > 0 and par == path[i - 1] else -1 if i + 1 < n and par == path[i + 1] else 0
        dirs.append(d)
        svals.append((i + (1 if d < 0 else 0)) / n)
    return {"path": path, "dir": dirs, "s": svals, "head": path[-1]}


def bone_dir(nm: str) -> Vector:
    return Vector(by_name[nm]["tail"]) - Vector(by_name[nm]["head"])


def lateral_axis(nm: str, sign: int) -> Vector:
    """Horizontal axis across a body bone: + turns the head end up (the nose-up of +X for
    a bone pointing +Y)."""
    d = bone_dir(nm) * (sign or 1)
    d.z = 0
    if d.length < 1e-6:
        return AXES["X"]
    return d.normalized().cross(AXES["Z"])


BODY = find_body() if PLAN in ("serpentine", "aquatic") else None


def body_wave(c: Clip, amp, cycles: float, env=lambda s: 1.0, speed: int = 1, skip_root: bool = True,
              phase: float = 0.0) -> None:
    """Lateral curvature wave along the body chain (see above); amp(t) or a number, in
    degrees of heading change per joint. speed 0: a standing wave (the shape sin(2 pi
    cycles s) scaled by amp(t)); `phase` shifts where a travelling wave's loop starts."""
    a = amp if callable(amp) else (lambda t, v=amp: v)
    for nm, d, sv in zip(BODY["path"], BODY["dir"], BODY["s"]):
        if d == 0 and skip_root:
            continue
        c.turn(nm, "Z", lambda t, d=d, sv=sv: (d or 1) * a(t) * env(sv) *
               math.sin(2 * math.pi * (cycles * sv + speed * t + phase)))


def wave_phase(amp: float, cycles: float, env=lambda s: 1.0) -> tuple[float, float]:
    """(phase, metres) for a travelling body_wave loop: the start phase whose first frame
    keeps the chain's joints closest to their rest places, and how far the farthest joint
    still is. A slither loops through every phase, so it can start at any of them; the
    idle's first frame is the rest pose, so this is the start with the smallest idle ->
    walk/swim jump (the snake swim: 1.25 m at phase 0). A flat FK of the chain about Z."""
    path, dirs, svals = BODY["path"], BODY["dir"], BODY["s"]
    n = len(path)
    ri = next((i for i, d in enumerate(dirs) if d == 0), 0)
    h0 = [Vector(by_name[nm]["head"]).to_2d() for nm in path]
    t0 = [Vector(by_name[nm]["tail"]).to_2d() for nm in path]
    best = (0.0, float("inf"))
    for k in range(48):
        ph = k / 48
        h, tl = [v.copy() for v in h0], [v.copy() for v in t0]

        def rot(idx, piv, deg):
            m = Matrix.Rotation(math.radians(deg), 2)
            for arr in (h, tl):
                for j in idx:
                    arr[j] = m @ (arr[j] - piv) + piv
        ang = [d * amp * env(sv) * math.sin(2 * math.pi * (cycles * sv + ph)) for d, sv in zip(dirs, svals)]
        for i in range(ri + 1, n):
            rot(range(i, n), h[i].copy(), ang[i])
        for i in range(ri - 1, -1, -1):
            rot(range(0, i + 1), h[i].copy(), ang[i])
        dev = max(max((a - b).length for a, b in zip(h, h0)), max((a - b).length for a, b in zip(tl, t0)))
        if dev < best[1]:
            best = (ph, dev)
    return best


def body_pitch(c: Clip, fn, s_from: float, s_to: float = 1.01) -> list[str]:
    """Raise (+) or lower (-) the part of the body chain between s_from and s_to: fn(t) is
    the total pitch in degrees, spread over the head-pointing bones in that span."""
    part = [(nm, d) for nm, d, sv in zip(BODY["path"], BODY["dir"], BODY["s"]) if s_from <= sv < s_to and d > 0]
    for nm, d in part:
        c.rot.setdefault(nm, []).append((lateral_axis(nm, d), lambda t: fn(t) / len(part)))
    return [nm for nm, _ in part]


def head_rise() -> float:
    """How high the head end sits above the body's lowest point, and the neck's length."""
    hd = Vector(by_name[BODY["head"]]["head"])
    return hd.z - min(z_all)


# the neck: the front quarter of the chain. A coiled mesh whose coils touch (the pilot
# snake) has whole loops bound to the bones behind that, so pitching more of the chain
# lifts coils into the air.
NECK_S = 0.75


def slither(name: str = "walk", seconds: float = 1.6, amp: float = 14.0, cycles: float = 1.5,
            env=lambda s: 1.0 - 0.6 * smooth((s - 0.75) / 0.25)) -> Clip:
    """In-place slither: a lateral wave runs from head to tail (one period per loop); the
    head itself is steadied (smaller amplitude at the front), the body stays on the ground.
    The loop starts at the phase nearest the rest pose (wave_phase), where the idle starts."""
    c = Clip(name, seconds, True)
    c.ground = "contact"
    ph, dev = wave_phase(amp, cycles, env)
    body_wave(c, amp, cycles, env=env, phase=ph)
    c.info = {"chain": BODY["path"], "wave_amp_deg": amp, "wave_cycles": cycles, "wave_phase": round(ph, 4),
              "start_dev_m": round(dev, 3)}
    return c


def swim_serpent() -> Clip:
    """Swim: the slither with one and a half waves on the body whose amplitude grows toward
    the tail (the head steady, the tail sculling), on the ground like the walk (the game
    places the water line). It was 18 degrees per joint over one wave: the tail swung 2 m
    on the 4 m snake and its first frame was 1.25 m from the idle's."""
    return slither("swim", 2.0, 16.0, 1.5, env=lambda s: 0.5 + 0.5 * (1 - s))


def idle_serpent() -> Clip:
    """Rest on the ground, breathing: a standing side-to-side flex of the body (not a
    travelling wave, so the first frame is the rest pose, where the one-shots start), the
    front quarter lifting a little, the head looking about."""
    c = Clip("idle", 3.0, True)
    c.ground = "contact"
    body_wave(c, lambda t: 3.0 * math.sin(2 * math.pi * t), 1.0, speed=0)
    body_pitch(c, lambda t: 4 * math.sin(2 * math.pi * t), NECK_S)
    c.turn(BODY["head"], "Z", wave(8.0, 1, 0.5))
    c.turn(BODY["head"], "X", wave(4.0, 2, 0.0))
    return c


STRIKE_S = 2 / 3    # the strike rears the front third of the body chain
STRIKE_T = (0, 0.38, 0.5, 0.62, 1.0)  # rest, wound up, struck, held, rest


def strike_serpent() -> Clip:
    """The front third rears up in an S (its base pitched up, the front half back down so
    the head looks ahead), the middle coils sideways and the body draws back as the wind-up;
    then the strike: the front third straightens forward and down to the ground in front,
    the coil straightens and the body slides forward, hold the bite, recover. Sizes are
    fractions of the body length, so the head travels about 0.3 x it from the wind-up to
    the bite whatever the snake's size (1.3 m on the 4 m pilot); the rear stays on the
    ground (`contact`)."""
    c = Clip("attack_melee", 1.0, False)
    c.ground = "contact"
    mid_s = STRIKE_S + 0.5 * (1.0 - STRIKE_S)
    base = body_pitch(c, keys(*zip(STRIKE_T, (0, 75, -8, -8, 0))), STRIKE_S, mid_s)
    front = body_pitch(c, keys(*zip(STRIKE_T, (0, -40, -6, -6, 0))), mid_s)
    c.turn(BODY["head"], "X", keys(*zip(STRIKE_T, (0, -10, 4, 4, 0))))
    # the coil: a sideways S on each side of the mid-body root, between 0.3 of the chain
    # and the strike span; each side's turns add up to nothing, so the tail keeps its
    # heading (it is drawn in sideways a little) and the front still points ahead
    coil = keys(*zip(STRIKE_T, (0, 1, 0, 0, 0)))
    for side in (-1, 1):
        part = [nm for nm, d, sv in zip(BODY["path"], BODY["dir"], BODY["s"]) if d == side and 0.3 <= sv < STRIKE_S]
        for j, nm in enumerate(part):
            amt = side * 16 * math.sin(2 * math.pi * (j + 0.5) / len(part))
            c.turn(nm, "Z", lambda t, amt=amt: amt * coil(t))
    fwd = Vector((0, 1, 0))
    if base:
        hd = bone_dir(base[0])
        hd.z = 0
        fwd = hd.normalized() if hd.length > 1e-6 else fwd
    slide = keys(*zip(STRIKE_T, (0, -0.04, 0.08, 0.08, 0)))
    c.lift = lambda t: fwd * body_len * slide(t)
    c.info = {"neck": base + front, "strike_span": round(1 - STRIKE_S, 3), "slide_m": round(0.12 * body_len, 3)}
    return c


def hit_serpent() -> Clip:
    c = Clip("hit", 0.5, False)
    c.ground = "contact"
    k = keys((0, 0), (0.18, 1), (1, 0))
    body_pitch(c, lambda t: 20 * k(t), NECK_S)
    body_wave(c, lambda t: 6 * k(t), 0.5)
    return c


def death_serpent() -> Clip:
    """The raised front slumps to the ground and the body goes slack with a last twitch;
    the last frame lies on the ground and holds."""
    c = Clip("death", 1.8, False)
    c.ground, c.rest_from = "rest", 0.75
    neck_part = [(nm, d, sv) for nm, d, sv in zip(BODY["path"], BODY["dir"], BODY["s"]) if sv >= NECK_S and d > 0]
    # enough pitch for the head to come down to the body's height: asin(rise / neck length)
    if neck_part:
        base = Vector(by_name[neck_part[0][0]]["head"])
        tip = Vector(by_name[BODY["head"]]["tail"])
        rise = tip.z - min(z_all) - 0.15 * (tip - base).length
        drop_deg = min(60.0, math.degrees(math.asin(max(0.0, min(1.0, rise / max((tip - base).length, 1e-6))))))
    else:
        drop_deg = 0.0
    fall = keys((0, 0), (0.15, 12), (0.65, -drop_deg - 6), (0.75, -drop_deg), (1, -drop_deg))
    body_pitch(c, fall, NECK_S)
    body_wave(c, lambda t: 8 * math.sin(2 * math.pi * 2 * t) * (1 - smooth((t - 0.2) / 0.5)), 1.0)
    c.info = {"head_drop_deg": round(drop_deg, 1)}
    return c


def cast_serpent() -> Clip:
    """Rear the front quarter up, hold with a small sway, thrust the head forward on the
    release, recover (the head-cast timing, CAST_T)."""
    c = Clip("cast", 1.2, False)
    c.ground = "contact"
    body_pitch(c, keys(*zip(CAST_T, (0, 40, 40, -8, -8, 0))), NECK_S)
    c.turn(BODY["head"], "X", keys(*zip(CAST_T, (0, 12, 12, -10, -10, 0))))
    c.turn(BODY["head"], "Z", lambda t: 4 * math.sin(2 * math.pi * 3 * smooth((t - 0.3) / 0.28)) if 0.3 < t < 0.58 else 0)
    c.info = {"cast": "head"}
    return c


def breath_serpent() -> Clip:
    """The breath timing on the body chain: rear the front quarter, thrust it forward and
    down, hold ~1 s, recover."""
    c = Clip("breath", 2.2, False)
    c.ground = "contact"
    body_pitch(c, keys((0, 0), (0.22, 30), (0.32, -8), (0.82, -8), (1, 0)), NECK_S)
    c.turn(BODY["head"], "X", keys((0, 0), (0.22, 10), (0.32, -6), (0.82, -6), (1, 0)))
    c.turn(BODY["head"], "Z", lambda t: 3 * math.sin(2 * math.pi * 3 * smooth((t - 0.32) / 0.5)))
    return c


def burrow_serpent() -> Clip:
    """Dive: the head turns down into the ground and the body follows it forward and down,
    half the height sunk at the end (ground `none`: below the ground is allowed)."""
    c = Clip("burrow", 1.5, False)
    c.ground = "none"
    body_pitch(c, keys((0, 0), (0.3, -35), (1, -40)), NECK_S)
    body_wave(c, lambda t: 8 * smooth(t / 0.3), 1.0)
    k = keys((0, 0), (0.3, 0.2), (1, 1))
    c.lift = lambda t: Vector((0, 0.08 * body_len * k(t), -BURROW_SINK * height * keys((0, 0), (0.35, 0), (1, 1))(t)))
    c.info = {"sinks_m": round(BURROW_SINK * height, 4), "below_ground": "allowed"}
    return c


def fly_serpent() -> Clip:
    """A lifted slither (couatl, flying snake): the swim wave held HOVER above the ground."""
    c = slither("fly", 2.0, 12.0, 1.0)
    c.ground, c.hover = "lift", HOVER
    c.lift = lambda t: Vector((0, 0, 0.02 * height * math.sin(2 * math.pi * t)))
    c.info["lift_m"] = round(HOVER, 4)
    return c


# floating (ghost, wraith, specter, elementals): a humanoid upper body over a robe or a
# tail of mist. SkinTokens rigs the ghost like a biped under its robe (legs to the hem);
# every clip hovers (ground "hover": the lowest vertex FLOAT x height above the ground
# over the cycle), the hem chains trail and sway, death sinks onto the ground face down.
FLOAT = 0.1
HEM_FOLD = 0.15     # death: hem bones whose joint is this low (x height) fold the train flat
hem = rig_topology.find_legs(bones, height, 4, False) if PLAN == "floating" else []


def hover_clip(name: str, seconds: float, loop: bool) -> Clip:
    c = Clip(name, seconds, loop)
    c.ground, c.hover = "lift", FLOAT * height
    c.pivot = Vector((cx, leg_y, 0.5 * height))
    return c


def hem_sway(c: Clip, amp: float, back: float = 0.0, zero: bool = False) -> None:
    """The hem chains trail (`back`) and sway; `zero`: shifted so the first frame is the
    rest pose (the idle and the one-shots, so they meet where the one-shots start)."""
    for i, g in enumerate(hem):
        f0 = lambda t, i=i: amp * math.sin(2 * math.pi * (t - 0.15 - 0.1 * i))
        f1 = lambda t, i=i: 0.6 * amp * math.sin(2 * math.pi * (t - 0.3 - 0.1 * i))
        z0, z1 = (f0(0.0), f1(0.0)) if zero else (0.0, 0.0)
        c.turn(g["chain"][0], "X", lambda t, f0=f0, z0=z0: -back + f0(t) - z0)
        if len(g["chain"]) > 1:
            c.turn(g["chain"][1], "X", lambda t, f1=f1, z1=z1: -0.5 * back + f1(t) - z1)


def from_rest(fn):
    """fn(t) - fn(0): a loop layer shifted so its first frame is the rest pose."""
    v0 = fn(0.0)
    return lambda t: fn(t) - v0


def idle_float() -> Clip:
    """Hover, breathing, looking about. Every layer is 0 on the first frame (from_rest), so
    the idle starts on the pose the one-shots start on: hit and death started on the rest
    pose while the idle's hem sway had lifted the robe train 0.07 m (idle -> hit 0.10 m)."""
    c = hover_clip("idle", 3.0, True)
    bob = 0.025 * height
    c.lift = lambda t: Vector((0, 0, bob * math.sin(2 * math.pi * t)))
    c.turn_chain(torso, "X", from_rest(wave(3.0, 1, 0.1)))
    c.turn_chain(neck[:-1] or neck, "Z", from_rest(wave(8.0, 1, 0.1)))
    c.turn(skull, "X", from_rest(wave(4.0, 2, 0.2)))
    relax_arms(c)
    for a in arms:
        c.turn(a["upper"], "X", from_rest(wave(5.0, 1, 0.3)))
        c.turn(a["forearm"], "X", from_rest(lambda t: 4.0 + 4.0 * math.sin(2 * math.pi * (t - 0.4))))
    hem_sway(c, 4.0, zero=True)
    return c


def fly_float() -> Clip:
    """Glide in place: lean into the travel, hem trailing back, a slow bob."""
    c = hover_clip("fly", 2.0, True)
    bob = 0.03 * height
    c.lift = lambda t: Vector((0, 0, bob * math.sin(2 * math.pi * t)))
    c.tilt = lambda t: Quaternion(AXES["X"], math.radians(-14 + 2 * math.sin(2 * math.pi * (t - 0.2))))
    c.turn_chain(neck, "X", lambda t: 10.0)
    relax_arms(c)
    for a in arms:
        c.turn(a["upper"], "X", lambda t: -15 + 4 * math.sin(2 * math.pi * (t - 0.3)))
    hem_sway(c, 6.0, back=12.0)
    return c


def swim_float() -> Clip:
    """Swim in place (water elemental): lean forward into the stroke, both arms sweep out and
    back together (a breaststroke), the hem kicks, a slow bob; held like every floating clip."""
    c = hover_clip("swim", 1.8, True)
    bob = 0.03 * height
    c.lift = lambda t: Vector((0, 0, bob * math.sin(2 * math.pi * t)))
    c.tilt = lambda t: Quaternion(AXES["X"], math.radians(-25 + 3 * math.sin(2 * math.pi * (t - 0.2))))
    c.turn_chain(neck, "X", lambda t: 15.0)
    relax_arms(c)
    for a in arms:
        c.turn(a["upper"], "X", lambda t: 40 + 25 * math.sin(2 * math.pi * t))
        c.turn(a["upper"], "Y", lambda t, s=a["side"]: sym(s, -20 - 20 * math.cos(2 * math.pi * t)))
    hem_sway(c, 10.0, back=10.0)
    return c


def attack_float() -> Clip:
    """Lunge with both arms reaching forward, clawing, recover."""
    c = hover_clip("attack_melee", 1.0, False)
    k = keys((0, 0), (0.3, -0.3), (0.5, 1), (0.65, 1), (1, 0))
    c.lift = lambda t: Vector((0, 0.12 * height * k(t), 0.02 * height * k(t)))
    c.tilt = lambda t: Quaternion(AXES["X"], math.radians(-12 * max(0.0, k(t)) + 6 * min(0.0, k(t)) * -1))
    reach = keys((0, 0), (0.3, 0.6), (0.5, 1), (0.65, 1), (1, 0))
    for a in arms:
        sx = 1 if a["side"] == "r" else -1
        aim_arm(c, a, lambda t, sx=sx: Vector((0.25 * sx, 1, 0.25)).normalized(),
                lambda t, sx=sx: Vector((0.05 * sx, 1, 0.0 - 0.3 * reach(t))).normalized(), reach)
    c.turn(skull, "X", keys((0, 0), (0.3, 10), (0.5, -8), (1, 0)))
    hem_sway(c, 3.0, back=0.0, zero=True)
    for g in hem:
        c.turn(g["chain"][0], "X", lambda t: -15 * max(0.0, k(t)))
    return c


def hit_float() -> Clip:
    c = hover_clip("hit", 0.5, False)
    k = keys((0, 0), (0.18, 1), (1, 0))
    c.lift = lambda t: Vector((0, -0.05 * height * k(t), 0.02 * height * k(t)))
    c.tilt = lambda t: Quaternion(AXES["X"], math.radians(10 * k(t)))
    c.turn_chain(torso, "X", lambda t: 10 * k(t))
    c.turn_chain(neck, "X", lambda t: 12 * k(t))
    relax_arms(c)
    for a in arms:
        c.turn(a["upper"], "Y", lambda t, s=a["side"]: sym(s, -15 * k(t)))
        c.turn(a["forearm"], "X", lambda t: 20 * k(t))
    for g in hem:
        c.turn(g["chain"][0], "X", lambda t: 12 * k(t))
    return c


def death_float() -> Clip:
    """Sag, then sink and fall forward onto the ground, arms limp; holds lying."""
    c = Clip("death", 2.0, False)
    c.ground, c.rest_from = "rest", 0.75
    front_y = max(p[1] for b in bones for p in (b["head"], b["tail"]))
    c.pivot = Vector((cx, front_y, 0.0))
    fall = lambda t: -85 * ease_in((t - 0.3) / 0.45) + 5 * math.sin(math.pi * smooth((t - 0.75) / 0.15))
    c.tilt = lambda t: Quaternion(AXES["X"], math.radians(fall(t)))
    c.lift = lambda t: Vector((0, 0, 0.15 * height * (1 - smooth(t / 0.3)) - 0.05 * height * smooth((t - 0.2) / 0.4)))
    c.turn_chain(torso, "X", keys((0, 0), (0.3, -15), (0.7, 5), (1, 5)))
    c.turn_chain(neck, "X", keys((0, 0), (0.3, -20), (0.8, 15), (1, 15)))
    relax_arms(c, keys((0, 1), (0.5, 0)))
    for a in arms:
        c.turn(a["upper"], "X", keys((0, 0), (0.3, 30), (0.8, 60), (1, 60)))
        c.turn(a["forearm"], "X", keys((0, 0), (0.4, 20), (1, 20)))
    fold = []
    for g in hem:
        c.turn(g["chain"][0], "X", keys((0, 0), (0.5, -8), (1, -8)))
        # the train spreads flat on the floor behind the feet at rest, square to the body:
        # carried through the fall it would stand up as a sheet behind the lying body, so
        # the hem's first low bone folds back against the fall and the train stays flat
        low = next((n for n in g["chain"][1:] if by_name[n]["head"][2] < HEM_FOLD * height), None)
        if low:
            c.turn(low, "X", lambda t: -fall(t))
            fold.append(low)
    c.info = {"falls": "forward", "hem_fold": fold}
    return c


# amorphous (oozes) and other vertical-chain bodies (blob fallback: spine_00 at the base is
# the root, the chain runs up the middle). Squash and stretch is the root's scale (local Y
# is up, so (w, h, w) widens and flattens about the base); leaning is turning the chain
# about X (- leans forward: a vertical bone's tip goes to +Y). Every clip keeps the lowest
# vertex on the ground ("contact"); death flattens into a puddle and holds.
STACK = sorted((n for n in by_name if n.startswith("spine_")), key=lambda n: int(n.split("_")[1])) \
    if PLAN in ("amorphous",) else []


def vscale(name: str, h: float, v: float) -> Vector:
    """A pose-bone scale (bone-local axes) that is `v` along world up and `h` across it:
    the glTF importer's bone axes need not have local Y up, so the local axis closest to
    world Z takes the vertical factor."""
    m = (world @ arm.data.bones[name].matrix_local).to_3x3()
    i = max(range(3), key=lambda k: abs(m.col[k].z))
    out = [h, h, h]
    out[i] = v
    return Vector(out)


def squash(c: Clip, fn) -> None:
    """fn(t) -> (stretch): 1 = rest, < 1 squashes (wider, lower), > 1 stretches (narrower, taller)."""
    def sc(t):
        k = fn(t)
        return vscale(STACK[0], k ** -0.3, k)  # less than volume-preserving: a puddle 1.5x wide, not 2x
    c.scale[STACK[0]] = sc


def lean(c: Clip, axis: str, fn, lag: float = 0.0) -> None:
    c.turn_chain(STACK[1:], axis, fn, lag)


def blob_clip(name: str, seconds: float, loop: bool) -> Clip:
    c = Clip(name, seconds, loop)
    c.ground = "contact"
    c.pivot = Vector(by_name[STACK[0]]["head"])
    return c


def idle_blob() -> Clip:
    c = blob_clip("idle", 2.4, True)
    squash(c, lambda t: 1 + 0.05 * math.sin(2 * math.pi * t))
    lean(c, "X", wave(4.0, 1, 0.25), 0.05)
    lean(c, "Y", wave(3.0, 1, 0.0), 0.05)
    return c


def walk_blob() -> Clip:
    """Ooze in place: the top surges forward and back over the base twice per loop while
    the body pulses (stretch as it leans in, squash as it catches up)."""
    c = blob_clip("walk", 1.6, True)
    squash(c, lambda t: 1 + 0.08 * math.sin(2 * math.pi * (2 * t)))
    lean(c, "X", lambda t: -14 * math.sin(2 * math.pi * (2 * t - 0.15)), 0.04)
    lean(c, "Y", wave(4.0, 1, 0.0), 0.05)
    c.info = {"chain": STACK}
    return c


def attack_blob() -> Clip:
    """Engulf: rear up tall and back, surge forward over the target (flattening onto it),
    hold, recover."""
    c = blob_clip("attack_melee", 1.2, False)
    squash(c, keys((0, 1), (0.35, 1.3), (0.55, 0.75), (0.7, 0.75), (1, 1)))
    lean(c, "X", keys((0, 0), (0.35, 15), (0.55, -45), (0.7, -45), (1, 0)))
    k = keys((0, 0), (0.35, -0.3), (0.55, 1), (0.7, 1), (1, 0))
    c.lift = lambda t: Vector((0, 0.12 * mesh_len * k(t), 0))
    return c


def hit_blob() -> Clip:
    c = blob_clip("hit", 0.5, False)
    k = keys((0, 0), (0.15, 1), (0.45, -0.4), (1, 0))
    squash(c, lambda t: 1 - 0.18 * k(t))
    lean(c, "X", lambda t: 12 * max(0.0, k(t)))
    return c


def death_blob() -> Clip:
    """Shudder, then slump flat into a puddle; holds."""
    c = blob_clip("death", 1.8, False)
    flat = keys((0, 1), (0.2, 1.1), (0.75, 0.22), (0.85, 0.27), (1, 0.25))
    squash(c, lambda t: flat(t) + 0.04 * math.sin(2 * math.pi * 4 * t) * (1 - smooth(t / 0.3)))
    lean(c, "X", keys((0, 0), (0.2, 6), (0.75, -10), (1, -8)))
    return c


# plant_object (fungi, shrubs, trees): a stalk from the root to the highest bone near the
# centre line, and limbs (tendrils, branches) = every other chain hanging off the stalk.
# Rooted: "contact" keeps the base on the ground; death bends the stalk over and holds.
def find_plant() -> tuple[list[str], list[list[str]]]:
    if STACK:
        return STACK, []
    central = [b for b in bones if abs((b["head"][0] + b["tail"][0]) / 2 - cx) < 0.15 * height] or bones
    top = max(central, key=lambda b: b["tail"][2])["name"]
    stalk = rig_topology.path_to_root(top, bones)
    on = set(stalk)
    limbs = []
    for n in stalk:
        for k in kids[n]:
            if k in on:
                continue
            ch = rig_topology.longest_chain(k, kids)
            tip = Vector(by_name[ch[-1]]["tail"])
            if len(ch) >= 2 or (tip - Vector(by_name[k]["head"])).length > 0.15 * height:
                limbs.append(ch)
    return stalk, limbs


if PLAN == "plant_object":
    if not STACK:
        STACK = sorted((n for n in by_name if n.startswith("spine_")), key=lambda n: int(n.split("_")[1]))
    STALK, LIMBS = find_plant()
else:
    STALK, LIMBS = [], []


def limb_out(ch: list[str]) -> Vector:
    """Horizontal direction a limb points away from the stalk."""
    d = Vector(by_name[ch[-1]]["tail"]) - Vector(by_name[STALK[0]]["head"])
    d.z = 0
    return d.normalized() if d.length > 1e-6 else AXES["Y"]


def limb_turn(c: Clip, ch: list[str], fn) -> None:
    """+ lifts the limb up and out (about the horizontal axis across its direction)."""
    ax = limb_out(ch).cross(AXES["Z"]) * -1
    for n in ch:
        c.rot.setdefault(n, []).append((ax, lambda t: fn(t) / len(ch)))


def plant_clip(name: str, seconds: float, loop: bool) -> Clip:
    c = Clip(name, seconds, loop)
    c.ground = "contact"
    c.pivot = Vector(by_name[STALK[0]]["head"])
    return c


def stalk_turn(c: Clip, axis: str, fn, lag: float = 0.04) -> None:
    c.turn_chain([n for n in STALK if n != root], axis, fn, lag)


def idle_plant() -> Clip:
    c = plant_clip("idle", 3.0, True)
    stalk_turn(c, "X", wave(4.0, 1, 0.0))
    stalk_turn(c, "Y", wave(3.0, 1, 0.25))
    for i, ch in enumerate(LIMBS):
        limb_turn(c, ch, lambda t, i=i: 10 * math.sin(2 * math.pi * (t - 0.17 * i)))
    return c


def walk_plant() -> Clip:
    """Shuffle in place: the stalk rocks side to side and nods, limbs drag along."""
    c = plant_clip("walk", 1.4, True)
    stalk_turn(c, "Y", wave(7.0, 1, 0.0))
    stalk_turn(c, "X", lambda t: -4 + 4 * math.sin(2 * math.pi * 2 * t))
    if STACK and not LIMBS:
        c.scale[STALK[0]] = lambda t: vscale(STALK[0], 1, 1 + 0.04 * math.sin(2 * math.pi * 2 * t))
    for i, ch in enumerate(LIMBS):
        limb_turn(c, ch, lambda t, i=i: 8 * math.sin(2 * math.pi * (t - 0.25 * i)))
    return c


def attack_plant() -> Clip:
    """Coil back, then whip the stalk and every limb forward; recover."""
    c = plant_clip("attack_melee", 1.0, False)
    stalk_turn(c, "X", keys((0, 0), (0.35, 12), (0.5, -25), (0.62, -25), (1, 0)), 0.0)
    for ch in LIMBS:
        fwd = limb_out(ch).y
        limb_turn(c, ch, keys((0, 0), (0.35, 35), (0.5, -20 if fwd > 0 else 40), (0.62, -20 if fwd > 0 else 40), (1, 0)))
    return c


def hit_plant() -> Clip:
    c = plant_clip("hit", 0.5, False)
    k = keys((0, 0), (0.18, 1), (0.5, -0.3), (1, 0))
    stalk_turn(c, "X", lambda t: 14 * k(t), 0.0)
    for ch in LIMBS:
        limb_turn(c, ch, lambda t: 15 * k(t))
    return c


def death_plant() -> Clip:
    """Wilt: the stalk sags, then topples over to its right onto the ground; limbs go limp.
    Holds. The root bone owns the base clump (the vertical fallback weights it alone up to
    about a fifth of the height) and stays planted: the stalk breaks over at the bones
    above it, by the angle that brings the cap down onto the ground (`fit`, found in
    build()), so the base never stands up on its side."""
    c = plant_clip("death", 2.0, False)
    c.ground = "none"
    over = [n for n in STALK if n != root][:2]
    share = [0.7, 0.3][:len(over)]
    if not over:  # a stalk of one bone: the whole plant tips over about its base
        c.ground, c.rest_from = "rest", 0.8
        c.pivot = Vector((cx + 0.2 * height, by_name[STALK[0]]["head"][1], 0))
    fit = {"deg": 85.0}
    fall = lambda t: fit["deg"] * ease_in((t - 0.3) / 0.45) - 4 * math.sin(math.pi * smooth((t - 0.75) / 0.15))
    if over:
        for n, k in zip(over, share):
            c.turn(n, "Y", lambda t, k=k: k * fall(t))
        c.fit = {"set": lambda v: fit.__setitem__("deg", v), "lo": 20.0, "hi": 130.0, "bone": root}
    else:
        c.tilt = lambda t: Quaternion(AXES["Y"], math.radians(fall(t)))
    stalk_turn(c, "X", keys((0, 0), (0.3, -12), (1, -12)), 0.0)
    for ch in LIMBS:
        limb_turn(c, ch, keys((0, 0), (0.5, -25), (1, -25)))
    c.info = {"falls_on": "right", "breaks_at": over[:1]}
    return c


# tentacled (octopus, kraken, roper, otyugh): the tentacle-hub fallback skeleton, root +
# body + tent_<j>_<k> chains. A tentacle turns about the horizontal axis across its own
# direction (+ curls it up and out). Ground-bound clips keep the lowest vertex on the
# ground; swim hovers FLOAT x height above it with the tentacles pulsing together.
TENTS = []
if PLAN == "tentacled":
    _t: dict[int, list] = {}
    for n in by_name:
        if n.startswith("tent_"):
            _, j, k = n.split("_")
            _t.setdefault(int(j), []).append((int(k), n))
    TENTS = [[n for _, n in sorted(v)] for _, v in sorted(_t.items())]


def tent_out(ch: list[str]) -> Vector:
    d = Vector(by_name[ch[-1]]["tail"]) - Vector(by_name[root]["head"])
    d.z = 0
    return d.normalized() if d.length > 1e-6 else AXES["Y"]


def tent_turn(c: Clip, ch: list[str], fn, lag: float = 0.0) -> None:
    ax = -tent_out(ch).cross(AXES["Z"])
    for i, n in enumerate(ch):
        c.rot.setdefault(n, []).append((ax, (lambda t, i=i: fn(t - lag * i) / len(ch)) if lag else
                                        (lambda t: fn(t) / len(ch))))


def tent_clip(name: str, seconds: float, loop: bool, ground: str = "contact") -> Clip:
    c = Clip(name, seconds, loop)
    c.ground = ground
    c.hover = FLOAT * height
    c.pivot = Vector(by_name[root]["head"])
    return c


def idle_tent() -> Clip:
    c = tent_clip("idle", 3.0, True)
    for i, ch in enumerate(TENTS):
        tent_turn(c, ch, lambda t, i=i: 14 * math.sin(2 * math.pi * (t - i / max(1, len(TENTS)))), 0.06)
    c.turn("body", "X", wave(4.0, 1, 0.1))
    c.scale[root] = lambda t: vscale(root, 1, 1 + 0.04 * math.sin(2 * math.pi * t))
    return c


def swim_tent(name: str = "swim") -> Clip:
    """Jet in place: tentacles open up and out, then sweep down together; the body bobs."""
    c = tent_clip(name, 1.6, True, "lift")
    pulse = lambda t: 22 * math.sin(2 * math.pi * t)
    for ch in TENTS:
        tent_turn(c, ch, pulse, 0.05)
    c.lift = lambda t: Vector((0, 0, 0.04 * height * math.sin(2 * math.pi * (t - 0.25))))
    c.turn("body", "X", lambda t: -6 * math.sin(2 * math.pi * (t - 0.1)))
    return c


def crawl_tent() -> Clip:
    """Walk for tentacle crawlers: a travelling wave of curls around the hub."""
    c = tent_clip("walk", 1.6, True)
    for i, ch in enumerate(TENTS):
        tent_turn(c, ch, lambda t, i=i: 18 * math.sin(2 * math.pi * (t - 2 * i / max(1, len(TENTS)))), 0.05)
    c.turn("body", "Y", wave(4.0, 1, 0.0))
    return c


def front_tents(n: int = 2) -> list:
    return sorted(TENTS, key=lambda ch: -by_name[ch[-1]]["tail"][1])[:n]


def attack_tent() -> Clip:
    """The two front tentacles rear up and back, then lash forward and down; hold, recover."""
    c = tent_clip("attack_melee", 1.1, False)
    for ch in front_tents():
        tent_turn(c, ch, keys((0, 0), (0.35, 70), (0.5, -35), (0.65, -35), (1, 0)), 0.03)
    c.turn("body", "X", keys((0, 0), (0.35, 8), (0.5, -10), (0.65, -10), (1, 0)))
    c.info = {"lash": [ch[0] for ch in front_tents()]}
    return c


def hit_tent() -> Clip:
    c = tent_clip("hit", 0.5, False)
    k = keys((0, 0), (0.18, 1), (1, 0))
    for ch in TENTS:
        tent_turn(c, ch, lambda t: 20 * k(t))
    c.turn("body", "X", lambda t: 12 * k(t))
    c.scale[root] = lambda t: vscale(root, 1 + 0.08 * k(t), 1 - 0.12 * k(t))
    return c


def death_tent() -> Clip:
    """Tentacles splay out flat, the body slumps and flattens; holds."""
    c = tent_clip("death", 1.8, False, "rest")
    c.rest_from = 0.75
    for i, ch in enumerate(TENTS):
        tent_turn(c, ch, keys((0, 0), (0.2, 20), (0.7, -12), (1, -12)))
    c.scale[root] = lambda t: vscale(root, 1 + 0.25 * smooth((t - 0.2) / 0.55), 1 - 0.45 * smooth((t - 0.2) / 0.55))
    c.turn("body", "X", keys((0, 0), (0.3, 10), (0.8, -25), (1, -25)))
    return c


# aquatic (sharks, whales, fish): the body chain swims with a carangiform wave (small at
# the head, growing toward the tail), in place and held SWIM_LIFT x height above the
# ground in every clip ("lift"); death rolls belly-up and sinks onto the ground.
SWIM_LIFT = 0.25


def fish_clip(name: str, seconds: float, loop: bool) -> Clip:
    c = Clip(name, seconds, loop)
    c.ground, c.hover = "lift", SWIM_LIFT * height
    c.pivot = Vector(by_name[root]["head"])
    return c


def tail_env(s: float) -> float:
    return 0.25 + 0.75 * (1 - s) ** 1.5


def swim_fish(name: str = "swim", seconds: float = 1.4, amp: float = 15.0) -> Clip:
    c = fish_clip(name, seconds, True)
    body_wave(c, amp, 0.8, env=tail_env)
    c.lift = lambda t: Vector((0, 0, 0.01 * height * math.sin(2 * math.pi * 2 * t)))
    c.info = {"chain": BODY["path"], "wave_amp_deg": amp}
    return c


def attack_fish() -> Clip:
    """Draw back, then dart forward with the head snapping up and down for the bite, a
    tail kick; hold the bite, recover."""
    c = fish_clip("attack_melee", 1.0, False)
    k = keys((0, 0), (0.3, -0.4), (0.48, 1), (0.62, 1), (1, 0))
    c.lift = lambda t: Vector((0, 0.1 * body_len * k(t), 0))
    body_pitch(c, keys((0, 0), (0.3, 12), (0.45, 18), (0.52, -10), (0.62, -6), (1, 0)), NECK_S)
    body_wave(c, lambda t: 10 * keys((0, 0), (0.3, 1), (0.5, -1), (0.7, 0.3), (1, 0))(t), 0.5, env=tail_env)
    return c


def hit_fish() -> Clip:
    c = fish_clip("hit", 0.5, False)
    k = keys((0, 0), (0.18, 1), (1, 0))
    c.lift = lambda t: Vector((0, -0.04 * body_len * k(t), 0.02 * height * k(t)))
    body_wave(c, lambda t: 8 * k(t), 0.5)
    body_pitch(c, lambda t: 10 * k(t), NECK_S)
    return c


def death_fish() -> Clip:
    """Thrash, roll belly-up and sink onto the ground; holds."""
    c = fish_clip("death", 2.2, False)
    c.ground, c.rest_from = "rest", 0.8
    c.pivot = Vector((cx, leg_y, 0.5 * height))
    roll = lambda t: 175 * smooth((t - 0.2) / 0.5)
    c.tilt = lambda t: Quaternion(AXES["Y"], math.radians(roll(t)))
    c.lift = lambda t: Vector((0, 0, SWIM_LIFT * height * (1 - smooth((t - 0.3) / 0.5))))
    body_wave(c, lambda t: 12 * math.sin(2 * math.pi * 2 * t) * (1 - smooth(t / 0.45)), 0.6, env=tail_env)
    c.info = {"rolls": "belly-up"}
    return c


# merfolk_naga (merfolk, merrow, salamander, marilith): a humanoid upper body (arms, torso,
# head as for bipeds) on a tail: the chain from the hips to the lowest leaf. The tail
# kicks in the body's vertical plane (about X) to swim, and sways sideways (about Z) to
# crawl; the arms reuse the biped sword cut. The body rests on its tail ("contact").
def find_fishtail() -> list[str]:
    taken = {n for a in arms for n in a["chain"]} | set(neck) | ({head} if head else set())
    leaves = [n for n in by_name if not kids[n] and n not in taken]
    if not leaves:
        return []
    low = min(leaves, key=lambda n: by_name[n]["tail"][2])
    upper = set(spine) | set(neck)
    return [n for n in rig_topology.path_to_root(low, bones) if n not in upper]


FISHTAIL = find_fishtail() if PLAN == "merfolk_naga" else []


def naga_clip(name: str, seconds: float, loop: bool, ground: str = "contact") -> Clip:
    c = Clip(name, seconds, loop)
    c.ground, c.hover = ground, FLOAT * height
    c.pivot = Vector(by_name[root]["head"])
    return c


def tail_kick(c: Clip, amp, axis: str = "X", cycles: float = 0.8, speed: int = 1) -> None:
    a = amp if callable(amp) else (lambda t, v=amp: v)
    n = max(1, len(FISHTAIL))
    for i, nm in enumerate(FISHTAIL):
        sv = i / n
        c.turn(nm, axis, lambda t, sv=sv: a(t) * (0.4 + 0.6 * sv) * math.sin(2 * math.pi * (speed * t - cycles * sv)))


def idle_naga() -> Clip:
    c = naga_clip("idle", 3.0, True)
    tail_kick(c, 4.0, "Z", 0.6)
    c.turn_chain(torso, "X", wave(2.0, 1, 0.0))
    c.turn_chain(neck, "Z", wave(6.0, 1, 0.1))
    relax_arms(c)
    for a in arms:
        c.turn(a["upper"], "X", wave(4.0, 1, 0.3))
        c.turn(a["forearm"], "X", lambda t: 5 + 4 * math.sin(2 * math.pi * (t - 0.4)))
    return c


def swim_naga() -> Clip:
    """Swim in place: the body pitches forward toward horizontal, the tail kicks up and
    down, the arms sweep back along the sides; held above the ground."""
    c = naga_clip("swim", 1.4, True, "lift")
    c.pivot = Vector((cx, leg_y, 0.5 * height))
    c.tilt = lambda t: Quaternion(AXES["X"], math.radians(-55 + 3 * math.sin(2 * math.pi * t)))
    tail_kick(c, 18.0, "X", 0.8)
    c.turn_chain(neck, "X", lambda t: 30.0)
    relax_arms(c)
    for a in arms:  # a held prop: steadier (arm_swing)
        arm_swing(c, a, lambda t: -30.0, lambda t: 15 * math.sin(2 * math.pi * (t - 0.2)))
        c.turn(a["upper"], "Y", lambda t, s=a["side"]: sym(s, 20.0))
    return c


def crawl_naga() -> Clip:
    """Walk for nagas and salamanders: the tail slithers sideways, the torso counter-sways."""
    c = naga_clip("walk", 1.6, True)
    tail_kick(c, 22.0, "Z", 1.2)
    c.turn_chain(torso, "Z", wave(5.0, 1, 0.5))
    relax_arms(c)
    for a in arms:
        arm_swing(c, a, lambda t: 0.0, lambda t, s=a["side"]: (1 if s == "r" else -1) * 10 * math.sin(2 * math.pi * t))
    return c


def attack_naga(name: str = "attack_melee") -> Clip:
    c = attack_melee_armed() if name == "attack_melee" else attack_throw()
    c.name, c.ground = name, "contact"
    tail_kick(c, lambda t: 8 * keys((0, 0), (0.3, 1), (0.6, -1), (1, 0))(t), "Z", 0.5)
    return c


def hit_naga() -> Clip:
    c = naga_clip("hit", 0.5, False)
    k = keys((0, 0), (0.18, 1), (1, 0))
    c.turn_chain(torso, "X", lambda t: 14 * k(t))
    c.turn_chain(neck, "X", lambda t: 12 * k(t))
    relax_arms(c)
    for a in arms:
        c.turn(a["upper"], "Y", lambda t, s=a["side"]: sym(s, -18 * k(t)))
        c.turn(a["forearm"], "X", lambda t: 25 * k(t))
    tail_kick(c, lambda t: 10 * k(t), "X", 0.5)
    return c


def death_naga() -> Clip:
    """Sag, then topple onto the right side, arms limp, tail flat; holds."""
    c = naga_clip("death", 1.8, False, "rest")
    c.rest_from = 0.78
    c.pivot = Vector((cx + 0.15 * height, leg_y, 0))
    fall = lambda t: 85 * ease_in((t - 0.25) / 0.5) - 5 * math.sin(math.pi * smooth((t - 0.75) / 0.15))
    c.tilt = lambda t: Quaternion(AXES["Y"], math.radians(fall(t)))
    c.turn_chain(torso, "X", keys((0, 0), (0.3, -12), (1, 6)))
    relax_arms(c, keys((0, 1), (0.5, 0)))  # from the relaxed arm into the fall
    for a in arms:
        # the right arm (underneath) swings up past the head, out of the ground; the left
        # one (on top) drops against the side, which after the roll lies along the body
        # instead of standing up (an arm held out sideways at rest points at the sky)
        end = -50 if a["side"] == "r" else 70
        c.turn(a["upper"], "Y", lambda t, s=a["side"], e=end: sym(s, keys((0, 0), (0.3, -10), (0.75, e), (1, e))(t)))
        c.turn(a["forearm"], "X", keys((0, 0), (0.4, 25), (1, 25)))
    return c


# swarm: the asset is one cluster mesh on a single root bone; the game spreads units with
# particles or instances. Clips only move the root: bob, scurry jitter, a surge forward,
# a flinch, and a collapse flat onto the ground (scale) that holds.
def swarm_clip(name: str, seconds: float, loop: bool, ground: str = "contact") -> Clip:
    c = Clip(name, seconds, loop)
    c.ground, c.hover = ground, FLOAT * height
    c.pivot = Vector(by_name[root]["head"])
    return c


def idle_swarm() -> Clip:
    c = swarm_clip("idle", 2.0, True)
    c.scale[root] = lambda t: vscale(root, 1 + 0.03 * math.sin(2 * math.pi * t), 1 + 0.06 * math.sin(2 * math.pi * 2 * t))
    c.tilt = lambda t: Quaternion(AXES["Z"], math.radians(4 * math.sin(2 * math.pi * t)))
    return c


def move_swarm(name: str) -> Clip:
    c = swarm_clip(name, 1.0, True, "lift" if name in ("fly", "swim") else "contact")
    c.lift = lambda t: Vector((0.02 * height * math.sin(2 * math.pi * 3 * t), 0.03 * height * math.sin(2 * math.pi * 2 * t),
                               0.05 * height * abs(math.sin(2 * math.pi * 2 * t)) if name == "fly" else 0))
    c.scale[root] = lambda t: vscale(root, 1, 1 + 0.08 * math.sin(2 * math.pi * 4 * t))
    return c


def attack_swarm() -> Clip:
    c = swarm_clip("attack_melee", 0.9, False)
    k = keys((0, 0), (0.3, -0.3), (0.5, 1), (0.65, 1), (1, 0))
    c.lift = lambda t: Vector((0, 0.25 * mesh_len * k(t), 0))
    c.scale[root] = lambda t: vscale(root, 1 + 0.1 * max(0, k(t)), 1 + 0.15 * max(0, k(t)))
    return c


def hit_swarm() -> Clip:
    c = swarm_clip("hit", 0.5, False)
    k = keys((0, 0), (0.15, 1), (1, 0))
    c.lift = lambda t: Vector((0, -0.1 * mesh_len * k(t), 0))
    c.scale[root] = lambda t: vscale(root, 1 + 0.1 * k(t), 1 - 0.15 * k(t))
    return c


def death_swarm() -> Clip:
    """Scatter and sink: the cluster spreads out over the ground (1.7x in X and Y) and
    settles to 0.3 of its height, a heap of bodies rather than a pancake (a squash to
    0.15 flattened the rats of the column-shaped pilot mesh into a disc). Holds."""
    c = swarm_clip("death", 1.5, False)
    k = lambda t: smooth(t / 0.7)
    c.scale[root] = lambda t: vscale(root, 1 + 0.7 * k(t), 1 - 0.7 * k(t))
    c.tilt = lambda t: Quaternion(AXES["Z"], math.radians(12 * k(t)))  # the heap slews a little as it spreads
    c.info = {"spread": 1.7, "sink": 0.3}
    return c


def legless_clips() -> dict:
    sets = {}
    if PLAN == "serpentine":
        sets = {"walk": lambda s: slither("walk"), "swim": lambda s: swim_serpent(),
                "idle": lambda s: idle_serpent(), "attack_melee": lambda s: strike_serpent(),
                "hit": lambda s: hit_serpent(), "death": lambda s: death_serpent(),
                "cast": lambda s: cast_serpent(), "breath": lambda s: breath_serpent(),
                "burrow": lambda s: burrow_serpent(), "fly": lambda s: fly_serpent()}
    elif PLAN == "amorphous" and STACK:
        sets = {"idle": lambda s: idle_blob(), "walk": lambda s: walk_blob(), "attack_melee": lambda s: attack_blob(),
                "hit": lambda s: hit_blob(), "death": lambda s: death_blob()}
    elif PLAN == "plant_object" and STALK:
        sets = {"idle": lambda s: idle_plant(), "walk": lambda s: walk_plant(), "attack_melee": lambda s: attack_plant(),
                "attack_special": lambda s: attack_plant(),
                "hit": lambda s: hit_plant(), "death": lambda s: death_plant()}
    elif PLAN == "tentacled" and TENTS:
        sets = {"idle": lambda s: idle_tent(), "swim": lambda s: swim_tent(), "fly": lambda s: swim_tent("fly"),
                "walk": lambda s: crawl_tent(), "attack_melee": lambda s: attack_tent(),
                "hit": lambda s: hit_tent(), "death": lambda s: death_tent()}
    elif PLAN == "aquatic" and BODY and len(BODY["path"]) >= 4:
        sets = {"idle": lambda s: swim_fish("idle", 2.4, 6.0), "swim": lambda s: swim_fish(),
                "walk": lambda s: swim_fish("walk"), "attack_melee": lambda s: attack_fish(),
                "attack_special": lambda s: attack_fish(), "hit": lambda s: hit_fish(), "death": lambda s: death_fish()}
    elif PLAN == "merfolk_naga" and FISHTAIL:
        sets = {"idle": lambda s: idle_naga(), "swim": lambda s: swim_naga(), "walk": lambda s: crawl_naga(),
                "hit": lambda s: hit_naga(), "death": lambda s: death_naga()}
        if arms:
            sets["attack_melee"] = lambda s: attack_naga()
            sets["attack_ranged"] = lambda s: attack_naga("attack_ranged")  # an overhand throw
    elif PLAN == "swarm":
        sets = {"idle": lambda s: idle_swarm(), "walk": lambda s: move_swarm("walk"), "fly": lambda s: move_swarm("fly"),
                "swim": lambda s: move_swarm("swim"), "attack_melee": lambda s: attack_swarm(),
                "hit": lambda s: hit_swarm(), "death": lambda s: death_swarm()}
    elif PLAN == "floating":
        sets = {"idle": lambda s: idle_float(), "fly": lambda s: fly_float(), "walk": lambda s: fly_float(),
                "attack_melee": lambda s: attack_float(), "hit": lambda s: hit_float(),
                "death": lambda s: death_float(), "swim": lambda s: swim_float(),
                "cast": lambda s: cast(hover_clip)}
    return sets


makers = legless_clips() if legless else plan_clips()


def lifted_idle(base):
    """job idle_lift (anim.py): "match_move" holds the idle where the move clip
    (job move_clip: swim or fly, for a creature with no walk) holds the body, its ground
    mode and height: a "lift" move clip (legless swim, a ghost's fly) gives the same
    constant body lift, a "hover" one (a bird's fly) the same lowest-vertex height; a
    number lifts the idle by that many metres ("lift"). A legged flier's idle is then a
    gentler beat of its fly clip, so it hovers instead of standing on the air."""
    v = job["idle_lift"]
    mv = makers.get(job.get("move_clip") or "")
    if v == "match_move" and mv is None:
        return base
    if v == "match_move" and legs and wings and job.get("move_clip") == "fly":
        def hover_idle(s):
            c = fly("idle", 0.6, 2.5)  # the idle's usual length: one slow beat per loop
            c.info["idle_lift"] = v
            return c
        return hover_idle

    def make(s):
        c = base(s)
        if v == "match_move":
            m = mv(1.0)  # its settings only: nothing is keyed until build()
            c.ground, c.hover = m.ground, m.hover
            if m.ground == "lift":
                # the body where the move clip has it on its first frame, its own bob
                # included: the octopus swim starts at the bottom of its bob (0.04 x height
                # below its lift), so a body-lift match left the swim's first frame 0.13 m
                # lower than the idle
                c.hover = m.hover + m.lift(0.0).z - c.lift(0.0).z
        else:
            c.ground, c.hover = "lift", float(v)
        c.ik = lambda g, t: 0.0
        c.info = {**c.info, "idle_lift": v, "idle_lift_m": round(c.hover, 4)}
        return c
    return make


# One-shots of a lifted monster: attack, hit (and breath) are held where the idle holds the
# body on its first frame (IDLE_Z, measured once the idle is built), so a one-shot neither
# drops from the idle nor jumps back to it; death starts there and eases down onto the
# ground. Legs go limp (no IK: there is no ground under them); a legged flier keeps its
# fly clip's leg tuck and first-frame wing pose, which its hovering idle starts on (faded
# out over the fall in death).
MOVE_CLIPS = ("walk", "swim", "fly")
IDLE_Z: dict = {}


def leg_tuck(c: Clip, w=lambda t: 1.0) -> None:
    for g in legs:
        if job["upright"]:
            c.turn(g["chain"][0], "X", lambda t: 15 * w(t))
            c.turn(g["chain"][1], "X", lambda t: -35 * w(t))
        else:
            c.turn(g["chain"][0], "X", lambda t, k=20 if g in front else -25: k * w(t))


def lifted_oneshot(base):
    def make(s):
        c = base(s)
        z = IDLE_Z["z"] - c.lift(0.0).z
        c.ik = lambda g, t: 0.0
        if c.ground == "rest":
            c.start_lift = z if abs(z) > 1e-3 else 0.0  # the shark's death already starts at its lift
            fade = lambda t: 1 - smooth(t / 0.5)
        else:
            if c.ground != "lift" or abs(c.hover - z) > 1e-3:  # ghost, shark: already lifted there
                c.ground, c.hover = "lift", z
            fade = lambda t: 1.0
        if legs and wings and job.get("move_clip") == "fly":
            leg_tuck(c, fade)
            wing_beat(c, lambda t: 1.0, at=0.0, w=fade)  # the wings where idle and fly start
        c.info = {**c.info, "lifted_m": round(IDLE_Z["z"], 4)}
        return c
    return make


def at_idle_height(base):
    """A legged floor one-shot (attack, hit, death, breath, cast) moved up or down as a
    whole so its first frame has the body where the idle's has it (IDLE_SINK x drop down):
    started at its own height (hit 0, the lunge 0.02 x drop) the standing legs' IK found
    other knees (centaur idle -> hit 0.165 m, harpy 0.060). Its ground pass corrects as
    before; a death still ends lying on the ground."""
    def make(s):
        c = base(s)
        dz = -drop * IDLE_SINK - c.lift(0.0).z
        if not c.loop and c.ground in ("floor", "rest") and abs(dz) > 1e-9:
            lf = c.lift
            c.lift = lambda t: lf(t) + Vector((0, 0, dz))
        return c
    return make


LIFTED = job.get("idle_lift") not in (None, False, 0) and "idle" in makers and "idle" in job["clips"]
if LIFTED:
    makers["idle"] = lifted_idle(makers["idle"])
    for _n in list(makers):
        if _n != "idle" and _n not in MOVE_CLIPS and _n != "burrow":  # burrow digs into the floor
            makers[_n] = lifted_oneshot(makers[_n])
elif legs and not legless:
    for _n in list(makers):
        if _n not in ("idle", "burrow") and _n not in MOVE_CLIPS:
            makers[_n] = at_idle_height(makers[_n])
report = {"height": round(height, 3), "drop": round(drop, 3), "size": round(SIZE, 3), "size_scale": round(SCALE, 4),
          **({"ground_contacts": int(GROUND.sum())} if GROUND.any() else {}),
          "root": root, "head": head, "neck": neck,
          "spine": spine, "tail": tail, "arms": {a["side"]: a["chain"] for a in arms},
          "wings": {w["side"]: w["chain"] for w in wings}, "legs": [g["chain"] for g in legs],
          "clips": {}, "skipped": [], **({"body": BODY["path"]} if BODY else {}),
          **({"tentacles": TENTS} if TENTS else {}), **({"body": STALK, "limbs": LIMBS} if STALK else {}),
          **({"body": STACK} if STACK and not STALK else {}), **({"tail": FISHTAIL} if FISHTAIL else {})}
order = sorted(job["clips"], key=lambda n: n != "idle") if LIFTED else job["clips"]  # the idle sets IDLE_Z
for name in order:
    if name not in makers:
        report["skipped"].append(name)
        continue
    TORN.clear()
    report["clips"][name] = build(makers[name](1.0))
    if name == "idle" and LIFTED:
        IDLE_Z["z"] = report["clips"]["idle"]["body_z_first"]
    if name in ("walk", "attack_melee", "cast"):  # a part skinned to a leg (owlbear: tail fan on a hind
        for k in (0.8, 0.65, 0.5):  # shin) sweeping through the ground, or feet that cannot
            r = report["clips"][name]  # reach it with the body raised: smaller steps
            if r["min_z"] >= -DIP_MAX and r.get("ik_reach_gap", 0) <= REACH_MAX * height \
                    and (name != "walk" or TORN.get("n", 0) <= TEAR_MAX):
                break
            report["clips"][name] = build(makers[name](k))
            if name == "walk" and TORN.get("n", 0) > TEAR_MAX:
                report["clips"][name]["torn_edges"] = TORN["n"]
    print(f"clip {name}: {report['clips'][name]}")
with open(job["report"], "w") as f:
    json.dump(report, f, indent=1)
report["clips"] = {n: report["clips"][n] for n in job["clips"] if n in report["clips"]}
print("_OK")

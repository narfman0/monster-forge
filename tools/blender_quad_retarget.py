"""Quadruped mocap: CC0 source clips -> the standard quadruped skeleton (docs/rig-anim.md "Quadruped mocap clips").

Two halves:

1. Sampling (run as a Blender script, tools/anim_quad.py `sample`):

       blender -b --factory-startup -P tools/blender_quad_retarget.py -- '<job json>'

   job: {"rig": "fox", "blend": <animation .blend>, "rig_dir": <dir holding rig-<rig>.blend>, "clip": "walk",
         "loop": true, "out": <json>}. Opens one Mesh2Motion animation .blend (its armature is a library override
   of rigs/rig-<rig>.blend, relinked from rig_dir; no scripts run), evaluates the deform bones on every frame
   (the rig's IK / copy constraints included) and writes a monster-independent motion file: per source bone the
   world rotation delta from the source idle's first frame (`REF`), in our axes (Z up, +Y forward, +X the
   creature's right: the sources face -Y with the left side on +X, so they are turned 180 degrees about Z),
   the pelvis' offset and each foot's offset from that frame in units of the source's hind hip height, and
   which feet are planted. data/quadmocap/<rig>/<clip>.json; small, so anim does not need the packs.

2. Mapping (imported by blender_clips.py, mathutils only): `SOURCES` names the source rigs' chains slot by
   slot, `target_slots` reads the same slots from a canonical rig's bone names (tools/quadcanon.py), and
   `bone_deltas` gives every target bone its world rotation delta per frame (chains resampled by length
   share, extra bones riding their nearest mapped ancestor).
"""
import json
import math
import sys
from pathlib import Path

from mathutils import Matrix, Quaternion, Vector

# Source rigs (Mesh2Motion, CC0). Chains are listed from the body outward. `chest` is the bone the front legs
# hang from (the source's zero-length anchors, the dragon's collar_anchor / hips_anchor, are left out). The
# timing's nominal animal size is anim_quad.NOMINAL_HIP.
SOURCES = {
    "fox": {
        "pelvis": "hips", "spine": ["spine_1", "spine_2"], "chest": "spine_3", "neck": ["spine_4", "spine_5"],
        "head": "head", "jaw": "mouth", "tail": ["tail_1", "tail_2", "tail_3", "tail_4"],
        "legs": {f"front_{s}": [f"front_leg_shoulder_{s}", f"front_leg_upper_{s}", f"front_leg_lower_{s}",
                                f"front_leg_ankle_{s}", f"front_leg_foot_{s}"] for s in "lr"}
        | {f"hind_{s}": [f"back_leg_pelvis_{s}", f"back_leg_upper_{s}", f"back_leg_lower_{s}",
                         f"back_leg_ankle_{s}", f"back_leg_foot_{s}", f"back_leg_toe_{s}"] for s in "lr"},
        "foot": {f"front_{s}": f"front_leg_foot_{s}" for s in "lr"} | {f"hind_{s}": f"back_leg_foot_{s}" for s in "lr"},
        "ears": {s: [f"ear_1_{s}", f"ear_2_{s}"] for s in "lr"},
    },
    "horse": {
        "pelvis": "hips", "spine": ["spine_1", "spine_2"], "chest": "spine_3", "neck": ["spine_4", "spine_5", "spine_6"],
        "head": "head", "jaw": "mouth", "tail": ["tail_1", "tail_2", "tail_3", "tail_4"],
        "legs": {f"front_{s}": [f"front_scapula_{s}", f"front_humerus_{s}", f"front_leg_upper_{s}",
                                f"front_leg_lower_{s}", f"front_leg_ankle_{s}", f"front_leg_foot_{s}"] for s in "lr"}
        | {f"hind_{s}": [f"back_leg_pelvis_{s}", f"back_leg_upper_{s}", f"back_leg_lower_{s}",
                         f"back_leg_ankle_{s}", f"back_leg_foot_{s}", f"back_leg_toe_{s}"] for s in "lr"},
        "foot": {f"front_{s}": f"front_leg_foot_{s}" for s in "lr"} | {f"hind_{s}": f"back_leg_toe_{s}" for s in "lr"},
        "ears": {s: [f"ear_1_{s}", f"ear_2_{s}"] for s in "lr"},
    },
    "dragon": {
        "pelvis": "hips", "spine": ["spine_0"], "chest": "spine_1", "neck": ["spine_2", "spine_3", "spine_4"],
        "head": "head", "jaw": "mouth_lower", "tail": [f"tail_{i}" for i in range(1, 7)],
        "legs": {f"front_{s}": [f"front_leg_collar_{s}", f"front_leg_{s}", f"front_ankle_{s}", f"front_foot_{s}",
                                f"front_toes_{s}"] for s in "lr"}
        | {f"hind_{s}": [f"back_leg_hip_{s}", f"back_leg_{s}", f"back_ankle_{s}", f"back_foot_{s}",
                         f"back_toes_{s}"] for s in "lr"},
        "foot": {f"front_{s}": f"front_foot_{s}" for s in "lr"} | {f"hind_{s}": f"back_foot_{s}" for s in "lr"},
        "wings": {s: [f"shoulder_{s}", f"wing_1_{s}", f"wing_2_{s}", f"wing_3_{s}", f"wing_4_{s}", f"wing_5_{s}"]
                  for s in "lr"},
    },
}
# The source's side suffix is the creature's own side (left on +X before the turn); after the 180 degree turn
# about Z the left side is on -X, as on our rigs, so `_l` maps to `_l`.
TURN = Matrix.Rotation(math.pi, 4, "Z")
NECK_K = 1.5      # neck share = NECK_K x (our neck length / the source's), both in hip heights ...
NECK_MIN = 0.5    # ... at least this
NECK_NONE = 0.4   # a head on the chest (no neck bone): this share of the source's neck and head turn
SPRING_HZ = 2.6     # follow_through: a chain one hip height long rings at SPRING_HZ / sqrt(2)
SPRING_ZETA = 0.45  # damping ratio (under-damped: a little overshoot)
SPRING_CAP = 30.0   # degrees a bone may lag its rigid pose
SPRING_AMP = 0.6    # share blended in = SPRING_AMP x (0.5 + chain length in hip heights), 0.3-1
SPRING_SUB = 6      # sub-steps per source frame
SPRING_CYCLES = 3   # a loop's spring is run this many times round, the last kept
CONTACT_UP = 0.035   # x hip height: a foot this close to its idle height (or lower) is on the ground ...
CONTACT_VZ = 0.8     # ... and rising or falling slower than this x hip height per second ...
CONTACT_SLIDE = 0.6  # ... and, in a one-shot, moving slower than this over the ground (a loop's stance foot
                     # moves backward with the walk)


# ----------------------------------------------------------------------------------------------- mapping

def quat(v) -> Quaternion:
    return Quaternion(v)


def hemi(q: Quaternion) -> Quaternion:
    """The same rotation with w >= 0 (to_quaternion gives either sign)."""
    return -q if q.w < 0 else q


def slerp(a: Quaternion, b: Quaternion, f: float) -> Quaternion:
    """Short-arc slerp: mathutils' Quaternion.slerp keeps the sign it is given and goes the long way round
    when the two lie in opposite hemispheres (10 -> 20 degrees about Z, -q20: 345 at the middle)."""
    return a.slerp(-b if a.dot(b) < 0 else b, f)


def chain_params(lengths: list[float]) -> list[float]:
    """Each bone's middle as a share of the chain's length."""
    tot = sum(lengths) or 1.0
    out, acc = [], 0.0
    for ln in lengths:
        out.append((acc + ln / 2) / tot)
        acc += ln
    return out


def sample_chain(qs: list[Quaternion], params: list[float], p: float) -> Quaternion:
    """The source chain's rotation delta at length share p (slerp between neighbouring bones)."""
    if p <= params[0]:
        return qs[0]
    for (a, qa), (b, qb) in zip(zip(params, qs), zip(params[1:], qs[1:])):
        if p <= b:
            return slerp(qa, qb, (p - a) / (b - a) if b > a else 0.0)
    return qs[-1]


def target_slots(names: set) -> dict:
    """The canonical rig's chains, slot by slot (tools/quadcanon.py names)."""
    def chain(prefix):
        out, i = [], 1
        while f"{prefix}_{i:02d}" in names:
            out.append(f"{prefix}_{i:02d}")
            i += 1
        return out
    legs = {}
    for leg in ("front_l", "front_r", "hind_l", "hind_r"):
        top = []
        for k in range(4, 1, -1):  # _shoulder_3 / _hip_3 furthest up
            top += [n for n in (f"{leg}_shoulder_{k}", f"{leg}_hip_{k}") if n in names]
        top += [n for n in (f"{leg}_shoulder", f"{leg}_hip") if n in names]
        mid = [n for n in (f"{leg}_upper", f"{leg}_lower", f"{leg}_ankle") if n in names]
        if f"{leg}_foot" in names:
            legs[leg] = {"above": top + mid, "foot": f"{leg}_foot", "toes": chain(f"{leg}_toe")}
    return {"pelvis": "pelvis" if "pelvis" in names else None, "spine": chain("spine"),
            "chest": "chest" if "chest" in names else None, "neck": chain("neck"),
            "head": "head" if "head" in names else None, "snout": "snout" if "snout" in names else None,
            "tail": chain("tail"), "wings": {s: chain(f"wing_{s}") for s in "lr" if chain(f"wing_{s}")},
            "legs": legs}


def bone_deltas(motion: dict, bones: dict, parent: dict, t_slots: dict, use_wings: bool, hip: float = 1.0,
                wing_mean: bool = False) -> dict:
    """{target bone: [world delta Quaternion per source frame]} for every mapped bone. `bones`: name ->
    (head, tail) Vectors at rest; `parent`: name -> parent name; `hip` the target's hind hip height (m).
    Unmapped bones are left out (they ride their parent: no local turn).

    Neck and head turn by the source's (relative to its chest) times NECK_SHARE: a target neck shorter than
    the source's (both in hip heights) takes a smaller share, a head with no neck bone NECK_NONE (the giant
    crocodile's head on its chest reared 90 degrees with the dragon's whole neck swing). `wing_mean`: wing
    bones turn relative to their mean over the clip (a fly beats about our rest wings; the source's wing
    deltas are from its folded idle wings)."""
    src = SOURCES[motion["rig"]]
    R = {n: [quat(q) for q in qs] for n, qs in motion["rot"].items()}
    L = motion["lengths"]
    n = motion["frames"]
    out = {}

    def put_chain(tgt: list, srcc: list):
        srcc = [s for s in srcc if s in R]
        if not tgt or not srcc:
            return
        sp = chain_params([L[s] for s in srcc])
        tp = chain_params([(bones[b][1] - bones[b][0]).length for b in tgt])
        for b, p in zip(tgt, tp):
            out[b] = [sample_chain([R[s][f] for s in srcc], sp, p) for f in range(n)]

    # trunk: pelvis, spine (+ chest when the rig has none of its own), neck, head
    if t_slots["pelvis"]:
        out[t_slots["pelvis"]] = R[src["pelvis"]]
    sp_t = t_slots["spine"] + ([t_slots["chest"]] if t_slots["chest"] else [])
    if t_slots["chest"]:
        put_chain(t_slots["spine"], src["spine"])
        out[t_slots["chest"]] = R[src["chest"]]
    else:
        put_chain(sp_t, src["spine"] + [src["chest"]])
    neck_src = src["neck"]
    if t_slots["neck"]:
        put_chain(t_slots["neck"], neck_src)
        if t_slots["head"]:
            out[t_slots["head"]] = R[src["head"]]
    elif t_slots["head"]:  # no neck bone (the head sits on the chest): the head takes the neck's turn too
        out[t_slots["head"]] = R[src["head"]]
    # neck and head: the source's turn relative to its chest, scaled by the neck's length share
    tn = sum((bones[b][1] - bones[b][0]).length for b in t_slots["neck"]) / max(hip, 1e-6)
    sn = sum(L[b] for b in src["neck"] if b in L)
    share = NECK_NONE if not t_slots["neck"] else max(NECK_MIN, min(1.0, NECK_K * tn / max(sn, 1e-6)))
    if share < 1.0:
        ch = R[src["chest"]]
        for b in t_slots["neck"] + ([t_slots["head"]] if t_slots["head"] else []):
            if b in out:
                out[b] = [ch[f] @ slerp(Quaternion(), ch[f].inverted() @ out[b][f], share) for f in range(n)]
    out["_neck_share"] = share
    put_chain(t_slots["tail"], src["tail"])
    for leg, g in t_slots["legs"].items():
        sl = src["legs"][leg]
        foot = src["foot"][leg]
        above = sl[:sl.index(foot)]
        put_chain(g["above"], above)
        out[g["foot"]] = R[foot]
        below = sl[sl.index(foot) + 1:]
        if g["toes"] and below:
            put_chain(g["toes"], below)
    if use_wings and "wings" in src:
        for s, ch in t_slots["wings"].items():
            put_chain(ch, src["wings"][s])
            if wing_mean:
                for b in ch:
                    if b in out:
                        mq = mean_quat(out[b])
                        out[b] = [q @ mq.inverted() for q in out[b]]
    return out


def mean_quat(qs: list) -> Quaternion:
    """Normalised sum in one hemisphere (fine for the spread of one clip). Normalised as a Quaternion: a 4D
    mathutils Vector's normalized() leaves its 4th component alone."""
    acc = Vector((0.0, 0.0, 0.0, 0.0))
    for q in qs:
        v = Vector(q[:])
        acc += -v if v.dot(Vector(qs[0][:])) < 0 else v
    return Quaternion(acc[:]).normalized()


def cap_roll(full: dict, root: str, cap_deg: float) -> None:
    """Turn the whole body back about the forward axis (Y) where the root's delta rolls it more than cap_deg
    (a death that rolls onto the back or onto a wing lies on its side or belly instead). In place, every
    bone, so local turns are unchanged."""
    import math
    cap = math.radians(cap_deg)
    n = len(full[root])
    corr = []
    for f in range(n):
        q = full[root][f]
        tw = Quaternion((q.w, 0.0, q.y, 0.0))  # swing-twist: the twist about Y
        if tw.magnitude < 1e-9:
            corr.append(Quaternion())
            continue
        tw.normalize()
        ang = 2 * math.atan2(tw.y, tw.w)
        ang = (ang + math.pi) % (2 * math.pi) - math.pi
        over = ang - math.copysign(cap, ang) if abs(ang) > cap else 0.0
        corr.append(Quaternion((0, 1, 0), -over))
    for b, qs in full.items():
        full[b] = [corr[f] @ qs[f] for f in range(n)]


def contacts(feet: dict, loop: bool, fps: float) -> dict:
    """{leg: [bool per frame]}: the foot is on the ground (near its idle height, not rising or falling fast,
    and in a one-shot not sliding)."""
    out = {}
    for k, ps in feet.items():
        n = len(ps)
        c = []
        for i in range(n):
            j, h = ((i + 1) % n, (i - 1) % n) if loop else (min(i + 1, n - 1), max(i - 1, 0))
            span = 2 if loop else max(1, j - h)
            v = (Vector(ps[j]) - Vector(ps[h])) * (fps / span)
            ok = ps[i][2] <= CONTACT_UP and abs(v.z) < CONTACT_VZ
            if not loop:
                ok = ok and v.xy.length < CONTACT_SLIDE
            c.append(ok)
        out[k] = c
    return out


def runs(c: list, loop: bool) -> list[list[int]]:
    """Index runs of True (a loop's run may wrap around the end)."""
    n = len(c)
    if all(c):
        return [list(range(n))]
    start = next((i for i in range(n) if not c[i]), 0) if loop else 0
    out, cur = [], []
    for k in range(n):
        i = (start + k) % n if loop else k
        if c[i]:
            cur.append(i)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def stance_speed(feet: dict, con: dict, loop: bool, fps: float) -> float:
    """Ground speed a loop implies (hip heights per second): the median backward speed of the planted feet."""
    if not loop:
        return 0.0
    v = []
    for k, ps in feet.items():
        n = len(ps)
        for i in range(n):
            j = (i + 1) % n
            if con[k][i] and con[k][j]:
                v.append(-(ps[j][1] - ps[i][1]) * fps)
    v.sort()
    return v[len(v) // 2] if v else 0.0


def lock_feet(feet: dict, con: dict, loop: bool, fps: float, speed: float) -> dict:
    """Foot paths with the planted frames locked: on the ground (z 0), still across (x), and along the body
    (y) still in a one-shot or sliding back at exactly `speed` in a loop, so the feet do not skate. A swing
    between two plants keeps the source's height (the end corrections blended in), and goes from one locked
    plant to the next along the source swing's own fore-aft progress, made monotonic."""
    out = {}
    for k, ps in feet.items():
        n = len(ps)
        P = [Vector(p) for p in ps]
        L = [None] * n
        for r in runs(con[k], loop):
            if loop:
                m = len(r)
                ymid = sum(P[i].y for i in r) / m
                xmid = sum(P[i].x for i in r) / m
                for j, i in enumerate(r):
                    L[i] = Vector((xmid, ymid - speed * (j - (m - 1) / 2) / fps, 0.0))
            else:
                a = P[r[0]]
                for i in r:
                    L[i] = Vector((a.x, a.y, 0.0))
        if all(x is None for x in L):
            out[k] = [list(p) for p in P]
            continue
        d = [None if L[i] is None else L[i] - P[i] for i in range(n)]
        res = []
        for i in range(n):
            if L[i] is not None:
                res.append(list(L[i]))
                continue
            # the swing: from the last plant's end a to the next plant's start b (wrapping in a loop)
            def near(step):
                for s_ in range(1, n + 1):
                    j = i + step * s_
                    if loop:
                        j %= n
                    elif j < 0 or j >= n:
                        return None, None
                    if d[j] is not None:
                        return j, s_
                return None, None
            a, sa = near(-1)
            b, sb = near(1)
            if a is None and b is None:
                q = P[i].copy()
            elif a is None or b is None:
                q = P[i] + d[b if a is None else a]
            else:
                u = sa / (sa + sb)
                u = u * u * (3 - 2 * u)
                q = P[i] + d[a].lerp(d[b], u)
                # fore-aft and across: the source swing's own progress between the two plants, kept monotonic
                # (a time blend of the end corrections drew the paw back mid-air: 14 cm on the wolf's hind foot)
                span = P[b] - P[a] if (loop or b > a) else None
                if span is not None and span.xy.length > 1e-6:
                    path = []
                    j = a
                    while True:
                        j = (j + 1) % n if loop else j + 1
                        if j == b:
                            break
                        path.append(j)
                    prog, best = {}, 0.0
                    for j in path:
                        t = (P[j] - P[a]).xy.dot(span.xy) / span.xy.length_squared
                        best = max(best, min(1.0, max(0.0, t)))
                        prog[j] = best
                    t = prog.get(i, u)
                    t = t * t * (3 - 2 * t) * 0.5 + t * 0.5  # eased toward the ends
                    q.x = L[a].x + (L[b].x - L[a].x) * t
                    q.y = L[a].y + (L[b].y - L[a].y) * t
            q.z = max(q.z, 1e-4)  # in the air: never a plant (blender_clips planted() reads z == 0)
            res.append(list(q))
        out[k] = res
    return out


def ride(deltas: dict, parent: dict, order: list, n: int) -> dict:
    """Every bone's world delta: its own, else its nearest mapped ancestor's (it rides)."""
    full = {}
    for b in order:  # parents first
        if b in deltas:
            full[b] = deltas[b]
        else:
            p = parent.get(b)
            full[b] = full[p] if p in full else [Quaternion()] * n
    return full


def rotvec(q: Quaternion) -> Vector:
    q = hemi(q)
    ax, ang = q.to_axis_angle()
    return Vector(ax) * ang


def follow_through(deltas: dict, full: dict, parent: dict, chains: list, dt: float, loop: bool, hip: float,
                   bones: dict) -> dict:
    """Secondary motion (docs/rig-anim.md "Quadruped mocap clips"): each bone of a chain (root to tip) lags the
    pose it would hold riding rigidly on its (already lagged) parent with its own mapped turn, through a
    damped angular spring: SPRING_HZ / sqrt(1 + chain length in hip heights), damping SPRING_ZETA, the lag
    at most SPRING_CAP degrees, blended in by SPRING_AMP x the chain's length share. Baked per source frame;
    a loop is run SPRING_CYCLES times and the last cycle kept, so it still closes. Returns a copy of
    `deltas` with the chain bones' world deltas replaced ({chain root: amp} under "_secondary")."""
    import math
    out = dict(deltas)
    n = len(next(iter(full.values())))
    rep = {}
    for ch in chains:
        ln = sum((bones[b][1] - bones[b][0]).length for b in ch) / max(hip, 1e-6)
        w0 = 2 * math.pi * SPRING_HZ / math.sqrt(1.0 + ln)
        amp = max(0.3, min(1.0, SPRING_AMP * (0.5 + ln)))
        cap = math.radians(SPRING_CAP)
        rep[ch[0]] = round(amp, 3)
        done = {}
        for b in ch:
            p = parent.get(b)
            base = done.get(p) or full.get(p) or [Quaternion()] * n
            orig_p = full.get(p) or [Quaternion()] * n
            G = [base[f] @ (orig_p[f].inverted() @ full[b][f]) for f in range(n)]
            S, v = G[0].copy(), Vector((0.0, 0.0, 0.0))
            h = dt / SPRING_SUB
            res = []
            for c in range(SPRING_CYCLES if loop else 1):
                for f in range(n):
                    for _ in range(SPRING_SUB):
                        e = rotvec(G[f].inverted() @ S)
                        v += (-w0 * w0 * e - 2 * SPRING_ZETA * w0 * v) * h
                        e = e + v * h
                        if e.length > cap:
                            e = e * (cap / e.length)
                            v *= 0.5
                        S = G[f] @ (Quaternion(e.normalized(), e.length) if e.length > 1e-9 else Quaternion())
                    if c == (SPRING_CYCLES - 1 if loop else 0):
                        res.append(slerp(G[f], S, amp))
            done[b] = res
            out[b] = res
    out["_secondary"] = rep
    return out


# ----------------------------------------------------------------------------------------------- sampling

def _sample(job: dict) -> None:
    import bpy
    import os
    bpy.ops.wm.open_mainfile(filepath=job["blend"], load_ui=False, use_scripts=False)
    for lib in bpy.data.libraries:  # authored on Windows: C:/git/mesh2motion-assets/rigs/rig-<rig>.blend
        lib.filepath = os.path.join(job["rig_dir"], os.path.basename(lib.filepath.replace("\\", "/")))
        lib.reload()
    arm = next(o for o in bpy.data.objects if o.type == "ARMATURE" and o.library is None and len(o.data.bones))
    act = arm.animation_data.action
    scene = bpy.context.scene
    fps = scene.render.fps / scene.render.fps_base
    f0, f1 = int(round(act.frame_range[0])), int(round(act.frame_range[1]))
    if f0 < 0:  # the dragon's clips start a frame early (a pre-roll key): start at 0
        f0 = 0
    frames = list(range(f0, f1 + 1))
    if job["loop"] and len(frames) > 2:
        frames = frames[:-1]  # the last frame is the first again
    src = SOURCES[job["rig"]]
    names = [src["pelvis"], *src["spine"], src["chest"], *src["neck"], src["head"], *src["tail"]]
    names += [b for ch in src["legs"].values() for b in ch]
    names += [b for ch in src.get("wings", {}).values() for b in ch]
    names += [b for ch in src.get("ears", {}).values() for b in ch]
    if src.get("jaw") in arm.pose.bones:
        names.append(src["jaw"])
    names = [b for b in names if b in arm.pose.bones]
    W = TURN @ arm.matrix_world

    def pose(fr):
        scene.frame_set(fr)
        return {b: (W @ arm.pose.bones[b].matrix) for b in names}

    # the reference: the source idle's first frame (job ref_blend), the same for every clip of the rig
    ref_file = job.get("ref")  # written by the rig's idle sample (dump_ref)
    if ref_file:
        mats = json.loads(Path(ref_file).read_text())["mats"]
        ref = {b: Matrix(mats[b]) for b in names}
    else:
        ref = pose(frames[0])
    seq = [pose(fr) for fr in frames]
    feet = src["foot"]
    hind_hip = sum(ref[src["legs"][k][1]].translation.z for k in ("hind_l", "hind_r")) / 2
    front_hip = sum(ref[src["legs"][k][1]].translation.z for k in ("front_l", "front_r")) / 2
    H = hind_hip

    def eff(m, b):  # the foot bone's tail: our rigs' IK end (blender_clips effector)
        return (m @ Matrix.Translation((0, arm.data.bones[b].length, 0))).translation
    rot = {b: [list(hemi((s[b].to_quaternion() @ ref[b].to_quaternion().inverted()).normalized())) for s in seq]
           for b in names}
    pel = src["pelvis"]
    pelvis = [list((s[pel].translation - ref[pel].translation) / H) for s in seq]
    foot = {k: [list((eff(s[b], b) - eff(ref[b], b)) / H) for s in seq] for k, b in feet.items()}
    lengths = {b: arm.data.bones[b].length / H for b in names}
    rest = {b: [list(ref[b].translation / H), list(eff(ref[b], b) / H)] for b in names}
    n = len(seq)
    out = {"rig": job["rig"], "clip": job["clip"], "action": act.name, "fps": fps, "frames": n,
           "seconds": round(n / fps if job["loop"] else (n - 1) / fps, 4), "loop": job["loop"],
           "hip_height": H, "front_hip_height_n": front_hip / H, "lengths": lengths, "rest_n": rest,
           "rot": {b: [[round(x, 6) for x in q] for q in qs] for b, qs in rot.items()},
           "pelvis": [[round(x, 6) for x in p] for p in pelvis],
           "feet": {k: [[round(x, 6) for x in p] for p in ps] for k, ps in foot.items()},
           "source": {"blend": os.path.basename(job["blend"])}}
    if job.get("dump_ref"):
        Path(job["dump_ref"]).write_text(json.dumps({"mats": {b: [list(r) for r in seq[0][b]] for b in names}}))
    Path(job["out"]).write_text(json.dumps(out, separators=(",", ":")))
    print("_OK", job["clip"], n, "frames")


if __name__ == "__main__":
    _sample(json.loads(sys.argv[sys.argv.index("--") + 1]))

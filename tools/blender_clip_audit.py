"""Read-only clip metrics for one rigged monster (tools/clip_audit.py runs it).

    blender -b --factory-startup --python-exit-code 1 -P tools/blender_clip_audit.py -- '<job json>'

job: id, src (model_rigged.glb), anims (dir of <clip>.glb), clips (names), parts (manifest
anim.parts: legs, arms, wings, tail, head, root), out (metrics json), tiles (dir or null: a few
review tiles per clip), tile_px.

Nothing is written next to the inputs. Each clip's action is played on the rig and the skinned mesh
is evaluated on every frame (the same mesh_pos as blender_retarget_ual.py); the metrics are raw
numbers (metres and fractions of the rest height), the flags are the driver's (thresholds can change
without re-running Blender).
"""
import json
import math
import sys
import time
from pathlib import Path

import bpy
import numpy as np
from mathutils import Quaternion, Vector

job = json.loads(sys.argv[sys.argv.index("--") + 1])
t_all = time.time()
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.fps = 30  # before any import: the glTF importer turns seconds into frames at the scene's fps
bpy.ops.import_scene.gltf(filepath=job["src"])
arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
meshes = [o for o in bpy.data.objects if o.type == "MESH" and o.vertex_groups]
ours = set(bpy.data.objects)
W = arm.matrix_world.copy()
parts = job.get("parts") or {}
PB = arm.pose.bones


def reset_pose():
    for pb in PB:
        pb.rotation_mode = "QUATERNION"
        pb.location, pb.rotation_quaternion, pb.scale = Vector(), Quaternion(), Vector((1, 1, 1))
    bpy.context.view_layer.update()


def mesh_pos():
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
    return np.concatenate(cos)


if arm.animation_data:
    arm.animation_data.action = None
reset_pose()
REST = mesh_pos()
GROUND = float(REST[:, 2].min())
HEIGHT = float(REST[:, 2].max()) - GROUND
LENGTH = float(np.ptp(REST[:, 1]))
SIZE = max(HEIGHT, LENGTH)
edges, dom, offs = [], [], 0
for o in meshes:
    e = np.empty(len(o.data.edges) * 2, dtype=np.int64)
    o.data.edges.foreach_get("vertices", e)
    edges.append(e.reshape(-1, 2) + offs)
    names = {vg.index: vg.name for vg in o.vertex_groups}
    for v in o.data.vertices:
        dom.append(names.get(max(v.groups, key=lambda g: g.weight).group, "") if len(v.groups) else "")
    offs += len(o.data.vertices)
edges = np.concatenate(edges) if edges else np.zeros((0, 2), np.int64)
dom = np.array(dom)
rest_len = np.linalg.norm(REST[edges[:, 0]] - REST[edges[:, 1]], axis=1)
ok_e = rest_len > 1e-3 * HEIGHT  # as blender_retarget_ual.evaluate: a degenerate sliver says nothing
E0, E1, L0 = edges[ok_e, 0], edges[ok_e, 1], rest_len[ok_e]


def desc(name):
    b = arm.data.bones.get(name)
    return {name} | {d.name for d in b.children_recursive} if b else set()


def chain_len(ch):
    return sum(arm.data.bones[n].length for n in ch if n in arm.data.bones) * W.to_scale()[0]


def bone_head(n):
    return np.array((W @ PB[n].head)[:])


def bone_tail(n):
    return np.array((W @ PB[n].tail)[:])


legs = [[n for n in ch if n in PB] for ch in parts.get("legs") or []]
legs = [ch for ch in legs if ch]
feet = [ch[-1] for ch in legs]
leg_len = [chain_len(ch) for ch in legs]
# per leg: vertices whose strongest bone is the foot or below it (the sole)
foot_verts = [np.nonzero(np.isin(dom, list(desc(f))))[0] for f in feet]
arms = {s: [n for n in ch if n in PB] for s, ch in (parts.get("arms") or {}).items()}
wings = {s: [n for n in ch if n in PB] for s, ch in (parts.get("wings") or {}).items()}
tail = [n for n in parts.get("tail") or [] if n in PB]
head = parts.get("head") if isinstance(parts.get("head"), str) and parts["head"] in PB else None
SOCKETS = [s for s in ("hand_r", "hand_l", "grip_r", "grip_l", "head") if s in PB]
# socket follow: the 24 rest vertices nearest each socket; their centroid should keep its offset in
# the socket's frame (a socket on the wrong bone drifts off the hand)
SOCK_NEAR = {}
for s in SOCKETS:
    p = np.array((W @ arm.data.bones[s].head_local)[:])
    SOCK_NEAR[s] = np.argsort(np.linalg.norm(REST - p, axis=1))[:24]
sock_rest = {s: (W @ arm.data.bones[s].matrix_local).inverted() for s in SOCKETS}
sock_off0 = {s: np.array((sock_rest[s] @ Vector(REST[SOCK_NEAR[s]].mean(axis=0))).to_tuple()) for s in SOCKETS}
# limb-through-body proxy: the torso's vertices (strongest bone on the root .. spine/neck path, not
# a limb, tail or wing) and per limb its distal half (the shin/forearm and below)
# bone -> part label, for where the worst-stretched edges are
PART = {}
for nm in parts.get("spine") or []:
    PART[nm] = "spine"
for nm in parts.get("neck") or []:
    PART[nm] = "neck"
if head:
    for nm in desc(head):
        PART[nm] = "head"
for i, ch in enumerate(legs):
    for k, nm in enumerate(ch):
        PART[nm] = f"leg{i}:" + ("upper" if k < len(ch) // 2 else "lower")
    for nm in desc(ch[-1]) - {ch[-1]}:
        PART[nm] = f"leg{i}:lower"
for s_, ch in arms.items():
    for nm in desc(ch[0]) if ch else ():
        PART[nm] = f"arm_{s_}"
for s_, ch in wings.items():
    for nm in desc(ch[0]) if ch else ():
        PART[nm] = f"wing_{s_}"
for k, nm in enumerate(tail):
    PART[nm] = "tail:" + ("root" if k < max(1, len(tail) // 3) else "end")
body = [n for n in parts.get("body") or [] if n in PB]
for k, nm in enumerate(body):
    PART[nm] = f"body{3 * k // len(body)}/3"
for ch in parts.get("tentacles") or []:
    for k, nm in enumerate(n for n in ch if n in PB):
        PART[nm] = "tentacle:" + ("root" if k < 2 else "end")
limb_bones = set()
for ch in legs + list(arms.values()) + list(wings.values()) + ([tail] if tail else []):
    if ch:
        limb_bones |= desc(ch[0])
torso_names = set(parts.get("spine") or []) | set(parts.get("neck") or [])
if parts.get("root"):
    torso_names.add(parts["root"])
torso_v = np.nonzero(np.isin(dom, list(torso_names - limb_bones)))[0]
distal = {}
for i, ch in enumerate(legs):
    distal[f"leg{i}"] = np.nonzero(np.isin(dom, list(desc(ch[len(ch) // 2]))))[0]
for s, ch in arms.items():
    if len(ch) >= 2:
        distal[f"arm_{s}"] = np.nonzero(np.isin(dom, list(desc(ch[len(ch) // 2]))))[0]


# wing probe (winged plans), from the mesh, not the rig: per side the vertices that stick out
# farthest up and out (s + z over the side's vertices past PROBE_SIDE x the height from the centre
# line and above PROBE_LOW x it), the wing's tip whether or not the rig has a wing chain for it.
# Their travel in the body's frame (a rigid fit to the spine's own vertices on every frame, so the
# hover bob and the tilt do not count) tells a beating wing from one riding the back
# (clip_audit flag wing_frozen:<side>).
PROBE_SIDE, PROBE_LOW, PROBE_TOP = 0.15, 0.35, 0.02
WING_PLANS = {"winged_quadruped", "winged_biped", "avian"}
PROBE, PROBE_CX = {}, None
if job.get("plan") in WING_PLANS:
    _spine = set(parts.get("spine") or []) | ({parts["root"]} if isinstance(parts.get("root"), str) else set())
    BODY_V = np.nonzero(np.isin(dom, list(_spine - limb_bones)))[0]
    # the centre line: the spine's own skin (the feet can all be on one side: a 2-leg rig)
    _cx = float(np.median(REST[BODY_V, 0])) if len(BODY_V) >= 30 else float(np.median(REST[:, 0]))
    for _s, _sg in (("r", 1.0), ("l", -1.0)):
        _side = (REST[:, 0] - _cx) * _sg
        _sel = np.nonzero((_side > PROBE_SIDE * HEIGHT) & (REST[:, 2] - GROUND > PROBE_LOW * HEIGHT))[0]
        if len(_sel) < 20:
            continue
        _e = _side[_sel] + REST[_sel, 2] - GROUND
        _tip = _sel[np.argsort(-_e)[:max(20, int(PROBE_TOP * len(_sel)))]]
        _own = {}
        for _n in dom[_tip]:
            _own[_n] = _own.get(_n, 0) + 1
        PROBE[_s] = {"v": _tip, "own": dict(sorted(_own.items(), key=lambda kv: -kv[1])[:3]),
                     "out_frac": round(float(_side[_tip].mean()) / HEIGHT, 3),
                     "up_frac": round(float((REST[_tip, 2] - GROUND).mean()) / HEIGHT, 3)}
    PROBE_CX = round(_cx, 4)


def rigid_to_rest(co, idx):
    """Rotation and translation that map the frame's idx vertices onto their rest positions (Kabsch)."""
    a, b = co[idx], REST[idx]
    ca, cb = a.mean(axis=0), b.mean(axis=0)
    u, _, vt = np.linalg.svd((a - ca).T @ (b - cb))
    d = np.sign(np.linalg.det(u @ vt))
    r = u @ np.diag([1, 1, d]) @ vt
    return r, cb - ca @ r


def wing_probe(P):
    """Per side: the probe tips' travel in the body frame (p90 over the tips, max over the frames), x height."""
    out = {}
    for s, pr in PROBE.items():
        v = pr["v"]
        rel = []
        for co in P:
            if len(BODY_V) >= 30:
                r, t = rigid_to_rest(co, BODY_V)
                rel.append(co[v] @ r + t)
            else:
                rel.append(co[v])
        rel = np.array(rel)
        trav = np.linalg.norm(rel - rel[0], axis=2)  # (frames, tips)
        out[s] = r4(float(np.percentile(trav.max(axis=0), 90)) / HEIGHT)
    return out


def torso_frame(co):
    """Torso proxy: an ellipsoid fitted to the torso vertices (PCA, 1.0 x the std per axis x 1.6)."""
    if len(torso_v) < 30:
        return None
    t = co[torso_v]
    c = t.mean(axis=0)
    u, s, vt = np.linalg.svd(t - c, full_matrices=False)
    r = s / math.sqrt(len(t)) * 1.6
    return c, vt, np.maximum(r, 1e-4)


def inside(frame, pts):
    c, vt, r = frame
    q = (pts - c) @ vt.T / r
    return (q * q).sum(axis=1) < 1.0


def load_action(path):
    before = set(bpy.data.objects)
    acts_before = set(bpy.data.actions)
    bpy.ops.import_scene.gltf(filepath=str(path))
    new = [o for o in bpy.data.objects if o not in before]
    pa = next((o for o in new if o.type == "ARMATURE"), None)
    act = None
    if pa and pa.animation_data:
        act = pa.animation_data.action or next((s.action for t in pa.animation_data.nla_tracks for s in t.strips), None)
    if act is None:
        act = next((a for a in bpy.data.actions if a not in acts_before), None)
    for o in new:
        bpy.data.objects.remove(o, do_unlink=True)
    return act


def r4(x):
    return round(float(x), 4)


def evaluate(name, act, tiles=None):
    arm.animation_data_create()
    arm.animation_data.action = act
    if getattr(act, "slots", None) and len(act.slots):
        arm.animation_data.action_slot = act.slots[0]
    f0, f1 = act.frame_range
    f0, n = int(round(f0)), int(round(f1 - f0))
    loop = name in LOOPS
    pos, lows, st_p, st_max, over2, joints, foot_low, sock_dev, pen = [], [], [], [], [], [], [], {}, {}
    st_where = (0, {})
    for i in range(n + 1):
        scene.frame_set(f0 + i)
        co = mesh_pos()
        pos.append(co)
        lows.append(float(co[:, 2].min()) - GROUND)
        ratio = np.linalg.norm(co[E0] - co[E1], axis=1) / L0
        st_p.append(float(np.percentile(ratio, 99.9)) if len(ratio) else 1.0)
        if len(ratio) and st_p[-1] >= max(st_p):  # where the worst frame's torn edges are (part: count)
            bad = E0[ratio > st_p[-1]]
            where = {}
            for nm in dom[bad]:
                where[PART.get(nm, "body")] = where.get(PART.get(nm, "body"), 0) + 1
            st_where = (i, where)
        st_max.append(float(ratio.max()) if len(ratio) else 1.0)
        over2.append(int((ratio > 2).sum()))
        jt = {k: bone_head(k) for k in feet + ([head] if head else [])}
        if tail:
            jt["tail_tip"] = bone_tail(tail[-1])
        for c in wings.values():
            if c:
                jt["wtip_" + c[-1]] = bone_tail(c[-1])
        joints.append(jt)
        foot_low.append([float(co[v, 2].min()) - GROUND if len(v) else 0.0 for v in foot_verts])
        for s in SOCKETS:
            m = (W @ PB[s].matrix).inverted()
            off = np.array((m @ Vector(co[SOCK_NEAR[s]].mean(axis=0))).to_tuple())
            sock_dev[s] = max(sock_dev.get(s, 0.0), float(np.linalg.norm(off - sock_off0[s])))
        fr = torso_frame(co)
        if fr is not None:
            for k, v in distal.items():
                if len(v):
                    # share of the limb's distal vertices inside the torso ellipsoid, beyond its rest share
                    pen.setdefault(k, []).append(float(inside(fr, co[v]).mean()))
    P = np.array(pos)  # (n+1, V, 3)
    step = np.linalg.norm(np.diff(P, axis=0), axis=2).max(axis=1)  # per frame, the largest vertex move
    acc_seq = P
    if loop and n >= 3:  # across the wrap too (frame n == frame 0)
        acc_seq = np.concatenate([P[-2:-1], P, P[1:2]], axis=0)
    acc = np.linalg.norm(acc_seq[2:] - 2 * acc_seq[1:-1] + acc_seq[:-2], axis=2).max(axis=1) if len(acc_seq) >= 3 \
        else np.zeros(1)
    disp = np.linalg.norm(P - P[0], axis=2).max(axis=1)
    # pops, per vertex: a jump (one frame's move larger than both neighbours') and a flicker (out on
    # one frame, back on the next: two large opposite moves between small ones). A strike that stops
    # dead is neither (its fast frames come in a run), which acc_max does not tell apart.
    D = np.diff(P, axis=0)  # (n, V, 3)
    S = np.linalg.norm(D, axis=2)
    if loop:
        Sp, Dp = np.concatenate([S[-2:], S, S[:2]]), np.concatenate([D[-2:], D, D[:2]])
    else:
        Sp, Dp = np.concatenate([S[:1], S[:1], S, S[-1:], S[-1:]]), np.concatenate([D[:1], D[:1], D, D[-1:], D[-1:]])
    m = len(S)
    jump = Sp[2:m + 2] - 2 * np.maximum(Sp[1:m + 1], Sp[3:m + 3])  # a 3-frame eased snap (the hit) is not one
    back = (Dp[2:m + 2] * Dp[3:m + 3]).sum(axis=2) < 0
    flick = np.where(back, np.minimum(Sp[2:m + 2], Sp[3:m + 3]) - 2 * np.maximum(Sp[1:m + 1], Sp[4:m + 4]), 0.0)
    popv = np.maximum(jump, flick)
    pop_f, pop_v = (int(x) for x in np.unravel_index(popv.argmax(), popv.shape))
    pop_n = int((popv > 0.02 * HEIGHT).any(axis=1).sum())
    rep = {"frames": n, "loop": loop, "seconds": round(n / 30.0, 3),
           "min_z": r4(min(lows)), "min_z_frac": r4(min(lows) / HEIGHT),
           "low_max_frac": r4(max(lows) / HEIGHT), "low_first_frac": r4(lows[0] / HEIGHT),
           "low_last_frac": r4(lows[-1] / HEIGHT),
           "low_range_frac": r4((max(lows) - min(lows)) / HEIGHT),
           "edge_stretch_p999": round(max(st_p), 3), "edge_stretch_max": round(max(st_max), 3),
           "edges_over_2x": max(over2), "stretch_frame": st_where[0],
           "stretch_where": dict(sorted(st_where[1].items(), key=lambda kv: -kv[1])[:4]),
           "step_max_frac": r4(step.max() / HEIGHT), "step_med_frac": r4(np.median(step) / HEIGHT),
           "step_max_frame": int(step.argmax()),
           "acc_max_frac": r4(acc.max() / HEIGHT), "acc_med_frac": r4(np.median(acc) / HEIGHT),
           "acc_max_frame": int(acc.argmax()),
           "pop_frac": r4(max(0.0, float(popv.max())) / HEIGHT), "pop_frame": pop_f, "pop_frames": pop_n,
           "pop_where": PART.get(dom[pop_v], dom[pop_v] or "body"),
           "motion_frac": r4(disp.max() / HEIGHT),
           "top_last_frac": r4((P[-1][:, 2].max() - GROUND) / HEIGHT),
           "hold_step_frac": r4(step[-3:].max() / HEIGHT) if len(step) >= 3 else 0.0}
    if loop:
        rep["loop_delta_frac"] = r4(np.linalg.norm(P[0] - P[-1], axis=1).max() / HEIGHT)
    if name == "idle":
        IDLE["first"], IDLE["cycle"] = P[0], P[::3]
    elif "first" in IDLE:
        rep["from_idle_m"] = r4(np.linalg.norm(P[0] - IDLE["first"], axis=1).max())
        rep["from_idle_max_m"] = r4(max(np.linalg.norm(P[0] - p, axis=1).max() for p in IDLE["cycle"]))
        rep["from_idle_frac"] = r4(rep["from_idle_m"] / HEIGHT)
        rep["from_idle_max_frac"] = r4(rep["from_idle_max_m"] / HEIGHT)
        if not loop:
            rep["to_idle_frac"] = r4(np.linalg.norm(P[-1] - IDLE["first"], axis=1).max() / HEIGHT)
    if legs:
        fl = np.array(foot_low)  # (n+1, legs)
        rep["feet_min_frac"] = r4(fl.min() / HEIGHT)
        rep["feet_all_up_max_frac"] = r4(fl.min(axis=1).max() / HEIGHT)  # every foot off the ground at once
        per = []
        for k, f in enumerate(feet):
            path = np.array([j[f] for j in joints])
            # in-place cycle: stance = the foot near its lowest; the body moves at the foot's backward speed
            lowz = path[:, 2] - path[:, 2].min()
            stance = lowz < 0.15 * max(1e-6, np.ptp(path[:, 2])) + 0.002 * HEIGHT
            vy = np.diff(path[:, 1]) * 30.0
            sv = -vy[stance[:-1] & stance[1:]] if (stance[:-1] & stance[1:]).any() else np.zeros(1)
            per.append({"foot": f, "leg_len": r4(leg_len[k]),
                        "stride_frac": r4(np.ptp(path[:, 1]) / max(leg_len[k], 1e-6)),
                        "lift_frac": r4(np.ptp(path[:, 2]) / max(leg_len[k], 1e-6)),
                        "slide_frac": r4(np.ptp(path[:, 0]) / max(leg_len[k], 1e-6)),
                        "stance_speed": round(float(np.median(sv)), 3), "stance_share": round(float(stance.mean()), 2)})
        rep["feet"] = per
    for s, c in wings.items():
        if c:
            path = np.array([j["wtip_" + c[-1]] for j in joints])
            rep.setdefault("wing_tip_travel_frac", {})[s] = r4(np.linalg.norm(path - path[0], axis=1).max()
                                                                 / max(chain_len(c), 1e-6))
    if tail:
        path = np.array([j["tail_tip"] for j in joints])
        rep["tail_tip_travel_frac"] = r4(np.linalg.norm(path - path[0], axis=1).max() / max(chain_len(tail), 1e-6))
    if head:
        path = np.array([j[head] for j in joints])
        rep["head_travel_frac"] = r4(np.linalg.norm(path - path[0], axis=1).max() / HEIGHT)
    if SOCKETS:
        rep["socket_drift_frac"] = {s: r4(v / HEIGHT) for s, v in sock_dev.items()}
        if any(s in PB for s in ("hand_r", "grip_r")):
            s = "grip_r" if "grip_r" in PB else "hand_r"
            tr = []
            for i in range(0, n + 1, max(1, n // 12)):
                scene.frame_set(f0 + i)
                tr.append(np.array((W @ PB[s].head)[:]))
            tr = np.array(tr)
            rep["hand_r_travel_frac"] = r4(np.linalg.norm(tr - tr[0], axis=1).max() / HEIGHT)
    if pen:
        rep["limb_in_torso"] = {k: round(max(v) - v[0], 3) for k, v in pen.items()}
    if PROBE and name in ("fly", "idle", "breath", "death"):
        rep["wing_probe_travel_frac"] = wing_probe(P)
    if tiles is not None:
        render_clip(name, n, f0, loop, P, tiles, pop_f if popv.max() > 0.015 * HEIGHT else None)
    arm.animation_data.action = None
    reset_pose()
    return rep


# --- review tiles ----------------------------------------------------------------------
def setup_render():
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.light = "STUDIO"
    try:
        scene.display.shading.color_type = "VERTEX"
    except TypeError:
        scene.display.shading.color_type = "ATTRIBUTE"
    scene.display.shading.show_object_outline = True
    scene.render.resolution_x = scene.render.resolution_y = int(job.get("tile_px", 200))
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
    cam.data.type = "ORTHO"
    scene.collection.objects.link(cam)
    scene.camera = cam
    import bmesh
    me = bpy.data.meshes.new("ground")
    b = bmesh.new()
    bmesh.ops.create_cube(b, size=1.0)
    b.to_mesh(me)
    b.free()
    g = bpy.data.objects.new("ground", me)
    scene.collection.objects.link(g)
    col = me.color_attributes.new("c", "FLOAT_COLOR", "POINT")
    for i in range(len(me.vertices)):
        col.data[i].color = (0.85, 0.1, 0.1, 1)
    # parts colour: left limbs blue, right orange, tail green, wings purple, head yellow, rest grey
    colour = {}
    cx = float(np.mean([(W @ arm.data.bones[f].head_local).x for f in feet])) if feet else \
        float(np.array((W @ arm.data.bones[parts["root"]].head_local)[:])[0]) \
        if isinstance(parts.get("root"), str) and parts["root"] in PB else 0.0
    for ch in legs + list(arms.values()):
        if ch:
            x = float(np.array((W @ arm.data.bones[ch[-1]].head_local)[:])[0])
            for nm in desc(ch[0]):
                colour[nm] = (0.95, 0.55, 0.15) if x >= cx else (0.25, 0.45, 0.95)
    for c in wings.values():
        for nm in desc(c[0]) if c else ():
            colour[nm] = (0.6, 0.3, 0.8)
    for nm in desc(tail[0]) if tail else ():
        colour[nm] = (0.2, 0.7, 0.3)
    if head:
        for nm in desc(head):
            colour[nm] = (0.9, 0.85, 0.2)
    for o in meshes:
        names = {vg.index: vg.name for vg in o.vertex_groups}
        col = o.data.color_attributes.new("parts", "FLOAT_COLOR", "POINT")
        cs = []
        for v in o.data.vertices:
            nm = names.get(max(v.groups, key=lambda g: g.weight).group, "") if len(v.groups) else ""
            cs.extend((*colour.get(nm, (0.78, 0.78, 0.75)), 1.0))
        col.data.foreach_set("color", cs)
        o.data.color_attributes.active_color = col
    for o in bpy.data.objects:
        if o.type == "MESH" and o not in meshes and o.name != "ground":
            o.hide_render = True
    return cam, g


def render_clip(name, n, f0, loop, P, tiles, pop=None):
    """5 side frames, 2 front; with `pop` (the step pop .. pop + 1 is the jump) 4 side frames around it."""
    cam, ground = CAM
    pts = np.concatenate(P[::2])
    k = int(job.get("tile_frames", 5))
    idx = [round(i * n / k) for i in range(k)] if loop else [round(i * n / (k - 1)) for i in range(k)]
    for view in ("side", "front"):
        ax = 0 if view == "front" else 1
        lo, hi = float(pts[:, ax].min()), float(pts[:, ax].max())
        top = float(pts[:, 2].max())
        bottom = min(GROUND, float(pts[:, 2].min()))
        size = 1.1 * max(hi - lo, top - bottom)
        cz = bottom + 0.46 * size
        ch = (lo + hi) / 2
        if view == "front":
            cam.location, cam.rotation_euler = (ch, GROUND + 50 * size, cz), (math.pi / 2, 0, math.pi)
        else:
            cam.location, cam.rotation_euler = (50 * size, ch, cz), (math.pi / 2, 0, math.pi / 2)
        cam.data.ortho_scale, cam.data.clip_end = size, 200 * size
        ground.scale = (20 * size, 20 * size, 0.004 * size)
        ground.location = (0, 0, GROUND - 0.002 * size)
        for j, i in enumerate(idx if view == "side" else idx[1:-1:max(1, (len(idx) - 2) // 2)] or idx[:1]):
            scene.frame_set(f0 + i)
            scene.render.filepath = str(tiles / f"{name}_{view}_{j}.png")
            bpy.ops.render.render(write_still=True)
        if view == "side" and pop is not None:
            for j, i in enumerate(range(pop - 1, pop + 3)):
                scene.frame_set(f0 + (i % n if loop else min(max(i, 0), n)))
                scene.render.filepath = str(tiles / f"{name}_pop_{j}.png")
                bpy.ops.render.render(write_still=True)


LOOPS = {"idle", "walk", "fly", "swim", "run"}
IDLE = {}
# the tail's own vertices: how many, and the share of them within 10% of the height of the ground at
# rest (a tail chain that took a hind leg or the rump stands on the floor); bones it shares with a
# leg or a wing chain (one chain found twice)
TAIL_INFO = {}
if tail:
    tv = np.isin(dom, list(desc(tail[0])))
    TAIL_INFO = {"tail_verts": int(tv.sum()),
                 "tail_low_share": r4(float((REST[tv, 2] - GROUND < 0.1 * HEIGHT).mean()) if tv.any() else 0.0),
                 "tail_shared": sorted({n for n in tail if any(n in ch for ch in legs + list(wings.values()))}),
                 "legs_under_tail": [i for i, ch in enumerate(legs) if ch[0] in desc(tail[0])]}
TILES = Path(job["tiles"]) if job.get("tiles") else None
if TILES:
    TILES.mkdir(parents=True, exist_ok=True)
    CAM = setup_render()
out = {"id": job["id"], "height": r4(HEIGHT), "length": r4(LENGTH), "size": r4(SIZE), "verts": int(len(REST)),
       "edges": int(ok_e.sum()), "ground_rest": r4(GROUND), "legs": len(legs), "arms": sorted(arms),
       "wings": sorted(wings),
       "wing_probe": {s: {k: v for k, v in pr.items() if k != "v"} for s, pr in PROBE.items()},
       "wing_probe_cx": PROBE_CX, "tail_bones": len(tail), **TAIL_INFO, "sockets": SOCKETS, "torso_verts": int(len(torso_v)),
       "clips": {}}
adir = Path(job["anims"])
order = sorted(job["clips"], key=lambda c: (c != "idle", c))  # idle first: from_idle
for c in order:
    p = adir / f"{c}.glb"
    if not p.exists():
        out["clips"][c] = {"error": "missing"}
        continue
    t0 = time.time()
    act = load_action(p)
    if act is None:
        out["clips"][c] = {"error": "no action"}
        continue
    try:
        out["clips"][c] = evaluate(c, act, TILES)
    except Exception as e:  # keep going: one bad clip must not hide the others
        out["clips"][c] = {"error": repr(e)[:300]}
    out["clips"][c]["eval_s"] = round(time.time() - t0, 1)
    bpy.data.actions.remove(act)
out["seconds"] = round(time.time() - t_all, 1)
Path(job["out"]).write_text(json.dumps(out, indent=1))
print("done", job["id"], out["seconds"])

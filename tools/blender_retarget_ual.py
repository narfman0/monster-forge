"""Retarget Quaternius UAL clips onto a SkinTokens biped (docs/anim-reuse.md). Run by retarget_ual.py.

    blender -b --factory-startup --python-exit-code 1 -P tools/blender_retarget_ual.py -- '<job json>'

job: src (model_rigged.glb), bonemap (bonemap.json), packs (UAL glbs, searched in order for each
clip), clips ({our name: UAL clip}), out_dir (<clip>.glb + <clip>.json + metrics.json), tiles
(dir for the review tiles, or null), sheet_clips (clips to render), leg_mode/arm_mode ("delta" |
"dir"), hip_scale (null: our leg length / UAL leg length).

Per frame and per mapped bone, in world space (Blender Z up):
- UAL is turned 180 deg about Z (it faces -Y, ours +Y) and sides are matched by name (bonemap.py).
- D = R_ual_pose @ R_ual_rest^-1: the UAL bone's rotation away from its rest (the T-pose).
- "delta" bones get R_ours = D @ R_ours_rest: our rest pose (A-pose arms, a crouch, a slanted shin)
  moved the way UAL's bone moved. "dir" bones first swing UAL's rest direction (bone -> next mapped
  joint) onto ours (O, the shortest arc), R_ours = D @ O^-1 @ R_ours_rest, so our bone points where
  UAL's points: needed for the arms, since UAL's rest is a T-pose and ours are not.
  Hands take their forearm's O (the wrist keeps its rest bend); clavicle, spine, neck, head, feet
  and balls are "delta".
- Missing UAL bones fold in for free: a mapped bone's world rotation already holds its unmapped
  parents' (spine_02 into spine_03, neck_01 into Head). Skipped and unmapped bones keep their rest
  local pose (identity basis) and follow the parent; the hand_l/r and grip_l/r sockets are bones,
  so they follow the wrist.
- Pelvis: UAL's pelvis offset from rest (relative to UAL root, so no root motion), turned, scaled by
  hip_scale, plus a per-frame ground fix: the lower foot's lowest joint (ankle or ball) is put at
  hip_scale x UAL's, measured from rest, so planted feet stay planted.
"""
import json
import math
import sys
import time
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector

job = json.loads(sys.argv[sys.argv.index("--") + 1])
FPS = 30
SIDES = "lr"
UAL_NEXT = {"pelvis": "spine_01", "spine_01": "spine_02", "spine_02": "spine_03", "spine_03": "neck_01",
            "neck_01": "Head", **{f"{a}_{s}": f"{b}_{s}" for s in SIDES for a, b in
                                  (("clavicle", "upperarm"), ("upperarm", "lowerarm"), ("lowerarm", "hand"),
                                   ("thigh", "calf"), ("calf", "foot"), ("foot", "ball"))}}
R180 = Matrix.Rotation(math.pi, 4, "Z")

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.fps = FPS

# --- ours -------------------------------------------------------------------------------
bpy.ops.import_scene.gltf(filepath=job["src"])
arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
meshes = [o for o in bpy.data.objects if o.type == "MESH" and o.vertex_groups]
ours = set(bpy.data.objects)
for o in bpy.data.objects:
    if o.type == "MESH" and not o.vertex_groups:
        o.hide_render = True  # the bone-shape sphere
W = arm.matrix_world.copy()
Wr = W.to_quaternion()
Wr_inv = Wr.inverted()
bm = json.loads(Path(job["bonemap"]).read_text())
MAP = bm["map"]                       # our bone -> UAL bone
INV = {v: k for k, v in MAP.items()}  # UAL bone -> our bone
bones = list(arm.data.bones)
order, seen = [], set()


def visit(b):
    if b.name in seen:
        return
    if b.parent:
        visit(b.parent)
    seen.add(b.name)
    order.append(b)


for b in bones:
    visit(b)
REST = {b.name: b.matrix_local.copy() for b in bones}
REST_INV = {n: m.inverted() for n, m in REST.items()}
REL = {b.name: (REST_INV[b.parent.name] @ REST[b.name]) if b.parent else REST[b.name] for b in bones}
rest_w = {n: W @ m for n, m in REST.items()}
rest_rot_w = {n: m.to_quaternion() for n, m in rest_w.items()}
rest_pos_w = {n: m.translation.copy() for n, m in rest_w.items()}

z_all = np.concatenate([np.array([(o.matrix_world @ v.co).z for v in o.data.vertices]) for o in meshes])
GROUND = float(z_all.min())
HEIGHT = float(z_all.max() - z_all.min())


def leg_len(pos, thigh, calf, foot, ground):
    return (pos[thigh] - pos[calf]).length + (pos[calf] - pos[foot]).length + (pos[foot].z - ground)


def side_names(s):
    return {u: INV.get(f"{u}_{s}") for u in ("thigh", "calf", "foot", "ball", "upperarm", "lowerarm", "hand")}


our_leg = sum(leg_len(rest_pos_w, *(side_names(s)[k] for k in ("thigh", "calf", "foot")), GROUND) for s in SIDES) / 2

# --- UAL --------------------------------------------------------------------------------
packs = []  # (armature, {clip: action}, mesh objects)
for p in job["packs"]:
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=p)
    new = [o for o in bpy.data.objects if o not in before]
    ua = next(o for o in new if o.type == "ARMATURE")
    acts = {}
    if ua.animation_data:
        for t in ua.animation_data.nla_tracks:
            for s in t.strips:
                if s.action:
                    acts.setdefault(t.name, s.action)
        for t in list(ua.animation_data.nla_tracks):
            ua.animation_data.nla_tracks.remove(t)
    ua.matrix_world = R180 @ ua.matrix_world  # face +Y like ours
    for o in new:
        o.hide_render = True
    packs.append((ua, acts, [o for o in new if o.type == "MESH" and o.vertex_groups]))
bpy.context.view_layer.update()


def ual_for(clip):
    for ua, acts, ms in packs:
        if clip in acts:
            return ua, acts[clip], ms
    raise SystemExit(f"UAL clip {clip} not in any pack")


UA = packs[0][0]
U_REST = {b.name: UA.matrix_world @ b.matrix_local for b in UA.data.bones}
u_rest_rot = {n: m.to_quaternion() for n, m in U_REST.items()}
u_rest_pos = {n: m.translation.copy() for n, m in U_REST.items()}
ual_leg = sum(leg_len(u_rest_pos, f"thigh_{s}", f"calf_{s}", f"foot_{s}", 0.0) for s in SIDES) / 2
HIP = job.get("hip_scale") or our_leg / ual_leg


def set_action(ua, act):
    ad = ua.animation_data or ua.animation_data_create()
    ad.action = act
    if getattr(act, "slots", None) and len(act.slots):
        ad.action_slot = act.slots[0]


# The reference pose the deltas are measured from: UAL's idle (first frame), a relaxed stand like our
# rests, so a rest stance that differs from UAL's (toe-out, a crouch, a forward thigh) is kept rather
# than added to UAL's own; "tpose" measures from UAL's bind pose instead.
REF = job.get("ref", "Idle_Loop")
if REF == "tpose":
    u_ref_rot, u_ref_pos = u_rest_rot, u_rest_pos
else:
    _ua, _act, _ = ual_for(REF)
    set_action(_ua, _act)
    scene.frame_set(int(_act.frame_range[0]))
    _up = {pb.name: _ua.matrix_world @ pb.matrix for pb in _ua.pose.bones}
    u_ref_rot = {n: m.to_quaternion() for n, m in _up.items()}
    u_ref_pos = {n: m.translation.copy() for n, m in _up.items()}

# per-bone alignment O (world): UAL rest direction -> ours, for "dir" bones
MODE = {}
for ob, ub in MAP.items():
    kind = ub.rsplit("_", 1)[0]
    MODE[ob] = job.get("arm_mode", "dir") if kind in ("upperarm", "lowerarm") else \
        job.get("leg_mode", "delta") if kind in ("thigh", "calf") else "delta"
O_INV = {}


def our_dir(ob):
    nxt = UAL_NEXT.get(MAP[ob])
    while nxt and nxt not in INV:  # a missing UAL bone: aim at the next one we have
        nxt = UAL_NEXT.get(nxt)
    return (rest_pos_w[INV[nxt]] - rest_pos_w[ob]) if nxt else None, nxt


for ob, ub in MAP.items():
    if MODE[ob] != "dir":
        continue
    d_t, nxt = our_dir(ob)
    d_s = u_ref_pos[nxt] - u_ref_pos[ub]
    O_INV[ob] = d_s.rotation_difference(d_t).inverted()
# Lying blend: "delta" legs and spine keep our stance (a crouch, toe-out, a hunch) while the body is
# upright, but that stance's offset from UAL's keeps its size under any rotation, so a thigh 19 deg
# off UAL's when standing points 19 deg into the air once the body lies down. As the pelvis turns
# away from upright, these bones blend to UAL's directions ("dir"): fully past LIE_DEG[1].
LIE_DEG = (25.0, 65.0)
O_LIE = {}
if job.get("lying_blend", True):
    for ob, ub in MAP.items():
        kind = ub.rsplit("_", 1)[0]
        if MODE[ob] == "delta" and kind in ("thigh", "calf", "spine_01", "spine_02", "spine_03", "neck_01"):
            d_t, nxt = our_dir(ob)
            if nxt:
                O_LIE[ob] = (u_ref_pos[nxt] - u_ref_pos[ub]).rotation_difference(d_t).inverted()


def lie_weight(up):
    """0 upright .. 1 lying: the trunk's (pelvis -> spine_03) tilt from vertical, beyond the reference's."""
    def tilt(p, c):
        return math.degrees((c - p).angle(Vector((0, 0, 1))))
    a = tilt(up["pelvis"].translation, up["spine_03"].translation) - \
        tilt(u_ref_pos["pelvis"], u_ref_pos["spine_03"])
    u = min(1.0, max(0.0, (a - LIE_DEG[0]) / (LIE_DEG[1] - LIE_DEG[0])))
    return u * u * (3 - 2 * u)


for s in SIDES:  # the hand keeps its rest bend against the forearm
    fa, hd = INV.get(f"lowerarm_{s}"), INV.get(f"hand_{s}")
    if fa in O_INV and hd:
        O_INV[hd] = O_INV[fa]


# --- retarget ---------------------------------------------------------------------------

def ual_pose(ua):
    mw = ua.matrix_world
    return {pb.name: mw @ pb.matrix for pb in ua.pose.bones}


GROUP_OF = {**{INV[f"{k}_{s}"]: f"arm_{s}" for s in SIDES for k in ("upperarm", "lowerarm", "hand")
               if f"{k}_{s}" in INV}, **({INV["Head"]: "head"} if "Head" in INV else {})}
GROUP_ROOT = {g: next(n for n in (INV.get(f"upperarm_{g[-1]}"), INV.get("Head")) if n and GROUP_OF.get(n) == g)
              for g in set(GROUP_OF.values())}


def solve(up, dz, adj=None):
    """Our armature-space bone matrices and basis (loc, quat) for one UAL pose `up`; dz: extra
    world Z on the pelvis."""
    M, basis = {}, {}
    root_d = (up["root"].translation - u_ref_pos["root"]) if "root" in up else Vector()
    lw = lie_weight(up)
    for b in order:
        n = b.name
        chain = (M[b.parent.name] @ REL[n]) if b.parent else REST[n].copy()
        ub = MAP.get(n)
        if ub and ub in up:
            D = up[ub].to_quaternion() @ u_ref_rot[ub].inverted()
            rw = D @ (O_INV[n] if n in O_INV else Quaternion()) @ rest_rot_w[n]
            if n in O_LIE and lw > 0:
                rw = rw.slerp(D @ O_LIE[n] @ rest_rot_w[n], lw)
            if adj and GROUP_OF.get(n) in adj:  # a limb swung rigidly out of the ground (clearance)
                rw = adj[GROUP_OF[n]] @ rw
            ra = (Wr_inv @ rw).normalized()
            pos = chain.translation.copy()
            if ub == "pelvis":
                d = (up[ub].translation - u_ref_pos[ub]) - root_d
                pos = pos + Wr_inv @ (d * HIP + Vector((0, 0, dz)))
            m = Matrix.Translation(pos) @ ra.to_matrix().to_4x4()
        else:
            m = chain
        M[n] = m
        bm_ = chain.inverted() @ m
        basis[n] = (bm_.translation.copy(), bm_.to_quaternion())
    return M, basis


def foot_low(posw, names_by_side, ref):
    """Lowest of (ankle, ball) rise from rest, over both feet."""
    lows = []
    for s in SIDES:
        js = [j for j in names_by_side[s] if j]
        lows.append(min(posw[j].z - ref[j].z for j in js))
    return min(lows)


OUR_FEET = {s: [INV.get(f"foot_{s}"), INV.get(f"ball_{s}")] for s in SIDES}
UAL_FEET = {s: [f"foot_{s}", f"ball_{s}"] for s in SIDES}


def segments(ual_clip):
    """"A+B+C": UAL clips played back to back (Spell_Simple_Enter+Shoot+Exit): [(ua, act, f0, f1, t0)]."""
    segs, t = [], 0.0
    for part in ual_clip.split("+"):
        ua, act, _ = ual_for(part)
        f0, f1 = act.frame_range
        segs.append((ua, act, f0, f1, t))
        t += (f1 - f0) / act_fps
    return segs, t


def seek(segs, total, i, n):
    """Pose the UAL rig at frame i of n over the chained segments; returns (segment index, UAL frame)."""
    t = total * i / n
    k = max(j for j, sg in enumerate(segs) if sg[4] <= t + 1e-9)
    ua, act, f0, f1, t0 = segs[k]
    set_action(ua, act)
    uf = min(f1, f0 + (t - t0) * act_fps)
    scene.frame_set(int(math.floor(uf)), subframe=uf - math.floor(uf))
    return k, uf


def retarget(clip, ual_clip):
    segs, secs = segments(ual_clip)
    ua = segs[0][0]
    n = max(1, round(secs * FPS))
    frames = []
    for i in range(n + 1):
        seg, uf = seek(segs, secs, i, n)
        up = ual_pose(ua)
        u_low = foot_low({k: v.translation for k, v in up.items()}, UAL_FEET, u_ref_pos)
        M, _ = solve(up, 0.0)
        posw = {k: (W @ m).translation for k, m in M.items()}
        o_low = foot_low(posw, OUR_FEET, rest_pos_w)
        dz = HIP * u_low - o_low if job.get("ground_fix", True) else 0.0
        M, basis = solve(up, dz)
        if job.get("debug") and i == (n if job.get("debug") == "last" else 0):
            for s_ in SIDES:
                ch = [INV.get(f"{k}_{s_}") for k in ("clavicle", "upperarm", "lowerarm", "hand", "thigh", "calf", "foot", "ball")]
                uc = [f"{k}_{s_}" for k in ("clavicle", "upperarm", "lowerarm", "hand", "thigh", "calf", "foot", "ball")]
                for a_, b_, ua_, ub_ in zip(ch, ch[1:], uc, uc[1:]):
                    if a_ and b_:
                        do = ((W @ M[b_]).translation - (W @ M[a_]).translation).normalized()
                        du = (up[ub_].translation - up[ua_].translation).normalized()
                        dr = (rest_pos_w[b_] - rest_pos_w[a_]).normalized()
                        if ua_.startswith("hand"):
                            arm_ch = bm["chains"][f"arm_{s_}"]
                            tip = arm_ch[-1] if arm_ch[-1] != a_ else None
                            ub = UA.data.bones[ua_]
                            kids_ = [up[c.name].translation for c in ub.children]
                            du = (sum(kids_, Vector()) / len(kids_) - up[ua_].translation).normalized()
                            if tip:
                                do = ((W @ M[tip]).translation - (W @ M[a_]).translation).normalized()
                                dr = (rest_pos_w[tip] - rest_pos_w[a_]).normalized()
                            else:
                                do = (W @ M[a_]).to_quaternion() @ Vector((0, 1, 0))
                                dr = rest_rot_w[a_] @ Vector((0, 1, 0))
                        print(f"DBG {clip} {ua_}: ours {tuple(round(x, 2) for x in do)} ual {tuple(round(x, 2) for x in du)} "
                              f"rest {tuple(round(x, 2) for x in dr)} angle {math.degrees(do.angle(du)):.0f}")
        frames.append({"basis": basis, "dz": dz, "u_low": u_low, "uf": uf, "up": up,
                       "ual_pts": np.array([v.translation[:] for v in up.values()])})
    return {"frames": frames, "n": n, "seconds": round(secs, 3), "ual_range": [[sg[2], sg[3]] for sg in segs]}


act_fps = job.get("ual_fps", 24)  # the UAL glbs are 24 fps (scene fps after import)

# --- review tiles -----------------------------------------------------------------------
TILES = Path(job["tiles"]) if job.get("tiles") else None
TW, TH = 240, 240


def setup_render():
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.light = "STUDIO"
    try:
        scene.display.shading.color_type = "VERTEX"
    except TypeError:
        scene.display.shading.color_type = "ATTRIBUTE"
    scene.display.shading.show_object_outline = True
    scene.render.resolution_x, scene.render.resolution_y = TW, TH
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    scene.world = scene.world or bpy.data.worlds.new("w")
    cam_data = bpy.data.cameras.new("cam")
    cam_data.type = "ORTHO"
    cam = bpy.data.objects.new("cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    # ground: a thin dark slab with a red top edge band, seen edge-on as a line at z=0
    mesh = bpy.data.meshes.new("ground")
    import bmesh
    b = bmesh.new()
    bmesh.ops.create_cube(b, size=1.0)
    b.to_mesh(mesh)
    b.free()
    g = bpy.data.objects.new("ground", mesh)
    scene.collection.objects.link(g)
    col = mesh.color_attributes.new("lr", "FLOAT_COLOR", "POINT")
    for i in range(len(mesh.vertices)):
        col.data[i].color = (0.85, 0.1, 0.1, 1)
    return cam, g


def lr_colors(objs, side_of):
    """Vertex colour by side: character-left blue, right orange, rest grey."""
    for o in objs:
        names = {vg.index: vg.name for vg in o.vertex_groups}
        side = {i: side_of(nm) for i, nm in names.items()}
        if "lr" in o.data.color_attributes:
            o.data.color_attributes.remove(o.data.color_attributes["lr"])
        col = o.data.color_attributes.new("lr", "FLOAT_COLOR", "POINT")
        cs = []
        for v in o.data.vertices:
            w = {"l": 0.0, "r": 0.0, None: 0.0}
            for g in v.groups:
                w[side.get(g.group)] += g.weight
            c = (0.25, 0.45, 0.95) if w["l"] > max(w["r"], 0.3) else (0.95, 0.55, 0.15) if w["r"] > max(w["l"], 0.3) \
                else (0.78, 0.78, 0.75)
            cs.extend((*c, 1.0))
        col.data.foreach_set("color", cs)
        o.data.color_attributes.active_color = col


def our_side_bones():
    side = {}
    for s in SIDES:
        for ch in ("arm_" + s, "leg_" + s):
            for nm in bm["chains"].get(ch, []):
                for d in [arm.data.bones[nm]] + list(arm.data.bones[nm].children_recursive):
                    side[d.name] = s
    return side


def frame_cam(cam, ground, view, pts, ground_z=0.0):
    """Frame the points (all sampled frames of a clip) square, ground near the bottom."""
    ax = 0 if view == "front" else 1
    lo, hi = float(pts[:, ax].min()), float(pts[:, ax].max())
    top = float(pts[:, 2].max())
    size = 1.1 * max(hi - lo, top - ground_z)
    cz = ground_z + 0.46 * size
    ch = (lo + hi) / 2
    if view == "front":   # looking at the face: the model faces +Y
        cam.location = (ch, ground_z + 50 * size, cz)
        cam.rotation_euler = (math.pi / 2, 0, math.pi)
    else:                 # its right side (character-right is +X)
        cam.location = (50 * size, ch, cz)
        cam.rotation_euler = (math.pi / 2, 0, math.pi / 2)
    cam.data.ortho_scale = size
    cam.data.clip_end = 200 * size
    ground.scale = (20 * size, 20 * size, 0.004 * size)
    ground.location = (0, 0, ground_z - 0.002 * size)


def sheet_frames(n, loop):
    k = 6
    return [round(i * n / k) for i in range(k)] if loop else [round(i * n / (k - 1)) for i in range(k)]


def render(path):
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)


# --- run --------------------------------------------------------------------------------
t_all = time.time()
out = Path(job["out_dir"])
out.mkdir(parents=True, exist_ok=True)
LOOPS = {"idle", "walk", "run"}
results = {}
for clip, uc in job["clips"].items():
    t0 = time.time()
    results[clip] = retarget(clip, uc)
    results[clip]["ual"] = uc
    results[clip]["retarget_s"] = round(time.time() - t0, 1)
    print(f"retarget {clip} <- {uc}: {results[clip]['n']} frames in {results[clip]['retarget_s']}s", flush=True)

if TILES:
    TILES.mkdir(parents=True, exist_ok=True)
    cam, ground = setup_render()
    # UAL reference strips, same frames
    for ua, acts, ms in packs:
        lr_colors(ms, lambda nm: nm[-1] if nm[-2:] in ("_l", "_r") else None)
    for o in ours:
        o.hide_render = True
    for clip in job.get("sheet_clips", []):
        r = results[clip]
        ua, act, ms = ual_for(r["ual"].split("+")[0])
        segs, secs = segments(r["ual"])
        for o in ms:
            o.hide_render = False
        pts = np.concatenate([f["ual_pts"] for f in r["frames"]])
        pts = np.concatenate([pts, pts + [0, 0, 0.12], pts + [0.1, 0.1, 0], pts - [0.1, 0.1, 0]])
        for view in ("side",):
            frame_cam(cam, ground, view, pts)
            for k, i in enumerate(sheet_frames(r["n"], clip in LOOPS)):
                seek(segs, secs, i, r["n"])
                render(TILES / f"{clip}_ual_{view}_{k}.png")
        for o in ms:
            o.hide_render = True

# drop UAL: only our armature and its one action may reach the exporter
for ua, acts, ms in packs:
    for o in [ua] + list(ua.children_recursive):
        bpy.data.objects.remove(o, do_unlink=True)
for o in list(bpy.data.objects):
    if o not in ours and o.name not in ("cam", "ground"):
        bpy.data.objects.remove(o, do_unlink=True)
for a in list(bpy.data.actions):
    bpy.data.actions.remove(a)
for o in ours:
    o.hide_render = o.type != "MESH" or not o.vertex_groups

# rest mesh (edges, vertex sets)
for pb in arm.pose.bones:
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


rest_co = mesh_pos()
edges, foot_mask, offs, dom = [], [], 0, []
foot_bones = set()
for s in SIDES:
    f = INV.get(f"foot_{s}")
    if f:
        foot_bones |= {f} | {d.name for d in arm.data.bones[f].children_recursive}
for o in meshes:
    e = np.empty(len(o.data.edges) * 2, dtype=np.int64)
    o.data.edges.foreach_get("vertices", e)
    edges.append(e.reshape(-1, 2) + offs)
    names = {vg.index: vg.name for vg in o.vertex_groups}
    for v in o.data.vertices:
        wf = sum(g.weight for g in v.groups if names.get(g.group) in foot_bones)
        foot_mask.append(wf >= 0.5)
        dom.append(names.get(max(v.groups, key=lambda g: g.weight).group) if len(v.groups) else None)
    offs += len(o.data.vertices)
edges = np.concatenate(edges)
foot_mask = np.array(foot_mask)
rest_len = np.linalg.norm(rest_co[edges[:, 0]] - rest_co[edges[:, 1]], axis=1)
ok_e = rest_len > 1e-3 * HEIGHT  # stretch of edges long enough to matter (a degenerate sliver says nothing)
if TILES:
    lr_colors(meshes, lambda nm, sb=our_side_bones(): sb.get(nm))
_dom = np.array([d if d else "" for d in dom])
GROUP_VERTS = {}
for g, root_ in GROUP_ROOT.items():  # an arm: the forearm on (the upper arm's own vertices hug the pivot)
    names_ = ({root_} if g == "head" else set()) | {d.name for d in arm.data.bones[root_].children_recursive}
    GROUP_VERTS[g] = np.nonzero(np.isin(_dom, list(names_)))[0]


def gltf_frame(path, name):
    """A node's rest world matrix straight from the glb's JSON, in Blender axes: the prop frame Godot
    uses (the importer may turn bones to point at their children)."""
    import struct
    with open(path, "rb") as f:
        _, _, _, n, _ = struct.unpack("<4sIIII", f.read(20))
        g = json.loads(f.read(n))
    nodes = g["nodes"]
    parent = {c: i for i, nd in enumerate(nodes) for c in nd.get("children", [])}

    def local(nd):
        if "matrix" in nd:
            return Matrix([nd["matrix"][r::4] for r in range(4)])
        t = Matrix.Translation(nd.get("translation", (0, 0, 0)))
        x, y, z, w = nd.get("rotation", (0, 0, 0, 1))
        sc = nd.get("scale", (1, 1, 1))
        return t @ Quaternion((w, x, y, z)).to_matrix().to_4x4() @ Matrix.Diagonal((*sc, 1))
    i = next((k for k, nd in enumerate(nodes) if nd.get("name") == name), None)
    if i is None:
        return None
    m = local(nodes[i])
    while i in parent:
        i = parent[i]
        m = local(nodes[i]) @ m
    yup = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))  # glTF -> Blender
    return yup @ m


def add_blade():
    """A 0.9 m test blade on grip_r (green, +Y along it, edge at -Z): the socket check in the tiles."""
    fr = gltf_frame(job["src"], "grip_r")
    if fr is None or "grip_r" not in arm.pose.bones:
        return None
    import bmesh
    me = bpy.data.meshes.new("blade")
    b = bmesh.new()
    bmesh.ops.create_cube(b, size=1.0)
    bmesh.ops.scale(b, vec=(0.025, 0.9, 0.07), verts=b.verts)
    bmesh.ops.translate(b, vec=(0, 0.45, -0.02), verts=b.verts)
    b.to_mesh(me)
    b.free()
    col = me.color_attributes.new("lr", "FLOAT_COLOR", "POINT")
    for i in range(len(me.vertices)):
        col.data[i].color = (0.1, 0.8, 0.2, 1)
    ob = bpy.data.objects.new("blade", me)
    scene.collection.objects.link(ob)
    ob.parent, ob.parent_type, ob.parent_bone = arm, "BONE", "grip_r"
    bpy.context.view_layer.update()
    ob.matrix_world = fr.normalized()
    return ob


first = {}
if TILES:  # the rest pose
    blade = add_blade()  # the rest pose, for reading the tiles against
    for view in ("front", "side"):
        frame_cam(cam, ground, view, rest_co, GROUND)
        render(TILES / f"rest_{view}.png")


def evaluate(clip, n, loop, tag, f0=1):
    """Metrics over frames 1..n+1 of the armature's current action, and the review tiles."""
    lows, foot_lows, stretch, pos, joints = [], [], [], [], []
    grip = {s: [] for s in ("grip_l", "grip_r", "hand_l", "hand_r")}
    for i in range(n + 1):
        scene.frame_set(i + f0)
        co = mesh_pos()
        pos.append(co)
        lows.append(float(co[:, 2].min()))
        foot_lows.append(float(co[foot_mask, 2].min()) if foot_mask.any() else lows[-1])
        el = np.linalg.norm(co[edges[:, 0]] - co[edges[:, 1]], axis=1)
        ratio = el[ok_e] / rest_len[ok_e]
        stretch.append((float(ratio.max()), float(np.percentile(ratio, 99.9)), int((ratio > 2).sum())))
        joints.append({pb.name: (W @ pb.head) for pb in arm.pose.bones})
        for s in grip:
            if s in arm.pose.bones:
                grip[s].append(W @ arm.pose.bones[s].matrix)
    rep = {"frames": n, "loop": loop, "fps": FPS,
           "min_z": round(min(lows) - GROUND, 4), "min_z_last": round(lows[-1] - GROUND, 4),
           "foot_low_min": round(min(foot_lows) - GROUND, 4), "foot_low_max": round(max(foot_lows) - GROUND, 4),
           "foot_low_min_frac": round((min(foot_lows) - GROUND) / HEIGHT, 4),
           "foot_low_max_frac": round((max(foot_lows) - GROUND) / HEIGHT, 4),
           "edge_stretch_max": round(max(x[0] for x in stretch), 3),
           "edge_stretch_p999": round(max(x[1] for x in stretch), 3),
           "edges_over_2x": max(x[2] for x in stretch)}
    if loop:
        rep["loop_delta"] = round(float(np.linalg.norm(pos[0] - pos[-1], axis=1).max()), 4)
    for s in ("l", "r"):  # sockets: the grip keeps its rest offset from the hand, and how far it travels
        g, h = grip.get(f"grip_{s}"), grip.get(f"hand_{s}")
        if g and h:
            rest_off = (REST_INV[f"hand_{s}"] @ REST[f"grip_{s}"]).translation
            dev = max(((hm.inverted() @ gm).translation - rest_off).length for gm, hm in zip(g, h))
            travel = max((gm.translation - g[0].translation).length for gm in g)
            rep[f"grip_{s}"] = {"offset_dev_m": round(dev, 5), "travel_m": round(travel, 3)}
    base = first.get(tag)
    if clip == "idle":
        first[tag] = {"pos": pos[0], "joints": joints[0], "cycle": pos}
    elif base:
        rep["from_idle_m"] = round(float(np.linalg.norm(pos[0] - base["pos"], axis=1).max()), 4)
        rep["from_idle_joint_m"] = round(max((joints[0][k] - base["joints"][k]).length for k in joints[0]), 4)
        rep["from_idle_max_m"] = round(max(float(np.linalg.norm(pos[0] - p, axis=1).max())
                                           for p in base["cycle"][::4]), 4)
    rep["max_joint_disp_m"] = round(max((j[k] - joints[0][k]).length for j in joints for k in j), 4)
    if TILES and clip in job.get("sheet_clips", []):
        pts = np.concatenate(pos[::3])
        for view in ("front", "side"):
            frame_cam(cam, ground, view, pts, GROUND)
            for k, i in enumerate(sheet_frames(n, loop)):
                scene.frame_set(i + f0)
                render(TILES / f"{clip}_{tag}_{view}_{k}.png")
    return rep


summary = {"id": bm["id"], "height": round(HEIGHT, 3), "hip_scale": round(HIP, 4), "our_leg": round(our_leg, 4),
           "ual_leg": round(ual_leg, 4), "modes": {k: MODE[k] for k in MAP}, "dir_bones": sorted(O_INV),
           "clips": {}, "procedural": {}}
arm.animation_data_create()
moving = [b.name for b in order if b.name in MAP]
FLOOR_TOL = 0.01  # x height: a vertex may sit this far below the ground before the body is raised


def key_clip(clip, r, lift=None):
    """A fresh action with every frame keyed (loc + quat on the mapped bones; the rest stay at rest);
    `lift`: extra world Z on the pelvis per frame."""
    if arm.animation_data.action:
        old = arm.animation_data.action
        arm.animation_data.action = None
        bpy.data.actions.remove(old)
    act = bpy.data.actions.new(clip)
    arm.animation_data.action = act
    prev = {}
    for i, fr in enumerate(r["frames"]):
        basis = solve(fr["up"], fr["dz"] + lift[i], fr.get("adj"))[1] if lift is not None and lift[i] else fr["basis"]
        for nm in moving:
            loc, q = basis[nm]
            if nm in prev and prev[nm].dot(q) < 0:
                q = -q
            prev[nm] = q
            pb = arm.pose.bones[nm]
            pb.rotation_mode = "QUATERNION"
            pb.rotation_quaternion = q
            pb.location = loc
            pb.keyframe_insert("rotation_quaternion", frame=i + 1)
            pb.keyframe_insert("location", frame=i + 1)
    return act


def apply_basis(basis):
    for nm in moving:
        loc, q = basis[nm]
        pb = arm.pose.bones[nm]
        pb.rotation_mode = "QUATERNION"
        pb.location, pb.rotation_quaternion = loc, q
    bpy.context.view_layer.update()


MAX_SWING = 60.0  # degrees


def clearance(r, passes=6):
    """Per frame, swing an arm (about the shoulder) or the head (about the neck) up, rigidly, until no
    vertex of it is more than FLOOR_TOL x height below the ground: our heads, torsos and arms are
    bigger than UAL's mannequin, so a lying pose puts them through the floor. What is left after
    this is the whole-body lift (floor_lift). Returns the largest swing per group, degrees."""
    if arm.animation_data.action:
        arm.animation_data.action = None
    most = {}
    floor = GROUND - FLOOR_TOL * HEIGHT
    for fr in r["frames"]:
        adj = {}
        for _ in range(passes):
            M, basis = solve(fr["up"], fr["dz"], adj)
            apply_basis(basis)
            co = mesh_pos()
            moved = False
            for g, idx in GROUP_VERTS.items():
                if not len(idx):
                    continue
                k = idx[np.argmin(co[idx, 2])]
                deficit = floor - co[k, 2]
                if deficit <= 0:
                    continue
                piv = (W @ M[GROUP_ROOT[g]]).translation
                h = Vector(co[k]) - piv
                axis = h.cross(Vector((0, 0, 1)))
                if axis.length < 1e-6 or h.length < 1e-4:
                    continue
                ang = min(math.asin(min(1.0, 1.15 * deficit / h.length)), math.radians(20))
                q = Quaternion(axis.normalized(), ang) @ adj.get(g, Quaternion())
                if math.degrees(q.angle) > MAX_SWING:  # a limb cannot clear a body that is itself in the ground
                    continue
                adj[g] = q
                moved = True
            if not moved:
                break
        if adj:
            fr["adj"] = adj
            fr["basis"] = solve(fr["up"], fr["dz"], adj)[1]
            for g, q in adj.items():
                most[g] = max(most.get(g, 0.0), math.degrees(q.angle))
    for pb in arm.pose.bones:
        pb.location, pb.rotation_quaternion = Vector(), Quaternion()
    return {g: round(v, 1) for g, v in most.items()}


def floor_lift(r):
    """Per frame, how far the body must rise so no vertex is more than FLOOR_TOL x height below the
    ground (a lying body with our proportions sinks where UAL's thin mannequin does not); dilated and
    smoothed so the rise eases in."""
    n = r["n"] + 1
    deficit = []
    for i in range(n):
        scene.frame_set(i + 1)
        co = mesh_pos()
        deficit.append(max(0.0, GROUND - FLOOR_TOL * HEIGHT - float(co[:, 2].min())))
        if job.get("debug") and i in (n // 2, n - 1):
            lo = np.argsort(co[:, 2])[:200]
            groups = {}
            for k in lo:
                groups[dom[k]] = groups.get(dom[k], 0) + 1
            print(f"DBG low {r['ual']} f{i}: z {co[lo[0], 2] - GROUND:.3f} {sorted(groups.items(), key=lambda x: -x[1])[:5]}"
                  f" ual_map {[MAP.get(g) for g, _ in sorted(groups.items(), key=lambda x: -x[1])[:5]]}")
    if max(deficit) <= 0:
        return None
    dil = [max(deficit[max(0, i - 3):i + 4]) for i in range(n)]
    return [sum(dil[max(0, i - 2):i + 3]) / len(dil[max(0, i - 2):i + 3]) for i in range(n)]


for clip, r in results.items():
    t0 = time.time()
    swung = clearance(r) if job.get("floor", True) else {}
    act = key_clip(clip, r)
    lift = floor_lift(r) if job.get("floor", True) else None
    if lift:
        act = key_clip(clip, r, lift)
    scene.frame_start, scene.frame_end = 1, r["n"] + 1
    rep = {"ual_clip": r["ual"], "seconds": r["seconds"], **evaluate(clip, r["n"], clip in LOOPS, "ours"),
           "pelvis_fix_dz": [round(min(f["dz"] for f in r["frames"]), 4), round(max(f["dz"] for f in r["frames"]), 4)],
           "floor_lift_max": round(max(lift), 4) if lift else 0.0, "floor_lift_last": round(lift[-1], 4) if lift else 0.0, "clearance_deg": swung, "ref": REF,
           "ual_foot_rise_max_frac": round(max(f["u_low"] for f in r["frames"]) / 1.8, 4),
           "lying_blend_max": round(max(lie_weight(f["up"]) for f in r["frames"]), 3)}
    # export like blender_clips.py: armature only, this one action
    bpy.ops.object.select_all(action="DESELECT")
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.export_scene.gltf(filepath=str(out / f"{clip}.glb"), export_format="GLB", use_selection=True,
                              export_yup=True, export_animations=True, export_animation_mode="ACTIONS",
                              export_anim_slide_to_zero=True, export_force_sampling=True)
    rep["build_seconds"] = round(time.time() - t0 + r["retarget_s"], 1)
    (out / f"{clip}.json").write_text(json.dumps(rep, indent=1))
    summary["clips"][clip] = rep
    print(f"{clip}: {json.dumps(rep)}", flush=True)
    arm.animation_data.action = None
    bpy.data.actions.remove(act)
    for pb in arm.pose.bones:
        pb.location, pb.rotation_quaternion = Vector(), Quaternion()
    for fr in r["frames"]:
        fr.pop("up", None)
        fr.pop("adj", None)

# the procedural clips (anims/<clip>.glb), same metrics and tiles, for comparison; after every export
for clip, path in (job.get("baseline") or {}).items():
    if not Path(path).exists():
        continue
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    new = [o for o in bpy.data.objects if o not in before]
    pa = next((o for o in new if o.type == "ARMATURE"), None)
    act = pa.animation_data.action if pa and pa.animation_data and pa.animation_data.action else \
        next((s.action for t in pa.animation_data.nla_tracks for s in t.strips), None) if pa and pa.animation_data else None
    for o in new:
        bpy.data.objects.remove(o, do_unlink=True)
    if not act:
        continue
    arm.animation_data.action = act
    if getattr(act, "slots", None) and len(act.slots):
        arm.animation_data.action_slot = act.slots[0]
    f0, f1 = act.frame_range
    n = int(round(f1 - f0))
    rep = evaluate(clip, n, clip in LOOPS, "proc", int(round(f0)))
    rep["source"] = Path(path).name
    summary["procedural"][clip] = rep
    print(f"proc {clip}: {json.dumps(rep)}", flush=True)
    arm.animation_data.action = None
    for pb in arm.pose.bones:
        pb.location, pb.rotation_quaternion = Vector(), Quaternion()

summary["seconds"] = round(time.time() - t_all, 1)
(out / "metrics.json").write_text(json.dumps(summary, indent=1))
print("done", summary["seconds"])

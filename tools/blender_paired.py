"""Paired animations in Blender (tools/paired.py runs it; docs/anim-reuse.md "Paired animations").

    blender -b --factory-startup --python-exit-code 1 -P tools/blender_paired.py -- <job.json>

mode "author": two copies of UAL1 (initiator at the origin facing -Y, receiver placed and scaled by the
  recipe), each posed per frame from its base UAL clip + the recipe's keyed rotation offsets and waves;
  IK constraints (Blender's) on the recipe's chains, their targets re-placed every frame on the OTHER
  actor's evaluated bones (a few passes, so hands that hold forearms that are themselves IK'd settle);
  legs IK'd to the floor where the base pose had the feet (planted). Writes an npz: per role (frames,
  bones, 4, 4) armature-space pose matrices, the bones' rest matrix_local, and a report (contact
  distances in Blender, per IK chain). tools/paired.py turns them into library clips.
mode "sheet": two skinned meshes (vertex positions per frame from tools/paired.py, glTF model space)
  rendered with Workbench from the side and the front at each listed frame: one PNG per tile.
"""
import json
import math
import sys

import bpy
import numpy as np
from mathutils import Euler, Matrix, Quaternion, Vector

job = json.loads(open(sys.argv[sys.argv.index("--") + 1]).read())
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.fps = 30
CV = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))  # glTF Y-up -> Blender Z-up


def catmull(keys, f):
    """Keys [[frame, value or [values]], ...]: Catmull-Rom through the keys, held outside."""
    if not keys:
        return None
    vals = [np.atleast_1d(np.asarray(k[1], float)) for k in keys]
    fr = [float(k[0]) for k in keys]
    if f <= fr[0]:
        return vals[0]
    if f >= fr[-1]:
        return vals[-1]
    i = max(k for k in range(len(fr) - 1) if fr[k] <= f)
    t = (f - fr[i]) / (fr[i + 1] - fr[i])
    p0, p1, p2, p3 = vals[max(i - 1, 0)], vals[i], vals[i + 1], vals[min(i + 2, len(vals) - 1)]
    # tangents scaled to the key spacing (non-uniform), zero at the ends: no overshoot past held ends
    d0 = fr[i + 1] - fr[i]
    m1 = (p2 - p0) / (fr[i + 1] - fr[max(i - 1, 0)]) * d0 if i > 0 else np.zeros_like(p1)
    m2 = (p3 - p1) / (fr[min(i + 2, len(fr) - 1)] - fr[i]) * d0 if i + 2 < len(fr) else np.zeros_like(p1)
    # a key that equals its neighbour holds: flat tangent there (no wobble on a hold)
    if np.allclose(p1, p0) or np.allclose(p1, p2):
        m1 = np.zeros_like(p1)
    if np.allclose(p2, p1) or np.allclose(p2, p3):
        m2 = np.zeros_like(p1)
    t2, t3 = t * t, t * t * t
    return (2 * t3 - 3 * t2 + 1) * p1 + (t3 - 2 * t2 + t) * m1 + (-2 * t3 + 3 * t2) * p2 + (t3 - t2) * m2


def actor_rot(pitch, yaw, roll):
    return (Matrix.Rotation(math.radians(yaw), 3, "Z") @ Matrix.Rotation(math.radians(roll), 3, "Y")
            @ Matrix.Rotation(math.radians(pitch), 3, "X"))


# ------------------------------------------------------------------------------------------------
def import_ual(path, tag):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    new = [o for o in bpy.data.objects if o not in before]
    arm = next(o for o in new if o.type == "ARMATURE")
    acts = {}
    if arm.animation_data:
        for t in arm.animation_data.nla_tracks:
            for s in t.strips:
                if s.action:
                    s.action.use_fake_user = True
                    acts.setdefault(t.name, s.action)
        for t in list(arm.animation_data.nla_tracks):
            arm.animation_data.nla_tracks.remove(t)
    for o in new:
        if o.type == "MESH" and not o.vertex_groups:
            bpy.data.objects.remove(o)
    arm.name = tag
    return arm, acts


def set_action(o, act):
    ad = o.animation_data or o.animation_data_create()
    ad.action = act
    if act is not None and getattr(act, "slots", None) and len(act.slots):
        ad.action_slot = act.slots[0]


def empty(name, loc=(0, 0, 0)):
    e = bpy.data.objects.new(name, None)
    scene.collection.objects.link(e)
    e.location = loc
    return e


def author():
    rc = job["recipe"]
    n = rc["frames"]
    rest_g = {b: Matrix(m) for b, m in job["rest_gltf"].items()}
    arms, acts = {}, None
    for role in ("initiator", "receiver"):
        arms[role], a = import_ual(job["ual"], role)
        acts = acts or a
    bones = [b.name for b in arms["initiator"].data.bones]
    # C_b: Blender bone frame -> UAL1 glTF bone frame (armature space): world point of a glTF-frame offset
    # = obj.matrix_world @ pb.matrix @ C[b] @ offset
    C = {b: arms["initiator"].data.bones[b].matrix_local.inverted() @ CV @ rest_g[b] for b in bones}
    d = rc["alignment"]["offset"]
    R = arms["receiver"]
    R.location = (0.0, -d, 0.0)   # the initiator faces -Y; the receiver stands d ahead, facing it
    R.rotation_mode = "XYZ"   # the glTF importer leaves objects in quaternion mode
    R.rotation_euler = (0.0, 0.0, math.radians(rc["alignment"]["yaw_deg"]))
    R.scale = (rc["actors"]["receiver"]["scale"],) * 3
    I = arms["initiator"]
    I.scale = (rc["actors"]["initiator"]["scale"],) * 3
    bpy.context.view_layer.update()

    # base clip per frame (pose-bone basis values), sampled once
    base = {}
    for role, arm in arms.items():
        a = rc["actors"][role]
        act = acts[a["base"]]
        f0, f1 = (int(round(x)) for x in act.frame_range)
        L = f1 - f0
        set_action(arm, act)
        rows = []
        for f in range(n + 1):
            scene.frame_set(f0 + (f + a.get("phase", 0)) % L)
            rows.append({pb.name: (pb.rotation_quaternion.copy(), pb.location.copy()) for pb in arm.pose.bones})
        set_action(arm, None)
        base[role] = rows
        for pb in arm.pose.bones:
            pb.rotation_mode = "QUATERNION"

    def set_fk(f):
        for role, arm in arms.items():
            a = rc["actors"][role]
            keys = a.get("keys", {})
            waves = rc.get("waves", {}).get(role, {})
            for pb in arm.pose.bones:
                q, loc = base[role][f][pb.name]
                e = np.zeros(3)
                if pb.name in keys:
                    e += catmull(keys[pb.name], f)
                for w in waves.get(pb.name, []):
                    env = float(catmull(w["env"], f)[0])
                    ax = {"pitch": 0, "yaw": 1, "roll": 2}[w["axis"]]
                    e[ax] += w["amp"] * env * math.sin(2 * math.pi * (f / w["period"] + w["phase"]))
                rest3 = pb.bone.matrix_local.to_3x3()
                if np.any(e):
                    D = rest3.inverted() @ actor_rot(*e) @ rest3
                    q = (D.to_quaternion() @ q).normalized()
                if pb.name + "@loc" in keys:
                    loc = loc + rest3.inverted() @ Vector(catmull(keys[pb.name + "@loc"], f))
                pb.rotation_quaternion = q
                pb.location = loc

    def world_pt(arm, bone, off, frame_bone=None):
        pb = arm.pose.bones[bone]
        fb = arm.pose.bones[frame_bone or bone]
        m = arm.matrix_world @ pb.matrix
        rot = (arm.matrix_world @ fb.matrix @ C[fb.name]).to_3x3()
        # the offset is UAL metres in the glTF bone frame; the object's scale scales it
        return m.translation + rot @ Vector(off)

    # constraints
    cons = []
    for k, c in enumerate(rc["ik"]):
        arm = arms[c["actor"]]
        root, mid, end = c["chain"]
        tgt, pole = empty(f"ik{k}_tgt"), empty(f"ik{k}_pole")
        con = arm.pose.bones[mid].constraints.new("IK")
        con.target, con.pole_target, con.chain_count, con.use_tail = tgt, pole, 2, True
        con.influence = 0.0
        cons.append((c, con, tgt, pole))
    feet = []
    set_fk(0)
    bpy.context.view_layer.update()
    for role, arm in arms.items():
        if not rc["actors"][role].get("plant_feet"):
            continue
        fwd = (arm.matrix_world.to_3x3() @ Vector((0, -1, 0))).normalized()
        for s in "lr":
            pf = arm.pose.bones[f"foot_{s}"]
            pc = arm.pose.bones[f"calf_{s}"]
            tgt = empty(f"{role}_foot_{s}", arm.matrix_world @ pf.head)
            rot = empty(f"{role}_footrot_{s}")
            rot.matrix_world = arm.matrix_world @ pf.matrix
            pole = empty(f"{role}_knee_{s}", arm.matrix_world @ pc.head + fwd * 0.6 * arm.scale[0])
            con = pc.constraints.new("IK")
            con.target, con.pole_target, con.chain_count, con.use_tail = tgt, pole, 2, True
            cr = pf.constraints.new("COPY_ROTATION")
            cr.target = rot
            feet.append((con, arm, pc, pole))

    def calibrate(con, arm, mid, pole_obj):
        """The pole_angle at which the IK reproduces the pose shown now (FK, constraint off) when its
        target sits on the FK end joint: so the IK keeps the animated roll of the chain and only bends
        it towards the pole. Scanned (Blender's pole-angle convention depends on the bones' roll)."""
        tgt = con.target
        keep_t, infl = tgt.location.copy(), con.influence
        con.influence = 0.0
        bpy.context.view_layer.update()
        pb_mid = arm.pose.bones[mid]
        fk = {b.name: b.matrix.to_quaternion() for b in (pb_mid.parent, pb_mid)}
        tgt.location = arm.matrix_world @ pb_mid.tail
        con.influence = 1.0

        def err(deg):
            con.pole_angle = math.radians(deg)
            bpy.context.view_layer.update()
            return sum(arm.pose.bones[n].matrix.to_quaternion().rotation_difference(q).angle for n, q in fk.items())
        best = min((err(d), d) for d in range(-180, 180, 5))
        best = min([best] + [(err(d), d) for d in np.arange(best[1] - 5, best[1] + 5.01, 0.5)])
        con.pole_angle = math.radians(best[1])
        tgt.location, con.influence = keep_t, infl
        bpy.context.view_layer.update()
        return [float(best[1]), round(math.degrees(best[0]), 2)]

    report = {"pole_angle": {}, "contact_m": {}}
    for con, arm, pc, pole in feet:
        report["pole_angle"][f"{arm.name}.{pc.name}"] = calibrate(con, arm, pc.name, pole)

    def place_targets():
        for c, con, tgt, pole in cons:
            arm = arms[c["actor"]]
            other = arms[c["to"]["actor"]]
            end = c["chain"][2]
            p = world_pt(other, c["to"]["bone"], c["to"]["offset"], c["to"].get("frame_bone"))
            tip = world_pt(arm, end, c["from"]["offset"])
            wrist = arm.matrix_world @ arm.pose.bones[end].head
            want = p - (tip - wrist)
            # blend the target from the FK wrist so the constraint's own influence blend stays smooth
            tgt.location = want
            sh = arm.matrix_world @ arm.pose.bones[c["chain"][0]].head
            pole.location = (sh + want) * 0.5 + arm.matrix_world.to_3x3() @ Vector(c["pole"]) * 0.5

    # arm poles: calibrate each chain once, at its first full-contact frame
    for k, (c, con, tgt, pole) in enumerate(cons):
        f_full = int(min(kf[0] for kf in c["influence"] if kf[1] >= 1.0))
        set_fk(f_full)
        for c2, con2, _, _ in cons:
            con2.influence = float(np.clip(catmull(c2["influence"], f_full)[0], 0, 1))
        for _ in range(3):
            bpy.context.view_layer.update()
            place_targets()
        report["pole_angle"][c["name"]] = calibrate(con, arms[c["actor"]], c["chain"][1], pole)

    frames = {r: np.zeros((n + 1, len(bones), 4, 4), np.float64) for r in arms}
    for f in range(n + 1):
        set_fk(f)
        for c, con, tgt, pole in cons:
            con.influence = float(np.clip(catmull(c["influence"], f)[0], 0, 1))
        for _ in range(4):
            bpy.context.view_layer.update()
            place_targets()
        bpy.context.view_layer.update()
        for c, con, tgt, pole in cons:
            if con.influence > 0:
                p = world_pt(arms[c["to"]["actor"]], c["to"]["bone"], c["to"]["offset"], c["to"].get("frame_bone"))
                tip = world_pt(arms[c["actor"]], c["chain"][2], c["from"]["offset"])
                report["contact_m"].setdefault(c["name"], []).append([f, round(con.influence, 3), round((p - tip).length, 4)])
        if f == rc["sync_frame"] and job.get("debug"):
            for c, con, tgt, pole in cons:
                arm = arms[c["actor"]]
                print("DBG", c["name"], "wrist", tuple(arm.matrix_world @ arm.pose.bones[c["chain"][2]].head),
                      "tgt", tuple(tgt.location), "infl", con.influence, "mute", con.mute, "valid", con.is_valid)
        for role, arm in arms.items():
            for k, b in enumerate(bones):
                frames[role][f, k] = np.array(arm.pose.bones[b].matrix)
    for c in cons:
        rows = [r for r in report["contact_m"].get(c[0]["name"], []) if r[1] >= 0.999]
        report.setdefault("contact_max_full_m", {})[c[0]["name"]] = max([r[2] for r in rows] or [0])
    feet_err = 0.0
    rest = np.array([np.array(arms["initiator"].data.bones[b].matrix_local) for b in bones])
    np.savez(job["out"], initiator=frames["initiator"], receiver=frames["receiver"], bones=np.array(bones),
             rest=rest, report=json.dumps({k: v for k, v in report.items() if k != "contact_m"} | {"feet_err": feet_err}))
    if job.get("blend"):
        bpy.ops.wm.save_as_mainfile(filepath=job["blend"])
    print("REPORT", json.dumps({k: v for k, v in report.items() if k != "contact_m"}))


# ------------------------------------------------------------------------------------------------
def sheet():
    z = np.load(job["npz"])
    frames = job["frames"]
    h = job["height"]
    world = bpy.data.worlds.new("w")
    scene.world = world
    world.color = (1, 1, 1)
    scene.render.engine = "BLENDER_WORKBENCH"
    sh = scene.display.shading
    sh.light, sh.color_type = "STUDIO", "OBJECT"
    sh.show_shadows = False
    sh.show_cavity = True
    sh.show_object_outline = True
    scene.render.resolution_x = job["px"]
    scene.render.resolution_y = int(job["px"] * 1.3)
    scene.render.film_transparent = False
    try:
        scene.view_settings.view_transform = "Standard"
    except TypeError:
        pass
    objs = {}
    colors = {"initiator": (0.95, 0.55, 0.2, 1), "receiver": (0.3, 0.55, 0.95, 1)}
    allv = []
    for role in ("initiator", "receiver"):
        tris = z[f"{role}_tris"]
        v0 = z[f"{role}_{frames[0]}"]
        me = bpy.data.meshes.new(role)
        me.from_pydata([tuple(p) for p in (v0 @ np.array(CV)[:3, :3].T)], [], [tuple(t) for t in tris])
        ob = bpy.data.objects.new(role, me)
        ob.color = colors[role]
        scene.collection.objects.link(ob)
        objs[role] = ob
        for f in frames:
            allv.append(z[f"{role}_{f}"])
    allv = np.concatenate(allv) @ np.array(CV)[:3, :3].T
    lo, hi = allv.min(0), allv.max(0)
    centre = Vector(((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2))
    # floor
    bpy.ops.mesh.primitive_plane_add(size=1)
    fl = bpy.context.active_object
    fl.scale = (8 * h, 8 * h, 1)
    fl.color = (0.75, 0.75, 0.72, 1)
    cam_d = bpy.data.cameras.new("cam")
    cam_d.type = "ORTHO"
    cam = bpy.data.objects.new("cam", cam_d)
    scene.collection.objects.link(cam)
    scene.camera = cam
    span = max(hi[2] - 0, hi[1] - lo[1], hi[0] - lo[0])
    cam_d.ortho_scale = span * 1.35
    cam_d.clip_end = 100 * h
    views = {"side": (90.0, 2.0), "front": (25.0, 10.0)}   # yaw from the pair axis, elevation (deg)
    for f in frames:
        for role, ob in objs.items():
            v = z[f"{role}_{f}"] @ np.array(CV)[:3, :3].T
            ob.data.vertices.foreach_set("co", v.astype(np.float32).ravel())
            ob.data.update()
        for name, (yaw, el) in views.items():
            # the pair axis is Blender Y (receiver at -Y... in glTF -Z = Blender +Y); look at the centre
            d = Vector((math.sin(math.radians(yaw)) * math.cos(math.radians(el)),
                        math.cos(math.radians(yaw)) * math.cos(math.radians(el)), math.sin(math.radians(el))))
            tgt = Vector((centre.x, centre.y, (0 + hi[2]) / 2))
            cam.location = tgt + d * 20 * h
            cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
            scene.render.filepath = f"{job['tiles']}/{name}_{f}.png"
            bpy.ops.render.render(write_still=True)
    print("SHEET done")


{"author": author, "sheet": sheet}[job["mode"]]()

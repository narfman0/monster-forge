"""Side-view lean measurement for a batch of monsters (tools/lean_check.py runs it).

    blender -b --factory-startup -P tools/blender_lean.py -- '<job json>'

job: {"items": [{id, kind ("model" | "idle"), src (model.glb or model_rigged.glb), clip
(anims/idle.glb for kind idle), fwd (+1: faces +Y in Blender, glTF -Z; -1: "+Z"), png, json}],
"px": tile size}. Read-only on the inputs: each item writes its own png + json.

Per item: the mesh (for "idle": model_rigged.glb skinned by the idle clip's first frame) is
measured in the side plane (forward, up) and rendered from the side, orthographic, the head
to the right. Angles are degrees from vertical, + forward / - back:

- lean_com: support (vertices in the lowest 3% of the height, mean forward position) to the
  area-weighted surface centroid.
- lean_upper: support to the centroid of the surface above 60% of the height.
- lean_spine: hips band (40-55% of the height) centroid to the head band (top 15%) centroid.
- lean_axis: the principal axis of the surface above 10% of the height (top end sign).
- com_behind: how far the centroid sits behind the support's rear edge, / height (0 when over it).
- core_lean: tools/lean.py core_lean, the body column's line (what QA corrects);
  core_slices are its slice centroids (drawn in orange).
- pitch_torso (horizontal bodies): a line through the centroids of 8 bins over the middle
  half of the length above 30% of the height (the trunk), degrees, + nose up.
- pitch_body: the principal axis of the surface above 35% of the height,
  degrees from horizontal, + nose up.
The png overlay (drawn by lean_check.py from `view`) shows the support, the centroid and the
spine line.
"""
import json
import math
import os
import sys

import bpy
import numpy as np
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lean  # noqa: E402  (tools/lean.py: the body column's lean, shared with blender_qa.py)

job = json.loads(sys.argv[sys.argv.index("--") + 1])
PX = int(job.get("px", 384))


def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    s = bpy.context.scene
    s.render.fps = 30  # before any import (seconds -> frames)
    return s


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


def tri_soup(meshes):
    """World-space triangle centres and areas of the evaluated meshes, plus vertices."""
    dg = bpy.context.evaluated_depsgraph_get()
    cens, areas, verts = [], [], []
    for o in meshes:
        ev = o.evaluated_get(dg)
        m = ev.to_mesh()
        m.calc_loop_triangles()
        co = np.empty(len(m.vertices) * 3)
        m.vertices.foreach_get("co", co)
        M = np.array(ev.matrix_world)
        co = co.reshape(-1, 3) @ M[:3, :3].T + M[:3, 3]
        tri = np.empty(len(m.loop_triangles) * 3, dtype=np.int64)
        m.loop_triangles.foreach_get("vertices", tri)
        tri = tri.reshape(-1, 3)
        ev.to_mesh_clear()
        a, b, c = co[tri[:, 0]], co[tri[:, 1]], co[tri[:, 2]]
        cens.append((a + b + c) / 3)
        areas.append(0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1))
        verts.append(co)
    return np.concatenate(cens), np.concatenate(areas), np.concatenate(verts)


def pca_angle(yz, w):
    """Major axis of weighted (forward, up) points: (angle from vertical, + top forward; elongation)."""
    if len(yz) < 3 or w.sum() <= 0:
        return None, None
    c = yz - np.average(yz, axis=0, weights=w)
    ev, vec = np.linalg.eigh(np.cov(c.T, aweights=w))
    ax = vec[:, int(np.argmax(ev))]
    if ax[1] < 0:
        ax = -ax
    return math.degrees(math.atan2(ax[0], ax[1])), float(math.sqrt(ev.max() / max(ev.min(), 1e-12)))


def measure(cen, area, verts, fwd):
    y, z = cen[:, 1] * fwd, cen[:, 2]
    vy, vz = verts[:, 1] * fwd, verts[:, 2]
    z0, z1 = float(vz.min()), float(vz.max())
    H = max(z1 - z0, 1e-9)
    foot = vz < z0 + 0.03 * H
    ys = float(vy[foot].mean())
    ys_lo, ys_hi = float(vy[foot].min()), float(vy[foot].max())

    def band(lo, hi):
        m = (z >= z0 + lo * H) & (z <= z0 + hi * H)
        if not m.any() or area[m].sum() <= 0:
            return None
        return np.array((np.average(y[m], weights=area[m]), np.average(z[m], weights=area[m])))

    com = np.array((np.average(y, weights=area), np.average(z, weights=area)))
    upper, hips, head = band(0.6, 1.0), band(0.4, 0.55), band(0.85, 1.0)

    def ang(p, q):
        return round(math.degrees(math.atan2(q[0] - p[0], q[1] - p[1])), 2)

    sup = np.array((ys, z0))
    hi = z > z0 + 0.1 * H
    ax, elong = pca_angle(np.c_[y[hi], z[hi]], area[hi])
    top = z > z0 + 0.35 * H
    pa, pel = pca_angle(np.c_[y[top], z[top]], area[top])
    pitch = None  # the major axis from horizontal; + when the front end is the higher one
    if pa is not None:
        a = math.radians(pa)
        dy, dz = math.sin(a), math.cos(a)  # top end of the axis
        if dy < 0:
            dy, dz = -dy, -dz
        pitch = round(math.degrees(math.atan2(dz, dy)), 2)
    L = float(np.ptp(vy))
    # torso pitch: centroids of 8 bins over the middle half of the length, above 30% of the
    # height (the trunk, not the legs, head or tail); line z(y), + nose up
    y0 = float(vy.min())
    tor = []
    for i in range(8):
        a_, b_ = y0 + L * (0.25 + 0.5 * i / 8), y0 + L * (0.25 + 0.5 * (i + 1) / 8)
        m = (y >= a_) & (y < b_) & (z > z0 + 0.3 * H)
        if area[m].sum() > 0:
            tor.append((np.average(y[m], weights=area[m]), np.average(z[m], weights=area[m])))
    pitch_torso = None
    if len(tor) >= 4:
        t_ = np.array(tor)
        pitch_torso = round(math.degrees(math.atan(np.polyfit(t_[:, 0], t_[:, 1], 1)[0])), 2)
    cl = lean.core_lean(cen, area, fwd)
    sl = lean.slices(cen, area, fwd)
    return {
        "core_lean": cl["lean"], "core_fits": cl["fits"], "core_spread": cl["spread"],
        "core_slices": [[round(z0 + a * H, 4), round(b * H, 4)] for a, b, _ in sl.tolist()],
        "height": round(H, 4), "length": round(L, 4),
        "support_y": round(ys, 4), "support_span": [round(ys_lo, 4), round(ys_hi, 4)],
        "com": [round(float(x), 4) for x in com],
        "upper": None if upper is None else [round(float(x), 4) for x in upper],
        "hips": None if hips is None else [round(float(x), 4) for x in hips],
        "head": None if head is None else [round(float(x), 4) for x in head],
        "lean_com": ang(sup, com), "lean_upper": None if upper is None else ang(sup, upper),
        "lean_spine": None if hips is None or head is None else ang(hips, head),
        "lean_axis": None if ax is None else round(ax, 2), "axis_elong": None if elong is None else round(elong, 2),
        "pitch_body": pitch, "pitch_torso": pitch_torso,
        "torso_pts": [[round(float(a), 4), round(float(b), 4)] for a, b in tor], "pitch_elong": None if pel is None else round(pel, 2),
        "com_behind": round(max(0.0, ys_lo - float(com[0])) / H, 4),
        "com_ahead": round(max(0.0, float(com[0]) - ys_hi) / H, 4),
        "z0": round(z0, 4),
    }


def render(scene, verts, fwd, png):
    scene.render.engine = "BLENDER_WORKBENCH"
    sh = scene.display.shading
    sh.light = "STUDIO"
    sh.color_type = "TEXTURE"
    sh.background_type = "VIEWPORT"
    sh.background_color = (0.86, 0.86, 0.86)
    scene.view_settings.view_transform = "Standard"
    scene.render.resolution_x = scene.render.resolution_y = PX
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    mn, mx = verts.min(0), verts.max(0)
    S = float(max(mx[2] - mn[2], mx[1] - mn[1])) * 1.12
    cy, cz = float((mn[1] + mx[1]) / 2), float(mn[2] + S / 2 - 0.04 * S)
    cd = bpy.data.cameras.new("cam")
    cd.type = "ORTHO"
    cd.ortho_scale = S
    cd.clip_end = 100 * S
    cam = bpy.data.objects.new("cam", cd)
    scene.collection.objects.link(cam)
    scene.camera = cam
    side = Vector((fwd, 0, 0))  # camera on +X for a +Y-facing body: forward is screen right
    cam.location = Vector((0, cy, cz)) + side * (10 * S)
    cam.rotation_euler = (-side).to_track_quat("-Z", "Y").to_euler()
    scene.render.filepath = png
    bpy.ops.render.render(write_still=True)
    # pixel = ((fwd*y - cyf) / S + 0.5) * PX, (0.5 - (z - cz) / S) * PX
    return {"S": S, "cy": cy * fwd, "cz": cz, "px": PX}


for it in job["items"]:
    try:
        scene = reset()
        bpy.ops.import_scene.gltf(filepath=it["src"])
        arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
        meshes = [o for o in bpy.data.objects if o.type == "MESH"]
        frame = None
        if it["kind"] == "idle":
            act = load_action(it["clip"])
            if arm is None or act is None:
                raise RuntimeError("no armature or idle action")
            meshes = [o for o in bpy.data.objects if o.type == "MESH" and o.vertex_groups]
            arm.animation_data_create()
            arm.animation_data.action = act
            if getattr(act, "slots", None) and len(act.slots):
                arm.animation_data.action_slot = act.slots[0]
            frame = int(round(act.frame_range[0]))
            scene.frame_set(frame)
        cen, area, verts = tri_soup(meshes)
        rep = {"id": it["id"], "kind": it["kind"], "src": it["src"], "frame": frame, "fwd": it["fwd"]}
        rep.update(measure(cen, area, verts, it["fwd"]))
        rep["view"] = render(scene, verts, it["fwd"], it["png"])
    except Exception as e:  # keep going with the next item
        rep = {"id": it["id"], "kind": it["kind"], "error": f"{type(e).__name__}: {e}"}
    with open(it["json"], "w") as f:
        json.dump(rep, f, indent=1)
    print("LEAN_ITEM", it["id"], it["kind"], rep.get("error", "ok"), flush=True)
print("LEAN_OK")

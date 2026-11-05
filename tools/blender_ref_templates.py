"""Depth-map pose templates for the ref stage's ControlNet (body plans a text prompt cannot pose).

    blender -b --factory-startup -P tools/blender_ref_templates.py -- [outdir=workflows/pose] [name ...]

Each template is a proxy body built from tapered capsules (cones + joint spheres), rendered
orthographically to a 1024 x 1024 depth map in the MiDaS convention controlnet-union's depth
mode expects (near = white, far = dark grey, background black). forge.py's REF_TEMPLATES
table maps body plans to these files (docs/references.md "Pose templates").

- serpentine_side_depth.png: a 4 m snake stretched along X in a shallow S on the ground,
  head raised a little at +X, seen from the side (camera 12 degrees above the ground).
- centaur_34_depth.png: a human torso rising from the front of a horse barrel (no horse
  neck or head), four legs with all hooves on the ground, arms a little away from the body
  with open hands, a tail; three-quarter front view.
"""
import math
import os
import sys

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
OUT = argv[0] if argv else "workflows/pose"
ONLY = set(argv[1:])
RES = 1024


def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def sphere(p, r):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, radius=r, location=p)
    bpy.ops.object.shade_smooth()


def capsule(a, b, ra, rb, joints=True):
    """Tapered cone from a (radius ra) to b (radius rb) with spheres at both ends."""
    a, b = Vector(a), Vector(b)
    d = b - a
    L = d.length
    if L < 1e-6:
        return
    bpy.ops.mesh.primitive_cone_add(vertices=32, radius1=ra, radius2=rb, depth=L, location=(a + b) / 2)
    ob = bpy.context.active_object
    ob.rotation_mode = "QUATERNION"
    ob.rotation_quaternion = Vector((0, 0, 1)).rotation_difference(d.normalized())
    bpy.ops.object.shade_smooth()
    if joints:
        sphere(a, ra)
        sphere(b, rb)


def chain(pts, radii):
    for i in range(len(pts) - 1):
        capsule(pts[i], pts[i + 1], radii[i], radii[i + 1])


def ellipsoid(p, sx, sy, sz):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=1, location=p)
    ob = bpy.context.active_object
    ob.scale = (sx, sy, sz)
    bpy.ops.object.shade_smooth()


# ---------------------------------------------------------------- proxies
def snake():
    """4 m snake along X, head at +X, shallow S (one sine period, 0.28 m amplitude) on the ground."""
    n, L, A = 80, 4.0, 0.42
    pts, radii = [], []
    for i in range(n + 1):
        t = i / n                              # 0 = tail tip, 1 = snout
        x = -L / 2 + t * L
        y = A * math.sin(2 * math.pi * t * 1.0)
        # body thickness: thin tail, thickest at ~40%, a neck behind the head
        if t < 0.35:
            r = 0.015 + 0.15 * math.sin(0.5 * math.pi * t / 0.35)
        elif t < 0.65:
            r = 0.165
        else:
            r = 0.165 - 0.07 * min(1.0, (t - 0.65) / 0.25)
        lift = 0.0
        if t > 0.86:                           # head raised a little
            lift = 0.32 * ((t - 0.86) / 0.14) ** 1.6
        pts.append((x, y, r + lift))
        radii.append(r)
    chain(pts, radii)
    # head: a wedge a bit wider than the neck, snout forward
    hx, hy, hz = pts[-1]
    ellipsoid((hx + 0.08, hy, hz + 0.005), 0.17, 0.115, 0.08)
    ellipsoid((hx + 0.2, hy, hz - 0.01), 0.09, 0.08, 0.06)
    return {"view": (0.0, -1.0, math.tan(math.radians(20))), "margin": 1.08}


def centaur():
    """Centaur facing +Y: horse barrel and legs, human torso where the horse's neck would be."""
    x = 0.17                                    # leg half-spacing
    # horse barrel, chest and rump
    ellipsoid((0, -0.12, 1.22), 0.32, 0.78, 0.34)
    ellipsoid((0, 0.62, 1.18), 0.28, 0.30, 0.31)       # chest, ahead of the rider-less torso
    ellipsoid((0, -0.72, 1.26), 0.31, 0.28, 0.32)     # rump
    # front legs: straight, hooves under the shoulders
    for s in (-1, 1):
        fy = 0.50 if s > 0 else 0.40            # one front leg a little ahead of the other
        chain([(s * x, fy, 1.02), (s * x, fy + 0.03, 0.56), (s * x, fy + 0.02, 0.18), (s * x, fy + 0.07, 0.05)],
              [0.11, 0.065, 0.035, 0.06])
        # hind legs: thigh back to the hock, cannon down to the hoof
        chain([(s * x, -0.70, 1.10), (s * (x + 0.01), -0.58 - 0.08 * (s < 0), 0.70), (s * x, -0.80 - 0.08 * (s < 0), 0.42), (s * x, -0.74 - 0.08 * (s < 0), 0.05)],
              [0.14, 0.075, 0.038, 0.06])
    # tail
    chain([(0, -0.98, 1.38), (0, -1.10, 1.15), (0, -1.12, 0.80)], [0.07, 0.06, 0.035])
    # human torso rising from the chest: waist, ribcage, shoulders
    capsule((0, 0.58, 1.38), (0, 0.62, 1.70), 0.19, 0.20)
    ellipsoid((0, 0.62, 1.86), 0.24, 0.15, 0.20)       # ribcage / chest
    capsule((0, 0.62, 2.02), (0, 0.63, 2.10), 0.06, 0.06)  # neck
    ellipsoid((0, 0.65, 2.22), 0.095, 0.11, 0.125)      # head
    for s in (-1, 1):
        sh = (s * 0.25, 0.62, 1.98)
        el = (s * 0.40, 0.66, 1.70)
        wr = (s * 0.50, 0.70, 1.46)
        chain([sh, el, wr], [0.075, 0.055, 0.04])
        ellipsoid((s * 0.53, 0.71, 1.38), 0.035, 0.055, 0.075)   # open hand
    return {"view": (math.sin(math.radians(65)), math.cos(math.radians(65)), math.tan(math.radians(10))),
            "margin": 1.12}


TEMPLATES = {"serpentine_side_depth": snake, "centaur_34_depth": centaur}


# ---------------------------------------------------------------- render
def render(name, build):
    reset()
    spec = build()
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    view = Vector(spec["view"]).normalized()           # direction from target to camera
    pts = [o.matrix_world @ v.co for o in meshes for v in o.data.vertices]
    lo = Vector([min(p[i] for p in pts) for i in range(3)])
    hi = Vector([max(p[i] for p in pts) for i in range(3)])
    centre = (lo + hi) / 2
    cam_data = bpy.data.cameras.new("cam")
    cam_data.type = "ORTHO"
    cam = bpy.data.objects.new("cam", cam_data)
    bpy.context.scene.collection.objects.link(cam)
    cam.location = centre + view * 20
    cam.rotation_mode = "QUATERNION"
    cam.rotation_quaternion = (-view).to_track_quat("-Z", "Y")
    bpy.context.view_layer.update()
    inv = cam.matrix_world.inverted()
    cp = [inv @ p for p in pts]
    w = max(p.x for p in cp) - min(p.x for p in cp)
    h = max(p.y for p in cp) - min(p.y for p in cp)
    cam_data.ortho_scale = max(w, h) * spec["margin"]
    # recentre on the projected box
    mx = (max(p.x for p in cp) + min(p.x for p in cp)) / 2
    my = (max(p.y for p in cp) + min(p.y for p in cp)) / 2
    cam.location = cam.matrix_world @ Vector((mx, my, 0))
    bpy.context.view_layer.update()
    inv = cam.matrix_world.inverted()
    zs = [-(inv @ p).z for p in pts]
    near, far = min(zs), max(zs)
    cam_data.clip_start, cam_data.clip_end = 0.1, far + 5
    bpy.context.scene.camera = cam

    mat = bpy.data.materials.new("depth")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    camn = nt.nodes.new("ShaderNodeCameraData")
    mr = nt.nodes.new("ShaderNodeMapRange")
    mr.inputs["From Min"].default_value = near
    mr.inputs["From Max"].default_value = far
    mr.inputs["To Min"].default_value = 1.0
    mr.inputs["To Max"].default_value = 0.0
    em = nt.nodes.new("ShaderNodeEmission")
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(camn.outputs["View Z Depth"], mr.inputs["Value"])
    nt.links.new(mr.outputs["Result"], em.inputs["Color"])
    nt.links.new(em.outputs["Emission"], out.inputs["Surface"])
    for o in meshes:
        o.data.materials.clear()
        o.data.materials.append(mat)

    sc = bpy.context.scene
    world = bpy.data.worlds.new("w")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0, 0, 0, 1)
    sc.world = world
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = 16
    sc.cycles.use_denoising = False
    sc.render.resolution_x = sc.render.resolution_y = RES
    sc.render.film_transparent = False
    sc.view_settings.view_transform = "Standard"
    sc.view_settings.look = "None"
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGB"
    path = os.path.abspath(os.path.join(OUT, name + ".png"))
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)
    print(f"wrote {path} (depth {near:.2f}..{far:.2f} m, ortho {cam_data.ortho_scale:.2f} m)")


os.makedirs(OUT, exist_ok=True)
for name, fn in TEMPLATES.items():
    if not ONLY or name in ONLY:
        render(name, fn)

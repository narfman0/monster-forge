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

One-body multi-head and hybrid quadrupeds (chosen per id with the override `ref_template`; the
point is ONE body silhouette, so the model cannot draw two dogs or three stacked animals):

- `quadruped(body, necks, wings, tail, head_scale, wing_y)`: the reusable builder. One barrel,
  chest and rump (`body` lion / dog / reptile proportions), four legs with every foot down, any
  number of Bezier necks from the top of the chest, each ending in a `head(kind)` (reptile, dog,
  lion, goat, human: a face under a headdress cap with two flaps, woman: a face, no flaps), optional raised wings
  (`feathered` or `membrane`). New multi-head monsters: a new function calling it.
- hydra_34_depth.png: low reptile body, short tail, five necks fanned from forward-low to
  up-and-back, a reptile head each; side view, 20 degrees from the front.
- twohead_dog_34_depth.png: lean dog, two necks splayed left and right from the same chest (the
  far head higher); three-quarter front view.
- sphinx_side_depth.png: lion body, raised feathered wings, an upright neck and a `woman` head
  (a face with a cloth/hair mass down the back of the neck; side flaps read as dog ears); side view.
- chimera_34_depth.png: lion body, membrane wings mid-back, goat neck rising on the far side,
  maned lion head in front, dragon head on a long neck reaching forward; side view.

Rig-friendly bipeds (2026-11-08, docs/references.md "Rig-friendly poses"): image-to-3D fuses parts
that touch in the picture, so these keep every appendage clear of the legs and arms. `biped(wings,
tail)`: a front-view humanoid in the wide A-pose (arms 45 degrees out, feet apart); each also
writes `<name>_keypoints.json` (COCO-18 pixel positions from the same camera) that
tools/pose_skeleton.py turns into the matching `<name>_openpose.png`, so the OpenPose and depth
images line up.

- biped_wings_front_depth.png: two membrane wings from the upper back, raised up and out (tips well
  above the head, the lower edge above the shoulders, clear of the arms).
- biped_tail_front_depth.png: a tail from behind the hips held out to the figure's left at hip
  height (under the hand), its end curving down well outside the foot.
- biped_wings_tail_front_depth.png: both.
- biped_*_small_depth.png: the same in stocky tiny/small proportions (bigger head, shorter legs).
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


def slab(a, b, width, thick, side=(1, 0, 0)):
    """A flat ellipsoid from a to b (a wing membrane, a headdress flap): `width` across, along
    the direction `side` (made perpendicular to b - a), `thick` through it."""
    a, b = Vector(a), Vector(b)
    z = (b - a).normalized()
    y = Vector(side) - z * Vector(side).dot(z)
    y = y.normalized()
    x = y.cross(z)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=1, location=(a + b) / 2)
    ob = bpy.context.active_object
    from mathutils import Matrix
    rot = Matrix((x, y, z)).transposed().to_4x4()
    ob.matrix_world = Matrix.Translation((a + b) / 2) @ rot @ Matrix.Diagonal((thick, width / 2, (b - a).length / 2, 1))
    bpy.ops.object.shade_smooth()


def bezier(p0, p1, p2, p3, n=10):
    """Points on a cubic Bezier (a curved neck)."""
    out = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        out.append(tuple(u ** 3 * p0[k] + 3 * u * u * t * p1[k] + 3 * u * t * t * p2[k] + t ** 3 * p3[k] for k in range(3)))
    return out


def head(kind, p, d, k=1.0):
    """A head at p, snout along the direction d (unit-ish), scaled by k. kind: reptile, dog, lion, goat, human."""
    p, d = Vector(p), Vector(d).normalized()
    before = set(bpy.context.scene.objects)
    if kind == "reptile":                       # long wedge skull, jaw, brow ridges
        capsule(p, p + d * 0.34, 0.12, 0.055)
        capsule(p + Vector((0, 0, -0.05)), p + d * 0.30 + Vector((0, 0, -0.06)), 0.08, 0.04)
        for s in (-1, 1):
            capsule(p + Vector((s * 0.06, 0, 0.08)), p - d * 0.14 + Vector((s * 0.07, 0, 0.17)), 0.03, 0.01)
    elif kind == "dog":                         # skull, muzzle, pricked ears
        sphere(p, 0.11)
        capsule(p + d * 0.05 + Vector((0, 0, -0.02)), p + d * 0.24 + Vector((0, 0, -0.04)), 0.075, 0.05)
        for s in (-1, 1):
            capsule(p + Vector((s * 0.06, 0, 0.07)) - d * 0.03, p + Vector((s * 0.08, 0, 0.17)) - d * 0.06, 0.04, 0.008)
    elif kind == "small":                       # a small mammal: round skull, tapering snout, small round ears
        sphere(p, 0.10)
        capsule(p + d * 0.04 + Vector((0, 0, -0.01)), p + d * 0.20 + Vector((0, 0, -0.03)), 0.075, 0.03)
        for s in (-1, 1):
            ellipsoid(p + Vector((s * 0.07, 0, 0.07)) - d * 0.04, 0.035, 0.015, 0.035)
    elif kind == "lion":                        # skull, muzzle and a mane ruff
        ellipsoid(p - d * 0.06, 0.24, 0.20, 0.26)
        sphere(p + d * 0.06, 0.15)
        capsule(p + d * 0.12 + Vector((0, 0, -0.03)), p + d * 0.27 + Vector((0, 0, -0.05)), 0.10, 0.075)
    elif kind == "goat":                        # narrow skull, long face, swept-back horns, beard
        sphere(p, 0.10)
        capsule(p + d * 0.04, p + d * 0.24 + Vector((0, 0, -0.08)), 0.075, 0.045)
        capsule(p + d * 0.16 + Vector((0, 0, -0.12)), p + d * 0.12 + Vector((0, 0, -0.24)), 0.03, 0.01)
        for s in (-1, 1):
            chain([p + Vector((s * 0.05, 0, 0.08)), p - d * 0.10 + Vector((s * 0.07, 0, 0.22)),
                   p - d * 0.26 + Vector((s * 0.08, 0, 0.18))], [0.035, 0.025, 0.01])
    elif kind == "human":                       # a face (nose, chin) under a striped-headdress shape
        ellipsoid(p, 0.11, 0.12, 0.14)
        ellipsoid(p + d * 0.10 + Vector((0, 0, -0.03)), 0.075, 0.05, 0.09)   # face plane
        capsule(p + d * 0.13 + Vector((0, 0, 0.0)), p + d * 0.17 + Vector((0, 0, -0.04)), 0.02, 0.015)  # nose
        # headdress: a cap over the crown and two flaps falling behind the jaw onto the chest
        ellipsoid(p + Vector((0, 0, 0.04)) - d * 0.02, 0.15, 0.15, 0.13)
        for s in (-1, 1):
            slab(p + Vector((s * 0.12, 0, 0.02)), p + Vector((s * 0.15, 0, -0.30)) + d * 0.03, 0.14, 0.03, d)
        capsule(p - d * 0.10, p - d * 0.16 + Vector((0, 0, -0.30)), 0.08, 0.05)  # the queue at the back
    elif kind == "woman":                       # a human face with no side flaps (they read as dog ears):
        ellipsoid(p, 0.10, 0.115, 0.14)         # skull, a flat face plane, nose, chin; a cloth/hair
        ellipsoid(p + d * 0.08 + Vector((0, 0, -0.02)), 0.085, 0.05, 0.11)   # mass down the back of the neck
        capsule(p + d * 0.12 + Vector((0, 0, 0.01)), p + d * 0.16 + Vector((0, 0, -0.03)), 0.02, 0.016)
        sphere(p + d * 0.07 + Vector((0, 0, -0.12)), 0.05)
        ellipsoid(p + Vector((0, 0, 0.05)) - d * 0.03, 0.12, 0.13, 0.12)
        capsule(p - d * 0.08 + Vector((0, 0, 0.02)), p - d * 0.14 + Vector((0, 0, -0.30)), 0.11, 0.09)
    else:
        raise ValueError(kind)
    if k != 1.0:                                # scale the new parts about the head point
        from mathutils import Matrix
        m = Matrix.Translation(p) @ Matrix.Scale(k, 4) @ Matrix.Translation(-p)
        for ob in set(bpy.context.scene.objects) - before:
            ob.matrix_world = m @ ob.matrix_world


def quadruped(body="lion", necks=(), wings=None, tail=True, head_scale=1.0, wing_y=-0.25):
    """A four-legged body facing +Y with any number of necks/heads from its shoulders: ONE barrel,
    chest and rump, four legs with every foot on the ground. `body`: lion, dog or reptile
    (proportions). `necks`: [(kind, [base, c1, c2, tip], neck radius at base, at tip), ...] with
    points as (x, y, z) offsets from the top of the chest; the head sits at `tip`, its snout along
    the last neck segment (tilted level). `wings`: None, "feathered" or "membrane": a raised pair
    from the shoulders, swept up and back. Returns the top of the chest (the neck origin)."""
    P = {  # barrel centre z, barrel radii, chest/rump y, leg x, leg top z, leg radius, foot, back length
        "lion":    dict(z=0.95, r=(0.30, 0.72, 0.31), cy=0.55, ry=-0.62, lx=0.17, lr=0.10, foot=(0.09, 0.12, 0.05)),
        "dog":     dict(z=0.72, r=(0.20, 0.55, 0.22), cy=0.42, ry=-0.45, lx=0.11, lr=0.06, foot=(0.06, 0.08, 0.035)),
        "reptile": dict(z=0.62, r=(0.42, 0.85, 0.34), cy=0.62, ry=-0.70, lx=0.34, lr=0.13, foot=(0.12, 0.16, 0.05)),
    }[body]
    z, (rx, ry_, rz) = P["z"], P["r"]
    ellipsoid((0, 0, z), rx, ry_, rz)
    ellipsoid((0, P["cy"], z + 0.04), rx * 0.95, rx * 1.0, rz * 1.08)          # chest
    ellipsoid((0, P["ry"], z + 0.03), rx * 0.95, rx * 0.95, rz * 1.02)         # rump
    lx, lr, foot = P["lx"], P["lr"], P["foot"]
    for s in (-1, 1):
        fy = P["cy"] + (0.04 if s > 0 else -0.04)
        by = P["ry"] + (0.03 if s > 0 else -0.05)
        if body == "reptile":                   # splayed: elbow out, foot under it
            chain([(s * lx * 0.8, fy, z), (s * (lx + 0.12), fy + 0.04, z * 0.55), (s * (lx + 0.10), fy + 0.06, foot[2])],
                  [lr, lr * 0.8, lr * 0.6])
            chain([(s * lx * 0.8, by, z), (s * (lx + 0.14), by - 0.06, z * 0.6), (s * (lx + 0.12), by, foot[2])],
                  [lr * 1.2, lr * 0.85, lr * 0.6])
        else:
            chain([(s * lx, fy, z), (s * lx, fy + 0.02, z * 0.5), (s * lx, fy + 0.03, foot[2])], [lr, lr * 0.75, lr * 0.65])
            chain([(s * lx, by, z + 0.02), (s * lx, by + 0.12, z * 0.62), (s * lx, by - 0.06, z * 0.28),
                   (s * lx, by - 0.02, foot[2])], [lr * 1.3, lr * 0.8, lr * 0.6, lr * 0.65])
        ellipsoid((s * (lx + (0.10 if body == "reptile" else 0)), fy + 0.07, foot[2]), foot[0], foot[1], foot[2])
        ellipsoid((s * (lx + (0.12 if body == "reptile" else 0)), by + 0.03, foot[2]), foot[0], foot[1], foot[2])
    if tail:
        t0 = (0, P["ry"] - rx * 0.8, z + 0.08)
        if body == "reptile":                   # heavy tail lying back along the ground
            if tail == "short":
                chain([t0, (0, t0[1] - 0.45, z * 0.5), (0.15, t0[1] - 0.85, 0.10)], [0.24, 0.14, 0.04])
            else:
                chain([t0, (0, t0[1] - 0.6, z * 0.5), (0.2, t0[1] - 1.2, 0.12), (0.45, t0[1] - 1.6, 0.06)], [0.24, 0.16, 0.08, 0.03])
        elif body == "dog":
            chain([t0, (0, t0[1] - 0.18, z + 0.10), (0, t0[1] - 0.30, z + 0.02)], [0.04, 0.03, 0.02])
        else:                                   # lion: down and curling up, a tuft
            chain([t0, (0, t0[1] - 0.25, z - 0.25), (0, t0[1] - 0.45, z - 0.45), (0, t0[1] - 0.65, z - 0.30)],
                  [0.05, 0.04, 0.035, 0.03])
            sphere((0, t0[1] - 0.68, z - 0.27), 0.06)
    top = Vector((0, P["cy"] + rx * 0.4, z + rz * 0.6))
    for kind, pts, r0, r1 in necks:
        pp = [top + Vector(q) for q in pts]
        curve = bezier(*pp, n=12)
        chain(curve, [r0 + (r1 - r0) * i / 12 for i in range(13)])
        d = Vector(curve[-1]) - Vector(curve[-3])
        d.z *= 0.25                             # heads look forward, not along a steep neck
        head(kind, curve[-1], d, head_scale)
    if wings:
        for s in (-1, 1):
            root = top + Vector((s * rx * 0.6, wing_y, 0.05))
            if wings == "feathered":            # a broad raised wing, a second fan of primaries behind
                slab(root, root + Vector((s * 0.30, -0.55, 0.85)), 0.55, 0.035, (0, 1, 0))
                slab(root + Vector((s * 0.12, -0.25, 0.25)), root + Vector((s * 0.40, -0.95, 0.80)), 0.45, 0.03, (0, 0, 1))
            else:                               # membrane: an arm bone and a thin sail behind it
                for j, (dy, dz) in enumerate(((0.05, 1.05), (-0.35, 0.95), (-0.70, 0.65))):  # three lobes, fanned
                    slab(root + Vector((0, -0.08 * j, 0.0)), root + Vector((s * 0.35, dy, dz)), 0.30, 0.025, (0, 1, 0))
    return top


def hydra():
    """Hydra: ONE low reptile body, four splayed legs, a tail on the ground, and FIVE long necks
    rising from the same shoulders in a fan (from forward-low to up-and-back, a little apart left
    and right), each with its own dragon-like head looking forward; seen from the side, a little
    from the front, so the five heads stand apart."""
    necks = []
    for i, ang in enumerate((15, 42, 68, 94, 120)):        # elevation of each neck in the side plane
        e = math.radians(ang)
        x = (i - 2) * 0.10
        L = 1.45 - 0.10 * abs(i - 2)
        tip = (x * 2.2, L * math.cos(e) + 0.15, L * math.sin(e) * 0.95 + 0.15)
        mid1 = (x * 0.8, 0.25 * math.cos(e) + 0.15, 0.45 * math.sin(e) + 0.05)
        mid2 = (x * 1.8, tip[1] - 0.35 * math.cos(e) - 0.15, tip[2] - 0.35)
        necks.append(("reptile", [(x * 0.5, 0.0, 0.0), mid1, mid2, tip], 0.17, 0.11))
    quadruped("reptile", necks, tail="short", head_scale=1.35)
    a = math.radians(70)
    return {"view": (math.sin(a), math.cos(a), math.tan(math.radians(14))), "margin": 1.08}


def twohead_dog():
    """Death dog: ONE lean dog body (four legs, one tail) with TWO necks from the same chest,
    splayed left and right, the far head a little higher so both read side by side;
    three-quarter front view."""
    necks = [("dog", [(s * 0.06, 0.0, 0.0), (s * 0.14, 0.08, 0.10 + 0.03 * (s < 0)),
                      (s * 0.24, 0.14, 0.22 + 0.06 * (s < 0)), (s * 0.28, 0.18, 0.28 + 0.08 * (s < 0))],
              0.09, 0.075) for s in (-1, 1)]
    quadruped("dog", necks, head_scale=1.25)
    a = math.radians(50)
    return {"view": (math.sin(a), math.cos(a), math.tan(math.radians(12))), "margin": 1.1}


def sphinx():
    """Sphinx: ONE lion body standing on all four legs, raised feathered wings, a short lion neck
    and a human head (face, headdress cap and two flaps falling onto the chest); seen from the
    side, a little from the front."""
    quadruped("lion", [("woman", [(0, -0.05, 0.0), (0, 0.0, 0.14), (0, 0.04, 0.30), (0, 0.06, 0.42)], 0.13, 0.10)],
              wings="feathered", head_scale=1.35, wing_y=-0.35)
    a = math.radians(70)
    return {"view": (math.sin(a), math.cos(a), math.tan(math.radians(10))), "margin": 1.08}


def chimera():
    """Chimera: ONE lion body, membrane wings raised from the middle of the back, and THREE necks
    from the same shoulders, laid out so a side view keeps them apart: a goat head rising up on
    the far side, the maned lion head in front (lowest), a dragon head on a long neck reaching up
    and forward on the near side; seen from the side, a little from the front."""
    quadruped("lion", [
        ("goat", [(-0.10, -0.06, 0.0), (-0.16, -0.10, 0.25), (-0.20, -0.06, 0.52), (-0.22, 0.02, 0.66)], 0.11, 0.08),
        ("lion", [(0.0, 0.0, 0.0), (0.0, 0.12, 0.06), (0.0, 0.22, 0.10), (0.0, 0.30, 0.12)], 0.16, 0.14),
        ("reptile", [(0.10, 0.02, 0.0), (0.18, 0.25, 0.25), (0.22, 0.48, 0.50), (0.24, 0.68, 0.62)], 0.11, 0.07),
    ], wings="membrane", wing_y=-0.55)
    a = math.radians(72)
    return {"view": (math.sin(a), math.cos(a), math.tan(math.radians(10))), "margin": 1.08}


def biped(wings=False, tail=False, small=False):
    """A front-view humanoid (faces -Y, toward the camera) 1.8 m tall in the wide A-pose: arms 45
    degrees from the body, legs splayed with the feet apart. wings: two membrane wings from the upper
    back raised up and out; tail: held out from behind the hips to the figure's left (image right)
    under the hand, the end curving down. small: stocky tiny/small proportions (a bigger head, a
    thicker trunk, shorter legs, as apose_openpose_small). Returns the spec with COCO-18 keypoints
    (3D) for the matching OpenPose image."""
    H = 1.8
    if small:                                   # height fractions of the joints and trunk widths
        P = dict(hip=0.40, knee=0.21, sh=0.72, neck=0.74, head=0.86, hr=0.10, trunk=1.25, sw=0.12, L=0.15)
    else:
        P = dict(hip=0.48, knee=0.25, sh=0.795, neck=0.81, head=0.915, hr=0.065, trunk=1.0, sw=0.10, L=0.17)
    z = lambda f: f * H                        # height fraction -> z
    sh, hip, tk = P["sw"] * H, 0.06 * H * P["trunk"], P["trunk"]
    ang, L = math.radians(45), P["L"] * H
    # trunk: pelvis, belly, ribcage, neck, head
    ellipsoid((0, 0, z(P["hip"] + 0.02)), 0.09 * H * tk, 0.06 * H * tk, 0.055 * H)
    capsule((0, 0, z(P["hip"] + 0.04)), (0, 0, z(P["sh"] - 0.095)), 0.08 * H * tk, 0.10 * H * tk)
    ellipsoid((0, 0, z(P["sh"] - 0.065)), 0.12 * H * tk, 0.07 * H * tk, 0.08 * H)
    capsule((0, 0, z(P["sh"] - 0.015)), (0, 0, z(P["head"] - 0.065)), 0.03 * H * tk, 0.03 * H * tk)
    hr = P["hr"] * H
    ellipsoid((0, -0.005 * H, z(P["head"])), hr * 0.85, hr * 0.92, hr * 1.05)
    hz = z(P["head"])
    kp = {0: (0, -hr, hz - 0.1 * hr), 1: (0, 0, z(P["neck"])), 14: (-0.3 * hr, -0.8 * hr, hz + 0.25 * hr),
          15: (0.3 * hr, -0.8 * hr, hz + 0.25 * hr), 16: (-0.7 * hr, -0.3 * hr, hz + 0.15 * hr),
          17: (0.7 * hr, -0.3 * hr, hz + 0.15 * hr)}
    for s, (i_sh, i_el, i_wr, i_hp, i_kn, i_an) in ((-1, (2, 3, 4, 8, 9, 10)), (1, (5, 6, 7, 11, 12, 13))):
        # image left = the figure's right (-X seen from -Y is image left): COCO 2-4 / 8-10 are "right"
        sz = z(P["sh"])
        shoulder = (s * sh, 0, sz)
        elbow = (s * (sh + L * math.sin(ang)), 0, sz - L * math.cos(ang))
        wrist = (s * (sh + 2 * L * math.sin(ang)), 0, sz - 2 * L * math.cos(ang))
        chain([shoulder, elbow, wrist], [0.035 * H * tk, 0.028 * H * tk, 0.022 * H * tk])
        hand = (s * (sh + 2.25 * L * math.sin(ang)), 0, sz - 2.25 * L * math.cos(ang))
        ellipsoid(hand, 0.025 * H * tk, 0.015 * H * tk, 0.04 * H * tk)
        hp = (s * hip, 0, z(P["hip"]))
        kn = (s * (hip + 0.04 * H), 0, z(P["knee"]))
        an = (s * (hip + 0.08 * H), 0, z(0.04))
        chain([hp, kn, an], [0.042 * H * tk, 0.033 * H * tk, 0.025 * H * tk])
        ellipsoid((an[0], -0.04 * H, 0.015 * H), 0.03 * H, 0.06 * H, 0.018 * H)   # foot, toes forward
        kp.update({i_sh: shoulder, i_el: elbow, i_wr: wrist, i_hp: hp, i_kn: kn, i_an: an})
    if wings:
        for s in (-1, 1):
            root = Vector((s * 0.05 * H, 0.06 * H, z(P["sh"] + 0.005)))
            elbow = root + Vector((s * 0.20 * H, 0.04 * H, 0.22 * H))
            tip = root + Vector((s * 0.40 * H, 0.06 * H, 0.42 * H))
            chain([root, elbow, tip], [0.022 * H, 0.016 * H, 0.008 * H])      # the wing arm, up and out
            for end in ((0.47, 0.34), (0.47, 0.22), (0.38, 0.11)):            # membrane lobes, above the shoulders
                slab(root, root + Vector((s * end[0] * H, 0.07 * H, end[1] * H)), 0.12 * H, 0.008 * H, (0, 0, 1))
    if tail:
        s = 1                                   # the figure's left, image right
        t0 = P["hip"] - 0.03                    # out under the hand, the end curving down
        pts = bezier((0, 0.05 * H, z(t0)), (s * 0.16 * H, 0.15 * H, z(t0 - 0.04)),
                     (s * 0.34 * H, 0.12 * H, z(t0 - 0.05)), (s * 0.44 * H, 0.04 * H, z(t0 - 0.21)), n=14)
        chain(pts, [(0.04 * H - 0.03 * H * i / 14) * tk for i in range(15)])
    return {"view": (0.0, -1.0, 0.0), "margin": 1.10, "keypoints": kp}


def membrane(outline, thick):
    """A flat sheet (a wing membrane) through the polygon `outline` (3D points, fan-triangulated from
    their centroid), `thick` thick."""
    import bmesh
    bm = bmesh.new()
    c = sum((Vector(p) for p in outline), Vector()) / len(outline)
    vc = bm.verts.new(c)
    vs = [bm.verts.new(Vector(p)) for p in outline]
    for i in range(len(vs)):
        bm.faces.new((vc, vs[i], vs[(i + 1) % len(vs)]))
    me = bpy.data.meshes.new("membrane")
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("membrane", me)
    bpy.context.scene.collection.objects.link(ob)
    mod = ob.modifiers.new("solid", "SOLIDIFY")
    mod.thickness, mod.offset = thick, 0.0
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.modifier_apply(modifier="solid")


def quad_pose(kind="mammal", wings=False, small=False):
    """A rig-friendly quadruped facing +Y (2026-11-08, docs/references.md "Rig-friendly quadrupeds"): all
    four legs straight and apart with every foot planted (no raised paw, no sitting, no rearing), the
    tail held out behind along the body line and clear of the floor and the hind legs, the head forward
    on the neck, wings (membrane) raised up and spread clear of the back and legs. Seen from the side and
    a little from the front (60 degrees off the front, camera 12 degrees up), so the far legs show beside
    the near ones instead of behind them. kind: mammal (a dog-like barrel on straight legs), reptile (a
    long low body, the elbows and knees bent out, feet planted outside the body, a long tail), dragon (a
    deep chest on straight legs, a long neck rising forward, a long tail). small: tiny/small proportions
    (a longer, lower body on shorter legs and a bigger head)."""
    k = 1.0
    if kind == "reptile":
        z, rx, ry_, rz = 0.48, 0.30, 0.95, 0.22
        cy, ry, lx, lr = 0.60, -0.62, 0.32, 0.11
    elif kind == "dragon":
        z, rx, ry_, rz = 1.00, 0.30, 0.72, 0.32
        cy, ry, lx, lr = 0.52, -0.55, 0.25, 0.11
    else:
        z, rx, ry_, rz = 0.72, 0.20, 0.55, 0.22
        cy, ry, lx, lr = 0.42, -0.45, 0.15, 0.065
    if small:                                   # longer, lower body, shorter legs, bigger head
        z *= 0.72
        ry_ *= 1.1
        k = 1.35
        if kind == "mammal":                    # a weasel / rat build: long and low on short legs
            z, ry_, rz, cy, ry = 0.36, 0.62, 0.17, 0.48, -0.50
            rx, lr, k = 0.16, 0.05, 1.0
    ellipsoid((0, 0, z), rx, ry_, rz)
    ellipsoid((0, cy, z + 0.04), rx * 0.95, rx * 1.05, rz * 1.08)              # chest
    ellipsoid((0, ry, z + 0.02), rx * 0.95, rx * 1.0, rz * 1.02)               # rump
    for s in (-1, 1):
        if kind == "reptile":                   # splayed: elbow and knee out, the foot planted outside the body
            for y0, r0 in ((cy + 0.02, lr), (ry - 0.02, lr * 1.25)):
                chain([(s * rx * 0.75, y0, z), (s * (lx + 0.10), y0 + 0.03, z * 0.70), (s * (lx + 0.14), y0 + 0.06, 0.06)],
                      [r0, r0 * 0.8, r0 * 0.6])
                ellipsoid((s * (lx + 0.15), y0 + 0.13, 0.03), r0 * 0.9, r0 * 1.4, 0.03)
        else:                                   # straight legs, a little wider than the body, toes forward
            foot = (lr * 1.05, lr * 1.6, lr * 0.6)
            chain([(s * lx * 0.85, cy, z), (s * lx, cy + 0.02, z * 0.48), (s * lx * 1.05, cy + 0.02, foot[2])],
                  [lr * 1.2, lr * 0.8, lr * 0.65])
            chain([(s * lx * 0.85, ry, z + 0.02), (s * lx, ry + 0.10 * z, z * 0.58), (s * lx * 1.05, ry - 0.06 * z, z * 0.25),
                   (s * lx * 1.05, ry - 0.03 * z, foot[2])], [lr * 1.45, lr * 0.9, lr * 0.65, lr * 0.65])
            for y0 in (cy + 0.02, ry - 0.03 * z):
                ellipsoid((s * lx * 1.05, y0 + foot[1] * 0.6, foot[2]), *foot)
    # tail: out behind along the body line, sloping a little down, clear of the floor and the hind legs
    t0 = (0, ry - rx * 0.8, z + 0.02)
    if kind == "reptile":
        pts = bezier(t0, (0, t0[1] - 0.55, z * 0.85), (0, t0[1] - 1.15, z * 0.55), (0, t0[1] - 1.65, z * 0.40), n=12)
        chain(pts, [rz * 0.95 * (1 - i / 13) + 0.015 for i in range(13)])
    elif kind == "dragon":
        pts = bezier(t0, (0, t0[1] - 0.70, z * 0.95), (0, t0[1] - 1.40, z * 0.75), (0, t0[1] - 2.00, z * 0.62), n=14)
        chain(pts, [0.20 * (1 - i / 15) + 0.015 for i in range(15)])
    else:
        L = 0.75 if small else 0.55
        pts = bezier(t0, (0, t0[1] - 0.35 * L, z * 0.98), (0, t0[1] - 0.75 * L, z * 0.85), (0, t0[1] - L, z * 0.72), n=10)
        chain(pts, [0.05 * (1 - i / 11) + 0.012 for i in range(11)])
    # neck and head, the head looking forward
    top = Vector((0, cy + rx * 0.5, z + rz * 0.55))
    if kind == "dragon":
        pp = [top, top + Vector((0, 0.25, 0.30)), top + Vector((0, 0.45, 0.65)), top + Vector((0, 0.75, 0.80))]
        curve = bezier(*pp, n=12)
        chain(curve, [0.22 - 0.09 * i / 12 for i in range(13)])
        d = Vector((0, 1, -0.15))
        head("reptile", curve[-1], d, 1.35 * k)
    elif kind == "reptile":
        p = Vector((0, cy + rx * 1.3, z + 0.02))
        capsule((0, cy + 0.1, z), p, 0.18, 0.15)
        head("reptile", p, Vector((0, 1, -0.05)), 1.7 * k)
    else:
        nk = [(0.08, 0.10), (0.16, 0.18), (0.24, 0.22)] if not small else [(0.07, 0.04), (0.14, 0.07), (0.20, 0.08)]
        pp = [top] + [top + Vector((0, a, b)) for a, b in nk]
        curve = bezier(*pp, n=8)
        chain(curve, [0.09 - 0.02 * i / 8 for i in range(9)])
        head("small" if small else "dog", curve[-1], Vector((0, 1, -0.1)), 1.15 * k)
    if wings:                                   # raised up and spread in a V above the back, clear of the legs and neck
        W = 1.35 if kind == "dragon" else 0.85
        tilt = math.radians(30)                 # the wing plane leans out from the vertical
        for s in (-1, 1):
            root = Vector((s * rx * 0.55, cy - 0.30, z + rz * 0.75))
            up = Vector((s * math.sin(tilt), 0, math.cos(tilt)))
            at = lambda u, v: root + Vector((0, u * W, 0)) + up * (v * W)
            # a bat wing: the arm up to the wrist, three fingers fanned back, a scalloped membrane between
            # them whose trailing edge stays above the back
            wrist, tips = at(0.05, 1.0), [at(-0.30, 1.18), at(-0.72, 0.98), at(-1.00, 0.58)]
            chain([root, at(0.10, 0.5), wrist], [0.06, 0.045, 0.025])
            for t in tips:
                capsule(wrist, t, 0.02, 0.008)
            outline = [root, at(0.08, 0.5), wrist, tips[0], at(-0.42, 0.80), tips[1], at(-0.68, 0.52), tips[2],
                       at(-0.70, 0.22), at(-0.50, 0.05)]
            membrane(outline, 0.012)
    a = math.radians(60)
    return {"view": (math.sin(a), math.cos(a), math.tan(math.radians(15 if kind == "reptile" else 12))), "margin": 1.08}


QUAD_POSES = {"quad_side": ("mammal", False), "quad_low_side": ("reptile", False), "quad_wings_side": ("dragon", True)}


TEMPLATES = {"serpentine_side_depth": snake, "centaur_34_depth": centaur, "hydra_34_depth": hydra,
             "twohead_dog_34_depth": twohead_dog, "sphinx_side_depth": sphinx, "chimera_34_depth": chimera,
             "biped_wings_front_depth": lambda: biped(wings=True), "biped_tail_front_depth": lambda: biped(tail=True),
             "biped_wings_tail_front_depth": lambda: biped(wings=True, tail=True),
             "biped_wings_front_small_depth": lambda: biped(wings=True, small=True),
             "biped_tail_front_small_depth": lambda: biped(tail=True, small=True),
             "biped_wings_tail_front_small_depth": lambda: biped(wings=True, tail=True, small=True),
             **{f"{n}{'_small' if sm else ''}_depth": (lambda kd=kd, w=w, sm=sm: quad_pose(kd, w, sm))
                for n, (kd, w) in QUAD_POSES.items() for sm in (False, True)}}


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
    if spec.get("keypoints"):                   # pixel positions for the matching OpenPose image
        from bpy_extras.object_utils import world_to_camera_view
        import json
        kp = {}
        for i, p in spec["keypoints"].items():
            c = world_to_camera_view(sc, cam, Vector(p))
            kp[str(i)] = [round(c.x * RES, 1), round((1 - c.y) * RES, 1)]
        kpath = path.replace("_depth.png", "_keypoints.json")
        with open(kpath, "w") as f:
            json.dump(kp, f, indent=1)
        print(f"wrote {kpath}")


os.makedirs(OUT, exist_ok=True)
for name, fn in TEMPLATES.items():
    if not ONLY or name in ONLY:
        render(name, fn)

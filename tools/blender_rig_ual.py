"""Rig a biped mesh directly to the Quaternius UAL humanoid skeleton (docs/anim-reuse.md, "Direct UAL
rig spike"). Run by tools/rig_ual.py:

    blender -b --factory-startup --python-exit-code 1 -P tools/blender_rig_ual.py -- '<job json>'

job: src (model_rigged.glb: its mesh is the game mesh; its SkinTokens skeleton + weights are the
reference and weight source "skintokens"; or a bare model.glb for fit "auto" + weights heat/transfer),
bonemap (bonemap.json; optional without a skeleton), ual (UAL1_Standard.glb), ual_extra (more packs on
the same rig whose actions are added, UAL2 for Zombie_*), fit ("auto" | "skintokens"), weights
("skintokens" | "heat" | "transfer"; heat_voxel, heat_direct), fingers ("hand": finger weights folded
into the hand | "keep"), sockets (registry list), lods {k: path}, base_color (albedo to set, optional),
clips {ours: UAL clip, "A+B" chained}, out_dir, tiles (dir or null), tile_px, export_anims.

1. Fit. Our armature is a copy of UAL's (same names, hierarchy and bone frames up to a rotation),
   with the joints moved onto the mesh. UAL faces -Y (Blender) with its left on +X; our meshes face
   +Y with the left on -X, so the armature object is turned 180 deg about Z (the bones keep UAL's
   armature-space frames; the turn lives on the Armature node, which no clip keys).
   "auto": landmarks from the mesh alone. The mesh is voxelised (solid), a distance transform gives
   each voxel's depth, and Dijkstra paths that hug the medial axis run hand tip -> hand tip (arms and
   shoulder line), sole -> sole (legs; its apex is the crotch, none = robe) and pelvis -> head top
   (spine; its narrowest point is the neck). The same detector runs on UAL's own mannequin; each of
   our joints is placed relative to our landmark the way UAL's joint sits relative to UAL's landmark
   (offsets scaled per part), and limb joints by UAL's length fractions along our path.
   "skintokens": the mapped SkinTokens joints (bonemap.json); unmapped UAL joints interpolated.
2. Each bone's rest frame is UAL's rest frame turned by R (world), the shortest arc from UAL's bone
   direction to ours (bone -> its main child), so every child offset lies along UAL's local direction
   scaled to our length. A UAL clip's local rotations then put each bone where UAL's goes; the
   mesh stays bound in its own (A-)pose. root and pelvis keep R = identity.
3. Clips: per frame, our pose basis = C_b @ UAL basis, C_b = (our rest local)^-1 @ (UAL rest local),
   a constant per bone, so the exported local rotations equal UAL's; the pelvis translation is UAL's
   scaled by our leg length / UAL's. No per-frame solve, no IK, no ground fix.
4. Sockets: hand_l/hand_r are UAL's own hand bones; grip_l/r (prop frame, +Y along UAL's knuckle line
   pinky_01 -> index_01, -Z along the fingers), head (on Head), back (spine_03), hip_l/r (pelvis) are
   added as non-deforming bones.
"""
import heapq
import json
import math
import sys
import time
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector
from mathutils.kdtree import KDTree

job = json.loads(sys.argv[sys.argv.index("--") + 1])
FPS = 30
UAL_FPS = 24
SIDES = "lr"
R180 = Matrix.Rotation(math.pi, 4, "Z")
T0 = time.time()
REPORT = {"id": job.get("id"), "fit": job.get("fit", "auto"), "weights": job.get("weights", "skintokens")}

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.fps = FPS


def select(*objs):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]


# --- inputs -----------------------------------------------------------------------------
bpy.ops.import_scene.gltf(filepath=job["src"])
# src may be a bare mesh (model.glb, forge.py rig with rig_source "ual"): no SkinTokens skeleton,
# no bonemap; then only fit "auto" with weights "heat"/"transfer" work and there is no sanity reference
SK = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
ME = max((o for o in bpy.data.objects if o.type == "MESH"), key=lambda o: len(o.data.vertices))
for o in list(bpy.data.objects):
    if o.type == "MESH" and o is not ME:
        bpy.data.objects.remove(o)
BM = json.loads(Path(job["bonemap"]).read_text()) if job.get("bonemap") and Path(job["bonemap"]).exists() else {"map": {}, "chains": {}}
if SK is None and (job.get("fit", "auto") != "auto" or job.get("weights", "skintokens") == "skintokens"):
    raise SystemExit("src has no skeleton: use fit auto and weights heat or transfer")
if SK is None and job.get("extras"):
    raise SystemExit("extras need the SkinTokens rig as src (its chains are adopted)")
# extras (docs/anim-reuse.md "Extra chains"): a tail the SkinTokens rig has no chain for is found
# on the mesh and inserted on the SkinTokens skeleton first (blender_tail_chain, the rig stage's
# tail insertion), then adopted like any other chain
if SK and job.get("add_tail"):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import blender_tail_chain  # noqa: E402
    import rig_topology  # noqa: E402
    REPORT["tail_added"] = blender_tail_chain.add_missing_tail(SK, ME, rig_topology, 2, isolated=False)
    print("tail_added", json.dumps(REPORT["tail_added"], default=str)[:400], flush=True)
    if (REPORT["tail_added"] or {}).get("found") and not any(e["root"] == "tail_00" for e in job.get("extras") or []):
        job["extras"] = list(job.get("extras") or []) + [{"root": "tail_00", "kind": "tail"}]
SK_POS = {b.name: (SK.matrix_world @ b.head_local).copy() for b in SK.data.bones} if SK else {}
SK_TAIL = {b.name: (SK.matrix_world @ b.tail_local).copy() for b in SK.data.bones} if SK else {}
SK_PARENT = {b.name: b.parent.name if b.parent else None for b in SK.data.bones} if SK else {}
SK_KIDS = {b.name: [c.name for c in b.children] for b in SK.data.bones} if SK else {}
SK_Z = {b.name: (SK.matrix_world.to_3x3() @ b.matrix_local.to_3x3() @ Vector((0, 0, 1))).normalized()
        for b in SK.data.bones} if SK else {}
SKW = {}  # the SkinTokens weights per bone (extras keep theirs)
if SK and job.get("extras"):
    _gn = {vg.index: vg.name for vg in ME.vertex_groups}
    for v in ME.data.vertices:
        for g in v.groups:
            SKW.setdefault(_gn[g.group], np.zeros(len(ME.data.vertices)))[v.index] += g.weight
for m in list(ME.modifiers):  # the SkinTokens armature goes; its weights stay as vertex groups
    ME.modifiers.remove(m)
_mw = ME.matrix_world.copy()
ME.parent = None
ME.matrix_world = _mw
if SK:
    bpy.data.objects.remove(SK)
if job.get("base_color"):  # the painted albedo (rig.py finalize does the same on the SkinTokens route)
    _img = bpy.data.images.load(job["base_color"])
    _img.colorspace_settings.name = "sRGB"
    for _mat in ME.data.materials:
        _bsdf = next((n for n in _mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None) if _mat and _mat.use_nodes else None
        for _l in (_bsdf.inputs["Base Color"].links if _bsdf else []):
            _n = _l.from_node
            while _n.type != "TEX_IMAGE":
                _n = next(i.links[0].from_node for i in _n.inputs if i.is_linked)
            _n.image = _img
        for _l in (list(_bsdf.inputs["Metallic"].links) if _bsdf else []):  # blender_rig.clamp_metallic's rule
            _mat.node_tree.links.remove(_l)
        if _bsdf:
            _bsdf.inputs["Metallic"].default_value = 0.0
# world-space mesh, triangles
ME.data.calc_loop_triangles()
MW = np.array(ME.matrix_world)
_co = np.empty(len(ME.data.vertices) * 3)
ME.data.vertices.foreach_get("co", _co)
VERTS = _co.reshape(-1, 3) @ MW[:3, :3].T + MW[:3, 3]
_tri = np.empty(len(ME.data.loop_triangles) * 3, dtype=np.int64)
ME.data.loop_triangles.foreach_get("vertices", _tri)
TRIS = _tri.reshape(-1, 3)
GROUND = float(VERTS[:, 2].min())
HEIGHT = float(VERTS[:, 2].max() - GROUND)

before = set(bpy.data.objects)
bpy.ops.import_scene.gltf(filepath=job["ual"])
_new = [o for o in bpy.data.objects if o not in before]
UA = next(o for o in _new if o.type == "ARMATURE")
UM = next(o for o in _new if o.type == "MESH" and o.vertex_groups)
for o in _new:
    if o.type == "MESH" and o is not UM:
        bpy.data.objects.remove(o)
ACTS = {}
if UA.animation_data:
    for t in UA.animation_data.nla_tracks:
        for s in t.strips:
            if s.action:
                ACTS.setdefault(t.name, s.action)
    for t in list(UA.animation_data.nla_tracks):
        UA.animation_data.nla_tracks.remove(t)
for extra in job.get("ual_extra") or []:  # more packs on the same rig (UAL2: Zombie_*): actions only
    _b = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=extra)
    for o in [o for o in bpy.data.objects if o not in _b]:
        if o.type == "ARMATURE" and o.animation_data:
            for t in o.animation_data.nla_tracks:
                for s in t.strips:
                    if s.action and t.name not in ACTS:
                        s.action.use_fake_user = True
                        ACTS[t.name] = s.action
            for t in list(o.animation_data.nla_tracks):
                o.animation_data.nla_tracks.remove(t)
        bpy.data.objects.remove(o)
UA.matrix_world = R180 @ UA.matrix_world  # face +Y like ours, left on -X
UA.name, UA.data.name = "UAL_Armature", "UAL_Armature"  # our copy takes "Armature"
bpy.context.view_layer.update()
UREST = {b.name: b.matrix_local.copy() for b in UA.data.bones}      # armature space
UPAR = {b.name: b.parent.name if b.parent else None for b in UA.data.bones}
ORDER = []
for b in UA.data.bones:  # parents first
    stack, x = [], b
    while x and x.name not in ORDER:
        stack.append(x.name)
        x = x.parent
    ORDER.extend(reversed(stack))
UW = UA.matrix_world.copy()
UPOS = {n: (UW @ m).translation.copy() for n, m in UREST.items()}    # world, facing +Y
UTAIL = {b.name: (UW @ b.tail_local).copy() for b in UA.data.bones}
_uco = np.array([(UM.matrix_world @ v.co)[:] for v in UM.data.vertices])
UAL_H = float(_uco[:, 2].max() - _uco[:, 2].min())
_top = _uco[_uco[:, 2] > _uco[:, 2].max() - 0.01]
UAL_TOP = Vector(_top[np.argmin(np.abs(_top[:, 0]))])


def mesh_world(o, evaluated=False, tris=True):
    if evaluated:
        dg = bpy.context.evaluated_depsgraph_get()
        ev = o.evaluated_get(dg)
        m = ev.to_mesh()
    else:
        ev, m = o, o.data
    co = np.empty(len(m.vertices) * 3)
    m.vertices.foreach_get("co", co)
    mw = np.array(ev.matrix_world)
    co = co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]
    t = None
    if tris:
        m.calc_loop_triangles()
        t = np.empty(len(m.loop_triangles) * 3, dtype=np.int64)
        m.loop_triangles.foreach_get("vertices", t)
        t = t.reshape(-1, 3)
    if evaluated:
        ev.to_mesh_clear()
    return co, t


# --- landmarks (voxels + medial Dijkstra) -----------------------------------------------
N6 = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]


def shift(a, d):
    out = np.zeros_like(a)
    src = tuple(slice(max(0, -k), a.shape[i] - max(0, k)) for i, k in enumerate(d))
    dst = tuple(slice(max(0, k), a.shape[i] - max(0, -k)) for i, k in enumerate(d))
    out[dst] = a[src]
    return out


def dilate(a):
    o = a.copy()
    for d in N6:
        o |= shift(a, d)
    return o


def erode(a):
    o = a.copy()
    for d in N6:
        o &= shift(a, d)
    return o


class Vox:
    """Solid voxel model of a triangle mesh; cell = height / res."""

    def __init__(self, co, tris, res=96):
        self.h = float(co[:, 2].max() - co[:, 2].min())
        self.cell = c = self.h / res
        pad = 3
        self.lo = co.min(0) - pad * c
        self.shape = tuple(int(x) for x in np.ceil((co.max(0) + pad * c - self.lo) / c) + 1)
        occ = np.zeros(self.shape, bool)
        a, b, cc = co[tris[:, 0]], co[tris[:, 1]], co[tris[:, 2]]
        area = 0.5 * np.linalg.norm(np.cross(b - a, cc - a), axis=1)
        n = int(min(3e6, max(2e5, area.sum() / (c * c) * 12)))
        rng = np.random.default_rng(0)
        k = rng.choice(len(tris), n, p=area / area.sum())
        u, v = rng.random(n), rng.random(n)
        flip = u + v > 1
        u[flip], v[flip] = 1 - u[flip], 1 - v[flip]
        p = a[k] + (b[k] - a[k]) * u[:, None] + (cc[k] - a[k]) * v[:, None]
        p = np.concatenate([p, co])
        ij = np.floor((p - self.lo) / c).astype(int)
        occ[ij[:, 0], ij[:, 1], ij[:, 2]] = True
        shell = dilate(occ)  # seal pinholes
        out = np.zeros(self.shape, bool)
        out[0], out[-1], out[:, 0], out[:, -1], out[:, :, 0], out[:, :, -1] = True, True, True, True, True, True
        out &= ~shell
        while True:
            nxt = dilate(out) & ~shell
            if nxt.sum() == out.sum():
                break
            out = nxt
        self.solid = erode(~out) | occ
        dt = np.zeros(self.shape, np.float32)
        cur = self.solid.copy()
        k = 0
        while cur.any():
            k += 1
            dt[cur] = k
            cur = erode(cur)
        self.dt = dt

    def idx(self, p):
        return tuple(int(x) for x in np.clip(np.floor((np.asarray(p) - self.lo) / self.cell), 0, np.array(self.shape) - 1))

    def pos(self, ijk):
        return self.lo + (np.asarray(ijk, float) + 0.5) * self.cell

    def snap(self, p, deep=False):
        """Nearest solid voxel to p (deep: the deepest within 3 cells of that one)."""
        s = np.argwhere(self.solid)
        q = (np.asarray(p) - self.lo) / self.cell - 0.5
        i = s[np.argmin(((s - q) ** 2).sum(1))]
        if deep:
            win = s[np.abs(s - i).max(1) <= 3]
            i = win[np.argmax(self.dt[tuple(win.T)])]
        return tuple(int(x) for x in i)

    def path(self, a, b, power=2.0):
        """Medial Dijkstra path between voxel indices a and b (list of ijk); b None: the distance
        field from a (power 0 = plain geodesic) as {flat index: distance}."""
        sh = self.shape
        st = (sh[1] * sh[2], sh[2], 1)
        flat_solid = self.solid.ravel()
        w = 1.0 / np.maximum(self.dt.ravel(), 1.0) ** power
        offs = [(dx, dy, dz) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1) if (dx, dy, dz) != (0, 0, 0)]
        nb = [(dx * st[0] + dy * st[1] + dz * st[2], math.sqrt(dx * dx + dy * dy + dz * dz)) for dx, dy, dz in offs]
        ia = a[0] * st[0] + a[1] * st[1] + a[2] * st[2]
        ib = b[0] * st[0] + b[1] * st[1] + b[2] * st[2] if b is not None else -1
        dist = {ia: 0.0}
        prev = {}
        pq = [(0.0, ia)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == ib:
                break
            if d > dist.get(u, 1e30):
                continue
            wu = w[u]
            for o, ln in nb:
                v = u + o
                if not flat_solid[v]:
                    continue
                nd = d + ln * 0.5 * (wu + w[v])
                if nd < dist.get(v, 1e30):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        if b is None:
            return dist
        out, u = [ib], ib
        while u != ia:
            u = prev[u]
            out.append(u)
        out.reverse()
        return [(u // st[0], (u % st[0]) // st[1], u % st[1]) for u in out]


def smooth(pts, k=2):
    pts = np.asarray(pts, float)
    out = pts.copy()
    for i in range(len(pts)):
        out[i] = pts[max(0, i - k):i + k + 1].mean(0)
    out[0], out[-1] = pts[0], pts[-1]
    return out


def arclen(pts):
    return np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(pts, axis=0), axis=1))])


def at_len(pts, s):
    L = arclen(pts)
    s = min(max(s, 0.0), L[-1])
    i = min(int(np.searchsorted(L, s, side="right")) - 1, len(pts) - 2)
    t = (s - L[i]) / max(L[i + 1] - L[i], 1e-9)
    return pts[i] + (pts[i + 1] - pts[i]) * t


def at_z(pts, z):
    """First point (from pts[0]) where the polyline crosses height z (interpolated)."""
    for i in range(len(pts) - 1):
        a, b = pts[i][2], pts[i + 1][2]
        if (a - z) * (b - z) <= 0 and a != b:
            t = (z - a) / (b - a)
            return pts[i] + (pts[i + 1] - pts[i]) * t
    return pts[int(np.argmin(np.abs(pts[:, 2] - z)))]


def landmarks(co, tris, label):
    """Mesh facing +Y, left on -X, feet at the bottom: medial paths and the landmarks on them."""
    t = time.time()
    vx = Vox(co, tris)
    z0, h = float(co[:, 2].min()), vx.h
    s = np.argwhere(vx.solid)
    P = vx.pos(s)
    mid = (P[:, 2] > z0 + 0.3 * h) & (P[:, 2] < z0 + 0.85 * h)
    cx = float(np.median(P[mid, 0]))
    L = {"cx": cx, "z0": z0, "h": h, "cell": vx.cell}
    # soles: centroid of the bottom 3 layers per side, toe tips the forward-most there
    bot = P[:, 2] < z0 + 3.5 * vx.cell
    soles = {}
    for sd, sgn in (("l", -1), ("r", 1)):
        q = P[bot & (sgn * (P[:, 0] - cx) > 0)]
        soles[sd] = vx.snap(q.mean(0), deep=True)
        foot = P[(P[:, 2] < z0 + 0.06 * h) & (sgn * (P[:, 0] - cx) > 0)]
        L[f"toe_{sd}"] = foot[np.argmax(foot[:, 1])]
        L[f"heel_{sd}"] = foot[np.argmin(foot[:, 1])]
        L[f"sole_{sd}"] = vx.pos(soles[sd])
    legs = smooth(vx.pos(vx.path(soles["l"], soles["r"])), 2)
    k = int(np.argmax(legs[:, 2]))
    L["leg_path"], L["crotch_k"], L["crotch_z"] = legs, k, float(legs[k, 2])
    # hand tips: per side, the solid voxel geodesically farthest from the chest among those above
    # the crotch (robes: 0.3 h) and off the centre line: a long arm beats a shoulder spike, an ear
    # or a sleeve flare (the lateral extreme picked a goblin's pauldron spike)
    chest = vx.snap((cx, float(np.median(P[np.abs(P[:, 2] - (z0 + 0.65 * h)) < vx.cell, 1])), z0 + 0.65 * h), deep=True)
    gd = vx.path(chest, None, power=0.0)
    sh = vx.shape
    flat = s[:, 0] * sh[1] * sh[2] + s[:, 1] * sh[2] + s[:, 2]
    G = np.array([gd.get(int(f), -1.0) for f in flat])
    zc = L["crotch_z"] if L["crotch_z"] > z0 + 0.2 * h else z0 + 0.3 * h
    tips = {}
    for sd, sgn in (("l", -1), ("r", 1)):
        # A-pose: the hands hang below the shoulders (ears and horns above 0.62 h are not hands)
        cand = (P[:, 2] > zc + 0.03 * h) & (sgn * (P[:, 0] - cx) > 0.06 * h) & (P[:, 2] < z0 + 0.62 * h)
        if not cand.any():
            L["tpose_warning"] = True
            cand = (P[:, 2] > zc + 0.03 * h) & (sgn * (P[:, 0] - cx) > 0.06 * h) & (P[:, 2] < z0 + 0.92 * h)
        if not cand.any():
            cand = sgn * (P[:, 0] - cx) > 0
        k = np.nonzero(cand)[0][np.argmax(G[cand])]
        tips[sd] = tuple(int(x) for x in s[k])
        L[f"tip_{sd}"] = vx.pos(tips[sd])
    arm = smooth(vx.pos(vx.path(tips["l"], tips["r"])), 2)
    L["arm_path"] = arm
    # the shoulder line: the arm path's points nearest cx (its middle)
    km = int(np.argmin(np.abs(arm[:, 0] - cx) + 0.0 * arm[:, 2]))
    L["arm_mid_k"] = km
    L["arm_mid"] = arm[km]
    # spine: from the pelvis (above the crotch, or 0.5 h for robes) to the head top
    top = P[P[:, 2] > P[:, 2].max() - 2 * vx.cell]
    head_top = top[np.argmin(np.abs(top[:, 0] - cx))]
    L["head_top"] = head_top
    pz = L["crotch_z"] + 0.04 * h if L["crotch_z"] > z0 + 0.2 * h else z0 + 0.5 * h
    pel = vx.snap((cx, float(np.median(P[np.abs(P[:, 2] - pz) < vx.cell, 1])), pz), deep=True)
    sp = vx.path(pel, vx.snap(head_top))
    spine = smooth(vx.pos(sp), 3)
    L["spine_path"] = spine
    L["spine_dt"] = np.array([vx.dt[i] for i in sp]) * vx.cell
    # neck: a local minimum of the spine path's depth with a wider head above it (between the
    # shoulder line and the head top); none (a head sunk into the shoulders) -> None, placed by
    # proportion in the fit
    zs = L["arm_mid"][2]
    dts = np.convolve(np.pad(L["spine_dt"], 2, mode="edge"), np.ones(5) / 5, mode="valid")
    cand = []
    for i in range(len(spine)):
        if not (zs - 0.03 * h < spine[i, 2] < head_top[2] - 0.06 * h):
            continue
        if dts[i] > dts[max(0, i - 3):i + 4].min() + 1e-6:
            continue
        rise = dts[i:].max() / max(dts[i], 1e-6)
        if rise > 1.15:
            cand.append((rise, i))
    L["neck"] = spine[max(cand)[1]] if cand else None
    L["neck_r"] = float(dts[max(cand)[1]]) if cand else None
    # arm depth along each half (centre -> tip): the shoulder is where the depth has fallen from
    # the trunk's to near the arm's own (calibrated on UAL in the fit)
    for sd in SIDES:
        half = arm[km::-1] if sd == "l" else arm[km:]
        d = np.array([vx.dt[vx.idx(q)] for q in half], float) * vx.cell
        d = np.convolve(np.pad(d, 1, mode="edge"), np.ones(3) / 3, mode="valid")
        al = arclen(half)
        tot = al[-1]
        mask = (al > 0.35 * tot) & (al < 0.8 * tot)
        L[f"arm_r_{sd}"] = float(np.median(d[mask])) if mask.any() else float(np.median(d))
        L[f"arm_dt_{sd}"] = d
    L["seconds"] = round(time.time() - t, 1)
    print(f"landmarks {label}: {L['seconds']}s cell {vx.cell:.3f} crotch {L['crotch_z']:.3f} "
          f"neck {None if L['neck'] is None else round(float(L['neck'][2]), 3)} arm r {L['arm_r_l']:.3f} {L['arm_r_r']:.3f}", flush=True)
    return L


def arm_half(L, sd):
    """Arm path from the shoulder line's middle out to the tip of side sd."""
    arm, km = L["arm_path"], L["arm_mid_k"]
    return arm[km::-1] if sd == "l" else arm[km:]


def leg_half(L, sd):
    """Leg path from the crotch down to the sole of side sd."""
    legs, k = L["leg_path"], L["crotch_k"]
    return legs[k::-1] if sd == "l" else legs[k:]


# --- fit ---------------------------------------------------------------------------------
MAIN_CHILD = {"root": None, "pelvis": None, "spine_01": "spine_02", "spine_02": "spine_03", "spine_03": "neck_01",
              "neck_01": "Head", "Head": "@head_top"}
for s in SIDES:
    MAIN_CHILD.update({f"clavicle_{s}": f"upperarm_{s}", f"upperarm_{s}": f"lowerarm_{s}",
                       f"lowerarm_{s}": f"hand_{s}", f"hand_{s}": f"middle_01_{s}",
                       f"thigh_{s}": f"calf_{s}", f"calf_{s}": f"foot_{s}", f"foot_{s}": f"ball_{s}",
                       f"ball_{s}": f"ball_leaf_{s}"})
CORE = ["root", "pelvis", "spine_01", "spine_02", "spine_03", "neck_01", "Head"] + \
    [f"{k}_{s}" for s in SIDES for k in ("clavicle", "upperarm", "lowerarm", "hand", "thigh", "calf", "foot", "ball",
                                          "ball_leaf", "middle_01")]


def fit_auto():
    """Our joint world positions for CORE (+ '@head_top') from the landmarks, calibrated on UAL's mannequin."""
    LO = landmarks(VERTS, TRIS, "ours")
    # UAL's mannequin is in a T-pose: droop its arms to our arm angle before measuring it
    if UA.animation_data:
        UA.animation_data.action = None
    droop = {}
    for s in SIDES:
        sgn = -1 if s == "l" else 1
        tip, mid = LO[f"tip_{s}"], LO["arm_mid"]
        a = math.atan2(mid[2] - tip[2], abs(tip[0] - mid[0]))
        a = min(max(a, 0.0), math.radians(80))
        droop[s] = round(math.degrees(a), 1)
        q = Vector((sgn, 0, 0)).rotation_difference(Vector((sgn * math.cos(a), 0, -math.sin(a))))
        qa = (UW.inverted().to_3x3() @ q.to_matrix() @ UW.to_3x3()).to_4x4()
        pb = UA.pose.bones[f"upperarm_{s}"]
        hd = UREST[f"upperarm_{s}"].translation
        pb.matrix = Matrix.Translation(hd) @ qa @ Matrix.Translation(-hd) @ UREST[f"upperarm_{s}"]
    bpy.context.view_layer.update()
    REPORT["arm_droop_deg"] = droop
    uco, utr = mesh_world(UM, evaluated=True)
    LU = landmarks(uco, utr, "ual")
    for pb in UA.pose.bones:
        pb.matrix_basis = Matrix()
    bpy.context.view_layer.update()
    REPORT["landmarks"] = {k: (np.round(v, 4).tolist() if isinstance(v, np.ndarray) and v.ndim == 1 else v)
                           for k, v in LO.items() if not (isinstance(v, np.ndarray) and v.ndim > 1)}
    REPORT["landmarks_ual"] = {k: (np.round(v, 4).tolist() if isinstance(v, np.ndarray) and v.ndim == 1 else v)
                               for k, v in LU.items() if not (isinstance(v, np.ndarray) and v.ndim > 1)}
    J = {}
    up = UPOS
    hu, ho = LU["h"], LO["h"]
    cxo = LO["cx"]
    robe = LO["crotch_z"] < LO["z0"] + 0.2 * ho
    REPORT["robe"] = bool(robe)
    # legs: hip height from the crotch (UAL offset scaled by leg ratio) or, robed, by UAL's ratio
    if robe:
        k_leg = ho / hu
        crotch = LO["z0"] + (LU["crotch_z"] - LU["z0"]) * k_leg
    else:
        crotch = LO["crotch_z"]
        k_leg = (crotch - LO["z0"]) / (LU["crotch_z"] - LU["z0"])
    REPORT["k_leg"] = round(k_leg, 4)
    for s in SIDES:
        sgn = -1 if s == "l" else 1
        ual_hip, ual_knee, ual_ank = up[f"thigh_{s}"], up[f"calf_{s}"], up[f"foot_{s}"]
        hip_z = crotch + (ual_hip.z - LU["crotch_z"]) * k_leg
        ank_z = LO["z0"] + (ual_ank.z - LU["z0"]) * k_leg
        if robe:  # no legs to see: straight UAL legs under the torso, feet toward the toe tips
            x_hip = cxo + sgn * abs(ual_hip.x) * ho / hu
            toe = LO[f"toe_{s}"]
            y_hip = float(at_z(LO["spine_path"], hip_z)[1])
            y_ank = float(toe[1]) - (UTAIL[f"ball_leaf_{s}"].y - ual_ank.y) * k_leg
            leg = np.array([[x_hip, y_hip, hip_z], [x_hip, y_ank, ank_z]])
        else:
            leg = leg_half(LO, s)
            lu = leg_half(LU, s)
            # hip x: the leg centre just under the crotch, scaled like UAL's hip vs its leg centre
            zq = crotch - 0.08 * (crotch - LO["z0"])
            xo = at_z(leg, zq)[0] - cxo
            xu = at_z(lu, LU["crotch_z"] - 0.08 * (LU["crotch_z"] - LU["z0"]))[0] - LU["cx"]
            x_hip = cxo + xo * (abs(ual_hip.x - LU["cx"]) / max(abs(xu), 1e-3))
        hip_y = at_z(leg, min(hip_z, leg[:, 2].max()))[1] if not robe else leg[0, 1]
        J[f"thigh_{s}"] = Vector((x_hip, hip_y, hip_z))
        knee_z = ank_z + (ual_knee.z - ual_ank.z) / (ual_hip.z - ual_ank.z) * (hip_z - ank_z)
        kp = at_z(leg, knee_z)
        ap = at_z(leg, ank_z)
        J[f"calf_{s}"] = Vector((kp[0], kp[1], knee_z))
        J[f"foot_{s}"] = Vector((ap[0], ap[1], ank_z))
        toe = LO[f"toe_{s}"]
        ank = J[f"foot_{s}"]
        uf = (up[f"ball_{s}"].y - ual_ank.y) / (UTAIL[f"ball_leaf_{s}"].y - ual_ank.y)
        J[f"ball_{s}"] = Vector((ank.x + (toe[0] - ank.x) * uf, ank.y + (toe[1] - ank.y) * uf,
                                 LO["z0"] + (up[f"ball_{s}"].z - LU["z0"]) * k_leg))
        J[f"ball_leaf_{s}"] = Vector((toe[0], toe[1], J[f"ball_{s}"].z))
    hips = (J["thigh_l"] + J["thigh_r"]) / 2
    J["pelvis"] = Vector((cxo, hips.y + (up["pelvis"].y - (up["thigh_l"].y + up["thigh_r"].y) / 2),
                          hips.z + (up["pelvis"].z - up["thigh_l"].z) * k_leg))
    J["root"] = Vector((cxo, J["pelvis"].y, LO["z0"]))
    # spine: pelvis -> Head along the spine path by UAL's height fractions; Head from the neck
    # (UAL's Head joint vs its neck), or by UAL's proportion between the shoulder line and the head top
    sp = LO["spine_path"]
    top_o, top_u = LO["head_top"][2], LU["head_top"][2]
    mid_o, mid_u = LO["arm_mid"][2], LU["arm_mid"][2]
    if LO["neck"] is not None and LU["neck"] is not None:  # (UAL's own neck can go unfound at some arm droops)
        k_up = (LO["neck"][2] - J["pelvis"].z) / (LU["neck"][2] - up["pelvis"].z)
        head_z = LO["neck"][2] + (up["Head"].z - LU["neck"][2]) * k_up
    else:
        head_z = mid_o + (up["Head"].z - mid_u) / (top_u - mid_u) * (top_o - mid_o)
        k_up = (head_z - J["pelvis"].z) / (up["Head"].z - up["pelvis"].z)
    REPORT["neck_found"] = LO["neck"] is not None and LU["neck"] is not None
    REPORT["k_torso"] = round(k_up, 4)
    for n in ("spine_01", "spine_02", "spine_03", "neck_01", "Head"):
        z = J["pelvis"].z + (up[n].z - up["pelvis"].z) / (up["Head"].z - up["pelvis"].z) * (head_z - J["pelvis"].z)
        p = at_z(sp, z)
        J[n] = Vector((cxo, p[1], z))
    J["@head_top"] = Vector(LO["head_top"])
    # arms: the shoulder joint is where the depth along the arm path has fallen to UAL's ratio
    # (depth at UAL's shoulder / UAL's arm depth), then elbow, wrist, knuckle at UAL's length
    # fractions along our path to the tip
    for s in SIDES:
        a_o, a_u = arm_half(LO, s), arm_half(LU, s)
        du, do = LU[f"arm_dt_{s}"], LO[f"arm_dt_{s}"]
        iu = int(np.argmin(np.linalg.norm(a_u - np.array(up[f"upperarm_{s}"]), axis=1)))
        ratio = du[iu] / LU[f"arm_r_{s}"]
        off_u = up[f"upperarm_{s}"] - Vector(a_u[iu])
        al = arclen(a_o)
        io = next((i for i in range(len(a_o)) if do[i] <= ratio * LO[f"arm_r_{s}"] and al[i] > 0.15 * al[-1]),
                  int(np.searchsorted(al, al[-1] * arclen(a_u)[iu] / arclen(a_u)[-1])))
        sc = LO[f"arm_r_{s}"] / LU[f"arm_r_{s}"]
        sh = Vector(a_o[io]) + off_u * sc
        REPORT.setdefault("shoulder", {})[s] = {"ratio": round(float(ratio), 3), "at": round(float(al[io] / al[-1]), 3),
                                               "ual_at": round(float(arclen(a_u)[iu] / arclen(a_u)[-1]), 3)}
        tip_o, tip_u = Vector(LO[f"tip_{s}"]), Vector(LU[f"tip_{s}"])
        Lu = (tip_u - up[f"upperarm_{s}"]).length
        fe = (up[f"lowerarm_{s}"] - up[f"upperarm_{s}"]).length / Lu
        fw = fe + (up[f"hand_{s}"] - up[f"lowerarm_{s}"]).length / Lu
        fm = fw + (up[f"middle_01_{s}"] - up[f"hand_{s}"]).length / Lu
        seg = np.concatenate([np.array([sh]), a_o[io + 1:]])
        Lo = arclen(seg)[-1]
        J[f"upperarm_{s}"] = sh
        J[f"lowerarm_{s}"] = Vector(at_len(seg, fe * Lo))
        J[f"hand_{s}"] = Vector(at_len(seg, fw * Lo))
        J[f"middle_01_{s}"] = Vector(at_len(seg, fm * Lo))
        J[f"@tip_{s}"] = tip_o
        cu = up[f"clavicle_{s}"] - up["spine_03"]
        J[f"clavicle_{s}"] = J["spine_03"] + Vector((cu.x * sc, cu.y * k_up, cu.z * k_up))
    REPORT["fit_paths"] = {"arm": len(LO["arm_path"]), "leg": len(LO["leg_path"]), "spine": len(LO["spine_path"])}
    return J, LO


def fit_skintokens():
    """Mapped SkinTokens joints; missing UAL joints interpolated or from UAL proportions."""
    inv = {v: k for k, v in BM["map"].items()}
    J = {}
    for u in CORE:
        if u in inv:
            J[u] = SK_POS[inv[u]].copy()
    if "spine_01" not in J:  # a 1-bone spine (imp): spine_01 a third of the way pelvis -> spine_03
        J["spine_01"] = J["pelvis"] + (J["spine_03"] - J["pelvis"]) / 3
        if "spine_02" not in J:
            J["spine_02"] = J["pelvis"] + (J["spine_03"] - J["pelvis"]) * 2 / 3
    if "spine_02" not in J:
        J["spine_02"] = (J["spine_01"] + J["spine_03"]) / 2
    if "neck_01" not in J:
        J["neck_01"] = J["spine_03"] + (J["Head"] - J["spine_03"]) * 0.65
    top = VERTS[VERTS[:, 2] > VERTS[:, 2].max() - 0.02 * HEIGHT]
    J["@head_top"] = Vector((J["Head"].x, J["Head"].y, float(top[:, 2].max())))
    J["root"] = Vector((J["pelvis"].x, J["pelvis"].y, GROUND))
    for s in SIDES:
        h = inv.get(f"hand_{s}")
        # hand tip: the end of the SkinTokens arm chain (last joint's tail)
        ch = BM["chains"].get(f"arm_{s}") or []
        tip = SK_TAIL[ch[-1]] if ch else SK_TAIL[h]
        J[f"@tip_{s}"] = tip
        lu = (UPOS[f"middle_01_{s}"] - UPOS[f"hand_{s}"]).length / (UTAIL[f"middle_04_leaf_{s}"] - UPOS[f"hand_{s}"]).length
        J[f"middle_01_{s}"] = J[f"hand_{s}"] + (tip - J[f"hand_{s}"]) * max(lu, 0.3)
        if f"ball_{s}" not in J:
            J[f"ball_{s}"] = J[f"foot_{s}"] + Vector((0, 0.08 * HEIGHT, -0.04 * HEIGHT))
        J[f"ball_{s}"].z = max(J[f"ball_{s}"].z, GROUND + 0.005 * HEIGHT)
        foot = VERTS[(VERTS[:, 2] < GROUND + 0.06 * HEIGHT) & ((VERTS[:, 0] - J["pelvis"].x) * (1 if s == "r" else -1) > 0)]
        toe = foot[np.argmax(foot[:, 1])]
        J[f"ball_leaf_{s}"] = Vector((toe[0], toe[1], J[f"ball_{s}"].z))
        if f"clavicle_{s}" not in J:
            J[f"clavicle_{s}"] = J["spine_03"] + (J[f"upperarm_{s}"] - J["spine_03"]) * 0.1
    return J, None


FIT = job.get("fit", "auto")
J, LO = fit_auto() if FIT == "auto" else fit_skintokens()
# sanity reference: SkinTokens' mapped joints vs ours (also when they are the fit)
_inv = {v: k for k, v in BM["map"].items() if k in SK_POS}
REPORT["vs_skintokens"] = {u: round((J[u] - SK_POS[_inv[u]]).length / HEIGHT, 3) for u in CORE if u in _inv and u in J}
_d = sorted(REPORT["vs_skintokens"].values())
REPORT["vs_skintokens_median"] = _d[len(_d) // 2] if _d else None
REPORT["vs_skintokens_max"] = _d[-1] if _d else None
REPORT["joints"] = {k: [round(x, 4) for x in v] for k, v in J.items()}


# --- build our armature: UAL's, joints moved, frames turned --------------------------------
# Bones that keep UAL's rest frame (R = I, "delta"): our hunch/lean stays as an offset under UAL's
# motion. Arms, hands and legs turn to UAL's directions ("dir"): needed for the arms (UAL rests in
# a T-pose, ours in an A-pose); legs so the feet land where UAL's do.
DELTA = {"spine_01", "spine_02", "spine_03", "neck_01", "Head", "clavicle_l", "clavicle_r"}
if job.get("leg_mode") == "delta":
    DELTA |= {f"{k}_{s}" for k in ("thigh", "calf", "foot", "ball") for s in SIDES}


def build():
    rig = UA.copy()
    rig.data = UA.data.copy()
    rig.animation_data_clear()
    rig.name = "Armature"
    rig.data.name = "Armature"
    scene.collection.objects.link(rig)
    rig.matrix_world = UW.copy()
    Winv = rig.matrix_world.inverted()
    k0 = HEIGHT / UAL_H
    # World position and turn R per UAL joint: CORE joints come from the fit; a bone with a main
    # child turns so its direction meets that child (R = shortest arc from UAL's direction) and
    # scales by its length ratio; every other joint (fingers, leaves) is its parent's offset turned
    # by the parent's R and scaled by the parent's ratio.
    Rw, P, sc = {}, {}, {}
    for n in ORDER:
        par = UPAR[n]
        P[n] = J[n].copy() if n in J else P[par] + Rw[par] @ ((UPOS[n] - UPOS[par]) * sc[par])
        mc = MAIN_CHILD.get(n)
        if n in ("root", "pelvis"):
            Rw[n], sc[n] = Quaternion(), k0
        elif n in DELTA and mc and mc in J:  # keeps UAL's frame: our stance rides on UAL's motion
            Rw[n] = Quaternion()
            sc[n] = (J[mc] - P[n]).length / max(((UAL_TOP if mc == "@head_top" else UPOS[mc]) - UPOS[n]).length, 1e-6)
        elif mc and mc in J:
            ual_dir = (UAL_TOP - UPOS[n]) if mc == "@head_top" else (UPOS[mc] - UPOS[n])
            our_dir = J[mc] - P[n]
            Rw[n] = ual_dir.rotation_difference(our_dir)
            sc[n] = our_dir.length / max(ual_dir.length, 1e-6)
        else:
            Rw[n], sc[n] = Rw[par], sc[par]
    for s in SIDES:
        REPORT.setdefault("hand_scale", {})[s] = round(sc[f"hand_{s}"], 3)
    select(rig)
    bpy.ops.object.mode_set(mode="EDIT")
    eb = rig.data.edit_bones
    for b in eb:
        b.use_connect = False
    for n in ORDER:
        b = eb[n]
        L0 = b.length
        rot_w = Rw[n].to_matrix() @ (UW @ UREST[n]).to_3x3()
        b.matrix = Winv @ (Matrix.Translation(P[n]) @ rot_w.to_4x4())
        b.length = max(L0 * sc[n], 0.004 * HEIGHT)
    bpy.ops.object.mode_set(mode="OBJECT")
    REPORT["R_deg"] = {n: round(math.degrees(Rw[n].angle), 1) for n in CORE if n in Rw and "leaf" not in n}
    REPORT["scale"] = {n: round(sc[n], 3) for n in CORE if n in sc and "leaf" not in n}
    return rig, P, Rw


RIG, P, RW = build()


# --- extra chains: SkinTokens chains for parts UAL has no bone for ----------------------------
# job extras: [{root (SkinTokens bone), kind (wing | tail | limb | strip | head), side?, parent?
# (UAL bone; default the UAL bone mapped from the root's nearest mapped ancestor, else the nearest
# trunk bone)}]. The root's whole subtree is copied into our armature (SkinTokens' joints and bone
# rolls), parented to that UAL bone, and renamed so rig_topology (the anim stage's procedural
# clips) finds it by name: wing_<side>_00.., tail_00.. (the first tail; more are tail<k>_00..),
# limb_<side><k>_00.., strip<k>_00.., head<k>_00... UAL clips key only the 65 UAL bones; the
# extras get a procedural layer per clip (extra_layer below) or follow their parent.
EXTRA_PREFIX = {"wing": "wing_{side}_", "tail": "tail_", "limb": "limb_{side}{k}_", "strip": "strip{k}_",
                "head": "head{k}_"}
TRUNK_UAL = ["pelvis", "spine_01", "spine_02", "spine_03", "neck_01", "Head", "clavicle_l", "clavicle_r",
             "upperarm_l", "upperarm_r"]
EXTRAS = []      # [{kind, side, prefix, root, parent, bones: [new names, parents first]}]
EXTRA_OF = {}    # our extra bone -> SkinTokens bone


def _seg_dist(p, a, b):
    ab = b - a
    t = max(0.0, min(1.0, (p - a).dot(ab) / max(ab.length_squared, 1e-12)))
    return (a + ab * t - p).length


def auto_head(side):
    """A two-bone neck+head chain for the head on one side of a multi-headed mesh: the vertices above
    the neck joint on that side of the centre line; base at the neck joint's height under their
    centroid, through 40% of their height, to their top. Registered as synthetic SkinTokens bones
    (@head_<side>_0/1) so add_extras copies them like any chain (no SkinTokens weights: use
    extra_weights "heat"). Returns the root's name, or None when that side has no head."""
    cx = J["pelvis"].x
    z0 = J["neck_01"].z
    sgn = -1 if side == "l" else 1
    sel = VERTS[(VERTS[:, 2] > z0) & (sgn * (VERTS[:, 0] - cx) > 0.03 * HEIGHT)]
    if len(sel) < 50:
        return None
    top = float(sel[:, 2].max())
    hi = sel[sel[:, 2] > z0 + 0.5 * (top - z0)]  # the head itself, not the shoulder under it
    c = (hi if len(hi) >= 10 else sel).mean(0)
    pts = [Vector((c[0], c[1], z0)), Vector((c[0], c[1], z0 + 0.4 * (top - z0))), Vector((c[0], c[1], top))]
    names = [f"@head_{side}_0", f"@head_{side}_1"]
    for i, n in enumerate(names):
        SK_POS[n], SK_TAIL[n] = pts[i], pts[i + 1]
        SK_PARENT[n] = names[i - 1] if i else None
        SK_KIDS[n] = [names[i + 1]] if i + 1 < len(names) else []
        SK_Z[n] = Vector((0, 1, 0))
    return names[0]


def add_extras(rig, specs):
    if not specs:
        return
    hw = rig.matrix_world
    Winv = hw.inverted()
    pos = {b.name: (hw @ b.head_local, hw @ b.tail_local) for b in rig.data.bones}
    count = {}
    for sp in specs:
        if sp.get("auto") == "head":  # a second head (ettin) SkinTokens has no chain for: from the mesh
            sp = dict(sp, root=auto_head(sp["side"]), parent=sp.get("parent") or "spine_03")
        r = sp.get("root")
        if r not in SK_POS:
            REPORT.setdefault("extras_missing", []).append(r)
            continue
        kind = sp["kind"]
        k = count.get(kind, 0)
        count[kind] = k + 1
        side = sp.get("side") or ("l" if SK_TAIL[r].x < J["pelvis"].x else "r")
        prefix = sp.get("prefix") or EXTRA_PREFIX[kind].format(side=side, k=k + 1)
        if kind == "tail" and k:
            prefix = f"tail{k + 1}_"
        if kind == "wing" and any(e["prefix"] == prefix for e in EXTRAS):
            prefix = f"wing_{side}{k + 1}_"
        parent = sp.get("parent")
        if not parent:
            a = SK_PARENT.get(r)
            while a and a not in BM["map"]:
                a = SK_PARENT.get(a)
            parent = BM["map"].get(a) if a else None
            if parent and parent not in TRUNK_UAL:  # a finger/hand/leg mapping: the arm or the hips
                parent = None
        if not parent:  # no bonemap (fit auto on a failed map): the nearest bone of the kind's usual set
            h0 = SK_POS[r]
            near = {"wing": ["spine_02", "spine_03", "neck_01"], "tail": ["pelvis", "spine_01"],
                    "strip": ["pelvis", "spine_01"], "head": ["spine_03", "neck_01"]}.get(kind, TRUNK_UAL)
            parent = min(near, key=lambda n: _seg_dist(h0, *pos[n]))
        # subtree, the longest branch first (so the main chain is numbered 00, 01, ...)
        def depth(n):
            return 1 + max((depth(c) for c in SK_KIDS[n]), default=0)

        order, stack = [], [r]
        while stack:
            n = stack.pop()
            order.append(n)
            stack.extend(sorted(SK_KIDS[n], key=depth))
        names = {sk: f"{prefix}{i:02d}" for i, sk in enumerate(order)}
        EXTRAS.append({"kind": kind, "side": side, "prefix": prefix, "root": r, "parent": parent,
                       "bones": [names[s] for s in order], "sk": order})
        EXTRA_OF.update({v: k_ for k_, v in names.items()})
    select(rig)
    bpy.ops.object.mode_set(mode="EDIT")
    eb = rig.data.edit_bones
    for ex in EXTRAS:
        for new, sk in zip(ex["bones"], ex["sk"]):
            b = eb.new(new)
            hd, tl = SK_POS[sk], SK_TAIL[sk]
            if (tl - hd).length < 0.005 * HEIGHT:
                tl = hd + (tl - hd).normalized() * 0.01 * HEIGHT if (tl - hd).length > 1e-9 else hd + Vector((0, 0, 0.01 * HEIGHT))
            b.head, b.tail = Winv @ hd, Winv @ tl
            b.align_roll(Winv.to_3x3() @ SK_Z[sk])
            b.use_deform = True
        for new, sk in zip(ex["bones"], ex["sk"]):
            p = SK_PARENT.get(sk)
            eb[new].parent = eb[ex["prefix"] + f"{ex['sk'].index(p):02d}"] if p in ex["sk"] else eb[ex["parent"]]
    bpy.ops.object.mode_set(mode="OBJECT")
    REPORT["extras"] = [{k_: v for k_, v in ex.items() if k_ != "sk"} | {"sk_bones": ex["sk"]} for ex in EXTRAS]


add_extras(RIG, job.get("extras") or [])
REST = {b.name: b.matrix_local.copy() for b in RIG.data.bones}


def rest_local(rest, par, n):
    return (rest[par[n]].inverted() @ rest[n]) if par[n] else rest[n]


C = {n: (rest_local(REST, UPAR, n).inverted() @ rest_local(UREST, UPAR, n)) for n in UREST}


# --- sockets --------------------------------------------------------------------------------
def add_sockets(rig, want):
    found = {}
    Winv = rig.matrix_world.inverted()
    select(rig)
    bpy.ops.object.mode_set(mode="EDIT")
    eb = rig.data.edit_bones

    def new_bone(name, parent, head, y_dir, z_dir, length):
        b = eb.new(name)
        y = Vector(y_dir).normalized()
        z = (Vector(z_dir) - y * Vector(z_dir).dot(y)).normalized()
        x = y.cross(z)
        m = Matrix((x, y, z)).transposed().to_4x4()
        m.translation = Vector(head)
        b.head = (0, 0, 0)
        b.tail = (0, 1, 0)
        b.matrix = Winv @ m
        b.length = length
        b.parent = eb[parent]
        b.use_deform = False
        found[name] = parent

    hw = rig.matrix_world
    for s in SIDES:
        wr = hw @ eb[f"hand_{s}"].head
        mk = hw @ eb[f"middle_01_{s}"].head
        y = (hw @ eb[f"index_01_{s}"].head) - (hw @ eb[f"pinky_01_{s}"].head)  # UAL's knuckle line = blade
        fingers = mk - wr
        new_bone(f"grip_{s}", f"hand_{s}", wr + fingers * 0.6, y, -fingers, 0.05 * HEIGHT)
        found[f"hand_{s}"] = f"hand_{s}"
    if "head" in want:
        hb = eb["Head"]
        m = hw @ hb.matrix
        new_bone("head", "Head", m.translation, m.to_3x3() @ Vector((0, 1, 0)), m.to_3x3() @ Vector((0, 0, 1)), 0.04 * HEIGHT)
    if "back" in want:
        s3 = hw @ eb["spine_03"].head
        new_bone("back", "spine_03", (J["pelvis"].x, s3.y - 0.06 * HEIGHT, s3.z), (0, 0, 1), (0, -1, 0), 0.05 * HEIGHT)
    for s, sx in (("l", -1), ("r", 1)):
        if f"hip_{s}" in want:
            pel = hw @ eb["pelvis"].head
            zh = max((hw @ eb["thigh_l"].head).z, (hw @ eb["thigh_r"].head).z)
            new_bone(f"hip_{s}", "pelvis", (J["pelvis"].x + sx * 0.12 * HEIGHT, pel.y, zh), (0, 0, 1), (0, 1, 0), 0.05 * HEIGHT)
    bpy.ops.object.mode_set(mode="OBJECT")
    return found


SOCKETS = add_sockets(RIG, set(job.get("sockets") or []) | {"hand_l", "hand_r"})
REPORT["sockets"] = SOCKETS
FINGERS = ("thumb", "index", "middle", "ring", "pinky")
NODEFORM = {"root"} | {b.name for b in RIG.data.bones if b.name.endswith(tuple(f"leaf_{s}" for s in SIDES))} | set(SOCKETS) - {"hand_l", "hand_r"}
if job.get("fingers", "hand") == "hand":
    NODEFORM |= {b.name for b in RIG.data.bones if b.name.startswith(FINGERS)}
for b in RIG.data.bones:
    b.use_deform = b.name not in NODEFORM


# --- weights -----------------------------------------------------------------------------------
def deform_target(n):
    """A deform bone for a UAL bone name (fingers -> hand when folded, leaf -> parent)."""
    while n and n in NODEFORM:
        n = UPAR.get(n)
    return n or "pelvis"


def set_groups(obj, W):
    """W: {group: np.array weights per vertex} -> obj vertex groups (old ones removed)."""
    obj.vertex_groups.clear()
    for g, w in W.items():
        idx = np.nonzero(w > 1e-4)[0]
        if not len(idx):
            continue
        vg = obj.vertex_groups.new(name=g)
        for i in idx:
            vg.add([int(i)], float(w[i]), "REPLACE")


def get_groups(obj):
    n = len(obj.data.vertices)
    names = {vg.index: vg.name for vg in obj.vertex_groups}
    W = {}
    for v in obj.data.vertices:
        for g in v.groups:
            nm = names[g.group]
            W.setdefault(nm, np.zeros(n))[v.index] += g.weight
    return W


def fold(W):
    out = {}
    for g, w in W.items():
        t = g if g in EXTRA_OF else deform_target(g) if g in UPAR else None
        if t is None:
            continue
        out[t] = out.get(t, 0) + w
    return out


def weights_skintokens():
    """SkinTokens groups merged by name onto the UAL bones: a mapped bone gives its weights to its
    UAL bone, every other bone to its nearest mapped ancestor (fingers past the wrist, skull tips,
    pelvis->thigh links, toes)."""
    mp = BM["map"]
    W0 = get_groups(ME)
    W = {}
    lost = 0.0
    for g, w in W0.items():
        n = g
        while n and n not in mp:
            n = SK_PARENT.get(n)
        t = deform_target(mp[n]) if n else "pelvis"
        if not n:
            lost += float(w.sum())
        W[t] = W.get(t, 0) + w
    REPORT["weights_unmapped_to_pelvis"] = round(lost, 2)
    return W


def heat_on(obj):
    """Blender's automatic (bone heat) weights on obj; returns {group: weights}, {} on failure."""
    obj.vertex_groups.clear()
    for m in list(obj.modifiers):
        if m.type == "ARMATURE":
            obj.modifiers.remove(m)
    select(obj, RIG)
    bpy.context.view_layer.objects.active = RIG
    bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    W = get_groups(obj)
    for m in list(obj.modifiers):
        if m.type == "ARMATURE":
            obj.modifiers.remove(m)
    mw = obj.matrix_world.copy()
    obj.parent = None
    obj.matrix_world = mw
    return W


def drop_small_islands(obj, frac):
    """Delete the loose parts of obj smaller than frac of its vertices (voxel crumbs, a prop's tip):
    bone heat finds no solution when a part sees no bone. Their vertices on the game mesh still get
    weights: the nearest-surface transfer maps them onto the body. Returns the parts dropped."""
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    comp = [-1] * len(bm.verts)
    sizes = []
    for v in bm.verts:
        if comp[v.index] >= 0:
            continue
        c, st, n = len(sizes), [v], 0
        comp[v.index] = c
        while st:
            x = st.pop()
            n += 1
            for e in x.link_edges:
                o = e.other_vert(x)
                if comp[o.index] < 0:
                    comp[o.index] = c
                    st.append(o)
        sizes.append(n)
    small = {c for c, n in enumerate(sizes) if n < frac * len(bm.verts)}
    if small:
        bmesh.ops.delete(bm, geom=[v for v in bm.verts if comp[v.index] in small], context="VERTS")
        bm.to_mesh(obj.data)
    bm.free()
    return len(small)


def weights_heat():
    """Bone heat on a watertight voxel-remeshed proxy and a nearest-surface transfer (Pixal3D meshes
    are not manifold: heat on the mesh itself fails, 'failed to find a solution'; job heat_direct
    tries the mesh first).
    The proxy drops its small loose parts; if heat still fails, coarser voxels are tried."""
    t = time.time()
    # direct heat only on request: where it "works" on a non-manifold mesh (priest) the loose shells
    # get each other's bones and the robe shatters; the proxy gives connected shells one field
    W = heat_on(ME) if job.get("heat_direct") else {}
    cover = float(np.mean(sum(W.values()) > 1e-6)) if W else 0.0
    REPORT["heat_direct_cover"] = round(cover, 4) if job.get("heat_direct") else None
    if cover < 0.98:
        tries = [(job.get("heat_voxel", 0.012), 0.0), (job.get("heat_voxel", 0.012), 0.02), (0.02, 0.02), (0.03, 0.05)]
        for voxel, frac in tries:
            proxy = ME.copy()
            proxy.data = ME.data.copy()
            proxy.name = "heat_proxy"
            scene.collection.objects.link(proxy)
            proxy.vertex_groups.clear()
            rm = proxy.modifiers.new("remesh", "REMESH")
            rm.mode = "VOXEL"
            rm.voxel_size = voxel * HEIGHT
            select(proxy)
            bpy.ops.object.modifier_apply(modifier=rm.name)
            dropped = drop_small_islands(proxy, frac) if frac else 0
            Wp = heat_on(proxy)
            pc = float(np.mean(sum(Wp.values()) > 1e-6)) if Wp else 0.0
            REPORT["heat_proxy_verts"] = len(proxy.data.vertices)
            REPORT["heat_proxy_cover"] = round(pc, 4)
            REPORT["heat_voxel"], REPORT["heat_islands_dropped"] = voxel, dropped
            if pc >= 0.9:
                break
            bpy.data.objects.remove(proxy)
            proxy = None
        if proxy is None:
            REPORT["heat_seconds"] = round(time.time() - t, 1)
            return {}
        ME.vertex_groups.clear()
        select(proxy, ME)
        bpy.context.view_layer.objects.active = proxy
        bpy.ops.object.data_transfer(data_type="VGROUP_WEIGHTS", use_create=True, vert_mapping="POLYINTERP_NEAREST",
                                     layers_select_src="ALL", layers_select_dst="NAME", mix_mode="REPLACE")
        W = get_groups(ME)
        bpy.data.objects.remove(proxy)
    REPORT["heat_seconds"] = round(time.time() - t, 1)
    return W


def weights_transfer():
    """UAL mannequin posed onto our rest skeleton (its own weights), then nearest-surface transfer."""
    # pose UA so every bone sits at our rest frame (armature space; same object transform)
    if UA.animation_data:
        UA.animation_data.action = None
    for pb in UA.pose.bones:
        pb.rotation_mode = "QUATERNION"
    for n in ORDER:
        pb = UA.pose.bones[n]
        par = UPAR[n]
        lu = rest_local(UREST, UPAR, n)
        target_local = rest_local(REST, UPAR, n) if par else REST[n]
        basis = lu.inverted() @ target_local
        loc, rot, _ = basis.decompose()
        pb.location, pb.rotation_quaternion = loc, rot
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    mesh = bpy.data.meshes.new_from_object(UM.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)
    src = bpy.data.objects.new("ual_conformed", mesh)
    scene.collection.objects.link(src)
    src.matrix_world = UM.matrix_world.copy()
    for vg in UM.vertex_groups:
        src.vertex_groups.new(name=vg.name)
    for pb in UA.pose.bones:
        pb.location, pb.rotation_quaternion = Vector(), Quaternion()
    ME.vertex_groups.clear()
    select(src, ME)
    bpy.context.view_layer.objects.active = src
    bpy.ops.object.data_transfer(data_type="VGROUP_WEIGHTS", use_create=True, vert_mapping="POLYINTERP_NEAREST",
                                 layers_select_src="ALL", layers_select_dst="NAME", mix_mode="REPLACE")
    W = get_groups(ME)
    co, _ = mesh_world(src, tris=False)
    REPORT["transfer_src_bbox"] = [co.min(0).round(3).tolist(), co.max(0).round(3).tolist()]
    bpy.data.objects.remove(src)
    return W


t = time.time()
WM = job.get("weights", "skintokens")
EW = job.get("extra_weights", "skintokens")
for _n in EXTRA_OF:  # "skintokens": the UAL bones' weights come without the extras, as on a plain rig
    RIG.data.bones[_n].use_deform = EW == "heat"
W = {"skintokens": weights_skintokens, "heat": weights_heat, "transfer": weights_transfer}[WM]()
for _n in EXTRA_OF:
    RIG.data.bones[_n].use_deform = True
W = fold(W)
def sk_mapped(skw, skip):
    """SkinTokens weights (skw, minus the bones in skip) merged onto the UAL bones by the bonemap,
    like weights_skintokens: a bone's weight goes to its nearest mapped ancestor's UAL bone."""
    mp = BM["map"]
    out = {}
    for g, w in skw.items():
        if g in skip:
            continue
        n = g
        while n and n not in mp:
            n = SK_PARENT.get(n)
        t = deform_target(mp[n]) if n else "pelvis"
        out[t] = out.get(t, 0) + w
    return out


def ring_fade(core, rings):
    """1 on core vertices, falling by 1 / (rings + 1) per edge ring outward, 0 beyond."""
    e = np.empty(len(ME.data.edges) * 2, dtype=np.int64)
    ME.data.edges.foreach_get("vertices", e)
    e = e.reshape(-1, 2)
    z = core.astype(float)
    cur = core.copy()
    for k in range(1, rings + 1):
        nb = np.zeros(len(core), bool)
        nb[e[cur[e[:, 0]], 1]] = True
        nb[e[cur[e[:, 1]], 0]] = True
        nb &= z == 0
        z[nb] = 1.0 - k / (rings + 1)
        cur = cur | nb
    return z


if EXTRA_OF and EW in ("skintokens", "zone"):
    # "skintokens": each vertex's SkinTokens weight on the adopted chains (s) goes to those chains as
    # painted; the rest (1 - s) is the UAL heat weights scaled down. "zone": the same, but where the
    # chains have any weight (and a few edge rings around) the rest comes from SkinTokens' own weights
    # (mapped onto UAL bones), so the seam between a tail and a thigh is SkinTokens' (or the rig
    # stage's tail/wing seam), not heat's; heat elsewhere
    _nv = len(VERTS)
    S = {n: SKW.get(sk, np.zeros(_nv)).copy() for n, sk in EXTRA_OF.items()}
    s = np.clip(sum(S.values()), 0.0, 1.0)
    base = {g: w for g, w in W.items() if g not in EXTRA_OF}
    bt = sum(base.values()) if base else np.zeros(_nv)
    base = {g: w / np.maximum(bt, 1e-9) for g, w in base.items()}
    if EW == "zone":
        skb = sk_mapped(SKW, set(EXTRA_OF.values()))
        st = sum(skb.values())
        skb = {g: w / np.maximum(st, 1e-9) for g, w in skb.items()}
        z = ring_fade(s > 0.02, int(job.get("zone_rings", 4)))
        REPORT["zone_verts"] = int((z > 0).sum())
        base = {g: base.get(g, 0) * (1 - z) + skb.get(g, 0) * z for g in set(base) | set(skb)}
    W = {g: w * (1.0 - s) for g, w in base.items()}
    W.update(S)
    REPORT["extra_share"] = {"verts_over_half": round(float(np.mean(s > 0.5)), 4), "mean": round(float(s.mean()), 4)}
    for ex in EXTRAS:
        ex_s = sum(S[n] for n in ex["bones"])
        ex["verts_over_half"] = int((ex_s > 0.5).sum())
    REPORT["extras"] = [{k_: v for k_, v in ex.items() if k_ != "sk"} | {"sk_bones": ex["sk"]} for ex in EXTRAS]
# limit 4, normalise, and fill unweighted vertices from the nearest weighted one
names = sorted(W)
if not names:
    raise SystemExit(f"weights {WM}: no weights at all")
A = np.stack([W[g] for g in names], 1)
if A.shape[1] > 4:
    kth = np.partition(A, -4, axis=1)[:, -4][:, None]
    A[A < kth] = 0
tot = A.sum(1)
bad = tot < 1e-6
REPORT["unweighted_verts"] = int(bad.sum())
if bad.any() and (~bad).any():
    kd = KDTree(int((~bad).sum()))
    good = np.nonzero(~bad)[0]
    for k, i in enumerate(good):
        kd.insert(VERTS[i], k)
    kd.balance()
    for i in np.nonzero(bad)[0]:
        _, k, _ = kd.find(VERTS[i])
        A[i] = A[good[k]]
    tot = A.sum(1)
A = A / np.maximum(tot, 1e-9)[:, None]
set_groups(ME, {g: A[:, k] for k, g in enumerate(names)})
REPORT["weights_seconds"] = round(time.time() - t, 1)
REPORT["groups"] = len([g for g in names if A[:, names.index(g)].max() > 0])
ME.parent = RIG
ME.matrix_parent_inverse = RIG.matrix_world.inverted()
mod = ME.modifiers.new("Armature", "ARMATURE")
mod.object = RIG


# --- LODs and the rigged export --------------------------------------------------------------
def transfer_lod(path):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    lod = next(o for o in bpy.data.objects if o not in before and o.type == "MESH")
    for o in set(bpy.data.objects) - before - {lod}:
        bpy.data.objects.remove(o)
    for i in range(min(len(lod.data.materials), len(ME.data.materials))):
        lod.data.materials[i] = ME.data.materials[i]
    select(ME, lod)
    bpy.context.view_layer.objects.active = ME
    bpy.ops.object.data_transfer(data_type="VGROUP_WEIGHTS", use_create=True, vert_mapping="POLYINTERP_NEAREST",
                                 layers_select_src="ALL", layers_select_dst="NAME", mix_mode="REPLACE")
    select(lod)
    bpy.ops.object.vertex_group_limit_total(group_select_mode="ALL", limit=4)
    bpy.ops.object.vertex_group_normalize_all(group_select_mode="ALL", lock_active=False)
    lod.parent = RIG
    lod.matrix_parent_inverse = RIG.matrix_world.inverted()
    lod.modifiers.new("Armature", "ARMATURE").object = RIG
    return lod


OUT = Path(job["out_dir"])
(OUT / "anims").mkdir(parents=True, exist_ok=True)
UA_HIDE = [UA, UM]
for o in UA_HIDE:
    o.hide_set(True)
    o.hide_render = True
lods = {k: transfer_lod(p) for k, p in (job.get("lods") or {}).items()}
select(ME, RIG)
bpy.ops.export_scene.gltf(filepath=str(OUT / "model_rigged.glb"), export_format="GLB", use_selection=True, export_yup=True)
for k, lod in lods.items():
    select(lod, RIG)
    bpy.ops.export_scene.gltf(filepath=str(OUT / f"model_rigged_lod{k}.glb"), export_format="GLB", use_selection=True,
                              export_yup=True, export_image_format="NONE")
    lod.hide_set(True)
    lod.hide_render = True
REPORT["lods"] = sorted(lods)
REPORT["rig_seconds"] = round(time.time() - T0, 1)
print(f"rig done {REPORT['rig_seconds']}s weights {WM} unweighted {REPORT['unweighted_verts']}", flush=True)


# --- clips ---------------------------------------------------------------------------------------
def set_action(o, act):
    ad = o.animation_data or o.animation_data_create()
    ad.action = act
    if getattr(act, "slots", None) and len(act.slots):
        ad.action_slot = act.slots[0]


def leg_len(pos, s, ground):
    return (pos[f"thigh_{s}"] - pos[f"calf_{s}"]).length + (pos[f"calf_{s}"] - pos[f"foot_{s}"]).length + \
        (pos[f"foot_{s}"].z - ground)


OUR_POS = {b.name: (RIG.matrix_world @ b.head_local) for b in RIG.data.bones}
HIP = sum(leg_len(OUR_POS, s, GROUND) for s in SIDES) / sum(leg_len(UPOS, s, 0.0) for s in SIDES)
REPORT["hip_scale"] = round(HIP, 4)
UAL_BONES = [n for n in ORDER]


HIPZ_U = sum(UPOS[f"thigh_{s}"].z for s in SIDES) / 2
HIPZ_O = sum(OUR_POS[f"thigh_{s}"].z for s in SIDES) / 2
PEL_REST_ARM = REST["pelvis"]          # our pelvis rest, armature space
UPEL_REST_ARM = UREST["pelvis"]
CHECK = [("upperarm", "lowerarm"), ("lowerarm", "hand"), ("thigh", "calf"), ("calf", "foot")]


def sample_clip(ual_clip):
    """Per output frame: {bone: our basis quaternion} and the pelvis armature-space position.
    Rotations are UAL's local rotations (C_b converts to our rest); the pelvis goes where UAL's
    goes, its height above the hips scaled by our leg length / UAL's (HIP), so straight UAL legs
    put our feet on the floor; horizontal motion likewise scaled."""
    segs, total = [], 0.0
    for part in ual_clip.split("+"):
        act = ACTS[part]
        f0, f1 = act.frame_range
        segs.append((act, f0, f1, total))
        total += (f1 - f0) / UAL_FPS
    n = max(1, round(total * FPS))
    frames, max_loc, max_root, dir_err = [], 0.0, 0.0, 0.0
    for i in range(n + 1):
        tt = total * i / n
        k = max(j for j, sg in enumerate(segs) if sg[3] <= tt + 1e-9)
        act, f0, f1, t0 = segs[k]
        set_action(UA, act)
        uf = min(f1, f0 + (tt - t0) * UAL_FPS)
        scene.frame_set(int(math.floor(uf)), subframe=uf - math.floor(uf))
        rot = {}
        for nm in UAL_BONES:
            up = UA.pose.bones[nm]
            rot[nm] = (C[nm].to_quaternion() @ up.matrix_basis.to_quaternion()).normalized()
            if nm != "pelvis":
                max_loc = max(max_loc, up.location.length)
        max_root = max(max_root, (UA.pose.bones["root"].matrix_basis.to_quaternion().angle))
        pu = UA.pose.bones["pelvis"].matrix.translation  # armature space
        r0 = UPEL_REST_ARM.translation
        o0 = PEL_REST_ARM.translation
        # our hips at UAL's hip height x HIP; our pelvis-over-hips offset kept
        pel = Vector((o0.x + (pu.x - r0.x) * HIP, o0.y + (pu.y - r0.y) * HIP,
                      (pu.z - (r0.z - HIPZ_U)) * HIP + (o0.z - HIPZ_O)))
        frames.append({"rot": rot, "pel": pel, "ual_dirs": {f"{a}_{s}": (UA.pose.bones[f"{b}_{s}"].head - UA.pose.bones[f"{a}_{s}"].head).normalized()
                                                             for a, b in CHECK for s in SIDES}})
    UA.animation_data.action = None
    return frames, n, round(total, 3), round(max_loc, 4), round(math.degrees(max_root), 2)


# Extra chains' layer on the UAL clips: degrees at the chain's end (the root turns least), cycles per
# loop (one-shots: cycles over the clip under a sin(pi u) envelope, so they start and end at rest
# like the idle's first frame). Tails and limbs sway side to side (a hanging one swings like a
# pendulum), strips (rags, loincloths) swing fore and aft, wings lift and drop a little at the
# root, a second head looks about. Death: none (the chains ride the fall). Clip-length loops.
EXTRA_AMP = {"tail": {"idle": 6, "walk": 12, "run": 16, "attack_melee": 12, "hit": 10, "cast": 6},
             "limb": {"idle": 5, "walk": 8, "run": 10, "attack_melee": 10, "hit": 8, "cast": 6},
             "strip": {"idle": 3, "walk": 8, "run": 12, "attack_melee": 6, "hit": 6, "cast": 3},
             "wing": {"idle": 3, "walk": 5, "run": 8, "attack_melee": 10, "hit": 8, "cast": 6},
             "head": {"idle": 6, "walk": 5, "run": 5, "attack_melee": 6, "hit": 8, "cast": 5}}
EXTRA_CYC = {"idle": 1, "walk": 1, "run": 1}


def extra_axes():
    """Per extra bone: (sway axis, lift axis) in the bone's local rest frame and its share of the
    chain's angle (root small, growing toward the end, summing to 1 along the main chain)."""
    out = {}
    if not EXTRAS:
        return out
    hw = RIG.matrix_world.to_3x3()
    for ex in EXTRAS:
        main = [ex["bones"][0]]
        while True:
            kids = [c.name for c in RIG.data.bones[main[-1]].children if c.name in ex["bones"]]
            if not kids:
                break
            main.append(kids[0])
        nmain = len(main)
        tot = nmain * (nmain + 1) / 2
        for nm in ex["bones"]:
            b = RIG.data.bones[nm]
            d = (hw @ (b.tail_local - b.head_local)).normalized()
            if ex["kind"] == "wing":
                sway = Vector((0, 1, 0))           # about the forward axis: the wing rises and drops
                lift = d.cross(Vector((0, 0, 1)))
            elif ex["kind"] == "strip":
                sway = Vector((1, 0, 0))           # about the side axis: fore and aft
                lift = Vector((0, 1, 0))
            elif ex["kind"] == "head":
                sway, lift = Vector((0, 0, 1)), Vector((1, 0, 0))
            else:                                  # tail, limb: side to side; hanging: a pendulum
                sway = Vector((0, 0, 1)) if abs(d.z) < 0.7 else Vector((0, 1, 0))
                lift = d.cross(sway)
            sway = sway - d * sway.dot(d)
            lift = lift - d * lift.dot(d)
            tolocal = (hw @ b.matrix_local.to_3x3()).inverted()
            k = main.index(nm) if nm in main else nmain - 1
            share = (k + 1) / tot if ex["kind"] != "wing" else (0.7 if k == 0 else 0.3 / max(nmain - 1, 1))
            sgn = -1.0 if (ex["kind"] == "wing" and ex["side"] == "r") else 1.0  # both wings rise together
            out[nm] = (ex["kind"], (tolocal @ sway).normalized() * sgn if sway.length > 1e-6 else Vector((1, 0, 0)),
                       (tolocal @ lift).normalized() if lift.length > 1e-6 else Vector((0, 0, 1)), share, k)
    return out


EXTRA_AXES = None


def extra_layer(clip, i, n):
    """{extra bone: basis quaternion} on frame i of n."""
    global EXTRA_AXES
    if EXTRA_AXES is None:
        EXTRA_AXES = extra_axes()
    out = {}
    if not EXTRA_AXES:
        return out
    u = i / max(n, 1)
    T = n / FPS
    loop = clip in LOOPS
    for nm, (kind, sway, lift, share, k) in EXTRA_AXES.items():
        amp = math.radians((EXTRA_AMP.get(kind) or {}).get(clip, 0.0)) * float(job.get("extra_amp", 1.0))
        if not amp:
            out[nm] = Quaternion()
            continue
        if loop:
            cyc = max(1, round(T / 2.5)) if clip == "idle" else EXTRA_CYC.get(clip, 1)
            env = 1.0
        else:
            cyc = max(1, round(T / 0.9))
            env = math.sin(math.pi * u)
        ph = 2 * math.pi * cyc * u - 0.5 * k
        a = amp * share * env * math.sin(ph)
        b = amp * share * env * 0.35 * math.sin(2 * ph)
        if kind == "wing":  # the wing lifts (one side mirrored by the axis sign), no flutter
            a, b = amp * share * env * 0.5 * (1 - math.cos(ph)), 0.0
        out[nm] = Quaternion(sway, a) @ Quaternion(lift, b)
    return out


def key_frames(name, frames, lift=None):
    act_out = bpy.data.actions.new(name)
    RIG.animation_data_create()
    RIG.animation_data.action = act_out
    prev = {}
    rinv = PEL_REST_ARM.to_quaternion().inverted()
    for i, fr in enumerate(frames):
        for nm, q in list(fr["rot"].items()) + list(extra_layer(name, i, len(frames) - 1).items()):
            if nm in prev and prev[nm].dot(q) < 0:
                q = -q
            prev[nm] = q
            pb = RIG.pose.bones[nm]
            pb.rotation_quaternion = q
            pb.keyframe_insert("rotation_quaternion", frame=i + 1)
        pel = fr["pel"] + Vector((0, 0, lift[i] if lift else 0.0))
        pb = RIG.pose.bones["pelvis"]
        pb.location = rinv @ (pel - PEL_REST_ARM.translation)  # root stays at rest (checked: max_root)
        pb.keyframe_insert("location", frame=i + 1)
    return act_out


# metrics (same definitions as blender_retarget_ual.evaluate)
for pb in RIG.pose.bones:
    pb.rotation_mode = "QUATERNION"
bpy.context.view_layer.update()
REST_CO, _ = mesh_world(ME, evaluated=True, tris=False)
_e = np.empty(len(ME.data.edges) * 2, dtype=np.int64)
ME.data.edges.foreach_get("vertices", _e)
EDGES = _e.reshape(-1, 2)
REST_LEN = np.linalg.norm(REST_CO[EDGES[:, 0]] - REST_CO[EDGES[:, 1]], axis=1)
OK_E = REST_LEN > 1e-3 * HEIGHT
OK_IDX = np.nonzero(OK_E)[0]
STRETCH_WHERE = [{}]
_names = {vg.index: vg.name for vg in ME.vertex_groups}
FOOT = {f"foot_{s}" for s in SIDES} | {f"ball_{s}" for s in SIDES}
DOM = np.array([_names[max(v.groups, key=lambda g: g.weight).group] if len(v.groups) else "" for v in ME.data.vertices])
FOOT_MASK = np.array([sum(g.weight for g in v.groups if _names.get(g.group) in FOOT) >= 0.5 for v in ME.data.vertices])
LOOPS = {"idle", "walk", "run"}

# --- tiles ---------------------------------------------------------------------------------------
TILES = Path(job["tiles"]) if job.get("tiles") else None
TW = TH = int(job.get("tile_px", 200))


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
    scene.world = scene.world or bpy.data.worlds.new("w")
    cd = bpy.data.cameras.new("cam")
    cd.type = "ORTHO"
    cam = bpy.data.objects.new("cam", cd)
    scene.collection.objects.link(cam)
    scene.camera = cam
    import bmesh
    mesh = bpy.data.meshes.new("ground")
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


def colour(obj, rgb_of_vertex):
    if "lr" in obj.data.color_attributes:
        obj.data.color_attributes.remove(obj.data.color_attributes["lr"])
    col = obj.data.color_attributes.new("lr", "FLOAT_COLOR", "POINT")
    cs = []
    for v in obj.data.vertices:
        cs.extend((*rgb_of_vertex(v), 1.0))
    col.data.foreach_set("color", cs)
    obj.data.color_attributes.active_color = col


def lr_colour(v):
    w = {"l": 0.0, "r": 0.0, None: 0.0, "x": 0.0}
    for g in v.groups:
        nm = _names[g.group]
        w["x" if nm in EXTRA_OF else nm[-1] if nm[-2:] in ("_l", "_r") else None] += g.weight
    if w["x"] > 0.5:  # extra chains (wings, tails, ...): magenta
        return (0.85, 0.25, 0.8)
    return (0.25, 0.45, 0.95) if w["l"] > max(w["r"], 0.3) else (0.95, 0.55, 0.15) if w["r"] > max(w["l"], 0.3) \
        else (0.78, 0.78, 0.75)


def frame_cam(cam, ground, view, pts):
    ax = 0 if view == "front" else 1
    lo, hi = float(pts[:, ax].min()), float(pts[:, ax].max())
    top = float(pts[:, 2].max())
    size = 1.1 * max(hi - lo, top - GROUND)
    cz = GROUND + 0.46 * size
    ch = (lo + hi) / 2
    if view == "front":
        cam.location = (ch, GROUND + 50 * size, cz)
        cam.rotation_euler = (math.pi / 2, 0, math.pi)
    else:
        cam.location = (50 * size, ch, cz)
        cam.rotation_euler = (math.pi / 2, 0, math.pi / 2)
    cam.data.ortho_scale = size
    cam.data.clip_end = 200 * size
    ground.scale = (20 * size, 20 * size, 0.004 * size)
    ground.location = (0, 0, GROUND - 0.002 * size)


def render(path):
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)


def add_blade():
    import bmesh
    me = bpy.data.meshes.new("blade")
    b = bmesh.new()
    bmesh.ops.create_cube(b, size=1.0)
    bmesh.ops.scale(b, vec=(0.025, 0.9, 0.07), verts=b.verts)
    bmesh.ops.translate(b, vec=(0, 0.45, -0.02), verts=b.verts)
    b.to_mesh(me)
    b.free()
    ob = bpy.data.objects.new("blade", me)
    scene.collection.objects.link(ob)
    colour(ob, lambda v: (0.1, 0.8, 0.2))
    ob.parent, ob.parent_type, ob.parent_bone = RIG, "BONE", "grip_r"
    bpy.context.view_layer.update()
    ob.matrix_world = RIG.matrix_world @ RIG.data.bones["grip_r"].matrix_local
    return ob


def joint_markers():
    """Small spheres at our joints (red) and SkinTokens' mapped joints (green) for the fit tile."""
    import bmesh
    obs = []
    for pts, rgb in (([OUR_POS[n] for n in CORE if n in OUR_POS and not n.startswith("ball_leaf")], (0.9, 0.1, 0.1)),
                     ([SK_POS[_inv[u]] for u in CORE if u in _inv], (0.1, 0.75, 0.2))):
        me = bpy.data.meshes.new("pts")
        b = bmesh.new()
        for p in pts:
            m = Matrix.Translation(p)
            bmesh.ops.create_icosphere(b, subdivisions=1, radius=0.013 * HEIGHT, matrix=m)
        b.to_mesh(me)
        b.free()
        ob = bpy.data.objects.new("pts", me)
        scene.collection.objects.link(ob)
        colour(ob, lambda v, c=rgb: c)
        ob.show_in_front = True
        obs.append(ob)
    return obs


if TILES:
    TILES.mkdir(parents=True, exist_ok=True)
    cam, ground = setup_render()
    colour(ME, lr_colour)
    blade = add_blade()
    # fit tile: x-ray mesh with the joints
    scene.display.shading.show_xray = True
    scene.display.shading.xray_alpha = 0.25
    marks = joint_markers()
    scene.render.resolution_x = scene.render.resolution_y = 3 * TW
    for view in ("front", "side"):
        frame_cam(cam, ground, view, REST_CO)
        render(TILES / f"fit_{view}.png")
    scene.render.resolution_x = scene.render.resolution_y = TW
    for o in marks:
        bpy.data.objects.remove(o)
    scene.display.shading.show_xray = False
    for view in ("front", "side"):
        frame_cam(cam, ground, view, REST_CO)
        render(TILES / f"rest_{view}.png")

metrics = {"id": job.get("id"), "height": round(HEIGHT, 3), "hip_scale": round(HIP, 4), "clips": {}}
def evaluate_mesh(n):
    lows, foot_lows, stretch, pos = [], [], [], []
    STRETCH_WHERE[0] = {}
    for i in range(n + 1):
        scene.frame_set(i + 1)
        co, _ = mesh_world(ME, evaluated=True, tris=False)
        pos.append(co)
        lows.append(float(co[:, 2].min()))
        foot_lows.append(float(co[FOOT_MASK, 2].min()) if FOOT_MASK.any() else lows[-1])
        el = np.linalg.norm(co[EDGES[:, 0]] - co[EDGES[:, 1]], axis=1)
        ratio = el[OK_E] / REST_LEN[OK_E]
        p999 = float(np.percentile(ratio, 99.9))
        stretch.append((float(ratio.max()), p999, int((ratio > 2).sum())))
        if p999 >= max(x[1] for x in stretch):  # where the worst frame tears: the torn edges' dominant groups
            torn = OK_IDX[ratio >= p999]
            g, c = np.unique(DOM[EDGES[torn, 0]], return_counts=True)
            STRETCH_WHERE[0] = {str(a): int(b) for a, b in sorted(zip(g, c), key=lambda x: -x[1])[:5]}
    return lows, foot_lows, stretch, pos


FLOOR_TOL = 0.01
# sample every clip from UAL first, then drop UAL (armature, mannequin, its 43 actions) so the
# exporter sees only our armature and the one action it is writing
SAMPLED = {}
for clip, uc in job["clips"].items():
    t = time.time()
    SAMPLED[clip] = (*sample_clip(uc), round(time.time() - t, 1))
for o in (UM, UA):
    bpy.data.objects.remove(o, do_unlink=True)
for a in list(bpy.data.actions):
    bpy.data.actions.remove(a)
for clip, uc in job["clips"].items():
    frames, n, secs, max_loc, max_root, t_sample = SAMPLED[clip]
    t = time.time() - t_sample
    act = key_frames(clip, frames)
    scene.frame_start, scene.frame_end = 1, n + 1
    # the direct mapping's check: our dir-bone segments point where UAL's do (frame 0)
    scene.frame_set(1)
    dir_err = max(math.degrees(((RIG.pose.bones[f"{b}_{s}"].head - RIG.pose.bones[f"{a}_{s}"].head).normalized())
                               .angle(frames[0]["ual_dirs"][f"{a}_{s}"])) for a, b in CHECK for s in SIDES
                  if f"{a}_{s}" not in DELTA)
    lows, foot_lows, stretch, pos = evaluate_mesh(n)
    raw_min = min(lows) - GROUND
    lift = None
    if job.get("floor", True):
        deficit = [max(0.0, GROUND - FLOOR_TOL * HEIGHT - z) for z in lows]
        if max(deficit) > 0:
            dil = [max(deficit[max(0, i - 3):i + 4]) for i in range(n + 1)]
            lift = [sum(dil[max(0, i - 2):i + 3]) / len(dil[max(0, i - 2):i + 3]) for i in range(n + 1)]
            RIG.animation_data.action = None
            bpy.data.actions.remove(act)
            act = key_frames(clip, frames, lift)
            lows, foot_lows, stretch, pos = evaluate_mesh(n)
    rep = {"ual_clip": uc, "frames": n, "seconds": secs, "loop": clip in LOOPS,
           "min_z": round(min(lows) - GROUND, 4),
           "foot_low_min": round(min(foot_lows) - GROUND, 4), "foot_low_max": round(max(foot_lows) - GROUND, 4),
           "foot_low_min_frac": round((min(foot_lows) - GROUND) / HEIGHT, 4),
           "foot_low_max_frac": round((max(foot_lows) - GROUND) / HEIGHT, 4),
           "edge_stretch_max": round(max(x[0] for x in stretch), 3),
           "edge_stretch_p999": round(max(x[1] for x in stretch), 3),
           "edges_over_2x": max(x[2] for x in stretch), "stretch_where": STRETCH_WHERE[0], "ual_bone_loc_dropped_max": max_loc,
           "ual_root_rot_max_deg": max_root, "dir_err_deg": round(dir_err, 2), "min_z_raw": round(raw_min, 4),
           "floor_lift_max": round(max(lift), 4) if lift else 0.0}
    if clip in LOOPS:
        rep["loop_delta"] = round(float(np.linalg.norm(pos[0] - pos[-1], axis=1).max()), 4)
    if TILES:
        pts = np.concatenate(pos[::3])
        k6 = [round(i * n / 6) for i in range(6)] if clip in LOOPS else [round(i * n / 5) for i in range(6)]
        for view in ("front", "side"):
            frame_cam(cam, ground, view, pts)
            for k, i in enumerate(k6):
                scene.frame_set(i + 1)
                render(TILES / f"{clip}_{view}_{k}.png")
    if job.get("export_anims", True):
        select(RIG)
        bpy.ops.export_scene.gltf(filepath=str(OUT / "anims" / f"{clip}.glb"), export_format="GLB", use_selection=True,
                                  export_yup=True, export_animations=True, export_animation_mode="ACTIONS",
                                  export_anim_slide_to_zero=True, export_force_sampling=True)
        (OUT / "anims" / f"{clip}.json").write_text(json.dumps(rep, indent=1))
    rep["build_seconds"] = round(time.time() - t, 1)
    metrics["clips"][clip] = rep
    print(f"{clip}: {json.dumps(rep)}", flush=True)
    RIG.animation_data.action = None
    bpy.data.actions.remove(act)
    for pb in RIG.pose.bones:
        pb.location, pb.rotation_quaternion = Vector(), Quaternion()

REPORT["seconds"] = round(time.time() - T0, 1)
metrics["rig"] = REPORT
(OUT / "metrics.json").write_text(json.dumps(metrics, indent=1, default=str))
print("done", REPORT["seconds"])

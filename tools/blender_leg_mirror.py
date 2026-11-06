"""Add a leg the SkinTokens roll left out, from its twin (override `rig_fix_legs: "mirror"`; Blender side).

Some rolls have a leg in the mesh but no chain for it: the brass dragon wyrmling's left fore leg
is skinned to a 3-bone stub at the chest, the bulette's left fore leg to the body. The anim stage
then finds 3 of 4 legs and walks on those (blender_clips PARTIAL). With the override the rig stage
(blender_rig.py score and finalize, before the wing and tail chains) takes the legs
rig_topology.find_legs finds (rescue on) and, for a leg with no twin (no leg on the other side of
the mesh's centre plane whose foot is within PAIR_TOL x the height of it front to back), builds the
twin's chain:

- the foot: the vertices on the other side within FOOT_BAND x the height of the floor that no leg
  owns, in pieces (grid cells of CELL x the height); the piece nearest the leg's mirrored foot,
  within FOOT_REACH x the height and of FOOT_MIN vertices or more. None: the leg is not added
  (`skipped`, with the reason: the crocodile's right hind has no foot on the mesh);
- the joints: the leg's chain and toes mirrored about the centre plane, each moved toward that
  foot in proportion to how far down the chain it is (the hip stays, the foot lands on the piece);
- bones `legm_<source bone>` with the mirrored roll, parented to the source chain's parent;
- weights: rig_reweight (`fit`: the joints move into the limb, then the vertices within reach and
  closer to the new chain than to any other bone go to it), reach from the source leg's own
  thickness.

rig/chosen.glb is not changed; `forge.py rig <id> --force` applies it (CPU only, no re-roll).
"""
import numpy as np

PREFIX = "legm_"
PAIR_TOL = 0.15   # x height: legs on opposite sides with feet this close front to back are twins
FOOT_BAND = 0.08  # x height: a foot's vertices lie this close to the floor
CELL = 0.03       # x height: grid cell for the foot pieces
FOOT_MIN = 20     # vertices: a smaller piece is a claw tip or a tail end
FOOT_REACH = 0.35  # x height: the foot is this close (floor plane) to the mirrored foot


def _verts(me) -> np.ndarray:
    co = np.empty(len(me.data.vertices) * 3)
    me.data.vertices.foreach_get("co", co)
    mw = np.array(me.matrix_world)
    return co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]


def _pieces(xy: np.ndarray, cell: float) -> np.ndarray:
    """Connected pieces of points on a grid (8-neighbour cells): a label per point."""
    keys = [tuple(k) for k in np.floor(xy / cell).astype(int)]
    cells = {}
    for i, k in enumerate(keys):
        cells.setdefault(k, []).append(i)
    lab, n = {}, 0
    for k in cells:
        if k in lab:
            continue
        lab[k] = n
        stack = [k]
        while stack:
            c = stack.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    q = (c[0] + dx, c[1] + dy)
                    if q in cells and q not in lab:
                        lab[q] = n
                        stack.append(q)
        n += 1
    return np.array([lab[k] for k in keys])


def add_mirrored_legs(arm, me, rt, rr, feet: int) -> dict:
    """Insert twin chains for unpaired legs until the rig has `feet` legs (rr: rig_reweight).
    Returns the report."""
    import bpy
    from mathutils import Vector

    m = arm.matrix_world
    bones = [{"name": b.name, "parent": b.parent.name if b.parent else None,
              "head": list((m @ b.head_local)[:]), "tail": list((m @ b.tail_local)[:])} for b in arm.data.bones]
    by_name = {b["name"]: b for b in bones}
    kids = rt._children(bones)
    V = _verts(me)
    z0 = float(V[:, 2].min())
    height = float(V[:, 2].max() - z0)
    legs = rt.find_legs(bones, height, feet, rescue=True)
    rep = {"legs_found": len(legs), "need": feet, "added": [], "skipped": []}
    if feet <= 0 or len(legs) >= feet or not legs:
        return rep
    mid = float(np.median(V[:, 0]))  # the mesh's centre plane (wings and a curled tail skew its bounds)
    rep["mid_x"] = round(mid, 4)
    mir = lambda p: np.array([2 * mid - p[0], p[1], p[2]])
    side = lambda g: np.sign(by_name[g["foot"]]["head"][0] - mid)
    fy = lambda g: by_name[g["foot"]]["head"][1]
    unpaired = [g for g in legs if not any(o is not g and side(o) != side(g) and abs(fy(o) - fy(g)) < PAIR_TOL * height
                                           for o in legs)]
    names = {g.index: g.name for g in me.vertex_groups}
    dom = np.array([names[max(v.groups, key=lambda g: g.weight).group] if len(v.groups) else "" for v in me.data.vertices])
    leg_bones = {n for g in legs for n in g["chain"] + rt._descendants(g["foot"], kids)}
    free = ~np.isin(dom, sorted(leg_bones))
    plan, made = [], 0
    for g in unpaired:
        if len(legs) + made >= feet:
            break
        src = g["chain"] + rt._descendants(g["foot"], kids)
        sgn = -side(g)
        low = free & (V[:, 2] < z0 + FOOT_BAND * height) & (np.sign(V[:, 0] - mid) == sgn) \
            & (np.abs(V[:, 0] - mid) > 0.03 * height)
        idx = np.nonzero(low)[0]
        want_xy = mir(by_name[g["foot"]]["head"])[:2]
        best = None
        if len(idx):
            lab = _pieces(V[idx, :2], CELL * height)
            for k in np.unique(lab):
                pts = idx[lab == k]
                if len(pts) < FOOT_MIN:
                    continue
                d = float(np.linalg.norm(V[pts, :2].mean(axis=0) - want_xy))
                if d < FOOT_REACH * height and (best is None or d < best[0]):
                    best = (d, pts)
        if best is None:
            rep["skipped"].append({"source": g["chain"][0], "reason": "no unowned foot on the mesh near the mirrored foot"})
            continue
        foot_xy = V[best[1], :2].mean(axis=0)
        own = np.isin(dom, src)
        if own.any():
            P = V[own]
            d = np.min([rr._seg_dist(P, np.array(by_name[n]["head"]), np.array(by_name[n]["tail"])) for n in src], axis=0)
            reach = float(np.clip(2.0 * np.median(d), 0.04 * height, 0.15 * height))
        else:
            reach = 0.08 * height
        # joints: mirrored, each moved toward the found foot by its share of the way down the chain
        top = mir(by_name[g["chain"][0]]["head"])
        ankle = mir(by_name[g["foot"]]["head"])
        delta = np.array([foot_xy[0] - ankle[0], foot_xy[1] - ankle[1], 0.0])
        span = max(top[2] - ankle[2], 1e-6)

        def place(p, top=top, delta=delta, span=span):
            q = mir(p)
            return q + delta * float(np.clip((top[2] - q[2]) / span, 0.0, 1.0))

        plan.append((g, src, place, reach))
        made += 1
        rep["added"].append({"source": g["chain"], "bones": [PREFIX + n for n in src], "foot_piece_verts": int(len(best[1])),
                             "foot_moved_m": round(float(np.linalg.norm(delta)), 4), "reach_m": round(reach, 4)})
    if not plan:
        return rep
    inv = m.inverted()
    R = np.diag([-1.0, 1.0, 1.0])
    bpy.context.view_layer.objects.active = arm
    arm.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.data.edit_bones
    for g, src, place, _ in plan:
        for n in src:
            zw = np.array((m.to_3x3() @ eb[n].z_axis)[:])
            e = eb.new(PREFIX + n)
            e.head = inv @ Vector(place(by_name[n]["head"]))
            e.tail = inv @ Vector(place(by_name[n]["tail"]))
            e.align_roll(inv.to_3x3() @ Vector(R @ zw))
            e.use_deform = True
        for n in src:
            p = by_name[n]["parent"]
            eb[PREFIX + n].parent = eb[PREFIX + p] if p in src else eb[p] if p else None
            eb[PREFIX + n].use_connect = False
    bpy.ops.object.mode_set(mode="OBJECT")
    for (g, src, _, reach), a in zip(plan, rep["added"]):
        a["weights"] = rr.reweight(arm, me, {"bones": [PREFIX + n for n in src], "fit": True, "reach": reach,
                                             "far": 0.15 * height, "blend": 0.02 * height})
    return rep

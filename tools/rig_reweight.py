"""Geometric re-weight of one bone chain (the `rig_reweight` override; Blender side).

SkinTokens sometimes skins a limb to the wrong bones: the harpy's right arm went to the
wing chain behind it, and its forearm and hand bones (bone_18, bone_19) ended up owning
patches of the right thigh instead (no vertex with weight > 0.5 on the arm). This is
tools/blender_wing_weights.py made generic: a vertex belongs to the chain when it is within
`reach` of the chain's segments and closer to them (distance x `bias`) than to any other
bone; those vertices go to their two nearest chain bones by inverse distance, with a
`blend` band at the crossover that keeps part of the old weights so the join does not
crease. Options:

- strip (default true): the chain's weights on vertices outside that region (a hand bone
  on the thigh) are removed, the rest re-normalised; a vertex left with nothing goes to
  its nearest other bone.
- fit (default false): first move the chain's joints into the limb. Each bone's head and
  tail go to the centroid of the region's vertices in a slab across the bone at that end
  (the harpy's arm bones run along the outside of the arm, so its hand socket floated
  8 cm off the skin). The region is then picked again with the moved bones.
- far (metres, default 0.3): in the region and within 2 x reach of the chain, weights of
  bones farther than this from the vertex are dropped (bleed from across the body: next
  to the harpy's arm, feathers that followed the left wing tore off the fixed arm).
- hand_room (metres, default 0, with fit): the tip bone's tail stops this far short of
  the limb's end, for an arm whose skeleton ends at the forearm: the hand bone
  add_sockets adds there (4% of the height) then spans the fist, grip_r at its middle.

Used by blender_rig.py "finalize" (job key `reweight`, from the override) before the
sockets are placed, so hand_r/grip_r follow fitted bones. rig/chosen.glb is not changed.

    spec: {"bones": [names, root to tip], "bias": 1.0, "reach": 0.15, "blend": 0.03,
           "strip": true, "fit": false, "hand_room": 0.0, "far": 0.3}
"""
import numpy as np
from mathutils import Vector

DEFAULTS = {"bias": 1.0, "reach": 0.15, "blend": 0.03, "strip": True, "fit": False, "hand_room": 0.0, "far": 0.3}


def _verts(me) -> np.ndarray:
    co = np.empty(len(me.data.vertices) * 3)
    me.data.vertices.foreach_get("co", co)
    mw = np.array(me.matrix_world)
    return co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]


def _segs(arm) -> dict:
    m = arm.matrix_world
    return {b.name: (np.array((m @ b.head_local)[:]), np.array((m @ b.tail_local)[:])) for b in arm.data.bones
            if b.use_deform}


def _seg_dist(V, a, b, with_t=False):
    ab = b - a
    t = ((V - a) @ ab) / max(float(ab @ ab), 1e-9)
    tc = np.clip(t, 0.0, 1.0)
    d = np.linalg.norm(V - (a + tc[:, None] * ab), axis=1)
    return (d, t) if with_t else d


def _region(V, segs, chain, sp):
    d_chain = np.stack([_seg_dist(V, *segs[b]) for b in chain], axis=1)
    near = d_chain.min(axis=1)
    others = [_seg_dist(V, *s) for n, s in segs.items() if n not in chain]
    d_other = np.min(others, axis=0) if others else np.full(len(V), np.inf)
    k = np.clip((d_other - sp["bias"] * near) / sp["blend"] + 0.5, 0.0, 1.0)
    k[near > sp["reach"]] = 0.0
    return k, d_chain


def _fit(arm, V, segs, chain, inside, sp) -> dict:
    """Move each chain bone's head and tail to the centroid of the region's vertices in a
    slab across the bone at that end. Returns {bone: [moved_head_m, moved_tail_m]}."""
    import bpy
    moved, new = {}, {}
    P = V[inside]
    for b in chain:
        a, c = segs[b]
        L = float(np.linalg.norm(c - a))
        d, t = _seg_dist(P, a, c, with_t=True)
        ends = []
        for t0 in (0.0, 1.0):
            sel = (np.abs(t - t0) * L < max(0.25 * L, 0.03)) & (d < sp["reach"])
            ends.append(P[sel].mean(axis=0) if sel.sum() >= 8 else (a if t0 == 0 else c))
        if b == chain[-1] and sp["hand_room"] > 0:
            # leave the last hand_room metres of the limb past the tip bone: add_sockets puts a
            # hand bone (4% of the height) at the tail of an arm that ends at the forearm, and
            # grip_r at its middle, which then lands in the fist instead of past it
            h, tl = ends
            u = (tl - h) / max(float(np.linalg.norm(tl - h)), 1e-9)
            far = float(((P[(t > 0.5) & (d < sp["reach"])] - h) @ u).max(initial=0.0))
            ends[1] = h + u * max(far - sp["hand_room"], 0.3 * float(np.linalg.norm(tl - h)))
        new[b] = ends
    inv = arm.matrix_world.inverted()
    bpy.context.view_layer.objects.active = arm
    arm.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.data.edit_bones
    for b, (h, t) in new.items():
        if eb[b].use_connect:
            eb[b].use_connect = False
        moved[b] = [round(float(np.linalg.norm(h - segs[b][0])), 4), round(float(np.linalg.norm(t - segs[b][1])), 4)]
        eb[b].head = inv @ Vector(h)
        eb[b].tail = inv @ Vector(t)
    bpy.ops.object.mode_set(mode="OBJECT")
    return moved


def reweight(arm, me, spec: dict) -> dict:
    sp = {**DEFAULTS, **spec}
    chain = list(sp["bones"])
    V = _verts(me)
    segs = _segs(arm)
    missing = [b for b in chain if b not in segs]
    if missing:
        raise SystemExit(f"rig_reweight: no deform bones {missing}")
    rep = {"bones": chain}
    if sp["fit"]:
        k, _ = _region(V, segs, chain, sp)
        rep["fit_moved_m"] = _fit(arm, V, segs, chain, k > 0.5, sp)
        segs = _segs(arm)
    k, d_chain = _region(V, segs, chain, sp)
    near = d_chain.min(axis=1)
    groups = [me.vertex_groups.get(b) or me.vertex_groups.new(name=b) for b in chain]
    gidx = {g.index for g in groups}
    names = {g.index: g.name for g in me.vertex_groups}
    other_bones = [n for n in segs if n not in chain]
    moved = stripped = orphan = dropped = 0
    d_all = {n: _seg_dist(V, *segs[n]) for n in segs if n not in chain}
    for v in me.data.vertices:
        i = v.index
        if k[i] > 0.0:
            old = [(g.group, g.weight) for g in v.groups]
            # old weights from bones farther than `far` (the harpy's right arm carried the
            # left wing's bone_12/bone_13) are bleed, not something to blend back in
            keep = [(gi, w) for gi, w in old if names[gi] not in d_all or d_all[names[gi]][i] <= sp["far"]]
            tot = sum(w for _, w in keep)
            for gi, w in old:
                me.vertex_groups[gi].remove([i])
            for gi, w in keep:
                me.vertex_groups[gi].add([i], (w / tot if tot > 0 else 0.0) * (1.0 - k[i]), "REPLACE")
            dropped += len(old) - len(keep)
            order = np.argsort(d_chain[i])
            a = order[0]
            b = order[1] if len(chain) > 1 else order[0]
            wa, wb = 1.0 / (d_chain[i, a] + 1e-4), 1.0 / (d_chain[i, b] + 1e-4)
            if a == b:
                groups[a].add([i], float(k[i]), "ADD")
            else:
                groups[a].add([i], float(k[i]) * wa / (wa + wb), "ADD")
                groups[b].add([i], float(k[i]) * wb / (wa + wb), "ADD")
            moved += 1
        else:
            # plain (group, weight) pairs: v.groups goes stale once a group is removed
            mine = [g.group for g in v.groups if g.group in gidx and g.weight > 0] if sp["strip"] else []
            # next to the limb (within 2 x reach), far bones' weights are bleed too: the harpy's
            # right-side feathers by the arm followed the left wing and tore off the fixed arm
            nearby = near[i] < 2 * sp["reach"]
            far_old = [g.group for g in v.groups if g.group not in gidx and names[g.group] in d_all
                       and d_all[names[g.group]][i] > sp["far"]] if nearby else []
            if not mine and not far_old:
                continue
            rest = [(g.group, g.weight) for g in v.groups if g.group not in gidx and g.group not in far_old
                    and g.weight > 1e-4 and names[g.group] in segs]
            if not sp["strip"]:
                rest += [(g.group, g.weight) for g in v.groups if g.group in gidx and g.weight > 1e-4]
            for gi in mine + far_old:
                me.vertex_groups[gi].remove([i])
            stripped += bool(mine)
            dropped += len(far_old)
            if rest:
                tot = sum(w for _, w in rest)
                for gi, w in rest:
                    me.vertex_groups[gi].add([i], w / tot, "REPLACE")
            else:  # nothing left: the nearest bone off the chain (the region test put it there)
                p = V[i:i + 1]
                nb = min(other_bones, key=lambda n: float(_seg_dist(p, *segs[n])[0]))
                (me.vertex_groups.get(nb) or me.vertex_groups.new(name=nb)).add([i], 1.0, "REPLACE")
                orphan += 1
    dom = {b: 0 for b in chain}
    for v in me.data.vertices:
        for g in v.groups:
            if g.group in gidx and g.weight > 0.5:
                dom[names[g.group]] += 1
    rep.update({"moved_verts": moved, "stripped_verts": stripped, "orphans": orphan, "far_weights_dropped": dropped,
                "dominant": dom, **{x: sp[x] for x in ("bias", "reach", "blend", "strip", "fit", "hand_room", "far")}})
    return rep

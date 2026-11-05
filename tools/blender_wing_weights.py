"""Wing chains for a winged rig that has none, and clean geometric wing weights.

SkinTokens gave the harpy no wing bones in 4 of 4 rolls, and when it is handed a wing
chain it spreads the wing's vertices over the forearm, thigh and spine, so the wing tears
on a flap (docs/rig-anim.md). Two parts:

**Detection on the mesh** (`detect_wings`, plain numpy). The body axis comes from the
skeleton (`rig_topology.centre_x`), the shoulder height from the head bone's first
branching ancestor (`shoulder_height`). A wing is a big lobe off the upper body: the
vertices more than MARGIN x the height from the centre line on one side, less the body band
below the shoulders (closer than BAND: arms, hips, a tail fan's root), cut into pieces by
proximity (voxels of VOXEL x the height, 26-neighbours, so loose feather sheets join up).
Per side the piece that reaches farthest is a wing when it reaches WING_REACH x the height
sideways (the same reach find_wings asks of a chain), holds at least MIN_SHARE of the
vertices and touches the body (its inner rim, within RIM of the margin); loose pieces within
ATTACH of it join it for the weights. Its root is the centroid of the inner rim above the
shoulders (where the lobe meets the back), its tip the geodesic far end (over the voxel
graph, from the root) of the lobe's part past OUTER of its reach, so the chain runs out to
the span and down a hanging wing, and the joints between are the centroids of the lobe's
level sets of that distance near the root-tip path: a medial axis, N_BONES bones.

**Weights** (`wing_spec`): each inserted chain is re-weighted with rig_reweight.py like a
`rig_reweight` override entry ({bones, reach, bias, blend, far}, WEIGHTS) plus the lobe's
vertices (`lobe`, `clean`): a vertex within `reach` of the chain and closer to it (x `bias`)
than to any other bone goes to its two nearest wing bones, a lobe vertex already when it is
within 2x (`lobe_bias` 0.5) of the next bone's distance, with no reach limit; a `blend` band
at the crossover keeps part of the old weight; the side's outer vertices lose weights of
bones farther than `far` (SkinTokens' forearm weight on the harpy's feather tips).

**Seam** (`seam_blend`, winged bipeds): where an inserted chain's wing meets the arm on the
same side, ownership between the arm and the wing follows the welded surface, not space
(geodesic distance from the arm's skin vs from the wing's core, a smooth falloff with the
arm skin held), so feathers behind the forearm leave the arm and the arm's skin loses the
wing's weight (the harpy roll_1's sword cut: 172 -> 38 edges past 4x).

**Body seam** (`body_seam`, after the arm-wing seam): the same surface rule between the wing
and the body (centre line + legs) and between the arm and the body, so feathers hanging from
the hips beside the thighs leave the wing tip, wing feathers on the floor leave the ankle, and
the back and waist leave the arm (harpy roll_1: fly 96 -> 4 edges past 4x, sword cut 48 -> 14).

**Adoption** (`_adopt`): a SkinTokens chain short of find_wings' reach that owns ADOPT of a
lobe's vertices is renamed wing_<side>_00.. instead of getting a second chain beside it.

blender_rig.py "score" and "finalize" call `add_missing_wings` when a winged plan's
skeleton has fewer than 2 wing chains (find_wings); the chains are `wing_<side>_00..`, on
the trunk bone nearest the root (docs/rig-anim.md "Wing chains from the mesh").

Stand-alone (the harpy's first hand fix, chains already in the GLB):

    blender -b --factory-startup -P tools/blender_wing_weights.py -- '<json job>'

job: src, dst (GLB), report (json path), wings: [[bone...], ...] (one chain per wing),
bias (default 1.0), reach (metres from the chain, default 0.4), blend (metres, default 0.04).
"""
import heapq
import json
import sys

import numpy as np

MARGIN = 0.15      # x height: a wing lobe's vertices are this far from the centre line or more
VOXEL = 0.02       # x height: proximity voxel for the lobe graph
WING_REACH = 0.4   # x height sideways from the centre line (rig_topology.WING_REACH)
MIN_SHARE = 0.02   # a lobe holds at least this share of the mesh's vertices
RIM = 0.05         # x height past the margin: the lobe's inner rim, where it meets the body
BAND = 0.3         # x height: below the shoulders, vertices this close to the centre line are body
SHOULDER_DROP = 0.05  # x height: the body band starts this far below the shoulder girdle
ATTACH = 0.08     # x height: a loose piece this close to the lobe is part of it (weights only)
ADOPT = 0.5        # an existing chain owning this share of a lobe's vertices is that wing
OUTER = 0.75       # the tip is the geodesic far end of the lobe's part past this share of its reach
N_BONES = 4        # bones per inserted chain, root to tip
PREFIX = "wing_"   # inserted chains: wing_r_00 .. wing_r_03, wing_l_00 ..
# rig_reweight entry for an inserted chain (x height for reach and blend; bias plain)
WEIGHTS = {"reach": 0.3, "bias": 1.0, "blend": 0.1, "far": 0.15, "strip": True, "lobe_bias": 0.5}
# arm-wing seam (`seam_blend`, x height): the arm core is the skin within `core` of the arm's
# joint path, the wing core the vertices the wing chain owns (> half) farther than
# `wing_core` from the arm core over the surface; past `hold` of the arm core the arm's share
# falls off as g_wing / (g_arm - hold + g_wing), geodesic over the welded mesh
SEAM = {"core": 0.04, "hold": 0.03, "wing_core": 0.3}
# body seam (`body_seam`, x height): the body core is the skin on the centre line (within
# `trunk`) and within SEAM["core"] of each leg's joint path; a limb's (wing's, arm's) share of
# its weight + the body's falls from 1 to 0 across `band` around the point where the vertex
# is as far from the limb's core as from the body core, over the welded surface
BODY_SEAM = {"trunk": 0.04, "band": 0.1}
WELD = 1e-5        # metres: glTF splits vertices at UV seams; these are one surface point


def _components(keys: np.ndarray) -> np.ndarray:
    """Connected components of occupied voxels (26-neighbours); keys (n,3) unique ints."""
    index = {tuple(k): i for i, k in enumerate(keys.tolist())}
    label = np.full(len(keys), -1)
    offs = [(a, b, c) for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1) if (a, b, c) != (0, 0, 0)]
    nxt = 0
    for s in range(len(keys)):
        if label[s] >= 0:
            continue
        label[s] = nxt
        stack = [s]
        while stack:
            i = stack.pop()
            x, y, z = keys[i]
            for a, b, c in offs:
                j = index.get((x + a, y + b, z + c))
                if j is not None and label[j] < 0:
                    label[j] = nxt
                    stack.append(j)
        nxt += 1
    return label


def _geodesic(keys: np.ndarray, centres: np.ndarray, seeds: list) -> tuple[np.ndarray, np.ndarray]:
    index = {tuple(k): i for i, k in enumerate(keys.tolist())}
    offs = [(a, b, c) for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1) if (a, b, c) != (0, 0, 0)]
    dist = np.full(len(keys), np.inf)
    pred = np.full(len(keys), -1)
    h = []
    for s in seeds:
        dist[s] = 0.0
        h.append((0.0, s))
    heapq.heapify(h)
    while h:
        d, i = heapq.heappop(h)
        if d > dist[i]:
            continue
        x, y, z = keys[i]
        for a, b, c in offs:
            j = index.get((x + a, y + b, z + c))
            if j is None:
                continue
            nd = d + float(np.linalg.norm(centres[j] - centres[i]))
            if nd < dist[j]:
                dist[j] = nd
                pred[j] = i
                heapq.heappush(h, (nd, j))
    return dist, pred


def detect_wings(V: np.ndarray, cx: float, height: float, sides=("r", "l"), shoulder_z: float | None = None,
                 n_bones: int = N_BONES) -> list[dict]:
    """Wing lobes on the rest mesh V (n,3), facing +Y with +Z up, the right side +X.
    Per side found: {"side", "joints" [root .. tip] (n_bones + 1 points), "reach" (x
    height), "verts" (lobe size), "share", "root", "tip"}; sides without a lobe are left
    out. `cx` is the skeleton's centre line (rig_topology.centre_x), `shoulder_z` the
    height of the shoulder girdle (shoulder_height): below it, the band within BAND x the
    height of the centre line (arms, hips, thighs, a tail fan's root) is not wing, so the
    lobe does not run down an arm or a thigh into a tail fan."""
    z0 = float(V[:, 2].min())
    zc = (shoulder_z if shoulder_z is not None else z0 + 0.5 * height) - SHOULDER_DROP * height
    vox = VOXEL * height
    out = []
    for side in sides:
        sg = 1.0 if side == "r" else -1.0
        s = (V[:, 0] - cx) * sg
        sel = np.nonzero((s > MARGIN * height) & ~((s < BAND * height) & (V[:, 2] < zc)))[0]
        if len(sel) < MIN_SHARE * len(V):
            continue
        P = V[sel]
        key = np.floor(P / vox).astype(np.int64)
        keys, inv = np.unique(key, axis=0, return_inverse=True)
        inv = inv.reshape(-1)
        label = _components(keys)
        vlabel = label[inv]
        best = None
        for lab in np.unique(label):
            m = vlabel == lab
            reach = float(s[sel][m].max()) / height
            rim = m & (s[sel] < (MARGIN + RIM) * height)
            if reach < WING_REACH or m.sum() < MIN_SHARE * len(V) or rim.sum() < 5:
                continue
            if best is None or reach > best[1]:
                best = (lab, reach, m, rim)
        if best is None:
            continue
        lab, reach, m, rim = best
        # loose pieces next to the lobe (feather tips hanging past it) belong to it too, for
        # the weights only: SkinTokens had hung the harpy's on the forearm
        extra = np.zeros(len(sel), dtype=bool)
        L = P[m][:: max(1, int(m.sum()) // 3000)]
        for other in np.unique(label):
            om = vlabel == other
            if other == lab or om.sum() > 0.5 * m.sum():
                continue
            Po = P[om]
            dmin = min(float(np.sqrt(((L - p) ** 2).sum(axis=1)).min()) for p in Po[:: max(1, len(Po) // 200)])
            if dmin < ATTACH * height:
                extra |= om
        Q, sq = P[m], s[sel][m]
        # root: the inner rim above the shoulder girdle (its upper quarter without one),
        # where the lobe meets the back
        R = P[rim]
        z_lo = zc + SHOULDER_DROP * height if shoulder_z is not None else np.percentile(R[:, 2], 75)
        hi = R[:, 2] >= z_lo
        if hi.sum() < 5:
            hi = R[:, 2] >= np.percentile(R[:, 2], 75)
        root = R[hi].mean(axis=0)
        # geodesic over the lobe's voxels from the voxels of that upper rim
        vk = np.nonzero(label == lab)[0]
        sub = keys[vk]
        remap = -np.ones(len(keys), dtype=np.int64)
        remap[vk] = np.arange(len(vk))
        vi = remap[inv[m]]  # lobe vertex -> sub voxel
        cent = np.zeros((len(vk), 3))
        np.add.at(cent, vi, Q)
        cent /= np.bincount(vi, minlength=len(vk))[:, None]
        seeds = np.unique(remap[inv[rim][hi]]).tolist()
        dist, pred = _geodesic(sub, cent, seeds)
        ok = np.isfinite(dist)
        # tip: the geodesic far end of the lobe's outer part (past OUTER x its reach), so the
        # chain runs out to the wing's span and down a hanging wing, not down the body side
        vs = np.zeros(len(vk))
        np.maximum.at(vs, vi, sq)
        outer = ok & (vs >= OUTER * reach * height)
        if not outer.any():
            outer = ok
        dmax = float(dist[outer].max())
        far = outer & (dist >= 0.95 * dmax)
        tip_v = int(np.argmax(np.where(outer, dist, -1)))
        gmax = dmax
        path = [tip_v]
        while pred[path[-1]] >= 0:
            path.append(int(pred[path[-1]]))
        path = path[::-1]
        dv = dist[vi]
        joints = [root]
        for k in range(1, n_bones):
            f = k / n_bones * gmax
            p = min(path, key=lambda i: abs(dist[i] - f))
            band = (np.abs(dv - f) < 0.5 * gmax / n_bones) & (np.linalg.norm(Q - cent[p], axis=1) < 0.12 * height)
            joints.append(Q[band].mean(axis=0) if band.sum() >= 5 else cent[p])
        tip = cent[far].mean(axis=0)
        joints.append(tip)
        out.append({"side": side, "_verts": sel[m | extra], "_clean": sel, "loose_verts": int(extra.sum()), "joints": [[round(float(c), 4) for c in j] for j in joints],
                    "reach": round(reach, 3), "verts": int(m.sum()), "share": round(float(m.sum()) / len(V), 3),
                    "geodesic_m": round(gmax, 3), "root": [round(float(c), 4) for c in root],
                    "tip": [round(float(c), 4) for c in tip],
                    "root_z": round(float(root[2] - z0) / height, 3), "tip_z": round(float(tip[2] - z0) / height, 3)})
    return out


def shoulder_height(bones: list[dict], head: str | None) -> float | None:
    """Height of the shoulder girdle: the first ancestor of the head bone that branches
    (the harpy's chest bone carries the neck and both arms, the eagle's the neck and the
    wings). None without a head or a branch."""
    by_name = {b["name"]: b for b in bones}
    kids = {}
    for b in bones:
        kids.setdefault(b["parent"], []).append(b["name"])
    p = by_name[head]["parent"] if head in by_name else None
    while p:
        if len(kids.get(p, [])) >= 2:
            return float(by_name[p]["head"][2])
        p = by_name[p]["parent"]
    return None


def wing_spec(bones: list[str], height: float) -> dict:
    """The rig_reweight entry for an inserted chain (blender_rig.py applies it)."""
    return {"bones": bones, "reach": round(WEIGHTS["reach"] * height, 4), "bias": WEIGHTS["bias"],
            "blend": round(WEIGHTS["blend"] * height, 4), "far": round(WEIGHTS["far"] * height, 4),
            "strip": WEIGHTS["strip"], "lobe_bias": WEIGHTS["lobe_bias"]}


def _adopt(arm, me, bones: list[dict], lobes: list[dict], legs, arms, head, rig_topology) -> dict:
    """Per lobe, the existing chain (off a branch, not a leg, an arm, the head or the trunk
    above them) whose bones own at least ADOPT of the lobe's vertices (max weight). Its
    longest path is renamed wing_<side>_00.. (vertex groups follow the bones), so
    find_wings takes it by name. Returns {side: {"bones": new names, "was": old, "share"}}."""
    kids = rig_topology._children(bones)
    by_name = {b["name"]: b for b in bones}
    cx = rig_topology.centre_x(bones, legs)
    height = float(max(max(b["head"][2], b["tail"][2]) for b in bones) - min(min(b["head"][2], b["tail"][2]) for b in bones))
    # a "head" off the centre line is a wing bone find_snout took (eagle roll_1's bone_17)
    central = head and abs((by_name[head]["head"][0] + by_name[head]["tail"][0]) / 2 - cx) < \
        rig_topology.SNOUT_SIDE * height
    limbs = {n for g in legs for n in g["chain"]} | {n for a in arms for n in a["chain"]} | ({head} if central else set())
    names = {g.index: g.name for g in me.vertex_groups}
    dom = np.array([names[max(v.groups, key=lambda g: g.weight).group] if len(v.groups) else "" for v in me.data.vertices])
    out = {}
    for lobe in lobes:
        own = dom[lobe["_verts"]]
        sg = 1.0 if lobe["side"] == "r" else -1.0
        best = None
        for b in bones:
            n = b["name"]
            if not b["parent"] or len(kids[b["parent"]]) < 2 or n in limbs:
                continue
            sub = {n, *rig_topology._descendants(n, kids)}
            if sub & limbs or len(rig_topology.longest_chain(n, kids)) < 2:
                continue
            # one-sided: the trunk above both wings (its subtree crosses the centre line) is not a wing
            if any((by_name[c]["head"][0] + by_name[c]["tail"][0] - 2 * cx) * sg < -0.05 * height for c in sub):
                continue
            share = float(np.isin(own, list(sub)).mean())
            if share >= ADOPT and (best is None or (share, -len(sub)) > (best[1], -best[2])):
                best = (n, share, len(sub))
        if best is None:
            continue
        chain = rig_topology.longest_chain(best[0], kids)
        new = [f"{PREFIX}{lobe['side']}_{i:02d}" for i in range(len(chain))]
        for old, nn in zip(chain, new):
            arm.data.bones[old].name = nn
        out[lobe["side"]] = {"bones": new, "was": chain, "share": round(best[1], 3),
                             "reach": round(max(abs(by_name[c]["tail"][0] - by_name[chain[0]]["head"][0])
                                                for c in chain), 3)}
    return out


def _surface(V: np.ndarray, E: np.ndarray):
    """Welded surface graph: (vertex -> welded id, adjacency [(neighbour ids, lengths)])."""
    _, wid = np.unique(np.round(V / WELD).astype(np.int64), axis=0, return_inverse=True)
    wid = wid.reshape(-1)
    n = int(wid.max()) + 1
    P = np.zeros((n, 3))
    P[wid] = V
    e = wid[E]
    e = e[e[:, 0] != e[:, 1]]
    e = np.unique(np.sort(e, axis=1), axis=0)
    L = np.linalg.norm(P[e[:, 0]] - P[e[:, 1]], axis=1)
    a, b, w = np.r_[e[:, 0], e[:, 1]], np.r_[e[:, 1], e[:, 0]], np.r_[L, L]
    o = np.argsort(a, kind="stable")
    a, b, w = a[o], b[o], w[o]
    starts = np.searchsorted(a, np.arange(n + 1))
    adj = [(b[starts[i]:starts[i + 1]].tolist(), w[starts[i]:starts[i + 1]].tolist()) for i in range(n)]
    return wid, adj


def _surface_dist(wid: np.ndarray, adj: list, seeds: np.ndarray) -> np.ndarray:
    """Geodesic distance (over the welded graph) from the seed vertices, per vertex; inf where unreachable."""
    dist = np.full(len(adj), np.inf)
    h = [(0.0, int(s)) for s in np.unique(wid[seeds])]
    for _, s in h:
        dist[s] = 0.0
    heapq.heapify(h)
    while h:
        d, i = heapq.heappop(h)
        if d > dist[i]:
            continue
        nb, ln = adj[i]
        for j, l in zip(nb, ln):
            nd = d + l
            if nd < dist[j]:
                dist[j] = nd
                heapq.heappush(h, (nd, j))
    return dist[wid]


def _seg(V: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ab = b - a
    t = np.clip(((V - a) @ ab) / max(float(ab @ ab), 1e-12), 0.0, 1.0)
    return np.linalg.norm(V - (a + t[:, None] * ab), axis=1)


def seam_blend(arm, me, arms: list[dict], chains: dict, height: float, rig_topology) -> dict:
    """The arm-wing seam, per side with an inserted wing chain and an arm (upright plans).

    SkinTokens skins by proximity, and on a winged biped the wing membrane behind the arm is
    close to the forearm in space but far from it over the surface (the harpy roll_1's
    membrane 0.23 m behind the forearm is 0.93 m away around the shoulder), so feathers
    carried forearm weight and tore off the wing when the sword cut raised the hand (and the
    wing chain's euclidean region put wing weight on the arm's skin). Here ownership follows
    the surface: g_arm is the geodesic distance from the arm core (the skin within
    SEAM["core"] of the arm's joint path shoulder -> elbow -> wrist -> hand tip, not of its
    bones, which may run off the limb: roll_1's upper arm bone lies along the wing's leading
    edge), g_wing from the wing core (vertices the wing chain owns, farther than
    SEAM["wing_core"] from the arm core). The arm's share of a vertex's arm + wing weight is
    a = 1 within SEAM["hold"] of the arm core, else g_wing / (g_arm - hold + g_wing), a
    smooth falloff across the boundary, and only ever moves one way:

    - arm -> wing where the arm holds more than a (a vertex with wing weight, or an arm-only
      vertex nearer the wing than the arm over the surface),
    - wing -> arm where the wing holds more than 1 - a (a vertex with arm weight, or the arm
      skin itself).

    Pure wing vertices never gain arm weight, the body's other bones keep their share, and
    lobe vertices stay on the wing. Returns {side: counts} for the report."""
    m = arm.matrix_world
    segs = {b.name: (np.array((m @ b.head_local)[:]), np.array((m @ b.tail_local)[:])) for b in arm.data.bones}
    kids = {b.name: [c.name for c in b.children] for b in arm.data.bones}
    V = np.empty(len(me.data.vertices) * 3)
    me.data.vertices.foreach_get("co", V)
    mw = np.array(me.matrix_world)
    V = V.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]
    E = np.empty(len(me.data.edges) * 2, dtype=np.int64)
    me.data.edges.foreach_get("vertices", E)
    wid, adj = _surface(V, E.reshape(-1, 2))
    names = {g.index: g.name for g in me.vertex_groups}
    out = {}
    for a_ in arms:
        side = a_["side"]
        if side not in chains:
            continue
        wing = list(chains[side]["bones"])
        up, fo, hd = a_["upper"], a_["forearm"], a_.get("hand")
        arm_b = [n for n in [up, *rig_topology._descendants(up, kids)] if n in segs and n not in wing]
        pts = [segs[up][0], segs[fo][0], segs[hd][0] if hd else segs[fo][1]] + ([segs[hd][1]] if hd else [])
        core = np.min([_seg(V, p, q) for p, q in zip(pts, pts[1:])], axis=0) < SEAM["core"] * height
        cols = {n: i for i, n in enumerate(arm_b + wing)}
        Wt = np.zeros((len(V), len(cols)))
        tot = np.zeros(len(V))
        for v in me.data.vertices:
            for g in v.groups:
                tot[v.index] += g.weight
                j = cols.get(names[g.group])
                if j is not None:
                    Wt[v.index, j] = g.weight
        na = len(arm_b)
        A, Wg = Wt[:, :na].sum(1), Wt[:, na:].sum(1)
        if not core.any():
            continue
        ga = _surface_dist(wid, adj, np.nonzero(core)[0])
        wcore = (ga > SEAM["wing_core"] * height) & (Wg > 0.5 * np.maximum(tot, 1e-9))
        if not wcore.any():
            out[side] = {"arm": arm_b, "skipped": "no wing core"}
            continue
        gw = _surface_dist(wid, adj, np.nonzero(wcore)[0])
        ga2 = np.maximum(ga - SEAM["hold"] * height, 0.0)
        ok = np.isfinite(ga) | np.isfinite(gw)
        with np.errstate(invalid="ignore", divide="ignore"):
            share = np.where(ga2 <= 0, 1.0, np.where(np.isfinite(gw), gw / np.maximum(ga2 + gw, 1e-12), 1.0))
        T = A + Wg
        to_wing = ok & (A > share * T + 1e-6) & ((Wg > 0) | (gw < ga2))
        to_arm = ok & ~to_wing & (Wg > (1 - share) * T + 1e-6) & ((A > 0) | (ga2 <= 0))
        dw = np.stack([_seg(V, *segs[n]) for n in wing], axis=1)
        da = np.stack([_seg(V, *segs[n]) for n in arm_b], axis=1)
        groups = [me.vertex_groups.get(n) or me.vertex_groups.new(name=n) for n in arm_b + wing]
        for i in np.nonzero(to_wing | to_arm)[0]:
            aw, ww = Wt[i, :na].copy(), Wt[i, na:].copy()
            if aw.sum() <= 0:
                aw[np.argmin(da[i])] = 1.0
            if ww.sum() <= 0:  # the two nearest wing bones by inverse distance
                o = np.argsort(dw[i])[:2]
                inv = 1.0 / (dw[i, o] + 1e-4)
                ww[o] = inv / inv.sum()
            new = np.r_[share[i] * T[i] * aw / aw.sum(), (1 - share[i]) * T[i] * ww / ww.sum()]
            for g, w in zip(groups, new):
                if w > 1e-5:
                    g.add([int(i)], float(w), "REPLACE")
                else:
                    g.remove([int(i)])
        out[side] = {"arm": arm_b, "arm_core_verts": int(core.sum()), "wing_core_verts": int(wcore.sum()),
                     "arm_to_wing": int(to_wing.sum()), "wing_to_arm": int(to_arm.sum()),
                     **{k: round(v * height, 4) for k, v in SEAM.items()}}
    return out


def _weights(me, cols: list[str]) -> np.ndarray:
    """(n verts, len(cols)) weight matrix of the named vertex groups."""
    ci = {n: j for j, n in enumerate(cols)}
    names = {g.index: g.name for g in me.vertex_groups}
    W = np.zeros((len(me.data.vertices), len(cols)))
    for v in me.data.vertices:
        for g in v.groups:
            j = ci.get(names[g.group])
            if j is not None:
                W[v.index, j] = g.weight
    return W


def body_seam(arm, me, arms: list[dict], legs: list[dict], chains: dict, lobes: dict, cx: float, height: float,
              W0: dict) -> dict:
    """Wing vs body and arm vs body over the welded surface (winged bipeds, after seam_blend).

    SkinTokens skins by proximity and the inserted chain's region is euclidean, so on the
    harpy roll_1 the long feathers that hang from the hips beside the thighs (0.5-0.9 m from
    a leg over the surface, 1.0-1.4 m from the wing) got the wing tip's weight where they came
    within its reach and kept the thigh/hip's next to it: 77 of fly's 96 edges past 4x were
    wing_*_03 against bone_0/14/18/20 when the wing swung out. A wing feather lying on the
    floor kept the ankle's weight (bone_20 1.0, 2.2 m from the leg over the surface). In the
    sword cut the back (on the centre line) held 0.5-0.8 of the right upper arm and the waist
    0.33 of the forearm that hangs beside it, so the raised arm pulled a sheet off the torso.

    Body core: the skin within BODY_SEAM["trunk"] of the centre line (front and back, head to
    tail) and within SEAM["core"] of each leg's joint path; g_body the geodesic distance from
    it. Per side with an inserted chain:

    - wing: g_wing from the lobe's vertices the chain owns farther than SEAM["wing_core"] from
      the body core. The body's share of a vertex's wing + body weight is 1 within
      SEAM["hold"] of the body core, else falls from 1 to 0 across BODY_SEAM["band"] around
      g_body = g_wing. Wing weight past that share goes to the vertex's body bones (its
      pre-wing weights when it has none left); body weight past it goes to the wing on
      vertices that hold wing weight, or on the side's outer vertices nearer the wing.
    - arm (that side's): g_arm from the arm core (seam_blend's); the arm's share of its
      weight + the body's falls from 1 to 0 across the band around g_arm - hold = g_body. Arm
      weight past it goes to the body; the body never gives weight to the arm.

    Arm vs wing stays seam_blend's; other bones (the other wing) keep theirs. `lobes`
    {side: {"verts", "outer"}} vertex indices, `W0` {bone: weights} before the wing chains."""
    m = arm.matrix_world
    segs = {b.name: (np.array((m @ b.head_local)[:]), np.array((m @ b.tail_local)[:])) for b in arm.data.bones
            if b.use_deform}
    kids = {b.name: [c.name for c in b.children] for b in arm.data.bones}
    cols = list(segs)
    W = _weights(me, cols)
    orig = W.copy()
    P0 = np.stack([W0.get(n, np.zeros(len(W))) for n in cols], axis=1)
    V = np.empty(len(me.data.vertices) * 3)
    me.data.vertices.foreach_get("co", V)
    mw = np.array(me.matrix_world)
    V = V.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]
    E = np.empty(len(me.data.edges) * 2, dtype=np.int64)
    me.data.edges.foreach_get("vertices", E)
    wid, adj = _surface(V, E.reshape(-1, 2))
    h, band, hold = height, BODY_SEAM["band"] * height, SEAM["hold"] * height

    def path(pts):
        return np.min([_seg(V, p, q) for p, q in zip(pts, pts[1:])], axis=0)

    core = np.abs(V[:, 0] - cx) < BODY_SEAM["trunk"] * h
    for g in legs:
        ch = [n for n in g["chain"] if n in segs]
        if ch:
            core |= path([segs[n][0] for n in ch] + [segs[ch[-1]][1]]) < SEAM["core"] * h
    gb = _surface_dist(wid, adj, np.nonzero(core)[0])
    wing_all = {n for c in chains.values() for n in c["bones"]}
    arm_of = {}
    for a_ in arms:
        up = a_["upper"]
        arm_of[a_["side"]] = [n for n in [up, *_desc(up, kids)] if n in segs and n not in wing_all]

    def give(i, frm, to, share_to, prior):
        """Move vertex i's weight on columns `frm` + `to` so `to` holds share_to of it,
        spread over `to` like its current weights there, else like `prior`, else the
        nearest `to` bone."""
        T = W[i, frm].sum() + W[i, to].sum()
        tv = W[i, to].copy()
        if tv.sum() <= 0:
            tv = prior[i, to].copy() if prior is not None else tv
        if tv.sum() <= 0:
            tv[int(np.argmin([float(_seg(V[i:i + 1], *segs[cols[j]])[0]) for j in to]))] = 1.0
        fv = W[i, frm].copy()
        if fv.sum() <= 0:
            fv[:] = 1.0
        W[i, to] = share_to * T * tv / tv.sum()
        W[i, frm] = (1 - share_to) * T * fv / fv.sum()

    out = {"body_core_verts": int(core.sum()), **{k: round(v * h, 4) for k, v in BODY_SEAM.items()}}
    for side, c in chains.items():
        sg = 1.0 if side == "r" else -1.0
        iw = [cols.index(n) for n in c["bones"] if n in segs]
        ia = [cols.index(n) for n in arm_of.get(side, [])]
        ib = [j for j, n in enumerate(cols) if n not in wing_all and j not in ia]
        lob = np.zeros(len(V), dtype=bool)
        outer = np.zeros(len(V), dtype=bool)
        lob[lobes[side]["verts"]] = True
        outer[lobes[side]["outer"]] = True
        Ww, Bw, tot = W[:, iw].sum(1), W[:, ib].sum(1), W.sum(1)
        wcore = lob & (Ww > 0.5 * np.maximum(tot, 1e-9)) & (gb > SEAM["wing_core"] * h)
        rs = out[side] = {"wing_core_verts": int(wcore.sum())}
        if wcore.any():
            gw = _surface_dist(wid, adj, np.nonzero(wcore)[0])
            ok = np.isfinite(gb) & np.isfinite(gw)
            with np.errstate(invalid="ignore"):
                b = np.clip(0.5 - (gb - gw) / band, 0.0, 1.0)
            b[gb <= hold] = 1.0
            T = Ww + Bw
            to_b = ok & (Ww > (1 - b) * T + 1e-6)
            to_w = ok & ~to_b & (Bw > b * T + 1e-6) & ((Ww > 0) | ((gw < gb) & outer)) & ((V[:, 0] - cx) * sg > 0)
            for i in np.nonzero(to_b | to_w)[0]:
                give(i, iw, ib, b[i], P0)
            rs.update({"wing_to_body": int(to_b.sum()), "body_to_wing": int(to_w.sum())})
        if ia:
            a_ = next(x for x in arms if x["side"] == side)
            up, fo, hd = a_["upper"], a_["forearm"], a_.get("hand")
            pts = [segs[up][0], segs[fo][0], segs[hd][0] if hd else segs[fo][1]] + ([segs[hd][1]] if hd else [])
            acore = path(pts) < SEAM["core"] * h
            ga = _surface_dist(wid, adj, np.nonzero(acore)[0])
            with np.errstate(invalid="ignore"):
                s = np.clip(0.5 - (ga - hold - gb) / band, 0.0, 1.0)
            s[ga <= hold] = 1.0
            A, Bw = W[:, ia].sum(1), W[:, ib].sum(1)
            mv = np.isfinite(gb) & (A > s * (A + Bw) + 1e-6)
            for i in np.nonzero(mv)[0]:
                give(i, ia, ib, 1 - s[i], P0)
            rs["arm_to_body"] = int(mv.sum())
    changed = np.nonzero(np.abs(W - orig).max(axis=1) > 1e-6)[0]
    groups = [me.vertex_groups.get(n) or me.vertex_groups.new(name=n) for n in cols]
    for i in changed:
        for j in np.nonzero((W[i] > 1e-5) | (orig[i] > 0))[0]:
            if W[i, j] > 1e-5:
                groups[j].add([int(i)], float(W[i, j]), "REPLACE")
            else:
                groups[j].remove([int(i)])
    out["changed_verts"] = int(len(changed))
    return out


def _desc(n: str, kids: dict) -> list[str]:
    out = []
    for c in kids.get(n, []):
        out += [c, *_desc(c, kids)]
    return out


def add_missing_wings(arm, me, rig_topology, rig_reweight, feet: int, upright: bool) -> dict | None:
    """In Blender: when the skeleton has fewer than 2 wing chains (find_wings, with the
    plan's foot count as the clips find legs), detect the missing sides' lobes on the mesh,
    insert wing_<side>_00.. chains on the nearest spine bone and weight them. Returns the
    report (None when nothing was missing)."""
    import bpy
    from mathutils import Vector

    m = arm.matrix_world
    bones = [{"name": b.name, "parent": b.parent.name if b.parent else None,
              "head": list((m @ b.head_local)[:]), "tail": list((m @ b.tail_local)[:])} for b in arm.data.bones]
    co = np.empty(len(me.data.vertices) * 3)
    me.data.vertices.foreach_get("co", co)
    mw = np.array(me.matrix_world)
    V = co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]
    height = float(V[:, 2].max() - V[:, 2].min())
    legs = rig_topology.find_legs(bones, height, max(feet, 1))
    arms = rig_topology.find_arms(bones, height, legs) if upright else []
    head = rig_topology.find_head(bones, height, legs) if upright else rig_topology.find_snout(bones, height, legs)
    have = rig_topology.find_wings(bones, height, legs, arms, exclude=(head,) if head else ())
    if len(have) >= 2:
        return None
    cx = rig_topology.centre_x(bones, legs)
    missing = [s for s in ("r", "l") if s not in {w["side"] for w in have}]
    zs = shoulder_height(bones, head)
    lobes = detect_wings(V, cx, height, missing, zs)
    rep = {"had": {w["side"]: w["chain"] for w in have}, "missing": missing, "centre_x": round(cx, 4),
           "shoulder_z": None if zs is None else round(zs, 3), "height_m": round(height, 3), "lobes": lobes,
           "chains": {}, "weights": []}
    if not lobes:
        return rep
    rep["lobes"] = lobes  # the "_verts" index arrays are popped once the weights are done
    # a chain SkinTokens already skinned the lobe to, that falls short of find_wings' reach
    # (the eagle's roll_1: 0.39 x the height), is the wing: it is renamed, not doubled
    adopted = _adopt(arm, me, bones, lobes, legs, arms, head, rig_topology)
    if adopted:
        rep["adopted"] = adopted
        for lobe in [lb for lb in lobes if lb["side"] in adopted]:
            lobe.pop("_verts")
            lobe.pop("_clean")
        lobes = [lb for lb in lobes if lb["side"] not in adopted]
    # parent: the spine bone nearest the root (the trunk and neck, not a limb)
    sp = rig_topology.find_spine(bones, height, legs, arms, head, upright)
    trunk = list(dict.fromkeys((sp["torso"] or []) + (sp["neck"] or []))) or \
        [b["name"] for b in bones if abs((b["head"][0] + b["tail"][0]) / 2 - cx) < 0.06 * height]
    trunk = [n for n in trunk if n != head] or trunk
    by_name = {b["name"]: b for b in bones}

    def seg_d(p, n):
        a, b = np.array(by_name[n]["head"]), np.array(by_name[n]["tail"])
        ab = b - a
        t = np.clip(((p - a) @ ab) / max(float(ab @ ab), 1e-9), 0, 1)
        return float(np.linalg.norm(p - (a + t * ab)))

    inv = arm.matrix_world.inverted()
    bpy.context.view_layer.objects.active = arm
    arm.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.data.edit_bones
    for lobe in lobes:
        pts = [np.array(p) for p in lobe["joints"]]
        parent = min(trunk, key=lambda n: seg_d(pts[0], n))
        names = [f"{PREFIX}{lobe['side']}_{i:02d}" for i in range(len(pts) - 1)]
        prev = eb[parent]
        for n, a, b in zip(names, pts, pts[1:]):
            e = eb.new(n)
            e.head = inv @ Vector(a)
            e.tail = inv @ Vector(b)
            e.parent = prev
            e.use_connect = prev.name != parent
            e.use_deform = True
            prev = e
        rep["chains"][lobe["side"]] = {"bones": names, "parent": parent}
    bpy.ops.object.mode_set(mode="OBJECT")
    # the weights before the chains: a vertex body_seam hands back to the body gets these
    pre = [b.name for b in arm.data.bones if b.use_deform]
    W0 = dict(zip(pre, _weights(me, pre).T))
    sides = {}
    for lobe in lobes:
        c = rep["chains"][lobe["side"]]
        sides[lobe["side"]] = {"verts": lobe["_verts"], "outer": lobe["_clean"]}
        rep["weights"].append(rig_reweight.reweight(arm, me, {**wing_spec(c["bones"], height), "lobe": lobe.pop("_verts"),
                                                            "clean": lobe.pop("_clean")}))
    if arms and rep["chains"]:  # the arm-wing seam over the surface (winged bipeds)
        rep["seam"] = seam_blend(arm, me, arms, rep["chains"], height, rig_topology)
        # then wing vs body and arm vs body over the surface (hip feathers, the back, the waist)
        rep["body_seam"] = body_seam(arm, me, arms, legs, rep["chains"], sides, cx, height, W0)
    return rep


def _cli() -> None:
    import bpy

    job = json.loads(sys.argv[sys.argv.index("--") + 1])
    blend = job.get("blend", 0.04)
    bias = job.get("bias", 1.0)
    reach = job.get("reach", 0.4)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=job["src"])
    arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    me = max((o for o in bpy.data.objects if o.type == "MESH"), key=lambda o: len(o.data.vertices))
    for o in list(bpy.data.objects):  # glTF bone display shapes
        if o.type == "MESH" and o is not me:
            bpy.data.objects.remove(o)

    V = np.array([(me.matrix_world @ v.co)[:] for v in me.data.vertices])
    seg = {b.name: (np.array((arm.matrix_world @ b.head_local)[:]), np.array((arm.matrix_world @ b.tail_local)[:]))
           for b in arm.data.bones}

    def seg_dist(name: str) -> np.ndarray:
        a, b = seg[name]
        ab = b - a
        t = np.clip(((V - a) @ ab) / max(float(ab @ ab), 1e-9), 0.0, 1.0)
        return np.linalg.norm(V - (a + t[:, None] * ab), axis=1)

    dist = {b: seg_dist(b) for b in seg}
    report = {"wings": []}
    for wing in job["wings"]:
        d_wing = np.stack([dist[b] for b in wing], axis=1)
        near = d_wing.min(axis=1)
        d_other = np.min([d for b, d in dist.items() if b not in wing], axis=0)
        groups = [me.vertex_groups.get(b) or me.vertex_groups.new(name=b) for b in wing]
        moved = 0
        for v in me.data.vertices:
            i = v.index
            # k = 1 inside the wing, 0 on the body, ramps across `blend` around the crossover
            k = float(np.clip((d_other[i] - bias * near[i]) / blend + 0.5, 0.0, 1.0))
            if k == 0.0 or near[i] > reach:
                continue
            for g in list(v.groups):
                me.vertex_groups[g.group].add([i], g.weight * (1.0 - k), "REPLACE")
            a, b = np.argsort(d_wing[i])[:2]
            wa, wb = 1.0 / (d_wing[i, a] + 1e-4), 1.0 / (d_wing[i, b] + 1e-4)
            groups[a].add([i], k * wa / (wa + wb), "ADD")
            groups[b].add([i], k * wb / (wa + wb), "ADD")
            moved += 1
        report["wings"].append({"bones": wing, "moved_verts": moved})

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.export_scene.gltf(filepath=job["dst"], export_format="GLB", use_selection=True, export_yup=True)
    with open(job["report"], "w") as f:
        json.dump(report, f, indent=1)


if __name__ == "__main__":
    _cli()

"""Tail chains for a tailed rig that has none (or one too short for the clips), found on the mesh.

No SkinTokens roll of the centaur (12 rolls) had a tail chain: its tail hangs to the ground
behind the rump and was skinned to a hind leg, so it kicked like a hoof in the walk. The wolf
has no tail chain (its tail is skinned to a one-bone rump stub), the owlbear's is 3 bones, and
the clips sway a tail of 4+ (rig_topology.find_tail). Like the wing chains
(blender_wing_weights.py), the rig stage finds the tail on the rest mesh and inserts a chain:

**Detection** (`detect_tail`, plain numpy). The body axis is the skeleton's centre line
(rig_topology.centre_x), the hips the hind legs' first joints (mean; the hind legs are the
legs behind the feet's mean). The tail is read off the body's rear profile, the way it shows in
a side view: the vertices behind the hips and within SIDE x the height of the centre line are
cut into horizontal bands (BAND x the height) from TOP x the height above the hips down to the
floor, and per band the rear cluster is the vertices within REAR of the band's rearmost point.

- root: the first band from the top whose rear cluster lies REAR_MIN x the height behind the
  hips (where the tail leaves the rump), no lower than TOP below the hips;
- the tail runs down band by band while the rear cluster stays REAR_MIN behind the hips, is
  one piece across (no gap wider than GAP in x: two hind legs side by side are not a tail),
  and does not jump forward by more than JUMP from the band above (the rear profile leaving a
  tail that ends above the floor and landing on the hind legs' backs); a tail may reach the
  floor (the centaur's does) but the legs, under the hips, are never behind them;
- at least MIN_BANDS bands, and thin: the rear clusters' median width at most THIN x the
  body's width at the hips;
- the tip is the last band's vertex farthest from the root; the joints are the band
  clusters' centroids, root to tip, resampled to equal arc lengths: 4-6 bones (one per
  BONE_LEN x the height), `tail_00 ..`, parented to the hip bone (rig_topology.find_spine's
  hip: the hind legs' common ancestor);
- the lobe (for the weights) is the union of the tail bands' rear clusters.

**Adoption** (`adopt_candidate`): a SkinTokens chain (off a branch, not a leg, not the trunk or
neck) with a joint within ADOPT_R of the tail line whose bones own ADOPT of the lobe's vertices
(dominant weight) is renamed tail_00.. and extended with inserted joints along the line from
its last joint to the tip, so there are at least 4 bones, instead of getting a second chain
beside it (the owlbear's 3-bone chain).

**Weights** (`tail_weights`): the tail share t rises from 0 at the root to 1 over BASE x the
height of arc length along the chain (the base blends into the rump) and across the lobe's
border from 0 at EDGE outside to 1 at EDGE inside, over the welded surface (the centaur's tail
sheet is fused to the back of a hind leg); t goes to the two nearest chain bones by inverse
distance, 1 - t stays on the vertex's old bones, re-normalised, but on the lobe's own vertices
(t > 1/2) without the legs' share (stripped: the centaur's tail was on a hind leg), or on the
hip bone when nothing is left. The rump, hips and thighs beyond the blend keep their weights.

blender_rig.py "score" and "finalize" call `add_missing_tail` for rig.adds_tail plans
(quadruped, winged quadruped, centauroid) when find_tail finds fewer than 4 bones; recorded as
`tail_added` in rig.json and manifest.rig (docs/rig-anim.md "Tail chains from the mesh").
"""
import numpy as np

BAND = 0.04       # x height: horizontal band of the rear profile
SIDE = 0.2        # x height: the profile uses vertices this close to the centre line
TOP = 0.2         # x height: the root lies within this of the hip height (above or below)
REAR = 0.1        # x height: a band's rear cluster is this deep from its rearmost vertex
REAR_MIN = 0.1    # x height: the tail's rear clusters lie this far behind the hips or more
GAP = 0.06        # x height: a gap this wide across a rear cluster splits it (two legs)
JUMP = 0.08       # x height: the rear profile jumping forward this much ends the tail
MIN_BANDS = 3     # bands in a tail, at least
THIN = 0.75       # a tail's median band width is at most this share of the body's width at the hips
BONE_LEN = 0.12   # x height of tail per bone: 4 to 6 bones
MIN_BONES, MAX_BONES = 4, 6
ADOPT = 0.5       # an existing chain owning this share of the lobe is the tail
ADOPT_R = 0.08    # x height: the adopted part starts at a bone whose head is this close to the tail line
SAMPLE = 0.01     # x height: surface sample spacing for the profile
EDGE = 0.05       # x height over the surface: the lobe border blends over this on either side
BASE = 0.06       # x height of arc length: the tail's base blends into the rump over this
PREFIX = "tail_"  # inserted chains: tail_00 .. tail_05


def hips(bones: list[dict], legs: list[dict]) -> tuple[np.ndarray, list[dict]]:
    """Mean first joint of the hind legs (behind the feet's mean; all legs when that leaves none)."""
    by_name = {b["name"]: b for b in bones}
    ys = [by_name[g["foot"]]["head"][1] for g in legs]
    hind = [g for g, y in zip(legs, ys) if y < np.mean(ys)] or legs
    return np.mean([by_name[g["chain"][0]]["head"] for g in hind], axis=0), hind


def _poly_param(Q: np.ndarray, line: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per point of Q: distance to the polyline and arc length of its nearest point on it."""
    seg = np.diff(line, axis=0)
    cum = np.r_[0.0, np.cumsum(np.linalg.norm(seg, axis=1))]
    best_d = np.full(len(Q), np.inf)
    best_s = np.zeros(len(Q))
    for i, (a, ab) in enumerate(zip(line, seg)):
        L2 = max(float(ab @ ab), 1e-12)
        t = np.clip(((Q - a) @ ab) / L2, 0.0, 1.0)
        d = np.linalg.norm(Q - (a + t[:, None] * ab), axis=1)
        m = d < best_d
        best_d[m] = d[m]
        best_s[m] = cum[i] + t[m] * np.sqrt(L2)
    return best_d, best_s


def _resample(line: np.ndarray, n: int, s0: float = 0.0) -> np.ndarray:
    """n + 1 points at equal arc lengths along the polyline from arc length s0 to its end."""
    cum = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(line, axis=0), axis=1))]
    out = []
    for s in np.linspace(s0, cum[-1], n + 1):
        i = min(int(np.searchsorted(cum, s, side="right")) - 1, len(line) - 2)
        f = (s - cum[i]) / max(cum[i + 1] - cum[i], 1e-12)
        out.append(line[i] + np.clip(f, 0, 1) * (line[i + 1] - line[i]))
    return np.array(out)


def surface_samples(V: np.ndarray, F: np.ndarray, step: float) -> np.ndarray:
    """The vertices plus points on every triangle at most `step` apart: a decimated mesh's
    big flat triangles (the centaur's tail sheet) leave whole bands without a vertex."""
    A, B, C = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    L = np.maximum.reduce([np.linalg.norm(B - A, axis=1), np.linalg.norm(C - B, axis=1), np.linalg.norm(A - C, axis=1)])
    n = np.clip(np.ceil(L / step).astype(int), 1, 64)
    pts = [V]
    for k in np.unique(n[n > 1]):
        s = n == k
        u, v = np.meshgrid(np.arange(k + 1), np.arange(k + 1))
        m = u + v <= k
        u, v = u[m] / k, v[m] / k
        P = A[s][:, None] * (1 - u - v)[None, :, None] + B[s][:, None] * u[None, :, None] + C[s][:, None] * v[None, :, None]
        pts.append(P.reshape(-1, 3))
    return np.concatenate(pts)


def detect_tail(V: np.ndarray, F: np.ndarray, bones: list[dict], height: float, legs: list[dict], cx: float) -> dict:
    """The tail on the rest mesh V (n,3) with triangles F, facing +Y with +Z up (module doc).
    Returns a report: "found" and, when found, "joints" (root .. tip), "line" (the band
    centroids), the numbers (x height) and "_verts" (the lobe's vertex indices); otherwise
    "reason". The profile runs on surface samples (SAMPLE x the height apart)."""
    h = height
    z0 = float(V[:, 2].min())
    hip, _ = hips(bones, legs)
    rep = {"found": False, "hip": [round(float(c), 4) for c in hip], "centre_x": round(cx, 4)}
    S = surface_samples(V, F, SAMPLE * h)
    sel = (np.abs(S[:, 0] - cx) < SIDE * h) & (S[:, 1] < hip[1])
    vsel = (np.abs(V[:, 0] - cx) < SIDE * h) & (V[:, 1] < hip[1])
    def band(lo_z: float, hi_z: float, prev_x: float | None) -> dict | None:
        """The band's rear cluster, cut at x gaps wider than GAP: the piece holding the
        rearmost point at the root, then the piece nearest the band above's (the centaur's
        tail hangs beside a hind leg near the floor)."""
        m = sel & (S[:, 2] <= hi_z) & (S[:, 2] > lo_z)
        if m.sum() < 3:
            return None
        ry = float(S[m, 1].min())
        c = np.nonzero(m & (S[:, 1] < ry + REAR * h))[0]
        o = c[np.argsort(S[c, 0])]
        cut = np.nonzero(np.diff(S[o, 0]) > GAP * h)[0] + 1
        pieces = np.split(o, cut)
        if prev_x is None:
            pc = next(q for q in pieces if S[q, 1].min() <= ry + 1e-9)
        else:
            pc = min(pieces, key=lambda q: abs(float(S[q, 0].mean()) - prev_x))
        P = S[pc]
        x0, x1 = float(P[:, 0].min()), float(P[:, 0].max())
        ry = float(P[:, 1].min())
        vm = vsel & (V[:, 2] <= hi_z) & (V[:, 2] > lo_z) & (V[:, 0] >= x0 - SAMPLE * h) & (V[:, 0] <= x1 + SAMPLE * h)
        return {"z": (lo_z + hi_z) / 2, "rear": ry, "idx": np.nonzero(vm & (V[:, 1] < ry + REAR * h))[0],
                "c": P.mean(axis=0), "pts": P, "w": x1 - x0, "pieces": len(pieces)}

    tail, prev = [], None
    zt = hip[2] + TOP * h
    while zt > z0:
        b = band(zt - BAND * h, zt, None if prev is None else float(prev["c"][0]))
        zt -= BAND * h
        if b is None:
            if tail:
                break
            continue
        behind = b["rear"] < hip[1] - REAR_MIN * h
        if not tail:
            if behind and b["pieces"] == 1 and b["z"] >= hip[2] - TOP * h:
                tail.append(b)
                prev = b
            continue
        if not behind or b["rear"] > prev["rear"] + JUMP * h or abs(b["c"][0] - prev["c"][0]) > JUMP * h:
            break
        tail.append(b)
        prev = b
    if len(tail) < MIN_BANDS:
        rep["reason"] = f"rear profile: {len(tail)} tail bands (need {MIN_BANDS})"
        return rep
    near_hip = (np.abs(S[:, 2] - hip[2]) < BAND * h) & (np.abs(S[:, 1] - hip[1]) < REAR * h)
    body_w = float(np.ptp(S[near_hip, 0])) if near_hip.sum() > 3 else h
    width = float(np.median([b["w"] for b in tail]))
    rep.update({"bands": len(tail), "width": round(width / h, 3), "body_width": round(body_w / h, 3)})
    if width > THIN * body_w:
        rep["reason"] = f"not thin: median width {width / h:.2f} x h vs body {body_w / h:.2f}"
        return rep
    root = tail[0]["c"]
    last = tail[-1]["pts"]
    tip = last[np.argmax(np.linalg.norm(last - root, axis=1))]
    line = np.array([b["c"] for b in tail] + [tip])
    length = float(np.linalg.norm(np.diff(line, axis=0), axis=1).sum())
    n = int(np.clip(round(length / (BONE_LEN * h)), MIN_BONES, MAX_BONES))
    joints = _resample(line, n)
    verts = np.unique(np.concatenate([b["idx"] for b in tail]))
    r = lambda p: [round(float(c), 4) for c in p]
    rep.update({"found": True, "joints": [r(j) for j in joints], "line": [r(p) for p in line], "root": r(root),
                "tip": r(tip), "verts": int(len(verts)), "length": round(length / h, 3),
                "root_z": round((root[2] - z0) / h, 3), "tip_z": round((tip[2] - z0) / h, 3),
                "root_behind": round((hip[1] - tail[0]["rear"]) / h, 3),
                "tip_behind": round((hip[1] - tip[1]) / h, 3), "_verts": verts})
    return rep


def adopt_candidate(bones: list[dict], dom: np.ndarray, lobe: np.ndarray, line: np.ndarray, height: float,
                    legs: list[dict], spine: dict, rig_topology) -> dict | None:
    """The existing chain that is the tail (module doc), or None: {"chain", "share"}. `dom` is
    each vertex's dominant bone name, `lobe` the lobe's vertex indices."""
    kids = rig_topology._children(bones)
    by_name = {b["name"]: b for b in bones}
    skip = {n for g in legs for n in g["chain"]} | set(spine.get("torso") or []) | set(spine.get("neck") or [])
    own = dom[lobe]
    best = None
    for b in bones:
        n = b["name"]
        if n in skip or not b["parent"] or len(kids[b["parent"]]) < 2:
            continue
        chain = rig_topology.longest_chain(n, kids)
        if any(c in skip for c in chain):
            continue
        d, _ = _poly_param(np.array([by_name[c]["head"] for c in chain]), line)
        if d.min() >= ADOPT_R * height:
            continue  # does not reach into the tail (the wolf's rump stub off the hip)
        share = float(np.isin(own, chain).mean())
        if share >= ADOPT and (best is None or share > best["share"]):
            best = {"chain": chain, "share": round(share, 3)}
    return best


def tail_weights(W: np.ndarray, cols: list[str], V: np.ndarray, E: np.ndarray, lobe: np.ndarray,
                 chain: list[str], segs: dict, leg_bones: set, parent: str, height: float) -> tuple[np.ndarray, np.ndarray, dict]:
    """New weight rows around the lobe (module doc). W (n verts, len(cols)) is modified in
    place; `segs` {bone: (head, tail)} world, E the mesh edges. The tail share t is the root
    ramp (0 at the root, 1 past BASE of arc length) times the edge blend: across the lobe's
    border over the welded surface, from 1 at EDGE inside to 0 at EDGE outside (the centaur's
    tail sheet is fused to the back of a hind leg: a hard border tore there). t goes to the two
    nearest chain bones; 1 - t keeps the vertex's old weights, but on the lobe's own vertices
    (t > 1/2) the legs' share is stripped (to the hip bone when nothing else is left).
    Returns W, the changed rows and counts."""
    from blender_wing_weights import _surface, _surface_dist

    ci = {n: j for j, n in enumerate(cols)}
    tc = np.array([ci[n] for n in chain])
    line = np.array([segs[chain[0]][0]] + [segs[n][1] for n in chain])
    inl = np.zeros(len(V), bool)
    inl[lobe] = True
    wid, adj = _surface(V, E)
    g_in = _surface_dist(wid, adj, np.nonzero(inl)[0])     # outside: how far from the lobe
    g_out = _surface_dist(wid, adj, np.nonzero(~inl)[0])   # inside: how far from the border
    edge = EDGE * height
    blend = np.where(inl, 0.5 + 0.5 * np.clip(g_out / edge, 0, 1), 0.5 - 0.5 * np.clip(g_in / edge, 0, 1))
    blend[~inl & ~np.isfinite(g_in)] = 0.0
    zone = np.nonzero(blend > 1e-4)[0]
    _, s = _poly_param(V[zone], line)
    t = np.clip(s / (BASE * height), 0.0, 1.0) * blend[zone]
    dist = np.stack([_seg(V[zone], *segs[n]) for n in chain], axis=1)
    legc = [ci[n] for n in leg_bones if n in ci]
    keep_all = np.ones(len(cols), bool)
    keep_all[tc] = False
    keep_noleg = keep_all.copy()
    keep_noleg[legc] = False
    leg_before = W[np.ix_(zone, legc)].sum(1) if legc else np.zeros(len(zone))
    stripped = np.zeros(len(zone), bool)
    for k, i in enumerate(zone):
        row = np.zeros(len(cols))
        o = np.argsort(dist[k])[:2]
        inv = 1.0 / (dist[k, o] + 1e-4)
        row[tc[o]] = t[k] * inv / inv.sum()
        strip = inl[i] and t[k] > 0.5
        stripped[k] = strip and leg_before[k] > 1e-6
        old = np.where(keep_noleg if strip else keep_all, W[i], 0.0)
        if old.sum() > 1e-9:
            row += (1 - t[k]) * old / old.sum()
        else:
            row[ci[parent]] += 1 - t[k]
        W[i] = row
    return W, zone, {"lobe_verts": int(len(lobe)), "blend_verts": int(len(zone)), "full_tail": int((t >= 0.999).sum()),
                     "leg_verts_stripped": int(stripped.sum()),
                     "leg_weight_stripped": round(float(leg_before[stripped].sum()), 1),
                     "base_m": round(BASE * height, 4), "edge_m": round(edge, 4)}


def _seg(V: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ab = b - a
    t = np.clip(((V - a) @ ab) / max(float(ab @ ab), 1e-12), 0.0, 1.0)
    return np.linalg.norm(V - (a + t[:, None] * ab), axis=1)


def add_missing_tail(arm, me, rig_topology, feet: int) -> dict | None:
    """In Blender: when find_tail finds fewer than 4 bones, find the tail on the mesh, insert
    (or adopt and extend) a tail_00.. chain on the hip bone and weight it. Returns the report
    (None when the skeleton already has a tail of 4+ bones or has not the plan's legs)."""
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
    if len(legs) < max(feet, 2):
        return None
    had = rig_topology.find_tail(bones, height, legs, min_bones=2)
    if len(rig_topology.find_tail(bones, height, legs)) >= MIN_BONES:
        return None
    cx = rig_topology.centre_x(bones, legs)
    me.data.calc_loop_triangles()
    F = np.empty(len(me.data.loop_triangles) * 3, dtype=np.int64)
    me.data.loop_triangles.foreach_get("vertices", F)
    rep = detect_tail(V, F.reshape(-1, 3), bones, height, legs, cx)
    rep = {"had": had, "height_m": round(height, 3), **rep}
    if not rep["found"]:
        return rep
    lobe = rep.pop("_verts")
    head = rig_topology.find_snout(bones, height, legs)
    sp = rig_topology.find_spine(bones, height, legs, [], head, False)
    _, hind = hips(bones, legs)
    parent = sp["hip"] or rig_topology.common_ancestor([g["chain"][0] for g in hind], bones) or bones[0]["name"]
    if parent in {n for g in legs for n in g["chain"]}:  # never on a leg bone
        parent = bones[[b["name"] for b in bones].index(parent)]["parent"] or parent
    names = {g.index: g.name for g in me.vertex_groups}
    dom = np.array([names[max(v.groups, key=lambda g: g.weight).group] if len(v.groups) else "" for v in me.data.vertices])
    line = np.array(rep["line"])
    adopted = adopt_candidate(bones, dom, lobe, line, height, legs, sp, rig_topology)
    by_name = {b["name"]: b for b in bones}
    if adopted:
        heads = np.array([by_name[n]["head"] for n in adopted["chain"]])
        _, s_heads = _poly_param(heads, line)
        s_end = float(s_heads.max())
        L = rep["length"] * height
        k = len(adopted["chain"])
        extra = max(MIN_BONES - k, int(round((L - s_end) / (BONE_LEN * height))), 1)
        extra = min(extra, max(1, MAX_BONES - k))
        ext = _resample(line, extra, s_end)[1:]  # joints past the adopted chain's last head
        joints = np.r_[heads, ext]
        parent = by_name[adopted["chain"][0]]["parent"]
    else:
        joints = np.array(rep["joints"])
    names_new = [f"{PREFIX}{i:02d}" for i in range(len(joints) - 1)]
    inv = arm.matrix_world.inverted()
    bpy.context.view_layer.objects.active = arm
    arm.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.data.edit_bones
    k = len(adopted["chain"]) if adopted else 0
    if adopted:
        for old, nn in zip(adopted["chain"], names_new):
            eb[old].name = nn
        # the adopted leaf's tail (glTF stores none) points at the first inserted joint
        eb[names_new[k - 1]].tail = inv @ Vector(joints[k])
    prev = eb[names_new[k - 1]] if k else eb[parent]
    for i in range(k, len(names_new)):
        e = eb.new(names_new[i])
        e.head = inv @ Vector(joints[i])
        e.tail = inv @ Vector(joints[i + 1])
        e.parent = prev
        e.use_connect = i > 0
        e.use_deform = True
        prev = e
    bpy.ops.object.mode_set(mode="OBJECT")
    for nn in names_new:
        if me.vertex_groups.get(nn) is None:
            me.vertex_groups.new(name=nn)
    segs = {b.name: (np.array((m @ b.head_local)[:]), np.array((m @ b.tail_local)[:])) for b in arm.data.bones
            if b.use_deform}
    cols = list(segs)
    ci = {g.name: g.index for g in me.vertex_groups}
    W = np.zeros((len(V), len(cols)))
    cj = {n: j for j, n in enumerate(cols)}
    gname = {g.index: g.name for g in me.vertex_groups}
    for v in me.data.vertices:
        for g in v.groups:
            j = cj.get(gname[g.group])
            if j is not None:
                W[v.index, j] = g.weight
    orig = W.copy()
    leg_bones = {n for g in legs for n in g["chain"]}
    E = np.empty(len(me.data.edges) * 2, dtype=np.int64)
    me.data.edges.foreach_get("vertices", E)
    W, zone, wrep = tail_weights(W, cols, V, E.reshape(-1, 2), lobe, names_new, segs, leg_bones, parent, height)
    groups = [me.vertex_groups[ci[n]] if n in ci else me.vertex_groups.new(name=n) for n in cols]
    for i in zone:
        for j in np.nonzero((W[i] > 1e-5) | (orig[i] > 0))[0]:
            if W[i, j] > 1e-5:
                groups[j].add([int(i)], float(W[i, j]), "REPLACE")
            else:
                groups[j].remove([int(i)])
    rep.update({"chain": {"bones": names_new, "parent": parent}, "weights": wrep})
    if adopted:
        rep["adopted"] = {"was": adopted["chain"], "share": adopted["share"], "bones": names_new[:k],
                          "inserted": names_new[k:]}
    return rep

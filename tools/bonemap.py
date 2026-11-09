"""Map a rigged biped's SkinTokens skeleton onto the Quaternius UAL humanoid bone names.

Plain Python, no Blender: reads only the glTF JSON chunk of model_rigged.glb (node TRS and the
mesh's POSITION min/max), so it is cheap enough to run beside a GPU job. docs/anim-reuse.md.

Our rigs are bone_<n> with a few named sockets (hand_l/r, grip_l/r, head, back, hip_l/r,
blender_rig.add_sockets). The structure is found from those anchors:
- head: the `head` socket, else the highest bone near the centre line (skull tip -> parent).
- trunk: the path root -> head. Each arm is the hand's ancestors below the trunk bone it hangs
  from; the higher of the two attach bones is the chest (spine_03).
- wrist: hand_<s>, unless it is a finger joint (SkinTokens often runs the arm on through a
  finger and the rig stage names the last joint): then the joint that leaves UAL's hand share
  of the arm length to the socket; both arms get the same index.
- legs: chains that reach the ground band, climbed up to the bone where a sibling subtree rises
  out of the band; the pair that mirrors best about the centre line (a tail has no twin).
  Ankle: the first joint below the knee near the floor or with a flat segment after it; ball:
  its child reaching farthest out.
- pelvis: the joint whose subtree holds both thighs and the head (the "hips").
- spine: the trunk between pelvis (excl.) and chest (incl.); neck: chest (excl.) to head (excl.).

Chains with a different segment count than UAL: the ends are pinned (upperarm/lowerarm before
the wrist, thigh/calf before the ankle, spine_01/spine_03) and the middle UAL bone takes the
joint nearest the middle of the chain's length. Extra bones are SKIPPED, never interpolated:
they get no clip rotation, keep their rest local pose and follow the parent; a world-space
retarget (rest-pose rotation transfer) still lands the next mapped bone on target, the skipped
bone only loses its share of the bend. Also skipped: bones between pelvis and thigh, finger
joints past the wrist, toes/heels other than the ball, bones on the head. Missing UAL bones (a
2-bone spine, no neck, no toe) are listed in `ual_missing`: the retarget folds their rotation
into the next mapped child (spine_02 -> spine_03, neck_01 -> Head) or drops it (toe roll).
`unmapped`: everything else (fingers, hair, robes, wings): rest pose, follows the parent.

Facing: our models face -Z (rig.py's facing gate), so character-left is -X; UAL faces +Z and
its left is +X. The hand_l/hand_r names are checked against that (a mismatch is a problem);
`toes_point` is measured from the ball bones. The retarget turns UAL 180 degrees about Y and
maps l->l by name, not by X sign.

Status: ok = all 22 UAL bones 1:1, no skipped joint inside a limb or the spine; partial = all
essential bones mapped, something folded or skipped; fail = structure or an essential bone
missing.

  python3 -I tools/bonemap.py archmage ogre ...   |   --all [--json out.json] [--show]
Writes out/srd-5.2.1/<id>/bonemap.json per monster.
"""
import argparse
import json
import math
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "srd-5.2.1"
PLANS = {"biped", "winged_biped"}
SOCKETS = {"grip_l", "grip_r", "back", "hip_l", "hip_r"}  # non-deforming bones the rig stage adds
INSERTED = ("wing_", "tail_")  # chains the rig stage inserts; never a limb
GROUND = 0.18   # a leg's lowest joint is within this fraction of the height above the ground
MIN_DROP = 0.5  # a leg falls at least this much per unit of length (a skirt flap or tail drags)
CLAVICLE = 0.15  # an arm's first bone shorter than this x height is a clavicle
HAND_SHARE = 0.27  # UAL: wrist -> last finger joint is 0.27 of shoulder -> last finger joint
FINGER = 0.05   # a hand socket less than this x height past its parent sits on a finger joint
ANKLE = 0.12    # the ankle is the first joint below the knee within this x height of the ground
FACING = "-Z"   # every rigged model faces -Z (rig.py's facing gate); UAL faces +Z
UAL_CORE = ["pelvis", "spine_01", "spine_02", "spine_03", "neck_01", "Head"] + [
    f"{b}_{s}" for s in "lr" for b in ("clavicle", "upperarm", "lowerarm", "hand", "thigh", "calf", "foot", "ball")]
# without these the clip cannot drive the limb at all; others are folded or dropped
ESSENTIAL = {"pelvis", "Head"} | {f"{b}_{s}" for s in "lr" for b in ("upperarm", "lowerarm", "hand", "thigh",
                                                                     "calf", "foot")}


# --- glTF -------------------------------------------------------------------------------

def read_gltf_json(path: Path) -> dict:
    """Only the JSON chunk of a .glb: the binary buffer (mesh, weights) is never read."""
    with open(path, "rb") as f:
        head = f.read(20)
        magic, _, _, n, kind = struct.unpack("<4sIIII", head)
        if magic != b"glTF" or kind != 0x4E4F534A:
            raise ValueError(f"{path}: not a glb")
        return json.loads(f.read(n))


def _mat(node: dict) -> list[list[float]]:
    if "matrix" in node:
        m = node["matrix"]  # column-major
        return [[m[c * 4 + r] for c in range(4)] for r in range(4)]
    x, y, z, w = node.get("rotation", (0, 0, 0, 1))
    sx, sy, sz = node.get("scale", (1, 1, 1))
    tx, ty, tz = node.get("translation", (0, 0, 0))
    r = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
         [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
         [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
    return [[r[0][0] * sx, r[0][1] * sy, r[0][2] * sz, tx], [r[1][0] * sx, r[1][1] * sy, r[1][2] * sz, ty],
            [r[2][0] * sx, r[2][1] * sy, r[2][2] * sz, tz], [0, 0, 0, 1]]


def _mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def load_skeleton(path: Path) -> dict:
    """{"joints": {name: {"parent", "children", "pos" (rest world xyz, glTF Y up)}},
    "ground", "height"} for the first skin. Height from the mesh bounds when present."""
    g = read_gltf_json(path)
    nodes = g["nodes"]
    parent = {c: i for i, n in enumerate(nodes) for c in n.get("children", [])}
    world: dict[int, list] = {}

    def w(i):
        if i not in world:
            world[i] = _mul(w(parent[i]), _mat(nodes[i])) if i in parent else _mat(nodes[i])
        return world[i]

    skin = g["skins"][0]
    jset = set(skin["joints"])
    joints = {}
    for i in skin["joints"]:
        m = w(i)
        p = parent.get(i)
        joints[nodes[i]["name"]] = {"parent": nodes[p]["name"] if p in jset else None,
                                    "children": [nodes[c]["name"] for c in nodes[i].get("children", []) if c in jset],
                                    "pos": (m[0][3], m[1][3], m[2][3])}
    ys = [j["pos"][1] for j in joints.values()]
    lo, hi = min(ys), max(ys)
    for i, n in enumerate(nodes):  # skinned mesh: vertices are in the armature's space
        if "mesh" in n:
            for prim in g["meshes"][n["mesh"]]["primitives"]:
                acc = g["accessors"][prim["attributes"]["POSITION"]]
                if "min" in acc:
                    lo, hi = min(lo, acc["min"][1]), max(hi, acc["max"][1])
    return {"joints": joints, "ground": lo, "height": hi - lo}


# --- structure ----------------------------------------------------------------------------

def _dist(a, b):
    return math.dist(a, b)


def _path_up(j: dict, name: str) -> list[str]:
    out = [name]
    while j[out[-1]]["parent"]:
        out.append(j[out[-1]]["parent"])
    return out  # name ... root


def _subtree(j: dict, name: str) -> list[str]:
    out, todo = [], [name]
    while todo:
        n = todo.pop()
        out.append(n)
        todo += j[n]["children"]
    return out


def _arc_pick(j: dict, chain: list[str], frac: float) -> str:
    """The joint of `chain` (excluding both ends) nearest `frac` of the way along it."""
    pts = [j[n]["pos"] for n in chain]
    cum = [0.0]
    for a, b in zip(pts, pts[1:]):
        cum.append(cum[-1] + _dist(a, b))
    target = cum[-1] * frac
    return min(chain[1:-1], key=lambda n: abs(cum[chain.index(n)] - target))


def _segment_fit(j: dict, chain: list[str], names: list[str], what: str, res: dict) -> None:
    """Map `chain` (top -> bottom, without the end bone mapped elsewhere) onto `names`
    (UAL, top -> bottom). Ends pinned, middles by arc length, extras skipped."""
    n, k = len(chain), len(names)
    if n == 0:
        res["ual_missing"] += names
        res["problems"].append(f"{what}: no bones (UAL has {k})")
        return
    if n == k:
        for o, u in zip(chain, names):
            res["map"][o] = u
        return
    if n < k:
        if k == 3 and n == 2:  # spine: keep the chest as spine_03, the lower as spine_01
            pairs = [(chain[0], names[0]), (chain[1], names[2])]
        elif k == 3 and n == 1:
            pairs = [(chain[0], names[2])]
        else:  # 2 UAL bones, 1 ours (an arm or leg missing a joint): keep the upper one
            pairs = [(chain[0], names[0])]
        for o, u in pairs:
            res["map"][o] = u
        miss = [u for u in names if u not in {u for _, u in pairs}]
        res["ual_missing"] += miss
        res["problems"].append(f"{what}: {n} bones for UAL's {k}, missing {','.join(miss)} (rotation folded into "
                               f"the next mapped bone)")
        return
    # more bones than UAL. Arm/leg (2 names): upper = first, lower = the joint nearest the
    # middle of the chain's length down to the hand/foot (the elbow/knee). Spine (3 names):
    # spine_01 first, spine_03 the chest, spine_02 the joint nearest the middle.
    end = res["_next"].get(what)
    full = chain + [end] if end else chain
    if k == 2:
        picks = {chain[0]: names[0], _arc_pick(j, full, 0.5) if len(full) > 2 else chain[1]: names[1]}
    else:
        picks = {chain[0]: names[0], chain[-1]: names[-1], _arc_pick(j, chain, 0.5): names[1]}
    for o, u in picks.items():
        res["map"][o] = u
    extra = [c for c in chain if c not in picks]
    for e in extra:
        res["extra"][e] = f"{what}: skipped (rest local pose, follows parent)"
    res["problems"].append(f"{what}: {n} bones for UAL's {k}, extra {','.join(extra)} skipped")


def find_head(j: dict, deform: set, h: float, ground: float, res: dict) -> str | None:
    """The `head` socket bone, else the highest joint near the centre line between the hands;
    a leaf right above its parent (< 0.07 x height away) is a skull tip, so the parent wins
    (rig_topology.find_head's rule, with joint distances standing in for bone tails)."""
    if "head" in j:
        if not j["head"]["parent"]:
            res["problems"].append("`head` socket is the root bone: the rig is scrambled")
            return None
        return "head"
    cx = (j["hand_l"]["pos"][0] + j["hand_r"]["pos"][0]) / 2
    central = [n for n in deform if abs(j[n]["pos"][0] - cx) < 0.08 * h and j[n]["parent"]]
    if not central:
        res["problems"].append("no `head` socket and no central bone to stand in")
        return None
    top = max(central, key=lambda n: j[n]["pos"][1])
    p = j[top]["parent"]
    if not [c for c in j[top]["children"] if c in deform] and p in central \
            and _dist(j[top]["pos"], j[p]["pos"]) < 0.07 * h:
        top = p
    res["problems"].append(f"no `head` socket: head found structurally ({top})")
    return top


def classify(sk: dict) -> dict:
    j, ground, h = sk["joints"], sk["ground"], sk["height"]
    res = {"status": "fail", "confidence": 0.0, "map": {}, "extra": {}, "unmapped": [], "ual_missing": [],
           "problems": [], "chains": {}, "facing": FACING, "height": round(h, 3), "joints": len(j), "_next": {}}
    deform = {n for n in j if n not in SOCKETS and not n.startswith(INSERTED)}
    for need in ("hand_l", "hand_r"):
        if need not in j:
            res["problems"].append(f"no `{need}` socket bone")
    if any(n not in j for n in ("hand_l", "hand_r")):
        return _finish(res)
    head = find_head(j, deform, h, ground, res)
    if not head:
        return _finish(res)
    up = lambda y: y - ground  # noqa: E731
    trunk = list(reversed(_path_up(j, head)))  # root ... head
    on_trunk = set(trunk)

    # arms: the hand's ancestors below the trunk bone they hang from
    arms = {}
    for s in "lr":
        p = _path_up(j, f"hand_{s}")
        i = next(k for k, n in enumerate(p) if n in on_trunk)
        arms[s] = {"attach": p[i], "chain": list(reversed(p[:i]))}  # shoulder ... hand_<s>
    xl, xr = j["hand_l"]["pos"][0], j["hand_r"]["pos"][0]
    lsign = 1.0 if xl > xr else -1.0  # +1: hand_l is on +X
    res["left_x"] = "+X" if lsign > 0 else "-X"
    if abs(xl - xr) < 0.05 * h:
        res["problems"].append("hands are not apart in X: side check unreliable")
    elif (lsign > 0) != (FACING == "+Z"):  # facing -Z (rig.py): character-left is -X
        res["problems"].append(f"hand_l is on {res['left_x']}, but facing {FACING} puts character-left on "
                               f"{'+X' if FACING == '+Z' else '-X'}: hand names are swapped")
    arm_bones = {n for a in arms.values() for n in a["chain"]} | set(_subtree(j, "hand_l")) | set(_subtree(j, "hand_r"))
    head_sub = set(_subtree(j, head))
    tall = {n: max(up(j[m]["pos"][1]) for m in _subtree(j, n)) for n in j}
    low = GROUND * h

    # legs: climb from each low joint while every other deforming sibling stays in the band
    tops: dict[str, list[str]] = {}
    for n in deform - arm_bones - head_sub - on_trunk:
        if up(j[n]["pos"][1]) > low:
            continue
        cur = n
        while True:
            p = j[cur]["parent"]
            if not p or p in on_trunk or p in arm_bones:
                break
            if any(tall[o] > low for o in j[p]["children"] if o != cur and o in deform):
                break
            cur = p
        tops.setdefault(cur, []).append(n)
    cands = []
    for top in tops:
        if up(j[top]["pos"][1]) < 0.25 * h:
            continue  # a toe or a hem flap, not a leg
        main = [top]  # follow the child whose subtree reaches lowest
        while kids := [c for c in j[main[-1]]["children"] if c in deform]:
            main.append(min(kids, key=lambda c: min(up(j[m]["pos"][1]) for m in _subtree(j, c))))
        pts = [j[n]["pos"] for n in main]
        length = sum(_dist(a, b) for a, b in zip(pts, pts[1:]))
        drop = pts[0][1] - min(p[1] for p in pts)
        if len(main) < 3 or length <= 0 or drop / length < MIN_DROP:
            continue
        side = "l" if (pts[-1][0] - (xl + xr) / 2) * lsign > 0 else "r"
        cands.append({"top": top, "main": main, "side": side, "score": drop / h})
    # the pair that mirrors best about the hands' centre line (a tail or a robe flap that also
    # reaches the floor has no twin), the steeper drop breaking ties
    cx = (xl + xr) / 2

    def mirror_cost(a, b):
        pa_, pb_ = [j[n]["pos"] for n in a["main"]], [j[n]["pos"] for n in b["main"]]
        k = min(len(pa_), len(pb_))
        return sum(_dist((2 * cx - p[0], p[1], p[2]), q) for p, q in zip(pa_[:k], pb_[:k])) / k / h \
            + abs(len(pa_) - len(pb_)) * 0.05 - 0.5 * (a["score"] + b["score"])
    pairs = [(a, b) for a in cands if a["side"] == "l" for b in cands if b["side"] == "r"]
    legs = {}
    if pairs:
        a, b = min(pairs, key=lambda ab: mirror_cost(*ab))
        legs = {"l": a, "r": b}
    else:
        for c in sorted(cands, key=lambda c: -c["score"]):
            legs.setdefault(c["side"], c)
    others = [c["top"] for c in cands if legs.get(c["side"]) is not c]
    if others:
        res["problems"].append(f"{len(others)} more chain(s) reach the ground ({','.join(others)}): not legs")
    if set(legs) != {"l", "r"}:
        res["problems"].append(f"legs found: {sorted(legs) or 'none'} (need l and r)")
        return _finish(res)

    # pelvis = the joint above both thighs and the head (the "hips")
    pa = [set(_path_up(j, legs[s]["top"])) for s in "lr"]
    pelvis = next(n for n in _path_up(j, head) if n in pa[0] and n in pa[1])
    res["map"][pelvis] = "pelvis"
    for s in "lr":  # bones between pelvis and thigh (SkinTokens often adds a hip roll pair)
        for n in _path_up(j, legs[s]["top"])[1:]:
            if n == pelvis:
                break
            res["extra"].setdefault(n, "pelvis->thigh: skipped (rest local pose, follows pelvis)")
    for n in trunk[:trunk.index(pelvis)]:
        res["extra"][n] = "above pelvis: skipped (UAL `root` is not animated in the in-place clips)"

    # spine / chest / neck
    att = {s: arms[s]["attach"] for s in "lr"}
    chest = max(att.values(), key=trunk.index)
    if att["l"] != att["r"]:
        res["problems"].append(f"arms hang from different trunk bones ({att['l']}, {att['r']}): chest = {chest}")
    if trunk.index(chest) <= trunk.index(pelvis):
        res["problems"].append("arms hang at or below the pelvis: no spine")
        spine, neck = [], trunk[trunk.index(pelvis) + 1:-1]
    else:
        spine = trunk[trunk.index(pelvis) + 1:trunk.index(chest) + 1]
        neck = trunk[trunk.index(chest) + 1:-1]
    _segment_fit(j, spine, ["spine_01", "spine_02", "spine_03"], "spine", res)
    if neck:
        res["map"][neck[0]] = "neck_01"
        for e in neck[1:]:
            res["extra"][e] = "neck: skipped (rest local pose, follows parent)"
        if len(neck) > 1:
            res["problems"].append(f"neck: {len(neck)} bones for UAL's 1, extra {','.join(neck[1:])} skipped")
    else:
        res["ual_missing"].append("neck_01")
        res["problems"].append("neck: no bone between chest and head (neck_01 folded into Head)")
    res["map"][head] = "Head"
    for n in _subtree(j, head)[1:]:
        if n in deform:
            res["extra"][n] = "on the head (jaw, skull tip, hair): skipped, follows Head"

    # arms: clavicle = a short first bone. SkinTokens often runs the arm on through a finger
    # and the rig stage names its last joint hand_<s>: when hand_<s> has no fingers of its own
    # and sits a finger-joint length past its parent, the wrist is the joint that leaves the
    # UAL hand's share (HAND_SHARE) of the arm's length to the socket. Elbow: the joint nearest
    # the middle of shoulder -> wrist. Two arms with the same number of joints get the
    # same wrist index (the summed error decides), so a borderline share cannot split them.
    err = {}
    for s in "lr":
        ch = arms[s]["chain"]
        if len(ch) >= 4 and _dist(j[ch[0]]["pos"], j[ch[1]]["pos"]) < CLAVICLE * h:
            ch = ch[1:]
        pts = [j[n]["pos"] for n in ch]
        cum = [0.0]
        for a, b in zip(pts, pts[1:]):
            cum.append(cum[-1] + _dist(a, b))
        if not [c for c in j[ch[-1]]["children"] if c in deform] and len(ch) > 3 \
                and cum[-1] - cum[-2] < FINGER * h:
            err[s] = {i: abs((cum[-1] - cum[i]) / cum[-1] - HAND_SHARE) for i in range(2, len(ch))}
    if len(err) == 2 and set(err["l"]) == set(err["r"]):
        common = min(err["l"], key=lambda i: err["l"][i] + err["r"][i])
        wrists = {"l": common, "r": common}
    else:
        wrists = {s: min(e, key=e.get) for s, e in err.items()}
    for s in "lr":
        ch = arms[s]["chain"]  # shoulder ... hand_<s>
        res["chains"][f"arm_{s}"] = ch
        if len(ch) >= 4 and _dist(j[ch[0]]["pos"], j[ch[1]]["pos"]) < CLAVICLE * h:
            res["map"][ch[0]] = f"clavicle_{s}"
            ch = ch[1:]
        else:
            res["ual_missing"].append(f"clavicle_{s}")
            res["problems"].append(f"arm_{s}: no clavicle (shrug dropped)")
        wrist = wrists.get(s, len(ch) - 1)
        res["map"][ch[wrist]] = f"hand_{s}"
        past = ch[wrist + 1:]
        for e in past:
            res["extra"][e] = f"arm_{s}: past the wrist (finger joints), skipped; follows UAL hand_{s}"
        if past:
            res["problems"].append(f"arm_{s}: wrist is {ch[wrist]}, {len(past)} finger joint(s) to the hand_{s} "
                                   "socket skipped")
        res["_next"][f"arm_{s}"] = ch[wrist]
        _segment_fit(j, ch[:wrist], [f"upperarm_{s}", f"lowerarm_{s}"], f"arm_{s}", res)

    # legs: foot = the first joint below the knee within ANKLE x height of the ground or whose
    # next segment runs more sideways/forward than down; ball = its child reaching farthest out
    toe_dz = []
    for s in "lr":
        main = legs[s]["main"]
        res["chains"][f"leg_{s}"] = main
        def flat(i):
            if i + 1 >= len(main):
                return False
            a, b = j[main[i]]["pos"], j[main[i + 1]]["pos"]
            return abs(a[1] - b[1]) < math.hypot(a[0] - b[0], a[2] - b[2])
        fi = next((i for i in range(2, len(main)) if up(j[main[i]]["pos"][1]) < ANKLE * h or flat(i)),
                  len(main) - 1)
        foot = main[fi]
        kids = [c for c in j[foot]["children"] if c in deform]
        if kids:
            fp = j[foot]["pos"]
            ball = max(kids, key=lambda c: max(math.hypot(j[m]["pos"][0] - fp[0], j[m]["pos"][2] - fp[2])
                                               for m in _subtree(j, c)))
            res["map"][ball] = f"ball_{s}"
            toe_dz.append(j[ball]["pos"][2] - fp[2])
            for c in kids:
                for e in _subtree(j, c):
                    if e != ball and e in deform:
                        res["extra"][e] = f"leg_{s}: toe/heel, skipped; follows foot or ball"
        else:
            res["ual_missing"].append(f"ball_{s}")
            res["problems"].append(f"leg_{s}: no toe bone (ball_{s} dropped)")
        res["map"][foot] = f"foot_{s}"
        res["_next"][f"leg_{s}"] = foot
        _segment_fit(j, main[:fi], [f"thigh_{s}", f"calf_{s}"], f"leg_{s}", res)

    if toe_dz:
        dz = sum(toe_dz) / len(toe_dz)
        res["toes_point"] = "-Z" if dz < 0 else "+Z"
        if res["toes_point"] != FACING and abs(dz) > 0.02 * h:
            res["problems"].append(f"toe bones point {res['toes_point']} (backwards; the body faces {FACING}): "
                                   "foot roll will bend the wrong way")
    res["chains"]["spine"] = [pelvis] + spine
    res["chains"]["neck_head"] = neck + [head]
    return _finish(res)


def _finish(res: dict) -> dict:
    """Status: ok = all 22 UAL bones mapped 1:1 with no skipped joint inside an arm, leg or
    spine and sides that agree with the facing; partial = every ESSENTIAL bone mapped but
    something folded, dropped or skipped; fail = an essential bone (or the structure) is missing.
    Skipped finger joints past the wrist, toes/heels and bones on the head or between pelvis
    and thigh do not count against ok (UAL has no counterpart there to lose)."""
    del res["_next"]
    mapped = set(res["map"].values())
    res["ual_missing"] = sorted(set(res["ual_missing"]) | {u for u in UAL_CORE if u not in mapped})
    essential_missing = [u for u in res["ual_missing"] if u in ESSENTIAL]
    soft = ("pelvis->", "above pelvis", "on the head", "toe/heel", "past the wrist")
    limb_extra = [e for e, why in res["extra"].items() if not any(t in why for t in soft)]
    side_bad = [p for p in res["problems"] if "swapped" in p or "unreliable" in p or "backwards" in p]
    if not res["map"] or essential_missing:
        res["status"] = "fail"
        if essential_missing and res["map"]:
            res["problems"].append(f"essential UAL bones unmapped: {','.join(essential_missing)}")
    else:
        res["status"] = "ok" if not res["ual_missing"] and not limb_extra and not side_bad else "partial"
    conf = 1.0 - 0.05 * len(res["ual_missing"]) - 0.05 * len(limb_extra) - 0.15 * len(side_bad)
    res["confidence"] = 0.0 if res["status"] == "fail" else round(max(0.05, min(1.0, conf)), 2)
    return res


# --- CLI ------------------------------------------------------------------------------------

def use_source(source: str) -> None:
    """Work on out/<source>/ and that source's registry (default the SRD)."""
    global SOURCE
    SOURCE = source


def rigged_bipeds() -> list[str]:
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    import sources
    reg = json.loads(sources.registry_path(SOURCE).read_text())
    return [m["id"] for m in reg["monsters"] if m.get("body_plan") in PLANS
            and (ROOT / "out" / SOURCE / m["id"] / "model_rigged.glb").exists()]


def run(mid: str, show: bool = False) -> dict:
    glb = ROOT / "out" / SOURCE / mid / "model_rigged.glb"
    if not glb.exists():
        return {"id": mid, "status": "fail", "confidence": 0.0, "problems": ["no model_rigged.glb"], "map": {}}
    sk = load_skeleton(glb)
    res = {"id": mid, **classify(sk)}
    unmapped = [n for n in sk["joints"] if n not in res["map"] and n not in res["extra"] and n not in SOCKETS]
    res["unmapped"] = unmapped  # fingers, toes' siblings, hair, robes, wings: stay at rest, follow parent
    (glb.parent / "bonemap.json").write_text(json.dumps(res, indent=1) + "\n")
    if show:
        print(f"\n== {mid}  (height {sk['height']:.2f} m, ground y {sk['ground']:.3f}, facing {res.get('facing')}, "
              f"left {res.get('left_x')})")
        for k, ch in res.get("chains", {}).items():
            print(f"  {k}:")
            for n in ch:
                p = sk["joints"][n]["pos"]
                print(f"    {n:10s} -> {res['map'].get(n, '(' + res['extra'].get(n, 'unmapped')[:30] + ')'):34s} "
                      f"({p[0]:+.3f}, {p[1] - sk['ground']:.3f}, {p[2]:+.3f})")
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--all", action="store_true", help="every rigged biped / winged biped in the registry")
    ap.add_argument("--json", help="also write all results to this file")
    ap.add_argument("--show", action="store_true", help="print each chain with rest positions (y above ground)")
    a = ap.parse_args()
    ids = rigged_bipeds() if a.all else a.ids
    if not ids:
        ap.error("give monster ids or --all")
    results = [run(m, a.show) for m in ids]
    print(f"{'monster':20s} {'status':8s} {'conf':>5s} {'joints':>6s}  reasons")
    for r in results:
        why = "; ".join(r.get("problems", [])) or "-"
        print(f"{r['id']:20s} {r['status']:8s} {r['confidence']:5.2f} {r.get('joints', 0):6d}  {why}")
    counts = {s: sum(r["status"] == s for r in results) for s in ("ok", "partial", "fail")}
    print(f"\n{len(results)} monsters: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=1) + "\n")


if __name__ == "__main__":
    main()

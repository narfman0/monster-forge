"""Find legs in an unnamed skeleton (SkinTokens bones are just bone_0, bone_1, ...).

Plain Python, shared by blender_rig.py (roll scoring) and blender_walk.py (gait). A bone
is a dict: name, parent (name or None), head, tail (xyz, Z up, in metres).
"""

GROUND = 0.18      # a foot ends within this fraction of the height above the ground
TOES = 0.09        # bones this low are toes: climbing past them finds the foot, not the shin
MIN_DROP = 0.35    # a leg's drop over its length: rejects a tail lying on the ground


def _children(bones: list[dict]) -> dict:
    kids = {b["name"]: [] for b in bones}
    for b in bones:
        if b["parent"]:
            kids[b["parent"]].append(b["name"])
    return kids


def _descendants(name: str, kids: dict) -> list[str]:
    out = []
    for c in kids[name]:
        out += [c] + _descendants(c, kids)
    return out


def _dist(a, b) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def find_legs(bones: list[dict], height: float, want: int, sprawl: bool = False) -> list[dict]:
    """Up to `want` legs, best first: {"chain": [hip ... foot], "foot", "shin", "level",
    "length"}. The foot is the bone that carries any toes, so toes move with it. With a
    real foot on the ground, IK drives `shin` and the foot is kept level; a spider leg's
    last bone plunges to the ground from high up, so there `shin` is that bone itself and
    IK drives its tip (level is False). Sprawled legs rank by length, so fangs lose."""
    by_name = {b["name"]: b for b in bones}
    kids = _children(bones)
    low = height * GROUND
    toes = height * TOES
    feet = set()
    for b in bones:
        if kids[b["name"]] or (not sprawl and min(b["head"][2], b["tail"][2]) > low):
            continue  # sprawled leg tips are unreliable: rank every chain by length instead
        foot = b["name"]
        while True:  # climb over toes: a parent whose whole subtree is on the ground
            p = by_name[foot]["parent"]
            if not p or by_name[p]["head"][2] > toes:
                break
            if not all(by_name[x]["head"][2] <= toes for x in _descendants(p, kids)):
                break
            foot = p
        feet.add(foot)
    legs = []
    for foot in feet:
        chain = [foot]
        while True:  # up to the branch point: the spine has more than one child
            p = by_name[chain[0]]["parent"]
            if not p or len(kids[p]) > 1:
                break
            chain.insert(0, p)
        if len(chain) < 3:
            continue
        last = by_name[foot]
        tip_only = last["head"][2] > toes
        pts = [by_name[n]["head"] for n in chain] + ([last["tail"]] if tip_only else [])
        length = sum(_dist(a, b) for a, b in zip(pts, pts[1:]))
        drop = pts[0][2] - pts[-1][2]
        if length <= 0 or (not sprawl and drop / length < MIN_DROP):
            continue
        legs.append({"chain": chain, "foot": foot, "shin": foot if tip_only else chain[-2], "level": not tip_only,
                     "length": length, "drop": drop})
    legs.sort(key=lambda g: g["length"] if sprawl else g["drop"] / g["length"], reverse=True)
    return legs[:want]


def symmetry(bones: list[dict], height: float) -> float:
    """Mean distance from each joint to the nearest mirrored joint, as a fraction of the
    height, about the skeleton's own centre line. 0 = perfectly mirrored."""
    pts = [b["head"] for b in bones]
    cx = sum(p[0] for p in pts) / len(pts)
    mirror = [(2 * cx - p[0], p[1], p[2]) for p in pts]
    return sum(min(_dist(m, p) for p in pts) for m in mirror) / len(pts) / height


def longest_chain(name: str, kids: dict) -> list[str]:
    return [name] + max((longest_chain(c, kids) for c in kids[name]), key=len, default=[])


def centre_x(bones: list[dict], legs: list[dict]) -> float:
    by_name = {b["name"]: b for b in bones}
    pts = [by_name[g["foot"]]["head"] for g in legs] or [b["head"] for b in bones]
    return sum(p[0] for p in pts) / len(pts)


def find_arms(bones: list[dict], height: float, legs: list[dict]) -> list[dict]:
    """Upright plans: per side the shortest chain off the spine that hangs below its root
    and is not a leg (the shortest, so a wing chain beside the arm loses). Facing +Y with
    +Z up, the right side is +X. Each: {"side": "r"|"l", "root", "upper", "forearm",
    "hand" (None when the forearm is the last bone), "chain"}."""
    by_name = {b["name"]: b for b in bones}
    kids = _children(bones)
    leg_bones = {n for g in legs for n in g["chain"]}
    cx = centre_x(bones, legs)
    best: dict[str, tuple] = {}
    for b in bones:
        if b["name"] in leg_bones or not b["parent"] or len(kids[b["parent"]]) < 2:
            continue
        chain = longest_chain(b["name"], kids)
        tip = by_name[chain[-1]]["head"]
        if len(chain) < 3 or tip[2] >= b["head"][2] or abs(tip[0] - cx) < 0.06 * height:
            continue
        if any(n in leg_bones for n in chain) or b["head"][2] < 0.4 * height:
            continue
        side = "r" if tip[0] > cx else "l"
        pts = [by_name[n]["head"] for n in chain]
        length = sum(_dist(a, c) for a, c in zip(pts, pts[1:]))
        if side not in best or length < best[side][0]:
            best[side] = (length, chain)
    arms = []
    for side, (_, chain) in best.items():
        clavicle = _dist(by_name[chain[0]]["head"], by_name[chain[1]]["head"]) < 0.15 * height
        i = 1 if clavicle else 0
        arms.append({"side": side, "root": chain[0], "upper": chain[i], "forearm": chain[i + 1],
                     "hand": chain[i + 2] if len(chain) > i + 2 else None, "chain": chain})
    return arms


def find_head(bones: list[dict], height: float, legs: list[dict]) -> str | None:
    """The highest bone on the centre line (ears and horns sit off it); a short one gives
    way to its parent, so a hair tuft does not win over the skull."""
    cx = centre_x(bones, legs)
    by_name = {b["name"]: b for b in bones}
    central = [b for b in bones if abs(b["head"][0] - cx) < 0.08 * height]
    if not central:
        return None
    top = max(central, key=lambda b: b["head"][2] + b["tail"][2])
    if _dist(top["head"], top["tail"]) < 0.07 * height and top["parent"] in {b["name"] for b in central}:
        top = by_name[top["parent"]]
    return top["name"]

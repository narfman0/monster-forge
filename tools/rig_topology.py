"""Find legs in an unnamed skeleton (SkinTokens bones are just bone_0, bone_1, ...).

Plain Python, shared by blender_rig.py (roll scoring) and blender_clips.py (clips). A bone
is a dict: name, parent (name or None), head, tail (xyz, Z up, in metres).
"""

import math

GROUND = 0.18      # a foot ends within this fraction of the height above the ground
TOES = 0.09        # bones this low are toes: climbing past them finds the foot, not the shin
MIN_DROP = 0.35    # a leg's drop over its length: rejects a tail lying on the ground
WING_REACH = 0.4   # a wing reaches this far sideways (x height) from the centre line
SNOUT_SIDE = 0.15  # find_snout: the head bone's midpoint is this close (x height) to the centre line
WING_PREFIX = "wing_"  # chains the rig stage inserts (blender_wing_weights.py): wings, never legs or arms
TAIL_PREFIX = "tail_"  # tail chains the rig stage inserts or adopts (blender_tail_chain.py): the tail, never a leg or an arm


def _inserted_wing(name: str) -> bool:
    return name.startswith(WING_PREFIX)


def _inserted_tail(name: str) -> bool:
    return name.startswith(TAIL_PREFIX)


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


def find_legs(bones: list[dict], height: float, want: int, sprawl: bool = False, rescue: bool = False,
              sprawl_drop: bool = False) -> list[dict]:
    """Up to `want` legs, best first: {"chain": [hip ... foot], "foot", "shin", "level",
    "length"}. The foot is the bone that carries any toes, so toes move with it. With a
    real foot on the ground, IK drives `shin` and the foot is kept level; a spider leg's
    last bone plunges to the ground from high up, so there `shin` is that bone itself and
    IK drives its tip (level is False). Sprawled legs rank by length, so fangs lose.

    `rescue` (the anim stage, the mirror-leg fix): when this finds fewer than `want` legs,
    rescue_legs() looks again with looser rules and its answer is taken when it has as many
    legs or more. A rig that already has its legs gets exactly the same answer.

    `sprawl_drop` (the anim stage; clip audit 2026-11-06): a sprawled chain whose end is higher
    than its root is not a leg. Ranked by length, a scorpion's tail curled over its back and its
    claw (giant scorpion: 2 of its 8 "legs", whose IK targets up in the air pulled the body
    0.3-0.4 x the height into the floor in the attack and the hit) and a spider's legs held up
    in the air (phase spider: 2 of 8) won over the real legs. A raised front leg that still
    ends below its hip (the giant spider's) stays a leg."""
    legs = _find_legs(bones, height, want, sprawl, sprawl_drop)
    if rescue and not sprawl and len(legs) < want:
        more = rescue_legs(bones, height, want)
        if len(more) >= len(legs):  # as many and none of them a tail on the floor (bronze wyrmling)
            return more
    return legs


TOE_FAN_TOP = 0.35  # rescue_legs: a toe fan's ankle is at most this x the height above the ground
TOE_FAN_RISE = 0.05  # ... and no toe rises more than this x the height above the ankle
TAIL_RUN = 1.0      # rescue_legs: a "leg" that runs backwards more than this x its drop ...
TAIL_MID = 0.1      # ... and ends this close (x height) to the root's centre line is a tail on the floor
TWIN_Y = 0.15      # rescue_legs: a raised twin's foot is this close (x height) front to back to its leg's
SAME_FOOT = 0.06    # rescue_legs: soles this close (x height) are one foot skinned to two chains; the better chain is kept
FOOT_SPAN = 0.15    # rescue_legs: a foot without a toe fan whose bones lie longer than this (x height) on the floor is a tail
LEG_ROOT_SIDE = 0.35  # rescue_legs: a chain that starts this far out (x height) and above half the height is a wing


def _toe_fans(bones: list[dict], height: float, kids: dict, by_name: dict, grounded: bool = True) -> set:
    """Ankles that fan into toes (rescue_legs): a bone low on the body with 2+ children whose
    subtrees stay at or below it, at least two of them reaching the ground band (`grounded`;
    without it a raised paw counts too). The red dragon wyrmling's right fore foot (bone_19,
    ankle at 0.25 x the height, toes ending at 0.17-0.21), the crocodile's (toes rooted at
    0.10-0.11, just above TOES)."""
    out = set()
    for b in bones:
        n = b["name"]
        if len(kids[n]) < 2 or b["head"][2] > TOE_FAN_TOP * height or _inserted_wing(n) or _inserted_tail(n):
            continue
        sub = _descendants(n, kids)
        top = max(max(by_name[x]["head"][2], by_name[x]["tail"][2]) for x in sub)
        if top > b["head"][2] + TOE_FAN_RISE * height:
            continue
        reach = [min(min(by_name[x]["head"][2], by_name[x]["tail"][2]) for x in [c, *_descendants(c, kids)])
                 for c in kids[n]]
        if not grounded or sum(r <= GROUND * height for r in reach) >= 2:
            out.add(n)
    return out


def rescue_legs(bones: list[dict], height: float, want: int) -> list[dict]:
    """Legs the strict rules of find_legs miss, for a non-sprawled body (anim stage, mirror-leg
    fix; the SkinTokens rolls of the dragon wyrmlings, crocodile, giant lizard and rust monster):

    - a foot that fans into toes from an ankle above TOES (_toe_fans) is the foot, so the
      chain above it is the leg (find_legs took each toe as a one-bone "leg");
    - not legs: a chain that runs backwards farther than it drops and ends near the root's
      centre line (the crocodile's tail on the floor, which find_legs took), one that starts
      LEG_ROOT_SIDE x the height or more out to the side and above half the height (a dragon's
      wing finger whose tip rests on the floor: the red and gold wyrmlings', 0.43-0.6 out at
      0.64-0.67 up; the giant frog's hips are 0.38 out at 0.33 up), one whose foot is not a
      toe fan and lies farther out from its hip than it drops or spans more than FOOT_SPAN x
      the height on the floor (the red dragon wyrmling's tail curled on the floor beside it,
      the green's curled under it: 0.23-0.24), and one of 2 bones (the giant frog's folded
      hind legs flip under the foot IK: they ride the body);
    - of two chains whose soles are within SAME_FOOT x the height, only the better one;
    - at most half of `want` per side first, then the rest by rank (two hind legs on one side
      do not crowd out the other side's only leg);
    - still short: a raised leg, the missing twin of a leg taken: a chain off the same bone, on
      the other side of it, within TWIN_Y x the height front to back, that drops (down to a
      toe fan when it has one) to 0.1 x the height or more below that bone (the rearing red
      dragon wyrmling holds both fore paws up and only the right one's toes reach the ground
      band; the brass wyrmling's left fore leg is lifted mid-stride)."""
    by_name = {b["name"]: b for b in bones}
    kids = _children(bones)
    low = height * GROUND
    toes = height * TOES
    fans = _toe_fans(bones, height, kids, by_name)
    raised = _toe_fans(bones, height, kids, by_name, grounded=False) - fans
    feet = set(fans)
    for b in bones:  # find_legs' own feet
        if _inserted_wing(b["name"]) or _inserted_tail(b["name"]):
            continue
        if kids[b["name"]] or min(b["head"][2], b["tail"][2]) > low:
            continue
        foot = b["name"]
        while True:
            p = by_name[foot]["parent"]
            if not p or by_name[p]["head"][2] > toes:
                break
            if not all(by_name[x]["head"][2] <= toes for x in _descendants(p, kids)):
                break
            foot = p
        feet.add(foot)
    # a toe inside another foot's fan is not a foot of its own
    feet = {f for f in feet if not any(f in _descendants(o, kids) for o in feet if o != f)}
    raised = {f for f in raised if not any(f in _descendants(o, kids) or o in _descendants(f, kids) for o in feet)}
    root = next(b for b in bones if not b["parent"])
    mid_x = root["head"][0]

    def leg(foot: str) -> dict | None:
        chain = [foot]
        while True:
            p = by_name[chain[0]]["parent"]
            if not p or len(kids[p]) > 1:
                break
            chain.insert(0, p)
        if len(chain) < 3:
            return None  # a 2-bone "leg" (the giant frog's folded hind legs) flips under IK: it rides the body
        last = by_name[foot]
        fan = foot in fans or foot in raised
        tip_only = last["head"][2] > toes and not fan
        pts = [by_name[n]["head"] for n in chain] + ([last["tail"]] if tip_only else [])
        length = sum(_dist(a, b) for a, b in zip(pts, pts[1:]))
        drop = pts[0][2] - pts[-1][2]
        if length <= 0 or drop / length < MIN_DROP:
            return None
        run = pts[-1][1] - pts[0][1]
        if -run > TAIL_RUN * drop and abs(pts[-1][0] - mid_x) < TAIL_MID * height:
            return None  # backwards along the floor on the centre line: a tail
        if abs(pts[0][0] - mid_x) >= LEG_ROOT_SIDE * height and pts[0][2] >= 0.5 * height:
            return None  # starts out at the side: a wing whose tip rests on the floor (the dragons')
        if not fan:
            sole = [p for n in [foot, *_descendants(foot, kids)] for p in (by_name[n]["head"], by_name[n]["tail"])]
            span = max(math.hypot(p[0] - q[0], p[1] - q[1]) for p in sole for q in sole)
            if math.hypot(pts[-1][0] - pts[0][0], run) > drop or span > FOOT_SPAN * height:
                return None  # lies out along the floor: a curled tail, not a leg
        return {"chain": chain, "foot": foot, "shin": foot if tip_only else chain[-2], "level": not tip_only,
                "length": length, "drop": drop, "x": by_name[foot]["head"][0]}

    legs = sorted((g for g in map(leg, feet) if g), key=lambda g: g["drop"] / g["length"], reverse=True)
    cx = sum(b["head"][0] for b in bones) / len(bones)  # the skeleton's middle, not the feet's (3 on one side)
    def sole(g: dict) -> list:  # the lowest point of the foot and its toes
        return min((by_name[n][e] for n in [g["foot"], *_descendants(g["foot"], kids)] for e in ("head", "tail")),
                   key=lambda p: p[2])

    twins = []
    for g in legs:  # two chains on one leg (the green wyrmling's right hind: bone_9.. and bone_50.., soles 3 cm apart)
        if not any(_dist(sole(g), sole(o)) < SAME_FOOT * height for o in twins):
            twins.append(g)
    legs = twins
    picked, per_side = [], {"r": 0, "l": 0}
    for g in legs:
        s = "r" if g["x"] >= cx else "l"
        if per_side[s] < max(1, want // 2):
            picked.append(g)
            per_side[s] += 1
    picked += [g for g in legs if g not in picked][:max(0, want - len(picked))]
    picked = picked[:want]
    used = {n for g in picked for n in g["chain"]}
    for o in list(picked):  # a raised leg: the missing twin of a leg taken, off the same bone
        if len(picked) >= want:
            break
        par = by_name[o["chain"][0]]["parent"]
        if not par:
            continue
        px = by_name[par]["head"][0]
        oy = by_name[o["foot"]]["head"][1]
        if any((g["x"] - px) * (o["x"] - px) < 0 and by_name[g["chain"][0]]["parent"] == par for g in picked):
            continue  # it has its twin (a rearing dragon's hind feet stand level with its fore paws: not twins)
        for sib in kids[par]:
            if sib == o["chain"][0] or sib in used or _inserted_wing(sib) or _inserted_tail(sib):
                continue
            chain = longest_chain(sib, kids)
            cut = next((i for i, n in enumerate(chain) if n in fans or n in raised), None)
            chain = chain[:cut + 1] if cut is not None else chain
            if used & set(chain):
                continue  # runs on into a leg taken (the bulette's bone_22 carries bone_23..26)
            foot = chain[-1]
            fan = cut is not None
            last = by_name[foot]
            pts = [by_name[n]["head"] for n in chain] + ([] if fan else [last["tail"]])
            if len(pts) < 3:
                continue
            length = sum(_dist(p, q) for p, q in zip(pts, pts[1:]))
            drop = pts[0][2] - pts[-1][2]
            if length <= 0 or drop / length < MIN_DROP or pts[-1][2] > by_name[par]["head"][2] - 0.1 * height:
                continue
            if (last["head"][0] - px) * (o["x"] - px) >= 0 or abs(last["head"][1] - oy) >= TWIN_Y * height:
                continue  # same side, or not level with it front to back (the neck and head off the chest)
            picked.append({"chain": chain, "foot": foot, "shin": chain[-2] if fan else foot, "level": fan,
                           "length": length, "drop": drop, "x": last["head"][0]})
            used |= set(chain)
            break
    picked.sort(key=lambda g: g["drop"] / g["length"], reverse=True)
    for g in picked:
        del g["x"]
    return picked


def _find_legs(bones: list[dict], height: float, want: int, sprawl: bool = False,
               sprawl_drop: bool = False) -> list[dict]:
    by_name = {b["name"]: b for b in bones}
    kids = _children(bones)
    low = height * GROUND
    toes = height * TOES
    feet = set()
    for b in bones:
        if _inserted_wing(b["name"]) or _inserted_tail(b["name"]):
            continue  # a hanging inserted wing (the harpy's) or tail (the centaur's) can reach the ground band
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
        if sprawl and sprawl_drop and drop < 0:
            continue  # ends above its root: a tail over the back, a claw, a leg held up (find_legs)
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
    "hand" (None when the forearm is the last bone), "chain"}.

    Not arms: chains that branch off the head (hair, ears, a jaw: find_head) and hanging
    chains whose tip reaches the ground band (a tail or a skirt). A side with no hanging
    chain takes a raised one (the merfolk's sword arm) when it reaches less than
    WING_REACH x the height sideways: anything farther is what find_wings calls a wing, so
    a winged biped's raised wings never stand in for arms, and a side with a hanging arm
    never looks at raised chains at all."""
    by_name = {b["name"]: b for b in bones}
    kids = _children(bones)
    leg_bones = {n for g in legs for n in g["chain"]}
    cx = centre_x(bones, legs)
    head = find_head(bones, height, legs)
    on_head = {head, *_descendants(head, kids)} if head and by_name[head]["parent"] else set()
    best: dict[str, tuple] = {}
    for b in bones:
        if b["name"] in leg_bones or b["name"] in on_head or not b["parent"] or len(kids[b["parent"]]) < 2 \
                or _inserted_wing(b["name"]) or _inserted_tail(b["name"]):
            continue
        chain = longest_chain(b["name"], kids)
        tip = by_name[chain[-1]]["head"]
        if len(chain) < 3 or abs(tip[0] - cx) < 0.06 * height:
            continue
        if any(n in leg_bones or n in on_head for n in chain) or b["head"][2] < 0.4 * height:
            continue  # the neck, which runs on into the head, too
        raised = tip[2] >= b["head"][2]
        if raised and max(abs(by_name[n]["tail"][0] - cx) for n in chain) >= WING_REACH * height:
            continue
        if not raised and tip[2] < GROUND * height:
            continue
        side = "r" if tip[0] > cx else "l"
        pts = [by_name[n]["head"] for n in chain]
        length = sum(_dist(a, c) for a, c in zip(pts, pts[1:]))
        if side not in best or (raised, length) < best[side][:2]:
            best[side] = (raised, length, chain)
    arms = []
    for side, (_, _, chain) in best.items():
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


def path_to_root(name: str, bones: list[dict]) -> list[str]:
    """[root ... name], following parents."""
    by_name = {b["name"]: b for b in bones}
    out = [name]
    while by_name[out[0]]["parent"]:
        out.insert(0, by_name[out[0]]["parent"])
    return out


def find_tail(bones: list[dict], height: float, legs: list[dict], min_bones: int = 4,
              ground: bool = False) -> list[str]:
    """Longest chain that leaves the hips backwards, stays low and is not a leg (forward is
    +Y). Only meaningful for horizontal bodies: on an upright plan that test matches an arm.
    `min_bones` 4 for the clips (a stub is not worth swaying); roll scoring counts shorter.

    `ground`: when no such chain exists, a chain of 2+ bones that leaves the hips backwards
    and slopes down into the ground band (its end within GROUND x the height of the
    floor), the farthest-reaching first. A bird's tail fan resting on the ground (the
    giant eagle's 2-bone fan) is one; the walk then turns it up at its root instead of
    raising the whole body.

    A chain the rig stage inserted or adopted (`tail_00 ..`, blender_tail_chain.py) is the
    tail by name, whatever its shape (the centaur's hangs straight down to the floor).

    Never a limb (clip audit 2026-11-06): a chain whose first bone has a leg under it starts
    at the pelvis, not the tail (the giant weasel's bone_24 carries the tail and the right
    hind leg, so the tail's sway swung the leg); the tail is the chain from that branch on.
    A chain with a joint WING_REACH x the height out to the side above the ground band is a
    sprawled limb (the giant toad's right hind leg, 0.53 x the height out at hip height,
    also taken for a wing); a tail lying curled on the floor (the red dragon's, 0.42 out at
    0.05 x the height) is still a tail. A tail leaves the body above the ground band (GROUND x
    the height; the red dragon's lying tail roots at 0.23)."""
    by_name = {b["name"]: b for b in bones}
    kids = _children(bones)
    if TAIL_PREFIX + "00" in by_name:
        return longest_chain(TAIL_PREFIX + "00", kids)
    leg_bones = {n for g in legs for n in g["chain"]}
    if not legs:
        return []
    leg_roots = {g["chain"][0] for g in legs}
    hip_y = sum(by_name[g["chain"][0]]["head"][1] for g in legs) / len(legs)
    cx = centre_x(bones, legs)
    best, sloped, reach = [], [], 0.0
    for b in bones:
        if b["name"] in leg_bones or not b["parent"] or len(kids[b["parent"]]) < 2:
            continue  # starts at a branch, so the spine above the tail is not part of it
        chain = longest_chain(b["name"], kids)
        if len(chain) < 2 or any(n in leg_bones for n in chain):
            continue
        if b["head"][2] <= GROUND * height:
            continue  # rooted in the ground band: a foot or toe piece (the toad's bone_20 at 0.16 x h)
        if leg_roots & set(_descendants(b["name"], kids)):
            continue  # a leg hangs under it: the pelvis, the tail starts below the branch
        if any(abs(p[0] - cx) >= WING_REACH * height and p[2] > GROUND * height
               for n in chain for p in (by_name[n]["head"], by_name[n]["tail"])):
            continue  # out to the side above the ground: a sprawled limb, not a tail
        h0, h1 = by_name[chain[0]]["head"], by_name[chain[-1]]["head"]
        if len(chain) >= min_bones and h1[1] < hip_y - 0.05 * height and h1[2] < h0[2] + 0.2 * height \
                and len(chain) > len(best):
            best = chain
        end = by_name[chain[-1]]["tail"]
        if ground and end[1] < hip_y - 0.05 * height and end[2] <= GROUND * height < h0[2] \
                and hip_y - end[1] > reach:
            sloped, reach = chain, hip_y - end[1]
    return best or sloped


STING_TOP = 0.5     # find_sting: a scorpion's tail rises over its back to this x the height or more
STING_MID = 0.15    # ... from a root this close (x height) to the centre line, behind the legs' hips,
STING_TIP = 0.3     # ... and ends this close to it (a leg reaches out to the side)
CLAW_FRONT = 0.1    # find_claws: a claw ends this far (x height) or more in front of the front-most foot


def find_sting(bones: list[dict], height: float, legs: list[dict]) -> list[str]:
    """A sprawled body's tail raised over its back (a scorpion's, with the sting at its end):
    the longest chain of 4+ bones off a branch, not a leg, rooted behind the legs' mean hip
    and within STING_MID x the height of the centre line, that rises to STING_TOP x the height
    and ends within STING_TIP x the height of the centre line (the giant wolf spider's hind leg,
    whose bones zigzag up over the body, ends 1.2 x the height out to the side). find_tail
    wants a tail that stays low; the anim stage strikes with this one."""
    if not legs:
        return []
    by_name = {b["name"]: b for b in bones}
    kids = _children(bones)
    leg_bones = {n for g in legs for n in g["chain"]}
    hip_y = sum(by_name[g["chain"][0]]["head"][1] for g in legs) / len(legs)
    cx = centre_x(bones, legs)
    best = []
    for b in bones:
        if b["name"] in leg_bones or not b["parent"] or len(kids[b["parent"]]) < 2:
            continue
        chain = longest_chain(b["name"], kids)
        if len(chain) < 4 or any(n in leg_bones for n in chain):
            continue
        if b["head"][1] >= hip_y or abs(b["head"][0] - cx) > STING_MID * height:
            continue
        if max(by_name[n]["tail"][2] for n in chain) < STING_TOP * height \
                or abs(by_name[chain[-1]]["tail"][0] - cx) > STING_TIP * height:
            continue
        if len(chain) > len(best):
            best = chain
    return best


def find_claws(bones: list[dict], height: float, legs: list[dict], skip: set = frozenset()) -> list[list[str]]:
    """A sprawled body's claws (a scorpion's pincers, its mouth parts): chains of 3+ bones off a
    branch, not legs nor in `skip`, that reach CLAW_FRONT x the height or more in front of the
    front feet (their mean, the feet in front of all the feet's mean). None on the spiders (their
    palps are 2 bones or behind the front feet)."""
    if not legs:
        return []
    by_name = {b["name"]: b for b in bones}
    kids = _children(bones)
    leg_bones = {n for g in legs for n in g["chain"]}
    ys = [by_name[g["foot"]]["head"][1] for g in legs]
    fy = [y for y in ys if y >= sum(ys) / len(ys)]
    front = sum(fy) / len(fy)
    out = []
    for b in bones:
        n = b["name"]
        if n in leg_bones or n in skip or not b["parent"] or len(kids[b["parent"]]) < 2 \
                or b["parent"] in leg_bones:
            continue
        chain = longest_chain(n, kids)
        if len(chain) < 3 or any(x in leg_bones or x in skip for x in chain):
            continue
        if any(n in o for o in out):
            continue
        if max(by_name[x]["tail"][1] for x in chain) >= front + CLAW_FRONT * height:
            out.append(chain)
    return [c for c in out if not any(c[0] in o[1:] for o in out)]


RAISED_SIDE = 0.25  # find_wings(raised=True): a raised wing's tip is this far out (x height) at least ...
RAISED_TOP = 0.5    # ... and above this share of the height


def find_wings(bones: list[dict], height: float, legs: list[dict], arms: list[dict] = (),
               exclude: tuple = (), raised: bool = False) -> list[dict]:
    """Per side the chain that leaves the upper body near the centre line and reaches
    farthest sideways, at least 0.4 x the height (and is not a leg, an arm or an
    `exclude`d bone such as the head, which rules out a neck that bends sideways):
    {"side", "chain", "reach"}. Ties go to
    the shorter chain, so a chain that starts on the spine and only then turns into the
    wing loses to the wing's own root. Facing +Y, +Z up, the right side is +X.

    Chains the rig stage inserted (`wing_<side>_00..`, blender_wing_weights.py) are wings
    by name and win their side, wherever they reach. A chain that branches off an arm
    bone (SkinTokens sometimes hangs the wing on the shoulder or upper arm) may start
    farther than 0.2 x the height from the centre line; the arm itself stays the arm.

    `raised` (the clips on winged plans): a side with no such chain takes the chain whose tip
    is held up over the back: RAISED_SIDE x the height out, above RAISED_TOP of it, and
    WING_REACH from the chain's root out and up (the ancient red dragon's wings reach 0.38 x
    the height sideways and rise 0.65 above their roots). Sides found the strict way keep
    their chain."""
    by_name = {b["name"]: b for b in bones}
    kids = _children(bones)
    arm_bones = {n for a in arms for n in a["chain"]}
    skip = {n for g in legs for n in g["chain"]} | arm_bones | set(exclude)
    cx = centre_x(bones, legs)
    cands = []
    for b in bones:
        if b["name"] in skip or not b["parent"] or len(kids[b["parent"]]) < 2:
            continue
        chain = longest_chain(b["name"], kids)
        tip = max((by_name[n]["tail"] for n in chain), key=lambda p: abs(p[0] - cx))
        reach = abs(tip[0] - cx)
        if _inserted_wing(b["name"]):
            if not _inserted_wing(b["parent"]):
                side = b["name"][len(WING_PREFIX)]
                cands.append({"side": side if side in "rl" else "r" if tip[0] > cx else "l", "chain": chain,
                              "reach": reach, "inserted": True})
            continue
        on_arm = b["parent"] in arm_bones
        if b["head"][2] < 0.25 * height or (abs(b["head"][0] - cx) > 0.2 * height and not on_arm):
            continue
        if len(chain) < 3 or any(n in skip for n in chain):
            continue
        if reach < WING_REACH * height:
            if raised and reach >= RAISED_SIDE * height:
                z0 = min(min(x["head"][2], x["tail"][2]) for x in bones)
                top = max((by_name[n]["tail"] for n in chain), key=lambda p: p[2])
                rise = top[2] - b["head"][2]
                if top[2] - z0 >= RAISED_TOP * height and math.hypot(reach, max(rise, 0.0)) >= WING_REACH * height:
                    cands.append({"side": "r" if tip[0] > cx else "l", "chain": chain, "reach": reach, "raised": True})
            continue
        cands.append({"side": "r" if tip[0] > cx else "l", "chain": chain, "reach": reach})
    wings = []
    for side in ("r", "l"):
        mine = [c for c in cands if c["side"] == side]
        strict = [c for c in mine if not c.get("raised")]
        mine = strict if strict or any(c.get("inserted") for c in mine) else \
            [{k: v for k, v in c.items() if k != "raised"} for c in mine]
        ins = [c for c in mine if c.get("inserted")]
        if ins:
            wings.append({k: v for k, v in ins[0].items() if k != "inserted"})
        elif mine:
            top = max(c["reach"] for c in mine)
            wings.append(min((c for c in mine if c["reach"] >= 0.95 * top), key=lambda c: len(c["chain"])))
    return wings


def find_snout(bones: list[dict], height: float, legs: list[dict]) -> str | None:
    """Head of a horizontal body (forward is +Y): the bone reaching farthest forward off
    the legs and above a third of the height. find_head's highest-bone rule picks a hip
    or a raised wing there. Wing chains are not the head: a bone off the centre line
    (midpoint more than SNOUT_SIDE x the height from it) on a chain that branches off the
    body and reaches WING_REACH x the height sideways (the giant eagle's left wing tip,
    bone_11, which reaches 0.6 m farther forward than its beak). A leaf whose parent also
    carries other leaves (a jaw beside an ear) gives way to that parent, the skull."""
    by_name = {b["name"]: b for b in bones}
    kids = _children(bones)
    leg_bones = {n for g in legs for n in g["chain"]}
    cx = centre_x(bones, legs)
    wing = set()
    for b in bones:  # chains from a branch point that reach a wing's span sideways
        if not b["parent"] or len(kids[b["parent"]]) < 2 or b["name"] in leg_bones:
            continue
        chain = [b["name"]] + _descendants(b["name"], kids)
        if max(abs(by_name[n]["tail"][0] - cx) for n in chain) >= WING_REACH * height:
            wing |= {n for n in chain
                     if abs((by_name[n]["head"][0] + by_name[n]["tail"][0]) / 2 - cx) > SNOUT_SIDE * height}
    cands = [b for b in bones if b["name"] not in leg_bones and b["parent"] and not _inserted_wing(b["name"])
             and (b["head"][2] + b["tail"][2]) / 2 > height / 3]
    cands = [b for b in cands if b["name"] not in wing] or cands
    if not cands:
        return None
    top = max(cands, key=lambda b: max(b["head"][1], b["tail"][1]))
    p = top["parent"]
    if not kids[top["name"]] and p and len(kids[p]) > 1 and all(not kids[c] for c in kids[p]):
        return p
    return top["name"]


# --- skeleton quality (roll scoring, blender_rig.py "score") -----------------------------

def common_ancestor(names: list[str], bones: list[dict]) -> str | None:
    """Deepest bone that every name descends from (or is)."""
    paths = [path_to_root(n, bones) for n in names]
    if not paths:
        return None
    out = None
    for level in zip(*paths):
        if any(x != level[0] for x in level):
            break
        out = level[0]
    return out


def path_between(a: str, b: str, bones: list[dict]) -> list[str]:
    """[a ... b] through the tree (up from a to the common ancestor, down to b); [] if the
    two are in different trees."""
    pa, pb = path_to_root(a, bones), path_to_root(b, bones)
    if pa[0] != pb[0]:
        return []
    k = 0
    while k < min(len(pa), len(pb)) and pa[k] == pb[k]:
        k += 1
    return pa[k - 1:][::-1] + pb[k:]


def leg_sides(bones: list[dict], legs: list[dict], cx: float, height: float) -> dict:
    """Feet per side of the centre line x = cx (+X is the right) and how far the feet are
    from mirrored pairs: mean distance from each foot's mirror image to the nearest foot on
    the other side, as a fraction of the height (0 = every foot has a twin)."""
    by_name = {b["name"]: b for b in bones}
    feet = [by_name[g["foot"]]["head"] for g in legs]
    r = [p for p in feet if p[0] >= cx]
    l = [p for p in feet if p[0] < cx]
    errs = []
    for mine, other in ((r, l), (l, r)):
        for p in mine:
            m = (2 * cx - p[0], p[1], p[2])
            errs.append(min((_dist(m, q) for q in other), default=height) / height)
    return {"r": len(r), "l": len(l), "mirror": sum(errs) / len(errs) if errs else 1.0}


def find_spine(bones: list[dict], height: float, legs: list[dict], arms: list[dict] = (),
               head: str | None = None, upright: bool = False) -> dict:
    """The trunk between the limbs. hip: where the hind legs (upright: the legs) branch off,
    the common ancestor of their first bones' parents; shoulder: the same for the front
    legs (upright: the arms). torso: the bones on the path hip -> shoulder (both ends
    included, so a skeleton whose legs all hang off one root bone has a torso of 1); neck:
    the head's ancestors below the shoulder, down to the head. A good SkinTokens quadruped has 3-5 torso bones (wolf), the owlbear's
    spineless roll has 1. Forward is +Y. Keys are None when a part is missing."""
    by_name = {b["name"]: b for b in bones}

    def branch(chains: list[list[str]]) -> str | None:
        parents = [by_name[c[0]]["parent"] for c in chains if by_name[c[0]]["parent"]]
        return common_ancestor(parents, bones) if parents else None

    if upright:
        hind, front = [g["chain"] for g in legs], [a["chain"] for a in arms]
    else:
        ys = [by_name[g["foot"]]["head"][1] for g in legs]
        mid = sum(ys) / len(ys) if ys else 0.0
        hind = [g["chain"] for g, y in zip(legs, ys) if y < mid]
        front = [g["chain"] for g, y in zip(legs, ys) if y >= mid]
    hip, shoulder = branch(hind), branch(front)
    torso = path_between(hip, shoulder, bones) if hip and shoulder else []
    top = shoulder or hip
    # only the part of the head's own path below the trunk: a head hung off the root
    # beside the trunk (the owlbear) has a neck of its own bones, not the trunk's
    neck = [n for n in path_to_root(head, bones) if n not in path_to_root(top, bones)] if top and head else []
    return {"hip": hip, "shoulder": shoulder, "torso": torso, "neck": neck,
            "torso_length": sum(_dist(by_name[a]["head"], by_name[b]["head"]) for a, b in zip(torso, torso[1:])) / height}

"""Standard quadruped skeleton: rename a quadruped's SkinTokens bones (bone_N) to canonical names.

    python3 -I tools/quadcanon.py wolf warhorse ...       # map, gate, rename (game files + clips)
    python3 -I tools/quadcanon.py --all --jobs 8 --dry-run # map and gate only: canon report, nothing renamed
    python3 -I tools/quadcanon.py wolf --undo             # back to bone_N (canon.json's map, inverted)
    (forge.py canon <id> runs the same; the rig stage runs it after every quadruped rig)

The skeleton and its weights are not touched: only the names of the joints change, in the glTF JSON
chunk of model_rigged.glb, its LODs and every clip in anims/ (a glTF skin binds joints by index and
a clip's channels target nodes by index, so the binary buffer is copied byte for byte). Blender names
vertex groups after the joints, Godot binds clip tracks by bone name: both follow the new names.
The anim stage's sidecars (anims/*.json, manifest.anim) are renamed with the same map, so a renamed
monster needs no new clips.

The parts come from blender_clips.py's own search (job parts_only: the legs, head, neck, spine,
tail and wings the clips would drive, after its topology gate), so a renamed rig gives the clips the
same parts (blender_clips.py reads them from the names: CANON). Names, Z up, +Y forward, +X right
(the creature's right; glTF: -Z forward):

- trunk: `pelvis` (where the hind legs branch: their chains' common ancestor), `spine_01..N`
  (pelvis -> chest, from the hips forward, whichever way the hierarchy runs), `chest` (where the
  front legs leave the root-to-head path), `neck_01..N` (chest -> head), `head` (the skull),
  `snout` (a leaf past the skull the clips lunge with: jaw or muzzle, optional). `root` only when
  the top bone is none of these (the top bone is the body transform, wherever it sits).
- legs: `<leg>_<slot>`, leg = front_l | front_r | hind_l | hind_r; from the body down:
  `shoulder` / `hip` (girdle; `_2`, `_3` for further bones above it, numbered from the top),
  `upper`, `lower`, `ankle` (metacarpus / metatarsus: only when the bone above the foot starts in
  the lower half of the leg), `foot` (the IK end), then `<leg>_toe_NN` for every bone below the foot.
- `tail_01..N` (root to tip), `wing_l_01..N`, `wing_r_01..N` (the chain the fly clip beats).
- everything else (ears, horns, jaw pieces, wing fingers, membrane bones, zero-length helpers):
  `<nearest canonical ancestor>_x<n>`. Socket bones (manifest rig.sockets) keep their names.

Gate (`status` in canon.json): fail = not renamed (fewer or more than one leg per quadrant, no head,
the head off the centre line, a name conflict); partial = renamed, with `problems` (a winged plan
with fewer than 2 wings, a tail the clips drop, no neck or no spine bone); ok = renamed, nothing
missing. canon.json also holds the measures a chain retarget needs (per leg: chain, bone lengths,
hip height, foot position; spine/neck/tail/wing chains and lengths).
Override `rig_canon: false` opts a monster out. docs/rig-anim.md "Standard quadruped skeleton".
"""
import argparse
import json
import math
import os
import re
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import overrides  # noqa: E402
import rig_topology as rt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "srd-5.2.1"
BLENDER = os.environ.get("BLENDER", "/usr/bin/blender")
CLIPS = ROOT / "tools" / "blender_clips.py"
PLANS = {"quadruped", "winged_quadruped"}
WINGED = {"winged_quadruped"}
HEAD_SIDE = 0.15   # the head's midpoint at most this x the height off the feet's centre line
QUAD_GAP = 0.1     # the front pair's leg tops at least this x the height ahead of the hind pair's
ANKLE = 0.5        # the bone above the foot is an ankle when it starts below this share of the leg's height
GIRDLE = {"front": "shoulder", "hind": "hip"}
SPEC = {
    "version": 1,
    "frame": "Blender world: Z up, +Y forward, +X the creature's right (glTF: Y up, -Z forward, +X right)",
    "trunk": ["root?", "pelvis", "spine_01..N", "chest", "neck_01..N", "head", "snout?"],
    "leg": ["<g>[_2..]", "upper", "lower", "ankle?", "foot", "toe_01..N?"],
    "legs": list(rt.CANON_LEGS),
    "chains": ["tail_01..N", "wing_l_01..N", "wing_r_01..N"],
    "extras": "<canonical ancestor>_x<n>",
}


# --- glb ------------------------------------------------------------------------------------

def read_glb(path: Path) -> tuple[dict, bytes]:
    data = path.read_bytes()
    magic, ver, _ = struct.unpack_from("<4sII", data, 0)
    n, kind = struct.unpack_from("<II", data, 12)
    if magic != b"glTF" or kind != 0x4E4F534A:
        raise ValueError(f"{path}: not a glb")
    return json.loads(data[20:20 + n]), data[20 + n:]


def write_glb(path: Path, g: dict, rest: bytes) -> None:
    js = json.dumps(g, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    out = struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(js) + len(rest)) + struct.pack("<II", len(js), 0x4E4F534A)
    tmp = path.with_suffix(".glb.tmp")
    tmp.write_bytes(out + js + rest)
    tmp.replace(path)


def joint_names(path: Path) -> list[str]:
    g, _ = read_glb(path)
    return [g["nodes"][i].get("name", "") for i in g["skins"][0]["joints"]] if g.get("skins") else \
        [n.get("name", "") for n in g["nodes"]]


def rename_glb(path: Path, names: dict) -> int:
    """Rename the nodes whose name is a key of `names` (joints; in a clip file, the armature's
    bones). Returns how many were renamed; the binary chunk is not touched."""
    g, rest = read_glb(path)
    n = 0
    for node in g["nodes"]:
        if node.get("name") in names:
            node["name"] = names[node["name"]]
            n += 1
    if n:
        write_glb(path, g, rest)
    return n


def rename_json(v, names: dict):
    """Every string equal to an old bone name, in any JSON value (anim sidecars, manifest.anim)."""
    if isinstance(v, str):
        return names.get(v, v)
    if isinstance(v, list):
        return [rename_json(x, names) for x in v]
    if isinstance(v, dict):
        return {k: rename_json(x, names) for k, x in v.items()}
    return v


# --- parts ----------------------------------------------------------------------------------

def registry() -> dict:
    return {m["id"]: m for m in json.loads((ROOT / "data" / "registry" / f"{SOURCE}.json").read_text())["monsters"]}


def detect(mon: dict, d: Path, blender: str = BLENDER) -> dict:
    """blender_clips.py parts_only on model_rigged.glb: the parts the clips would drive."""
    import anim
    rep = d / "canon_parts.tmp.json"
    job = {**anim.clip_job(mon, d, d / "anims", rep), "parts_only": True}
    p = subprocess.run([blender, "-b", "--factory-startup", "--python-exit-code", "1", "-P", str(CLIPS), "--",
                        json.dumps(job)], capture_output=True, text=True, timeout=900)
    if not rep.exists():
        raise RuntimeError(f"{mon['id']}: parts_only failed rc={p.returncode}: {(p.stdout + p.stderr)[-1500:]}")
    parts = json.loads(rep.read_text())
    rep.unlink()
    return parts


def _slots(chain: list[str], by_name: dict, end: str) -> list[str]:
    """Slot names for a leg chain [top .. foot] (see the module doc)."""
    above = chain[:-1]
    k = len(above)
    z0 = by_name[chain[-1]]["head"][2]
    ztop = by_name[chain[0]]["head"][2]
    low = []  # bottom up
    if k >= 3 and (by_name[above[-1]]["head"][2] - z0) < ANKLE * max(ztop - z0, 1e-6):
        low.append("ankle")
    low += ["lower", "upper"][:max(0, k - len(low))] if k >= 2 else ["upper"]
    rest = k - len(low)
    top = [end] + [f"{end}_{i}" for i in range(2, rest + 1)] if rest else []
    return top + low[::-1] + ["foot"]


def plan(mon: dict, parts: dict, kept: set) -> dict:
    """The rename map and the gate verdict from parts_only's answer."""
    bones = parts["bones"]
    by_name = {b["name"]: b for b in bones}
    kids = rt._children(bones)
    h = parts["height"]
    fail, problems = [], []
    if parts.get("canon"):
        return {"status": "fail", "problems": ["already canonical"], "names": {}}
    quad, changes = {}, []
    legs = parts["legs"]
    if parts.get("partial_legs") or parts.get("stand_fallback") or len(legs) != 4:
        fail.append(f"{len(legs)} legs found" + (": the clips walk on a partial leg set" if parts.get("partial_legs")
                                                 else ""))
    else:
        # quadrants from the legs' joints (centroid below the chain's first bone): the two farthest
        # forward are the front pair, then left (-X) / right in each pair. The clips' own split (feet against their mean y and
        # x) mislabels a foot crossed under the body or a stride's planted foot.
        def centroid(g):  # the leg's joints below its first bone (which often starts on the trunk)
            pts = [by_name[n]["head"] for n in g["chain"][1:]]
            return [sum(p[i] for p in pts) / len(pts) for i in range(3)]

        top = {id(g): centroid(g) for g in legs}
        order = sorted(legs, key=lambda g: -top[id(g)][1])
        gap = min(top[id(g)][1] for g in order[:2]) - max(top[id(g)][1] for g in order[2:])
        if gap < QUAD_GAP * h:
            fail.append(f"front and hind legs {gap / h:.2f} x h apart front to back (< {QUAD_GAP}): "
                        "the four chains are not two pairs")
        for end, pair in (("front", order[:2]), ("hind", order[2:])):
            l, r = sorted(pair, key=lambda g: top[id(g)][0])
            for side, g in (("l", l), ("r", r)):
                key = f"{end}_{side}"
                quad[key] = g
                if (g["front"], g["side"]) != (end == "front", side):
                    fail.append(f"{g['chain'][0]}.. was {'front' if g['front'] else 'hind'}_{g['side']} in the "
                                   f"clips, is {key} by its joints: the clips' grouping is not the anatomical one (re-rig, or a "
                                f"look at the gait; renaming would change the clips)")
    head, skull = parts.get("head"), parts.get("skull")

    def off(n):
        b = by_name[n]
        return abs((b["head"][0] + b["tail"][0]) / 2 - parts["cx"]) / h

    if not head:
        fail.append("no head")
    elif off(head) > HEAD_SIDE:
        if skull and skull != head and off(skull) <= HEAD_SIDE:
            changes.append(f"the clips' head {head} is {off(head):.2f} x h off the centre line (an ear or a horn): "
                           f"the head is its parent {skull}")
            head = skull
        else:
            fail.append(f"head {head} {off(head):.2f} x h off the centre line")
    if head and any(n.startswith(rt.WING_PREFIX) for n in rt.path_to_root(head, bones)):
        fail.append(f"the path to the head {head} runs through a wing chain")
    if fail:
        return {"status": "fail", "problems": fail, "names": {}}
    # neck / spine as blender_clips.neck_and_spine cuts them, with the front pair above
    path = rt.path_to_root(head, bones)
    cut = max((max((i for i, n in enumerate(path) if n in rt.path_to_root(quad[k]["chain"][0], bones)), default=-1)
               for k in ("front_l", "front_r")), default=-1)
    if cut < 0:
        cut = max(0, len(path) - 3)
    neck_path, spine_path = path[cut + 1:] or [head], path[:cut + 1]
    if not kids[head] and by_name[head]["parent"] in neck_path[:-1]:
        skull = by_name[head]["parent"]
    else:
        skull = head
    if (neck_path, spine_path) != (parts["neck"], parts["spine"]) and not changes:
        changes.append("neck/spine split differs from the clips'")
    parts = {**parts, "neck": neck_path, "spine": spine_path}

    names: dict[str, str] = {}

    def put(old: str, new: str) -> None:
        if old in names and names[old] != new:
            problems.append(f"name conflict: {old} is {names[old]} and {new}")
        names[old] = new

    for s in kept:  # sockets keep their names
        names[s] = s
    if skull and skull != head:
        put(skull, "head")
        put(head, "snout")
    else:
        put(head, "head")
    neck = [n for n in parts["neck"] if n not in (head, skull)]
    for i, n in enumerate(neck, 1):
        put(n, f"neck_{i:02d}")
    root = parts["root"]
    chest = parts["spine"][-1] if parts["spine"] else root
    hp = [by_name[quad[k]["chain"][0]]["parent"] for k in ("hind_l", "hind_r")]
    pelvis = rt.common_ancestor([p for p in hp if p], bones) if all(hp) else None
    if pelvis is None:
        problems.append("hind legs share no ancestor")
        pelvis = chest
    if pelvis == chest:
        problems.append("pelvis is the chest bone (one trunk bone)")
        put(chest, "chest")
        spine = []
    else:
        put(pelvis, "pelvis")
        put(chest, "chest")
        spine = rt.path_between(pelvis, chest, bones)[1:-1]
        for i, n in enumerate(spine, 1):
            put(n, f"spine_{i:02d}")
    if not neck:
        problems.append("no neck bone (head on the chest)")
    legs_out = {}
    for key in rt.CANON_LEGS:
        g = quad[key]
        for n, slot in zip(g["chain"], _slots(g["chain"], by_name, GIRDLE[key.split("_")[0]])):
            put(n, f"{key}_{slot}")
        for i, n in enumerate(rt._descendants(g["foot"], kids), 1):
            put(n, f"{key}_toe_{i:02d}")
        legs_out[key] = g
    # the tail before the gate: a tail the gate drops for lying on the floor is still the tail (the
    # clips' gate drops it again); one that shares a leg's or a wing's bones is not named
    tail = parts.get("tail_found") or parts["tail"]
    taken = set(names)
    if tail and (set(tail) & taken or any(n in {x for w in parts["wings"].values() for x in w} for n in tail)):
        problems.append(f"tail {tail[0]}..{tail[-1]} overlaps another part: not named")
        tail = []
    if tail and tail != parts["tail"]:
        problems.append("the clips' topology gate drops the tail (" + "; ".join(
            w for w in parts.get("warnings", []) if w.startswith("tail")) + ")")
    for i, n in enumerate(tail, 1):
        put(n, f"tail_{i:02d}")
    wings = {}
    for s, ch in sorted(parts["wings"].items()):
        if set(ch) & set(names):
            problems.append(f"wing {s} overlaps another part: not named")
            continue
        for i, n in enumerate(ch, 1):
            put(n, f"wing_{s}_{i:02d}")
        wings[s] = ch
    if mon["body_plan"] in WINGED and len(wings) < 2:
        problems.append(f"{len(wings)} of 2 wings found ({sorted(wings) or 'none'})")
    if root not in names:
        put(root, "root")
    # extras: nearest canonical ancestor + _x<n>, depth first from the root
    count: dict[str, int] = {}

    def walk(n: str, anchor: str) -> None:
        if n not in names:
            count[anchor] = count.get(anchor, 0) + 1
            names[n] = f"{anchor}_x{count[anchor]}"
            nxt = anchor
        else:
            nxt = names[n]
        for c in kids[n]:
            walk(c, nxt)

    walk(root, "root")
    if len(set(names.values())) != len(names):
        dup = sorted({v for v in names.values() if list(names.values()).count(v) > 1})
        return {"status": "fail", "problems": problems + [f"duplicate names {dup}"], "names": {}}
    if any(problems_fatal(p) for p in problems):
        return {"status": "fail", "problems": problems, "names": {}}
    status = "partial" if problems or changes else "ok"
    return {"status": status, "problems": problems, "changes": changes, "names": {k: v for k, v in names.items() if k != v},
            "measures": measures(parts, names, legs_out, tail, wings, pelvis, chest)}


def problems_fatal(p: str) -> bool:
    return p.startswith("name conflict")


def measures(parts: dict, names: dict, legs: dict, tail: list, wings: dict, pelvis: str, chest: str) -> dict:
    """What a chain retarget needs (Blender world, metres, Z up, +Y forward)."""
    by_name = {b["name"]: b for b in parts["bones"]}
    r = lambda v: [round(x, 4) for x in v]  # noqa: E731

    def chain_len(ch: list, tip: bool = True) -> list:
        pts = [by_name[n]["head"] for n in ch] + ([by_name[ch[-1]]["tail"]] if tip and ch else [])
        return [round(math.dist(a, b), 4) for a, b in zip(pts, pts[1:])]

    out = {"height": round(parts["height"], 4), "centre_x": round(parts["cx"], 4),
           "pelvis": r(by_name[pelvis]["head"]), "chest": r(by_name[chest]["head"]), "legs": {}}
    for key, g in legs.items():
        ch = g["chain"]
        foot = by_name[g["foot"]]
        out["legs"][key] = {"chain": [names[n] for n in ch], "bone_lengths": chain_len(ch, False),
                            "length": round(sum(chain_len(ch, False)), 4),
                            "hip_height": round(by_name[ch[0]]["head"][2], 4), "top": r(by_name[ch[0]]["head"]),
                            "foot": r(foot["head"]), "foot_tip": r(foot["tail"]), "level": g["level"]}
    for nm, ch in (("spine", [n for n, v in names.items() if re.match(r"^spine_\d\d$", v)]),
                   ("neck", [n for n in parts["neck"] if n in names and names[n].startswith("neck_")]),
                   ("tail", tail), *((f"wing_{s}", ch) for s, ch in sorted(wings.items()))):
        ch = sorted(ch, key=lambda n: names[n])
        out[nm] = {"chain": [names[n] for n in ch], "bone_lengths": chain_len(ch),
                   "length": round(sum(chain_len(ch)), 4)}
    return out


# --- apply ----------------------------------------------------------------------------------

def game_files(d: Path) -> list[Path]:
    return sorted(d.glob("model_rigged*.glb")) + sorted((d / "anims").glob("*.glb")) + \
        sorted((d / "anims_proc").glob("*.glb"))


def rename_all(d: Path, names: dict) -> dict:
    """Rename every game file and the anim sidecars with `names` (old -> new)."""
    n = {str(p.relative_to(d)): rename_glb(p, names) for p in game_files(d)}
    for p in list((d / "anims").glob("*.json")) + list((d / "anims_proc").glob("*.json")):
        p.write_text(json.dumps(rename_json(json.loads(p.read_text()), names), indent=1))
    mp = d / "manifest.json"
    man = json.loads(mp.read_text())
    if man.get("anim"):
        man["anim"] = rename_json(man["anim"], names)
    mp.write_text(json.dumps(man, indent=1))
    return n


def run(mid: str, root: Path, dry: bool = False, blender: str = BLENDER, mon: dict | None = None) -> dict:
    mon = mon or registry()[mid]
    d = root / mid
    if mon["body_plan"] not in PLANS:
        return {"id": mid, "status": "skip", "problems": [f"plan {mon['body_plan']}"]}
    if overrides.load(mon["source"], mid).get("rig_canon") is False:
        return {"id": mid, "status": "skip", "problems": ["override rig_canon false"]}
    if not (d / "model_rigged.glb").exists():
        return {"id": mid, "status": "skip", "problems": ["not rigged"]}
    man = json.loads((d / "manifest.json").read_text())
    if (man.get("rig") or {}).get("tool") not in ("skintokens", None):
        return {"id": mid, "status": "skip", "problems": [f"rig tool {man['rig'].get('tool')}"]}
    if all(f"{g}_foot" in joint_names(d / "model_rigged.glb") for g in rt.CANON_LEGS):
        return {"id": mid, "status": "skip", "problems": ["already canonical"]}
    kept = set((man.get("rig") or {}).get("sockets") or {})
    parts = detect(mon, d, blender)
    res = {"id": mid, **plan(mon, parts, kept)}
    res["joints"] = len(parts["bones"])
    out = {"spec": SPEC, **{k: v for k, v in res.items() if k != "id"}, "applied": False}
    if not dry and res["status"] in ("ok", "partial"):
        out["files"] = rename_all(d, res["names"])
        out["applied"] = True
        man = json.loads((d / "manifest.json").read_text())
        man.setdefault("rig", {})["canon"] = {"status": res["status"], "problems": res["problems"],
                                             "file": "canon.json"}
        (d / "manifest.json").write_text(json.dumps(man, indent=1))
    if not dry:
        (d / "canon.json").write_text(json.dumps(out, indent=1))
    res["applied"] = out["applied"]
    return res


def undo(mid: str, root: Path) -> str:
    d = root / mid
    c = json.loads((d / "canon.json").read_text())
    if not c.get("applied"):
        return f"{mid}: not applied"
    inv = {v: k for k, v in c["names"].items()}
    rename_all(d, inv)
    c["applied"] = False
    (d / "canon.json").write_text(json.dumps(c, indent=1))
    man = json.loads((d / "manifest.json").read_text())
    (man.get("rig") or {}).pop("canon", None)
    (d / "manifest.json").write_text(json.dumps(man, indent=1))
    return f"{mid}: back to bone_N ({len(inv)} names)"


def stage(fg, mon: dict, d: Path) -> None:
    """After the rig stage (rig.stage_rig): rename a fresh quadruped rig, unless opted out."""
    if mon["body_plan"] not in PLANS:
        return
    r = run(mon["id"], d.parent, blender=fg.BLENDER, mon=mon)
    fg.log(f"{mon['id']}: canon {r['status']}" + (f" ({'; '.join(r['problems'])})" if r.get("problems") else "")
           + (", renamed" if r.get("applied") else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--all", action="store_true", help="every rigged quadruped / winged quadruped")
    ap.add_argument("--root", default=str(ROOT / "out" / SOURCE))
    ap.add_argument("--dry-run", action="store_true", help="map and gate only, nothing written")
    ap.add_argument("--undo", action="store_true", help="rename back to bone_N from canon.json")
    ap.add_argument("--jobs", type=int, default=4, help="Blender jobs at once (at most 8)")
    ap.add_argument("--report", help="write every result to this json")
    a = ap.parse_args()
    root = Path(a.root)
    reg = registry()
    ids = a.ids or ([m for m in sorted(reg) if reg[m]["body_plan"] in PLANS and (root / m / "model_rigged.glb").exists()]
                    if a.all else [])
    if not ids:
        ap.error("ids or --all")
    if a.undo:
        for mid in ids:
            print(undo(mid, root))
        return 0

    def one(mid):
        try:
            return run(mid, root, a.dry_run, mon=reg[mid])
        except Exception as e:  # noqa: BLE001
            return {"id": mid, "status": "error", "problems": [str(e)[-400:]]}

    with ThreadPoolExecutor(min(a.jobs, 8)) as ex:
        res = list(ex.map(one, ids))
    for r in res:
        print(f"{r['id']:28s} {r['status']:8s} " + "; ".join(r.get("problems") or []))
    cnt = {}
    for r in res:
        cnt[r["status"]] = cnt.get(r["status"], 0) + 1
    print(cnt)
    if a.report:
        Path(a.report).write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Rig an externally made character with ITS OWN skeleton and skin weights, then let the stock UAL
route retarget the shared humanoid clips onto it (tools/rig_ual.py: fit "skintokens", weights
"skintokens" = "use the source skeleton's joints and weights"; the source skeleton is just not
SkinTokens').

    python3 tools/ext_rig.py --source <src> <id> [<id> ...]   # prep, bonemap, then forge.py rig
    python3 tools/ext_rig.py --source <src> <id> --map-only    # prep + bonemap.json, report, stop
    python3 tools/ext_rig.py --source <src> --list             # which records have a skeleton

Needs tools/ext_import.py to have run (out/<source>/<id>/ext/source.glb, manifest["ext"], qa.json):
the skinned copy is put in the same frame as model.glb (QA's yaw, scale, feet on z=0). Writes
rig/skintokens_rigged.glb (the skinned, QA-framed glb: the stock UAL stage reads that name),
bonemap.json (source bone -> UAL bone, plus chains), and the override keys rig_source ual,
rig_ual_fit skintokens, rig_ual_weights skintokens in the record's override file. Then `forge.py
rig` / `anim` / `promote` run as for any biped.

Name mapping (map_bones) understands Mixamo (Hips/Spine1/LeftUpLeg/LeftLeg), Blender/Rigify-ish
(upper_arm.L), and the Japanese-game convention (hip, waist/chest, L_collar/L_arm/L_elbow/L_hand,
L_leg/L_knee/L_ankle/L_toe, where `leg` is the THIGH). Convention for `leg` is decided per rig: a
bone called upleg/thigh means `leg` is the calf. Unmatched bones (hair, skirt, accessories, face)
stay unmapped: the retarget leaves them at rest, they follow their parent.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge  # noqa: E402
import sources  # noqa: E402

SKIP = re.compile(r"(?i)finger|thumb|index|middle|ring|pinky|little|hair|skirt|tail|bust|breast|cloth|ribbon|"
                  r"bang|ponytail|socket|\bex\b|_ex|headband|bk_|cape|belt|eye|brow|jaw|tongue|lip|teeth|mouth|"
                  r"cheek|weapon|grab|effect|face|nose|forehead|_end|_tip|twist|roll|helper|ik[_ .]|_ik|_fk|^c_|^cc_|pole|"
                  r"scale_fix|_b\.|bend|master|traj|pos_|^root$")


def norm(name: str) -> str:
    n = re.sub(r"^[a-z]+rig:", "", name, flags=re.I)          # mixamorig:
    n = re.sub(r"[_ ]?0*\d+$", "", n)                          # trailing glTF/blender index: Hips_01, Arm.001
    n = re.sub(r"\.\d+$", "", n)
    n = re.sub(r"\.x$", "", n)                                # Auto-Rig Pro centre bones: spine_01.x, head.x
    return n


def side_of(n: str):
    m = re.match(r"^(left|right)[ _.:\-]?(.*)$", n, re.I)
    if m:
        return m.group(1)[0].lower(), m.group(2)
    m = re.match(r"^([lr])[ _.:\-](.*)$", n, re.I)
    if m:
        return m.group(1).lower(), m.group(2)
    m = re.match(r"^(.*?)[ _.:\-]([lr])$", n, re.I)           # upper_arm.L
    if m:
        return m.group(2).lower(), m.group(1)
    return None, n


def classify(name: str, mixamo_leg: bool):
    """(side or None, kind) for a core bone, else None."""
    if SKIP.search(name):
        return None
    n = norm(name)
    side, rest = side_of(n)
    r = re.sub(r"[ _.\-]", "", rest).lower()
    if side is None:
        if r in ("hips", "hip", "pelvis", "root"):
            return None, "pelvis"
        if re.fullmatch(r"spine\d*", r) or r in ("waist", "chest", "upperchest", "torso", "abdomen"):
            return None, "trunk"
        if r == "neck":
            return None, "neck"
        if r == "head":
            return None, "head"
        return None
    if r in ("collar", "clavicle", "shoulder"):
        return side, "clavicle"
    if r in ("armstretch", "armtwist"):                       # Auto-Rig Pro deform bones: map_bones picks the
        return side, "upperarm"                               # one at the limb's origin, merges the others in
    if r in ("forearmstretch", "forearmtwist"):
        return side, "lowerarm"
    if r in ("thighstretch", "thightwist"):
        return side, "thigh"
    if r in ("legstretch", "legtwist"):
        return side, "calf"
    if r in ("arm", "upperarm"):
        return side, "upperarm"
    if r in ("elbow", "forearm", "lowerarm"):
        return side, "lowerarm"
    if r in ("hand", "wrist"):
        return side, "hand"
    if r in ("upleg", "thigh", "upperleg"):
        return side, "thigh"
    if r in ("leg", "lowerleg", "calf", "shin"):
        return side, ("calf" if (mixamo_leg or r != "leg") else "thigh")
    if r == "knee":
        return side, "calf"
    if r in ("foot", "ankle"):
        return side, "foot"
    if r in ("toebase", "toe", "ball", "toes01", "toes"):
        return side, "ball"
    return None


LIMB_UAL = {"upperarm_l", "upperarm_r", "lowerarm_l", "lowerarm_r", "hand_l", "hand_r", "thigh_l", "thigh_r", "calf_l", "calf_r", "foot_l", "foot_r"}


def map_bones(bones: dict, weighted: set | None = None) -> dict:
    """bones: {name: {parent, head, tail}} (world, QA frame, +Z up, faces -Z in glTF = +Y here).
    Returns a bonemap.json dict (tools/bonemap.py's schema, status ok|partial|fail)."""
    names = list(bones)
    mixamo_leg = any(re.search(r"(?i)upleg|thigh|upperleg", n) for n in names)
    found: dict[tuple, list] = {}
    for n in names:
        c = classify(n, mixamo_leg)
        if c:
            found.setdefault(c, []).append(n)
    z = lambda n: bones[n]["head"][2]  # noqa: E731
    height = max(b["head"][2] for b in bones.values())
    out, problems = {}, []

    merge: dict = {}

    def one(key, prefer=None, near=None):
        c = found.get(key) or []
        if weighted and any(n in weighted for n in c):
            c = [n for n in c if n in weighted]  # a skin-weighted bone beats an unweighted duplicate
        if not c:
            return None
        if len(c) > 1 and near is not None:  # stretch/twist pairs: the bone nearest the previous joint starts the piece
            pick = min(c, key=lambda n: sum((a - b) ** 2 for a, b in zip(bones[n]["head"], near)))
            merge.update({n: pick for n in c if n != pick and re.search(r"(?i)twist|stretch", n)})
            return pick
        return max(c, key=z) if prefer == "high" else (min(c, key=z) if prefer == "low" else c[0])

    pel = one((None, "pelvis"), "low")
    head = one((None, "head"), "high")
    neck = one((None, "neck"), "low")
    if pel:
        out[pel] = "pelvis"
    if head:
        out[head] = "Head"
    if neck:
        out[neck] = "neck_01"
    trunk = sorted([t for t in found.get((None, "trunk"), []) if not weighted or t in weighted or len(found.get((None, "trunk"), [])) < 3], key=z)
    if pel:
        trunk = [t for t in trunk if z(t) - z(pel) > 0.03 * height or t == trunk[-1]]
    ual_spine = {0: [], 1: ["spine_03"], 2: ["spine_01", "spine_03"]}.get(min(len(trunk), 2) if len(trunk) < 3 else 3)
    if len(trunk) >= 3:
        mid = trunk[len(trunk) // 2]
        ual_spine = None
        out[trunk[0]], out[mid], out[trunk[-1]] = "spine_01", "spine_02", "spine_03"
    elif ual_spine:
        for t, u in zip(trunk, ual_spine):
            out[t] = u
    chains = {"spine": [n for n in ([pel] + trunk) if n], "neck_head": [n for n in (neck, head) if n]}
    for s in "lr":
        ch = []
        prev = None
        for kind, u in (("clavicle", "clavicle"), ("upperarm", "upperarm"), ("lowerarm", "lowerarm"), ("hand", "hand")):
            n = one((s, kind), "low" if kind in ("clavicle", "upperarm", "lowerarm") else None, near=prev)
            if n:
                prev = bones[n]["head"] if kind == "clavicle" else (bones[n]["head"] if kind == "upperarm" else bones[n]["head"])
            if n:
                out[n] = f"{u}_{s}"
                ch.append(n)
        chains[f"arm_{s}"] = ch
        ch = []
        prev = bones[pel]["head"] if pel else None
        for kind, u in (("thigh", "thigh"), ("calf", "calf"), ("foot", "foot"), ("ball", "ball")):
            n = one((s, kind), "high" if kind == "thigh" else None, near=prev if kind in ("thigh", "calf") else None)
            if n and kind in ("thigh", "calf"):
                prev = bones[n]["head"]
            if n:
                out[n] = f"{u}_{s}"
                ch.append(n)
        chains[f"leg_{s}"] = ch
    inv = {v: k for k, v in out.items()}
    ess = ["pelvis", "Head"] + [f"{b}_{s}" for s in "lr" for b in ("upperarm", "lowerarm", "hand", "thigh", "calf", "foot")]
    miss = [u for u in ess if u not in inv]
    ual_missing = [u for u in ["spine_01", "spine_02", "neck_01"] + [f"{b}_{s}" for s in "lr" for b in ("clavicle", "ball")]
                   if u not in inv]
    # side check: UAL's right is character-right; our frame faces -Z (glTF) so character-left is -X... verified below
    for s, sign in (("l", -1), ("r", 1)):
        h = inv.get(f"hand_{s}")
        if h and bones[h]["head"][0] * sign < 0:
            problems.append(f"hand_{s} {h} is on the wrong side (x={bones[h]['head'][0]:.2f})")
    # weighted helper bones that did not map (arm_EX, wrist_EX, BK_ corrective, twist offsets, ...): fold each into the
    # mapped limb bone whose segment is nearest its head. Left alone they fold into the nearest mapped ANCESTOR (the
    # collar / pelvis), which pins half the upper arm to the shoulder and the arms stay stiff.
    if weighted:
        import math
        keep = re.compile(r"(?i)twist|stretch|_ex(_|\d|$)|^bk_|^ac_.*(wrist|elbow|arm|leg|knee)|wrist|elbow|^[lr]_arm|leg\b|knee|shin|thigh|forearm|upperarm")
        limb = {n for n, u in out.items() if u in LIMB_UAL}
        def any_side(name):
            m = re.search(r"(?:^|[_.\- ])([lr])(?:[_.\- ]|$)", name, re.I) or re.search(r"(left|right)", name, re.I)
            return m.group(1)[0].lower() if m else None
        def child_head(n):
            kids = [k for k in limb if bones[k]["parent"] == n or bones.get(bones[k]["parent"], {}).get("parent") == n]
            return bones[kids[0]]["head"] if kids else bones[n]["tail"]
        def seg_d(pt, a, b):
            ab = [y - x for x, y in zip(a, b)]
            L2 = sum(x * x for x in ab) or 1e-9
            t = max(0.0, min(1.0, sum((p - x) * d for p, x, d in zip(pt, a, ab)) / L2))
            return math.dist(pt, [x + t * d for x, d in zip(a, ab)])
        for n in sorted(weighted):
            if n in out or n not in bones or not keep.search(n) or re.search(r"(?i)finger|thumb|index|middle|ring|pinky|hair", n):
                continue
            side = any_side(n)
            cands = [m for m in limb if side is None or any_side(m) == side]
            wr = re.search(r"(?i)wrist", n) and not re.search(r"(?i)^ac_|scl", n)  # a wrist twist deforms the forearm skin
            low = [m for m in cands if out[m].startswith("lowerarm")] if wr else []
            if low:
                merge.setdefault(n, low[0])
            elif cands:
                merge.setdefault(n, min(cands, key=lambda m: seg_d(bones[n]["head"], bones[m]["head"], child_head(m))))
    status = "fail" if miss else ("ok" if not problems and not ual_missing else "partial")
    if problems and not miss:
        status = "partial"
    return {"status": status, "confidence": 1.0 if status == "ok" else 0.6, "map": out,
            "unmapped": [n for n in names if n not in out and not SKIP.search(n)],
            "ual_missing": ual_missing, "problems": problems + [f"missing essential {m}" for m in miss],
            "merge": merge, "chains": chains, "facing": "-Z", "height": round(height, 3), "joints": len(names),
            "left_x": "-X", "toes_point": "-Z", "source": "ext_rig"}


def merge_plan(names: list) -> dict:
    """Twist bones (Auto-Rig Pro: arm_twist.l, thigh_twist.r...) fold onto the stretch bone of their limb piece;
    without this their weight would walk up to an unrelated ancestor (the pelvis)."""
    out = {}
    for n in names:
        m = re.match(r"^(thigh|leg|arm|forearm)_twist(\.[lr])_\d+$", n)
        if m:
            tgt = next((x for x in names if re.match(rf"^{m.group(1)}_stretch{re.escape(m.group(2))}_\d+$", x)), None)
            if tgt:
                out[n] = tgt
    return out


def prep(source: str, mon: dict, ext: dict, merge: dict | None = None) -> dict:
    d = forge.OUT_ROOT / source / mon["id"]
    src = d / "ext" / "source.glb"
    qa = json.loads((d / "qa.json").read_text())
    # candidate hand bones for the blender step: classify the source glb's joints from its JSON chunk
    import struct
    b = src.read_bytes()
    n = struct.unpack_from("<I", b, 12)[0]
    g = json.loads(b[20:20 + n])
    sk = max(g.get("skins") or [{"joints": []}], key=lambda s: len(s["joints"]))
    jn = [g["nodes"][i].get("name", "") for i in sk["joints"]]
    mixamo_leg = any(re.search(r"(?i)upleg|thigh|upperleg", x) for x in jn)
    hands = [x for x in jn if (classify(x, mixamo_leg) or (0, 0))[1] == "hand"]
    out = d / "rig" / "skintokens_rigged.glb"
    out.parent.mkdir(exist_ok=True)
    job = {"src": str(src), "dst": str(out),
           "pose": ext.get("pose", "REST"), "drop": ext.get("drop"), "drop_materials": ext.get("drop_materials"), "drop_cards": ext.get("drop_cards"), "alpha_key": ext.get("alpha_key"), "material_colors": ext.get("material_colors"), "clear_normals": ext.get("clear_normals"), "yaw_deg": qa["yaw_deg"], "scale": qa["scale_factor"],
           "tri_budget": ext.get("tri_budget", 40000), "hands": hands, "merge": merge or {}}
    p = subprocess.run([forge.BLENDER, "-b", "--factory-startup", "-P",
                        str(Path(__file__).with_name("blender_ext_rig.py")), "--", json.dumps(job)],
                       capture_output=True, text=True, timeout=900)
    info = Path(str(out) + ".json")
    if p.returncode or not info.exists():
        sys.stderr.write(p.stdout[-3000:] + p.stderr[-3000:])
        raise SystemExit(f"{mon['id']}: blender_ext_rig failed")
    return json.loads(info.read_text())


def run_one(source: str, mon: dict, ext: dict, map_only: bool, run_rig: bool = True) -> None:
    d = forge.OUT_ROOT / source / mon["id"]
    if not (d / "ext" / "source.glb").exists() or not (d / "qa.json").exists():
        raise SystemExit(f"{mon['id']}: run tools/ext_import.py first")
    rep = prep(source, mon, ext)
    bm = map_bones(rep["bones"], set(rep.get("weighted") or []))
    if bm.get("merge"):  # stretch/twist pairs: fold the extras into the limb-origin bone, then map again
        rep = prep(source, mon, ext, bm["merge"])
        bm = map_bones(rep["bones"], set(rep.get("weighted") or []))
    bm["id"] = mon["id"]
    (d / "bonemap.json").write_text(json.dumps(bm, indent=1))
    forge.log(f"{mon['id']}: source skeleton {len(rep['bones'])} bones ({len(rep['pruned'])} pruned), height {rep['height']} m, "
              f"bonemap {bm['status']}: {len([k for k in bm['map']])} mapped, missing {bm['problems'] or 'none'}, "
              f"UAL-missing {bm['ual_missing']}")
    if bm["status"] == "fail":
        raise SystemExit(f"{mon['id']}: bone map failed: {bm['problems']}; map by hand in bonemap.json or pick another model")
    of = sources.overrides_dir(source) / f"{mon['id']}.json"
    ov = json.loads(of.read_text()) if of.exists() else {}
    new = dict(ov, rig_source="ual", rig_ual_fit="skintokens", rig_ual_weights="skintokens")
    if new != ov:
        of.parent.mkdir(parents=True, exist_ok=True)
        of.write_text(json.dumps(new, indent=2) + "\n")
    if map_only or not run_rig:
        return
    r = subprocess.run([sys.executable, str(Path(__file__).with_name("forge.py")), "rig", mon["id"], "--source", source, "--force"],
                       capture_output=True, text=True)
    sys.stdout.write("".join(l + "\n" for l in (r.stderr + r.stdout).splitlines() if mon["id"] in l or "rror" in l))
    if r.returncode:
        raise SystemExit(f"{mon['id']}: rig failed")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True)
    ap.add_argument("--map-only", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("ids", nargs="*")
    a = ap.parse_args()
    table = json.loads(sources.curation_path(a.source, "external").read_text())
    reg = forge.load_registry(a.source)
    ids = a.ids or sorted(k for k in table if not k.startswith("_"))
    for i in ids:
        if i not in table or i not in reg:
            raise SystemExit(f"{i}: not in external.json / registry")
        if a.list:
            print(i, table[i].get("rigged_uid") or table[i]["uid"][:8])
            continue
        run_one(a.source, reg[i], table[i], a.map_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Rig stage: SkinTokens rolls -> best skeleton -> rigged GLB and rigged LODs.

SkinTokens samples without a seed, so every roll differs and some crash; the stage takes
N rolls, scores them and keeps the winner in rig/chosen.glb. That file is the rig's source
of truth: `--force` rebuilds the game files from it (a new albedo, new LODs), only
`--rig-reroll` rolls again, so a hand-fixed skeleton survives re-runs. model.glb stays the
static source for paint and lod; the game files are model_rigged.glb and
model_rigged_lod<k>.glb. Facing matters: a monster that does not point along -Z gets a
scrambled skeleton (docs/TODO.md), so before rolling the stage measures the body axis of
model.glb on its own (blender_rig.py "facing") and refuses to roll when it is more than
FACING_MAX_DEG off with good confidence; pin `yaw`, or set the `rig_facing_check`
override to false for a false alarm. Roll scoring: score() below, docs/rig-anim.md.
"""
import json
import math
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import overrides  # noqa: E402
import paint  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SKINTOKENS = ROOT / "tools" / "skintokens"
BLENDER_RIG = ROOT / "tools" / "blender_rig.py"
ROLLS = 4
ROLL_TIMEOUT = 240
SPRAWLED = {"insectoid"}
# Legs a plan must have; insectoids are 6 or 8, FEET_BY_PLAN has the minimum.
FEET = overrides.FEET_BY_PLAN


def clear_rig_outputs(d: Path) -> None:
    """Drop the game files (paint or lod changed); rig/chosen.glb stays valid."""
    for f in d.glob("model_rigged*.glb"):
        f.unlink()
    (d / "rig.json").unlink(missing_ok=True)


def clear_rig(d: Path, man: dict | None = None) -> None:
    """Drop everything (qa or mesh changed the geometry): skeleton and clips are stale."""
    clear_rig_outputs(d)
    shutil.rmtree(d / "rig", ignore_errors=True)
    shutil.rmtree(d / "anims", ignore_errors=True)
    if man is not None:
        man.pop("rig", None)
        man.pop("anim", None)


def blender(fg, job: dict) -> dict:
    proc = subprocess.run([fg.BLENDER, "-b", "--factory-startup", "-P", str(BLENDER_RIG), "--", json.dumps(job)],
                          capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or "_OK" not in proc.stdout:
        sys.stderr.write(proc.stdout[-4000:] + proc.stderr[-4000:])
        raise RuntimeError(f"blender_rig {job['mode']} failed (rc={proc.returncode})")
    return json.loads(Path(job["report"]).read_text())


# Facing check (blender_rig.py "facing"): refuse to roll past this angle when the
# estimate's confidence is at least FACING_CONF. The rotated pilots are found to within
# about 1 degree; the dragon left at QA's auto yaw is ~60 degrees off.
FACING_MAX_DEG = 30.0
FACING_CONF = 0.5
# Plans whose mirror plane says little about where the head points: a snake lying in an
# S-curve (its best mirror plane cuts across the coils), a blob, a fungus, a swarm cluster,
# a radial octopus. The check still runs and is recorded, but never refuses to roll.
FACING_ADVISORY = {"serpentine", "amorphous", "plant_object", "swarm", "tentacled"}

# What a plan's skeleton should have (docs/body-plans.md). Upright plans have arms and
# no tail test (find_tail matches an arm there); spiders have no spine to speak of.
UPRIGHT = {"biped", "winged_biped", "centauroid", "merfolk_naga"}
# floating ghosts and wraiths are upright bodies with arms and no legs: score their arms and head
UPRIGHT_BODY = UPRIGHT | {"floating"}
TAILED = {"quadruped", "winged_quadruped"}
WINGED = {"winged_quadruped", "winged_biped"}
SPINED = {"biped", "winged_biped", "quadruped", "winged_quadruped", "avian", "centauroid", "merfolk_naga"}
HEADLESS = {"insectoid", "amorphous", "plant_object", "swarm"}  # no head term: spiders' is in the body, blobs have none
LEG_RANGE = {"insectoid": (6, 10)}  # 8 legs + pedipalps or claws reach the ground too
# plausible joint counts; SkinTokens gave goblin 29-37, wolf 22-28, owlbear 18-46,
# dragon 45-80, spider 37, harpy 22-43
JOINTS = {"biped": (24, 70), "winged_biped": (24, 80), "quadruped": (20, 70), "winged_quadruped": (36, 100),
          "insectoid": (24, 90), "avian": (18, 80)}
JOINTS_DEFAULT = (8, 100)

# Penalty weights (score = -sum of the weighted terms). The units: one missing structural
# part (a leg, an arm side, a wing side, the spine, the tail) costs 0.5, so any one of
# them outweighs the small-number terms, which rank rolls that have every part. The
# mesh and pose terms are fractions; their weights put a clearly bad value (a third of
# the far body moving, a tenth of the moving edges torn) at about one missing part.
WEIGHTS = {
    "symmetry": 5.0,        # v1: joint mirror error / height (0.003-0.02 good)
    "legs": 0.5,            # per leg missing or extra against the plan
    "leg_pairs": 0.3,       # per leg without a twin on the other side
    "feet_mirror": 1.0,     # feet mirror error / height
    "spine": 0.5,           # per torso bone short of 3 (hip, one between, shoulder)
    "head": 0.5,            # no head found
    "tail": 0.125,          # per tail bone short of 4, tailed plans
    "wings": 0.5,           # per wing side missing, winged plans
    "arms": 0.5,            # per arm side missing, upright plans
    "joints": 0.02,         # per joint outside the plan's band
    "dead_bones": 0.02,     # v1: per empty or zero-length bone
    "outside": 2.0,         # share of joints more than 5% of the height outside the mesh
    "bleed": 1.0,           # share of weight far from its bone + side bones' weight across the centre
    "torn": 10.0,           # pose test: mean share of moving edges stretched past 2x
    "stretch": 0.25,        # pose test: log2 of the worst edge stretch past 8x
    "stray": 2.0,           # pose test: mean share of far vertices that moved
    "follow": 0.5,          # pose test: per bend, how much of the part's own skin stays behind
}


def score_v1(st: dict, feet: int) -> float:
    """The first score (kept for comparison): a mirrored skeleton with all its legs and no
    dead bones. It picked a 22-joint wolf over a 28-joint one and cannot see a spine."""
    if st["unweighted_verts"]:
        return -10.0
    return -5.0 * st["symmetry"] - 0.5 * max(0, feet - st["legs"]) \
        - 0.02 * st["empty_bones"] - 0.02 * st["zero_length_bones"]


def score_terms(st: dict, plan: str, hands: bool = False) -> dict:
    """Weighted penalties per term (all >= 0) from blender_rig.py's score report."""
    w = WEIGHTS
    # a coiled snake, a blob or a radial octopus has no mirror plane to measure joints against
    t = {"symmetry": 0.0 if plan in FACING_ADVISORY else w["symmetry"] * st["symmetry"],
         "dead_bones": w["dead_bones"] * (st["empty_bones"] + st["zero_length_bones"])}
    if plan in FEET:
        lo, hi = LEG_RANGE.get(plan, (FEET[plan], FEET[plan]))
        t["legs"] = w["legs"] * (max(0, lo - st["legs"]) + max(0, st["legs"] - hi))
        t["leg_pairs"] = w["leg_pairs"] * abs(st["legs_r"] - st["legs_l"])
        t["feet_mirror"] = w["feet_mirror"] * min(st["feet_mirror"], 1.0)
    if plan in SPINED:
        t["spine"] = w["spine"] * max(0, 3 - st["torso_bones"])
    if plan not in HEADLESS:
        t["head"] = w["head"] * (st["head"] is None)
    if plan in TAILED:
        t["tail"] = w["tail"] * max(0, 4 - st["tail_bones"])
    if plan in WINGED or (plan == "avian" and not st.get("wingless")):  # theropods: no wings
        t["wings"] = w["wings"] * max(0, 2 - st["wings"])
    if plan in UPRIGHT_BODY and hands:
        t["arms"] = w["arms"] * max(0, 2 - st["arms"])
    lo, hi = JOINTS.get(plan, JOINTS_DEFAULT)
    t["joints"] = w["joints"] * (max(0, lo - st["joints"]) + max(0, st["joints"] - hi))
    t["outside"] = w["outside"] * st["outside"]
    t["bleed"] = w["bleed"] * (st["bleed_far"] + st["bleed_cross"])
    pose = st.get("pose") or []
    if pose:
        t["torn"] = w["torn"] * sum(b["torn"] for b in pose) / len(pose)
        t["stretch"] = w["stretch"] * max(0.0, math.log2(max(b["stretch_max"] for b in pose) / 8))
        t["stray"] = w["stray"] * sum(b["stray"] for b in pose) / len(pose)
        t["follow"] = w["follow"] * sum(max(0.0, 1 - b["follow"]) for b in pose)
    return {k: round(v, 4) for k, v in t.items()}


def score(st: dict, plan: str, hands: bool = False) -> float:
    """Higher is better (0 is a perfect roll). Falls back to score_v1 for an old report
    without the quality terms."""
    if st["unweighted_verts"]:
        return -10.0
    if "torso_bones" not in st:
        return score_v1(st, FEET.get(plan, 0))
    return -sum(score_terms(st, plan, hands).values())


# Terms that mean a part the plan needs is missing or doubled: the anim stage cannot build
# that part's motion, so the stage logs them loudly and the viewer can flag the monster.
STRUCTURE = ("legs", "leg_pairs", "spine", "head", "tail", "wings", "arms")


def scored(st: dict, plan: str, hands: bool) -> dict:
    """st plus score, score_v1, terms and problems (what roll_<i>.json and manifest
    rig.rolls keep)."""
    st["score"] = round(score(st, plan, hands), 3)
    st["score_v1"] = round(score_v1(st, FEET.get(plan, 0)), 3)
    if "torso_bones" in st:
        st["terms"] = score_terms(st, plan, hands)
        st["problems"] = [k for k in STRUCTURE if st["terms"].get(k, 0) > 0]
    return st


def summary(st: dict) -> dict:
    """A roll report without the per-bone lists, for the manifest."""
    out = {k: v for k, v in st.items() if k not in ("parts", "pose")}
    if st.get("pose"):
        out["pose"] = [{k: b[k] for k in ("part", "bone", "torn", "stray", "follow")} for b in st["pose"]]
    return out


def score_job(mon: dict, src: Path, report: Path) -> dict:
    plan = mon["body_plan"]
    return {"mode": "score", "src": str(src), "report": str(report), "sprawl": plan in SPRAWLED,
            "wingless": mon.get("rig_template") == "custom.theropod",
            "upright": plan in UPRIGHT_BODY, "plan": plan}


# How each plan gets its skeleton. "skintokens" rolls (above); anything else is a generated
# fallback skeleton (blender_rig.py mode "fallback": kind, bones) because SkinTokens gives
# that plan junk (docs/rig-anim.md "Body plans"). The rolls' scores are still recorded when
# --rig-reroll is asked for on a fallback plan, so a later SkinTokens can be compared.
RIG_STRATEGY = {
    "serpentine": {"kind": "spine_chain", "bones": 16},   # SkinTokens: 7-13 joint star, no chain
    "amorphous": {"kind": "vertical", "bones": 4},        # a blob: base-to-top chain, squash and stretch
    "plant_object": {"kind": "vertical", "bones": 5},     # SkinTokens roots a fungus at its cap, stalk hanging down
    "tentacled": {"kind": "tentacle_hub", "bones": 5},    # root + body + 5-bone chain per tentacle tip
    "swarm": {"kind": "root"},                            # one cluster on one bone; the game instances units
}


def fallback_rig(fg, mon: dict, d: Path) -> dict:
    """Generate the plan's fallback skeleton into rig/fallback.glb and keep it as chosen.glb."""
    rdir = d / "rig"
    rdir.mkdir(exist_ok=True)
    spec = RIG_STRATEGY[mon["body_plan"]]
    out = rdir / "fallback.glb"
    rep = blender(fg, {"mode": "fallback", "src": str(d / "model.glb"), "dst": str(out),
                       "report": str(rdir / "fallback_build.json"), "plan": mon["body_plan"], **spec})
    st = scored(blender(fg, score_job(mon, out, rdir / "fallback.json")), mon["body_plan"], bool(mon.get("hands")))
    st["file"] = out.name
    st["build"] = rep
    (rdir / "fallback.json").write_text(json.dumps(st, indent=1))
    fg.log(f"{mon['id']}: rig fallback {spec['kind']}: {st['joints']} joints, score {st['score']}, build {rep}")
    shutil.copyfile(out, rdir / "chosen.glb")
    return {"rolls": [summary(st)], "picked": out.name, "tool": f"fallback:{spec['kind']}"}


def facing_check(fg, mon: dict, d: Path) -> dict:
    """Body axis of model.glb vs +Y (blender_rig.py "facing"), before any roll."""
    (d / "rig").mkdir(exist_ok=True)
    man = fg.load_manifest(d)
    rep = blender(fg, {"mode": "facing", "src": str(d / "model.glb"), "report": str(d / "rig" / "facing.json"),
                       "plan": mon["body_plan"], "head_region": (man.get("qa") or {}).get("head_region"),
                       "forward": overrides.load(mon["source"], mon["id"]).get("forward") or "-Z"})
    bad = rep["angle_deg"] > FACING_MAX_DEG
    advisory = mon["body_plan"] in FACING_ADVISORY
    out = {"method": "mirror_plane" + ("+" + rep["sign_rule"] if rep["head_known"] else ""),
           "angle_deg": rep["angle_deg"], "confidence": rep["confidence"],
           "ok": advisory or not (bad and rep["confidence"] >= FACING_CONF),
           **({"advisory": True} if advisory else {}),
           "head_known": rep["head_known"], "bearing_deg": rep["bearing_deg"],
           "symmetry_error": rep["symmetry_error"], "symmetry_contrast": rep["symmetry_contrast"]}
    for k in ("footprint_axis_deg", "footprint_elongation", "head_region_bearing_deg"):
        if k in rep:
            out[k] = rep[k]
    yaw = (man.get("qa") or {}).get("yaw_deg")
    if yaw is not None:  # QA turns by yaw about +Z; adding the bearing lands the estimate on +Y
        out["suggested_yaw"] = round(((yaw + rep["bearing_deg"] + 180) % 360) - 180, 1)
    if bad or out["confidence"] < FACING_CONF:
        fg.log(f"{mon['id']}: WARNING rig facing: body axis {out['angle_deg']} deg off +Y "
               f"(confidence {out['confidence']}{', head end unknown' if not rep['head_known'] else ''})"
               + (f"; advisory only for {mon['body_plan']} (no mirror-plane body axis), rolling anyway" if advisory else
                  f"; pin `yaw` (about {out.get('suggested_yaw')}) in data/overrides and re-run qa" if bad else
                  "; low confidence (asymmetric pose?), rolling anyway"))
    else:
        fg.log(f"{mon['id']}: rig facing ok: {out['angle_deg']} deg off +Y, confidence {out['confidence']}")
    return out


def roll(fg, mon: dict, d: Path, rolls: int) -> dict:
    rdir = d / "rig"
    rdir.mkdir(exist_ok=True)
    plan, hands = mon["body_plan"], bool(mon.get("hands"))
    fg.wait_idle()
    paint.free_comfy(fg)  # SkinTokens needs ~14 GB
    results = []
    for i in range(rolls):
        out = rdir / f"roll_{i}.glb"
        proc = subprocess.run([str(SKINTOKENS / ".venv/bin/python"), "demo.py", "--input", str(d / "model.glb"),
                               "--output", str(out), "--use_transfer"], cwd=SKINTOKENS,
                              capture_output=True, text=True, timeout=ROLL_TIMEOUT)
        if proc.returncode != 0 or not out.exists():
            fg.log(f"{mon['id']}: rig roll {i} failed ({proc.stderr.strip().splitlines()[-1][:120] if proc.stderr else proc.returncode})")
            continue
        st = scored(blender(fg, score_job(mon, out, rdir / f"roll_{i}.json")), plan, hands)
        st["file"] = out.name
        (rdir / f"roll_{i}.json").write_text(json.dumps(st, indent=1))
        worst = sorted(st.get("terms", {}).items(), key=lambda kv: -kv[1])[:3]
        fg.log(f"{mon['id']}: rig roll {i}: {st['joints']} joints, {st['legs']} legs, "
               f"score {st['score']} (v1 {st['score_v1']}), worst {worst}")
        results.append(st)
    if not results:
        raise RuntimeError(f"{mon['id']}: all {rolls} SkinTokens rolls failed")
    best = max(results, key=lambda r: r["score"])
    if best.get("problems"):
        fg.log(f"{mon['id']}: WARNING best rig roll {best['file']} still has problems {best['problems']} "
               f"(terms {best['terms']}); consider --rig-reroll or a hand fix")
    shutil.copyfile(rdir / best["file"], rdir / "chosen.glb")
    return {"rolls": [summary(r) for r in results], "picked": best["file"]}


def stage_rig(fg, mon: dict, d: Path, args) -> None:
    man = fg.load_manifest(d)
    if not (d / "model.glb").exists():
        raise RuntimeError(f"{mon['id']}: no model.glb; run `qa` first")
    lods = json.loads((d / "lods.json").read_text())["lods"] if (d / "lods.json").exists() else {}
    lod_paths = {k: str(d / v["file"]) for k, v in lods.items() if k != "0" and v.get("file")}
    done = (d / "model_rigged.glb").exists() and all((d / f"model_rigged_lod{k}.glb").exists() for k in lod_paths)
    if done and man.get("rig") and not args.force and not args.rig_reroll:
        fg.log(f"{mon['id']}: rig exists, skip")
        return
    t0 = time.time()
    chosen = d / "rig" / "chosen.glb"
    info = {}
    rolling = args.rig_reroll or not chosen.exists()
    facing = facing_check(fg, mon, d)
    check = overrides.load(mon["source"], mon["id"]).get("rig_facing_check")
    if rolling and not facing["ok"] and check is not False:
        man = fg.load_manifest(d)
        man.setdefault("rig", {"error": "facing check failed"})["facing"] = facing
        fg.save_manifest(d, man)
        raise RuntimeError(f"{mon['id']}: model.glb faces {facing['angle_deg']} deg off +Y (confidence "
                           f"{facing['confidence']}): SkinTokens scrambles misfaced meshes. Pin `yaw` "
                           f"(about {facing.get('suggested_yaw')}) and re-run qa, or set rig_facing_check false")
    if rolling and mon["body_plan"] in RIG_STRATEGY:
        if args.rig_reroll:  # still measure SkinTokens on the plan, for the record
            try:
                skt = roll(fg, mon, d, args.rig_rolls or ROLLS)
                fg.log(f"{mon['id']}: SkinTokens rolls for the record: "
                       f"{[(r['file'], r['score'], r['joints']) for r in skt['rolls']]}")
            except RuntimeError as e:
                fg.log(f"{mon['id']}: SkinTokens rolls failed ({e})")
        info = fallback_rig(fg, mon, d)
    elif rolling:
        info = roll(fg, mon, d, args.rig_rolls or ROLLS)
    else:
        fg.log(f"{mon['id']}: rig uses the kept skeleton rig/chosen.glb")
    clear_rig_outputs(d)
    tex = d / "textures"
    albedo = tex / "base_color_painted.png"
    rep = blender(fg, {"mode": "finalize", "src": str(chosen), "base_color": str(albedo if albedo.exists() else tex / "base_color.png"),
                       "lods": lod_paths, "out_dir": str(d), "report": str(d / "rig.json"),
                       "sprawl": mon["body_plan"] in SPRAWLED, "hands": bool(mon.get("hands")),
                       "sockets": mon.get("sockets") or []})
    # the kept skeleton's quality terms, also when it was not rolled now (a hand-fixed rig)
    q = scored(blender(fg, score_job(mon, chosen, d / "rig" / "chosen.json")), mon["body_plan"], bool(mon.get("hands")))
    (d / "rig" / "chosen.json").write_text(json.dumps(q, indent=1))
    man = fg.load_manifest(d)
    missing = [s for s in (mon.get("sockets") or []) if s not in rep["sockets"]]
    if missing:
        fg.log(f"{mon['id']}: rig could not place sockets {missing}")
    tool = info.get("tool") or (man.get("rig") or {}).get("tool") or \
        (f"fallback:{RIG_STRATEGY[mon['body_plan']]['kind']}" if mon["body_plan"] in RIG_STRATEGY else "skintokens")
    man["rig"] = {"tool": tool, "joints": rep["joints"], "legs": rep["legs"], "symmetry": rep["symmetry"],
                  "sockets": rep["sockets"], "sockets_missing": missing,
                  "empty_bones": rep["empty_bones"], "seconds": round(time.time() - t0, 1),
                  "facing": facing, "quality": summary(q),
                  **({"picked": info["picked"], "rolls": info["rolls"]} if info else
                     {k: v for k, v in (man.get("rig") or {}).items() if k in ("picked", "rolls")} or {"picked": "chosen.glb"})}
    man.setdefault("timings", {})["rig"] = man["rig"]["seconds"]
    fg.save_manifest(d, man)
    fg.log(f"{mon['id']}: rig done in {man['rig']['seconds']}s, {rep['joints']} joints, {rep['legs']} legs, "
           f"sockets {sorted(rep['sockets'])}")

"""Anim stage: procedural clips (anims/<clip>.glb, one clip per file), see blender_clips.py.

SkinTokens bones have no names, so library clips cannot be retargeted without a bone map;
blender_clips.py builds every clip of the monster's `animation_set` it knows how to make
from the skeleton itself (walk, idle, attack_melee, hit, death; fly: a wing beat when it
finds wings, else a levitation; breath on any head chain; cast, swim, burrow on the legged
plans, cast and swim for floating, cast, breath, burrow and fly for serpentine;
attack_ranged for two-armed plans), in one Blender run. Clips it cannot make are listed in
manifest.anim.missing. An upright rig with fewer leg chains than feet (lich, medusa, hezrou)
gets the standing set (no steps; manifest.anim.stand_fallback) instead of failing; a horizontal
one short of legs after rig_topology's rescue pass walks on the legs it has
(manifest.anim.partial_legs, and a line in manifest.anim.warnings), or stands with none.

Override `anim_source: "ual"` (bipeds, winged bipeds) also retargets the Quaternius UAL clips onto
the rig after the procedural run (tools/retarget_ual.py, on this machine's Blender) into anims_ual/
with a per-clip quality gate against the procedural clip (anims_ual/ual.json); anims/ is untouched
until `forge.py promote <id>` (or `retarget_ual.py --promote`) copies the passing clips over it,
moving the procedural files to anims_proc/. docs/anim-reuse.md.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import overrides  # noqa: E402
import rig  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BLENDER_CLIPS = ROOT / "tools" / "blender_clips.py"
SPRAWLED = {"insectoid"}
UPRIGHT = {"biped", "winged_biped"}
# Plans with no walking legs: blender_clips.py's "legless" builders drive their body chain
# (and arms, tentacles) instead of feet. docs/rig-anim.md "Body plans".
LEGLESS = {"serpentine", "floating", "amorphous", "plant_object", "tentacled", "aquatic", "merfolk_naga", "swarm"}
# Plans whose arms the clips drive (when the record has hands), besides the upright ones.
ARMED = {"centauroid", "merfolk_naga", "floating"}
PROPS = ROOT / "data" / "registry" / "props.json"


def wielded(mon: dict, socket: str = "hand_r") -> dict | None:
    """The prop in `socket` by default, the way the viewer picks it: the first of the
    record's `wields` whose props.json `grip_socket` is that socket. blender_clips.py picks
    the attack style from its category (a spear thrusts, a sword cuts) and, for both hands,
    how much that arm swings in the walk and swim (a pike or a bow is held steady); `grip`
    is props.json's hand count (2: a two-hander). None: unarmed."""
    wields = mon.get("wields") or []
    if not wields or not PROPS.exists():
        return None
    props = {p["id"]: p for p in json.loads(PROPS.read_text()).get("props", [])}
    pid = next((w for w in wields if (props.get(w) or {}).get("grip_socket") == socket), None)
    return {"id": pid, "category": props[pid].get("category"), "grip": props[pid].get("grip", 1)} if pid else None


def idle_lift(mon: dict, want: list) -> tuple:
    """(idle_lift, move clip) for blender_clips.py. The game moves a monster with `walk`,
    else `swim`, else `fly` (godot-dnd Monster.move_clip()). A monster whose
    animation_set has no walk moves only by swimming or flying, so its idle is held where
    that clip holds the body ("match_move") and idle -> move does not drop or jump; one
    that walks keeps its idle on the floor (the harpy, the dragon). The `idle_lift`
    override (metres, "match_move", or false for the floor) replaces the rule."""
    move = next((c for c in ("walk", "swim", "fly") if c in want), None)
    lift = overrides.load(mon["source"], mon["id"]).get("idle_lift")
    if lift is None:
        lift = "match_move" if move in ("swim", "fly") else None
    return (lift or None), move


def clip_job(mon: dict, d: Path, adir: Path, report: Path) -> dict:
    """blender_clips.py's job for this monster (also its parts_only run, tools/quadcanon.py)."""
    want = list(mon.get("animation_set") or [])
    plan = mon["body_plan"]
    lift, move = idle_lift(mon, want)
    return {"idle_lift": lift, "move_clip": move,
            "src": str(d / "model_rigged.glb"), "out_dir": str(adir), "report": str(report), "clips": want,
            "feet": overrides.FEET_BY_PLAN.get(plan, 0), "sprawl": plan in SPRAWLED, "upright": plan in UPRIGHT,
            "arms": plan in ARMED and bool(mon.get("hands")), "plan": plan, "weapon_r": wielded(mon),
            "weapon_l": wielded(mon, "hand_l"), "tail_fused": rig.tail_fused(mon),
            "template": mon.get("rig_template")}


def stage_anim(fg, mon: dict, d: Path, args) -> None:
    want = list(mon.get("animation_set") or [])
    adir = d / "anims"
    done = (fg.load_manifest(d).get("anim") or {}).get("clips") or want
    if want and all((adir / f"{c}.glb").exists() for c in done) and not args.force:
        fg.log(f"{mon['id']}: clips exist, skip")  # incl. the ones it cannot make (manifest anim.missing)
        stage_ual(fg, mon, d, args)
        return
    plan = mon["body_plan"]
    if plan not in overrides.FEET_BY_PLAN and plan not in LEGLESS:
        fg.log(f"{mon['id']}: no clips for plan {plan} yet, skip")
        return
    if not (d / "model_rigged.glb").exists():
        raise RuntimeError(f"{mon['id']}: no model_rigged.glb; run `rig` first")
    adir.mkdir(exist_ok=True)
    report = adir / "clips.json"
    report.unlink(missing_ok=True)
    job = clip_job(mon, d, adir, report)
    t0 = time.time()
    proc = subprocess.run([fg.BLENDER, "-b", "--factory-startup", "--python-exit-code", "1", "-P", str(BLENDER_CLIPS),
                           "--", json.dumps(job)], capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0 or not report.exists():
        sys.stderr.write(proc.stdout[-4000:] + proc.stderr[-4000:])
        raise RuntimeError(f"{mon['id']}: clips failed (rc={proc.returncode})")
    rep = json.loads(report.read_text())
    dt = round(time.time() - t0, 1)
    clips = sorted(rep["clips"], key=want.index)
    man = fg.load_manifest(d)
    man["anim"] = {"clips": clips, "missing": [c for c in want if c not in clips], "seconds": dt,
                   **{c: rep["clips"][c] for c in clips},
                   "parts": {k: rep[k] for k in ("root", "head", "neck", "spine", "tail", "arms", "wings", "legs", "body",
                                                     "tentacles", "sting", "claws") if k in rep}}
    if rep.get("stand_fallback"):  # fewer leg chains than feet: blender_clips.py's standing set
        man["anim"]["stand_fallback"] = rep["stand_fallback"]
    if rep.get("partial_legs"):  # a horizontal body short of legs: it walks on the ones found
        man["anim"]["partial_legs"] = rep["partial_legs"]
    if rep.get("warnings"):  # blender_clips.py's topology gate: parts dropped from the clips, odd legs
        man["anim"]["warnings"] = rep["warnings"]
    man.setdefault("timings", {})["anim"] = dt
    rig.clear_stage_errors(man, "anim")
    fg.save_manifest(d, man)
    fg.log(f"{mon['id']}: {len(clips)} clips in {dt}s: {', '.join(clips)}"
           + (f"; missing {man['anim']['missing']}" if man["anim"]["missing"] else "")
           + (f"; standing set ({rep['stand_fallback']['legs_found']} of {rep['stand_fallback']['need']} leg chains)"
              if rep.get("stand_fallback") else ""))
    for w in rep.get("warnings") or []:
        fg.log(f"{mon['id']}: WARNING anim topology: {w}")
    stage_ual(fg, mon, d, args)


def stage_ual(fg, mon: dict, d: Path, args) -> None:
    """Override anim_source "ual": retarget the UAL clips into anims_ual/ (retarget_ual.run: Blender on
    this machine, ~15 s), gate each against the procedural clip, record manifest.anim.ual. Nothing in
    anims/ changes here; promote() does that."""
    if (fg.load_manifest(d).get("rig") or {}).get("tool") == "ual":
        return  # rig_source "ual": the rig stage already wrote anims_ual/ (no retarget onto UAL's own skeleton)
    if overrides.load(mon["source"], mon["id"]).get("anim_source") != "ual":
        return
    if mon["body_plan"] not in UPRIGHT:
        fg.log(f"{mon['id']}: anim_source ual is for bipeds only, skip")
        return
    import bonemap
    import retarget_ual
    if (d / "anims_ual" / "ual.json").exists() and not args.force:
        fg.log(f"{mon['id']}: UAL clips exist, skip")
        return
    if not (d / "bonemap.json").exists():
        bonemap.run(mon["id"])
    if json.loads((d / "bonemap.json").read_text()).get("status") not in ("ok", "partial"):
        raise RuntimeError(f"{mon['id']}: bonemap.json status is not ok/partial; UAL clips cannot be mapped")
    t0 = time.time()
    m = retarget_ual.run(mon["id"], retarget_ual.plan_clips(mon), "none", {}, blender=fg.BLENDER, quiet=True)
    man = fg.load_manifest(d)
    man.setdefault("anim", {})["ual"] = {c: v["use"] for c, v in m["verdict"].items()}
    man.setdefault("timings", {})["anim_ual"] = round(time.time() - t0, 1)
    fg.save_manifest(d, man)
    fg.log(f"{mon['id']}: UAL clips in anims_ual/: " + ", ".join(f"{c}={v['use']}" for c, v in m["verdict"].items())
           + f"; promote with `forge.py promote {mon['id']}`")

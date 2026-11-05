"""Anim stage: procedural clips (anims/<clip>.glb, one clip per file), see blender_clips.py.

SkinTokens bones have no names, so library clips cannot be retargeted without a bone map;
blender_clips.py builds every clip of the monster's `animation_set` it knows how to make
from the skeleton itself (walk, idle, attack_melee, hit, death; fly when it finds wings,
breath for winged quadrupeds, attack_ranged for two-armed plans), in one Blender run.
Clips it cannot make are listed in manifest.anim.missing.
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


def stage_anim(fg, mon: dict, d: Path, args) -> None:
    want = list(mon.get("animation_set") or [])
    adir = d / "anims"
    done = (fg.load_manifest(d).get("anim") or {}).get("clips") or want
    if want and all((adir / f"{c}.glb").exists() for c in done) and not args.force:
        fg.log(f"{mon['id']}: clips exist, skip")  # incl. the ones it cannot make (manifest anim.missing)
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
    job = {"src": str(d / "model_rigged.glb"), "out_dir": str(adir), "report": str(report), "clips": want,
           "feet": overrides.FEET_BY_PLAN.get(plan, 0), "sprawl": plan in SPRAWLED, "upright": plan in UPRIGHT,
           "arms": plan in ARMED and bool(mon.get("hands")), "plan": plan}
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
                                                     "tentacles") if k in rep}}
    man.setdefault("timings", {})["anim"] = dt
    rig.clear_stage_errors(man, "anim")
    fg.save_manifest(d, man)
    fg.log(f"{mon['id']}: {len(clips)} clips in {dt}s: {', '.join(clips)}"
           + (f"; missing {man['anim']['missing']}" if man["anim"]["missing"] else ""))

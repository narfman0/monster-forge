"""Anim stage: a procedural in-place walk (anims/walk.glb), see blender_walk.py.

SkinTokens bones have no names, so library clips cannot be retargeted without a bone map;
this builds the gait from the skeleton itself, for every plan with legs.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import overrides  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BLENDER_WALK = ROOT / "tools" / "blender_walk.py"
SPRAWLED = {"insectoid"}
UPRIGHT = {"biped", "winged_biped"}


def stage_anim(fg, mon: dict, d: Path, args) -> None:
    clip = d / "anims" / "walk.glb"
    if clip.exists() and not args.force:
        fg.log(f"{mon['id']}: walk exists, skip")
        return
    if "walk" not in mon.get("animation_set", []) or mon["body_plan"] not in overrides.FEET_BY_PLAN:
        fg.log(f"{mon['id']}: no walk for plan {mon['body_plan']}, skip")
        return
    if not (d / "model_rigged.glb").exists():
        raise RuntimeError(f"{mon['id']}: no model_rigged.glb; run `rig` first")
    clip.parent.mkdir(exist_ok=True)
    report = d / "anims" / "walk.json"
    report.unlink(missing_ok=True)
    plan = mon["body_plan"]
    job = {"src": str(d / "model_rigged.glb"), "dst": str(clip), "report": str(report),
           "feet": overrides.FEET_BY_PLAN[plan], "sprawl": plan in SPRAWLED, "upright": plan in UPRIGHT}
    t0 = time.time()
    proc = subprocess.run([fg.BLENDER, "-b", "--factory-startup", "-P", str(BLENDER_WALK), "--", json.dumps(job)],
                          capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not report.exists():
        sys.stderr.write(proc.stdout[-4000:] + proc.stderr[-4000:])
        raise RuntimeError(f"{mon['id']}: walk failed (rc={proc.returncode})")
    rep = json.loads(report.read_text())
    dt = round(time.time() - t0, 1)
    man = fg.load_manifest(d)
    man["anim"] = {"clips": ["walk"], "walk": rep, "seconds": dt}
    man.setdefault("timings", {})["anim"] = dt
    fg.save_manifest(d, man)
    fg.log(f"{mon['id']}: walk done in {dt}s, {rep['legs']} legs, {rep['seconds']}s cycle")

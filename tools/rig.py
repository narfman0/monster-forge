"""Rig stage: SkinTokens rolls -> best skeleton -> rigged GLB and rigged LODs.

SkinTokens samples without a seed, so every roll differs and some crash; the stage takes
N rolls, scores them and keeps the winner in rig/chosen.glb. That file is the rig's source
of truth: `--force` rebuilds the game files from it (a new albedo, new LODs), only
`--rig-reroll` rolls again, so a hand-fixed skeleton survives re-runs. model.glb stays the
static source for paint and lod; the game files are model_rigged.glb and
model_rigged_lod<k>.glb. Facing matters: a monster that does not point along -Z gets a
scrambled skeleton (docs/TODO.md), so check `yaw` before blaming the rig.
"""
import json
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


def score(st: dict, feet: int) -> float:
    """Higher is better: a mirrored skeleton with all its legs and no dead bones."""
    if st["unweighted_verts"]:
        return -10.0
    return -5.0 * st["symmetry"] - 0.5 * max(0, feet - st["legs"]) \
        - 0.02 * st["empty_bones"] - 0.02 * st["zero_length_bones"]


def roll(fg, mon: dict, d: Path, rolls: int) -> dict:
    rdir = d / "rig"
    rdir.mkdir(exist_ok=True)
    feet = FEET.get(mon["body_plan"], 0)
    sprawl = mon["body_plan"] in SPRAWLED
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
        st = blender(fg, {"mode": "score", "src": str(out), "report": str(rdir / f"roll_{i}.json"),
                          "sprawl": sprawl})
        st["score"] = round(score(st, feet), 3)
        st["file"] = out.name
        fg.log(f"{mon['id']}: rig roll {i}: {st['joints']} joints, {st['legs']} legs, "
               f"symmetry {st['symmetry']}, score {st['score']}")
        results.append(st)
    if not results:
        raise RuntimeError(f"{mon['id']}: all {rolls} SkinTokens rolls failed")
    best = max(results, key=lambda r: r["score"])
    shutil.copyfile(rdir / best["file"], rdir / "chosen.glb")
    return {"rolls": results, "picked": best["file"]}


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
    if args.rig_reroll or not chosen.exists():
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
    man = fg.load_manifest(d)
    missing = [s for s in (mon.get("sockets") or []) if s not in rep["sockets"]]
    if missing:
        fg.log(f"{mon['id']}: rig could not place sockets {missing}")
    man["rig"] = {"tool": "skintokens", "joints": rep["joints"], "legs": rep["legs"], "symmetry": rep["symmetry"],
                  "sockets": rep["sockets"], "sockets_missing": missing,
                  "empty_bones": rep["empty_bones"], "seconds": round(time.time() - t0, 1),
                  **({"picked": info["picked"], "rolls": info["rolls"]} if info else {"picked": (man.get("rig") or {}).get("picked", "chosen.glb")})}
    man.setdefault("timings", {})["rig"] = man["rig"]["seconds"]
    fg.save_manifest(d, man)
    fg.log(f"{mon['id']}: rig done in {man['rig']['seconds']}s, {rep['joints']} joints, {rep['legs']} legs, "
           f"sockets {sorted(rep['sockets'])}")

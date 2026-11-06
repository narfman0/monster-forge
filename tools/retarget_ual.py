"""UAL retarget spike driver (docs/anim-reuse.md): runs tools/blender_retarget_ual.py per monster on
this machine's Blender and writes out/srd-5.2.1/<id>/anims_ual/<clip>.glb (+ <clip>.json, metrics.json)
plus review contact sheets.

    python3 -I tools/retarget_ual.py archmage goblin-warrior [--clips idle walk ...] [--no-sheet]

Needs <id>/model_rigged.glb and bonemap.json (tools/bonemap.py) and the UAL packs (CC0) under
~/.cache/monster-forge-scratch/anim-packs/ual1|ual2. Sheets (Pillow) go to
~/.cache/monster-forge-scratch/retarget/<id>_<group>.png, tiles beside them.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "srd-5.2.1"
BLENDER = os.environ.get("BLENDER", "/usr/bin/blender")
SCRIPT = ROOT / "tools" / "blender_retarget_ual.py"
PACKS = Path.home() / ".cache" / "monster-forge-scratch" / "anim-packs"
SCRATCH = Path.home() / ".cache" / "monster-forge-scratch" / "retarget"
# our clip -> UAL clip (UAL1); "A+B" plays UAL clips back to back
CLIPS = {"idle": "Idle_Loop", "walk": "Walk_Loop", "run": "Sprint_Loop", "attack": "Sword_Attack",
         "hit": "Hit_Chest", "death": "Death01", "cast": "Spell_Simple_Enter+Spell_Simple_Shoot+Spell_Simple_Exit"}
PROCEDURAL = {"idle": "idle", "walk": "walk", "attack": "attack_melee", "hit": "hit", "death": "death", "cast": "cast"}
SHEETS = {"move": ["idle", "walk", "run"], "action": ["attack", "hit", "cast", "death"]}


def packs() -> list[str]:
    found = sorted(PACKS.glob("ual*/**/Unreal-Godot/UAL*_Standard.glb"))
    if not found:
        raise SystemExit(f"no UAL*_Standard.glb under {PACKS}")
    return [str(p) for p in found]


def sheet(tiles: Path, clips: list[str], dst: Path, title: str) -> None:
    from PIL import Image, ImageDraw
    rows = []
    for c in clips:
        for who, view in (("ours", "front"), ("ours", "side"), ("ual", "side"), ("proc", "side")):
            ims = [tiles / f"{c}_{who}_{view}_{k}.png" for k in range(6)]
            if all(p.exists() for p in ims):
                rows.append((f"{c} {who} {view}", ims))
    if not rows:
        return
    tw, th = Image.open(rows[0][1][0]).size
    lab = 110
    out = Image.new("RGB", (lab + 6 * tw, 24 + len(rows) * th), "white")
    d = ImageDraw.Draw(out)
    d.text((4, 4), title, fill="black")
    for r, (name, ims) in enumerate(rows):
        y = 24 + r * th
        d.text((4, y + th // 2 - 6), name.replace(" ", "\n", 1), fill="black")
        for k, p in enumerate(ims):
            out.paste(Image.open(p).convert("RGB"), (lab + k * tw, y))
        d.line([(0, y), (out.width, y)], fill=(160, 160, 160))
    out.save(dst)


def run(mid: str, clips: dict, do_sheet: bool, extra: dict, tag: str = "") -> dict:
    """tag: a variant (A/B runs): clips go to anims_ual<tag>/, sheets to <id><tag>_*.png."""
    d = ROOT / "out" / SOURCE / mid
    tiles = SCRATCH / f"{mid}{tag}_tiles"
    job = {"src": str(d / "model_rigged.glb"), "bonemap": str(d / "bonemap.json"), "packs": packs(),
           "clips": clips, "out_dir": str(d / f"anims_ual{tag}"), "tiles": str(tiles) if do_sheet else None,
           "sheet_clips": [c for c in clips], **extra,
           "baseline": {c: str(d / "anims" / f"{PROCEDURAL[c]}.glb") for c in clips if c in PROCEDURAL}}
    t0 = time.time()
    proc = subprocess.run([BLENDER, "-b", "--factory-startup", "--python-exit-code", "1", "-P", str(SCRIPT),
                           "--", json.dumps(job)], capture_output=True, text=True, timeout=3600)
    log = SCRATCH / f"{mid}{tag}.log"
    SCRATCH.mkdir(parents=True, exist_ok=True)
    log.write_text(proc.stdout + proc.stderr)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-3000:] + proc.stderr[-3000:])
        raise SystemExit(f"{mid}: retarget failed (rc={proc.returncode}), log {log}")
    if do_sheet:
        for g, cs in SHEETS.items():
            sheet(tiles, [c for c in cs if c in clips], SCRATCH / f"{mid}{tag}_{g}.png", f"{mid}{tag}: UAL retarget ({g})")
    m = json.loads((d / f"anims_ual{tag}" / "metrics.json").read_text())
    print(f"{mid}: {len(clips)} clips in {time.time() - t0:.0f}s, hip_scale {m['hip_scale']}")
    for c, r in m["clips"].items():
        print(f"  {c:7s} foot_low {r['foot_low_min_frac']:+.3f}..{r['foot_low_max_frac']:+.3f} h  min_z {r['min_z']:+.3f}"
              f"  lift {r.get('floor_lift_max', 0):.3f} swing {r.get('clearance_deg')} ual_foot {r.get('ual_foot_rise_max_frac')} lie {r.get('lying_blend_max')}  stretch {r['edge_stretch_p999']:.2f}  loop {r.get('loop_delta', '-')}  from_idle {r.get('from_idle_m', '-')}")
    return m


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="+")
    ap.add_argument("--clips", nargs="+", default=list(CLIPS), help=f"of {list(CLIPS)} or ours=UAL_name")
    ap.add_argument("--no-sheet", action="store_true")
    ap.add_argument("--tag", default="", help="variant suffix for the output dir and sheets (A/B runs)")
    ap.add_argument("--leg-mode", choices=("delta", "dir"), default="delta")
    ap.add_argument("--arm-mode", choices=("delta", "dir"), default="dir")
    ap.add_argument("--hip-scale", type=float, default=None)
    ap.add_argument("--ref", default="Idle_Loop", help="UAL clip whose first frame the deltas are measured from, or tpose")
    ap.add_argument("--no-floor", action="store_true", help="do not raise the body out of the ground")
    a = ap.parse_args()
    clips = dict(c.split("=", 1) if "=" in c else (c, CLIPS[c]) for c in a.clips)
    if "idle" not in clips:  # from_idle metrics need it first
        clips = {"idle": CLIPS["idle"], **clips}
    clips = {"idle": clips.pop("idle"), **clips}
    for mid in a.ids:
        run(mid, clips, not a.no_sheet, {"leg_mode": a.leg_mode, "arm_mode": a.arm_mode, "hip_scale": a.hip_scale,
                                               "ref": a.ref, "floor": not a.no_floor}, a.tag)


if __name__ == "__main__":
    main()

"""UAL retarget driver (docs/anim-reuse.md): runs tools/blender_retarget_ual.py per monster on this
machine's Blender, writes out/srd-5.2.1/<id>/anims_ual/<clip>.glb (+ <clip>.json, metrics.json and
ual.json, the per-clip verdict: use the UAL clip or keep the procedural one) and review sheets.

    python3 -I tools/retarget_ual.py archmage goblin-warrior      # clips picked from the registry
    python3 -I tools/retarget_ual.py --all --jobs 6               # every rigged biped (bonemap ok/partial)
    python3 -I tools/retarget_ual.py ape --clips attack_melee=Punch_Cross --tag _punch   # A/B variant
    python3 -I tools/retarget_ual.py --promote archmage           # copy the passing clips into anims/
    python3 -I tools/retarget_ual.py --unpromote archmage         # put the procedural ones back
    python3 -I tools/retarget_ual.py --all --regate               # redo the quality gate, no Blender

Clip files carry the anim stage's names (idle, walk, attack_melee, hit, death, cast; plus run, which
the procedural stage does not make), so a promoted clip is a drop-in. `--promote` moves the
procedural files it replaces to anims_proc/ first (nothing is lost; `--unpromote` restores them) and
records manifest anim.source[clip] = "ual". The viewer reads anims/ only.

Needs <id>/model_rigged.glb and bonemap.json (tools/bonemap.py), the procedural anims/ (the quality
baseline) and the UAL packs (CC0) under ~/.cache/monster-forge-scratch/anim-packs/ual1|ual2. Sheets
(Pillow) go to ~/.cache/monster-forge-scratch/retarget/.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "srd-5.2.1"
BLENDER = os.environ.get("BLENDER", "/usr/bin/blender")
SCRIPT = ROOT / "tools" / "blender_retarget_ual.py"
PACKS = Path.home() / ".cache" / "monster-forge-scratch" / "anim-packs"
SCRATCH = Path.home() / ".cache" / "monster-forge-scratch" / "retarget"
REGISTRY = ROOT / "data" / "registry" / f"{SOURCE}.json"
PROPS = ROOT / "data" / "registry" / "props.json"
SPELL = "Spell_Simple_Enter+Spell_Simple_Shoot+Spell_Simple_Exit"
# our clip (the anim stage's names) -> UAL clip; "A+B" plays UAL clips back to back. UAL1 and UAL2 are
# searched in that order.
CLIPS = {"idle": "Idle_Loop", "walk": "Walk_Loop", "run": "Jog_Fwd_Loop", "attack_melee": "Sword_Attack",
         "hit": "Hit_Chest", "death": "Death01", "cast": SPELL}
UNARMED_ATTACK = "Punch_Cross"
# Zombie_Idle_Loop / Zombie_Walk_Fwd_Loop / Zombie_Scratch, no run (ids containing "zombie" too)
SHAMBLERS = {"mummy", "ghoul", "ghast"}
NO_UAL_ATTACK = {"polearm", "staff"}  # thrusts and staff swings: UAL has none, the procedural clip is kept
# Clips UAL can serve; every other animation_set entry (fly, swim, breath, burrow, attack_ranged) stays
# procedural. run is extra (not in any animation_set).
SERVED = ["idle", "walk", "run", "attack_melee", "hit", "death", "cast"]
LOOPS = {"idle", "walk", "run"}
# Quality gate per clip (calibrated on the 50-biped sheets, docs/anim-reuse.md). Edge stretch p99.9
# flags torn robes and coats but does not separate them cleanly from a deep lunge in tight clothes, so
# it errs toward the procedural clip (the status quo):
STRETCH_RATIO, STRETCH_ABS = 1.6, 3.0   # tear: p99.9 > max(ratio x procedural, procedural + abs) ...
STRETCH_CEIL = {"loop": 7.0, "once": 10.0}  # ... or above this whatever the procedural clip does
FLOOR_FRAC = 0.02                       # sinks: lowest vertex more than this x height under the ground
FOOT_FLOAT_FRAC = 0.04                  # idle/walk: the lower foot this much (x height) higher than in
                                        # the procedural clip (a robe hem or a tail can be the lowest point)

def packs() -> list[str]:
    found = sorted(PACKS.glob("ual*/**/Unreal-Godot/UAL*_Standard.glb"))
    if not found:
        raise SystemExit(f"no UAL*_Standard.glb under {PACKS}")
    return [str(p) for p in found]


def registry() -> dict:
    return {m["id"]: m for m in json.loads(REGISTRY.read_text())["monsters"]}


def use_source(source: str) -> None:
    """Work on out/<source>/ and that source's registry (tools/sources.py; default the SRD). The pipeline
    calls it with the monster's source (rig_ual.stage, forge.py promote / share)."""
    global SOURCE, REGISTRY
    import sources
    SOURCE, REGISTRY = source, sources.registry_path(source)


def held(mon: dict, socket: str = "hand_r") -> dict | None:
    """props.json entry of the prop in `socket` (anim.wielded's rule: the first wields entry with that
    grip_socket); None = unarmed."""
    if not PROPS.exists():
        return None
    props = {p["id"]: p for p in json.loads(PROPS.read_text()).get("props", [])}
    return next((props[w] for w in mon.get("wields") or [] if (props.get(w) or {}).get("grip_socket") == socket), None)


def plan_clips(mon: dict) -> dict:
    """{our clip: UAL clip} for one record: the animation_set entries UAL can serve, plus run."""
    want = set(mon.get("animation_set") or []) | {"idle", "run"}
    out = {c: CLIPS[c] for c in SERVED if c in want}
    if "attack_melee" in out:
        prop = held(mon)
        if prop is None:
            out["attack_melee"] = UNARMED_ATTACK
        elif prop.get("category") in NO_UAL_ATTACK:
            out.pop("attack_melee")
    if mon["id"] in SHAMBLERS or "zombie" in mon["id"]:
        out["idle"], out["walk"] = "Zombie_Idle_Loop", "Zombie_Walk_Fwd_Loop"
        if "attack_melee" in out and held(mon) is None:
            out["attack_melee"] = "Zombie_Scratch"
        out.pop("run", None)
    return out


def bonemap_ok() -> list[str]:
    ids = []
    for p in sorted((ROOT / "out" / SOURCE).glob("*/bonemap.json")):
        b = json.loads(p.read_text())
        if b.get("status") in ("ok", "partial") and (p.parent / "model_rigged.glb").exists():
            ids.append(b["id"])
    return ids


# --- quality gate -------------------------------------------------------------------------

def verdict(m: dict, exclude=()) -> dict:
    """Per clip: use "ual" or "procedural" (the procedural clip stays; for run: no clip), and why.
    exclude: clips forced procedural (override anim_ual_exclude, a visual call)."""
    height = m["height"]
    out = {}
    for c, r in m["clips"].items():
        base = m["procedural"].get("walk" if c == "run" else c)
        why = []
        s = r["edge_stretch_p999"]
        ceil = STRETCH_CEIL["loop" if c in LOOPS else "once"]
        if base:
            lim = max(STRETCH_RATIO * base["edge_stretch_p999"], base["edge_stretch_p999"] + STRETCH_ABS)
            if s > lim:
                why.append(f"stretch {s:.1f} > {lim:.1f} (procedural {base['edge_stretch_p999']:.1f})")
        if s > ceil and not why:
            why.append(f"stretch {s:.1f} > {ceil}")
        if r["min_z"] < -FLOOR_FRAC * height:
            why.append(f"min_z {r['min_z']:+.3f} m")
        pb = m["procedural"].get(c) or {}
        if c in ("idle", "walk") and r["foot_low_min_frac"] > max(pb.get("foot_low_min_frac", 0.0), 0.0) + FOOT_FLOAT_FRAC:
            why.append(f"feet off the floor ({r['foot_low_min_frac']:.3f} h)")
        if c in exclude:
            why.append("excluded by override anim_ual_exclude")
        out[c] = {"ual": r["ual_clip"], "use": "procedural" if why else "ual", "why": why}
    return out


def exclusions(mid: str) -> list:
    sys.path.insert(0, str(ROOT / "tools"))
    import overrides
    return list(overrides.load(SOURCE, mid).get("anim_ual_exclude") or [])


# --- sheets -------------------------------------------------------------------------------

def sheet(tiles: Path, clips: list[str], dst: Path, title: str) -> None:
    """Spike layout: one monster, ours front/side, UAL side, procedural side, 6 frames each."""
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


def batch_sheet(ids: list[str], dst: Path, tag: str = "") -> None:
    """Several monsters: per clip one row, 6 side frames then front frames 0, 2, 4 (start, middle,
    late); the clip label says the UAL source and the gate's verdict."""
    from PIL import Image, ImageDraw
    blocks = []
    for mid in ids:
        tiles = SCRATCH / f"{mid}{tag}_tiles"
        vpath = ROOT / "out" / SOURCE / mid / f"anims_ual{tag}" / "ual.json"
        ver = json.loads(vpath.read_text())["clips"] if vpath.exists() else {}
        rows = []
        for c in [c for c in SERVED if c in ver]:
            ims = [tiles / f"{c}_ours_side_{k}.png" for k in range(6)] + \
                  [tiles / f"{c}_ours_front_{k}.png" for k in (0, 2, 4)]
            if all(p.exists() for p in ims):
                v = ver.get(c, {})
                rows.append((f"{mid}\n{c}\n{v.get('ual', '')[:22]}\n{v.get('use', '?').upper()}", ims))
        blocks.append(rows)
    rows = [r for b in blocks for r in b]
    if not rows:
        return
    tw, th = Image.open(rows[0][1][0]).size
    lab, gap = 130, 8
    out = Image.new("RGB", (lab + 9 * tw + gap, 20 + len(rows) * th), "white")
    d = ImageDraw.Draw(out)
    d.text((4, 4), f"UAL retarget: {', '.join(ids)}  (side x6 | front 0,2,4)", fill="black")
    y = 20
    for b in blocks:
        for name, ims in b:
            d.text((4, y + 6), name, fill="black")
            for k, p in enumerate(ims):
                out.paste(Image.open(p).convert("RGB"), (lab + k * tw + (gap if k >= 6 else 0), y))
            y += th
        d.line([(0, y - 1), (out.width, y - 1)], fill=(0, 0, 0), width=3)
    out.save(dst)


# --- run ----------------------------------------------------------------------------------

def proc_clip(d: Path, c: str) -> Path:
    """The procedural clip (the quality baseline): anims_proc/ once promote() has moved it there."""
    b = d / "anims_proc" / f"{c}.glb"
    return b if b.exists() else d / "anims" / f"{c}.glb"


def run(mid: str, clips: dict, sheets: str, extra: dict, tag: str = "", blender: str = BLENDER,
        quiet: bool = False) -> dict:
    """Retarget one monster. sheets: "spike" (per-monster sheets with UAL and procedural rows),
    "batch" (our tiles only, for batch_sheet) or "none". tag: a variant (A/B runs): clips go to
    anims_ual<tag>/, tiles to <id><tag>_tiles. Returns metrics.json with "verdict"."""
    d = ROOT / "out" / SOURCE / mid
    clips = {"idle": clips.get("idle", CLIPS["idle"]), **{c: u for c, u in clips.items() if c != "idle"}}
    tiles = SCRATCH / f"{mid}{tag}_tiles"
    spike = sheets == "spike"
    job = {"src": str(d / "model_rigged.glb"), "bonemap": str(d / "bonemap.json"), "packs": packs(),
           "clips": clips, "out_dir": str(d / f"anims_ual{tag}"), "tiles": str(tiles) if sheets != "none" else None,
           "sheet_clips": list(clips), "ual_tiles": spike, "proc_tiles": spike, "tile_px": 240 if spike else 160,
           **extra, "baseline": {c: str(proc_clip(d, c)) for c in list(clips) + ["walk"]}}
    shutil.rmtree(tiles, ignore_errors=True)
    t0 = time.time()
    proc = subprocess.run([blender, "-b", "--factory-startup", "--python-exit-code", "1", "-P", str(SCRIPT),
                           "--", json.dumps(job)], capture_output=True, text=True, timeout=3600)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    log = SCRATCH / f"{mid}{tag}.log"
    log.write_text(proc.stdout + proc.stderr)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-3000:] + proc.stderr[-3000:])
        raise RuntimeError(f"{mid}: retarget failed (rc={proc.returncode}), log {log}")
    od = d / f"anims_ual{tag}"
    m = json.loads((od / "metrics.json").read_text())
    v = verdict(m, exclusions(mid))
    (od / "ual.json").write_text(json.dumps({"id": mid, "clips": v, "grip_fix": m.get("grip_fix"),
                                             "seconds": round(time.time() - t0, 1)}, indent=1))
    m["verdict"] = v
    if spike:
        for g, cs in {"move": ["idle", "walk", "run"], "action": ["attack_melee", "hit", "cast", "death"]}.items():
            sheet(tiles, [c for c in cs if c in clips], SCRATCH / f"{mid}{tag}_{g}.png", f"{mid}{tag}: UAL retarget ({g})")
    if not quiet:
        report(mid, m, time.time() - t0)
    return m


def report(mid: str, m: dict, dt: float) -> None:
    print(f"{mid}: {len(m['clips'])} clips in {dt:.0f}s, hip_scale {m['hip_scale']}, grip_fix "
          + " ".join(f"{s}:{g['before_deg']}->{g['after_deg']}deg(roll {g['roll_deg']})" for s, g in (m.get("grip_fix") or {}).items()))
    for c, r in m["clips"].items():
        v = m["verdict"][c]
        print(f"  {c:12s} {r['ual_clip'][:20]:20s} {v['use']:10s} stretch {r['edge_stretch_p999']:5.2f} min_z {r['min_z']:+.3f}"
              f" foot {r['foot_low_min_frac']:+.3f}..{r['foot_low_max_frac']:+.3f} lift {r.get('floor_lift_max', 0):.3f}"
              f" from_idle {r.get('from_idle_m', '-')} to_idle {r.get('to_idle_m', '-')} loop {r.get('loop_delta', '-')}"
              + (f"  [{'; '.join(v['why'])}]" if v["why"] else ""))


# --- promote ------------------------------------------------------------------------------

def _manifest(d: Path) -> dict:
    p = d / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _save_manifest(d: Path, man: dict) -> None:
    tmp = d / "manifest.json.tmp"
    tmp.write_text(json.dumps(man, indent=2) + "\n")
    tmp.replace(d / "manifest.json")


def promote(mid: str, clips: list[str] | None = None, force: bool = False) -> list[str]:
    """Copy anims_ual/<clip>.glb (+ .json) over anims/ for every clip ual.json says to use (or the
    named ones; force: even those the gate rejected). A procedural file it replaces is moved to
    anims_proc/ first (once: a second promote keeps the first backup). manifest.anim gets
    source {clip: "ual"} and the clip's report; unpromote() undoes it."""
    d = ROOT / "out" / SOURCE / mid
    ver = json.loads((d / "anims_ual" / "ual.json").read_text())["clips"]
    pick = [c for c in (clips or ver) if c in ver and (force or ver[c]["use"] == "ual")]
    adir, bdir = d / "anims", d / "anims_proc"
    adir.mkdir(exist_ok=True)
    man = _manifest(d)
    an = man.setdefault("anim", {})
    src = an.setdefault("source", {})
    for c in pick:
        for ext in (".glb", ".json"):
            cur = adir / f"{c}{ext}"
            if cur.exists() and src.get(c) != "ual" and not (bdir / cur.name).exists():
                bdir.mkdir(exist_ok=True)
                shutil.move(str(cur), bdir / cur.name)
            shutil.copy2(d / "anims_ual" / f"{c}{ext}", cur)
        src[c] = "ual"
        an[c] = {**json.loads((d / "anims_ual" / f"{c}.json").read_text()), "source": "ual"}
        if c not in an.setdefault("clips", []):
            an["clips"].append(c)
        if c in an.get("missing", []):
            an["missing"].remove(c)
    _save_manifest(d, man)
    return pick


def unpromote(mid: str) -> list[str]:
    """Undo promote(): restore anims_proc/ files, drop UAL-only clips (run), clear anim.source."""
    d = ROOT / "out" / SOURCE / mid
    adir, bdir = d / "anims", d / "anims_proc"
    man = _manifest(d)
    an = man.get("anim") or {}
    src = an.get("source") or {}
    done = []
    for c, s in list(src.items()):
        if s != "ual":
            continue
        had = False
        for ext in (".glb", ".json"):
            (adir / f"{c}{ext}").unlink(missing_ok=True)
            if (bdir / f"{c}{ext}").exists():
                shutil.move(str(bdir / f"{c}{ext}"), adir / f"{c}{ext}")
                had = True
        if had and (adir / f"{c}.json").exists():
            an[c] = json.loads((adir / f"{c}.json").read_text())
        elif not had:
            an.pop(c, None)
            if c in an.get("clips", []):
                an["clips"].remove(c)
        src.pop(c)
        done.append(c)
    if not src:
        an.pop("source", None)
    if bdir.exists() and not any(bdir.iterdir()):
        bdir.rmdir()
    _save_manifest(d, man)
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--all", action="store_true", help="every rigged biped whose bonemap is ok/partial")
    ap.add_argument("--skip", nargs="*", default=["medusa"])
    ap.add_argument("--clips", nargs="+", help=f"of {list(CLIPS)} or ours=UAL_name (default: from the registry)")
    ap.add_argument("--sheets", choices=("batch", "spike", "none"), default="batch")
    ap.add_argument("--per-sheet", type=int, default=2, help="monsters per batch sheet")
    ap.add_argument("--sheet-name", default="batch", help="batch sheets: <name>_<k>.png")
    ap.add_argument("--jobs", type=int, default=1, help="Blender runs in parallel")
    ap.add_argument("--tag", default="", help="variant suffix for the output dir and sheets (A/B runs)")
    ap.add_argument("--leg-mode", choices=("delta", "dir"), default="delta")
    ap.add_argument("--arm-mode", choices=("delta", "dir"), default="dir")
    ap.add_argument("--hip-scale", type=float, default=None)
    ap.add_argument("--ref", default="Idle_Loop", help="UAL clip whose first frame the deltas are measured from, or tpose")
    ap.add_argument("--no-floor", action="store_true", help="do not raise the body out of the ground")
    ap.add_argument("--no-grip-fix", action="store_true")
    ap.add_argument("--no-lead", action="store_true", help="no lead-in/out on the one-shots")
    ap.add_argument("--no-phase", action="store_true", help="loops start at UAL's frame 0, not the idle's nearest")
    ap.add_argument("--promote", action="store_true", help="copy the passing clips from anims_ual/ to anims/")
    ap.add_argument("--unpromote", action="store_true", help="restore the procedural clips")
    ap.add_argument("--force", action="store_true", help="--promote: also clips the quality gate rejected")
    ap.add_argument("--regate", action="store_true", help="redo ual.json from metrics.json (no Blender)")
    a = ap.parse_args()
    ids = (bonemap_ok() if a.all else []) + a.ids
    ids = [i for i in dict.fromkeys(ids) if i not in a.skip]
    if a.regate:
        for mid in ids:
            od = ROOT / "out" / SOURCE / mid / f"anims_ual{a.tag}"
            if not (od / "metrics.json").exists():
                continue
            m = json.loads((od / "metrics.json").read_text())
            u = json.loads((od / "ual.json").read_text()) if (od / "ual.json").exists() else {"id": mid}
            u["clips"] = m["verdict"] = verdict(m, exclusions(mid))
            (od / "ual.json").write_text(json.dumps(u, indent=1))
            print(f"{mid}: " + " ".join(f"{c}={v['use'][0].upper()}" for c, v in u["clips"].items()))
        return
    if a.promote or a.unpromote:
        for mid in ids:
            got = promote(mid, [c.split("=")[0] for c in a.clips] if a.clips else None, a.force) if a.promote else unpromote(mid)
            print(f"{mid}: {'promoted' if a.promote else 'restored'} {got}")
        return
    reg = registry()
    extra = {"leg_mode": a.leg_mode, "arm_mode": a.arm_mode, "hip_scale": a.hip_scale, "ref": a.ref,
             "floor": not a.no_floor, "grip_fix": not a.no_grip_fix, "lead": not a.no_lead, "phase": not a.no_phase}

    def one(mid):
        clips = dict(c.split("=", 1) if "=" in c else (c, CLIPS[c]) for c in a.clips) if a.clips else plan_clips(reg[mid])
        t0 = time.time()
        try:
            m = run(mid, clips, a.sheets, extra, a.tag, quiet=True)
        except Exception as e:  # keep going
            print(f"{mid}: FAILED {e}", flush=True)
            return mid, None
        report(mid, m, time.time() - t0)
        sys.stdout.flush()
        return mid, m

    with ThreadPoolExecutor(max(1, a.jobs)) as ex:
        res = dict(ex.map(one, ids))
    if a.sheets == "batch":
        ok = [i for i in ids if res.get(i)]
        for k in range(0, len(ok), a.per_sheet):
            dst = SCRATCH / f"{a.sheet_name}{a.tag}_{k // a.per_sheet:02d}.png"
            batch_sheet(ok[k:k + a.per_sheet], dst, a.tag)
            print("sheet", dst, ok[k:k + a.per_sheet])
    bad = [i for i, m in res.items() if not m]
    if bad:
        raise SystemExit(f"failed: {bad}")


if __name__ == "__main__":
    main()

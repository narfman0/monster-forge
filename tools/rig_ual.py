"""Direct UAL rig spike driver (docs/anim-reuse.md, "Direct UAL rig spike"): rig a biped's mesh to the
Quaternius UAL skeleton itself (tools/blender_rig_ual.py on this machine's Blender) so UAL clips play on
it with no retarget, and compare with the retarget route (anims_ual/).

    python3 -I tools/rig_ual.py archmage goblin-warrior ape flesh-golem
    python3 -I tools/rig_ual.py ape --weights skintokens heat transfer --fit auto --jobs 3
    python3 -I tools/rig_ual.py --compare archmage ape      # table only, from the metrics on disk

Writes out/srd-5.2.1/<id>/rig_ual/<fit>_<weights>/: model_rigged.glb (+ _lod<k>), anims/<clip>.glb
(+ .json; the anim stage's names), metrics.json (fit, landmarks, weights, per-clip metrics). Sheets
(Pillow): ~/.cache/monster-forge-scratch/rigual/<id>_<fit>_<weights>.png. Needs model_rigged.glb (its
mesh is the game mesh; its SkinTokens skeleton is the "skintokens" weight source and the fit's sanity
reference), bonemap.json and the UAL pack (CC0) under ~/.cache/monster-forge-scratch/anim-packs/ual1.
Pipeline (opt-in): override `rig_source: "ual"` or `forge.py rig <id> --rig-source ual` runs stage() below.
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

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import retarget_ual  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "srd-5.2.1"
BLENDER = os.environ.get("BLENDER", "/usr/bin/blender")
SCRIPT = ROOT / "tools" / "blender_rig_ual.py"
SCRATCH = Path.home() / ".cache" / "monster-forge-scratch" / "rigual"
CLIP_ORDER = ["idle", "walk", "run", "attack_melee", "hit", "cast", "death"]


def use_source(source: str) -> None:
    """Work on out/<source>/ and that source's registry, here and in retarget_ual / bonemap (default the SRD)."""
    global SOURCE
    import bonemap
    SOURCE = source
    retarget_ual.use_source(source)
    bonemap.use_source(source)


def ual1() -> str:
    return next(p for p in retarget_ual.packs() if "UAL1" in Path(p).name)


def run(mid: str, fit: str, weights: str, sheets: bool = True, tile_px: int = 160, src: Path | None = None,
        extra: dict | None = None, blender: str = BLENDER, mon: dict | None = None, out_root: Path | None = None,
        tag: str | None = None) -> dict:
    """One rig into out/<source>/<id>/rig_ual/<fit>_<weights>/ (out_root: <out_root>/<id>/<tag>/, sheets
    and logs there too). src: the mesh (default model_rigged.glb, the SkinTokens rig; model.glb for fit
    auto without SkinTokens); extra: more job keys (extras, extra_weights, add_tail: see blender_rig_ual)."""
    d = ROOT / "out" / SOURCE / mid
    mon = mon or retarget_ual.registry()[mid]
    tag = tag or f"{fit}_{weights}"
    out = (Path(out_root) / mid if out_root else d / "rig_ual") / tag
    scratch = Path(out_root) / mid if out_root else SCRATCH
    tiles = scratch / f"{mid}_{tag}_tiles"
    lods = json.loads((d / "lods.json").read_text())["lods"] if (d / "lods.json").exists() else {}
    job = {"id": mid, "src": str(src or d / "model_rigged.glb"), "bonemap": str(d / "bonemap.json"), "ual": ual1(),
           "ual_extra": [p for p in retarget_ual.packs() if p != ual1()],
           "fit": fit, "weights": weights, "sockets": mon.get("sockets") or [],
           "lods": {k: str(d / v["file"]) for k, v in lods.items() if k != "0" and v.get("file")},
           "clips": retarget_ual.plan_clips(mon), "out_dir": str(out), "tiles": str(tiles) if sheets else None,
           "tile_px": tile_px, **(extra or {})}
    t0 = time.time()
    proc = subprocess.run([blender, "-b", "--factory-startup", "--python-exit-code", "1", "-P", str(SCRIPT), "--",
                           json.dumps(job)], capture_output=True, text=True, timeout=3600)
    scratch.mkdir(parents=True, exist_ok=True)
    log = scratch / f"{mid}_{tag}.log"
    log.write_text(proc.stdout + proc.stderr)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-3000:] + proc.stderr[-3000:])
        raise RuntimeError(f"{mid} {tag}: failed (rc={proc.returncode}), log {log}")
    m = json.loads((out / "metrics.json").read_text())
    m["wall_seconds"] = round(time.time() - t0, 1)
    (out / "metrics.json").write_text(json.dumps(m, indent=1))
    if sheets:
        sheet(tiles, [c for c in CLIP_ORDER if c in m["clips"]], scratch / f"{mid}_{tag}.png", f"{mid} {tag}", m)
    return m


# --- extra chains (docs/anim-reuse.md "Extra chains") -----------------------------------------------
EXTRA_PARENTS = {"pelvis", "spine_01", "spine_02", "spine_03", "neck_01", "clavicle_l", "clavicle_r",
                 "upperarm_l", "upperarm_r"}
SOCKET_NAMES = {"hand_l", "hand_r", "grip_l", "grip_r", "head", "back", "hip_l", "hip_r"}
EXTRA_REACH = 0.12  # x height: shorter unmapped chains (ears, horns, toe stubs, finger stubs) follow their parent


def skintokens_src(mid: str) -> Path:
    """The SkinTokens rig: rig/skintokens_rigged.glb once the game files are a UAL rig, else model_rigged.glb."""
    d = ROOT / "out" / SOURCE / mid
    keep = d / "rig" / "skintokens_rigged.glb"
    return keep if keep.exists() else d / "model_rigged.glb"


def auto_extras(mid: str, mon: dict, src: Path | None = None, reach: float = EXTRA_REACH) -> list[dict]:
    """The SkinTokens chains UAL has no bone for, as blender_rig_ual job extras: unmapped bones (not
    sockets, not bonemap's skipped extras) whose nearest mapped ancestor is a trunk, clavicle or
    upper-arm bone, reaching >= reach x height from it. Kind: the rig stage's wing_/tail_ names, else
    by shape (glTF: +Y up, the mesh faces -Z, left on -X): out to the side and not hanging on a winged
    plan = wing; behind the hips off pelvis/spine_01 = tail; hanging off the pelvis = strip; else limb."""
    import bonemap
    d = ROOT / "out" / SOURCE / mid
    bm = json.loads((d / "bonemap.json").read_text())
    sk = bonemap.load_skeleton(src or skintokens_src(mid))
    j, h = sk["joints"], sk["height"] or 1.0
    mp, skipped = bm.get("map") or {}, set(bm.get("extra") or {})
    inv = {v: k for k, v in mp.items()}
    cx = j[inv["pelvis"]]["pos"][0] if "pelvis" in inv else 0.0
    winged = mon.get("body_plan") == "winged_biped"
    out = []
    for r, x in j.items():
        par = x["parent"]
        if r in mp or r in skipped or r in SOCKET_NAMES or par is None or not (par in mp or par in skipped):
            continue
        a = par
        while a and a not in mp:
            a = j[a]["parent"]
        u = mp.get(a)
        if u not in EXTRA_PARENTS:
            continue
        base = np.array(j[par]["pos"])
        sub, stack = [], [r]
        while stack:
            y = stack.pop()
            sub.append(np.array(j[y]["pos"]))
            stack += j[y]["children"]
        dv = np.array(sub) - base
        k = int(np.argmax(np.linalg.norm(dv, axis=1)))
        rr = float(np.linalg.norm(dv[k])) / h
        if rr < reach and not r.startswith(("wing_", "tail_")):
            continue
        t = dv[k] / h
        side = "l" if base[0] + dv[k][0] < cx else "r"
        if r.startswith("wing_"):
            kind, side = "wing", r[5]
        elif r.startswith("tail_"):
            kind = "tail"
        elif winged and abs(t[0]) >= 0.15 and t[1] > -0.25 and u != "pelvis":
            kind = "wing"
        elif t[2] >= 0.12 and u in ("pelvis", "spine_01"):
            kind = "tail"
        elif u == "pelvis" and t[1] < 0:
            kind = "strip"
        else:
            kind = "limb"
        out.append({"root": r, "kind": kind, "side": side, "ual_parent": u, "joints": len(sub), "reach": round(rr, 2),
                    "dir": [round(float(v), 2) for v in t]})
    order = {"wing": 0, "tail": 1, "limb": 2, "strip": 3, "head": 4}
    return sorted(out, key=lambda e: (order[e["kind"]], -e["reach"]))


# --- pipeline: forge.py rig with rig_source "ual" ------------------------------------------------
UPRIGHT = {"biped", "winged_biped"}


def variant(fit: str, weights: str, extras: bool) -> str:
    """rig_ual/<variant>/ the stage writes: <fit>_<weights>, + "_x" with extra chains."""
    return f"{fit}_{weights}" + ("_x" if extras else "")


def variant_for(mid: str, source: str | None = None) -> str:
    """The variant the overrides make the stage write (rig_ual_verdict reads that one)."""
    import overrides
    ov = overrides.load(source or SOURCE, mid)
    return variant(ov.get("rig_ual_fit") or "auto", ov.get("rig_ual_weights") or "heat",
                   bool(ov.get("rig_ual_extras") or ov.get("rig_ual_add_tail")))


def rig_source(mon: dict, args=None) -> str:
    """"skintokens" (default) or "ual": forge.py --rig-source wins over the override rig_source."""
    import overrides
    return (getattr(args, "rig_source", None) or overrides.load(mon["source"], mon["id"]).get("rig_source")
            or "skintokens")


def needs_skintokens_rig(mon: dict, d: Path) -> bool:
    """True when this monster's UAL fit reads the SkinTokens rig (rig_ual_fit skintokens, extras, add_tail,
    skintokens weights) and there is none for the current mesh: no rig/skintokens_rigged.glb and no
    SkinTokens model_rigged.glb (after a re-roll the mesh stage clears both)."""
    import overrides
    ov = overrides.load(mon["source"], mon["id"])
    if not (ov.get("rig_ual_fit") == "skintokens" or ov.get("rig_ual_extras") or ov.get("rig_ual_add_tail")
            or ov.get("rig_ual_weights") == "skintokens"):
        return False
    if (d / "rig" / "skintokens_rigged.glb").exists():
        return False
    man = fg_manifest(d)
    return not ((d / "model_rigged.glb").exists() and (man.get("rig") or {}).get("tool") != "ual")


def fg_manifest(d: Path) -> dict:
    p = d / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else {}


def stage(fg, mon: dict, d: Path, args) -> None:
    """forge.py rig for rig_source "ual" (opt-in; bipeds and winged bipeds): rig the mesh to the UAL
    skeleton with heat weights, no SkinTokens roll. Fit: override rig_ual_fit, "auto" (default, from
    model.glb alone; failed on goblin-warrior's big head) or "skintokens" (the joints of an existing
    SkinTokens rig: model_rigged.glb is kept once as rig/skintokens_rigged.glb and bonemap.json made
    from it). Overrides rig_ual_weights (heat | skintokens), rig_ual_extras ("auto" or a list: SkinTokens
    chains for wings, tails, extra limbs, strips, or a second head from the mesh, copied onto the UAL
    skeleton with their own weights), rig_ual_extra_weights and rig_ual_add_tail also read the SkinTokens
    rig (docs/anim-reuse.md "Extra chains"). Writes rig_ual/<variant()>/ (metrics, clips), copies its model_rigged*.glb over the game
    files and its clips into anims_ual/ with ual.json (retarget_ual.verdict: stretch ceilings, floor,
    anim_ual_exclude; no procedural baseline), so `forge.py promote <id>` works unchanged. The old
    skeleton's clips (anims/, anims_proc/, manifest.anim) are dropped: they name bone_N joints."""
    import bonemap
    import overrides
    import rig
    mid = mon["id"]
    if mon["body_plan"] not in UPRIGHT:
        raise RuntimeError(f"{mid}: rig_source ual is for bipeds/winged bipeds, not {mon['body_plan']}")
    use_source(mon.get("source") or SOURCE)
    if d.resolve() != (ROOT / "out" / SOURCE / mid).resolve():
        raise RuntimeError(f"{mid}: rig_source ual writes out/{SOURCE}/<id>/ only (no --out-suffix)")
    if not (d / "model.glb").exists():
        raise RuntimeError(f"{mid}: no model.glb; run `qa` first")
    man = fg.load_manifest(d)
    lods = json.loads((d / "lods.json").read_text())["lods"] if (d / "lods.json").exists() else {}
    done = (d / "model_rigged.glb").exists() and all((d / f"model_rigged_lod{k}.glb").exists() for k in lods if k != "0")
    if done and (man.get("rig") or {}).get("tool") == "ual" and not args.force:
        fg.log(f"{mid}: UAL rig exists, skip")
        return
    ov = overrides.load(mon["source"], mid)
    fit = ov.get("rig_ual_fit") or "auto"
    weights = ov.get("rig_ual_weights") or "heat"
    xspec, add_tail = ov.get("rig_ual_extras"), bool(ov.get("rig_ual_add_tail"))
    src = d / "model.glb"
    keep0 = d / "rig" / "skintokens_rigged.glb"
    if not keep0.exists() and (d / "model_rigged.glb").exists() and (man.get("rig") or {}).get("tool") != "ual":
        keep0.parent.mkdir(exist_ok=True)  # every fit: the SkinTokens game rig stays, for a later compare or revert
        shutil.copy2(d / "model_rigged.glb", keep0)
    if fit == "skintokens" or xspec or add_tail or weights == "skintokens":  # these read the SkinTokens rig
        keep = d / "rig" / "skintokens_rigged.glb"
        if not keep.exists():
            if not (d / "model_rigged.glb").exists() or (man.get("rig") or {}).get("tool") == "ual":
                raise RuntimeError(f"{mid}: rig_ual_fit skintokens needs a SkinTokens model_rigged.glb (run rig with "
                                   f"rig_source skintokens first)")
            if not (d / "bonemap.json").exists():
                bonemap.run(mid)
            keep.parent.mkdir(exist_ok=True)
            shutil.copy2(d / "model_rigged.glb", keep)
        # a re-meshed record (frost-giant, 2026-11-08): the stage rigged it with SkinTokens again first, but the
        # bonemap.json of the old SkinTokens rig was kept and named bones the new rig does not have (KeyError
        # in fit_skintokens). Map again whenever the kept SkinTokens rig is newer than the map.
        bm = d / "bonemap.json"
        if (man.get("rig") or {}).get("tool") != "ual" and (d / "model_rigged.glb").exists() and \
                (not bm.exists() or bm.stat().st_mtime < keep.stat().st_mtime):
            bonemap.run(mid)
        if fit == "skintokens" and json.loads((d / "bonemap.json").read_text()).get("status") not in ("ok", "partial"):
            raise RuntimeError(f"{mid}: bonemap.json status is not ok/partial")
        src = keep
    tex = d / "textures"
    albedo = next((p for p in (tex / "base_color_painted.png", tex / "base_color.png") if p.exists()), None)
    extras = auto_extras(mid, mon, src) if xspec == "auto" else list(xspec or [])
    extra = {"base_color": str(albedo) if albedo else None}
    if extras or add_tail:
        extra.update(extras=[{k: e[k] for k in ("root", "kind", "side", "parent", "auto") if k in e} for e in extras],
                     extra_weights=ov.get("rig_ual_extra_weights") or "skintokens", add_tail=add_tail)
    t0 = time.time()
    tag = variant(fit, weights, bool(extras or add_tail))
    m = run(mid, fit, weights, sheets=False, src=src, blender=fg.BLENDER, mon=mon, extra=extra, tag=tag)
    out = d / "rig_ual" / tag
    rig.clear_rig_outputs(d)
    for f in out.glob("model_rigged*.glb"):
        shutil.copy2(f, d / f.name)
    for sub in ("anims", "anims_proc", "anims_ual"):
        shutil.rmtree(d / sub, ignore_errors=True)
    shutil.copytree(out / "anims", d / "anims_ual")
    m["procedural"] = {}
    (d / "anims_ual" / "metrics.json").write_text(json.dumps(m, indent=1))
    v = retarget_ual.verdict(m, retarget_ual.exclusions(mid))
    (d / "anims_ual" / "ual.json").write_text(json.dumps({"id": mid, "clips": v, "rig_source": "ual"}, indent=1))
    man = fg.load_manifest(d)
    man.pop("anim", None)
    r = m["rig"]
    nx = sum(len(e["bones"]) for e in r.get("extras") or [])
    man["rig"] = {"tool": "ual", "fit": fit, "weights": weights, "joints": 65 + nx + len([x for x in r.get("sockets") or [] if x not in ("hand_l", "hand_r")]),
                  **({"extras": {e["prefix"]: {"kind": e["kind"], "parent": e["parent"], "bones": len(e["bones"]), "from": e["root"]}
                                 for e in r["extras"]}} if r.get("extras") else {}),
                  "deform_groups": r.get("groups"), "legs": 2,
                  "sockets": r.get("sockets"), "sockets_missing": [s for s in (mon.get("sockets") or []) if s not in (r.get("sockets") or [])],
                  "seconds": round(time.time() - t0, 1), "hip_scale": m.get("hip_scale"),
                  "vs_skintokens_median": r.get("vs_skintokens_median"), "unweighted_verts": r.get("unweighted_verts")}
    man.setdefault("timings", {})["rig"] = man["rig"]["seconds"]
    rig.clear_stage_errors(man, "rig")
    fg.save_manifest(d, man)
    fg.log(f"{mid}: UAL rig ({fit} fit, {weights} weights{f', {nx} extra bones' if nx else ''}) in {man['rig']['seconds']}s; clips in anims_ual/: "
           + ", ".join(f"{c}={x['use']}" for c, x in v.items()) + f"; promote with `forge.py promote {mid}`")


def sheet(tiles: Path, clips: list[str], dst: Path, title: str, m: dict) -> None:
    """Fit (x-ray, red = our joints, green = SkinTokens') and rest, then per clip 6 side and 6 front
    frames."""
    from PIL import Image, ImageDraw
    rows = [("fit / rest", [tiles / f"fit_{v}.png" for v in ("front", "side")] + [tiles / f"rest_{v}.png" for v in ("front", "side")])]
    for c in clips:
        r = m["clips"][c]
        rows.append((f"{c}\n{r['ual_clip'][:20]}\nstretch {r['edge_stretch_p999']}\nmin_z {r['min_z']}",
                     [tiles / f"{c}_side_{k}.png" for k in range(6)] + [tiles / f"{c}_front_{k}.png" for k in range(6)]))
    rows = [(n, [p for p in ims if p.exists()]) for n, ims in rows]
    tw, th = Image.open(rows[1][1][0]).size if len(rows) > 1 else Image.open(rows[0][1][0]).size
    lab = 120
    out = Image.new("RGB", (lab + 12 * tw, 20 + len(rows) * th), "white")
    d = ImageDraw.Draw(out)
    d.text((4, 4), f"{title}: direct UAL rig (side x6 | front x6); left blue, right orange, blade on grip_r", fill="black")
    for r, (name, ims) in enumerate(rows):
        y = 20 + r * th
        d.text((4, y + 6), name, fill="black")
        for k, p in enumerate(ims):
            out.paste(Image.open(p).convert("RGB").resize((tw, th)), (lab + k * tw, y))
        d.line([(0, y), (out.width, y)], fill=(160, 160, 160))
    out.save(dst)
    fits = [Image.open(tiles / f"fit_{v}.png").convert("RGB") for v in ("front", "side") if (tiles / f"fit_{v}.png").exists()]
    if fits:  # the fit at full size: red = our joints, green = SkinTokens' mapped joints
        f = Image.new("RGB", (sum(i.width for i in fits), fits[0].height), "white")
        for k, i in enumerate(fits):
            f.paste(i, (k * fits[0].width, 0))
        f.save(dst.with_name(dst.stem + "_fit.png"))


def compare(mid: str, tags: list[str]) -> None:
    """Per clip: edge stretch p99.9 / lowest vertex / feet for each direct-rig variant beside the
    retarget (anims_ual) and the procedural clip."""
    d = ROOT / "out" / SOURCE / mid
    rt = json.loads((d / "anims_ual" / "metrics.json").read_text()) if (d / "anims_ual" / "metrics.json").exists() else {}
    vs = {t: json.loads((d / "rig_ual" / t / "metrics.json").read_text()) for t in tags if (d / "rig_ual" / t / "metrics.json").exists()}
    if not vs:
        return
    h = next(iter(vs.values()))["height"]
    print(f"\n{mid} (h {h} m)  stretch p99.9 | min_z m | foot low min..max (x h)")
    for t, m in vs.items():
        r = m["rig"]
        print(f"  {t}: fit vs SkinTokens median {r.get('vs_skintokens_median')} max {r.get('vs_skintokens_max')} (x h), "
              f"robe {r.get('robe')}, unweighted {r.get('unweighted_verts')}, hip_scale {r.get('hip_scale')}, "
              f"{r.get('seconds')}s (weights {r.get('weights_seconds')}s)")
    for c in CLIP_ORDER:
        cells = []
        for t, m in vs.items():
            x = m["clips"].get(c)
            if x:
                cells.append(f"{t} {x['edge_stretch_p999']:5.2f} {x['min_z']:+.3f} {x['foot_low_min_frac']:+.3f}..{x['foot_low_max_frac']:+.3f}")
        x = (rt.get("clips") or {}).get(c)
        if x:
            cells.append(f"retarget {x['edge_stretch_p999']:5.2f} {x['min_z']:+.3f} {x['foot_low_min_frac']:+.3f}..{x['foot_low_max_frac']:+.3f}")
        x = (rt.get("procedural") or {}).get(c)
        if x:
            cells.append(f"proc {x['edge_stretch_p999']:5.2f}")
        if cells:
            print(f"  {c:12s} " + " | ".join(cells))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="+")
    ap.add_argument("--fit", nargs="+", default=["auto"], choices=("auto", "skintokens"))
    ap.add_argument("--weights", nargs="+", default=["skintokens"], choices=("skintokens", "heat", "transfer"))
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--no-sheets", action="store_true")
    ap.add_argument("--compare", action="store_true", help="print the comparison from the metrics on disk only")
    a = ap.parse_args()
    tags = [f"{f}_{w}" for f in a.fit for w in a.weights]
    if a.compare:
        for mid in a.ids:
            compare(mid, tags if (a.fit != ["auto"] or a.weights != ["skintokens"]) else
                    sorted(p.name for p in (ROOT / "out" / SOURCE / mid / "rig_ual").glob("*_*")))
        return
    work = [(mid, f, w) for mid in a.ids for f in a.fit for w in a.weights]

    def one(x):
        mid, f, w = x
        t0 = time.time()
        try:
            run(mid, f, w, not a.no_sheets)
            print(f"{mid} {f}_{w}: done in {time.time() - t0:.0f}s", flush=True)
            return True
        except Exception as e:
            print(f"{mid} {f}_{w}: FAILED {e}", flush=True)
            return False

    with ThreadPoolExecutor(max(1, a.jobs)) as ex:
        ok = list(ex.map(one, work))
    for mid in a.ids:
        compare(mid, tags)
    if not all(ok):
        raise SystemExit("some runs failed")


if __name__ == "__main__":
    main()

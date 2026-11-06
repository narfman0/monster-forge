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
"""
import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import retarget_ual  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "srd-5.2.1"
BLENDER = os.environ.get("BLENDER", "/usr/bin/blender")
SCRIPT = ROOT / "tools" / "blender_rig_ual.py"
SCRATCH = Path.home() / ".cache" / "monster-forge-scratch" / "rigual"
CLIP_ORDER = ["idle", "walk", "run", "attack_melee", "hit", "cast", "death"]


def ual1() -> str:
    return next(p for p in retarget_ual.packs() if "UAL1" in Path(p).name)


def run(mid: str, fit: str, weights: str, sheets: bool = True, tile_px: int = 160) -> dict:
    d = ROOT / "out" / SOURCE / mid
    mon = retarget_ual.registry()[mid]
    tag = f"{fit}_{weights}"
    out = d / "rig_ual" / tag
    tiles = SCRATCH / f"{mid}_{tag}_tiles"
    lods = json.loads((d / "lods.json").read_text())["lods"] if (d / "lods.json").exists() else {}
    job = {"id": mid, "src": str(d / "model_rigged.glb"), "bonemap": str(d / "bonemap.json"), "ual": ual1(),
           "fit": fit, "weights": weights, "sockets": mon.get("sockets") or [],
           "lods": {k: str(d / v["file"]) for k, v in lods.items() if k != "0" and v.get("file")},
           "clips": retarget_ual.plan_clips(mon), "out_dir": str(out), "tiles": str(tiles) if sheets else None,
           "tile_px": tile_px}
    t0 = time.time()
    proc = subprocess.run([BLENDER, "-b", "--factory-startup", "--python-exit-code", "1", "-P", str(SCRIPT), "--",
                           json.dumps(job)], capture_output=True, text=True, timeout=3600)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    log = SCRATCH / f"{mid}_{tag}.log"
    log.write_text(proc.stdout + proc.stderr)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-3000:] + proc.stderr[-3000:])
        raise RuntimeError(f"{mid} {tag}: failed (rc={proc.returncode}), log {log}")
    m = json.loads((out / "metrics.json").read_text())
    m["wall_seconds"] = round(time.time() - t0, 1)
    (out / "metrics.json").write_text(json.dumps(m, indent=1))
    if sheets:
        sheet(tiles, [c for c in CLIP_ORDER if c in m["clips"]], SCRATCH / f"{mid}_{tag}.png", f"{mid} {tag}", m)
    return m


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

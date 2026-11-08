"""Side-view lean check: does a monster lean back (or pitch nose-up) at rest and on its idle's
first frame? Renders an orthographic profile of each model (head to the right) and measures
the lean in the side plane (tools/blender_lean.py: angles, + forward / - back), then writes
summary.json / summary.tsv and contact sheets with the support, centroid and spine drawn on.

    python3 -I tools/lean_check.py                       # every monster with a model.glb
    python3 -I tools/lean_check.py ogre mage --tag test  # some, into another scratch dir
    python3 -I tools/lean_check.py --root ~/.cache/monster-forge-scratch/lean/copies --tag after
    python3 -I tools/lean_check.py --sheets-only         # redraw sheets from the json

Output: ~/.cache/monster-forge-scratch/lean/<tag>/ (never /tmp: it is RAM-backed):
<id>_model.png/json (model.glb at rest), <id>_idle.png/json (model_rigged.glb on
anims/idle.glb's first frame), sheet_<group>_<n>.png, summary.tsv. At most --jobs (8)
Blender processes at once (12+ concurrent Blender jobs segfault). Read-only on the inputs.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import overrides  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BLENDER = os.environ.get("BLENDER", "/usr/bin/blender")
SCRATCH = Path.home() / ".cache" / "monster-forge-scratch" / "lean"
UPRIGHT = {"biped", "winged_biped", "floating", "merfolk_naga"}
HORIZONTAL = {"quadruped", "winged_quadruped", "centauroid", "insectoid", "avian", "aquatic"}
# Flags (a hint for the eye, not a verdict): upright bodies whose column (tools/lean.py
# core_lean) leans back past LEAN_BACK; horizontal bodies whose trunk (pitch_torso) pitches
# nose-up past PITCH_UP.
LEAN_BACK = -5.0
PITCH_UP = 6.0


def flag(r: dict, plan: str) -> str:
    if "error" in r or "lean_com" not in r:
        return ""
    if plan in UPRIGHT:
        if r.get("core_lean") is not None and r["core_lean"] < LEAN_BACK:
            return "BACK"
    elif plan in HORIZONTAL:
        if r.get("pitch_torso") is not None and r["pitch_torso"] > PITCH_UP:
            return "NOSE_UP"
    return ""


def items_for(mid: str, d: Path, out: Path, kinds: list[str]) -> list[dict]:
    ov = overrides.load("srd-5.2.1", mid) if overrides.ID_RE.match(mid) else {}
    fwd = -1 if ov.get("forward") == "+Z" else 1
    its = []
    if "model" in kinds and (d / "model.glb").exists():
        its.append({"id": mid, "kind": "model", "src": str(d / "model.glb"), "fwd": fwd})
    if "idle" in kinds and (d / "model_rigged.glb").exists() and (d / "anims" / "idle.glb").exists():
        its.append({"id": mid, "kind": "idle", "src": str(d / "model_rigged.glb"),
                    "clip": str(d / "anims" / "idle.glb"), "fwd": fwd})
    for it in its:
        it["png"] = str(out / f"{mid}_{it['kind']}.png")
        it["json"] = str(out / f"{mid}_{it['kind']}.json")
    return its


def run_blender(items: list[dict], px: int) -> str:
    job = {"items": items, "px": px}
    p = subprocess.run([BLENDER, "-b", "--factory-startup", "-P", str(ROOT / "tools" / "blender_lean.py"),
                        "--", json.dumps(job)], capture_output=True, text=True, timeout=3600)
    if "LEAN_OK" not in p.stdout:
        return f"rc={p.returncode} {p.stdout[-800:]} {p.stderr[-800:]}"
    return ""


def font(size):
    from PIL import ImageFont
    for p in ("/usr/share/fonts/google-noto/NotoSans-Regular.ttf",
              "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
              "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf"):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def tile(r: dict, png: Path, plan: str, size: int):
    """The side render with the support (green), support -> centroid (red), hips -> head (blue)."""
    from PIL import Image, ImageDraw
    im = Image.open(png).convert("RGB")
    v = r["view"]
    S, cy, cz, px = v["S"], v["cy"], v["cz"], v["px"]

    def P(y, z):
        return ((y - cy) / S + 0.5) * px, (0.5 - (z - cz) / S) * px

    dr = ImageDraw.Draw(im)
    z0 = r["z0"]
    dr.line([P(cy - S, z0), P(cy + S, z0)], fill=(90, 90, 90), width=1)
    a, b = r["support_span"]
    dr.line([P(a, z0), P(b, z0)], fill=(0, 170, 0), width=4)
    dr.line([P(r["support_y"], z0), P(r["support_y"], z0 + r["height"])], fill=(0, 170, 0), width=1)
    dr.line([P(r["support_y"], z0), P(*r["com"])], fill=(220, 0, 0), width=2)
    c = P(*r["com"])
    dr.ellipse([c[0] - 4, c[1] - 4, c[0] + 4, c[1] + 4], fill=(220, 0, 0))
    if r.get("hips") and r.get("head"):
        dr.line([P(*r["hips"]), P(*r["head"])], fill=(0, 60, 230), width=1)
    for z, y in (r.get("core_slices") or []) if plan not in HORIZONTAL else []:
        c = P(y, z)
        dr.ellipse([c[0] - 2, c[1] - 2, c[0] + 2, c[1] + 2], fill=(255, 140, 0))
    for y, z in r.get("torso_pts") or []:
        c = P(y, z)
        dr.ellipse([c[0] - 3, c[1] - 3, c[0] + 3, c[1] + 3], fill=(160, 0, 200))
    if r.get("core_lean") is not None and plan not in HORIZONTAL:  # the column's lean as a line from the support up
        t = math.radians(r["core_lean"])
        h = r["height"]
        dr.line([P(r["support_y"], z0), P(r["support_y"] + h * math.tan(t), z0 + h)], fill=(255, 140, 0), width=2)
    return im.resize((size, size))


def label(r: dict, plan: str) -> str:
    if "error" in r:
        return f"{r['id']} {r['kind']}: ERROR"
    if plan in HORIZONTAL:
        return f"{r['id']} {r['kind'][0]} torso pitch {r.get('pitch_torso') or 0:+.1f}"
    cl = r.get("core_lean")
    return f"{r['id']} {r['kind'][0]} core {cl:+.1f} spine {r.get('lean_spine') or 0:+.1f}" if cl is not None else r["id"]


def sheets(out: Path, rows: list[dict], name: str, size=256, cols=6, per=36):
    from PIL import Image, ImageDraw
    f = font(13)
    paths = []
    for n in range(0, len(rows), per):
        chunk = rows[n:n + per]
        nr = (len(chunk) + cols - 1) // cols
        sh = Image.new("RGB", (cols * size, nr * (size + 22)), "white")
        dr = ImageDraw.Draw(sh)
        for i, (r, plan, fl) in enumerate(chunk):
            x, y = (i % cols) * size, (i // cols) * (size + 22)
            png = out / f"{r['id']}_{r['kind']}.png"
            if "error" not in r and png.exists():
                sh.paste(tile(r, png, plan, size), (x, y + 22))
            dr.text((x + 3, y + 3), label(r, plan), fill=(200, 0, 0) if fl else "black", font=f)
            dr.rectangle([x, y, x + size - 1, y + size + 21], outline=(170, 170, 170))
        p = out / f"sheet_{name}_{n // per}.png"
        sh.save(p)
        paths.append(p)
    return paths


def compare_sheets(before: Path, out: Path, rows: list, size: int, cols: int) -> list[Path]:
    """sheet_compare_<kind>_<n>.png: pairs of tiles, before (left, from `before`) and after."""
    from PIL import Image, ImageDraw
    f = font(13)
    made = []
    for kind in sorted({r["kind"] for r, _, _ in rows}):
        pairs = []
        for r, plan, _ in rows:
            pj = before / f"{r['id']}_{kind}.json"
            if r["kind"] == kind and pj.exists():
                pairs.append((json.loads(pj.read_text()), r, plan))
        pairs.sort(key=lambda t: t[0].get("core_lean") or 0)
        pc = max(1, cols // 2)
        per = pc * 5
        for n in range(0, len(pairs), per):
            chunk = pairs[n:n + per]
            nr = (len(chunk) + pc - 1) // pc
            sh = Image.new("RGB", (pc * 2 * size, nr * (size + 22)), "white")
            dr = ImageDraw.Draw(sh)
            for i, (b, r, plan) in enumerate(chunk):
                x, y = (i % pc) * 2 * size, (i // pc) * (size + 22)
                for j, (rr, d) in enumerate(((b, before), (r, out))):
                    png = d / f"{rr['id']}_{kind}.png"
                    if "error" not in rr and png.exists():
                        sh.paste(tile(rr, png, plan, size), (x + j * size, y + 22))
                cb, ca = b.get("core_lean"), r.get("core_lean")
                dr.text((x + 3, y + 3), f"{r['id']} {kind}: {cb if cb is None else f'{cb:+.1f}'} -> "
                        f"{ca if ca is None else f'{ca:+.1f}'} deg (before | after)", fill="black", font=f)
                dr.rectangle([x, y, x + 2 * size - 1, y + size + 21], outline=(150, 150, 150))
            p = out / f"sheet_compare_{kind}_{n // per}.png"
            sh.save(p)
            made.append(p)
    return made


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--root", default=str(ROOT / "out" / "srd-5.2.1"), help="dir of <id>/ outputs")
    ap.add_argument("--tag", default="before", help="scratch subdir under " + str(SCRATCH))
    ap.add_argument("--jobs", type=int, default=8, help="Blender processes at once (max 8)")
    ap.add_argument("--kinds", default="model,idle")
    ap.add_argument("--px", type=int, default=384)
    ap.add_argument("--sheets-only", action="store_true")
    ap.add_argument("--compare", metavar="TAG", help="also write before/after sheets: each monster's render in "
                    "<TAG>/ (before) next to this run's (after), same kind")
    ap.add_argument("--all-sheets", action="store_true", help="sheets of every monster, not only flagged ones")
    ap.add_argument("--tile", type=int, default=256, help="sheet tile size (px)")
    ap.add_argument("--cols", type=int, default=6)
    a = ap.parse_args()
    root, out = Path(a.root).expanduser(), SCRATCH / a.tag
    out.mkdir(parents=True, exist_ok=True)
    ids = a.ids or sorted(p.name for p in root.iterdir() if p.is_dir() and (p / "model.glb").exists())
    kinds = a.kinds.split(",")
    plans = {}
    for mid in ids:  # a scratch copy without a manifest takes the plan from out/
        m = next((p for p in (root / mid / "manifest.json", ROOT / "out" / "srd-5.2.1" / mid / "manifest.json")
                  if p.exists()), None)
        plans[mid] = json.loads(m.read_text()).get("body_plan", "") if m else ""
    if not a.sheets_only:
        items = [it for mid in ids for it in items_for(mid, root / mid, out, kinds)]
        nj = max(1, min(8, a.jobs))
        chunks = [items[i::nj] for i in range(nj)]
        with ThreadPoolExecutor(nj) as ex:
            for err in ex.map(lambda c: run_blender(c, a.px) if c else "", chunks):
                if err:
                    print("blender batch failed:", err, file=sys.stderr)
    rows = []
    for mid in ids:
        for k in kinds:
            p = out / f"{mid}_{k}.json"
            if p.exists():
                r = json.loads(p.read_text())
                rows.append((r, plans[mid], flag(r, plans[mid])))
    with open(out / "summary.tsv", "w") as f:
        f.write("id\tkind\tplan\tflag\tcore_lean\tlean_spine\tlean_com\tlean_upper\tlean_axis\tcom_behind\tpitch_body\tpitch_torso\n")
        for r, plan, fl in rows:
            f.write("\t".join(str(x) for x in (r["id"], r["kind"], plan, fl, r.get("core_lean"), r.get("lean_spine"), r.get("lean_com"),
                                                 r.get("lean_upper"), r.get("lean_axis"), r.get("com_behind"),
                                                 r.get("pitch_body"), r.get("pitch_torso"))) + "\n")
    (out / "summary.json").write_text(json.dumps([dict(r, plan=p, flag=fl) for r, p, fl in rows], indent=1))

    def key(t):
        r, plan, _ = t
        return (r.get("pitch_torso") or 0) * -1 if plan in HORIZONTAL else (r.get("core_lean") or 0)
    groups = {"upright": [t for t in rows if t[1] in UPRIGHT], "horizontal": [t for t in rows if t[1] in HORIZONTAL],
              "other": [t for t in rows if t[1] not in UPRIGHT | HORIZONTAL]}
    made = []
    for g, rs in groups.items():
        for k in kinds:
            sel = [t for t in rs if t[0]["kind"] == k and (a.all_sheets or t[2])]
            made += sheets(out, sorted(sel, key=key), f"{g}_{k}{'' if a.all_sheets else '_flagged'}",
                           size=a.tile, cols=a.cols, per=a.cols * 6)
    if a.compare:
        made += compare_sheets(SCRATCH / a.compare, out, rows, a.tile, a.cols)
    nflag = sum(1 for t in rows if t[2])
    print(f"{len(rows)} renders, {nflag} flagged; {out / 'summary.tsv'}")
    for p in made:
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
